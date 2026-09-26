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

int main(int argc, char **argv)
{
    static char buf[4096];
    static char detail[160];
    unsigned n;

    if (argc > 2 && !strcmp(argv[1], "--emit"))
        return emitFixture(argv[2]);

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
        buf[n] = '\0';
        say("rec-carries-the-parent-path",
            strncmp(buf, "w 0/3/1 0/3 ", 12) == 0, buf);
    }
    {
        Node a = mk("0", "#32770", "Save As", 0);
        n = rc_uirec_window(buf, sizeof(buf), a, NULL);
        buf[n] = '\0';
        say("rec-root-has-no-parent", strncmp(buf, "w 0 - ", 6) == 0, buf);
    }

    {
        /* THE reason this format is length-prefixed. */
        Node a = mk("0/2", "Edit", "one\ntwo", 1001);
        n = rc_uirec_window(buf, sizeof(buf), a, NULL);
        buf[n] = '\0';
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
        buf[n] = '\0';
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
        buf[n] = '\0';
        say("rec-writes-an-accessible-element",
            n > 0 && buf[0] == 'a' && strstr(buf, "First link") != NULL,
            "src=a, and a child id where a window has a control id");
    }

    return 0;
}
