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
#include "ui/uilink.h"
#include "ui/uicontext.h"
#include "net/caps.h"
#include "ui/uirec.h"
#include "automation/steps.h"
#include "core/log.h"
#include "core/rc_cpp.h"
#include "core/str.h"

#include <stdlib.h>
#include <string.h>

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
