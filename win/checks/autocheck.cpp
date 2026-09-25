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

/* --- the grammar ---------------------------------------------------- */

static void checkParses(const char *name, const char *sel, bool want)
{
    Selector s;
    char err[128];
    bool ok = s.parse(sel, err, sizeof(err));
    char detail[256];
    rc_strf(detail, sizeof(detail), "%s -> %s", sel, ok ? "ok" : err);
    say(name, ok == want, detail);
}

static void grammar(void)
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

    /* A selector that names only MSAA attributes must say so, or the
     * caller will never enrich and it will never match. */
    {
        Selector a, b;
        char err[128];
        a.parse("[role=pushbutton]", err, sizeof(err));
        b.parse("Edit[text=\"x\"]", err, sizeof(err));
        say("msaa-flagged-when-needed", a.needsEnrichment(), "[role=...]");
        say("msaa-not-flagged-otherwise", !b.needsEnrichment(),
            "class + text only");
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
    Selector s;
    char err[128];
    if (!s.parse(sel, err, sizeof(err)))
    {
        say(name, false, err);
        return;
    }
    bool got = s.matchesLeaf(n, e);
    say(name, got == want, sel);
}

static void matching(void)
{
    Node edit = makeNode("Edit", "Untitled - Notepad", 1001, true, true, false);
    Node hidden = makeNode("Edit", "Untitled - Notepad", 1001, false, true,
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
        Enrichment e;
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

static void walking(void)
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
    char detail[128];

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

int main(void)
{
    grammar();
    matching();
    walking();
    /* Zero regardless: the verdict rows are the result, and the exit status
     * is reserved for setup failure, as in the other checks. */
    return 0;
}
