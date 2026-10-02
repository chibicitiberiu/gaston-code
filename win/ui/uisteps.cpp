/* uisteps.cpp -- UIRUN: the `input` tool's step list, on the desktop lane.
 *
 * The adapter between the wire and libautomation's StepRunner, and nothing
 * more: it decodes the body into Steps, runs them, and encodes one result
 * line per step that ran plus a UIEND that says how the list ended.
 *
 * BODY IN: one step per line, `verb` then TAB-separated `key=value` fields,
 * every value percent-encoded by the bridge (bridge/agent/steps.py) so a
 * selector or a typed string may hold any byte, TABs and newlines included.
 * Steps ride the BODY and not headers because a header value is capped at
 * 255 encoded bytes, and a selector clipped to fit is a different selector.
 *
 * BODY OUT: the same shape, one line per step that ran:
 *     i=<n> status=<word> ms=<n> hit=<window> detail=<text> part=<n>
 * and a last line `released=1` when the list ended with something still
 * held, which the runner released.
 *
 * UICANCEL id=... arrives on the READER thread (rpc.cpp, the immediate
 * lane), because the desktop worker is the thread busy running the list it
 * is meant to stop. It raises a flag the runner looks at between steps and
 * inside every wait.
 */
#include "ui/uirpc.h"
#include "ui/uicontext.h"
#include "ui/uirec.h"
#include "automation/steps.h"
#include "core/log.h"
#include "core/rc_cpp.h"
#include "core/str.h"

#include <stdlib.h>
#include <string.h>

/* The list that is running, if one is, and its stop flag. A plain buffer
 * the reader compares against: a race here can only miss a cancel for a
 * list that is finishing anyway, or arrive for none, and both are logged. */
static volatile LONG g_run_cancel;
static char          g_run_id[48];

void rc_uirpc_cancel(const char *id)
{
    if (!id || !g_run_id[0] || strcmp(id, g_run_id) != 0)
    {
        rc_log("desktop: UICANCEL %s matches no running list -- ignored",
               id ? id : "(no id)");
        return;
    }
    InterlockedExchange(&g_run_cancel, 1);
}

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
#define RC_RUN_MAX 64

static int decodeSteps(char *body, unsigned len, Step *out, char *why,
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

static void sendEnd(rc_net *net, const char *id, const char *status,
                    const char *const *k, const char *const *v, int n,
                    const LineSink *body)
{
    const char *keys[10];
    const char *vals[10];
    int m = 0;
    keys[m] = "id";     vals[m++] = id;
    keys[m] = "status"; vals[m++] = status;
    for (int i = 0; i < n && m < 10; i++)
    {
        keys[m] = k[i];
        vals[m++] = v[i];
    }
    rc_net_send(net, "UIEND", keys, vals, m,
                body && body->ok() ? body->data() : NULL,
                body && body->ok() ? body->size() : 0);
}

void rc_uirpc_run(rc_net *net, UiContext *ctx, rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    char why[96];
    if (!id)
    {
        rc_log("UIRUN: no id -- dropped");
        return;
    }
    HeapPtr<Step> steps((Step *)HeapAlloc(GetProcessHeap(), 0,
                                          RC_RUN_MAX * sizeof(Step)));
    LineSink sink;
    if (!steps.valid() || !sink.ok())
    {
        sendEnd(net, id, "no_memory", NULL, NULL, 0, NULL);
        return;
    }
    int count = f->body ? decodeSteps(f->body, f->body_len, steps.get(),
                                      why, sizeof(why)) : 0;
    if (count < 0)
    {
        const char *k[1] = { "detail" };
        const char *v[1] = { why };
        sendEnd(net, id, "bad_steps", k, v, 1, NULL);
        return;
    }

    /* The walk every step shares: the whole desktop, never our own windows,
     * and per-message timeouts small enough that one hung program costs a
     * walk 200ms rather than the step. */
    WalkOptions o;
    o.visibleOnly = true;
    o.wantText = true;
    o.maxNodes = 400;
    o.callTimeoutMs = 200;
    o.deadlineMs = 8000;
    o.excludePid = ctx->selfPid;

    StepLimits lim;
    lim.scriptMs = (DWORD)headerNum(f, "timeout_ms", 120000, 100, 600000);
    lim.stepMs = (DWORD)headerNum(f, "step_ms", 30000, 100, 120000);
    lim.settleMs = (DWORD)headerNum(f, "settle", 0, 0, 5000);

    StepRunner runner(ctx->walker, ctx->msaa.available() ? &ctx->msaa : 0,
                      ctx->watch, ctx->input, &ctx->deadline);
    InterlockedExchange(&g_run_cancel, 0);
    rc_strf(g_run_id, sizeof(g_run_id), "%s", id);
    runner.setCancel(&g_run_cancel);

    StepReport failed;
    int at = runner.run(steps.get(), count, o, lim, sink, failed);
    g_run_id[0] = '\0';
    if (runner.releasedAtEnd())
        sink.put("released=1\n");

    char done[16], step[16];
    rc_strf(done, sizeof(done), "%d", at < 0 ? count : at);
    if (at < 0)
    {
        const char *k[1] = { "done" };
        const char *v[1] = { done };
        sendEnd(net, id, "ok", k, v, 1, &sink);
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
    sendEnd(net, id, failed.status, k, v, n, &sink);
}
