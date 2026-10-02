/* uiengine.cpp -- the desktop handlers, with no wire under them.
 *
 * Two programs run these: the client answers UIENT/UIDAT/UIEND through
 * rc_net, and gremote.exe answers DATA/OK/ERR through a Responder
 * (docs/REMOTE.md step 6). Everything they disagree about is behind
 * `UiLink` (ui/uilink.h), so neither op namespace is spelled in here --
 * REMOTE.md is explicit that the two must not drift into each other, and a
 * shared handler that knew either spelling is how they would.
 *
 * Moved out of ui/uisteps.cpp and ui/uirpc.cpp on 2026-10-02, unchanged
 * except for the seam. uisteps.cpp was ABSORBED rather than renamed beside
 * this file: a new translation unit costs ~2,144 bytes of -xs machinery
 * before any of its own code (win/Makefile), and the client had 2,679 bytes
 * of its committed-at-load budget left.
 *
 * UIRUN, the `input` tool's step list:
 *
 * BODY IN: one step per line, `verb` then TAB-separated `key=value`, every
 * value percent-encoded by the bridge (bridge/agent/steps.py) so a selector
 * or a typed string may hold any byte, TABs and newlines included. Steps
 * ride the BODY and not headers because a header value is capped at 255
 * encoded bytes, and a selector clipped to fit is a different selector.
 *
 * BODY OUT: the same shape, one line per step that ran:
 *     i=<n> status=<word> ms=<n> hit=<window> detail=<text> part=<n>
 * and a last line `released=1` when the list ended with something still
 * held, which the runner released. It goes through UiLink::report() rather
 * than on the terminator: see uilink.h for why a terminal frame may not
 * carry a body.
 *
 * The stop flag is the caller's -- UiLink::stopFlag(). The client raises it
 * from UICANCEL on the reader thread; gremote from STOP or CANCEL. The
 * runner looks at it between steps and inside every wait.
 */
#include "ui/uiengine.h"
#include "ui/uirec.h"
#include "automation/resolve.h"
#include "net/caps.h"
#include "ui/uirec.h"
#include "automation/steps.h"
#include "core/log.h"
#include "core/rc_cpp.h"
#include "core/str.h"

#include <stdlib.h>
#include <string.h>

long rc_ui_header_num(rc_frame *f, const char *key, long dflt,
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

bool rc_ui_header_flag(rc_frame *f, const char *key, bool dflt)
{
    const char *s = rc_frame_get(f, key);
    if (!s || !s[0])
        return dflt;
    return s[0] != '0';
}

/* Fill the walk options every op shares. */
void rc_ui_base_options(WalkOptions &o, UiLink &link, DWORD selfPid,
                        rc_frame *f)
{
    const UiLimits &L = link.limits();
    const char *scope = rc_frame_get(f, "scope");

    o.visibleOnly = rc_ui_header_flag(f, "hidden", false) ? false : true;
    o.wantText = rc_ui_header_flag(f, "text", true);
    o.maxNodes = (int)rc_ui_header_num(f, "max_nodes", L.maxNodes, 1,
                                L.maxNodesTop);
    o.callTimeoutMs = (DWORD)rc_ui_header_num(f, "call_ms", (long)L.callMs, 20,
                                        (long)L.callMsTop);
    o.deadlineMs = (DWORD)rc_ui_header_num(f, "timeout_ms", (long)L.treeMs, 200,
                                     (long)L.treeMsTop);
    /* Never our own windows. The walker sends WM_GETTEXT and a same-thread
     * SendMessage is a direct call; this worker is a different thread from
     * the window's, but the HTML control's reentrancy is not the only
     * reason -- an agent has no business reading the transcript it is
     * writing into. */
    o.excludePid = selfPid;

    if (scope && !strcmp(scope, "foreground"))
    {
        o.root = GetForegroundWindow();
        if (o.root && GetWindowThreadProcessId(o.root, NULL))
        {
            DWORD pid = 0;
            GetWindowThreadProcessId(o.root, &pid);
            if (pid == selfPid)
                o.root = NULL;      /* it is us; fall back to the desktop */
        }
    }
    else if (scope && !strcmp(scope, "pid"))
    {
        o.pid = (DWORD)rc_ui_header_num(f, "pid", 0, 0, 0x7FFFFFFF);
    }
}

/* "More than one matched", with WHO matched: up to RC_SAMPLE_MAX
 * candidates, each with the ancestors the agent can narrow by. A count alone
 * sent the agent to `:nth`, which picks by position -- the one choice that
 * silently changes meaning when a window opens. */
void rc_ui_end_ambiguous(UiLink &link, const char *detail,
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
    link.end("ambiguous", k, v, n);
}

/* The context both programs' desktop workers hold. Defined here rather than
 * in the client's service, because gremote has no UiService to put it in and
 * linking that file would drag the client's whole RPC side across. */
UiContext::UiContext()
{
    selfPid = GetCurrentProcessId();
    ready = false;
}

/* ---- TREE ------------------------------------------------------------ */

/* Records are batched into frames. The buffer is HEAP, not a local: the
 * worker thread gets the image's stack commit and a 62KB frame on the stack
 * is the fault that crashed AUTOCHK on the P200. syncrpc.cpp's manifest does
 * the same for the same reason. */
TreeSink::TreeSink(UiLink &l, UiContext *c, bool wantMsaa)
    : link(l), ctx(c), frameCap(l.bodyCap()),
      buf((char *)HeapAlloc(GetProcessHeap(), 0, l.bodyCap())),
      len(0), batch(0), total(0), enriched(0), dropped(0),
      msaa(wantMsaa), cap(0), stopped(false)
{
}

TreeSink::~TreeSink() { if (buf) HeapFree(GetProcessHeap(), 0, buf); }

void TreeSink::flush()
{
    if (!len)
        return;
    link.records(buf, len, batch);
    len = 0;
    batch = 0;
}

bool TreeSink::onNode(const Node &n)
{
    const Enrichment *e = 0;
    Enrichment got;

    /* Stopped between nodes. The walker already had WALK_STOPPED for a sink
     * that says no, and a node is the right grain: one more WM_GETTEXT at a
     * bounded timeout, not a whole walk. */
    {
        volatile LONG *stop = link.stopFlag();
        if (stop && *stop)
        {
            stopped = true;
            return false;
        }
    }

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

    unsigned w = rc_uirec_window(room(), left(), n, e);
    if (!w)
    {
        /* Full: flush and try once more. A record that still will not
         * fit an empty buffer is absurd rather than unlucky, and is
         * counted rather than silently lost. */
        flush();
        w = rc_uirec_window(room(), left(), n, e);
        if (!w)
        {
            dropped++;
            return true;
        }
    }
    took(w);
    return cap <= 0 || total < cap;
}


const char *rc_ui_walk_end_name(WalkEnd e)
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
/* ---- resolving a target --------------------------------------------- */

/* Shared by CLICK and KEYS. Answers the request itself on any failure and
 * returns NULL, so a caller that gets NULL is done. */
HWND rc_ui_resolve_target(UiLink &link, UiContext *ctx, rc_frame *f,
                          const WalkOptions &o)
{
    const char *target = rc_frame_get(f, "target");
    const char *k[1];
    const char *v[1];
    char err[128];
    Selector sel;

    if (!target || !target[0])
    {
        link.end("no_target", NULL, NULL, 0);
        return NULL;
    }
    if (!sel.parse(target, err, sizeof(err)))
    {
        k[0] = "detail"; v[0] = err;
        link.end("bad_selector", k, v, 1);
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
        rc_ui_end_ambiguous(link, detail, cand, candN);
        return NULL;
    }
    if (out == RESOLVE_TRUNCATED)
    {
        link.end("truncated", NULL, NULL, 0);
        return NULL;
    }
    if (out == RESOLVE_NO_MEMORY)
    {
        link.end("no_memory", NULL, NULL, 0);
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
            link.end("not_found", k, v, 1);
            return NULL;
        }
    }
    link.end("not_found", NULL, NULL, 0);
    return NULL;
}

struct AccRecords : public AccSink
{
    AccRecords(TreeSink &t, RcPathBuilder &p) : tree(t), paths(p), total(0)
    {
        rc_path_reset(paths);
    }

    virtual bool onAcc(const AccNode &n)
    {
        char path[RC_PATH_MAX], parent[RC_PATH_MAX];
        if (n.depth == 0)
            return true;                    /* the control itself: listed */
        rc_path_next(paths, n.depth - 1, path, parent);
        /* Through room()/took() rather than the fields, because this is
         * not the only writer: the client's in-process tier fills the same
         * sink from its own file. Two callers poking len and batch directly
         * is how the batch count and the buffer get out of step. */
        unsigned w = rc_uirec_acc(tree.room(), tree.left(), n, path, parent);
        if (!w)
        {
            tree.flush();
            w = rc_uirec_acc(tree.room(), tree.left(), n, path, parent);
            if (!w)
                return true;
        }
        tree.took(w);
        total++;
        return true;
    }

    TreeSink      &tree;
    RcPathBuilder &paths;               /* on the heap: most of a page */
    int            total;
};

static void do_items(UiLink &link, UiContext *ctx, rc_frame *f,
                     const WalkOptions &o)
{
    const char *k[2];
    const char *v[2];
    char nodes[16];
    if (!ctx->msaa.available())
    {
        link.end("no_msaa", NULL, NULL, 0);
        return;
    }
    HWND h = rc_ui_resolve_target(link, ctx, f, o);
    if (!h)
        return;                             /* resolve_target answered */
    UniqueObj<TreeSink> tree(new TreeSink(link, ctx, false));
    if (!tree.valid() || !tree->ready())
    {
        link.end("no_memory", NULL, NULL, 0);
        return;
    }
    HeapPtr<RcPathBuilder> paths((RcPathBuilder *)HeapAlloc(
        GetProcessHeap(), 0, sizeof(RcPathBuilder)));
    if (!paths.valid())
    {
        link.end("no_memory", NULL, NULL, 0);
        return;
    }
    UniqueObj<AccRecords> recs(new AccRecords(*tree.get(), *paths.get()));
    if (!recs.valid())
    {
        link.end("no_memory", NULL, NULL, 0);
        return;
    }
    AccWalkOptions ao;
    ao.want = Selector::WANT_NAME | Selector::WANT_ROLE
            | Selector::WANT_STATE;
    ao.maxNodes = link.limits().maxNodes;
    ao.maxDepth = 3;
    ao.deadlineMs = o.deadlineMs;
    ctx->deadline.arm(o.deadlineMs);
    AccEnd end = ctx->msaa.enumerateFrom(h, ao, *recs.get());
    ctx->deadline.disarm();
    tree->flush();
    if (end == ACC_NO_OBJECT)
    {
        link.end("no_msaa", NULL, NULL, 0);
        return;
    }
    rc_strf(nodes, sizeof(nodes), "%d", recs->total);
    k[0] = "nodes"; v[0] = nodes;
    k[1] = "walk";
    v[1] = end == ACC_COMPLETE ? "complete"
         : end == ACC_DEADLINE ? "deadline" : "maxnodes";
    link.end("ok", k, v, 2);
}

void rc_ui_tree(UiLink &link, UiContext *ctx, rc_frame *f)
{
    const char *waitFor = rc_frame_get(f, "wait_for");
    const char *k[6];
    const char *v[6];
    char nodes[16], enr[16], drop[16], deep[16], self[16];
    WalkOptions o;
    int nkv = 0;

    if (link.automationLevel() == RC_AUTOMATION_NONE)
    {
        link.end("automation_off", NULL, NULL, 0);
        return;
    }
    rc_ui_base_options(o, link, ctx->selfPid, f);

    /* One control's items, not the window tree. */
    if (rc_ui_header_flag(f, "acc", false))
    {
        do_items(link, ctx, f, o);
        return;
    }

    /* `inproc` is NOT handled here, and that is the point. The in-process
     * tier is the client's alone -- it needs HOOKDLL.DLL beside the exe and
     * a deny list with no force escape -- so the branch lives in each
     * caller's own dispatcher, which either serves it or refuses it.
     * Falling through to the out-of-process walk here would answer a caller
     * who asked for the in-process tier with the other tier's answer, and
     * an empty list box reads the same either way. */

    TreeSink sink(link, ctx, rc_ui_header_flag(f, "msaa", false));
    if (!sink.ready())
    {
        link.end("no_memory", NULL, NULL, 0);
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
            link.end("bad_selector", k, v, 1);
            return;
        }
        WaitReport rep;
        UiWaiter waiter(ctx->watch, ctx->walker, &ctx->msaa);
        DWORD budget = (DWORD)rc_ui_header_num(f, "wait_ms", 5000, 100, 60000);
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
                rc_ui_end_ambiguous(link, detail, rep.samples, rep.sampleN);
                return;
            }
            k[0] = "detail"; v[0] = detail;
            link.end(why, k, v, 1);
            return;
        }
    }

    {
        DWORD settle = (DWORD)rc_ui_header_num(f, "settle_ms", 0, 0, 5000);
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

    if (sink.stopped)
    {
        /* The caller asked for this; it is not a short answer to explain
         * away. The records already sent stand -- they were true when they
         * were written -- but the status says the walk did not finish. */
        link.end("cancelled", NULL, NULL, 0);
        return;
    }

    rc_strf(nodes, sizeof(nodes), "%d", sink.total);
    rc_strf(enr, sizeof(enr), "%d", sink.enriched);
    rc_strf(drop, sizeof(drop), "%d", sink.dropped);
    rc_strf(deep, sizeof(deep), "%d", ctx->walker.tooDeep());
    rc_strf(self, sizeof(self), "%d", ctx->walker.selfSkipped());
    k[nkv] = "nodes";    v[nkv++] = nodes;
    k[nkv] = "walk";     v[nkv++] = rc_ui_walk_end_name(end);
    k[nkv] = "enriched"; v[nkv++] = enr;
    k[nkv] = "dropped";  v[nkv++] = drop;
    k[nkv] = "too_deep"; v[nkv++] = deep;
    /* Our own windows are never walked, and the bridge has to be able to
     * SAY that rather than hand back a list they are merely absent from. */
    k[nkv] = "self";     v[nkv++] = self;
    link.end("ok", k, v, nkv);
}


/* ---- RUN: the step list --------------------------------------------- */

/* ---- decoding, in place --------------------------------------------- */

static int hexVal(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* Percent-decode [p, p+n) where it lies, NUL-terminate, return its new
 * length. Decoding only ever shrinks, so the frame's own body is the
 * buffer and a Step's strings point straight into it. */
static unsigned unescapeInPlace(char *p, unsigned n)
{
    unsigned w = 0;
    for (unsigned i = 0; i < n; i++)
    {
        if (p[i] == '%' && i + 2 < n)
        {
            int hi = hexVal(p[i + 1]), lo = hexVal(p[i + 2]);
            if (hi >= 0 && lo >= 0)
            {
                p[w++] = (char)((hi << 4) | lo);
                i += 2;
                continue;
            }
        }
        p[w++] = p[i];
    }
    p[w] = '\0';
    return w;
}

static bool parsePair(const char *v, int &a, int &b)
{
    const char *c = strchr(v, ',');
    if (!c)
        return false;
    a = atoi(v);
    b = atoi(c + 1);
    return true;
}

static bool verbKind(const char *verb, StepKind &k)
{
    static const struct { const char *name; StepKind kind; } VERBS[] = {
        { "focus", STEP_FOCUS },     { "click", STEP_CLICK },
        { "close", STEP_CLOSE },     { "kd", STEP_KEYDOWN },
        { "ku", STEP_KEYUP },        { "bd", STEP_BTNDOWN },
        { "bu", STEP_BTNUP },        { "mv", STEP_MOVE },
        { "mvr", STEP_MOVEBY },      { "tap", STEP_TAP },
        { "type", STEP_TYPE },       { "wait", STEP_WAIT },
        { "waitfor", STEP_WAITFOR }, { "waitgone", STEP_WAITGONE },
        { "waittext", STEP_WAITTEXT }, { "idle", STEP_IDLE }
    };
    for (unsigned i = 0; i < sizeof(VERBS) / sizeof(VERBS[0]); i++)
    {
        if (!strcmp(VERBS[i].name, verb))
        {
            k = VERBS[i].kind;
            return true;
        }
    }
    return false;
}

static void setField(Step &s, const char *key, const char *v, unsigned vlen)
{
    StepTarget &t = s.target;
    if      (!strcmp(key, "sel"))     t.sel = v;
    else if (!strcmp(key, "item"))    t.item = v;
    else if (!strcmp(key, "inth"))    t.itemNth = atoi(v);
    else if (!strcmp(key, "text"))    { t.text = v; s.text = v;
                                        s.textLen = vlen; }
    else if (!strcmp(key, "tnth"))    t.textNth = atoi(v);
    else if (!strcmp(key, "edge"))    t.edgeEnd = !strcmp(v, "end");
    else if (!strcmp(key, "at"))      t.hasAt = parsePair(v, t.atX, t.atY);
    else if (!strcmp(key, "screen"))  t.hasScreen = parsePair(v, t.screenX,
                                                              t.screenY);
    else if (!strcmp(key, "in"))      s.in = v;
    else if (!strcmp(key, "key"))     s.key = v;
    else if (!strcmp(key, "mods"))    s.mods = atoi(v) & 7;
    else if (!strcmp(key, "button"))
        s.button = !strcmp(v, "right") ? BTN_RIGHT
                 : !strcmp(v, "middle") ? BTN_MIDDLE : BTN_LEFT;
    else if (!strcmp(key, "count"))   s.count = atoi(v);
    else if (!strcmp(key, "d"))       parsePair(v, s.dx, s.dy);
    else if (!strcmp(key, "ms"))      s.ms = (DWORD)atol(v);
    else if (!strcmp(key, "timeout")) s.timeoutMs = (DWORD)atol(v);
}

/* Bounded by what the bridge may send: RC_RUN_MAX steps in the frame's own
 * 64KB. False with a reason for anything malformed -- the bridge validated
 * it, so a bad line here is a version skew worth refusing loudly rather
 * than running a different list from the one sent. */
int rc_ui_decode_steps(char *body, unsigned len, Step *out, char *why,
                       unsigned whyCap)
{
    int n = 0;
    unsigned at = 0;
    while (at < len)
    {
        unsigned end = at;
        while (end < len && body[end] != '\n')
            end++;
        if (end > at)
        {
            if (n >= RC_RUN_MAX)
            {
                rc_strf(why, whyCap, "more than %d steps", RC_RUN_MAX);
                return -1;
            }
            Step &s = out[n];
            stepInit(s);
            /* Split the line on TAB into NUL-terminated fields, in place. */
            unsigned f = at;
            while (f < end && body[f] != '\t')
                f++;
            char saved = body[f];
            body[f] = '\0';
            if (!verbKind(body + at, s.kind))
            {
                rc_strf(why, whyCap, "step %d: unknown verb", n);
                return -1;
            }
            body[f] = saved;
            while (f < end)
            {
                unsigned k0 = ++f;
                while (f < end && body[f] != '\t')
                    f++;
                char *eq = (char *)memchr(body + k0, '=', f - k0);
                if (!eq)
                    continue;
                *eq = '\0';
                char *v = eq + 1;
                unsigned vlen = unescapeInPlace(v, (unsigned)(body + f - v));
                setField(s, body + k0, v, vlen);
            }
            n++;
        }
        at = end + 1;
    }
    return n;
}

/* ---- encoding ------------------------------------------------------- */

/* The reply body. Sized for the worst list the bridge may send: 64 lines
 * of at most a few hundred bytes each. On the heap, never the stack. */
#define RC_RUN_REPLY 32768

class LineSink : public StepSink
{
public:
    LineSink() : m_buf((char *)HeapAlloc(GetProcessHeap(), 0, RC_RUN_REPLY)),
                 m_len(0) {}

    bool ok() const { return m_buf.valid(); }
    const char *data() const { return m_buf.get(); }
    unsigned size() const { return m_len; }

    void put(const char *s)
    {
        while (*s && m_len + 1 < RC_RUN_REPLY)
            m_buf.get()[m_len++] = *s++;
    }

    void putEscaped(const char *s)
    {
        static const char HEX[] = "0123456789ABCDEF";
        for (; *s && m_len + 4 < RC_RUN_REPLY; s++)
        {
            unsigned char c = (unsigned char)*s;
            if (c < 0x20 || c == '%' || c == 0x7F)
            {
                m_buf.get()[m_len++] = '%';
                m_buf.get()[m_len++] = HEX[c >> 4];
                m_buf.get()[m_len++] = HEX[c & 15];
            }
            else
                m_buf.get()[m_len++] = (char)c;
        }
    }

    virtual void onStep(int index, const Step &, const StepReport &r)
    {
        char num[48];
        if (!ok())
            return;
        rc_strf(num, sizeof(num), "i=%d\tstatus=", index);
        put(num);
        put(r.status);
        rc_strf(num, sizeof(num), "\tms=%lu", (unsigned long)r.ms);
        put(num);
        if (r.hit[0])
        {
            put("\thit=");
            putEscaped(r.hit);
        }
        if (r.detail[0])
        {
            put("\tdetail=");
            putEscaped(r.detail);
        }
        if (r.selStep >= 0)
        {
            rc_strf(num, sizeof(num), "\tpart=%d", r.selStep);
            put(num);
        }
        put("\n");
    }

private:
    HeapPtr<char> m_buf;
    unsigned      m_len;
};

static long headerNum(rc_frame *f, const char *key, long dflt, long lo,
                      long hi)
{
    const char *s = rc_frame_get(f, key);
    long v;
    if (!s || !s[0])
        return dflt;
    v = atol(s);
    return v < lo ? lo : v > hi ? hi : v;
}

void rc_ui_run(UiLink &link, UiContext *ctx, rc_frame *f)
{
    char why[96];
    if (link.automationLevel() < RC_AUTOMATION_STEPS)
    {
        /* SCREEN is read-only: a box that may be photographed and
         * enumerated but not typed into. The client never declares that
         * middle value -- it sends STEPS or NONE -- so this refusal is
         * reachable for the first time through gremote's [ui] section. */
        link.end("automation_off", NULL, NULL, 0);
        return;
    }
    HeapPtr<Step> steps((Step *)HeapAlloc(GetProcessHeap(), 0,
                                          RC_RUN_MAX * sizeof(Step)));
    LineSink sink;
    if (!steps.valid() || !sink.ok())
    {
        link.end("no_memory", NULL, NULL, 0);
        return;
    }
    int count = f->body ? rc_ui_decode_steps(f->body, f->body_len,
                                         steps.get(), why,
                                         sizeof(why)) : 0;
    if (count < 0)
    {
        const char *k[1] = { "detail" };
        const char *v[1] = { why };
        link.end("bad_steps", k, v, 1);
        return;
    }

    /* Defaults and ceilings from the caller, floors from here: a floor is
     * a sanity bound on a number off the wire, not policy a deployment gets
     * to choose, and a configurable one is a way to ask for a 1ms step. */
    const UiLimits &L = link.limits();

    /* The walk every step shares: the whole desktop, never our own windows,
     * and per-message timeouts small enough that one hung program costs a
     * walk 200ms rather than the step. */
    WalkOptions o;
    o.visibleOnly = true;
    o.wantText = true;
    o.maxNodes = L.maxNodes;
    o.callTimeoutMs = L.callMs;
    o.deadlineMs = L.treeMs;
    o.excludePid = ctx->selfPid;

    StepLimits lim;
    lim.scriptMs = (DWORD)headerNum(f, "timeout_ms", (long)L.scriptMs, 100,
                                    (long)L.scriptMsTop);
    lim.stepMs = (DWORD)headerNum(f, "step_ms", (long)L.stepMs, 100,
                                  (long)L.stepMsTop);
    lim.settleMs = (DWORD)headerNum(f, "settle", 0, 0, 5000);

    StepRunner runner(ctx->walker, ctx->msaa.available() ? &ctx->msaa : 0,
                      ctx->watch, ctx->input, &ctx->deadline);
    runner.setCancel(link.stopFlag());

    StepReport failed;
    int at = runner.run(steps.get(), count, o, lim, sink, failed);
    if (runner.releasedAtEnd())
        sink.put("released=1\n");

    char done[16], step[16];
    rc_strf(done, sizeof(done), "%d", at < 0 ? count : at);
    if (at < 0)
    {
        const char *k[1] = { "done" };
        const char *v[1] = { done };
        link.report(sink.data(), sink.size());
        link.end("ok", k, v, 1);
        return;
    }

    /* The failing step, with WHO collided when that is why: the same
     * candidate values `ambiguous` carries for click and wait. On the heap:
     * three of them is most of a kilobyte. */
    static const char *const CAND_KEYS[RC_SAMPLE_MAX] =
        { "cand1", "cand2", "cand3" };
    typedef char CandBuf[RC_CAND_BUDGET + 16];
    HeapPtr<CandBuf> cand((CandBuf *)HeapAlloc(GetProcessHeap(), 0,
                                               sizeof(CandBuf)
                                               * RC_SAMPLE_MAX));
    const char *k[3 + RC_SAMPLE_MAX];
    const char *v[3 + RC_SAMPLE_MAX];
    int n = 0;
    rc_strf(step, sizeof(step), "%d", at);
    k[n] = "step";   v[n++] = step;
    k[n] = "done";   v[n++] = done;
    k[n] = "detail"; v[n++] = failed.detail;
    for (int i = 0; cand.valid() && i < failed.candN && i < RC_SAMPLE_MAX;
         i++)
    {
        if (!rc_uirec_candidate(cand.get()[i], sizeof(CandBuf),
                                failed.cand[i]))
            continue;
        k[n] = CAND_KEYS[i];
        v[n++] = cand.get()[i];
    }
    link.report(sink.data(), sink.size());
    link.end(failed.status, k, v, n);
}
