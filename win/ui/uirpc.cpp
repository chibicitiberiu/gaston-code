#include "ui/uirpc.h"
#include "ui/uicontext.h"
#include "ui/uirec.h"
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

void rc_uirpc_attach(DWORD threadId, rc_net *net, UiContext *ctx)
{
    g_ui_net = net;
    g_ui_ctx = ctx;
    g_ui_queued = 0;
    InterlockedExchange(&g_ui_tid, (LONG)threadId);
}

void rc_uirpc_detach(void)
{
    InterlockedExchange(&g_ui_tid, 0);
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

/* ---- replies -------------------------------------------------------- */

/* Every request ends in exactly one of these, whatever happened. A request
 * that produces no UIEND costs the bridge its whole timeout and tells it
 * nothing, which is the failure `rpc.cpp`'s reject_busy exists to avoid. */
static void ui_end(rc_net *net, const char *id, const char *status,
                   const char *const *keys, const char *const *vals, int nkv)
{
    const char *k[8];
    const char *v[8];
    int n = 0, i;

    k[n] = "id";     v[n++] = id ? id : "";
    k[n] = "status"; v[n++] = status;
    for (i = 0; i < nkv && n < 8; i++)
    {
        k[n] = keys[i];
        v[n++] = vals[i];
    }
    rc_net_send(net, "UIEND", k, v, n, NULL, 0);
}

/* "More than one matched", with WHO matched: up to RC_SAMPLE_MAX
 * candidates, each with the ancestors the agent can narrow by. A count alone
 * sent the agent to `:nth`, which picks by position -- the one choice that
 * silently changes meaning when a window opens. */
static void ui_end_ambiguous(rc_net *net, const char *id, const char *detail,
                             const HWND *cand, int candN)
{
    static const char *const CAND_KEYS[RC_SAMPLE_MAX] =
        { "cand1", "cand2", "cand3" };
    char vals[RC_SAMPLE_MAX][RC_CAND_BUDGET + 16];
    const char *k[1 + RC_SAMPLE_MAX];
    const char *v[1 + RC_SAMPLE_MAX];
    int n = 0, i;

    k[n] = "detail"; v[n++] = detail;
    for (i = 0; i < candN && i < RC_SAMPLE_MAX; i++)
    {
        if (!rc_uirec_candidate(vals[i], sizeof(vals[i]), cand[i]))
            continue;
        k[n] = CAND_KEYS[i]; v[n++] = vals[i];
    }
    ui_end(net, id, "ambiguous", k, v, n);
}

void rc_uirpc_refuse(rc_net *net, rc_frame *f, const char *why)
{
    const char *id = rc_frame_get(f, "id");
    if (!id)
    {
        rc_log("%s: no id -- dropped, and the bridge will wait out its "
               "timeout", f->type);
        return;
    }
    ui_end(net, id, why, NULL, NULL, 0);
}

/* ---- shared helpers -------------------------------------------------- */

static long header_num(rc_frame *f, const char *key, long dflt,
                       long lo, long hi)
{
    const char *s = rc_frame_get(f, key);
    long v;
    if (!s || !s[0])
        return dflt;
    v = atol(s);
    if (v < lo) v = lo;
    if (v > hi) v = hi;
    return v;
}

static bool header_flag(rc_frame *f, const char *key, bool dflt)
{
    const char *s = rc_frame_get(f, key);
    if (!s || !s[0])
        return dflt;
    return s[0] != '0';
}

/* Fill the walk options every op shares. */
static void base_options(WalkOptions &o, UiContext *ctx, rc_frame *f)
{
    const char *scope = rc_frame_get(f, "scope");

    o.visibleOnly = header_flag(f, "hidden", false) ? false : true;
    o.wantText = header_flag(f, "text", true);
    o.maxNodes = (int)header_num(f, "max_nodes", 400, 1, 4000);
    o.callTimeoutMs = (DWORD)header_num(f, "call_ms", 200, 20, 2000);
    o.deadlineMs = (DWORD)header_num(f, "timeout_ms", 8000, 200, 60000);
    /* Never our own windows. The walker sends WM_GETTEXT and a same-thread
     * SendMessage is a direct call; this worker is a different thread from
     * the window's, but the HTML control's reentrancy is not the only
     * reason -- an agent has no business reading the transcript it is
     * writing into. */
    o.excludePid = ctx->selfPid;

    if (scope && !strcmp(scope, "foreground"))
    {
        o.root = GetForegroundWindow();
        if (o.root && GetWindowThreadProcessId(o.root, NULL))
        {
            DWORD pid = 0;
            GetWindowThreadProcessId(o.root, &pid);
            if (pid == ctx->selfPid)
                o.root = NULL;      /* it is us; fall back to the desktop */
        }
    }
    else if (scope && !strcmp(scope, "pid"))
    {
        o.pid = (DWORD)header_num(f, "pid", 0, 0, 0x7FFFFFFF);
    }
}

/* ---- TREE ------------------------------------------------------------ */

/* Records are batched into frames. The buffer is HEAP, not a local: the
 * worker thread gets the image's stack commit and a 62KB frame on the stack
 * is the fault that crashed AUTOCHK on the P200. syncrpc.cpp's manifest does
 * the same for the same reason. */
struct TreeSink : public NodeSink
{
    TreeSink(rc_net *n, const char *sid, UiContext *c, bool wantMsaa)
        : net(n), ctx(c), buf((char *)HeapAlloc(GetProcessHeap(), 0, CAP)),
          len(0), batch(0), total(0), enriched(0), dropped(0),
          msaa(wantMsaa), cap(0)
    {
        rc_strncpy(id, sizeof(id), sid, (unsigned)strlen(sid));
    }
    ~TreeSink() { if (buf) HeapFree(GetProcessHeap(), 0, buf); }

    enum { CAP = RC_MAX_FRAME - 2048 };

    bool ready() const { return buf != 0; }
    void limit(int n) { cap = n; }

    void flush()
    {
        const char *k[2];
        const char *v[2];
        char nbuf[16];
        if (!len)
            return;
        rc_strf(nbuf, sizeof(nbuf), "%d", batch);
        k[0] = "id"; v[0] = id;
        k[1] = "n";  v[1] = nbuf;
        rc_net_send(net, "UIENT", k, v, 2, buf, len);
        len = 0;
        batch = 0;
    }

    virtual bool onNode(const Node &n)
    {
        const Enrichment *e = 0;
        Enrichment got;

        /* Only when asked. Each field is a marshalled call into another
         * process and a tree is hundreds of nodes; this is the difference
         * between a tree that reads in a second and one that does not read
         * at all. */
        if (msaa && ctx->msaa.available())
        {
            int want = Selector::WANT_NAME | Selector::WANT_STATE
                     | Selector::WANT_ACTION;
            if (!n.role[0])
                want |= Selector::WANT_ROLE;
            if (ctx->msaa.enrich(n.hwnd, want, got))
            {
                e = &got;
                enriched++;
            }
        }

        unsigned w = rc_uirec_window(buf + len, CAP - len, n, e);
        if (!w)
        {
            /* Full: flush and try once more. A record that still will not
             * fit an empty buffer is absurd rather than unlucky, and is
             * counted rather than silently lost. */
            flush();
            w = rc_uirec_window(buf + len, CAP - len, n, e);
            if (!w)
            {
                dropped++;
                return true;
            }
        }
        len += w;
        batch++;
        total++;
        return cap <= 0 || total < cap;
    }

    rc_net    *net;
    UiContext *ctx;
    char      *buf;
    unsigned   len;
    int        batch;
    int        total;
    int        enriched;
    int        dropped;
    bool       msaa;
    int        cap;
    char       id[64];
};

static const char *walk_end_name(WalkEnd e)
{
    switch (e)
    {
    case WALK_COMPLETE: return "complete";
    case WALK_DEADLINE: return "deadline";
    case WALK_MAXNODES: return "maxnodes";
    case WALK_STOPPED:  return "stopped";
    }
    return "unknown";
}

/* Defined below, beside the other target-resolving code: CLICK and KEYS
 * use it too, and the in-process tier still needs the out-of-process walk
 * to know which window to go to. */
static HWND resolve_target(rc_net *net, UiContext *ctx, rc_frame *f,
                           const char *id, const WalkOptions &o);

/* The in-process tier, behind the same op.
 *
 * Opt-in per request and never the default: this puts our code inside a
 * program that did not ask for it. It also REFUSES rather than falling back
 * silently, and the distinction matters -- the two tiers give different
 * answers and only one of them can read a list box, so a caller has to be
 * able to tell which one it got. `status=` says so either way.
 */
static void do_tree_inproc(rc_net *net, UiContext *ctx, rc_frame *f,
                           const char *id, const WalkOptions &o)
{
    const char *target = rc_frame_get(f, "target");
    const char *k[6];
    const char *v[6];
    char nodes[16], items[16], ms[16];
    int nkv = 0;

    if (!ctx->injector.open())
    {
        k[0] = "detail"; v[0] = ctx->injector.lastError();
        ui_end(net, id, "inproc_unavailable", k, v, 1);
        return;
    }

    /* A window of the target, found the ordinary way. The in-process tier
     * still needs the out-of-process one to know where to go. */
    HWND win = NULL;
    if (target && target[0])
    {
        win = resolve_target(net, ctx, f, id, o);
        if (!win)
            return;             /* already answered */
    }
    else
    {
        win = GetForegroundWindow();
        if (!win)
        {
            ui_end(net, id, "no_window", NULL, NULL, 0);
            return;
        }
    }

    InjectResult r = ctx->injector.collect(
        win, NULL, header_flag(f, "items", true),
        (DWORD)header_num(f, "timeout_ms", 3000, 200, 20000));

    if (r != INJECT_OK && r != INJECT_TRUNCATED)
    {
        static const char *const NAMES[] = {
            "ok", "denied", "no_dll", "no_window", "hook_refused",
            "no_reply", "truncated", "failed"
        };
        k[0] = "detail"; v[0] = ctx->injector.lastError();
        ui_end(net, id, NAMES[(int)r], k, v, 1);
        return;
    }

    const RcInShm *shm = ctx->injector.shm();
    TreeSink sink(net, id, ctx, false);
    if (!sink.ready())
    {
        ui_end(net, id, "no_memory", NULL, NULL, 0);
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
        ui_end(net, id, "no_memory", NULL, NULL, 0);
        return;
    }
    rc_path_reset(*ps.get());

    for (long i = 0; i < shm->nodeN; i++)
    {
        const RcInNode &n = shm->nodes[i];
        char path[RC_PATH_MAX];
        char parent[RC_PATH_MAX];

        rc_path_next(*ps.get(), n.depth, path, parent);

        unsigned w = rc_uirec_inproc(sink.buf + sink.len, TreeSink::CAP - sink.len,
                                     n, path, parent);
        if (!w)
        {
            sink.flush();
            w = rc_uirec_inproc(sink.buf + sink.len,
                                TreeSink::CAP - sink.len, n, path, parent);
        }
        if (w) { sink.len += w; sink.batch++; sink.total++; }
        else   { sink.dropped++; }

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
            w = rc_uirec_item(sink.buf + sink.len, TreeSink::CAP - sink.len,
                              shm->items[idx].text, ipath, path, it);
            if (!w)
            {
                sink.flush();
                w = rc_uirec_item(sink.buf + sink.len,
                                  TreeSink::CAP - sink.len,
                                  shm->items[idx].text, ipath, path, it);
            }
            if (w) { sink.len += w; sink.batch++; sink.total++; }
            else   { sink.dropped++; }
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
    ui_end(net, id, "ok", k, v, nkv);
}

static void do_tree(rc_net *net, UiContext *ctx, rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *waitFor = rc_frame_get(f, "wait_for");
    const char *k[6];
    const char *v[6];
    char nodes[16], enr[16], drop[16], deep[16], self[16];
    WalkOptions o;
    int nkv = 0;

    if (!id)
    {
        rc_log("UITREE: no id -- dropped");
        return;
    }
    base_options(o, ctx, f);

    /* Opt-in, off by default, and it does not fall back. A caller that
     * asked for the in-process answer and quietly got the out-of-process
     * one would conclude a list box is empty. */
    if (header_flag(f, "inproc", false))
    {
        do_tree_inproc(net, ctx, f, id, o);
        return;
    }

    TreeSink sink(net, id, ctx, header_flag(f, "msaa", false));
    if (!sink.ready())
    {
        ui_end(net, id, "no_memory", NULL, NULL, 0);
        return;
    }
    sink.limit(o.maxNodes);

    /* The wait is a PARAMETER, not a separate op: the loop an agent runs is
     * "do a thing, then look", and folding the wait into the look turns two
     * round trips into one. On a 200MHz machine behind a socket the round
     * trip is the expensive part. */
    if (waitFor && waitFor[0])
    {
        Selector sel;
        char err[128];
        if (!sel.parse(waitFor, err, sizeof(err)))
        {
            k[0] = "detail"; v[0] = err;
            ui_end(net, id, "bad_selector", k, v, 1);
            return;
        }
        WaitReport rep;
        UiWaiter waiter(ctx->watch, ctx->walker, &ctx->msaa);
        DWORD budget = (DWORD)header_num(f, "wait_ms", 5000, 100, 60000);
        ctx->deadline.arm(budget + o.deadlineMs);
        waiter.forControl(sel, o, budget, rep);
        ctx->deadline.disarm();
        if (rep.end != UIWAIT_ALREADY && rep.end != UIWAIT_SETTLED)
        {
            char detail[96];
            /* THE WAITER'S OWN ANSWER, not "not_found" for all four.
             *
             * Every non-settled end used to go out as `not_found`, whose
             * sentence begins "Nothing on screen matches that selector" -- so
             * a wait that matched THREE controls and could not choose between
             * them told the caller nothing matched. Measured on real hardware
             * 2026-10-02: `ui wait_for='[text="Control Panel"]'` after opening
             * Control Panel came back `end=3 after 5005ms, 5 scans, matched=3`
             * rendered as "nothing matches ... the window may not be open",
             * and the agent believed it and went looking for the window with a
             * screenshot. The window was open; the title just appears three
             * times, on the frame, the toolbar and the address bar.
             *
             * The statuses already exist and the bridge already has the right
             * sentence for each; this stopped throwing the distinction away. */
            const char *why = "not_found";
            if (rep.end == UIWAIT_AMBIGUOUS)      why = "ambiguous";
            else if (rep.end == UIWAIT_TRUNCATED) why = "truncated";
            rc_strf(detail, sizeof(detail),
                    "%d controls match after %lums and %d scans",
                    rep.matchCount, (unsigned long)rep.ms, rep.rechecks);
            if (rep.end == UIWAIT_TIMEOUT)
                rc_strf(detail, sizeof(detail),
                        "waited %lums over %d scans; %s",
                        (unsigned long)rep.ms, rep.rechecks,
                        rep.everMatched ? "it appeared and then went"
                                        : "it never appeared");
            if (rep.end == UIWAIT_AMBIGUOUS)
            {
                ui_end_ambiguous(net, id, detail, rep.samples, rep.sampleN);
                return;
            }
            k[0] = "detail"; v[0] = detail;
            ui_end(net, id, why, k, v, 1);
            return;
        }
    }

    {
        DWORD settle = (DWORD)header_num(f, "settle_ms", 0, 0, 5000);
        if (settle)
        {
            WaitReport rep;
            UiWaiter waiter(ctx->watch, ctx->walker, &ctx->msaa);
            waiter.forIdle(settle, settle * 4, rep);
        }
    }

    ctx->deadline.arm(o.deadlineMs);
    WalkEnd end = ctx->walker.walk(o, sink);
    ctx->deadline.disarm();
    sink.flush();

    rc_strf(nodes, sizeof(nodes), "%d", sink.total);
    rc_strf(enr, sizeof(enr), "%d", sink.enriched);
    rc_strf(drop, sizeof(drop), "%d", sink.dropped);
    rc_strf(deep, sizeof(deep), "%d", ctx->walker.tooDeep());
    rc_strf(self, sizeof(self), "%d", ctx->walker.selfSkipped());
    k[nkv] = "nodes";    v[nkv++] = nodes;
    k[nkv] = "walk";     v[nkv++] = walk_end_name(end);
    k[nkv] = "enriched"; v[nkv++] = enr;
    k[nkv] = "dropped";  v[nkv++] = drop;
    k[nkv] = "too_deep"; v[nkv++] = deep;
    /* Our own windows are never walked, and the bridge has to be able to
     * SAY that rather than hand back a list they are merely absent from. */
    k[nkv] = "self";     v[nkv++] = self;
    ui_end(net, id, "ok", k, v, nkv);
}

/* ---- resolving a target --------------------------------------------- */

/* Shared by CLICK and KEYS. Answers the request itself on any failure and
 * returns NULL, so a caller that gets NULL is done. */
static HWND resolve_target(rc_net *net, UiContext *ctx, rc_frame *f,
                           const char *id, const WalkOptions &o)
{
    const char *target = rc_frame_get(f, "target");
    const char *k[1];
    const char *v[1];
    char err[128];
    Selector sel;

    if (!target || !target[0])
    {
        ui_end(net, id, "no_target", NULL, NULL, 0);
        return NULL;
    }
    if (!sel.parse(target, err, sizeof(err)))
    {
        k[0] = "detail"; v[0] = err;
        ui_end(net, id, "bad_selector", k, v, 1);
        return NULL;
    }

    Resolver r(sel, ctx->msaa.available() ? &ctx->msaa : 0);
    ctx->deadline.arm(o.deadlineMs);
    ResolveOutcome out = r.resolve(o, ctx->walker);
    ctx->deadline.disarm();

    if (out == RESOLVE_OK && r.match())
        return r.match()->hwnd;

    /* Ambiguity is reported WITH THE COUNT, because the count is what tells
     * the caller whether the :nth(n) it is about to write exists. Silently
     * clicking the first of four buttons called OK is the failure this
     * whole layer refuses to have. */
    if (out == RESOLVE_AMBIGUOUS)
    {
        char detail[64];
        HWND cand[RC_SAMPLE_MAX];
        int candN = 0;
        rc_strf(detail, sizeof(detail), "%d controls match", r.matchCount());
        for (int i = 0; i < r.sampleCount() && i < RC_SAMPLE_MAX; i++)
            cand[candN++] = r.sample(i)->hwnd;
        ui_end_ambiguous(net, id, detail, cand, candN);
        return NULL;
    }
    if (out == RESOLVE_TRUNCATED)
    {
        ui_end(net, id, "truncated", NULL, NULL, 0);
        return NULL;
    }
    if (out == RESOLVE_NO_MEMORY)
    {
        ui_end(net, id, "no_memory", NULL, NULL, 0);
        return NULL;
    }
    /* WHICH PART was not there, when the selector has more than one.
     * `A > B` failing says nothing about whether A was present, and a caller
     * told only "nothing matches" reads it as the window being gone: in a live
     * session `HelloGuiClass[text="Hello Application"] > Button[text="Close"]`
     * was answered that way while the window was on screen and only the button
     * did not exist -- the program had no Close button at all (2026-10-02).
     *
     * The INDEX, not the text. The caller has the selector it sent and can
     * split it; sending the component back would be this side re-rendering a
     * string it was given, and getting the spacing subtly wrong. */
    {
        int step = r.unmatchedStep();
        char stepText[16];
        if (step >= 0 && sel.stepCount() > 1)
        {
            rc_strf(stepText, sizeof(stepText), "%d", step);
            k[0] = "step"; v[0] = stepText;
            ui_end(net, id, "not_found", k, v, 1);
            return NULL;
        }
    }
    ui_end(net, id, "not_found", NULL, NULL, 0);
    return NULL;
}

/* CLICK, FOCUS and KEYS lived here until 2026-10-02. A client that runs
 * step lists is offered `input` instead (ui/uisteps.cpp), which does all
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
    ShotFrames(rc_net *n, const char *sid)
        : m_net(n), m_buf((char *)HeapAlloc(GetProcessHeap(), 0, CAP)),
          m_len(0), m_sent(0), m_ok(true)
    {
        rc_strncpy(m_id, sizeof(m_id), sid, (unsigned)strlen(sid));
    }
    ~ShotFrames() { if (m_buf) HeapFree(GetProcessHeap(), 0, m_buf); }

    enum { CAP = RC_MAX_FRAME - 2048 };

    bool ready() const { return m_buf != 0; }
    bool ok() const { return m_ok; }
    unsigned sent() const { return m_sent; }

    virtual bool write(const void *data, unsigned len)
    {
        const char *p = (const char *)data;
        while (len)
        {
            unsigned take = CAP - m_len;
            if (take > len)
                take = len;
            memcpy(m_buf + m_len, p, take);
            m_len += take;
            p += take;
            len -= take;
            if (m_len == CAP && !flush())
                return false;
        }
        return true;
    }

    bool flush()
    {
        const char *k[1];
        const char *v[1];
        if (!m_len)
            return true;
        k[0] = "id"; v[0] = m_id;
        if (rc_net_send(m_net, "UIDAT", k, v, 1, m_buf, m_len) != 0)
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
    rc_net  *m_net;
    char    *m_buf;
    unsigned m_len;
    unsigned m_sent;
    bool     m_ok;
    char     m_id[64];
};

static void do_shot(rc_net *net, UiContext *ctx, rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *k[5];
    const char *v[5];
    char w[16], h[16], bpp[16], bytes[16];
    ShotOptions o;
    int nkv = 0;

    (void)ctx;
    if (!id)
    {
        rc_log("UISHOT: no id -- dropped");
        return;
    }

    o.maxBpp = (int)header_num(f, "bpp", 8, 4, 32);
    o.rect.left   = (LONG)header_num(f, "x", 0, 0, 0x4000);
    o.rect.top    = (LONG)header_num(f, "y", 0, 0, 0x4000);
    o.rect.right  = (LONG)header_num(f, "w", 0, 0, 0x4000);
    o.rect.bottom = (LONG)header_num(f, "h", 0, 0, 0x4000);
    /* A width and a height, not a right and a bottom: a caller says "100
     * wide", and making it say "left plus 100" is an arithmetic trap. */
    if (o.rect.right)  o.rect.right  += o.rect.left;
    if (o.rect.bottom) o.rect.bottom += o.rect.top;

    ShotFrames sink(net, id);
    if (!sink.ready())
    {
        ui_end(net, id, "no_memory", NULL, NULL, 0);
        return;
    }

    Screenshot shot;
    bool got = shot.capture(o, sink);
    if (got)
        got = sink.flush();

    if (!got)
    {
        k[0] = "detail"; v[0] = sink.ok() ? shot.error() : "connection lost";
        ui_end(net, id, "failed", k, v, 1);
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
    ui_end(net, id, "ok", k, v, nkv);
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

    if      (strcmp(f->type, "UITREE")  == 0) do_tree(net, ctx, f);
    else if (strcmp(f->type, "UISHOT")  == 0) do_shot(net, ctx, f);
    else if (strcmp(f->type, "UIRUN")   == 0) rc_uirpc_run(net, ctx, f);
    else rc_log("desktop: unhandled frame %s", f->type);
}
