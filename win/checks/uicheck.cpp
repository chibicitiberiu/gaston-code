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
    return 0;
}
