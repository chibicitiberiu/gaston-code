#include "ui/uirpc.h"
#include "ui/uiengine.h"
#include "ui/uirec.h"
#include "ui/inject.h"
#include "automation/resolve.h"
#include "net/caps.h"
#include "automation/shot.h"
#include "ui/inject.h"
#include "core/log.h"
#include "core/rc_cpp.h"
#include "core/str.h"

#include <stdlib.h>
#include <string.h>

/* The worker's address, for a router that is C and has no object to reach
 * through. Written once as the thread comes up and once as it goes down,
 * both from that thread; read by the reader thread. rpc.cpp keeps `g_net`
 * the same way, and the rule against per-module state is about the
 * LIBRARIES -- this is exe code that exists to bridge a C callback to an
 * object. */
static volatile LONG g_ui_tid;
static rc_net      *g_ui_net;
static UiContext   *g_ui_ctx;

/* How many requests may be waiting. REMOTE.md wants one in flight per lane;
 * the queue is serviced serially so that holds, and this bounds how far
 * ahead a bridge can run before it is told to wait. Small on purpose: a ui
 * request is seconds, so a deep queue only produces answers nobody still
 * wants. */
enum { UI_QUEUE_MAX = 4 };
static volatile LONG g_ui_queued;

/* The in-process tier's controller. It was a member of UiContext until
 * 2026-10-03, which linked inject.cpp into every program that held one --
 * and gremote holds one and refuses that tier, so it belongs with the only
 * handler that uses it.
 *
 * A POD pointer owned by attach/detach, exactly like g_ui_ctx above: a
 * file-scope object with a constructor runs before WinMain, and `make check`
 * refuses one. */
static Injector *g_injector;

/* The request that is running, if one is, and its stop flag. A plain buffer
 * the reader compares against: a race here can only miss a cancel for a
 * request that is finishing anyway, or arrive for none, and both are logged.
 *
 * It covers all three ops, not just the step list. UICANCEL stopped a UIRUN
 * from the day it was built and left UITREE and UISHOT to run to completion
 * -- which is BACKLOG.md 26: the desktop worker is single-threaded, so a
 * cancelled enumeration is work the NEXT request queues behind. The step
 * runner already polled a flag; the walk and the capture needed two polls on
 * functions that already returned bool for exactly this
 * (NodeSink::onNode -> WALK_STOPPED, ByteSink::write). */
static volatile LONG g_run_cancel;
static char          g_run_id[48];

/* And the one a cancel ARRIVED FOR but could not stop, because the request
 * was still in the queue rather than in the worker's hands.
 *
 * Found with a probe on the guest, 2026-10-03: eight attempts to cancel a
 * desktop walk mid-flight and the cancel won every race, logging "matches no
 * running request" each time. The reader answers UICANCEL immediately -- it
 * is RC_LANE_IMMEDIATE precisely so it does not queue behind the work it is
 * stopping -- so between rc_uirpc_post and the worker picking the frame up
 * there is a window where nothing yet knows the id. A request that is
 * cancelled before it starts should not run.
 *
 * One slot and not a table: the queue is four deep, but a caller cancels the
 * request it just sent, and remembering the last one asked for is what that
 * costs. The worst case of being wrong is a cancel for a request that never
 * arrives, which is cleared by the next one. */
static char          g_cancel_id[48];

void rc_uirpc_attach(DWORD threadId, rc_net *net, UiContext *ctx)
{
    g_ui_net = net;
    g_ui_ctx = ctx;
    g_ui_queued = 0;
    /* Both id buffers are cleared here, not just on each request. The
     * bridge restarts its ui ids at u1 on a fresh session, so a cancel
     * remembered for a request that never ran would otherwise be waiting
     * for the same id on the next connection and stop a request nobody
     * asked to stop. */
    g_run_id[0] = '\0';
    g_cancel_id[0] = '\0';
    InterlockedExchange(&g_run_cancel, 0);
    /* The in-process tier's controller, made on the thread that will use it
     * and nowhere near a file-scope constructor. A failure here is not fatal:
     * `inproc` then answers its own `no_dll`, which is what it would have
     * said anyway on a machine with no HOOKDLL.DLL beside the exe. */
    g_injector = new Injector;
    InterlockedExchange(&g_ui_tid, (LONG)threadId);
}

void rc_uirpc_detach(void)
{
    InterlockedExchange(&g_ui_tid, 0);
    delete g_injector;
    g_injector = 0;
}

int rc_uirpc_post(rc_frame *f)
{
    LONG tid = InterlockedCompareExchange(&g_ui_tid, 0, 0);
    if (!tid || !f)
        return -1;
    if (InterlockedIncrement(&g_ui_queued) > UI_QUEUE_MAX)
    {
        InterlockedDecrement(&g_ui_queued);
        return -1;
    }
    if (!PostThreadMessageA((DWORD)tid, RC_WM_UI_REQUEST, 0, (LPARAM)f))
    {
        InterlockedDecrement(&g_ui_queued);
        return -1;
    }
    return 0;
}

/* ---- the step list's stop, and this exe's end of the seam ------------ */


void rc_uirpc_cancel(const char *id)
{
    if (!id)
    {
        rc_log("desktop: UICANCEL with no id -- ignored");
        return;
    }
    if (g_run_id[0] && strcmp(id, g_run_id) == 0)
    {
        InterlockedExchange(&g_run_cancel, 1);
        return;
    }
    /* Not the one running, so either it is still queued or it is already
     * over. Remember it; the worker checks this as it picks each request up
     * and starts that one already stopped. */
    rc_strf(g_cancel_id, sizeof(g_cancel_id), "%s", id);
}

/* The client's UiLink: the engine's calls as the frames the bridge parses.
 *
 * `report()` stashes rather than sends, because the client's wire puts the
 * step report in the UIEND's own body -- one frame, which is what
 * bridge/agent/steps.py has always parsed. gremote's link sends it as DATA
 * instead; the engine does not know or care which. */
class NetUiLink : public UiLink
{
public:
    NetUiLink(rc_net *net, const char *id)
        : m_net(net), m_body(0), m_bodyLen(0)
    {
        rc_strncpy(m_id, sizeof(m_id), id ? id : "",
                   (unsigned)strlen(id ? id : ""));
    }

    virtual bool end(const char *status, const char *const *keys,
                     const char *const *vals, int nkv)
    {
        const char *k[8];
        const char *v[8];
        int n = 0, i;
        k[n] = "id";     v[n++] = m_id;
        k[n] = "status"; v[n++] = status;
        for (i = 0; i < nkv && n < 8; i++)
        {
            k[n] = keys[i];
            v[n++] = vals[i];
        }
        rc_net_send(m_net, "UIEND", k, v, n, m_body, m_bodyLen);
        return true;
    }

    virtual bool records(const void *body, unsigned len, int n)
    {
        const char *k[2];
        const char *v[2];
        char nbuf[16];
        rc_strf(nbuf, sizeof(nbuf), "%d", n);
        k[0] = "id"; v[0] = m_id;
        k[1] = "n";  v[1] = nbuf;
        rc_net_send(m_net, "UIENT", k, v, 2, body, len);
        return true;
    }

    virtual bool bytes(const void *body, unsigned len)
    {
        const char *k[1];
        const char *v[1];
        k[0] = "id"; v[0] = m_id;
        rc_net_send(m_net, "UIDAT", k, v, 1, body, len);
        return true;
    }

    virtual bool report(const void *body, unsigned len)
    {
        m_body = body;
        m_bodyLen = len;
        return true;
    }

    virtual unsigned bodyCap() const { return RC_MAX_FRAME - 2048; }

    /* Live, not captured: turning the capability off takes effect on the
     * next request rather than the next connection. */
    virtual int automationLevel() const
    {
        return m_net ? m_net->automation : RC_AUTOMATION_NONE;
    }

    virtual const UiLimits &limits() const { return m_limits; }
    virtual volatile LONG *stopFlag() { return &g_run_cancel; }

private:
    rc_net     *m_net;
    const void *m_body;
    unsigned    m_bodyLen;
    UiLimits    m_limits;
    char        m_id[64];
};

void rc_uirpc_refuse(rc_net *net, rc_frame *f, const char *why)
{
    const char *id = rc_frame_get(f, "id");
    if (!id)
    {
        rc_log("%s: no id -- dropped, and the bridge will wait out its "
               "timeout", f->type);
        return;
    }
    {
        NetUiLink link(net, id);
        link.end(why, NULL, NULL, 0);
    }
}

/* ---- the in-process tier, which stays here ---------------------------- */



/* The in-process tier, behind the same op.
 *
 * Opt-in per request and never the default: this puts our code inside a
 * program that did not ask for it. It also REFUSES rather than falling back
 * silently, and the distinction matters -- the two tiers give different
 * answers and only one of them can read a list box, so a caller has to be
 * able to tell which one it got. `status=` says so either way.
 */
static void do_tree_inproc(UiLink &link, UiContext *ctx, rc_frame *f,
                           const WalkOptions &o)
{
    const char *target = rc_frame_get(f, "target");
    const char *k[6];
    const char *v[6];
    char nodes[16], items[16], ms[16];
    int nkv = 0;

    if (!g_injector->open())
    {
        k[0] = "detail"; v[0] = g_injector->lastError();
        link.end("inproc_unavailable", k, v, 1);
        return;
    }

    /* A window of the target, found the ordinary way. The in-process tier
     * still needs the out-of-process one to know where to go. */
    HWND win = NULL;
    if (target && target[0])
    {
        win = rc_ui_resolve_target(link, ctx, f, o);
        if (!win)
            return;             /* already answered */
    }
    else
    {
        win = GetForegroundWindow();
        if (!win)
        {
            link.end("no_window", NULL, NULL, 0);
            return;
        }
    }

    InjectResult r = g_injector->collect(
        win, NULL, rc_ui_header_flag(f, "items", true),
        (DWORD)rc_ui_header_num(f, "timeout_ms", 3000, 200, 20000));

    if (r != INJECT_OK && r != INJECT_TRUNCATED)
    {
        static const char *const NAMES[] = {
            "ok", "denied", "no_dll", "no_window", "hook_refused",
            "no_reply", "truncated", "failed"
        };
        k[0] = "detail"; v[0] = g_injector->lastError();
        link.end(NAMES[(int)r], k, v, 1);
        return;
    }

    const RcInShm *shm = g_injector->shm();
    TreeSink sink(link, ctx, false);
    if (!sink.ready())
    {
        link.end("no_memory", NULL, NULL, 0);
        return;
    }

    /* The collector works in HWNDs and the wire works in paths, so the
     * path is built here -- and it has to NEST, because the bridge walks
     * `parent` up the chain to build a selector. A first version keyed the
     * parent on the node's array index, which is not a path: a grandchild
     * came out as "3/0" while node 3's own path was "1/2", so the chain
     * broke at the second level and every selector silently lost its
     * ancestry.
     *
     * The collector emits depth-first, exactly as `tree.cpp` does, so the
     * same trick works: a stack indexed by depth holds the path of the
     * node currently open at each level. On the HEAP, because seventeen
     * 64-byte paths on the stack is most of the 4096 a frame may use
     * before it steps over Win98's guard page. */
    HeapPtr<RcPathBuilder> ps((RcPathBuilder *)HeapAlloc(
        GetProcessHeap(), 0, sizeof(RcPathBuilder)));
    if (!ps.valid())
    {
        link.end("no_memory", NULL, NULL, 0);
        return;
    }
    rc_path_reset(*ps.get());

    for (long i = 0; i < shm->nodeN; i++)
    {
        const RcInNode &n = shm->nodes[i];
        char path[RC_PATH_MAX];
        char parent[RC_PATH_MAX];

        rc_path_next(*ps.get(), n.depth, path, parent);

        /* Through the sink's own accessors, not its fields. It has two
         * writers -- this tier and the MSAA item walk in the engine -- and
         * reaching in to move `len` and `batch` from both is how the batch
         * count and the buffer get out of step. */
        unsigned w = rc_uirec_inproc(sink.room(), sink.left(), n, path,
                                     parent);
        if (!w)
        {
            sink.flush();
            w = rc_uirec_inproc(sink.room(), sink.left(), n, path, parent);
        }
        if (w) sink.took(w);
        else   sink.dropped++;

        /* The rows. THE reason this tier exists: LB_GETTEXT and friends
         * take a pointer and nothing marshals it across a process boundary
         * on 9x, at any clock speed. */
        for (long it = 0; it < n.itemN; it++)
        {
            long idx = n.itemFirst + it;
            char ipath[RC_PATH_MAX];
            if (idx < 0 || idx >= shm->itemN)
                break;
            rc_strf(ipath, sizeof(ipath), "%s/i%ld", path, it);
            w = rc_uirec_item(sink.room(), sink.left(),
                              shm->items[idx].text, ipath, path, it);
            if (!w)
            {
                sink.flush();
                w = rc_uirec_item(sink.room(), sink.left(),
                                  shm->items[idx].text, ipath, path, it);
            }
            if (w) sink.took(w);
            else   sink.dropped++;
        }
    }
    sink.flush();

    rc_strf(nodes, sizeof(nodes), "%d", sink.total);
    rc_strf(items, sizeof(items), "%ld", shm->itemN);
    rc_strf(ms, sizeof(ms), "%ld", shm->elapsedMs);
    k[nkv] = "nodes";  v[nkv++] = nodes;
    k[nkv] = "items";  v[nkv++] = items;
    k[nkv] = "tier";   v[nkv++] = "inproc";
    k[nkv] = "walk";   v[nkv++] = (r == INJECT_TRUNCATED) ? "maxnodes"
                                                          : "complete";
    k[nkv] = "took_ms"; v[nkv++] = ms;
    link.end("ok", k, v, nkv);
}

/* ---- ITEMS: the accessible children of one control ------------------
 *
 * What `item` targets (`input`'s `{"click": SEL, "item": "readme.txt"}`):
 * list rows, tree nodes, tab and toolbar buttons, desktop icons -- the
 * things that have no HWND, so the window walk cannot see them and the agent
 * had no way to learn their names but a screenshot. One control's MSAA
 * subtree, streamed as 'a' records through the same UIENT batches a tree
 * uses. Bounded: 400 elements, 3 levels, the request's deadline. */
/* CLICK, FOCUS and KEYS lived here until 2026-10-02. A client that runs
 * step lists is offered `input` instead (ui/uiengine.cpp), which does all
 * three in one round trip with the focus check and held-key release they
 * never had, so the single-shot ops were retired with their bridge tools.
 * The library calls they made -- Input::clickWindow, focus, typeText -- stay,
 * measured by autocheck and the probes. */

/* ---- SHOT ------------------------------------------------------------ */

/* Straight into frames. The image is never held whole: a 640x480 at 8bpp is
 * 300KB and the guest has 28KB free after a turn, so the capture is
 * strip-wise and each strip goes out as it is produced. */
class ShotFrames : public ByteSink
{
public:
    ShotFrames(UiLink &link)
        : m_link(link), m_cap(link.bodyCap()),
          m_buf((char *)HeapAlloc(GetProcessHeap(), 0, link.bodyCap())),
          m_len(0), m_sent(0), m_ok(true), m_stopped(false)
    {
    }
    ~ShotFrames() { if (m_buf) HeapFree(GetProcessHeap(), 0, m_buf); }

    bool ready() const { return m_buf != 0; }
    bool ok() const { return m_ok; }
    bool stopped() const { return m_stopped; }
    unsigned sent() const { return m_sent; }

    virtual bool write(const void *data, unsigned len)
    {
        const char *p = (const char *)data;
        /* Stopped, not just disconnected. A 640x480 at 8bpp is 300KB that
         * nobody is waiting for, and the strip boundary is where ByteSink's
         * false is meant to be read. */
        volatile LONG *stop = m_link.stopFlag();
        if (stop && *stop)
        {
            m_stopped = true;
            return false;
        }
        while (len)
        {
            unsigned take = m_cap - m_len;
            if (take > len)
                take = len;
            memcpy(m_buf + m_len, p, take);
            m_len += take;
            p += take;
            len -= take;
            if (m_len == m_cap && !flush())
                return false;
        }
        return true;
    }

    bool flush()
    {
        if (!m_len)
            return true;
        if (!m_link.bytes(m_buf, m_len))
        {
            /* The connection has gone. Stop rather than produce a megabyte
             * for nobody -- which is what ByteSink's false is for. */
            m_ok = false;
            return false;
        }
        m_sent += m_len;
        m_len = 0;
        return true;
    }

private:
    UiLink  &m_link;
    unsigned m_cap;
    char    *m_buf;
    unsigned m_len;
    unsigned m_sent;
    bool     m_ok;
    bool     m_stopped;

    ShotFrames(const ShotFrames &);
    ShotFrames &operator=(const ShotFrames &);
};

static void do_shot(UiLink &link, UiContext *ctx, rc_frame *f)
{
    const char *k[5];
    const char *v[5];
    char w[16], h[16], bpp[16], bytes[16];
    ShotOptions o;
    int nkv = 0;

    (void)ctx;

    o.maxBpp = (int)rc_ui_header_num(f, "bpp", 8, 4, 32);
    o.rect.left   = (LONG)rc_ui_header_num(f, "x", 0, 0, 0x4000);
    o.rect.top    = (LONG)rc_ui_header_num(f, "y", 0, 0, 0x4000);
    o.rect.right  = (LONG)rc_ui_header_num(f, "w", 0, 0, 0x4000);
    o.rect.bottom = (LONG)rc_ui_header_num(f, "h", 0, 0, 0x4000);
    /* A width and a height, not a right and a bottom: a caller says "100
     * wide", and making it say "left plus 100" is an arithmetic trap. */
    if (o.rect.right)  o.rect.right  += o.rect.left;
    if (o.rect.bottom) o.rect.bottom += o.rect.top;

    ShotFrames sink(link);
    if (!sink.ready())
    {
        link.end("no_memory", NULL, NULL, 0);
        return;
    }

    Screenshot shot;
    bool got = shot.capture(o, sink);
    if (got)
        got = sink.flush();

    if (!got)
    {
        /* Three reasons a capture stops, and they are not the same answer:
         * somebody cancelled it, the link died, or GDI refused. */
        if (sink.stopped())
        {
            link.end("cancelled", NULL, NULL, 0);
            return;
        }
        k[0] = "detail"; v[0] = sink.ok() ? shot.error() : "connection lost";
        link.end("failed", k, v, 1);
        return;
    }

    rc_strf(w, sizeof(w), "%d", shot.width());
    rc_strf(h, sizeof(h), "%d", shot.height());
    rc_strf(bpp, sizeof(bpp), "%d", shot.bpp());
    rc_strf(bytes, sizeof(bytes), "%u", sink.sent());
    k[nkv] = "w";     v[nkv++] = w;
    k[nkv] = "h";     v[nkv++] = h;
    k[nkv] = "bpp";   v[nkv++] = bpp;
    k[nkv] = "bytes"; v[nkv++] = bytes;
    link.end("ok", k, v, nkv);
}

/* ---- the table ------------------------------------------------------- */

void rc_uirpc_handle(rc_net *net, UiContext *ctx, rc_frame *f)
{
    InterlockedDecrement(&g_ui_queued);

    if (!ctx->ready)
    {
        rc_uirpc_refuse(net, f, "busy");
        return;
    }

    /* THE CONTROL, not the optimisation (BACKLOG.md 19).
     *
     * The handshake tells the bridge not to offer the five screen tools when
     * `[ui] automation` is off, and that is where the context saving is -- but
     * a declaration cannot be the control, for two reasons. A conversation
     * outlives a reconnect, so a session built while the setting was on still
     * holds the five tools after the client comes back declaring none of them.
     * And nothing about the bridge is under this machine's control anyway: the
     * only place that can refuse to move somebody's mouse is the process that
     * would move it.
     *
     * Read from `net` on every request rather than captured in the context, so
     * turning the capability OFF takes effect on the next request instead of
     * the next connection -- the asymmetry 19 asks for, since the safe
     * direction should never wait. */
    if (net && net->automation == RC_AUTOMATION_NONE)
    {
        /* No log line of its own: ui_end carries the status to the bridge,
         * which already logs every refusal and holds the sentence the agent
         * reads. A second copy here would cost CONST bytes on the one image
         * that has none spare (below). */
        rc_uirpc_refuse(net, f, "automation_off");
        return;
    }

    {
        const char *id = rc_frame_get(f, "id");
        if (!id)
        {
            rc_log("%s: no id -- dropped, and the bridge will wait out its "
                   "timeout", f->type);
            return;
        }
        NetUiLink link(net, id);

        /* Armed for EVERY op, and before the work rather than inside it, so
         * a UICANCEL that arrives while a request is still being decoded is
         * not lost: the walk, the capture or the runner sees the flag on
         * its first node, strip or step instead.
         *
         * A cancel that arrived while this request was still in the QUEUE is
         * honoured here, which is the race a guest probe lost eight times
         * out of eight before this existed. */
        InterlockedExchange(&g_run_cancel,
                            g_cancel_id[0] && !strcmp(id, g_cancel_id));
        g_cancel_id[0] = '\0';
        rc_strf(g_run_id, sizeof(g_run_id), "%s", id);

        if (strcmp(f->type, "UITREE") == 0)
        {
            /* The in-process branch is OURS, and is taken before the shared
             * handler sees the frame. gremote does not carry that tier --
             * it needs HOOKDLL.DLL beside the exe -- and a shared handler
             * that fell through to the out-of-process walk would answer a
             * caller who asked for the in-process answer with the other
             * one. An empty list box reads the same either way, which is
             * why the two tiers have never been allowed to substitute. */
            if (rc_ui_header_flag(f, "inproc", false))
            {
                WalkOptions o;
                rc_ui_base_options(o, link, ctx->selfPid, f);
                do_tree_inproc(link, ctx, f, o);
            }
            else
                rc_ui_tree(link, ctx, f);
        }
        else if (strcmp(f->type, "UISHOT") == 0)
            do_shot(link, ctx, f);
        else if (strcmp(f->type, "UIRUN") == 0)
            rc_ui_run(link, ctx, f);
        else if (strcmp(f->type, "UICLIP") == 0)
            rc_ui_clip(link, f);           /* no context: see uiengine.h */
        else
            rc_log("desktop: unhandled frame %s", f->type);
        g_run_id[0] = '\0';
    }
}
