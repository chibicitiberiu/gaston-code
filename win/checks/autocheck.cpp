/* autocheck.exe -- libautomation's selector parser and tree walker.
 *
 * Runs the real code under Wine and prints `verdict<TAB>case<TAB>detail`
 * rows for tests/python/guest/test_guest_automation.py. Windows are created
 * WS_POPUP and never shown, and messages are SENT not posted, so the window
 * procedures run on this thread and no message loop is needed.
 *
 * What Wine can and cannot settle here: the selector grammar is pure string
 * work and Wine proves it completely. The walk is USER32 behaviour and Wine
 * only proves it does not crash -- the numbers come off the guest.
 */
#include "automation/tree.h"
#include "automation/select.h"
#include "automation/resolve.h"
#include "automation/watch.h"
#include "automation/wait.h"
#include "automation/input.h"
#include "automation/msaa.h"
#include "automation/shot.h"
#include "core/rc_cpp.h"
#include "core/str.h"

#include <stdio.h>
#include <string.h>

static int g_fails;

static void say(const char *name, bool pass, const char *detail)
{
    printf("%s\t%s\t%s\n", pass ? "pass" : "FAIL", name, detail ? detail : "");
    if (!pass)
        g_fails++;
}

/* Not static, and CALLED THROUGH A TABLE at the bottom of the file. Neither
 * is a style choice.
 *
 * As statics called once each, the compiler inlined all six into main and
 * their locals summed to a 9632-byte frame -- over the 4096 a frame may use
 * before it is written past Win98's guard page, which never grows the stack
 * and faults instead. External linkage fixed that, and then stopped being
 * enough at nine sections: Watcom inlined them anyway and the gate caught
 * main at 4708 bytes. A call through a pointer cannot be inlined at all. */

/* --- the grammar ---------------------------------------------------- */

static void checkParses(const char *name, const char *sel, bool want)
{
    static Selector s;
    static char err[128];
    bool ok = s.parse(sel, err, sizeof(err));
    static char detail[256];
    rc_strf(detail, sizeof(detail), "%s -> %s", sel, ok ? "ok" : err);
    say(name, ok == want, detail);
}

void grammar(void)
{
    checkParses("parse-class",        "Notepad", true);
    checkParses("parse-ctrlid",       "#1001", true);
    checkParses("parse-descendant",   "Notepad Edit", true);
    checkParses("parse-direct-child", "Notepad > Edit", true);
    checkParses("parse-attr-exact",   "[text=\"Untitled\"]", true);
    checkParses("parse-attr-prefix",  "[text^=\"Unt\"]", true);
    checkParses("parse-attr-suffix",  "[text$=\"pad\"]", true);
    checkParses("parse-attr-contains","[text*=\"tit\"]", true);
    checkParses("parse-pseudo",       "Edit:visible:enabled", true);
    checkParses("parse-nth",          "Button:nth(2)", true);
    checkParses("parse-attr-action",  "[action=press]", true);
    checkParses("parse-attr-state",   "[state*=\"checked\"]", true);
    checkParses("parse-combined",     "Notepad > Edit[text^=\"a\"]:visible",
                true);

    /* Refusals. Each of these is a thing somebody will type. */
    checkParses("reject-empty",        "", false);
    checkParses("reject-dangling-gt",  "Notepad >", false);
    checkParses("reject-leading-gt",   "> Edit", false);
    checkParses("reject-bad-attr",     "[colour=\"red\"]", false);
    checkParses("reject-no-equals",    "[text]", false);
    checkParses("reject-unterminated", "[text=\"abc]", false);
    checkParses("reject-bad-pseudo",   "Edit:sparkly", false);
    checkParses("reject-nth-zero",     "Edit:nth(0)", false);
    checkParses("reject-nonnumeric-id","#abc", false);
    /* :nth counts what the WHOLE selector matched, so anywhere but the last
     * step it is meaningless -- and accepting it there would have returned a
     * confidently wrong control. */
    checkParses("reject-nth-not-last", "Notepad:nth(2) Edit", false);

    /* A selector that names only MSAA attributes must say so, or the
     * caller will never enrich and it will never match. */
    {
        static Selector a, b;
        static char err[128];
        a.parse("[role=pushbutton]", err, sizeof(err));
        b.parse("Edit[text=\"x\"]", err, sizeof(err));
        say("msaa-flagged-when-needed", a.needsEnrichment(), "[role=...]");
        say("msaa-not-flagged-otherwise", !b.needsEnrichment(),
            "class + text only");

        /* One bit per field, because each field is a separate call across
         * the process boundary and the boundary is the whole cost. */
        static Selector c;
        c.parse("Button[state*=\"checked\"][action=press]", err, sizeof(err));
        say("msaa-wants-only-named-fields",
            c.wanted() == (Selector::WANT_STATE | Selector::WANT_ACTION),
            "[state][action] asks for neither name nor role");
    }
}

/* --- matching ------------------------------------------------------- */

static Node makeNode(const char *cls, const char *text, int ctrlId,
                     bool visible, bool enabled, bool focused)
{
    Node n;
    memset(&n, 0, sizeof(n));
    rc_strncpy(n.cls, sizeof(n.cls), cls, (unsigned)strlen(cls));
    rc_strncpy(n.text, sizeof(n.text), text, (unsigned)strlen(text));
    n.ctrlId = ctrlId;
    n.visible = visible;
    n.enabled = enabled;
    n.focused = focused;
    return n;
}

static void checkMatch(const char *name, const char *sel, const Node &n,
                       const Enrichment *e, bool want)
{
    static Selector s;
    static char err[128];
    if (!s.parse(sel, err, sizeof(err)))
    {
        say(name, false, err);
        return;
    }
    bool got = s.matchesLeaf(n, e);
    say(name, got == want, sel);
}

void matching(void)
{
    static Node edit; edit = makeNode("Edit", "Untitled - Notepad", 1001, true, true, false);
    static Node hidden; hidden = makeNode("Edit", "Untitled - Notepad", 1001, false, true,
                           false);

    checkMatch("match-class",       "Edit", edit, NULL, true);
    checkMatch("match-wrong-class", "Button", edit, NULL, false);
    checkMatch("match-ctrlid",      "#1001", edit, NULL, true);
    checkMatch("match-wrong-ctrlid","#42", edit, NULL, false);
    checkMatch("match-exact",       "[text=\"Untitled - Notepad\"]", edit,
               NULL, true);
    checkMatch("match-prefix",      "[text^=\"Untitled\"]", edit, NULL, true);
    checkMatch("match-suffix",      "[text$=\"Notepad\"]", edit, NULL, true);
    checkMatch("match-contains",    "[text*=\"d - N\"]", edit, NULL, true);
    checkMatch("match-prefix-no",   "[text^=\"Notepad\"]", edit, NULL, false);
    checkMatch("match-visible",     "Edit:visible", edit, NULL, true);
    checkMatch("match-visible-no",  "Edit:visible", hidden, NULL, false);
    checkMatch("match-focused-no",  "Edit:focused", edit, NULL, false);

    /* Without enrichment an MSAA test is UNKNOWN, not false -- saying false
     * would drop every candidate before the caller could pay for MSAA on
     * it, and the selector would match nothing, ever. */
    checkMatch("msaa-unknown-passes", "[role=pushbutton]", edit, NULL, true);
    {
        static Enrichment e;
        memset(&e, 0, sizeof(e));
        rc_strncpy(e.role, sizeof(e.role), "pushbutton", 10);
        checkMatch("msaa-known-matches", "[role=pushbutton]", edit, &e, true);
        rc_strncpy(e.role, sizeof(e.role), "text", 4);
        checkMatch("msaa-known-rejects", "[role=pushbutton]", edit, &e, false);
    }
}

/* --- the walk ------------------------------------------------------- */

class CountingSink : public NodeSink
{
public:
    CountingSink() : m_n(0), m_stopAfter(0), m_sawSelf(false) {}
    int  count() const { return m_n; }
    bool sawSelf() const { return m_sawSelf; }
    void stopAfter(int n) { m_stopAfter = n; }

    virtual bool onNode(const Node &n)
    {
        m_n++;
        if (!strcmp(n.cls, "GastonAutoCheck"))
            m_sawSelf = true;
        if (m_stopAfter && m_n >= m_stopAfter)
            return false;
        return true;
    }
private:
    int  m_n;
    int  m_stopAfter;
    bool m_sawSelf;
};

static LRESULT CALLBACK sinkProc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    return DefWindowProcA(h, m, w, l);
}

void walking(void)
{
    WNDCLASSA wc;
    memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc = (WNDPROC)sinkProc;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = "GastonAutoCheck";
    RegisterClassA(&wc);

    HWND top = CreateWindowExA(0, "GastonAutoCheck", "parent", WS_POPUP,
                               0, 0, 80, 80, NULL, NULL, wc.hInstance, NULL);
    HWND kid = CreateWindowExA(0, "GastonAutoCheck", "child", WS_CHILD,
                               0, 0, 20, 20, top, NULL, wc.hInstance, NULL);
    static char detail[128];

    {
        /* Rooted at our own window so the result does not depend on what
         * else happens to be on the desktop. visibleOnly off because these
         * are deliberately never shown. */
        WalkOptions o;
        o.root = top;
        o.visibleOnly = false;
        o.wantText = true;
        CountingSink sink;
        TreeWalker w;
        WalkEnd e = w.walk(o, sink);
        rc_strf(detail, sizeof(detail), "%d nodes, end=%d", sink.count(),
                (int)e);
        say("walk-finds-parent-and-child",
            e == WALK_COMPLETE && sink.count() == 2, detail);
        say("walk-sees-our-class", sink.sawSelf(), detail);
    }

    {
        /* A sink that stops must stop the walk, not merely be ignored. */
        WalkOptions o;
        o.root = top;
        o.visibleOnly = false;
        CountingSink sink;
        sink.stopAfter(1);
        TreeWalker w;
        WalkEnd e = w.walk(o, sink);
        rc_strf(detail, sizeof(detail), "%d nodes, end=%d", sink.count(),
                (int)e);
        say("walk-stops-when-sink-says", e == WALK_STOPPED
            && sink.count() == 1, detail);
    }

    {
        /* Hidden windows are skipped by default, which is the rule that
         * stopped the Start menu being realised by a walk. */
        WalkOptions o;
        o.root = top;
        o.visibleOnly = true;
        CountingSink sink;
        TreeWalker w;
        w.walk(o, sink);
        rc_strf(detail, sizeof(detail), "%d nodes with visibleOnly",
                sink.count());
        say("walk-skips-hidden", sink.count() <= 1, detail);
    }

    {
        /* The child-index path is what survives a repaint, so it has to be
         * built even for a one-deep tree. */
        class PathSink : public NodeSink
        {
        public:
            PathSink() { m_last[0] = '\0'; m_n = 0; }
            virtual bool onNode(const Node &n)
            {
                if (n.depth == 1)
                    rc_strncpy(m_last, sizeof(m_last), n.path,
                               (unsigned)strlen(n.path));
                m_n++;
                return true;
            }
            const char *last() const { return m_last; }
        private:
            char m_last[RC_PATH_MAX];
            int  m_n;
        };
        WalkOptions o;
        o.root = top;
        o.visibleOnly = false;
        PathSink sink;
        TreeWalker w;
        w.walk(o, sink);
        say("walk-builds-child-path", strcmp(sink.last(), "0/0") == 0,
            sink.last());
    }

    DestroyWindow(kid);
    DestroyWindow(top);
    UnregisterClassA("GastonAutoCheck", wc.hInstance);
}

/* --- resolving ------------------------------------------------------
 *
 * The join between a parsed selector and a walk. Everything here is pure
 * tree shape and string work, so Wine settles it completely -- these are
 * the cases that were parsed and then silently ignored before `Resolver`
 * existed: `>` versus descendant, `:nth`, and refusing to guess when more
 * than one control answers to the same name.
 */

struct ResolveWant
{
    ResolveOutcome outcome;
    int            count;      /* -1: do not care */
    const char    *text;       /* NULL: do not care */
    int            enriched;   /* -1: do not care */
};

static HWND checkResolve(const char *name, HWND root, const char *sel,
                         MsaaSource *msaa, const ResolveWant &want)
{
    static Selector s;
    static char err[128];
    static char detail[256];

    if (!s.parse(sel, err, sizeof(err)))
    {
        say(name, false, err);
        return NULL;
    }

    WalkOptions o;
    o.root = root;
    o.visibleOnly = false;
    o.wantText = true;

    Resolver r(s, msaa);
    TreeWalker w;
    ResolveOutcome got = r.resolve(o, w);
    const Node *m = r.match();

    bool ok = (got == want.outcome);
    if (ok && want.count >= 0)
        ok = (r.matchCount() == want.count);
    if (ok && want.text)
        ok = (m != 0 && strcmp(m->text, want.text) == 0);
    if (ok && want.enriched >= 0)
        ok = (r.enriched() == want.enriched);

    rc_strf(detail, sizeof(detail),
            "%s -> outcome=%d count=%d text=\"%s\" enriched=%d",
            sel, (int)got, r.matchCount(), m ? m->text : "", r.enriched());
    say(name, ok, detail);
    return m ? m->hwnd : NULL;
}

void resolving(void)
{
    WNDCLASSA wc;
    memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc = (WNDPROC)sinkProc;
    wc.hInstance = GetModuleHandleA(NULL);

    wc.lpszClassName = "GRTop";  RegisterClassA(&wc);
    wc.lpszClassName = "GRMid";  RegisterClassA(&wc);
    wc.lpszClassName = "GRLeaf"; RegisterClassA(&wc);

    /* top
     *  +- mid1 -- leaf "alpha"
     *  +- mid2 -- leaf "beta"
     *  +- leaf "gamma"          <- the only DIRECT GRLeaf child of top
     *  +- Button (BS_AUTOCHECKBOX)
     *
     * Three GRLeaf nodes so ambiguity has something to count, at two
     * different depths so `>` and descendant give different answers. */
    HWND top = CreateWindowExA(0, "GRTop", "top", WS_POPUP,
                               0, 0, 80, 80, NULL, NULL, wc.hInstance, NULL);
    HWND mid1 = CreateWindowExA(0, "GRMid", "mid1", WS_CHILD,
                                0, 0, 20, 20, top, NULL, wc.hInstance, NULL);
    HWND mid2 = CreateWindowExA(0, "GRMid", "mid2", WS_CHILD,
                                0, 0, 20, 20, top, NULL, wc.hInstance, NULL);
    HWND leaf1 = CreateWindowExA(0, "GRLeaf", "alpha", WS_CHILD,
                                 0, 0, 10, 10, mid1, NULL, wc.hInstance, NULL);
    HWND leaf2 = CreateWindowExA(0, "GRLeaf", "beta", WS_CHILD,
                                 0, 0, 10, 10, mid2, NULL, wc.hInstance, NULL);
    HWND leaf3 = CreateWindowExA(0, "GRLeaf", "gamma", WS_CHILD,
                                 0, 0, 10, 10, top, NULL, wc.hInstance, NULL);
    HWND chk = CreateWindowExA(0, "Button", "tick", WS_CHILD | BS_AUTOCHECKBOX,
                               0, 0, 10, 10, top, NULL, wc.hInstance, NULL);
    static ResolveWant want;

    /* One match is an answer. */
    want.outcome = RESOLVE_OK; want.count = 1; want.text = "gamma";
    want.enriched = 0;
    checkResolve("resolve-single", top, "GRTop > GRLeaf", NULL, want);

    /* The same selector without the '>' reaches all three, which is the
     * proof that directChild is read and not merely parsed. */
    want.outcome = RESOLVE_AMBIGUOUS; want.count = 3; want.text = NULL;
    want.enriched = 0;
    checkResolve("resolve-descendant-reaches-deeper", top, "GRTop GRLeaf",
                 NULL, want);

    /* A grandchild is NOT a direct child. Before Resolver this matched. */
    want.outcome = RESOLVE_NONE; want.count = 0; want.text = NULL;
    want.enriched = -1;
    checkResolve("resolve-direct-child-excludes-grandchild", top,
                 "GRTop > GRLeaf[text=\"alpha\"]", NULL, want);

    /* ...and the descendant form of the same thing does match it, so the
     * miss above is the combinator and not a broken text test. */
    want.outcome = RESOLVE_OK; want.count = 1; want.text = "alpha";
    want.enriched = 0;
    checkResolve("resolve-descendant-finds-grandchild", top,
                 "GRTop GRLeaf[text=\"alpha\"]", NULL, want);

    /* A three-step chain, every level named. */
    want.outcome = RESOLVE_OK; want.count = 1; want.text = "beta";
    want.enriched = 0;
    checkResolve("resolve-three-step-chain", top,
                 "GRTop > GRMid > GRLeaf[text=\"beta\"]", NULL, want);

    /* An ancestor that is not there. */
    want.outcome = RESOLVE_NONE; want.count = 0; want.text = NULL;
    want.enriched = -1;
    checkResolve("resolve-missing-ancestor", top, "GRLeaf GRLeaf", NULL, want);
    checkResolve("resolve-missing-class", top, "GRNope", NULL, want);

    /* Ambiguity is an ERROR with an EXACT count, not a first match. The
     * count is what the caller needs to know whether the :nth(n) it is
     * about to write exists at all. */
    want.outcome = RESOLVE_AMBIGUOUS; want.count = 3; want.text = NULL;
    want.enriched = -1;
    checkResolve("resolve-ambiguous-counts-all", top, "GRLeaf", NULL, want);

    /* :nth resolves that ambiguity deliberately, and runs out where the
     * matches do. */
    want.outcome = RESOLVE_OK; want.count = 1; want.text = NULL;
    want.enriched = -1;
    HWND n1 = checkResolve("resolve-nth-first", top, "GRLeaf:nth(1)", NULL,
                           want);
    want.count = 2;
    HWND n2 = checkResolve("resolve-nth-second", top, "GRLeaf:nth(2)", NULL,
                           want);
    want.count = 3;
    HWND n3 = checkResolve("resolve-nth-third", top, "GRLeaf:nth(3)", NULL,
                           want);
    want.outcome = RESOLVE_NONE; want.count = 3;
    checkResolve("resolve-nth-past-the-end", top, "GRLeaf:nth(4)", NULL, want);

    {
        /* Ordinals must address DIFFERENT nodes. Which one is first depends
         * on sibling z-order, so the assertion is distinctness and not a
         * particular caption. */
        static char detail[128];
        bool distinct = n1 && n2 && n3 && n1 != n2 && n2 != n3 && n1 != n3;
        rc_strf(detail, sizeof(detail), "%p %p %p", (void *)n1, (void *)n2,
                (void *)n3);
        say("resolve-nth-picks-distinct-nodes", distinct, detail);
    }

    /* A truncated walk is not an absence. Saying so is the difference
     * between a caller retrying and a caller concluding the control is
     * gone. */
    {
        static Selector s;
        static char err[128];
        static char detail[128];
        s.parse("GRNope", err, sizeof(err));
        WalkOptions o;
        o.root = top;
        o.visibleOnly = false;
        o.maxNodes = 2;
        Resolver r(s, NULL);
        TreeWalker w;
        ResolveOutcome got = r.resolve(o, w);
        rc_strf(detail, sizeof(detail), "outcome=%d end=%d", (int)got,
                (int)r.walkEnd());
        say("resolve-reports-truncation", got == RESOLVE_TRUNCATED, detail);
    }

    /* The free role. A Button's style IS its role, so [role=checkbox] on a
     * real checkbox must resolve with ZERO cross-process calls -- that is
     * the 46-of-82 controls that never reach MSAA. */
    want.outcome = RESOLVE_OK; want.count = 1; want.text = NULL;
    want.enriched = 0;
    checkResolve("resolve-role-from-style-is-free", top,
                 "GRTop > Button[role=checkbox]", NULL, want);
    want.outcome = RESOLVE_NONE; want.count = 0; want.enriched = 0;
    checkResolve("resolve-role-from-style-rejects", top,
                 "GRTop > Button[role=pushbutton]", NULL, want);

    /* An unrecognised class has no free role, and with no MSAA to ask the
     * node is REFUSED rather than guessed at. Answering "probably" here is
     * how a caller ends up clicking the wrong control. */
    want.outcome = RESOLVE_NONE; want.count = 0; want.enriched = -1;
    checkResolve("resolve-refuses-msaa-attr-without-source", top,
                 "GRLeaf[role=pushbutton]", NULL, want);

    DestroyWindow(chk);
    DestroyWindow(leaf3);
    DestroyWindow(leaf2);
    DestroyWindow(leaf1);
    DestroyWindow(mid2);
    DestroyWindow(mid1);
    DestroyWindow(top);
    UnregisterClassA("GRLeaf", wc.hInstance);
    UnregisterClassA("GRMid", wc.hInstance);
    UnregisterClassA("GRTop", wc.hInstance);
}

/* --- the hung window ------------------------------------------------
 *
 * The branch that had never executed. Every walk on both real machines
 * reported `0 timed out`, so the three-tier deferral -- the machinery for a
 * 16-bit app holding Win16Mutex and blocking USER and GDI for every thread
 * in the system -- was entirely untested. Hangs are common on 9x, and this
 * is the one path where being wrong freezes the client's UI thread rather
 * than returning a wrong answer.
 *
 * No real hung application is needed: a thread that owns windows and never
 * pumps its queue is a hung thread by definition. Every SendMessageTimeout
 * to its windows has to run out the clock.
 */

struct Wedge
{
    HANDLE    ready;
    HANDLE    release;
    HINSTANCE inst;
    HWND      top;
    int       kids;
};

static DWORD WINAPI wedgeThread(LPVOID p)
{
    Wedge *w = (Wedge *)p;
    w->top = CreateWindowExA(0, "GastonWedge", "wedged", WS_POPUP,
                             0, 0, 40, 40, NULL, NULL, w->inst, NULL);
    for (int i = 0; i < w->kids; i++)
        CreateWindowExA(0, "GastonWedge", "kid", WS_CHILD, 0, 0, 10, 10,
                        w->top, NULL, w->inst, NULL);
    SetEvent(w->ready);
    /* And now it never pumps. A window's messages are dispatched by the
     * thread that owns it, so from here on every one of these windows is
     * unreachable and each ask must cost exactly its budget. */
    WaitForSingleObject(w->release, INFINITE);
    DestroyWindow(w->top);      /* takes the children with it */
    return 0;
}

static HANDLE startWedge(Wedge &w, HINSTANCE inst, int kids)
{
    memset(&w, 0, sizeof(w));
    w.ready = CreateEventA(NULL, TRUE, FALSE, NULL);
    w.release = CreateEventA(NULL, TRUE, FALSE, NULL);
    w.inst = inst;
    w.kids = kids;
    DWORD tid = 0;
    HANDLE h = CreateThread(NULL, 0, wedgeThread, &w, 0, &tid);
    if (h)
        WaitForSingleObject(w.ready, 5000);
    return h;
}

static void stopWedge(Wedge &w, HANDLE h)
{
    SetEvent(w.release);
    if (h)
    {
        WaitForSingleObject(h, 5000);
        CloseHandle(h);
    }
    CloseHandle(w.ready);
    CloseHandle(w.release);
}

void hungWindows(void)
{
    WNDCLASSA wc;
    memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc = (WNDPROC)sinkProc;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = "GastonWedge";
    RegisterClassA(&wc);
    static char detail[192];

    {
        /* One wedged thread, four wedged windows, a 200 ms budget. Asking
         * each of them separately is 800 ms; keying the refusal on the
         * THREAD makes it one. */
        static Wedge w;
        HANDLE th = startWedge(w, wc.hInstance, 3);

        WalkOptions o;
        o.root = w.top;
        o.visibleOnly = false;
        o.callTimeoutMs = 200;
        o.deadlineMs = 10000;
        CountingSink sink;
        TreeWalker tw;
        DWORD t0 = GetTickCount();
        WalkEnd e = tw.walk(o, sink);
        DWORD ms = GetTickCount() - t0;

        rc_strf(detail, sizeof(detail),
                "%d nodes, end=%d, %lu timed out, %lu deferred, %lu ms",
                sink.count(), (int)e, (unsigned long)tw.textTimeouts(),
                (unsigned long)tw.deferredNodes(), (unsigned long)ms);

        /* It finishes at all. A walk that blocks here blocks the client's
         * repaint, which is the failure this whole design is shaped around. */
        say("hung-walk-completes", e == WALK_COMPLETE && sink.count() == 4,
            detail);

        /* Every window on the thread refuses... */
        say("hung-every-window-times-out", tw.textTimeouts() == 4, detail);

        /* ...but only the FIRST one pays for the discovery. */
        say("hung-defers-the-rest-of-the-thread", tw.deferredNodes() == 3,
            detail);

        /* The point of keying on thread rather than window, in wall clock:
         * four full budgets would be 800 ms. */
        say("hung-costs-one-budget-not-four", ms < 500, detail);

        stopWedge(w, th);
    }

    {
        /* Two wedged threads in one process. The middle tier: a thread that
         * has not itself failed, in a process that has, is asked at a
         * QUARTER of the budget -- not the full one (a 16-bit app holding
         * Win16Mutex takes the whole process down with it) and not the
         * token 1 ms (it may well be fine).
         *
         * 600 ms so the tiers are far enough apart to tell apart on a
         * loaded machine: one full budget plus one quarter is ~750 ms, where
         * two full budgets would be 1200 and two token asks would be ~600. */
        static Wedge a, b;
        HANDLE ta = startWedge(a, wc.hInstance, 0);
        HANDLE tb = startWedge(b, wc.hInstance, 0);

        WalkOptions o;
        o.root = NULL;                      /* every top-level window ... */
        o.pid = GetCurrentProcessId();      /* ... that is ours */
        o.visibleOnly = false;
        o.callTimeoutMs = 600;
        o.deadlineMs = 10000;
        CountingSink sink;
        TreeWalker tw;
        DWORD t0 = GetTickCount();
        tw.walk(o, sink);
        DWORD ms = GetTickCount() - t0;

        rc_strf(detail, sizeof(detail),
                "%d nodes, %lu timed out, %lu deferred, %lu ms "
                "(full=600, +quarter=750, two full=1200)",
                sink.count(), (unsigned long)tw.textTimeouts(),
                (unsigned long)tw.deferredNodes(), (unsigned long)ms);

        /* Exactly one window in the whole walk pays full price, whichever
         * the z-order puts first. Not "two nodes": a thread gets windows it
         * did not ask for -- Wine gives each one an IME window, and 9x has
         * its own -- so the assertion is the ratio and not the count. */
        say("hung-only-one-window-pays-full-price",
            tw.textTimeouts() >= 2
            && tw.deferredNodes() == tw.textTimeouts() - 1, detail);

        say("hung-second-thread-pays-a-quarter",
            ms >= 650 && ms < 1050, detail);

        stopWedge(a, ta);
        stopWedge(b, tb);
    }

    {
        /* A deadline shorter than the budget: the walk must give up and SAY
         * it gave up, with the nodes it did get. Returning WALK_COMPLETE
         * here would tell a caller the control it wants is not there. */
        static Wedge w;
        HANDLE th = startWedge(w, wc.hInstance, 3);

        WalkOptions o;
        o.root = w.top;
        o.visibleOnly = false;
        o.callTimeoutMs = 300;
        o.deadlineMs = 100;
        CountingSink sink;
        TreeWalker tw;
        WalkEnd e = tw.walk(o, sink);

        rc_strf(detail, sizeof(detail), "end=%d after %d of 4 nodes",
                (int)e, sink.count());
        say("hung-deadline-truncates-and-says-so",
            e == WALK_DEADLINE && sink.count() >= 1 && sink.count() < 4,
            detail);

        stopWedge(w, th);
    }

    UnregisterClassA("GastonWedge", wc.hInstance);
}

/* --- the feed and the waits ------------------------------------------
 *
 * A window that appears LATER, from a thread that is not this one, so the
 * waits have something real to wait for. Hidden rather than shown: a check
 * that flashes a window across somebody's desktop is a check nobody runs,
 * and EVENT_OBJECT_CREATE fires on creation whether or not WS_VISIBLE is
 * set.
 *
 * The helper thread stays alive and PUMPS until it is released, and both
 * halves of that matter. A thread's windows are destroyed WITH the thread,
 * so one that creates a window and returns leaves nothing to find -- which
 * is exactly how these three cases failed the first time they ran. And a
 * thread that does not pump cannot answer WM_GETTEXT, so every scan would
 * pay a timeout for text the wait needs.
 */

struct Appear
{
    HANDLE    go;         /* create it now */
    HANDLE    kill;       /* destroy it now */
    HANDLE    quit;       /* and go away */
    HANDLE    made;       /* signalled once the window exists */
    HINSTANCE inst;
    DWORD     delayMs;
    HWND      hwnd;
};

static DWORD WINAPI appearThread(LPVOID p)
{
    Appear *a = (Appear *)p;
    WaitForSingleObject(a->go, 20000);
    Sleep(a->delayMs);
    a->hwnd = CreateWindowExA(0, "GastonLater", "late arrival", WS_POPUP,
                              0, 0, 30, 30, NULL, NULL, a->inst, NULL);
    SetEvent(a->made);

    for (;;)
    {
        HANDLE h[2];
        h[0] = a->kill;
        h[1] = a->quit;
        DWORD r = MsgWaitForMultipleObjects(2, h, FALSE, 20000, QS_ALLINPUT);
        if (r == WAIT_OBJECT_0)
        {
            if (a->hwnd)
                DestroyWindow(a->hwnd);
            a->hwnd = NULL;
            ResetEvent(a->kill);
            continue;
        }
        if (r == WAIT_OBJECT_0 + 1 || r == WAIT_TIMEOUT)
            break;
        MSG m;
        while (PeekMessageA(&m, NULL, 0, 0, PM_REMOVE))
        {
            TranslateMessage(&m);
            DispatchMessageA(&m);
        }
    }

    if (a->hwnd)
        DestroyWindow(a->hwnd);
    a->hwnd = NULL;
    return 0;
}

static HANDLE startAppear(Appear &a, HINSTANCE inst, DWORD delayMs)
{
    memset(&a, 0, sizeof(a));
    a.go = CreateEventA(NULL, TRUE, FALSE, NULL);
    a.kill = CreateEventA(NULL, TRUE, FALSE, NULL);
    a.quit = CreateEventA(NULL, TRUE, FALSE, NULL);
    a.made = CreateEventA(NULL, TRUE, FALSE, NULL);
    a.inst = inst;
    a.delayMs = delayMs;
    DWORD tid = 0;
    return CreateThread(NULL, 0, appearThread, &a, 0, &tid);
}

static void stopAppear(Appear &a, HANDLE th)
{
    SetEvent(a.quit);
    if (th)
    {
        WaitForSingleObject(th, 5000);
        CloseHandle(th);
    }
    CloseHandle(a.go);
    CloseHandle(a.kill);
    CloseHandle(a.quit);
    CloseHandle(a.made);
}

static void skipFeedCases(void)
{
    say("watch-delivers-a-creation", true, "skipped: no feed here");
    say("watch-bumps-the-structure-sequence", true, "skipped: no feed");
    say("watch-drains-oldest-first", true, "skipped: no feed");
    say("wait-finds-a-window-that-appears-later", true, "skipped: no feed");
    say("wait-rides-the-feed-not-the-clock", true, "skipped: no feed");
    say("wait-already-true-costs-one-scan", true, "skipped: no feed");
    say("wait-for-text-separates-absent-from-silent", true,
        "skipped: no feed");
    say("wait-for-gone-sees-a-destruction", true, "skipped: no feed");
    say("wait-times-out-and-says-so", true, "skipped: no feed");
    say("wait-for-idle-settles", true, "skipped: no feed");
    say("wait-backstop-covers-a-missing-event", true, "skipped: no feed");
    say("watch-can-skip-our-own-process", true, "skipped: no feed");
    say("watch-stops-cleanly", true, "skipped: no feed");
}

void watching(void)
{
    WNDCLASSA wc;
    memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc = (WNDPROC)sinkProc;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = "GastonLater";
    RegisterClassA(&wc);
    static char detail[224];

    UiWatch watch;
    say("watch-resolves-setwineventhook", true,
        watch.available() ? "SetWinEventHook present"
                          : "absent -- the waits fall back to polling");

    /* Classification is pure arithmetic and worth asserting whether or not
     * the feed runs here. A LOCATIONCHANGE misfiled as structure would make
     * every wait rescan the tree for the whole of a window drag, which is a
     * poll that polls harder than a poll. */
    say("watch-classifies-create-as-structure",
        UiWatch::classify(0x8000) == UI_STRUCTURE, "EVENT_OBJECT_CREATE");
    say("watch-classifies-namechange-as-content",
        UiWatch::classify(0x800C) == UI_CONTENT, "EVENT_OBJECT_NAMECHANGE");
    say("watch-classifies-focus",
        UiWatch::classify(0x8005) == UI_FOCUS, "EVENT_OBJECT_FOCUS");
    say("watch-keeps-locationchange-out-of-structure",
        UiWatch::classify(0x800B) == UI_LOCATION,
        "EVENT_OBJECT_LOCATIONCHANGE -- fires all through a drag");

    /* false: everything these cases create is in THIS process, and the
     * default keeps our own events out precisely so a client does not wake
     * on its own repaints. */
    bool started = watch.start(0, 0, false);
    rc_strf(detail, sizeof(detail), "start=%d running=%d", (int)started,
            (int)watch.running());
    say("watch-installs", started == watch.available(), detail);

    if (!watch.running())
    {
        skipFeedCases();
        UnregisterClassA("GastonLater", wc.hInstance);
        return;
    }

    TreeWalker walker;
    UiWaiter waiter(watch, walker, NULL);
    static Selector sel;
    static char err[128];
    static WaitReport rep;

    WalkOptions o;
    o.visibleOnly = false;          /* deliberately never shown */
    o.pid = GetCurrentProcessId();
    o.callTimeoutMs = 200;
    o.deadlineMs = 4000;

    static Appear a;
    HANDLE th = startAppear(a, wc.hInstance, 600);

    {
        /* The claim of WINEVENT_OUTOFCONTEXT: a window created on another
         * thread reaches this one, with no DLL anywhere. */
        unsigned before = watch.structureSeq();
        sel.parse("GastonLater", err, sizeof(err));
        SetEvent(a.go);

        /* The one that matters. `rechecks` is the evidence: a 150 ms poll
         * over a 600 ms delay is four or five scans, and one scan is most of
         * a second on a P200. */
        waiter.forControl(sel, o, 6000, rep);
        rc_strf(detail, sizeof(detail),
                "end=%d after %lu ms, %d scans, polled=%d, %d events",
                (int)rep.end, (unsigned long)rep.ms, rep.rechecks,
                (int)rep.polled, watch.events());
        say("wait-finds-a-window-that-appears-later",
            rep.end == UIWAIT_SETTLED && rep.haveNode, detail);

        /* It arrived close to when it happened, and cost almost nothing to
         * find out. */
        say("wait-rides-the-feed-not-the-clock",
            rep.ms >= 500 && rep.ms < 2000 && rep.rechecks <= 8, detail);

        say("watch-delivers-a-creation", watch.events() > 0, detail);
        rc_strf(detail, sizeof(detail), "structure seq %u -> %u, %d dropped",
                before, watch.structureSeq(), watch.dropped());
        say("watch-bumps-the-structure-sequence",
            watch.structureSeq() > before, detail);
    }

    {
        /* Oldest first. A caller reading "the dialog opened, then the OK
         * button appeared" backwards draws the opposite conclusion. */
        static UiEvent evs[RC_WATCH_RING];
        int n = watch.drain(evs, RC_WATCH_RING);
        bool ordered = true;
        for (int i = 1; i < n; i++)
            if ((long)(evs[i].at - evs[i - 1].at) < 0)
                ordered = false;
        int after = watch.drain(evs, RC_WATCH_RING);
        rc_strf(detail, sizeof(detail), "%d drained, %d left after", n, after);
        say("watch-drains-oldest-first", n > 0 && ordered && after == 0,
            detail);
    }

    {
        /* Already true is the common case and has to be free. */
        waiter.forControl(sel, o, 5000, rep);
        rc_strf(detail, sizeof(detail), "end=%d, %d scans, %lu ms",
                (int)rep.end, rep.rechecks, (unsigned long)rep.ms);
        say("wait-already-true-costs-one-scan",
            rep.end == UIWAIT_ALREADY && rep.rechecks == 1, detail);
    }

    {
        /* forText is not [text*=...] folded into the selector, and this is
         * why: on a miss the report says whether the control was ever there
         * at all. "No such window" and "that window never said that" are
         * different bugs with different next moves. */
        waiter.forText(sel, "late", o, 1500, rep);
        bool sawIt = (rep.end == UIWAIT_ALREADY || rep.end == UIWAIT_SETTLED);
        static WaitReport miss;
        waiter.forText(sel, "never printed this", o, 900, miss);
        rc_strf(detail, sizeof(detail),
                "match end=%d; miss end=%d everMatched=%d",
                (int)rep.end, (int)miss.end, (int)miss.everMatched);
        say("wait-for-text-separates-absent-from-silent",
            sawIt && miss.end == UIWAIT_TIMEOUT && miss.everMatched, detail);
    }

    {
        /* And gone again. The window belongs to the thread that made it, so
         * that thread destroys it -- on a signal from here, which is
         * deterministic where a sleep is not. */
        int evBefore = watch.events();
        SetEvent(a.kill);
        waiter.forGone(sel, o, 4000, rep);
        int evAfter = watch.events();
        rc_strf(detail, sizeof(detail),
                "end=%d after %lu ms, %d scans, events %d -> %d, hwnd=%p",
                (int)rep.end, (unsigned long)rep.ms, rep.rechecks,
                evBefore, evAfter, (void *)a.hwnd);
        say("wait-for-gone-sees-a-destruction",
            (rep.end == UIWAIT_SETTLED || rep.end == UIWAIT_ALREADY)
            && !rep.haveNode, detail);

        /* And it settled WITHOUT being told. Wine emits CREATE for this
         * window and never emits DESTROY, so the only thing that ended this
         * wait was the backstop rescan -- which is the whole reason there is
         * one. A feed that drops one event in ten does not make a wait fail,
         * it makes it hang, rarely, which is the worst failure schedule
         * there is. Win98 may well send the event; then this row says so by
         * showing the count move. */
        if (evAfter == evBefore)
            say("wait-backstop-covers-a-missing-event",
                rep.end == UIWAIT_SETTLED
                && rep.ms < RC_WAIT_BACKSTOP_MS * 3, detail);
        else
            say("wait-backstop-covers-a-missing-event", true,
                "not exercised: the feed reported the destruction");
    }

    {
        /* Something that will never exist must come back at its deadline and
         * SAY so. A primitive that hangs instead is worse than none, because
         * it hangs rarely, which is the worst failure schedule there is. */
        static Selector nope;
        nope.parse("GastonNeverExists", err, sizeof(err));
        DWORD t0 = GetTickCount();
        waiter.forControl(nope, o, 700, rep);
        DWORD ms = GetTickCount() - t0;
        rc_strf(detail, sizeof(detail),
                "end=%d after %lu ms (budget 700), %d scans, everMatched=%d",
                (int)rep.end, (unsigned long)ms, rep.rechecks,
                (int)rep.everMatched);
        say("wait-times-out-and-says-so",
            rep.end == UIWAIT_TIMEOUT && !rep.everMatched
            && ms >= 650 && ms < 2500, detail);
    }

    {
        /* Idle is the feed alone: it must cost ZERO tree scans. */
        waiter.forIdle(250, 4000, rep);
        rc_strf(detail, sizeof(detail), "end=%d after %lu ms, %d scans",
                (int)rep.end, (unsigned long)rep.ms, rep.rechecks);
        say("wait-for-idle-settles",
            rep.end == UIWAIT_SETTLED && rep.rechecks == 0
            && rep.ms >= 250, detail);
    }

    {
        /* And the default the other way round. A watch that skips our own
         * process must hear NOTHING from it, or a client watching the
         * desktop wakes on its own repaint, rescans, repaints, and wakes
         * itself again. */
        watch.stop();
        UiWatch quiet;
        if (quiet.start(0, 0, true))
        {
            int before = quiet.events();
            HWND noise = CreateWindowExA(0, "GastonLater", "ours", WS_POPUP,
                                         0, 0, 20, 20, NULL, NULL,
                                         wc.hInstance, NULL);
            quiet.settle(400);
            int after = quiet.events();
            /* Destroyed AFTER the measurement. A destroy arrives once the
             * window is already gone, so its owner cannot be looked up and
             * the filter lets it through by design -- that is one stray
             * rescan, where dropping a real destruction is a wait that
             * hangs. The loop this guards is the other direction: our own
             * window APPEARING must not wake us. */
            DestroyWindow(noise);
            quiet.settle(200);
            static UiEvent leak[RC_WATCH_RING];
            int ln = quiet.drain(leak, RC_WATCH_RING);
            DWORD lo = 0;
            if (ln > 0) GetWindowThreadProcessId(leak[0].hwnd, &lo);
            rc_strf(detail, sizeof(detail),
                    "%d kept, %d dropped as ours, %d unattributable; "
                    "first raw=0x%lX pid-now=%lu",
                    after - before, quiet.ownDropped(),
                    quiet.unattributed(),
                    ln ? (unsigned long)leak[0].raw : 0UL,
                    (unsigned long)lo);
            /* Under Wine, two of the three events a CreateWindow produces
             * are ours and get dropped; the third resolves to a pid that is
             * not ours at callback time -- Wine's own bookkeeping. So the
             * assertion is that the filter FIRES and that essentially
             * nothing survives, not that the count is a clean zero. If Win98
             * honours WINEVENT_SKIPOWNPROCESS outright the system filters
             * them for us and both numbers read zero, which also passes;
             * if more than one leaks, this row is how we find out. */
            say("watch-can-skip-our-own-process",
                (after - before) <= 1
                && (after == before || quiet.ownDropped() > 0), detail);
            quiet.stop();
        }
        else
        {
            say("watch-can-skip-our-own-process", true,
                "skipped: SKIPOWNPROCESS refused here");
        }
    }

    stopAppear(a, th);
    say("watch-stops-cleanly", !watch.running(), "hooks removed");

    UnregisterClassA("GastonLater", wc.hInstance);
}

/* --- input refusals -------------------------------------------------
 *
 * Only the refusals are testable without a second machine looking on, and
 * the refusals are the part that can hurt somebody. Delivery needs the
 * guest.
 */
void inputRefusals(void)
{
    WNDCLASSA wc;
    memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc = (WNDPROC)sinkProc;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = "GastonInputCheck";
    RegisterClassA(&wc);
    HWND mine = CreateWindowExA(0, "GastonInputCheck", "mine", WS_POPUP,
                                0, 0, 40, 40, NULL, NULL, wc.hInstance, NULL);
    HWND kid = CreateWindowExA(0, "GastonInputCheck", "kid", WS_CHILD,
                               0, 0, 10, 10, mine, NULL, wc.hInstance, NULL);
    Input in;
    static char detail[128];

    /* Our OWN window, by process. The HWND comparison this replaces missed
     * a child control, which is a different HWND on the same thread. */
    say("input-refuses-own-window", Input::isOurs(mine), "top level");
    say("input-refuses-own-child", Input::isOurs(kid),
        "child control, different HWND, same process");
    say("input-click-refuses-self", in.clickWindow(mine) == INPUT_REFUSED_SELF,
        "clickWindow on ourselves");
    say("input-type-refuses-self", in.typeText(mine, "x") == INPUT_REFUSED_SELF,
        "typeText into ourselves");
    say("input-rejects-dead-window", in.clickWindow(NULL) == INPUT_NO_WINDOW,
        "NULL hwnd");

    rc_strf(detail, sizeof(detail), "SendInput %s",
            in.usingSendInput() ? "present" : "absent, using keybd_event");
    say("input-resolves-sendinput-dynamically", true, detail);

    /* The setting must come back as it was. Leaving it at zero is a change
     * to somebody's machine that outlives the process. */
    {
        DWORD before = 0;
        SystemParametersInfoA(0x2000, 0, &before, 0);
        DWORD saved = Input::relaxForegroundLock();
        Input::restoreForegroundLock(saved);
        DWORD after = 0;
        SystemParametersInfoA(0x2000, 0, &after, 0);
        rc_strf(detail, sizeof(detail), "was %lu, saved %lu, now %lu",
                (unsigned long)before, (unsigned long)saved,
                (unsigned long)after);
        say("input-restores-foreground-lock", after == before, detail);
    }

    DestroyWindow(kid);
    DestroyWindow(mine);
    UnregisterClassA("GastonInputCheck", wc.hInstance);
}

/* --- MSAA and capture ------------------------------------------------ */

/* The buffer is on the HEAP, and this is the bug that crashed the P200.
 *
 * It was a 64KB member, and this class is a LOCAL in capture(), so the
 * frame was 64KB. Win98 grows a stack one page at a time by faulting on a
 * guard page; a frame that large is written past the guard page in one go,
 * the stack never grows, and the write faults. The crash landed on the SEH
 * prologue -- `pop dword ptr [eax]` with EAX 200KB above ESP -- which is
 * the first thing that touches the new frame, not the buffer itself.
 *
 * It ran green under Wine and on the emulator because a Linux stack is
 * committed up front. Only the real machine has the guard page that makes
 * this a rule, and BACKLOG.md section 9 already carried the postmortem for
 * the same class of fault in sync.cpp: every job buffer heap-allocates. */
class CollectSink : public ByteSink
{
public:
    CollectSink()
        : m_buf((unsigned char *)HeapAlloc(GetProcessHeap(), 0, CAP)),
          m_n(0) {}
    ~CollectSink() { if (m_buf) HeapFree(GetProcessHeap(), 0, m_buf); }
    bool ready() const { return m_buf != 0; }
    virtual bool write(const void *d, unsigned len)
    {
        if (m_buf && m_n + len <= CAP)
            memcpy(m_buf + m_n, d, len);
        m_n += len;
        return true;
    }
    unsigned count() const { return m_n; }
    const unsigned char *bytes() const { return m_buf; }
private:
    enum { CAP = 64 * 1024 };
    unsigned char *m_buf;
    unsigned       m_n;
};

class RefusingSink : public ByteSink
{
public:
    RefusingSink() : m_calls(0) {}
    virtual bool write(const void *, unsigned) { m_calls++; return false; }
    int calls() const { return m_calls; }
private:
    int m_calls;
};

void enrichment(void)
{
    MsaaSource msaa;
    static char detail[160];

    rc_strf(detail, sizeof(detail), "oleacc %s",
            msaa.available() ? "loaded" : "absent");
    say("msaa-loads-dynamically", true, detail);
    if (!msaa.available())
    {
        say("msaa-enriches-a-window", true, "skipped: no oleacc here");
        return;
    }

    /* Our own window is a fine subject: the question is whether the call
     * path works and fills the struct, not what any particular control
     * happens to be called. */
    WNDCLASSA wc;
    memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc = (WNDPROC)sinkProc;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = "GastonMsaaCheck";
    RegisterClassA(&wc);
    HWND w = CreateWindowExA(0, "GastonMsaaCheck", "subject", WS_POPUP,
                             0, 0, 60, 60, NULL, NULL, wc.hInstance, NULL);

    Enrichment e;
    int all = Selector::WANT_NAME | Selector::WANT_ROLE
            | Selector::WANT_VALUE | Selector::WANT_ACTION
            | Selector::WANT_STATE;
    bool got = msaa.enrich(w, all, e);
    rc_strf(detail, sizeof(detail), "role='%s' name='%s'", e.role, e.name);
    say("msaa-enriches-a-window", got, detail);

    /* Roles come back from GetRoleTextA as human phrases with spaces and
     * capitals; a selector is written [role=pushbutton] and should not make
     * the caller guess the spacing. */
    bool clean = true;
    for (const char *q = e.role; *q; q++)
        if (*q == ' ' || (*q >= 'A' && *q <= 'Z'))
            clean = false;
    say("msaa-role-is-selector-shaped", clean, e.role);

    /* The change that makes this affordable: ask for one field, make one
     * call. A selector that tests [role=...] must not pay for name and
     * value as well. */
    {
        static Enrichment one;
        msaa.enrich(w, Selector::WANT_ROLE, one);
        say("msaa-fetches-only-what-was-asked",
            one.role[0] != 0 && one.name[0] == 0 && one.value[0] == 0
            && one.action[0] == 0 && one.state[0] == 0,
            one.role);
    }

    /* State is the field nothing else can supply. A checkbox's tick is not
     * in the window text, and BM_GETCHECK is not marshalled across a process
     * boundary on 9x -- out of process, MSAA is the only answer.
     *
     * Whether it CAN answer depends on the oleacc underneath. Win98's ships
     * the Win32 class proxies, so a Button reports role "checkbutton" with a
     * state and a default action. Wine's returns the generic client object
     * for every class -- role "client", no state bits, no action -- so these
     * two rows report and do not assert there. That is the Wine boundary
     * this project keeps naming: it proves the code, never the platform. */
    {
        HWND chk = CreateWindowExA(0, "Button", "tick",
                                   WS_CHILD | WS_VISIBLE | BS_AUTOCHECKBOX,
                                   0, 0, 20, 20, w, NULL, wc.hInstance, NULL);
        static Enrichment probe;
        msaa.enrich(chk, Selector::WANT_ROLE, probe);
        bool proxied = probe.role[0] && strcmp(probe.role, "client") != 0;
        rc_strf(detail, sizeof(detail), "Button reports role '%s' -- %s",
                probe.role,
                proxied ? "class proxies present"
                        : "generic client object only");
        say("msaa-button-proxy-present", true, detail);

        static Enrichment before, after;
        msaa.enrich(chk, Selector::WANT_STATE, before);
        SendMessageA(chk, BM_SETCHECK, BST_CHECKED, 0);
        msaa.enrich(chk, Selector::WANT_STATE, after);
        rc_strf(detail, sizeof(detail), "'%s' -> '%s'", before.state,
                after.state);
        if (proxied)
            say("msaa-state-sees-the-tick",
                strstr(before.state, "checked") == NULL
                && strstr(after.state, "checked") != NULL, detail);
        else
            say("msaa-state-sees-the-tick", true,
                "reported, not asserted: no class proxy here");

        /* Shape is ours either way -- our own spellings and not
         * GetStateTextA's, which returns a phrase in the system's language,
         * and a grammar does not move with the locale. */
        bool clean2 = true;
        for (const char *q = after.state; *q; q++)
            if (*q == ' ' || (*q >= 'A' && *q <= 'Z'))
                clean2 = false;
        say("msaa-state-is-selector-shaped", clean2, after.state);

        static Enrichment act;
        msaa.enrich(chk, Selector::WANT_ACTION, act);
        rc_strf(detail, sizeof(detail), "action='%s'", act.action);
        if (proxied)
            say("msaa-reports-default-action", act.action[0] != 0, detail);
        else
            say("msaa-reports-default-action", true,
                "reported, not asserted: no class proxy here");

        DestroyWindow(chk);
    }

    say("msaa-refuses-dead-window", !msaa.enrich(NULL, all, e),
        "NULL hwnd");

    /* The batched tier. A document is a single HWND, so the WINDOW walk
     * sees one node and stops -- this is the only tree a web page has. */
    {
        class AccCount : public AccSink
        {
        public:
            AccCount() : m_n(0), m_deepest(0), m_rects(0), m_doc(false) {}
            virtual bool onAcc(const AccNode &n)
            {
                m_n++;
                if (n.depth > m_deepest) m_deepest = n.depth;
                if (n.hasRect) m_rects++;
                if (n.roleId == RC_ROLE_DOCUMENT) m_doc = true;
                return true;
            }
            int  count() const { return m_n; }
            int  deepest() const { return m_deepest; }
            int  rects() const { return m_rects; }
            bool sawDocument() const { return m_doc; }
        private:
            int m_n, m_deepest, m_rects;
            bool m_doc;
        };

        /* Real children, so the BATCHING has something to batch. Without
         * them the walk is one node and the whole point of the tier goes
         * untested. */
        HWND kid[3];
        for (int i = 0; i < 3; i++)
            kid[i] = CreateWindowExA(0, "Button", "kid",
                                     WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
                                     0, (long)(i * 22), 60, 20, w, NULL,
                                     wc.hInstance, NULL);

        AccWalkOptions ao;
        AccCount counter;
        AccEnd ae = msaa.enumerateFrom(w, ao, counter);
        int batches = msaa.enumeratorLevels() + msaa.helperLevels();
        rc_strf(detail, sizeof(detail),
                "end=%d, %d nodes, depth %d, %d with a rect, "
                "%d enum batches / %d helper batches",
                (int)ae, counter.count(), counter.deepest(), counter.rects(),
                msaa.enumeratorLevels(), msaa.helperLevels());
        /* The root object itself always arrives, children or not. */
        say("msaa-enumerates-from-a-window",
            ae == ACC_COMPLETE && counter.count() >= 1, detail);

        /* A rectangle is not a nicety here: a link's text says it exists,
         * the rectangle is what lets an agent press it. */
        say("msaa-enumeration-carries-rectangles", counter.rects() >= 1,
            detail);

        /* Three real children must arrive in ONE batch, not three round
         * trips. Wine's oleacc reports no accessible children for any class
         * -- the same missing Win32 proxies as the state and action rows --
         * so there this reports instead of asserting, and the guest run is
         * what settles it. Which batcher won is a per-machine fact either
         * way: the P200 preferred AccessibleChildren and the XP preferred
         * the enumerator by seven times. */
        if (counter.count() > 1)
            say("msaa-batches-children",
                batches >= 1 && batches <= counter.count() / 2 + 1, detail);
        else
            say("msaa-batches-children", true,
                "reported, not asserted: no accessible children here");

        for (int i = 0; i < 3; i++)
            if (kid[i]) DestroyWindow(kid[i]);

        say("msaa-refuses-a-dead-window-for-enumeration",
            msaa.enumerateFrom(NULL, ao, counter) == ACC_NO_OBJECT,
            "NULL hwnd");
    }

    {
        /* A sink that stops must stop the walk, and a node cap must cap it.
         * Unbounded recursion through somebody else's accessibility tree is
         * exactly the allocation this project refuses to make. */
        class StopAt : public AccSink
        {
        public:
            StopAt() : m_n(0) {}
            virtual bool onAcc(const AccNode &) { m_n++; return false; }
            int count() const { return m_n; }
        private:
            int m_n;
        };
        AccWalkOptions ao;
        StopAt s1;
        AccEnd ae = msaa.enumerateFrom(w, ao, s1);
        rc_strf(detail, sizeof(detail), "end=%d after %d node(s)", (int)ae,
                s1.count());
        say("msaa-enumeration-stops-when-sink-says",
            ae == ACC_STOPPED && s1.count() == 1, detail);
    }

    DestroyWindow(w);
    UnregisterClassA("GastonMsaaCheck", wc.hInstance);
}

void capture(void)
{
    static char detail[160];
    {
        ShotOptions o;
        o.maxBpp = 8;
        o.rect.left = 0; o.rect.top = 0;
        o.rect.right = 64; o.rect.bottom = 48;
        CollectSink sink;
        Screenshot shot;
        bool ok = shot.capture(o, sink);
        rc_strf(detail, sizeof(detail), "%dx%d at %dbpp, %u bytes: %s",
                shot.width(), shot.height(), shot.bpp(), sink.count(),
                ok ? "ok" : shot.error());
        say("shot-captures-a-region", ok && shot.width() == 64
            && shot.height() == 48, detail);

        /* A real BMP or nothing. 'BM', and a bfSize that agrees with what
         * actually came out of the sink -- a header that lies about the
         * length is the failure the bridge would find hardest to read. */
        bool looksRight = sink.count() > 54
                       && sink.bytes()[0] == 'B' && sink.bytes()[1] == 'M';
        unsigned declared = 0;
        if (sink.count() >= 6)
            memcpy(&declared, sink.bytes() + 2, 4);
        rc_strf(detail, sizeof(detail), "declared %u, wrote %u",
                declared, sink.count());
        say("shot-writes-a-real-bmp", looksRight && declared == sink.count(),
            detail);

        /* 8bpp must carry its colour table or the bridge gets indices and
         * no way to turn them into colours. */
        unsigned off = 0;
        if (sink.count() >= 14)
            memcpy(&off, sink.bytes() + 10, 4);
        rc_strf(detail, sizeof(detail), "pixels start at %u", off);
        say("shot-8bpp-carries-a-palette",
            shot.bpp() > 8 || off == 54 + 256 * 4, detail);
    }

    {
        /* maxBpp is a CAP, not a request: asking for 32 on an 8bpp screen
         * must not invent colour that is not there. */
        ShotOptions o;
        o.maxBpp = 32;
        o.rect.left = 0; o.rect.top = 0;
        o.rect.right = 32; o.rect.bottom = 32;
        CollectSink sink;
        Screenshot shot;
        shot.capture(o, sink);
        HDC dc = GetDC(NULL);
        int screen = GetDeviceCaps(dc, BITSPIXEL);
        ReleaseDC(NULL, dc);
        rc_strf(detail, sizeof(detail), "screen %d, asked 32, got %d",
                screen, shot.bpp());
        say("shot-bpp-is-capped-not-requested", shot.bpp() <= screen, detail);
    }

    {
        /* A sink that has gone away stops the capture rather than having a
         * megabyte produced for nobody. */
        ShotOptions o;
        o.rect.left = 0; o.rect.top = 0;
        o.rect.right = 64; o.rect.bottom = 64;
        RefusingSink sink;
        Screenshot shot;
        bool ok = shot.capture(o, sink);
        rc_strf(detail, sizeof(detail), "%d sink calls before it stopped",
                sink.calls());
        say("shot-stops-when-the-sink-refuses", !ok && sink.calls() == 1,
            detail);
    }

    {
        ShotOptions o;
        o.window = (HWND)0x1;          /* not a window */
        CollectSink sink;
        Screenshot shot;
        say("shot-refuses-a-dead-window", !shot.capture(o, sink),
            shot.error());
    }
}

/* Write one capture to a file so the host can DECODE it rather than trust
 * a byte count. "Looks like a BMP" and "is a BMP" are different claims, and
 * only the second one is worth having -- the same reason the RLE8 gate was
 * re-done as an off-machine comparison. */
class FileSink : public ByteSink
{
public:
    explicit FileSink(const char *path)
        : m_f(fopen(path, "wb")) {}
    ~FileSink() { if (m_f.valid()) fclose(m_f.get()); }
    bool ok() const { return m_f.valid(); }
    virtual bool write(const void *d, unsigned len)
    {
        return m_f.valid()
            && fwrite(d, 1, len, m_f.get()) == len;
    }
private:
    File m_f;
};

static int writeShot(const char *path)
{
    ShotOptions o;
    o.maxBpp = 8;
    o.rect.left = 0; o.rect.top = 0;
    o.rect.right = 96; o.rect.bottom = 64;
    FileSink sink(path);
    if (!sink.ok())
    {
        fprintf(stderr, "cannot write %s\n", path);
        return 2;
    }
    Screenshot shot;
    if (!shot.capture(o, sink))
    {
        fprintf(stderr, "capture failed: %s\n", shot.error());
        return 1;
    }
    printf("%d %d %d %u\n", shot.width(), shot.height(), shot.bpp(),
           shot.bytesWritten());
    return 0;
}

typedef void (*Section)(void);

static const Section SECTIONS[] =
{
    grammar, matching, walking, resolving, hungWindows, watching,
    inputRefusals, enrichment, capture
};

int main(int argc, char **argv)
{
    /* A mode, not a case: it writes a file for the Python side to open, and
     * prints dimensions rather than verdict rows. */
    if (argc > 2 && !strcmp(argv[1], "--shot"))
        return writeShot(argv[2]);

    (void)argc; (void)argv;
    for (unsigned i = 0; i < sizeof(SECTIONS) / sizeof(SECTIONS[0]); i++)
        SECTIONS[i]();
    /* Zero regardless: the verdict rows are the result, and the exit status
     * is reserved for setup failure, as in the other checks. */
    return 0;
}
