#include <windows.h>
#include <winsock.h>
#include <process.h>
#include <stdio.h>
#include <string.h>
#include "core/rc_cpp.h"
#include "net/net.h"
#include "net/caps.h"
#include <new>
#include "core/heap.h"
#include "core/log.h"
#include "core/str.h"
#include "rpc/rpc.h"
#include "rpc/exec.h"
#include "sync/syncrpc.h"
#include "miniz.h"

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

/* THE CONNECTION'S COMPRESSION STATE, in one place.
 *
 * These were eleven file statics. They are gathered here so the transport can
 * move to wire/connection.cpp with its codec as a member (docs/wire-connection-
 * plan.md, step b); there is still exactly one, `s_codec`, because there is
 * still exactly one connection. A POD with no constructor, so it costs no
 * static initialiser and the same bytes at load as the statics did.
 *
 * Threads. The inflate half is touched only by the READER, inside
 * inflateBody(). The deflate half and the stats are touched under send_lock,
 * by whoever is sending. begin()/finish() run only when neither thread is
 * alive, which is what rc_net_stop's join gates are for.
 *
 * Every member has one caller and is `inline`: Watcom emits an out-of-line
 * copy of a member defined outside its class even after inlining it, which
 * the static functions these replace never cost (+2 KB of code, measured). */
struct Codec
{
    /* One persistent inflate stream for the whole connection.
     *
     * The bridge compresses with a persistent deflate stream and Z_SYNC_FLUSH
     * per frame, which keeps the dictionary warm across frames -- worth 1.27x
     * over compressing each frame alone. That only works if this side keeps
     * its state too, so this is deliberately NOT per-frame. Both sides reset
     * on reconnect, in lockstep. */
    mz_stream inf;
    int infReady;

    /* ONE inflate scratch for the whole connection, not one per frame.
     *
     * inflate_body used to HeapAlloc its full output cap for EVERY compressed
     * frame and never shrink it: f->body_len recorded the real length while
     * f->_body_alloc held the entire block until the UI thread freed the
     * frame. With the reader posting up to RC_MAX_INFLIGHT frames before it
     * blocks, that is a per-frame cost measured in hundreds of KB, multiplied
     * by however many frames the UI has not drained -- and the UI thread
     * stalls whenever MSHTML reflows or a folder browser is open. On a 32MB
     * machine that is the largest transient demand in the program, and when
     * HeapAlloc finally failed the reader dropped the LINK rather than slowing
     * down.
     *
     * Now: inflate into this one buffer, then allocate exactly the bytes that
     * came out and copy. Peak concurrent cost becomes one scratch plus the
     * real sizes.
     *
     * Heap rather than BSS so it is not committed at load on a machine where
     * committed-at-load is audited; lazily allocated per connection and
     * released with the stream it serves.
     *
     * 2x RC_MAX_FRAME, not 4x. The cap exists so a corrupt or hostile frame
     * cannot expand without limit, and the real bound is far lower:
     * bridge/wire/link.py caps an UNCOMPRESSED chunk at MAX_FRAME - 1024 =
     * 64,512 bytes before it compresses, so a legitimate inflated body never
     * exceeds ~63KB. 2x leaves more than double the largest value the protocol
     * permits. */
    char *infScratch;

    /* One persistent deflate stream for the whole connection (guest ->
     * bridge). */
    mz_stream def;
    int defReady;
    char *defScratch;

    /* Measurement counters for adaptive compression (03.3 / A4). */
    unsigned long deflateIn;
    unsigned long deflateOut;
    unsigned long deflateUs;
    unsigned long deflateSamples;
    unsigned long deflateBps;
    unsigned long linkBps;
    LARGE_INTEGER perfFreq;
    int perfInit;

    enum
    {
        INFLATE_SCRATCH = RC_MAX_FRAME * 2,
        DEFLATE_SCRATCH = RC_MAX_FRAME + 1024
    };

    /* A fresh pair of streams and zeroed stats, for a new connection. */
    void begin();
    /* Separate, because their gates differ: see rc_net_stop. */
    void finishInflate();
    void finishDeflate();

    int inflateBody(rc_frame *f);
    bool shouldCompress(unsigned bodyLen) const;
    /* The compressed bytes, in defScratch, or null if deflate failed. */
    const char *compressBody(const void *body, unsigned bodyLen, unsigned *outLen);
    void takeSample(unsigned long *bytesIn, unsigned long *us);

private:
    void beginInflate();
    void beginDeflate();
    unsigned long elapsedSince(const LARGE_INTEGER &t0);
};

Codec s_codec;

inline void Codec::beginInflate()
{
    if (infReady)
    {
        mz_inflateEnd(&inf);
        infReady = 0;
    }
    memset(&inf, 0, sizeof(inf));
    if (mz_inflateInit(&inf) == MZ_OK)
        infReady = 1;
    else
        rc_log("net: mz_inflateInit failed");
    if (!infScratch)
    {
        infScratch = (char *)HeapAlloc(GetProcessHeap(), 0,
                                       INFLATE_SCRATCH + 1);
        /* Not fatal here: inflateBody reports the failure per frame, which
         * is where there is a frame to fail. */
        if (!infScratch)
            rc_log("net: no memory for the %u byte inflate scratch",
                   (unsigned)INFLATE_SCRATCH);
    }
}

inline void Codec::beginDeflate()
{
    if (defReady)
    {
        mz_deflateEnd(&def);
        defReady = 0;
    }
    memset(&def, 0, sizeof(def));
    if (mz_deflateInit(&def, 1) == MZ_OK)
        defReady = 1;
    else
        rc_log("net: mz_deflateInit failed");
    if (!defScratch)
    {
        defScratch = (char *)HeapAlloc(GetProcessHeap(), 0, DEFLATE_SCRATCH);
        if (!defScratch)
            rc_log("net: no memory for the %u byte deflate scratch",
                   (unsigned)DEFLATE_SCRATCH);
    }
}

inline void Codec::begin()
{
    beginInflate();
    beginDeflate();
    deflateIn = 0;
    deflateOut = 0;
    deflateUs = 0;
    deflateSamples = 0;
    deflateBps = 0;
    linkBps = 0;
}

inline void Codec::finishInflate()
{
    if (infReady)
    {
        mz_inflateEnd(&inf);
        infReady = 0;
    }
    if (infScratch)
    {
        HeapFree(GetProcessHeap(), 0, infScratch);
        infScratch = NULL;
    }
}

inline void Codec::finishDeflate()
{
    if (defReady)
    {
        mz_deflateEnd(&def);
        defReady = 0;
    }
    if (defScratch)
    {
        HeapFree(GetProcessHeap(), 0, defScratch);
        defScratch = NULL;
    }
}

/* Inflates f->body in place-ish: allocates a new buffer, points body at it,
 * and records it for rc_frame_free. Returns 0 on success. */
inline int Codec::inflateBody(rc_frame *f)
{
    /* Bounded so a malicious or corrupt frame cannot expand without limit.
     * See the note on infScratch for why this is one shared buffer and why
     * the bound is 2x rather than 4x. */
    unsigned cap = INFLATE_SCRATCH;

    if (!infReady)
    {
        rc_log("net: deflate frame but no inflater");
        return -1;
    }
    if (f->body_len == 0)
        return 0;
    if (!infScratch)
    {
        rc_log("net: no inflate scratch");
        return -1;
    }

    inf.next_in = (const unsigned char *)f->body;
    inf.avail_in = f->body_len;
    inf.next_out = (unsigned char *)infScratch;
    inf.avail_out = cap;

    int rc = mz_inflate(&inf, MZ_SYNC_FLUSH);
    if (rc != MZ_OK && rc != MZ_STREAM_END)
    {
        rc_log("net: mz_inflate failed: %d", rc);
        return -1;
    }
    if (inf.avail_in != 0)
    {
        rc_log("net: inflate left %u bytes unconsumed (output cap hit)",
               inf.avail_in);
        return -1;
    }

    /* EXACTLY what came out. This is the allocation the frame carries until
     * the UI thread frees it, so it is the one that multiplies by the number
     * of frames in flight. */
    unsigned got = cap - inf.avail_out;
    char *out = (char *)HeapAlloc(GetProcessHeap(), 0, got + 1);
    if (!out)
    {
        rc_log("net: no memory for a %u byte inflated body", got);
        return -1;
    }
    memcpy(out, infScratch, got);

    f->_body_alloc = out;
    f->body_len = got;
    out[got] = '\0';
    f->body = out;
    return 0;
}

/* Compress while the first few samples are still coming in, then only while
 * the link is slower than half of what deflate manages: on a fast link the
 * CPU time costs more than the bytes it saves. */
inline bool Codec::shouldCompress(unsigned bodyLen) const
{
    if (bodyLen < 64 || !defReady || !defScratch)
        return false;
    if (deflateSamples < 5 || deflateBps == 0)
        return true;
    return linkBps < deflateBps / 2;
}

inline unsigned long Codec::elapsedSince(const LARGE_INTEGER &t0)
{
    unsigned long us;
    if (perfFreq.QuadPart > 0)
    {
        LARGE_INTEGER t1;
        QueryPerformanceCounter(&t1);
        us = (unsigned long)(((t1.QuadPart - t0.QuadPart) * 1000000ULL) /
                             perfFreq.QuadPart);
    }
    else
    {
        us = (GetTickCount() - t0.LowPart) * 1000UL;
    }
    return us ? us : 1;
}

inline const char *Codec::compressBody(const void *body, unsigned bodyLen,
                               unsigned *outLen)
{
    LARGE_INTEGER t0;

    if (!perfInit)
    {
        if (!QueryPerformanceFrequency(&perfFreq) || perfFreq.QuadPart <= 0)
            perfFreq.QuadPart = 0;
        perfInit = 1;
    }
    if (perfFreq.QuadPart > 0)
        QueryPerformanceCounter(&t0);
    else
        t0.LowPart = GetTickCount();

    def.next_in = (const unsigned char *)body;
    def.avail_in = bodyLen;
    def.next_out = (unsigned char *)defScratch;
    def.avail_out = DEFLATE_SCRATCH;

    int rc = mz_deflate(&def, MZ_SYNC_FLUSH);
    unsigned long us = elapsedSince(t0);

    if (rc != MZ_OK || def.avail_in != 0)
    {
        rc_log("net: mz_deflate failed: %d, avail_in=%u", rc, def.avail_in);
        return NULL;
    }
    *outLen = DEFLATE_SCRATCH - def.avail_out;
    deflateIn += bodyLen;
    deflateOut += *outLen;
    deflateUs += us;
    deflateSamples++;
    if (deflateUs > 0)
        deflateBps = (unsigned long)((double)deflateIn * 1000000.0 / deflateUs);
    return defScratch;
}

inline void Codec::takeSample(unsigned long *bytesIn, unsigned long *us)
{
    if (bytesIn)
        *bytesIn = deflateIn;
    if (us)
        *us = deflateUs;
    if (deflateUs > 0)
        deflateBps = (unsigned long)((double)deflateIn * 1000000.0 / deflateUs);
    deflateIn = 0;
    deflateUs = 0;
}

void rc_net_get_deflate_sample(rc_net *n, unsigned long *bytes_in, unsigned long *us)
{
    ScopedLock lock(&n->send_lock);
    s_codec.takeSample(bytes_in, us);
}

void rc_net_set_link_bps(rc_net *n, unsigned long bps)
{
    ScopedLock lock(&n->send_lock);
    s_codec.linkBps = bps;
}

/* The UI thread's half of the three-way ownership rule. See net.h. */
void rc_net_frame_done(rc_net *n, rc_frame *f)
{
    rc_frame_delete(f);
    InterlockedDecrement(&n->inflight);
}

unsigned long rc_net_rx_bytes(const rc_net *n)
{
    return n->rx_bytes;
}

unsigned long rc_net_tx_bytes(const rc_net *n)
{
    return n->tx_bytes;
}

long rc_net_inflight(const rc_net *n)
{
    return (long)n->inflight;
}

unsigned rc_net_outq_bytes(rc_net *n)
{
    unsigned b;
    {
        ScopedLock lock(&n->send_lock);
        b = n->outq_bytes;
    }
    return b;
}

const char *rc_state_name(rc_state s)
{
    switch (s) {
    case RC_DISCONNECTED: return "disconnected";
    case RC_CONNECTING:   return "connecting";
    case RC_CONNECTED:    return "connected";
    default:              return "failed";
    }
}

static void set_state(rc_net *n, rc_state s)
{
    n->state = s;
    if (n->notify) PostMessage(n->notify, WM_RC_STATE, (WPARAM)s, 0);
}

static unsigned __stdcall sender_thread(void *arg);

static int recv_exact(SOCKET s, char *buf, unsigned len, volatile LONG *running)
{
    unsigned got = 0;
    while (got < len) {
        int r = recv(s, buf + got, (int)(len - got), 0);
        if (r == 0) return -1;                    /* peer closed */
        if (r == SOCKET_ERROR) {
            if (!*running) return -1;
            return -1;
        }
        got += (unsigned)r;
    }
    return 0;
}

static unsigned reader_thread_body(void *arg)
{
    rc_net *n = (rc_net *)arg;
    unsigned char hdr[4];

    while (n->running) {
        unsigned len;
        char *payload;

        if (recv_exact(n->sock, (char *)hdr, 4, &n->running) != 0) break;

        len = (unsigned)hdr[0] | ((unsigned)hdr[1] << 8)
            | ((unsigned)hdr[2] << 16) | ((unsigned)hdr[3] << 24);

        /* A corrupted length prefix must not be able to allocate the machine
         * away. Never try to resync a stream whose framing is already lost. */
        if (len == 0 || len > RC_MAX_FRAME) {
            rc_log("net: bad frame length %u (cap %u), dropping link",
                   len, RC_MAX_FRAME);
            break;
        }

        payload = (char *)HeapAlloc(GetProcessHeap(), 0, len + 1);
        if (!payload) { rc_log("net: out of memory for %u byte frame", len); break; }

        if (recv_exact(n->sock, payload, len, &n->running) != 0) {
            HeapFree(GetProcessHeap(), 0, payload);
            break;
        }
        payload[len] = '\0';
        n->rx_bytes += len + 4;

        /* Owns just the rc_frame struct, never `payload` -- that passes into
         * the frame via rc_frame_parse's out->_raw and is freed by
         * rc_frame_free, not by this guard. Declared here rather than at the
         * top of the loop because HeapPtr has no default constructor: a
         * non-copyable guard cannot be assigned into after the fact either,
         * so it is built with the pointer it owns from the start. */
        HeapPtr<rc_frame> f((rc_frame *)HeapAlloc(GetProcessHeap(), 0, sizeof(rc_frame)));
        if (!f.valid()) { HeapFree(GetProcessHeap(), 0, payload); break; }

        if (rc_frame_parse(payload, len, f.get()) != 0) {
            rc_log("net: malformed frame, dropping link");
            HeapFree(GetProcessHeap(), 0, payload);
            break;
        }

        {
            const char *enc = rc_frame_get(f.get(), "enc");
            if (enc && strcmp(enc, "deflate") == 0 && s_codec.inflateBody(f.get()) != 0) {
                rc_log("net: cannot inflate frame, dropping link");
                rc_frame_free(f.get());
                break;
            }
        }

        /* EXEC goes straight to the RPC worker and never reaches the UI
         * thread. The type is already parsed here, so routing it now saves a
         * hop and a copy -- and, more importantly, `inflight` is the wrong
         * instrument for it: that counter measures UI QUEUE DEPTH for the
         * backpressure gate below, and holding it for the sixty seconds a
         * command might run would conflate two unrelated queues and stall
         * display frames behind a compile. The worker has its own counter. */
        /* Ask the RPC layer whether this frame is one of its own.
         *
         * This loop used to hard-code EXEC, EXECKILL and the SYNC prefix --
         * the transport knowing the names of the tools it carries, which made
         * a genuine net<->rpc cycle and would have put another branch here for
         * each of the eight tools Phase 6 adds. The vocabulary lives in
         * rc_rpc_route now; net.c only knows that something else may want the
         * frame, and that if it is taken it has also been freed. */
        if (rc_rpc_route(f.get(), &n->running) == RC_ROUTE_TAKEN) {
            /* Ownership genuinely transferred: rc_rpc_route has ALREADY freed
             * the frame by the time it returns RC_ROUTE_TAKEN (rpc.h), whether
             * by handing it to the worker or by freeing it itself on a refusal
             * path. release() so this guard does not free it a second time --
             * that double free is exactly the failure mode net.h's three-way
             * ownership rule exists to rule out. */
            f.release();
            continue;
        }

        /* Backpressure: if the UI is not keeping up, wait for it rather than
         * queueing without bound. Yielding here is correct -- the socket's
         * receive window does the rest. */
        while (n->inflight >= RC_MAX_INFLIGHT && n->running)
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

    /* THE LINK IS DEAD, AND SAYING SO IS THIS THREAD'S JOB.
     *
     * Leaving `running` at 1 here was the single worst bug in the four-thread
     * design: Reconnector::tryConnect would call rc_net_start again with the sender and
     * the RPC worker still alive, which re-initialised a HELD critical
     * section, zeroed the outbound queue from under the sender, and left two
     * sender threads calling send() on one socket. Two concurrent sends
     * interleave, the bridge's framing has no resync, so it drops the link --
     * which reconnects, adding a third sender. */
    {
        int was_running = n->running;
        n->running = 0;
        set_state(n, was_running ? RC_FAILED : RC_DISCONNECTED);
    }
    rc_log("net: reader thread exiting (rx=%lu tx=%lu)", n->rx_bytes, n->tx_bytes);
    return 0;
}

/* Catches std::bad_alloc, which operator new throws (core/heap.cpp): an
 * exception leaving a thread's entry point ends the process with no log. */
static unsigned __stdcall reader_thread(void *arg)
{
    try
    {
        return reader_thread_body(arg);
    }
    catch (std::bad_alloc &)
    {
        rc_out_of_memory("the network reader");
    }
    return 0;
}

static int do_connect(rc_net *n)
{
    struct sockaddr_in addr;
    unsigned long ip;
    struct hostent *he;

    n->sock = socket(AF_INET, SOCK_STREAM, 0);
    if (n->sock == INVALID_SOCKET) {
        rc_log("net: socket() failed: %d", WSAGetLastError());
        return -1;
    }

    ip = inet_addr(n->host);
    if (ip == INADDR_NONE) {
        he = gethostbyname(n->host);
        if (!he) {
            rc_log("net: cannot resolve %s: %d", n->host, WSAGetLastError());
            closesocket(n->sock); n->sock = INVALID_SOCKET;
            return -1;
        }
        memcpy(&ip, he->h_addr_list[0], 4);
    }

    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons((u_short)n->port);
    addr.sin_addr.s_addr = ip;

    if (connect(n->sock, (struct sockaddr *)&addr, sizeof(addr)) == SOCKET_ERROR) {
        rc_log("net: connect to %s:%d failed: %d", n->host, n->port,
               WSAGetLastError());
        closesocket(n->sock); n->sock = INVALID_SOCKET;
        return -1;
    }
    rc_log("net: connected to %s:%d", n->host, n->port);
    return 0;
}

/* The only way to add a HELLO header. Drops rather than overruns, and says so.
 * A dropped header degrades the bridge's picture of the machine; an overrun
 * corrupts the stack while building the very first frame. */
#define RC_HELLO_MAX 32
/* Stringify a macro's VALUE, not its name -- RC_STR2 is the extra expansion
 * pass without which HDR("eol", ...) would send the literal text
 * "RC_CAP_EOL". */
#define RC_STR2(x) #x
#define RC_STR(x)  RC_STR2(x)
#define HDR(k, v) do { \
        if (nkv < RC_HELLO_MAX) { keys[nkv] = (k); vals[nkv++] = (v); } \
        else rc_log("net: HELLO header '%s' dropped, array full", (k)); \
    } while (0)

int rc_net_start(rc_net *n, HWND notify, const char *host, int port,
                 const char *secret, const void *tools, unsigned tools_len)
{
    WSADATA wsa;
    char maxframe[16], ver[16], memkb[16], memfree[16], disk[24], acp[16];
    char oemcp[16];
    char osver[96], cpu[64], screen[32];
    /* Lives for the whole function, not just the block that fills it: HDR
     * records the pointer and rc_frame_build runs at the end. On the stack
     * with its neighbours rather than in BSS -- this frame is ~1.4KB against
     * a 4096 gate, and BSS is the budget that is actually tight. */
    char sroot[MAX_PATH];
    /* This array overflowed the moment mem_kb was added, and the fix at the
     * time was to make it bigger -- which left the same trap armed for the
     * next field. There are eleven entries now and there was NO bounds check
     * of any kind: overrunning it writes past two stack arrays in the HELLO
     * path, on a machine with no guard pages worth the name. Use HDR() and
     * nothing else. */
    const char *keys[RC_HELLO_MAX]; const char *vals[RC_HELLO_MAX];
    int nkv = 0;
    unsigned tid;

    /* REFUSE, rather than re-initialise objects a surviving thread still
     * holds. rc_net_stop leaks send_lock and both events when a join times
     * out, precisely because a live thread may be inside them -- and then
     * cleared `started`, so this function had no way to know and ran
     * InitializeCriticalSection on a HELD section and overwrote two live
     * event handles, every reconnect after a hung shutdown. Undefined
     * behaviour on 9x, plus an accumulating handle leak.
     *
     * There is no safe recovery here: the old reader and sender still point
     * at this struct. Saying so and stopping is better than a fault with no
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
    n->sock = INVALID_SOCKET;
    n->inflight = 0;

    InitializeCriticalSection(&n->send_lock);
    n->outq_head = n->outq_tail = NULL;
    n->outq_bytes = 0;
    /* Auto-reset: the sender takes one item per wake and loops until empty. */
    n->outq_wake = CreateEventA(NULL, FALSE, FALSE, NULL);
    /* Manual-reset would wake every blocked writer at once; auto-reset wakes
     * one, which is what "a slot freed up" means. */
    n->outq_room = CreateEventA(NULL, FALSE, FALSE, NULL);
    /* Mark it needing teardown HERE, not on success.
     *
     * The critical section and both events now exist, and every `return -1`
     * below leaves them behind. Setting this only on success meant a failed
     * attempt -- a bridge that is simply down, which is the common case --
     * left rc_net_stop thinking there was nothing to clean up, so the next
     * Reconnector::tryConnect re-initialised a LIVE critical section and overwrote both
     * event handles. That is a fault inside KERNEL32, and it is what three
     * bridge restarts reproduced. */
    n->started = 1;
    set_state(n, RC_CONNECTING);

    if (WSAStartup(MAKEWORD(1, 1), &wsa) != 0) {
        rc_log("net: WSAStartup failed");
        set_state(n, RC_FAILED);
        return -1;
    }

    if (do_connect(n) != 0) { set_state(n, RC_FAILED); return -1; }

    s_codec.begin();
    n->running = 1;

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

    {
        MEMORYSTATUS ms;
        OSVERSIONINFOA os;
        SYSTEM_INFO si;
        DWORD spc = 0, bps = 0, freec = 0, totalc = 0;

        memset(&ms, 0, sizeof(ms));
        ms.dwLength = sizeof(ms);
        GlobalMemoryStatus(&ms);
        rc_strf(memkb, sizeof(memkb), "%lu",
                  (unsigned long)(ms.dwTotalPhys / 1024));
        HDR("mem_kb", memkb);
        rc_strf(memfree, sizeof(memfree), "%lu",
                  (unsigned long)(ms.dwAvailPhys / 1024));
        HDR("mem_free_kb", memfree);

        memset(&os, 0, sizeof(os));
        os.dwOSVersionInfoSize = sizeof(os);
        if (GetVersionExA(&os)) {
            rc_strf(osver, sizeof(osver), "Windows %lu.%lu build %lu %s",
                      (unsigned long)os.dwMajorVersion,
                      (unsigned long)os.dwMinorVersion,
                      (unsigned long)(os.dwBuildNumber & 0xFFFF),
                      os.szCSDVersion);
            HDR("os", osver);
        }

        memset(&si, 0, sizeof(si));
        GetSystemInfo(&si);
        rc_strf(cpu, sizeof(cpu), "x86 family %u model %u, %lu cpu",
                  (unsigned)si.wProcessorLevel,
                  (unsigned)(si.wProcessorRevision >> 8),
                  (unsigned long)si.dwNumberOfProcessors);
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

        /* GetDiskFreeSpaceEx is NT4+/OSR2 and a static import of it would kill
         * the image at load on an older 9x. The old call is fine here: the
         * numbers only have to be roughly right. */
        if (GetDiskFreeSpaceA("C:\\", &spc, &bps, &freec, &totalc)) {
            rc_strf(disk, sizeof(disk), "%lu",
                      (unsigned long)((double)spc * bps * freec / 1024.0));
            HDR("disk_free_kb", disk);
        }
    }
    if (n->sess[0]) HDR("sess", n->sess);

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
    rc_strf(acp, sizeof(acp), "%u", (unsigned)GetACP());
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
    rc_strf(oemcp, sizeof(oemcp), "%u", (unsigned)GetOEMCP());
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

    /* _beginthreadex, not CreateThread. Open Watcom's multi-threaded CRT
     * allocates its per-thread block here; a raw CreateThread thread never
     * gets one and never releases it. It also takes a non-NULL thread-id
     * slot, which Win95/98 requires -- CreateThread(..., NULL) fails there
     * with ERROR_INVALID_PARAMETER while NT accepts it. */
    /* Order matters. The worker must exist before the reader can route an
     * EXEC to it, or an early frame is dropped silently and the bridge waits
     * out its whole budget for a command nobody ran. */
    rc_rpc_start(n);
    n->sender = (HANDLE)_beginthreadex(NULL, 0, sender_thread, n, 0, &tid);
    if (!n->sender) {
        /* Without a sender nothing can put a byte on the wire -- including the
         * HELLO already queued. Reporting "connected" here would leave the app
         * looking healthy forever. */
        rc_log("net: could not start sender thread");
        rc_rpc_stop();
        rc_rpc_join(2000);
        closesocket(n->sock);
        n->sock = INVALID_SOCKET;
        n->running = 0;
        set_state(n, RC_FAILED);
        return -1;
    }
    n->thread = (HANDLE)_beginthreadex(NULL, 0, reader_thread, n, 0, &tid);
    if (!n->thread) {
        /* THE SAME TEARDOWN AS THE SENDER PATH ABOVE.
         *
         * This used to log, set RC_FAILED and return -- leaving a live sender
         * thread on an open socket, n->running still 1 and the RPC worker
         * still going. Recovery did save it: WM_RC_STATE arms
         * TIMER_RECONNECT and Reconnector::tryConnect calls rc_net_stop first, with
         * n->started already 1 so teardown runs. But that made correctness
         * depend on every future caller preserving an ordering this file
         * records as having been broken once already, at the cost of three
         * bridge restarts. _beginthreadex fails under the same USER/GDI and
         * thread pressure that makes CreateWindowExA fail, which is not a
         * theoretical condition on this machine.
         *
         * Note the sender is stopped by clearing n->running and closing the
         * socket: it wakes on outq_wake, sees running == 0 and exits. */
        rc_log("net: could not start reader thread");
        n->running = 0;
        rc_rpc_stop();
        rc_rpc_join(2000);
        if (n->outq_wake) SetEvent(n->outq_wake);
        closesocket(n->sock);
        n->sock = INVALID_SOCKET;
        if (n->sender) {
            WaitForSingleObject(n->sender, 2000);
            CloseHandle(n->sender);
            n->sender = NULL;
        }
        set_state(n, RC_FAILED);
        return -1;
    }

    set_state(n, RC_CONNECTED);
    return 0;
}

/* ------------------------------------------------------------- sending */
/* rc_net_send no longer touches the socket. It builds the frame, puts it on a
 * bounded queue and returns; one sender thread owns the wire.
 *
 * The old version held a CRITICAL_SECTION across a blocking send(), which had
 * two failure modes waiting for a second sender to arrive. A 32KB exec result
 * at 40kbit is ~6.5 seconds with the lock held, and the UI thread calls this
 * from do_send, do_stop, chats_switch and the permission bar -- so the message
 * pump stopped. And releasing the lock mid-frame to fix that would interleave
 * two frames on a stream with no resync, which drops the link. */

static void outq_free(rc_outq_item *it)
{
    HeapFree(GetProcessHeap(), 0, it);
}

/* Drop everything queued. Called on shutdown; the socket is already going. */
static void outq_clear(rc_net *n)
{
    rc_outq_item *it;
    {
        ScopedLock lock(&n->send_lock);
        it = n->outq_head;
        n->outq_head = n->outq_tail = NULL;
        n->outq_bytes = 0;
    }
    while (it) {
        rc_outq_item *next = it->next;
        outq_free(it);
        it = next;
    }
    if (n->outq_room) SetEvent(n->outq_room);
}

static unsigned sender_thread_body(void *arg)
{
    rc_net *n = (rc_net *)arg;

    while (n->running) {
        rc_outq_item *it = NULL;

        {
            ScopedLock lock(&n->send_lock);
            if (n->outq_head) {
                it = n->outq_head;
                n->outq_head = it->next;
                if (!n->outq_head) n->outq_tail = NULL;
                n->outq_bytes -= it->len;
            }
        }

        if (!it) {
            /* Timed wait rather than infinite: `running` going to 0 must be
             * noticed even if nothing is ever queued again. */
            WaitForSingleObject(n->outq_wake, 200);
            continue;
        }
        /* A writer may have been waiting for space. */
        SetEvent(n->outq_room);

        while (it->sent < it->len && n->running) {
            int w;
            SOCKET s = n->sock;
            if (s == INVALID_SOCKET) break;
            w = send(s, it->data + it->sent, (int)(it->len - it->sent), 0);
            if (w == SOCKET_ERROR) {
                rc_log("net: send failed: %d", WSAGetLastError());
                n->running = 0;
                break;
            }
            it->sent += (unsigned)w;
            n->tx_bytes += (unsigned long)w;
        }
        outq_free(it);
    }
    outq_clear(n);
    return 0;
}

/* Catches std::bad_alloc, which operator new throws (core/heap.cpp): an
 * exception leaving a thread's entry point ends the process with no log. */
static unsigned __stdcall sender_thread(void *arg)
{
    try
    {
        return sender_thread_body(arg);
    }
    catch (std::bad_alloc &)
    {
        rc_out_of_memory("the network sender");
    }
    return 0;
}

/* The one implementation. `wait_ticks` is how many 100ms slices this caller
 * will spend waiting for the outbound queue to drain before giving up -- see
 * the two wrappers at the end of this function for who uses which. */
static int send_frame(rc_net *n, const char *type,
                      const char *const *keys, const char *const *vals, int nkv,
                      const void *body, unsigned body_len, int wait_ticks)
{
    /* A SHARED static, serialised by send_lock -- which is what the two
     * comments that used to sit here denied. One said "there is no shared
     * build buffer to race over any more" directly above this declaration;
     * the other said the buffer was "still shared, but only for the memcpy",
     * which is true and was the opposite of the first.
     *
     * It is 66,560 bytes committed at load, the single largest static in the
     * image. The largest frame actually built is SYNCENT at 63,488 plus
     * headers, and rc_frame_build refuses anything over RC_MAX_FRAME
     * (frameproto.c), so the + 1024 of slack is roughly 1020 bytes more than
     * can ever be used. Left alone deliberately: BACKLOG.md §2 owns the
     * memory pass, and shrinking a buffer to its measured high-water mark is
     * exactly the change that wants measuring rather than reasoning. */
    static char build[RC_MAX_FRAME + 1024];
    rc_outq_item *it;
    int total;
    int waited = 0;

    if (n->sock == INVALID_SOCKET || !n->running) return RC_SEND_DEAD;

    /* The lock covers building INTO the shared buffer and copying OUT of it.
     * Neither can block, which is the property that matters: the wire write
     * happens on the sender thread, from the heap block, with this released. */
    {
        ScopedLock lock(&n->send_lock);

        /* Backpressure, in bytes. Wait for the sender to drain room before
         * compressing and allocating `it`. Waiting before mz_deflate ensures
         * that if the send times out with RC_SEND_FULL, no frame was compressed
         * into the stream, so the persistent dictionary is never corrupted by
         * a dropped frame. Furthermore, send_lock is not released after
         * mz_deflate, preserving strict stream order. */
        while (n->outq_bytes + body_len + 256 > RC_OUTQ_MAX_BYTES && n->running
               && waited < wait_ticks) {
            LeaveCriticalSection(&n->send_lock);
            WaitForSingleObject(n->outq_room, 100);
            waited++;
            EnterCriticalSection(&n->send_lock);
        }
        if (!n->running) return RC_SEND_DEAD;
        if (n->outq_bytes + body_len + 256 > RC_OUTQ_MAX_BYTES) {
            rc_log("net: send queue full after %dms wait, dropping %s "
                   "(body=%u bytes, outq_bytes=%u)", waited * 100, type, body_len,
                   n->outq_bytes);
            return RC_SEND_FULL;
        }

        const void *send_body = body;
        unsigned send_len = body_len;
        const char *def_keys[RC_HELLO_MAX + 2];
        const char *def_vals[RC_HELLO_MAX + 2];
        const char *const *use_keys = keys;
        const char *const *use_vals = vals;
        int use_nkv = nkv;

        if (rc_frame_compressible(type) && s_codec.shouldCompress(body_len)) {
            unsigned def_len;
            const char *def_body = s_codec.compressBody(body, body_len, &def_len);
            if (def_body) {
                send_body = def_body;
                send_len = def_len;

                if (nkv < (int)(sizeof(def_keys) / sizeof(def_keys[0])) - 1) {
                    int i;
                    for (i = 0; i < nkv; ++i) {
                        def_keys[i] = keys[i];
                        def_vals[i] = vals[i];
                    }
                    def_keys[nkv] = "enc";
                    def_vals[nkv] = "deflate";
                    use_keys = def_keys;
                    use_vals = def_vals;
                    use_nkv = nkv + 1;
                }
            }
        }

        total = rc_frame_build(build, sizeof(build), type, use_keys, use_vals, use_nkv,
                               send_body, send_len);
        if (total < 0) {
            rc_log("net: frame too large to build (%s, body=%u)", type, send_len);
            return RC_SEND_DEAD;
        }
        it = (rc_outq_item *)HeapAlloc(GetProcessHeap(), 0,
                                       sizeof(rc_outq_item) + (unsigned)total);
        if (!it) {
            rc_log("net: out of memory queueing %s (%d bytes)", type, total);
            return RC_SEND_DEAD;
        }
        memcpy(it->data, build, (unsigned)total);
        it->len = (unsigned)total;
        it->sent = 0;
        it->next = NULL;

        if (n->outq_tail) n->outq_tail->next = it;
        else              n->outq_head = it;
        n->outq_tail = it;
        n->outq_bytes += it->len;
    }

    SetEvent(n->outq_wake);
    return RC_SEND_OK;
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

void rc_net_stop(rc_net *n)
{
    DWORD rw = WAIT_OBJECT_0, sw = WAIT_OBJECT_0;
    int   rpcj;

    /* Safe to call on a connection that never started, so the reconnect path
     * can call it unconditionally rather than tracking state itself. */
    if (!n->started) return;
    n->started = 0;

    /* SHUTDOWN IS THREE PHASES, AND THE ORDER IS THE WHOLE POINT (01.51).
     *
     * `running` used to be cleared right here, before rc_rpc_stop(). But
     * rc_net_send bails on `!n->running`, so every ack the RPC worker's
     * shutdown drain tried to send returned -1 -- and rpc.cpp's comment over
     * that drain says the frames are "answered, not silently dropped -- the
     * bridge is holding a future for it". That was false on the only path
     * that reaches it in production. The bridge waited out the whole timeout
     * for an answer the guest had already composed and thrown away.
     *
     * The mistake was that one flag meant two things: "the link is usable"
     * and "accept new work". They are separated by sequence now.
     *
     * PHASE 1 -- let the worker finish and ANSWER. `running` is still 1, the
     * socket is still open and the sender thread is still on the wire, so the
     * drain's acks can actually leave. rc_rpc_stop kills any child process
     * first, so this cannot wait on a two-minute exec. The 30s budget is the
     * same one that used to sit further down; it has moved, not grown. */
    rc_rpc_stop();              /* signals the worker and kills any child */
    rpcj = rc_rpc_join(30000);
    if (!rpcj)
        rc_log("net: RPC worker did not exit");

    /* PHASE 2 -- give those acks time to reach the wire. The sender pops an
     * item before writing it, so an empty queue means "handed to send()", not
     * "acknowledged by the peer" -- which is as much as this side can know
     * anyway, and the shutdown(SD_SEND) below is what actually flushes. A
     * bounded wait: a link slow enough to need more than two seconds for a
     * handful of small acks is a link that is already gone. */
    {
        int waited = 0;
        for (;;) {
            int empty;
            {
                ScopedLock lock(&n->send_lock);
                empty = (n->outq_head == NULL);
            }
            if (empty || waited >= 200 || !n->running) break;
            Sleep(10);
            waited++;
        }
        if (waited >= 200)
            rc_log("net: outbound queue still had work after 2s of draining");
    }

    /* PHASE 3 -- now nothing new is accepted and the threads may go. */
    n->running = 0;
    /* Wake anything parked on the queue so it can notice `running`. */
    if (n->outq_wake) SetEvent(n->outq_wake);
    if (n->outq_room) SetEvent(n->outq_room);

    /* FIN NOW, CLOSE LATER -- and the gap between those is finding 01.52.
     *
     * `n->sock` is read and written by four threads with no lock: the reader
     * in recv_exact, the sender in its write loop, the RPC worker through
     * rc_net_send, and this one. The named hazard is the sender snapshotting
     * the descriptor, passing the INVALID_SOCKET test, and being preempted
     * right there while this function closes it. Win9x recycles descriptors
     * aggressively, so the resumed send() does not fail -- it can land on
     * whatever socket was opened in the meantime and write a frame into it.
     *
     * A lock is the obvious fix and the wrong one: the reader holds the
     * socket across a BLOCKING recv, so any mutex covering it would be held
     * for the length of an idle connection and this function would wait
     * behind it.
     *
     * Ordering removes the race instead of guarding it. The descriptor is not
     * freed until both threads that can touch it have been joined, so there
     * is no window in which a live thread holds a stale one. Only the FIN
     * happens here; closesocket moved below the joins.
     *
     * The FIN itself stays graceful, and that part is not stylistic: an
     * abrupt RST is reported to crash Win98's Winsock stack outright, taking
     * the machine with it. This is client-side discipline because the server
     * cannot defend itself. The FIN is also what unblocks the reader -- the
     * bridge sees it, closes, and recv returns 0 -- so it has to be sent
     * before the join rather than after it, or the join waits out its whole
     * timeout on an idle link every single time. */
    if (n->sock != INVALID_SOCKET)
        shutdown(n->sock, 1 /* SD_SEND */);

    /* Join every thread that can touch send_lock BEFORE deleting it. The old
     * code waited 2000ms, DISCARDED the result and deleted the section
     * regardless -- so any exec longer than two seconds meant the worker's
     * next EnterCriticalSection ran on a destroyed object, which on 9x is a
     * GPF dialog at exit, every time. */
    if (n->thread) {
        rw = WaitForSingleObject(n->thread, 5000);
        CloseHandle(n->thread);
        n->thread = NULL;
    }
    if (n->sender) {
        sw = WaitForSingleObject(n->sender, 5000);
        CloseHandle(n->sender);
        n->sender = NULL;
    }
    /* The worker calls rc_net_send, so it holds send_lock as readily as the
     * other two. An earlier version computed this join and then left it out of
     * the condition, which destroyed the section under a live worker -- the
     * exact GPF-at-exit the rewrite existed to remove. `rpcj` is from PHASE 1
     * above; the worker was joined BEFORE the socket closed so its drain could
     * still be answered, but the condition it feeds here is unchanged. */
    /* NOW the descriptor can go: nothing is left inside send() or recv().
     * If a thread did not exit, the socket is deliberately LEAKED rather than
     * closed under it -- the same trade as the critical section below, and
     * for the same reason. A leaked descriptor at process exit costs nothing;
     * one freed under a live send() is a frame written into whatever the OS
     * handed out next. */
    if (n->sock != INVALID_SOCKET) {
        if (rw == WAIT_OBJECT_0 && sw == WAIT_OBJECT_0) {
            closesocket(n->sock);
            n->sock = INVALID_SOCKET;
        } else {
            rc_log("net: reader=%lu sender=%lu still running; leaking the "
                   "socket rather than closing it under them",
                   (unsigned long)rw, (unsigned long)sw);
        }
    }

    if (rw == WAIT_OBJECT_0 && sw == WAIT_OBJECT_0 && rpcj) {
        outq_clear(n);
        DeleteCriticalSection(&n->send_lock);
    } else {
        /* A thread is still in there somewhere. Leaking a critical section at
         * process exit costs nothing; destroying one out from under a live
         * thread costs the user a crash dialog. */
        n->leaked = 1;
        rc_log("net: reader=%lu sender=%lu did not exit; not deleting the lock",
               (unsigned long)rw, (unsigned long)sw);
    }

    if (rw == WAIT_OBJECT_0 && sw == WAIT_OBJECT_0 && rpcj) {
        /* Only once nothing can still be waiting on them. Closing these under
         * a surviving thread turns its WaitForSingleObject into an instant
         * WAIT_FAILED, i.e. a hot spin. */
        if (n->outq_wake) { CloseHandle(n->outq_wake); n->outq_wake = NULL; }
        if (n->outq_room) { CloseHandle(n->outq_room); n->outq_room = NULL; }
    }
    /* GATED, like every release above it -- this line was the one exception.
     *
     * finishInflate() frees the 128KB scratch and ends the mz_stream, and
     * BOTH are touched only by the READER thread, inside inflateBody(). If
     * the reader's join timed out, that free lands under a live mz_inflate:
     * exactly the hazard that makes the socket, send_lock and both events
     * above deliberately leak rather than be released. Only `rw` is relevant
     * -- the sender and the RPC worker never touch either object.
     * See docs/mem-audit F3. */
    if (rw == WAIT_OBJECT_0) {
        s_codec.finishInflate();
    } else {
        rc_log("net: reader did not exit; leaking the inflate scratch rather "
               "than freeing it under a live mz_inflate");
    }

    if (rpcj && sw == WAIT_OBJECT_0) {
        s_codec.finishDeflate();
    } else {
        rc_log("net: worker/sender did not exit; leaking the deflate scratch rather "
               "than freeing it under a live mz_deflate");
    }

    /* Same argument one step wider. WSACleanup tears Winsock down for the
     * whole process, and a surviving reader or sender is still inside
     * recv()/send() on the descriptor this function has just decided to leak
     * for that very reason. The refcount it leaves behind is reclaimed at
     * process exit; a socket call into a torn-down stack is not. */
    if (rw == WAIT_OBJECT_0 && sw == WAIT_OBJECT_0) {
        WSACleanup();
    } else {
        rc_log("net: reader=%lu sender=%lu still running; skipping WSACleanup",
               (unsigned long)rw, (unsigned long)sw);
    }
}
