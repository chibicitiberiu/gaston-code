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

/* --- input refusals -------------------------------------------------
 *
 * Only the refusals are testable without a second machine looking on, and
 * the refusals are the part that can hurt somebody. Delivery needs the
 * guest.
 */
static void inputRefusals(void)
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
    char detail[128];

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

class CollectSink : public ByteSink
{
public:
    CollectSink() : m_n(0) {}
    virtual bool write(const void *d, unsigned len)
    {
        if (m_n + len <= sizeof(m_buf))
            memcpy(m_buf + m_n, d, len);
        m_n += len;
        return true;
    }
    unsigned count() const { return m_n; }
    const unsigned char *bytes() const { return m_buf; }
private:
    unsigned char m_buf[64 * 1024];
    unsigned      m_n;
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

static void enrichment(void)
{
    MsaaSource msaa;
    char detail[160];

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
    bool got = msaa.enrich(w, e);
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

    say("msaa-refuses-dead-window", !msaa.enrich(NULL, e), "NULL hwnd");

    DestroyWindow(w);
    UnregisterClassA("GastonMsaaCheck", wc.hInstance);
}

static void capture(void)
{
    char detail[160];
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

int main(int argc, char **argv)
{
    /* A mode, not a case: it writes a file for the Python side to open, and
     * prints dimensions rather than verdict rows. */
    if (argc > 2 && !strcmp(argv[1], "--shot"))
        return writeShot(argv[2]);

    (void)argc; (void)argv;
    grammar();
    matching();
    walking();
    inputRefusals();
    enrichment();
    capture();
    /* Zero regardless: the verdict rows are the result, and the exit status
     * is reserved for setup failure, as in the other checks. */
    return 0;
}
