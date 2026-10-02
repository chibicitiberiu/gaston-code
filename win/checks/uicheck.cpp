/* uicheck.exe -- the wire record, and nothing else.
 *
 * Separate from autocheck because this is the one piece of the ui work that
 * is an AGREEMENT: the guest writes these bytes and the bridge parses them,
 * and the two must not drift. `--emit` writes a fixture the Python side
 * parses and compares field by field; the verdict rows here cover the cases
 * a fixture cannot show, which are the refusals.
 *
 * The thing under test is a format, so the interesting inputs are the ugly
 * ones: text with a newline in it, text with a NUL, a record one byte too
 * long for the buffer.
 */
#include "ui/uirec.h"
#include "ui/uiengine.h"
#include "net/caps.h"
#include "ui/inject.h"
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

static Node mk(const char *path, const char *cls, const char *text,
               int ctrl)
{
    Node n;
    memset(&n, 0, sizeof(n));
    rc_strncpy(n.path, sizeof(n.path), path, (unsigned)strlen(path));
    rc_strncpy(n.cls, sizeof(n.cls), cls, (unsigned)strlen(cls));
    rc_strncpy(n.text, sizeof(n.text), text, (unsigned)strlen(text));
    rc_strncpy(n.role, sizeof(n.role), "pushbutton", 10);
    n.ctrlId = ctrl;
    n.visible = true;
    n.enabled = true;
    n.rect.left = 12; n.rect.top = 40;
    n.rect.right = 300; n.rect.bottom = 60;
    return n;
}

/* The fixture the bridge parses. Every record here exists to break a
 * parser that took a shortcut. */
static int emitFixture(const char *path)
{
    static char buf[8192];
    unsigned len = 0, n;
    FILE *f;

    {
        Node a = mk("0", "#32770", "Save As", 0);
        n = rc_uirec_window(buf + len, sizeof(buf) - len, a, NULL);
        len += n;
    }
    {
        Node b = mk("0/1", "Button", "OK", 1);
        Enrichment e;
        memset(&e, 0, sizeof(e));
        rc_strncpy(e.name, sizeof(e.name), "OK", 2);
        rc_strncpy(e.state, sizeof(e.state), "focusable", 9);
        rc_strncpy(e.action, sizeof(e.action), "press", 5);
        b.focused = true;
        n = rc_uirec_window(buf + len, sizeof(buf) - len, b, &e);
        len += n;
    }
    {
        /* A multi-line Edit. This is the record that a key=value line
         * format would have turned into three bogus records. */
        Node c = mk("0/2", "Edit", "line one\nline two\ttabbed", 1001);
        n = rc_uirec_window(buf + len, sizeof(buf) - len, c, NULL);
        len += n;
    }
    {
        /* Text that fills Node::text exactly, leaving no terminator. */
        Node d = mk("0/3", "Static", "", 0);
        memset(d.text, 'x', sizeof(d.text));
        n = rc_uirec_window(buf + len, sizeof(buf) - len, d, NULL);
        len += n;
    }
    {
        /* Not a window at all. */
        AccNode g;
        memset(&g, 0, sizeof(g));
        g.depth = 1;
        g.childId = 7;
        g.roleId = 30;
        rc_strncpy(g.role, sizeof(g.role), "link", 4);
        rc_strncpy(g.name, sizeof(g.name), "First link", 10);
        rc_strncpy(g.value, sizeof(g.value), "http://example.com/one", 22);
        rc_strncpy(g.state, sizeof(g.state), "focusable", 9);
        g.rect.left = 4; g.rect.top = 8; g.rect.right = 90; g.rect.bottom = 24;
        g.hasRect = true;
        n = rc_uirec_acc(buf + len, sizeof(buf) - len, g, "0/4/7", "0/4");
        len += n;
    }

    f = fopen(path, "wb");
    if (!f)
    {
        fprintf(stderr, "cannot write %s\n", path);
        return 1;
    }
    fwrite(buf, 1, len, f);
    fclose(f);
    printf("%u\n", len);
    return 0;
}

/* The deny list, and the refusals around it.
 *
 * Injection is the one thing in this project that puts our code inside a
 * program somebody else is running, so the interesting tests are all
 * refusals. None of these inject anything.
 */
static void injection(void)
{
    static char detail[160];

    say("inject-allows-an-ordinary-program",
        rc_inject_allowed("notepad.exe"), "notepad.exe");
    say("inject-refuses-the-shell",
        !rc_inject_allowed("EXPLORER.EXE"), "EXPLORER.EXE");
    say("inject-refuses-the-shell-by-full-path",
        !rc_inject_allowed("C:\\WINDOWS\\EXPLORER.EXE"),
        "the list is checked against the BASENAME, not the string");
    say("inject-refuses-whatever-the-case",
        !rc_inject_allowed("explorer.exe") && !rc_inject_allowed("ExPlOrEr.ExE"),
        "a list that matches one spelling is not a list");
    say("inject-refuses-the-9x-system-processes",
        !rc_inject_allowed("MSGSRV32.EXE") && !rc_inject_allowed("MPREXE.EXE")
        && !rc_inject_allowed("KERNEL32.DLL") && !rc_inject_allowed("MMTASK.TSK"),
        "killing these hangs 9x; injecting is worse, it shows up later");
    say("inject-refuses-an-empty-name", !rc_inject_allowed("")
        && !rc_inject_allowed(NULL) && !rc_inject_allowed("C:\\"),
        "unnamed is a refusal, not a licence: if we cannot say what a "
        "process is, we cannot say it is not the shell");

    /* No force escape, unlike KILL. REMOTE.md:344 lets KILL take Explorer
     * with force=1; there is deliberately no equivalent here, and the
     * absence is asserted by there being no second argument to check. */
    say("inject-has-no-force-escape", true,
        "rc_inject_allowed takes a name and nothing else");

    {
        /* Our own window is refused before anything is loaded. */
        Injector inj;
        bool opened = inj.open();
        rc_strf(detail, sizeof(detail), "open=%d", (int)opened);
        say("inject-opens-its-section", opened, detail);
        if (opened)
        {
            const RcInShm *shm = inj.shm();
            say("inject-stamps-the-section",
                shm && shm->magic == RC_SHM_MAGIC
                && shm->version == RC_SHM_VER
                && shm->cb == sizeof(RcInShm) && shm->generation != 0,
                "magic, version, size and a generation");
            /* Zeroed per run, which is what gate 0.15d was missing: the
             * counters were cumulative since boot and the verdict read
             * one of them. */
            say("inject-zeroes-the-counters",
                shm && shm->attaches == 0 && shm->pids_n == 0
                && shm->draws == 0 && shm->patched == 0,
                "a run measures itself");
            say("inject-names-nobody-until-asked",
                shm && shm->patch_pid == 0 && shm->patch_exe[0] == 0,
                "a section that names a target is what a resident copy acts on");

            HWND mine = CreateWindowExA(0, "GastonInjectCheck", "mine",
                                        WS_POPUP, 0, 0, 30, 30, NULL, NULL,
                                        GetModuleHandleA(NULL), NULL);
            if (mine)
            {
                InjectResult r = inj.collect(mine, NULL, false, 500);
                rc_strf(detail, sizeof(detail), "result=%d (%s)", (int)r,
                        inj.lastError());
                say("inject-refuses-our-own-process", r == INJECT_DENIED,
                    detail);
                DestroyWindow(mine);
            }
            else
            {
                say("inject-refuses-our-own-process", true,
                    "skipped: no window");
            }

            InjectResult r2 = inj.collect(NULL, NULL, false, 200);
            say("inject-refuses-a-null-window", r2 == INJECT_NO_WINDOW,
                "NULL hwnd");

            /* And it disarms on the way out, so a copy of the DLL still
             * resident in some process has nothing to act on. */
            inj.close();
            say("inject-disarms-on-close", !inj.ready(), "section released");
        }
        else
        {
            say("inject-stamps-the-section", true, "skipped: no section");
            say("inject-zeroes-the-counters", true, "skipped: no section");
            say("inject-names-nobody-until-asked", true, "skipped");
            say("inject-refuses-our-own-process", true, "skipped");
            say("inject-refuses-a-null-window", true, "skipped");
            say("inject-disarms-on-close", true, "skipped");
        }
    }
}

/* The record the in-process collector produces goes through the SAME
 * parser as the out-of-process one. That is the point of it having the
 * same shape: the bridge does not learn a second format. */
static void inprocRecords(void)
{
    static char buf[2048];
    unsigned n;

    {
        RcInNode n1;
        memset(&n1, 0, sizeof(n1));
        n1.ctrlId = 1;
        n1.style = WS_VISIBLE | WS_CHILD | BS_AUTOCHECKBOX;
        n1.checked = 1;
        n1.left = 4; n1.top = 8; n1.right = 100; n1.bottom = 28;
        rc_strncpy(n1.cls, sizeof(n1.cls), "Button", 6);
        rc_strncpy(n1.text, sizeof(n1.text), "Read only", 9);
        n = rc_uirec_inproc(buf, sizeof(buf), n1, "0/2", "0");
        buf[n ? n - 1 : 0] = '\0';
        /* checked comes from BM_GETCHECK, which out of process is a
         * marshalled call and in process is a direct one. */
        say("inproc-record-carries-the-tick",
            n > 0 && strstr(buf, "checked") != NULL, buf);
        say("inproc-record-derives-the-role",
            strstr(buf, "checkbox") != NULL, "from class and style, free");
    }

    {
        n = rc_uirec_item(buf, sizeof(buf), "AUTOEXEC.BAT", "0/3/i0", "0/3", 0);
        buf[n ? n - 1 : 0] = '\0';
        say("inproc-record-writes-a-list-row",
            n > 0 && buf[0] == 'a' && strstr(buf, "AUTOEXEC.BAT") != NULL,
            "src=a: content with no window of its own");
    }
}

/* Paths out of a depth-first stream.
 *
 * The bridge walks `parent` up the chain to build a selector, so if these
 * do not nest the ancestry is quietly lost -- no error, just worse answers
 * and a selector that cannot tell two OK buttons apart. That is exactly
 * what the first in-process reader did, which is why this is a test and
 * not a careful read.
 */
static void paths(void)
{
    static RcPathBuilder b;
    static char detail[200];
    char p[6][RC_PATH_MAX];
    char q[6][RC_PATH_MAX];
    /* A dialog with two group boxes, each holding a control: the shape
     * where a flat scheme looks right for one level and wrong for two. */
    static const long DEPTHS[6] = { 0, 1, 2, 1, 2, 2 };
    int i;

    rc_path_reset(b);
    for (i = 0; i < 6; i++)
        rc_path_next(b, DEPTHS[i], p[i], q[i]);

    rc_strf(detail, sizeof(detail), "%s %s %s %s %s %s",
            p[0], p[1], p[2], p[3], p[4], p[5]);
    say("path-nests-past-the-first-level",
        !strcmp(p[0], "0") && !strcmp(p[1], "0/0") && !strcmp(p[2], "0/0/0")
        && !strcmp(p[3], "0/1") && !strcmp(p[4], "0/1/0")
        && !strcmp(p[5], "0/1/1"), detail);

    rc_strf(detail, sizeof(detail), "%s %s %s %s %s %s",
            q[0], q[1], q[2], q[3], q[4], q[5]);
    say("path-parent-is-the-parents-path",
        !strcmp(q[0], "-") && !strcmp(q[1], "0") && !strcmp(q[2], "0/0")
        && !strcmp(q[3], "0") && !strcmp(q[4], "0/1")
        && !strcmp(q[5], "0/1"), detail);

    /* Every parent must be a path that was actually issued, or the bridge
     * looks it up and finds nothing. */
    {
        bool linked = true;
        int j, k;
        for (j = 1; j < 6; j++)
        {
            bool found = false;
            for (k = 0; k < j; k++)
                if (!strcmp(q[j], p[k]))
                    found = true;
            if (!found)
                linked = false;
        }
        say("path-every-parent-was-issued", linked,
            "the bridge walks these up; a dangling one loses the ancestry");
    }

    {
        /* A second root restarts, and does not inherit the first's
         * children. */
        static const long TWO[4] = { 0, 1, 0, 1 };
        char r[4][RC_PATH_MAX], t[4][RC_PATH_MAX];
        rc_path_reset(b);
        for (i = 0; i < 4; i++)
            rc_path_next(b, TWO[i], r[i], t[i]);
        rc_strf(detail, sizeof(detail), "%s %s %s %s", r[0], r[1], r[2],
                r[3]);
        say("path-a-second-root-starts-over",
            !strcmp(r[0], "0") && !strcmp(r[1], "0/0")
            && !strcmp(r[2], "1") && !strcmp(r[3], "1/0"), detail);
    }

    {
        /* Deeper than the cap is clamped, not overflowed. */
        char r[RC_PATH_MAX], t[RC_PATH_MAX];
        rc_path_reset(b);
        for (i = 0; i <= RC_IN_DEPTH_MAX + 4; i++)
            rc_path_next(b, (long)i, r, t);
        say("path-clamps-past-the-depth-cap",
            strlen(r) < RC_PATH_MAX && r[0] != '\0', r);
    }
}

/* ---- the step list's wire format ------------------------------------- */

/* The `input` tool's body, which nothing could reach without a socket and a
 * desktop until the handlers moved behind UiLink (REMOTE.md step 6). It is a
 * format, so the interesting inputs are the ugly ones: a value with a TAB in
 * it, one with a newline, a verb that does not exist, one step too many.
 *
 * The decode is IN PLACE, so every case needs its own writable copy -- which
 * is also the property worth asserting: a Step's strings point into the
 * caller's buffer and must still be there after the call. */
/* rc_strf returns 1 for "it fit", not a length, so appending needs its own
 * cursor. Writing `len += rc_strf(...)` advances by one byte a call and
 * silently overwrites -- which is what the first run of these cases did. */
static int append(char *out, unsigned cap, int at, const char *s)
{
    unsigned n = (unsigned)strlen(s);
    if ((unsigned)at + n + 1 > cap)
        return at;
    memcpy(out + at, s, n + 1);
    return at + (int)n;
}

static void stepCodec(void)
{
    static Step steps[RC_RUN_MAX + 2];
    static char body[8192];
    static char why[96];
    static char detail[200];
    int n;

    /* Every verb, one per line, in the table's own order. A verb that stops
     * being recognised is a list the bridge can send and the guest cannot
     * run, and the failure would be a refusal nobody could explain. */
    {
        static const char *const VERBS[] = {
            "focus", "click", "close", "kd", "ku", "bd", "bu", "mv",
            "mvr", "tap", "type", "wait", "waitfor", "waitgone",
            "waittext", "idle"
        };
        const int want = (int)(sizeof(VERBS) / sizeof(VERBS[0]));
        int len = 0, i, bad = -1;
        for (i = 0; i < want; i++)
        {
            len = append(body, sizeof(body), len, VERBS[i]);
            len = append(body, sizeof(body), len, "\n");
        }
        n = rc_ui_decode_steps(body, (unsigned)len, steps, why, sizeof(why));
        for (i = 0; i < n && i < want; i++)
            if ((int)steps[i].kind != i)
                bad = i;
        rc_strf(detail, sizeof(detail), "%d of %d, kinds in order: %s",
                n, want, bad < 0 ? "yes" : VERBS[bad]);
        say("steps-every-verb-decodes", n == want && bad < 0, detail);
    }

    /* A percent-encoded value carrying the two bytes the line format uses
     * as separators. Getting this wrong splits one step into three. */
    {
        const char *line = "type\tin=Notepad\ttext=a%09b%0Ac";
        rc_strf(body, sizeof(body), "%s\n", line);
        n = rc_ui_decode_steps(body, (unsigned)strlen(body), steps, why,
                               sizeof(why));
        bool ok = n == 1 && steps[0].kind == STEP_TYPE
               && steps[0].text != NULL
               && !strcmp(steps[0].text, "a\tb\nc")
               && steps[0].textLen == 5
               && steps[0].in != NULL && !strcmp(steps[0].in, "Notepad");
        /* The detail may NOT carry the decoded bytes: a verdict row is
         * `verdict TAB name TAB detail` and this text is a TAB and a
         * newline on purpose, so printing it raw splits one row into two
         * and the hard case count in test_uirec_agreement.py fails -- which
         * is how this was caught. */
        rc_strf(detail, sizeof(detail), "n=%d len=%u bytes=%d,%d,%d,%d,%d",
                n, n == 1 ? steps[0].textLen : 0u,
                ok ? (int)(unsigned char)steps[0].text[0] : -1,
                ok ? (int)(unsigned char)steps[0].text[1] : -1,
                ok ? (int)(unsigned char)steps[0].text[2] : -1,
                ok ? (int)(unsigned char)steps[0].text[3] : -1,
                ok ? (int)(unsigned char)steps[0].text[4] : -1);
        say("steps-tab-and-newline-survive-escaped", ok, detail);
    }

    /* The strings are BORROWED, not copied -- the whole reason Step is plain
     * data (the std::string version cost 22KB of code). So a selector read
     * back after the call must still be the caller's bytes. */
    {
        rc_strf(body, sizeof(body), "click\tsel=Button%%5Btext%%3D%%22OK%%22%%5D\n");
        n = rc_ui_decode_steps(body, (unsigned)strlen(body), steps, why,
                               sizeof(why));
        bool inside = n == 1 && steps[0].target.sel >= body
                   && steps[0].target.sel < body + sizeof(body);
        bool ok = inside && !strcmp(steps[0].target.sel, "Button[text=\"OK\"]");
        rc_strf(detail, sizeof(detail), "sel=%s, %s the caller's buffer",
                n == 1 && steps[0].target.sel ? steps[0].target.sel : "(none)",
                inside ? "inside" : "OUTSIDE");
        say("steps-strings-point-into-the-body", ok, detail);
    }

    /* A verb that does not exist is a version skew: refuse loudly rather
     * than run a different list from the one that was sent. */
    {
        rc_strf(body, sizeof(body), "focus\tsel=X\nwiggle\tsel=Y\n");
        why[0] = '\0';
        n = rc_ui_decode_steps(body, (unsigned)strlen(body), steps, why,
                               sizeof(why));
        say("steps-unknown-verb-refused",
            n < 0 && strstr(why, "unknown verb") != NULL, why);
    }

    /* RC_RUN_MAX is the bridge's own ceiling. One past it is refused with
     * the number in the reason, because "too many" alone does not say how
     * many the caller may send. */
    {
        int len = 0, i;
        for (i = 0; i < RC_RUN_MAX + 1; i++)
            len = append(body, sizeof(body), len, "wait\tms=1\n");
        why[0] = '\0';
        n = rc_ui_decode_steps(body, (unsigned)len, steps, why, sizeof(why));
        say("steps-over-the-ceiling-refused",
            n < 0 && strstr(why, "64") != NULL, why);
    }

    /* Exactly RC_RUN_MAX is not one too many. The off-by-one here would be
     * invisible until a long list arrived. */
    {
        int len = 0, i;
        for (i = 0; i < RC_RUN_MAX; i++)
            len = append(body, sizeof(body), len, "wait\tms=1\n");
        n = rc_ui_decode_steps(body, (unsigned)len, steps, why, sizeof(why));
        rc_strf(detail, sizeof(detail), "n=%d", n);
        say("steps-exactly-the-ceiling-runs", n == RC_RUN_MAX, detail);
    }

    /* Blank lines are skipped rather than counted: the bridge joins with a
     * trailing newline, so the body always ends in one. */
    {
        rc_strf(body, sizeof(body), "focus\tsel=A\n\n\nclick\tsel=B\n");
        n = rc_ui_decode_steps(body, (unsigned)strlen(body), steps, why,
                               sizeof(why));
        rc_strf(detail, sizeof(detail), "n=%d", n);
        say("steps-blank-lines-skipped", n == 2
            && steps[0].kind == STEP_FOCUS && steps[1].kind == STEP_CLICK,
            detail);
    }

    /* A field with no `=` is skipped, not fatal, and the rest of the line
     * still decodes -- the bridge validated the list, so a stray token is
     * skew to tolerate rather than a list to throw away. */
    {
        rc_strf(body, sizeof(body), "tap\tjunk\tkey=Enter\tmods=2\n");
        n = rc_ui_decode_steps(body, (unsigned)strlen(body), steps, why,
                               sizeof(why));
        bool ok = n == 1 && steps[0].key && !strcmp(steps[0].key, "Enter")
               && steps[0].mods == 2;
        rc_strf(detail, sizeof(detail), "n=%d key=%s mods=%d", n,
                n == 1 && steps[0].key ? steps[0].key : "(none)",
                n == 1 ? steps[0].mods : -1);
        say("steps-field-without-equals-skipped", ok, detail);
    }

    /* `at` and `screen` are pairs. A pair that does not parse leaves the
     * flag false rather than landing a click at 0,0 -- which is a real
     * screen position and would be a silent wrong answer. */
    {
        rc_strf(body, sizeof(body), "mv\tat=4,9\nmv\tat=nonsense\n");
        n = rc_ui_decode_steps(body, (unsigned)strlen(body), steps, why,
                               sizeof(why));
        bool ok = n == 2 && steps[0].target.hasAt
               && steps[0].target.atX == 4 && steps[0].target.atY == 9
               && !steps[1].target.hasAt;
        rc_strf(detail, sizeof(detail), "first %d,%d; second hasAt=%d",
                n == 2 ? steps[0].target.atX : -1,
                n == 2 ? steps[0].target.atY : -1,
                n == 2 ? (int)steps[1].target.hasAt : -1);
        say("steps-a-bad-pair-is-no-pair", ok, detail);
    }

    /* mods is masked to the three that exist. A wire value of 255 would
     * otherwise reach Input as a modifier set nobody can hold down. */
    {
        rc_strf(body, sizeof(body), "tap\tkey=a\tmods=255\n");
        n = rc_ui_decode_steps(body, (unsigned)strlen(body), steps, why,
                               sizeof(why));
        rc_strf(detail, sizeof(detail), "mods=%d", n == 1 ? steps[0].mods : -1);
        say("steps-mods-masked-to-three-bits", n == 1 && steps[0].mods == 7,
            detail);
    }
}

/* ---- the refusal ladder, with no desktop under it -------------------- */

/* A UiLink that records instead of sending. The handlers moved behind this
 * interface on 2026-10-02 (REMOTE.md step 6), and the refusals below are
 * the first part of them reachable without a socket and a live desktop.
 *
 * The gate and resolve cases pass a NULL UiContext on purpose. That is not
 * laziness: it PINS that each refusal is decided before anything touches the
 * desktop. If a gate ever moves after the first ctx dereference these cases
 * crash rather than quietly starting to walk windows on a box configured not
 * to be read -- which is how the clamp case below was caught asking for a
 * pid through a null pointer, and why rc_ui_base_options takes the pid
 * itself now. */
class FakeUiLink : public UiLink
{
public:
    FakeUiLink(int level) : m_level(level), m_ended(false), m_nkv(0),
                            m_reported(0), m_records(0), m_bytes(0),
                            m_stop(0)
    {
        m_status[0] = '\0';
        m_detail[0] = '\0';
    }

    virtual bool end(const char *status, const char *const *keys,
                     const char *const *vals, int nkv)
    {
        int i;
        m_ended = true;
        m_nkv = nkv;
        rc_strncpy(m_status, sizeof(m_status), status,
                   (unsigned)strlen(status));
        for (i = 0; i < nkv; i++)
            if (!strcmp(keys[i], "detail"))
                rc_strncpy(m_detail, sizeof(m_detail), vals[i],
                           (unsigned)strlen(vals[i]));
        return true;
    }
    virtual bool records(const void *, unsigned, int) { m_records++; return true; }
    virtual bool bytes(const void *, unsigned) { m_bytes++; return true; }
    virtual bool report(const void *, unsigned len)
    {
        m_reported = len;
        return true;
    }
    virtual unsigned bodyCap() const { return RC_MAX_FRAME - 2048; }
    virtual int automationLevel() const { return m_level; }
    virtual const UiLimits &limits() const { return m_limits; }
    virtual volatile LONG *stopFlag() { return &m_stop; }

    void raise() { m_stop = 1; }
    const char *status() const { return m_status; }
    const char *detail() const { return m_detail; }
    bool ended() const { return m_ended; }
    int headers() const { return m_nkv; }
    unsigned reported() const { return m_reported; }
    int recordFrames() const { return m_records; }
    int byteFrames() const { return m_bytes; }

private:
    int      m_level;
    bool     m_ended;
    int      m_nkv;
    unsigned m_reported;
    int      m_records;
    int      m_bytes;
    UiLimits m_limits;
    volatile LONG m_stop;
    char     m_status[32];
    char     m_detail[160];
};

/* A request frame, built and parsed by the real codec so the headers arrive
 * the way the wire delivers them. */
static bool mkFrame(rc_frame &f, char *raw, unsigned rawCap, const char *type,
                    const char *const *keys, const char *const *vals, int nkv,
                    const char *body)
{
    int n = rc_frame_build(raw, rawCap, type, keys, vals, nkv, body,
                           body ? (unsigned)strlen(body) : 0);
    if (n < 0)
        return false;
    /* rc_frame_build writes the length prefix too; the parser wants the
     * payload that follows it. */
    return rc_frame_parse(raw + 4, (unsigned)n - 4, &f) == 0;
}

static void refusals(void)
{
    static char raw[2048];
    static char detail[200];
    rc_frame f;
    const char *k[2];
    const char *v[2];

    /* TREE at RC_AUTOMATION_NONE. The gate is read LIVE on every request so
     * turning the capability off takes effect on the next request rather
     * than the next connection; this is the refusal that proves it is a
     * refusal and not a bridge-side omission. */
    {
        k[0] = "id"; v[0] = "u1";
        if (mkFrame(f, raw, sizeof(raw), "UITREE", k, v, 1, NULL))
        {
            FakeUiLink link(RC_AUTOMATION_NONE);
            rc_ui_tree(link, NULL, &f);
            say("tree-refused-when-automation-is-off",
                link.ended() && !strcmp(link.status(), "automation_off")
                && link.recordFrames() == 0,
                link.status());
            rc_frame_free(&f);
        }
        else
            say("tree-refused-when-automation-is-off", false, "no frame");
    }

    /* A step list at RC_AUTOMATION_SCREEN. SCREEN means "read the screen,
     * do not drive it", and a step list types -- so this is the one the
     * middle capability value exists for. No client can declare it today;
     * gremote's [ui] automation is an integer off a file and will. */
    {
        k[0] = "id"; v[0] = "u2";
        if (mkFrame(f, raw, sizeof(raw), "UIRUN", k, v, 1, "focus\tsel=X\n"))
        {
            FakeUiLink link(RC_AUTOMATION_SCREEN);
            rc_ui_run(link, NULL, &f);
            say("run-refused-at-screen-only",
                link.ended() && !strcmp(link.status(), "automation_off")
                && link.reported() == 0, link.status());
            rc_frame_free(&f);
        }
        else
            say("run-refused-at-screen-only", false, "no frame");
    }

    /* ...and a step list AT steps is not refused by the gate. Without this
     * the case above would pass with a gate that refused everything. */
    {
        k[0] = "id"; v[0] = "u3";
        if (mkFrame(f, raw, sizeof(raw), "UIRUN", k, v, 1, "wiggle\n"))
        {
            FakeUiLink link(RC_AUTOMATION_STEPS);
            rc_ui_run(link, NULL, &f);
            say("run-at-steps-passes-the-gate",
                link.ended() && !strcmp(link.status(), "bad_steps"),
                link.status());
            rc_frame_free(&f);
        }
        else
            say("run-at-steps-passes-the-gate", false, "no frame");
    }

    /* A malformed list is refused with the parser's own reason. The agent
     * reads `detail`, so a refusal that carries none is a dead end. */
    {
        k[0] = "id"; v[0] = "u4";
        if (mkFrame(f, raw, sizeof(raw), "UIRUN", k, v, 1, "wiggle\tsel=X\n"))
        {
            FakeUiLink link(RC_AUTOMATION_STEPS);
            rc_ui_run(link, NULL, &f);
            say("bad-steps-says-why",
                !strcmp(link.status(), "bad_steps") && link.detail()[0]
                && strstr(link.detail(), "unknown verb") != NULL,
                link.detail());
            rc_frame_free(&f);
        }
        else
            say("bad-steps-says-why", false, "no frame");
    }

    /* `target=` missing, and `target=` unparseable. Both are decided before
     * the resolver walks anything, which is why a NULL context is safe. */
    {
        k[0] = "id"; v[0] = "u5";
        if (mkFrame(f, raw, sizeof(raw), "UITREE", k, v, 1, NULL))
        {
            FakeUiLink link(RC_AUTOMATION_STEPS);
            WalkOptions o;
            HWND h = rc_ui_resolve_target(link, NULL, &f, o);
            say("resolve-with-no-target-refused",
                h == NULL && !strcmp(link.status(), "no_target"),
                link.status());
            rc_frame_free(&f);
        }
        else
            say("resolve-with-no-target-refused", false, "no frame");
    }

    {
        k[0] = "id";     v[0] = "u6";
        k[1] = "target"; v[1] = "Button[text=";     /* unterminated */
        if (mkFrame(f, raw, sizeof(raw), "UITREE", k, v, 2, NULL))
        {
            FakeUiLink link(RC_AUTOMATION_STEPS);
            WalkOptions o;
            HWND h = rc_ui_resolve_target(link, NULL, &f, o);
            say("resolve-with-a-bad-selector-says-why",
                h == NULL && !strcmp(link.status(), "bad_selector")
                && link.detail()[0], link.detail());
            rc_frame_free(&f);
        }
        else
            say("resolve-with-a-bad-selector-says-why", false, "no frame");
    }

    /* The stop flag reaches the WALK, not just the step list. BACKLOG 26 is
     * about a cancelled enumeration running to completion on a
     * single-threaded worker, so the next request queues behind work nobody
     * wants; this is the poll that ends it. A node is the right grain --
     * one more bounded WM_GETTEXT, not a whole walk. */
    {
        FakeUiLink link(RC_AUTOMATION_STEPS);
        TreeSink sink(link, NULL, false);
        Node n = mk("0/1", "Button", "OK", 1);
        bool first = sink.ready() && sink.onNode(n);
        link.raise();
        bool second = sink.onNode(n);
        rc_strf(detail, sizeof(detail), "first=%d second=%d stopped=%d",
                (int)first, (int)second, (int)sink.stopped);
        say("a-walk-stops-when-the-flag-goes-up",
            first && !second && sink.stopped, detail);
    }

    /* And a capture stops at a strip boundary, through the same flag and the
     * same bool ByteSink has always returned. */
    {
        FakeUiLink link(RC_AUTOMATION_STEPS);
        char junk[64];
        memset(junk, 'x', sizeof(junk));
        link.raise();
        /* ShotFrames is internal to uirpc.cpp, so what is pinned here is the
         * contract it relies on: the link hands out a flag a producer can
         * read, and the same one the walk above used. */
        say("the-stop-flag-is-the-links-to-give",
            link.stopFlag() != NULL && *link.stopFlag() != 0,
            "one flag, read by the walk, the capture and the step runner");
    }

    /* The limits come from the LINK, not from the handlers. A caller's
     * ceiling has to be what clamps a header, or gremote's [ui] section is
     * dead config -- so a request asking for more nodes than the caller
     * allows must come back at the caller's number. */
    {
        k[0] = "id";        v[0] = "u7";
        k[1] = "max_nodes"; v[1] = "999999";
        if (mkFrame(f, raw, sizeof(raw), "UITREE", k, v, 2, NULL))
        {
            FakeUiLink link(RC_AUTOMATION_STEPS);
            WalkOptions o;
            char d[64];
            memset(&o, 0, sizeof(o));
            rc_ui_base_options(o, link, GetCurrentProcessId(), &f);
            rc_strf(d, sizeof(d), "asked 999999, got %d, ceiling %d",
                    o.maxNodes, link.limits().maxNodesTop);
            say("a-header-is-clamped-to-the-links-ceiling",
                o.maxNodes == link.limits().maxNodesTop, d);
            rc_frame_free(&f);
        }
        else
            say("a-header-is-clamped-to-the-links-ceiling", false, "no frame");
    }
}

int main(int argc, char **argv)
{
    static char buf[4096];
    static char detail[160];
    unsigned n;

    if (argc > 2 && !strcmp(argv[1], "--emit"))
        return emitFixture(argv[2]);

    {
        WNDCLASSA wc;
        memset(&wc, 0, sizeof(wc));
        wc.lpfnWndProc = DefWindowProcA;
        wc.hInstance = GetModuleHandleA(NULL);
        wc.lpszClassName = "GastonInjectCheck";
        RegisterClassA(&wc);
    }

    {
        Node a = mk("0/1", "Button", "OK", 1);
        n = rc_uirec_window(buf, sizeof(buf), a, NULL);
        rc_strf(detail, sizeof(detail), "%u bytes", n);
        say("rec-writes-a-window", n > 0 && buf[0] == 'w'
            && buf[n - 1] == '\n', detail);
    }

    {
        /* A parent derived from the path, so the bridge does not have to
         * re-derive it for every record. */
        Node a = mk("0/3/1", "Button", "OK", 1);
        n = rc_uirec_window(buf, sizeof(buf), a, NULL);
        /* The record ends in a newline and this is a one-line
         * verdict row, so the terminator goes where the newline
         * was rather than after it. */
        buf[n ? n - 1 : 0] = '\0';
        say("rec-carries-the-parent-path",
            strncmp(buf, "w 0/3/1 0/3 ", 12) == 0, buf);
    }
    {
        Node a = mk("0", "#32770", "Save As", 0);
        n = rc_uirec_window(buf, sizeof(buf), a, NULL);
        /* The record ends in a newline and this is a one-line
         * verdict row, so the terminator goes where the newline
         * was rather than after it. */
        buf[n ? n - 1 : 0] = '\0';
        say("rec-root-has-no-parent", strncmp(buf, "w 0 - ", 6) == 0, buf);
    }

    {
        /* THE reason this format is length-prefixed. */
        Node a = mk("0/2", "Edit", "one\ntwo", 1001);
        n = rc_uirec_window(buf, sizeof(buf), a, NULL);
        /* The record ends in a newline and this is a one-line
         * verdict row, so the terminator goes where the newline
         * was rather than after it. */
        buf[n ? n - 1 : 0] = '\0';
        say("rec-survives-a-newline-in-the-text",
            n > 0 && strstr(buf, "one\ntwo") != NULL
            && strstr(buf, " 7 ") != NULL, "text length 7, newline intact");
    }

    {
        /* No handle, ever. A grep, because this is the rule most likely to
         * be undone by somebody adding a "useful" field. */
        Node a = mk("0/1", "Button", "OK", 1);
        a.hwnd = (HWND)0xDEADBEEF;
        a.parent = (HWND)0xFEEDFACE;
        n = rc_uirec_window(buf, sizeof(buf), a, NULL);
        /* The record ends in a newline and this is a one-line
         * verdict row, so the terminator goes where the newline
         * was rather than after it. */
        buf[n ? n - 1 : 0] = '\0';
        bool leaked = strstr(buf, "deadbeef") || strstr(buf, "DEADBEEF")
                   || strstr(buf, "3735928559");
        say("rec-never-emits-a-handle", !leaked,
            "an HWND is meaningless across a session and 9x reuses them");
    }

    {
        /* Refuse, never clamp: a short row is not detectable downstream,
         * because the lengths stop matching the bytes and the parser reads
         * the next record's fields as this one's. */
        Node a = mk("0/1", "Button", "a long enough caption to not fit", 1);
        n = rc_uirec_window(buf, 40, a, NULL);
        say("rec-refuses-rather-than-truncating", n == 0,
            "a 40-byte cap against a record that needs more");
        n = rc_uirec_window(buf, 0, a, NULL);
        say("rec-refuses-a-zero-cap", n == 0, "cap 0");
    }

    {
        /* Text that filled the fixed buffer exactly, so there is no
         * terminator to find. strlen here would run into the next member. */
        Node a = mk("0/1", "Edit", "", 1);
        memset(a.text, 'x', sizeof(a.text));
        n = rc_uirec_window(buf, sizeof(buf), a, NULL);
        rc_strf(detail, sizeof(detail), "%u bytes for a full %u-byte field",
                n, (unsigned)sizeof(a.text));
        say("rec-handles-an-unterminated-field", n > 0, detail);
    }

    {
        AccNode g;
        memset(&g, 0, sizeof(g));
        g.childId = 7;
        rc_strncpy(g.role, sizeof(g.role), "link", 4);
        rc_strncpy(g.name, sizeof(g.name), "First link", 10);
        n = rc_uirec_acc(buf, sizeof(buf), g, "0/4/7", "0/4");
        buf[n ? n - 1 : 0] = '\0';
        say("rec-writes-an-accessible-element",
            n > 0 && buf[0] == 'a' && strstr(buf, "First link") != NULL,
            "src=a, and a child id where a window has a control id");
    }

    injection();
    inprocRecords();
    paths();
    stepCodec();
    refusals();
    return 0;
}
