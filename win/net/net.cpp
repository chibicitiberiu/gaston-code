#include <windows.h>
#include <winsock.h>
#include <stdio.h>
#include <string.h>
#include "core/rc_cpp.h"
#include "net/net.h"
#include "fs/drives.h"
#include "net/caps.h"
#include <new>
#include "core/heap.h"
#include "exec/system.h"
#include "core/log.h"
#include "core/str.h"
#include "rpc/rpc.h"
#include "sync/syncrpc.h"
#include "wire/connection.h"

/* Cap on frames posted to the UI thread but not yet freed.
 *
 * PostMessage CAN fail -- Win9x message queues are bounded and smaller than
 * NT's -- and a modal dialog running its own message loop is exactly when the
 * reader keeps recv-ing while the UI drains slowly. Without a cap the reader
 * outruns the UI and every failed post leaks. */
/* 64, not 256, and the number now comes from the memory budget rather than
 * from nowhere. Each posted frame holds its payload -- up to RC_MAX_FRAME --
 * until the UI thread frees it, so the cap IS a memory bound: 64 x 64KB is a
 * 4MB worst case against 256's 16MB, on a machine with 32MB total. Typical
 * cost is far lower (the bridge coalesces to ~2KB frames), so this changes
 * nothing in normal use; it lowers the ceiling for the case that actually
 * hurts, which is the UI thread stalled in an MSHTML reflow or behind a modal
 * folder browser while the reader keeps draining the socket. Hitting the cap
 * blocks the reader, which is ordinary TCP backpressure and the behaviour
 * this counter exists to produce. */
#define RC_MAX_INFLIGHT 64

static void set_state(rc_net *n, rc_state s)
{
    n->state = s;
    if (n->notify) PostMessage(n->notify, WM_RC_STATE, (WPARAM)s, 0);
}

/* The client's FrameSink, and the Connection it feeds: one allocation per
 * connection attempt, owned through rc_net's opaque `conn`. A reconnect makes
 * a new one; one whose threads would not stop is leaked, never reused. */
class ClientLink : public FrameSink
{
public:
    explicit ClientLink(rc_net *n) : m_net(n), m_conn(this, true) {}

    Connection &conn() { return m_conn; }

    void onFrame(Connection &c, rc_frame *f);
    void onState(Connection &c, rc_state s)
    {
        (void)c;
        set_state(m_net, s);
    }

private:
    rc_net *m_net;
    Connection m_conn;
};

static Connection *conn_of(rc_net *n)
{
    return n->conn ? &((ClientLink *)n->conn)->conn() : NULL;
}

/* THE READER THREAD, holding a frame it now owns. */
void ClientLink::onFrame(Connection &c, rc_frame *frame)
{
    rc_net *n = m_net;
    HeapPtr<rc_frame> f(frame);

    /* Ask the RPC layer whether this frame is one of its own.
     *
     * EXEC goes straight to the RPC worker and never reaches the UI thread.
     * The type is already parsed here, so routing it now saves a hop and a
     * copy -- and, more importantly, `inflight` is the wrong instrument for
     * it: that counter measures UI QUEUE DEPTH for the backpressure gate
     * below, and holding it for the sixty seconds a command might run would
     * conflate two unrelated queues and stall display frames behind a
     * compile. The worker has its own counter.
     *
     * The reader loop used to hard-code EXEC, EXECKILL and the SYNC prefix
     * -- the transport knowing the names of the tools it carries. The
     * vocabulary lives in rc_rpc_route; this only knows that something else
     * may want the frame, and that if it is taken it has also been freed. */
    if (rc_rpc_route(f.get(), c.runningFlag()) == RC_ROUTE_TAKEN) {
        /* Ownership genuinely transferred: rc_rpc_route has ALREADY freed
         * the frame by the time it returns RC_ROUTE_TAKEN (rpc.h), whether
         * by handing it to the worker or by freeing it itself on a refusal
         * path. release() so this guard does not free it a second time --
         * that double free is exactly the failure mode net.h's three-way
         * ownership rule exists to rule out. */
        f.release();
        return;
    }

    /* Backpressure: if the UI is not keeping up, wait for it rather than
     * queueing without bound. Yielding here is correct -- the socket's
     * receive window does the rest. */
    while (n->inflight >= RC_MAX_INFLIGHT && c.isRunning())
        Sleep(10);

    InterlockedIncrement(&n->inflight);
    if (!PostMessage(n->notify, WM_RC_FRAME, 0, (LPARAM)f.get())) {
        /* The post failed, so the UI thread will never see this frame and
         * will never free it. Freeing it here is the whole reason the
         * inflight counter exists. The guard still owns `f`, so letting it
         * go out of scope below does the HeapFree half of that. */
        InterlockedDecrement(&n->inflight);
        rc_log("net: PostMessage(hwnd=%p, WM_RC_FRAME) failed: err=%lu "
               "(inflight=%ld)", (void *)n->notify, GetLastError(),
               n->inflight);
        rc_frame_free(f.get());
    } else {
        /* Handed to the UI thread, which now owns it (net.h: "a frame
         * posted with WM_RC_FRAME is owned by the UI thread, which frees
         * it"). release() so this guard does not free what the UI thread
         * is about to. */
        f.release();
    }
}

/* The UI thread's half of the three-way ownership rule. See net.h. */
void rc_net_frame_done(rc_net *n, rc_frame *f)
{
    rc_frame_delete(f);
    InterlockedDecrement(&n->inflight);
}

/* Live while connected; the last connection's totals after rc_net_stop. */
unsigned long rc_net_rx_bytes(const rc_net *n)
{
    Connection *c = conn_of((rc_net *)n);
    return c ? c->rxBytes() : n->rx_bytes;
}

unsigned long rc_net_tx_bytes(const rc_net *n)
{
    Connection *c = conn_of((rc_net *)n);
    return c ? c->txBytes() : n->tx_bytes;
}

long rc_net_inflight(const rc_net *n)
{
    return (long)n->inflight;
}

unsigned rc_net_outq_bytes(rc_net *n)
{
    Connection *c = conn_of(n);
    return c ? c->outqBytes() : 0;
}

void rc_net_get_deflate_sample(rc_net *n, unsigned long *bytes_in, unsigned long *us)
{
    Connection *c = conn_of(n);
    if (c) {
        c->takeDeflateSample(bytes_in, us);
        return;
    }
    if (bytes_in) *bytes_in = 0;
    if (us) *us = 0;
}

void rc_net_set_link_bps(rc_net *n, unsigned long bps)
{
    Connection *c = conn_of(n);
    if (c) c->setLinkBps(bps);
}

void rc_net_set_machine(rc_net *n, const char *id)
{
    if (!n) return;
    if (!id) id = "";
    /* DROPPED, not truncated. A truncated id is a perfectly valid-looking
     * id belonging to nobody, and the bridge would file this machine's
     * conversations under it forever. */
    if (!rc_strf(n->machine, sizeof(n->machine), "%s", id)) {
        rc_log("net: machine id does not fit in %u bytes; not reporting it",
               (unsigned)sizeof(n->machine));
        n->machine[0] = '\0';
    }
}

void rc_net_set_automation(rc_net *n, int mode)
{
    if (!n) return;
    /* Anything this build does not recognise becomes NONE rather than being
     * passed through: the bridge refuses a value it does not know, and a
     * refused handshake over a mistyped setting would take the whole client
     * down rather than one capability. */
    n->automation = (mode == RC_AUTOMATION_SCREEN) ? RC_AUTOMATION_SCREEN
                                                   : RC_AUTOMATION_NONE;
}

/* The only way to add a HELLO header. Drops rather than overruns, and says so.
 * A dropped header degrades the bridge's picture of the machine; an overrun
 * corrupts the stack while building the very first frame. */
/* 48 since 2026-10-02, up from 32. It was 29 used of 32, and
 * tests/python/meta/test_hello_headers.py refuses to let the last two slots
 * go: the header list IS the capability declaration, so running out of room
 * means the next client rule has nowhere to go and the cheap workaround --
 * packing them into bit fields -- is the thing caps.h exists to refuse.
 * bridge/wire/frame.py's MAX_HELLO_HEADERS moves with it, and that test
 * asserts the two agree. */
#define RC_HELLO_MAX 48
/* Stringify a macro's VALUE, not its name -- RC_STR2 is the extra expansion
 * pass without which HDR("eol", ...) would send the literal text
 * "RC_CAP_EOL". */
#define RC_STR2(x) #x
#define RC_STR(x)  RC_STR2(x)
#define HDR(k, v) do { \
        if (nkv < RC_HELLO_MAX) { keys[nkv] = (k); vals[nkv++] = (v); } \
        else rc_log("net: HELLO header '%s' dropped, array full", (k)); \
    } while (0)

/* A thread would not start: tear down through rc_net_stop, the one path
 * that joins before it frees and leaks what a live thread still holds.
 *
 * Both failure paths used to do their own partial teardown. The reader's
 * closed the socket BEFORE joining the sender, which was still inside
 * send() on it, and discarded the join's result -- the two things
 * rc_net_stop exists to get right (docs/wire-connection-plan.md, invariant
 * 8). _beginthreadex fails under the same USER/GDI and thread pressure that
 * makes CreateWindowExA fail, which is not a theoretical condition on this
 * machine.
 *
 * Connection::start has already stopped accepting sends, so rc_net_stop's
 * drain does not wait out two seconds for a HELLO no sender will take. */
static int start_failed(rc_net *n)
{
    rc_net_stop(n);
    set_state(n, RC_FAILED);
    return -1;
}

int rc_net_start(rc_net *n, HWND notify, const char *host, int port,
                 const char *secret, const void *tools, unsigned tools_len)
{
    char maxframe[16], ver[16], memkb[16], memfree[16], disk[24], acp[16];
    char oemcp[16];
    char osver[96], cpu[160], screen[32];
    /* Lives for the whole function, not just the block that fills it: HDR
     * records the pointer and rc_frame_build runs at the end. On the stack
     * with its neighbours rather than in BSS -- this frame is ~1.4KB against
     * a 4096 gate, and BSS is the budget that is actually tight. */
    char sroot[MAX_PATH];
    char temp[MAX_PATH];
    /* This array overflowed the moment mem_kb was added, and the fix at the
     * time was to make it bigger -- which left the same trap armed for the
     * next field. There are eleven entries now and there was NO bounds check
     * of any kind: overrunning it writes past two stack arrays in the HELLO
     * path, on a machine with no guard pages worth the name. Use HDR() and
     * nothing else. */
    const char *keys[RC_HELLO_MAX]; const char *vals[RC_HELLO_MAX];
    int nkv = 0;

    /* REFUSE, rather than re-initialise objects a surviving thread still
     * holds. rc_net_stop leaks send_lock and both events when a join times
     * out, precisely because a live thread may be inside them -- and then
     * cleared `started`, so this function had no way to know and ran
     * InitializeCriticalSection on a HELD section and overwrote two live
     * event handles, every reconnect after a hung shutdown. Undefined
     * behaviour on 9x, plus an accumulating handle leak. A Connection is
     * single-use now, so that exact fault cannot recur -- but the old
     * reader, sender or worker is still loose in the process.
     *
     * There is no safe recovery here: the old threads still point at the
     * old Connection and at this struct. Saying so and stopping is better than a fault with no
     * explanation. (mem-audit F5) */
    if (n->leaked) {
        rc_log("net: refusing to connect -- a previous shutdown left a thread "
               "running and its lock and events are still in use. Restart the "
               "application.");
        set_state(n, RC_FAILED);
        return -1;
    }

    n->notify = notify;
    rc_strf(n->host, sizeof(n->host), "%s", host);
    n->port = port;
    rc_strf(n->secret, sizeof(n->secret), "%s", secret);
    n->inflight = 0;

    /* A start with no stop between leaves the old link running beside the
     * new one; the reconnect path always stops first, so this is a bug in a
     * caller, and stopping here is the recovery. */
    if (n->conn) {
        rc_log("net: rc_net_start over a live link; stopping it first");
        rc_net_stop(n);
        if (n->leaked) {
            set_state(n, RC_FAILED);
            return -1;
        }
    }

    ClientLink *link = new ClientLink(n);
    n->conn = link;
    /* Mark it needing teardown HERE, not on success.
     *
     * From here every `return -1` leaves something behind -- a lock, two
     * events, Winsock's reference, a socket. Setting this only on success
     * meant a failed attempt -- a bridge that is simply down, which is the
     * common case -- left rc_net_stop thinking there was nothing to clean
     * up, so the next Reconnector::tryConnect re-initialised a LIVE critical
     * section and overwrote both event handles. That is a fault inside
     * KERNEL32, and it is what three bridge restarts reproduced. */
    n->started = 1;

    /* Reports CONNECTING, and FAILED on the way out of any failure. */
    if (!link->conn().connect(n->host, n->port)) return -1;

    rc_strf(ver, sizeof(ver), "%d", RC_PROTO_VERSION);
    rc_strf(maxframe, sizeof(maxframe), "%d", RC_MAX_FRAME);
    HDR("ver", ver);
    HDR("secret", n->secret);
    HDR("max_frame", maxframe);
    /* What this machine actually is.
     *
     * Two consumers. The bridge sizes pages from mem_kb -- a 128MB box can
     * hold a far bigger page than a 32MB one, and there is no other way for it
     * to know which it is talking to. And the rest goes into the agent's
     * system prompt, because a model that thinks it is on a modern machine
     * cannot make good decisions about which tool to reach for: "just install
     * this 400MB toolchain" is a wrong answer here however right it is
     * elsewhere. */
    {
        /* The folder this machine shares, so the agent's very first system
         * prompt can name it. The bridge already READS this header
         * (server.py:503) and the guest has never sent it -- so machine.py
         * fell back to the phrase "the machine's sync folder", the exact
         * vagueness the comment above that fallback warns about, on every
         * first session. The value comes from the INI and is known before
         * the socket opens.
         *
         * THE CONFIGURED ROOT, NOT THE LIVE ONE. A conversation's own folder
         * is chosen by the bridge, so reporting it here tells the bridge
         * what it already decided -- and after a bridge restart it reports
         * a tree the new process knows nothing about, which is how one came
         * to pick C:\Gaston\OK's baseline and pair it against its whole
         * mirrored directory. */
        /* A copy, not the shared buffer: rc_syncrpc_set_home runs on the
         * UI thread and this runs while HELLO is built (mem-audit F7). */
        if (rc_syncrpc_home_copy(sroot, sizeof(sroot))) HDR("sync_root", sroot);
    }

    /* The raw numbers come from libexec; the header text is ours, and must
     * stay byte-for-byte what the bridge parses. */
    SystemInfo sys;
    readSystemInfo(sys);
    {

        rc_strf(memkb, sizeof(memkb), "%lu", (unsigned long)sys.memKb);
        HDR("mem_kb", memkb);
        rc_strf(memfree, sizeof(memfree), "%lu", (unsigned long)sys.memFreeKb);
        HDR("mem_free_kb", memfree);

        if (sys.osKnown) {
            rc_strf(osver, sizeof(osver), "Windows %lu.%lu build %lu %s",
                      (unsigned long)sys.osMajor,
                      (unsigned long)sys.osMinor,
                      (unsigned long)sys.osBuild,
                      sys.osExtra);
            HDR("os", osver);
        }

        /* WHAT THE PROCESSOR CALLS ITSELF, when it can say.
         *
         * This read `x86 family 6 model 10, 1 cpu` until 2026-10-02. That is
         * accurate, and it is also what an agent reads as "a weak machine":
         * one chose an interpreted language over the Visual C++ 6.0 installed
         * beside it, reasoning from "tight memory and single CPU" -- on an
         * Athlon XP 2000+ with 256MB, which for this operating system is
         * close to the best hardware it ever ran on.
         *
         * The brand string is what System Properties and dxdiag show, because
         * all three read the same CPUID leaves (exec/cpuid.h). The family and
         * model stay on the line behind it: they are what the OS itself
         * believes, they are two numbers, and when the brand string is absent
         * -- every CPU older than about 1999, the Pentium 200 included --
         * they plus the vendor are the whole answer. */
        if (sys.cpuBrandName[0])
            rc_strf(cpu, sizeof(cpu),
                    "%s (family %u model %u), %lu cpu",
                    sys.cpuBrandName, sys.cpuFamily, sys.cpuModel,
                    (unsigned long)sys.cpuCount);
        else if (sys.cpuVendorName[0])
            rc_strf(cpu, sizeof(cpu), "%s x86 family %u model %u, %lu cpu",
                    sys.cpuVendorName, sys.cpuFamily, sys.cpuModel,
                    (unsigned long)sys.cpuCount);
        else
            rc_strf(cpu, sizeof(cpu), "x86 family %u model %u, %lu cpu",
                    sys.cpuFamily, sys.cpuModel, (unsigned long)sys.cpuCount);
        HDR("cpu", cpu);

        /* The SCREEN dc: GetDC(NULL) pairs with ReleaseDC(NULL, dc), which
         * is exactly what WindowDC does with a null window. */
        {
            WindowDC dc(NULL);
            if (dc.valid()) {
                rc_strf(screen, sizeof(screen), "%dx%dx%d",
                          GetDeviceCaps(dc.get(), HORZRES),
                          GetDeviceCaps(dc.get(), VERTRES),
                          GetDeviceCaps(dc.get(), BITSPIXEL));
                HDR("screen", screen);
            }
        }

        /* Through libfs, which resolves GetDiskFreeSpaceExA with
         * GetProcAddress and falls back to the old call. The old call ALONE
         * is not "roughly right" on a modern disk, which is what this used to
         * say: the OS clamps its cluster counts to about 2GB on anything
         * larger, so a 20GB volume reported the same figure as a 2GB one and
         * the agent was told so in its system prompt on every request. */
        {
            ULARGE_INTEGER freeb;
            if (freeBytes64("C:\\", freeb)) {
                rc_strf(disk, sizeof(disk), "%lu",
                        (unsigned long)(freeb.QuadPart / 1024ULL));
                HDR("disk_free_kb", disk);
            }
        }
    }
    /* WHERE A SCRATCH FILE GOES ON THIS MACHINE.
     *
     * The bridge needs one writable path that is not the user's work folder:
     * `command.com /c` loses a program's exit code (always 0, even for a
     * program that does not exist), and the only way to get it back is a
     * wrapper batch file that runs the command and echoes what `if errorlevel`
     * saw -- measured on real hardware, docs/measurements/
     * exit-codes-via-batch-p200-20261002.txt. That file is plumbing. It does
     * not belong in a folder the person looks at.
     *
     * GetTempPathA, so it honours TMP and TEMP the way everything else on the
     * machine does, and it is KERNEL32 on 95 onward. The trailing backslash it
     * returns is kept: the bridge joins with it and a path is easier to
     * compose than to guess the convention of. Absent means the bridge does
     * not write there, which costs the exit code and nothing else. */
    {
        DWORD got = GetTempPathA(sizeof(temp), temp);
        if (got > 0 && got < sizeof(temp)) HDR("temp", temp);
    }

    if (n->sess[0]) HDR("sess", n->sess);

    /* WHICH MACHINE THIS IS, so the bridge can keep one guest's chat list
     * out of another's. `mine` on the bridge says only that THIS BRIDGE
     * started a conversation, which is the same answer for every machine
     * that dials it. Absent means "do not narrow", so a client that has no
     * id -- or an older one that never sent this -- behaves as before.
     * (BACKLOG.md §21.) */
    if (n->machine[0]) HDR("machine", n->machine);

    /* The ANSI codepage, and this one closes a SILENT corruption.
     *
     * The bridge encodes every path it sends with a codepage table, and it
     * assumed cp1252. On a CP1250 or CP932 machine that assumption is wrong,
     * and wrong in the one way nothing can catch: it encodes `cafe.md` with
     * the wrong table, CreateFileA stores those bytes, the manifest reports
     * them back, and the bridge decodes with the SAME wrong table and gets the
     * name it started with. It round-trips perfectly, forever, and the user
     * simply has a file whose name is mangled on their own disk.
     *
     * There is no detector for that at any layer -- the sync layer cannot see
     * it, because every operation is self-consistent. The only thing that can
     * tell is the machine itself, so it says. GetACP is in KERNEL32 on Win95
     * onward, so it survives the import audit.
     *
     * ACP and not OEM: VFAT long names are stored in the ANSI codepage, and
     * the scanner keys on long names only (never cAlternateFileName). The
     * console codepage is a different question with a different answer. */
    rc_strf(acp, sizeof(acp), "%u", sys.acp);
    HDR("acp", acp);

    /* ...and the OEM one, which is the "different question" above, now
     * answered. A console child writes OEM, not ANSI, and the two differ on
     * every machine that is not US-English: cp1252/cp437 here, cp1251/cp866
     * on a Russian box. The bridge used to take this from the shell's profile
     * -- which can say THAT this shell's children write OEM but cannot know
     * WHICH OEM this machine is set to. Only the machine knows, so it says,
     * for exactly the reason the ANSI page is sent.
     *
     * GetOEMCP sits beside GetACP in KERNEL32 from Win95 onward, so it
     * survives the import audit the same way. */
    rc_strf(oemcp, sizeof(oemcp), "%u", sys.oemcp);
    HDR("oemcp", oemcp);

    /* The rest of what this machine is. Every one a literal from caps.h, so a
     * Windows 3.1 or MS-DOS build changes that header and nothing here. */
    HDR("case",       RC_STR(RC_CAP_CASE));
    HDR("devices",    RC_STR(RC_CAP_DEVICES));
    HDR("trailing",   RC_STR(RC_CAP_TRAILING));
    HDR("illegal",    RC_STR(RC_CAP_ILLEGAL));
    HDR("eol",        RC_STR(RC_CAP_EOL));
    HDR("shortnames", RC_STR(RC_CAP_SHORTNAMES));
    HDR("dirreserve", RC_STR(RC_CAP_DIRRESERVE));
    HDR("workreserve", RC_STR(RC_CAP_WORKRESERVE));
    HDR("maxpath",    RC_STR(RC_CAP_MAXPATH));
    HDR("maxcomp",    RC_STR(RC_CAP_MAXCOMP));
    HDR("manifest",   RC_STR(RC_CAP_MANIFEST));
    HDR("maxdepth",   RC_STR(RC_CAP_MAXDEPTH));
    HDR("maxfiles",   RC_STR(RC_CAP_MAXFILES));
    HDR("deflate",    RC_STR(RC_CAP_DEFLATE));

    /* ...and the one that is not a property of the hardware. Whether the
     * agent is offered the five screen tools at all, because declaring a tool
     * the client cannot use is wrong however little it costs -- and it is not
     * nothing: measured 2026-10-01, `ui`, `click`, `keys`, `shot` and `focus`
     * are 1,576 tokens of the 20,360 a session starts with, sitting in the
     * request's `tools` array for the whole session.
     *
     * A literal either way, so no buffer: rc_net_set_automation has already
     * reduced whatever was configured to one of the two values. */
    HDR("automation", n->automation == RC_AUTOMATION_SCREEN
                      ? RC_STR(RC_AUTOMATION_SCREEN)
                      : RC_STR(RC_AUTOMATION_NONE));

    /* THE BODY IS THE INVENTORY. It rides HELLO rather than a frame of its
     * own because the bridge builds the agent's system prompt inside the
     * same call that consumes HELLO, before it starts reading any further
     * frames -- so anything sent afterwards arrives too late to be in the
     * prompt for that session, which is every session on a fresh
     * connection. A bridge that does not understand it ignores it. */
    if (rc_net_send(n, "HELLO", keys, vals, nkv, tools, tools_len) != 0) {
        rc_log("net: HELLO failed");
        set_state(n, RC_FAILED);
        return -1;
    }

    /* Order matters. The worker must exist before the reader can route an
     * EXEC to it, or an early frame is dropped silently and the bridge waits
     * out its whole budget for a command nobody ran. The Connection then
     * starts its sender, reports CONNECTED, and starts its reader. */
    rc_rpc_start(n);
    if (!link->conn().start()) return start_failed(n);
    return 0;
}

/* ------------------------------------------------------------- sending */

/* `wait_ticks` is how many 100ms slices this caller will spend waiting for
 * the outbound queue to drain before giving up -- see the two wrappers below
 * for who uses which. No connection (never started, or stopped) is a dead
 * link: rpccheck drives the RPC worker over a zeroed rc_net and relies on
 * exactly that. */
static int send_frame(rc_net *n, const char *type,
                      const char *const *keys, const char *const *vals, int nkv,
                      const void *body, unsigned body_len, int wait_ticks)
{
    Connection *c = conn_of(n);
    if (!c) return RC_SEND_DEAD;
    return c->send(type, keys, vals, nkv, body, body_len, wait_ticks,
                   rc_frame_compressible(type) != 0);
}

/* Bulk: the full five-second budget. Manifest rows, SYNCDAT chunks, EXECOUT
 * -- things produced by the worker, where waiting for the wire is the whole
 * point of backpressure. */
int rc_net_send(rc_net *n, const char *type, const char *const *keys,
                const char *const *vals, int nkv,
                const void *body, unsigned body_len)
{
    return send_frame(n, type, keys, vals, nkv, body, body_len,
                      RC_SEND_WAIT_BULK);
}

/* A USER GESTURE: half a second, not five.
 *
 * ui_send calls this from the window thread -- Allow/Always/Deny, Send, a
 * chat switch, New, Export, Permissions, SETWORK -- and the message pump does
 * not run while it waits. With the bulk budget, pressing Allow while a
 * SYNCGET was streaming froze the window for five seconds, long enough for
 * Win98's ghost frame, and then reported the link as down. Half a second is
 * long enough to ride out an ordinary drain and short enough not to read as a
 * hang; past it the caller is told the queue is busy and that trying again is
 * worth it, which is true and is a different sentence from "the connection is
 * down". (audit 01.17) */
int rc_net_send_ui(rc_net *n, const char *type, const char *const *keys,
                   const char *const *vals, int nkv,
                   const void *body, unsigned body_len)
{
    return send_frame(n, type, keys, vals, nkv, body, body_len,
                      RC_SEND_WAIT_UI);
}

int rc_net_stop(rc_net *n)
{
    int rpcj;

    /* Safe to call on a connection that never started, so the reconnect path
     * can call it unconditionally rather than tracking state itself. */
    if (!n->started) return !n->leaked;
    n->started = 0;

    /* SHUTDOWN IS THREE PHASES, AND THE ORDER IS THE WHOLE POINT (01.51).
     *
     * `running` used to be cleared first, before rc_rpc_stop(). But a send
     * bails on a link that is not running, so every ack the RPC worker's
     * shutdown drain tried to send failed -- and rpc.cpp's comment over that
     * drain says the frames are "answered, not silently dropped -- the
     * bridge is holding a future for it". That was false on the only path
     * that reaches it in production. The bridge waited out the whole timeout
     * for an answer the guest had already composed and thrown away.
     *
     * The mistake was that one flag meant two things: "the link is usable"
     * and "accept new work". They are separated by sequence now.
     *
     * PHASE 1 -- let the worker finish and ANSWER. The link still accepts
     * sends and the sender thread is still on the wire, so the drain's acks
     * can actually leave. rc_rpc_stop kills any child process first, so this
     * cannot wait on a two-minute exec. */
    rc_rpc_stop();              /* signals the worker and kills any child */
    rpcj = rc_rpc_join(30000);
    if (!rpcj)
        rc_log("net: RPC worker did not exit");

    ClientLink *link = (ClientLink *)n->conn;
    if (!link) return rpcj != 0;
    Connection &c = link->conn();

    /* PHASE 2 -- give those acks time to reach the wire. */
    c.drain(2000);

    /* PHASE 3 -- nothing new is accepted, the FIN goes out, the threads go,
     * and only then is anything freed. The worker sends too, so it is one of
     * the threads release() must know about. */
    c.signalStop();
    c.join(5000);
    n->rx_bytes = c.rxBytes();
    n->tx_bytes = c.txBytes();
    if (c.release(rpcj != 0)) {
        n->conn = NULL;
        delete link;
        return 1;
    } else {
        /* A thread is still inside the Connection, so it stays allocated
         * and `conn` keeps pointing at it: the worker, if it is the one
         * still running, sends through it. rc_net_start refuses from now
         * on. */
        n->leaked = 1;
        return 0;
    }
}
