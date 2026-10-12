/* Does libview turn records into the markup the bridge used to send?
 *
 * Runs the real win/view/render.cpp against records it builds here, and
 * prints one `verdict<TAB>case<TAB>detail` row per case for
 * tests/python/guest/test_guest_render.py to diff.
 *
 * Three modes, and the two that are not the default exist for the rest of
 * the stage rather than for this file:
 *
 *   (no args)          the cases below
 *   --render           records on stdin, markup on stdout. B2 feeds the
 *                      same records to this and to the bridge's emitter and
 *                      compares BYTE FOR BYTE, which is what turns "the
 *                      guest emits the same HTML MSHTML gets today" from an
 *                      intention into a fact -- and what makes regenerating
 *                      the frame goldens safe.
 *   --dump-templates   key<TAB>value for every loaded template. The guest
 *                      gets to disagree with Wine about what came back out
 *                      of GetPrivateProfileStringA before any of the above
 *                      is believed. win/core/inifile.cpp:50-107 is 58 lines
 *                      about why that is not paranoia.
 *
 * --template <path> points the loader at a directory; without it the
 * templates are the compiled-in fallback, which is itself one of the cases.
 */
#include <windows.h>
#include <fcntl.h>
#include <io.h>
#include <stdio.h>
#include <string.h>

#include "core/bytesink.h"
#include "core/str.h"
#include "view/render.h"

static int g_failures;

static void say(const char *name, bool pass, const char *detail)
{
    printf("%s\t%s\t%s\n", pass ? "pass" : "FAIL", name, detail);
    if (!pass)
        g_failures++;
}

/* A sink that keeps what it was given, so a case can compare it. Capped,
 * and the cap is a case of its own: a producer that ignores a false from
 * write() is the bug ByteSink exists to make visible.
 *
 * The buffer is on the HEAP and not a member array. As a member it was 32KB
 * of stack per instance, and `expect()` holds one -- six times the
 * stack-frame gate's limit, in a check binary whose whole job is to be the
 * place those gates run. */
class StringSink : public ByteSink
{
public:
    StringSink() : m_buf(0), m_len(0), m_cap(0), m_stopAt(0), m_stopped(false)
    {
        m_cap = 32768;
        m_buf = (char *)HeapAlloc(GetProcessHeap(), 0, m_cap);
        if (m_buf)
            m_buf[0] = '\0';
    }

    ~StringSink()
    {
        if (m_buf)
            HeapFree(GetProcessHeap(), 0, m_buf);
    }

    void stopAfter(unsigned n) { m_stopAt = n; }

    bool write(const void *data, unsigned len)
    {
        if (!m_buf)
            return false;
        if (m_stopAt && m_len + len > m_stopAt)
        {
            m_stopped = true;
            return false;
        }
        if (m_len + len >= m_cap)
        {
            m_stopped = true;
            return false;
        }
        memcpy(m_buf + m_len, data, len);
        m_len += len;
        m_buf[m_len] = '\0';
        return true;
    }

    const char *str() const { return m_buf ? m_buf : ""; }
    unsigned len() const { return m_len; }
    bool stopped() const { return m_stopped; }

private:
    StringSink(const StringSink &);
    StringSink &operator=(const StringSink &);

    char    *m_buf;
    unsigned m_len;
    unsigned m_cap;
    unsigned m_stopAt;
    bool     m_stopped;
};

class StdoutSink : public ByteSink
{
public:
    bool write(const void *data, unsigned len)
    {
        return fwrite(data, 1, len, stdout) == len;
    }
};

/* --- the cases ---------------------------------------------------------- */

static void expect(const char *name, const char *record, const char *want,
                   unsigned wrapcol = 56)
{
    StringSink out;
    char detail[600];
    bool ok = rc_render(record, (unsigned)strlen(record), wrapcol, out);
    if (!ok)
    {
        say(name, false, "rc_render refused the record");
        return;
    }
    if (strcmp(out.str(), want) == 0)
    {
        say(name, true, "");
        return;
    }
    rc_strf(detail, sizeof detail, "want [%.240s] got [%.240s]", want,
            out.str());
    say(name, false, detail);
}

static void expectRefused(const char *name, const char *record)
{
    StringSink out;
    bool ok = rc_render(record, (unsigned)strlen(record), 56, out);
    say(name, !ok, ok ? "it was accepted" : "");
}

/* --- one per kind ------------------------------------------------------- */

static void caseKinds()
{
    expect("para", "para\nt hello\n.\n", "<p>hello</p>");
    expect("para.two.lines", "para\nt a\nt b\n.\n", "<p>a<br>b</p>");
    expect("para.empty.line", "para\nt a\nt\nt b\n.\n",
           "<p>a<br><br>b</p>");
    expect("heading", "heading level=3\nt H\n.\n", "<h3>H</h3>");
    expect("heading.six", "heading level=6\nt D\n.\n", "<h6>D</h6>");
    expect("quote", "quote\nt q\nt r\n.\n",
           "<blockquote>q<br>r</blockquote>");
    expect("rule", "rule\n.\n", "<hr>");
    expect("bullet", "bullet\nt a\nt b\n.\n",
           "<ul><li>a</li><li>b</li></ul>");
    expect("numbered", "numbered\nt a\n.\n", "<ol><li>a</li></ol>");
    expect("code", "code\nt int x;\nt return 0;\n.\n",
           "<div class=\"codewrap\"><pre>int x;\nreturn 0;</pre></div>");
    expect("code.lang", "code\nl c\nt int x;\n.\n",
           "<div class=\"codewrap\"><div class=\"lang\">c</div>"
           "<pre>int x;</pre></div>");
    expect("table", "table header=1\nr\nc a\nc b\nr\nc 1\nc 2\n.\n",
           "<table><tr><th>a</th><th>b</th></tr>"
           "<tr><td>1</td><td>2</td></tr></table>");
    expect("table.headerless", "table header=0\nr\nc x\n.\n",
           "<table><tr><td>x</td></tr></table>");
    expect("user", "user\nt a\nt\nt b\n.\n",
           "<div class=\"you\">a<br><br>b</div>");
    expect("notice", "notice\nt n\n.\n", "<div class=\"notice\">n</div>");
    expect("error", "error\nt e\n.\n", "<div class=\"err\">e</div>");
    expect("thinking", "thinking\nt a\nt b\n.\n",
           "<div class=\"thinking\">a\nb</div>");
    expect("tool", "tool\nn exec\nt dir C:\\WORK\n.\n",
           "<div class=\"tool\"><span class=\"toolname\">exec</span> "
           "dir C:\\WORK</div>");
    expect("toolresult", "toolresult error=0\nt out\n.\n",
           "<div class=\"toolresult\"><pre>out</pre></div>");
    expect("toolresult.err", "toolresult error=1\nt bad\n.\n",
           "<div class=\"toolresult err\"><pre>bad</pre></div>");
    expect("toolresult.clipped", "toolresult clipped=7 error=0\nt x\n.\n",
           "<div class=\"toolresult\"><pre>x</pre>"
           "<div class=\"clip\">-- shown to you in full; 7 more "
           "characters were sent to the model --</div></div>");
    expect("clip", "clip dropped=12\n.\n",
           "<div class=\"clip\">-- shown to you in full; 12 more "
           "characters were sent to the model --</div>");
    expect("exportheading", "exportheading\nh Title\nt when\n.\n",
           "<div class=\"exporthead\"><h1>Title</h1>"
           "<div class=\"notice\">when</div></div>");
    expect("exportheading.no.when", "exportheading\nh T\n.\n",
           "<div class=\"exporthead\"><h1>T</h1></div>");
}

/* --- escaping, all three modes ------------------------------------------ */

static void caseEscaping()
{
    /* THREE characters in record content, not four. The bridge's esc is
     * html.escape(quote=False) and user_turn('say "hi"') emits a raw quote
     * today. A renderer escaping the double quote would put &quot; where
     * the oracle puts ", in the one place none of the bridge's five
     * escaping tests looks. */
    expect("esc.three", "para\nt a & b < c > d\n.\n",
           "<p>a &amp; b &lt; c &gt; d</p>");
    expect("esc.quote.is.raw", "para\nt say \"hi\"\n.\n",
           "<p>say \"hi\"</p>");
    expect("esc.in.code", "code\nt a<b>&c\n.\n",
           "<div class=\"codewrap\"><pre>a&lt;b&gt;&amp;c</pre></div>");
    expect("esc.in.cell", "table header=0\nr\nc <x>\n.\n",
           "<table><tr><td>&lt;x&gt;</td></tr></table>");
    /* Already-escaped text must not be escaped twice. */
    expect("esc.not.twice", "para\nt &amp;\n.\n", "<p>&amp;amp;</p>");
}

/* --- newlines ----------------------------------------------------------- */

static void caseNewlines()
{
    expect("nl.prose.is.br", "para\nt a\nt b\n.\n", "<p>a<br>b</p>");
    expect("nl.literal.in.pre", "code\nt a\nt b\n.\n",
           "<div class=\"codewrap\"><pre>a\nb</pre></div>");
    /* \r travels. HtmlStream.feed splits with str.splitlines(), which
     * splits on it, so a lone \r is already a line break on the far side;
     * one that survives into a payload is in the unterminated tail, where
     * today's HTML carries it raw. An earlier draft of the plan had this
     * backwards and cited the wrong function. B2 would have caught it. */
    expect("nl.cr.travels", "para\nt a\rb\n.\n", "<p>a\rb</p>");
    expect("nl.tab.travels", "code\nt a\tb\n.\n",
           "<div class=\"codewrap\"><pre>a\tb</pre></div>");
}

/* --- the inline stack --------------------------------------------------- */

static void caseSpans()
{
    expect("span.code", "para\nt \x01" "ca\x02\n.\n",
           "<p><code>a</code></p>");
    expect("span.strong", "para\nt \x01" "sa\x02\n.\n", "<p><b>a</b></p>");
    expect("span.em", "para\nt \x01" "ea\x02\n.\n", "<p><i>a</i></p>");
    /* Depth three, the case that killed the flat 3-bit encoding. */
    expect("span.depth.three",
           "para\nt \x01" "sa \x01" "eb \x01" "cc\x02 d\x02 e\x02\n.\n",
           "<p><b>a <i>b <code>c</code> d</i> e</b></p>");
    /* A kind nests inside itself, which is why the stack cannot be one
     * slot per kind. */
    expect("span.same.kind.nested",
           "para\nt \x01" "ea \x01" "eb\x02 c\x02\n.\n",
           "<p><i>a <i>b</i> c</i></p>");
    /* Two adjacent runs, which a flat per-character state cannot tell from
     * one run twice as long. */
    expect("span.adjacent.same",
           "para\nt \x01" "sa\x02\x01" "sb\x02\n.\n",
           "<p><b>a</b><b>b</b></p>");
    /* An unclosed run is closed rather than left dangling: a LIVE update
     * carries the open block mid-write, and an unbalanced <b> leaks the
     * styling into everything appended after it. */
    expect("span.unclosed.is.closed", "para\nt \x01" "sa\n.\n",
           "<p><b>a</b></p>");
    /* A close with nothing open is dropped, not shown. */
    expect("span.stray.close", "para\nt a\x02" "b\n.\n", "<p>ab</p>");
    /* An open with no letter likewise. */
    expect("span.stray.open", "para\nt a\x01" "b\n.\n", "<p>ab</p>");
    expect("span.escapes.inside", "para\nt \x01" "s<x>\x02\n.\n",
           "<p><b>&lt;x&gt;</b></p>");
}

/* --- the wrap ------------------------------------------------------------ */

static void caseWrap()
{
    /* Only a literal block wraps. Prose is the browser's job. */
    expect("wrap.long.breaks.at.a.space",
           "toolresult error=0\nt "
           "aaaaaaaaaa bbbbbbbbbb cccccccccc dddddddddd eeeeeeeeee ffffff\n"
           ".\n",
           "<div class=\"toolresult\"><pre>"
           "aaaaaaaaaa bbbbbbbbbb cccccccccc dddddddddd eeeeeeeeee\n"
           "ffffff</pre></div>");
    expect("wrap.unbreakable.breaks.anyway",
           "toolresult error=0\nt "
           "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
           ".\n",
           "<div class=\"toolresult\"><pre>"
           "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
           "xxxx</pre></div>");
    expect("wrap.short.untouched", "toolresult error=0\nt short\n.\n",
           "<div class=\"toolresult\"><pre>short</pre></div>");
    /* NEW BEHAVIOUR WITH NO ORACLE, and the reason it has a case: the
     * bridge's helper HUNG on width 0 -- rfind(" ", 0, 0) is -1, the cut
     * lands at 0 and the line never advances. The bridge could not reach
     * it because nothing passed 0; the guest can, because wrapcol comes
     * from a window. The bridge helper gained the same guard. */
    expect("wrap.zero.does.not.wrap",
           "toolresult error=0\nt "
           "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
           ".\n",
           "<div class=\"toolresult\"><pre>"
           "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
           "</pre></div>", 0);
    expect("wrap.escapes.after.breaking",
           "toolresult error=0\nt <xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
           "xxxxxxxxxxxxxxxxxxxx>\n.\n",
           "<div class=\"toolresult\"><pre>"
           "&lt;xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
           "x&gt;</pre></div>");
}

/* --- the permission card -------------------------------------------------- */

static void casePermission()
{
    expect("perm.ask",
           "permission settleable=1\nh ask\nb delete\nd C:\\X\n.\n",
           "<div class=\"perm\" id=\"perm\">"
           "<div class=\"permhead\">Permission required</div>"
           "<div class=\"permdonehead\">Answered</div>"
           "<p><b>delete</b> &mdash; C:\\X</p></div>");
    expect("perm.no.description",
           "permission settleable=1\nh ask\nb delete\n.\n",
           "<div class=\"perm\" id=\"perm\">"
           "<div class=\"permhead\">Permission required</div>"
           "<div class=\"permdonehead\">Answered</div>"
           "<p><b>delete</b></p></div>");
    expect("perm.view.is.not.settleable",
           "permission settleable=0\nh view\nb Manual\n.\n",
           "<div class=\"perm\">"
           "<div class=\"permhead\">What can run without asking</div>"
           "<p><b>Manual</b></p></div>");
    expect("perm.warning.and.preview",
           "permission settleable=1\nh ask\nw careful\np 3 files\n.\n",
           "<div class=\"perm\" id=\"perm\">"
           "<div class=\"permhead\">Permission required</div>"
           "<div class=\"permdonehead\">Answered</div>"
           "<p class=\"permwarn\"><b>Check this one:</b> careful</p>"
           "<p><b>What it touches:</b> 3 files</p></div>");
    expect("perm.steps.then.table",
           "permission settleable=1\ns one\ns two\nk path\nv C:\\X\n.\n",
           "<div class=\"perm\" id=\"perm\">"
           "<ol><li>one</li><li>two</li></ol>"
           "<table><tr><th>path</th><td>C:\\X</td></tr></table></div>");
    /* Order is meaningful: the request puts its table last and the rules
     * view puts its paragraphs after it, so emitting by tag in a fixed
     * order gets one of the two wrong. */
    expect("perm.table.then.paragraphs",
           "permission settleable=0\nk a\nv 1\nt after\n.\n",
           "<div class=\"perm\">"
           "<table><tr><th>a</th><td>1</td></tr></table>"
           "<p>after</p></div>");
    expect("perm.unknown.head.is.absent",
           "permission settleable=1\nh future\nb x\n.\n",
           "<div class=\"perm\" id=\"perm\"><p><b>x</b></p></div>");
}

/* --- refusals and malformed records --------------------------------------- */

static void caseRefusals()
{
    expectRefused("refuse.no.terminator", "para\nt hello\n");
    expectRefused("refuse.truncated.head", "para");
    expectRefused("refuse.attr.without.value", "heading level\nt x\n.\n");
    expectRefused("refuse.empty.kind", " level=1\n.\n");

    /* An UNKNOWN KIND is not a refusal: it means a bridge newer than this
     * client, and a transcript missing one block beats a transcript that
     * stopped. It renders as nothing and says so in the log. */
    expect("unknown.kind.renders.nothing", "marquee\nt x\n.\n", "");

    /* Two whole records in one body, which is what pack() produces. */
    expect("two.records", "para\nt a\n.\npara\nt b\n.\n",
           "<p>a</p><p>b</p>");
    /* Zero is real: an empty LIVE body is what clears the open slot. */
    expect("empty.body", "", "");
}

static void caseCounting()
{
    const char *two = "para\nt a\n.\nrule\n.\n";
    char detail[120];
    int n = rc_render_count(two, (unsigned)strlen(two));
    rc_strf(detail, sizeof detail, "counted %d", n);
    say("count.two", n == 2, detail);
    say("count.zero", rc_render_count("", 0) == 0, "");
    say("count.malformed", rc_render_count("para\nt a\n", 9) == -1, "");
}

/* --- the sink can stop the producer --------------------------------------- */

static void caseSinkStops()
{
    StringSink out;
    const char *rec = "para\nt aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n.\n";
    out.stopAfter(8);
    bool ok = rc_render(rec, (unsigned)strlen(rec), 56, out);
    say("sink.false.stops.the.render", !ok && out.stopped(),
        ok ? "it ran to completion" : "");
}

/* --- the same record into two sinks --------------------------------------- */

static void caseTwoSinks()
{
    StringSink a, b;
    const char *rec = "code\nl c\nt int x;\nt <y>\n.\n";
    rc_render(rec, (unsigned)strlen(rec), 56, a);
    rc_render(rec, (unsigned)strlen(rec), 56, b);
    say("two.sinks.agree", strcmp(a.str(), b.str()) == 0, a.str());
}

/* --- the template layer ---------------------------------------------------- */

static void caseTemplates(const char *dir)
{
    char err[256];
    char detail[400];

    if (!dir)
    {
        say("tpl.absent.degrades", rc_tpl_degraded(),
            "no --template was given and the renderer is not degraded");
        return;
    }

    bool ok = rc_tpl_load(dir, err, sizeof err);
    rc_strf(detail, sizeof detail, "%s", ok ? "" : err);
    say("tpl.load", ok, detail);
    say("tpl.not.degraded.after.load", ok && !rc_tpl_degraded(), "");

    rc_strf(detail, sizeof detail, "version %d, want %d", rc_tpl_version(),
            RC_VIEW_VERSION);
    say("tpl.version", rc_tpl_version() == RC_VIEW_VERSION, detail);

    /* A repeated {name} hole: {level} is in the prefix and the suffix. */
    expect("tpl.repeated.name.hole", "heading level=4\nt H\n.\n",
           "<h4>H</h4>");

    /* A missing directory degrades rather than refusing. */
    {
        bool bad = rc_tpl_load("C:\\NO\\SUCH\\PLACE", err, sizeof err);
        say("tpl.missing.file.is.reported", !bad && err[0] != '\0', err);
        say("tpl.missing.file.degrades", rc_tpl_degraded(), "");
        expect("tpl.degraded.still.renders", "para\nt a & b\n.\n",
               "<p>a &amp; b</p>");
        expect("tpl.degraded.list.is.still.a.list", "bullet\nt a\n.\n",
               "<p><li>a</li></p>");
        /* And back, so the cases after this one see real templates. */
        rc_tpl_load(dir, err, sizeof err);
    }
}

int main(int argc, char **argv)
{
    const char *dir = 0;
    bool doRender = false, doDump = false;
    int i;

    for (i = 1; i < argc; i++)
    {
        if (strcmp(argv[i], "--template") == 0 && i + 1 < argc)
            dir = argv[++i];
        else if (strcmp(argv[i], "--render") == 0)
            doRender = true;
        else if (strcmp(argv[i], "--dump-templates") == 0)
            doDump = true;
        else
        {
            fprintf(stderr, "usage: rendercheck [--template DIR] "
                            "[--render [--wrapcol N]] [--dump-templates]\n");
            return 2;
        }
    }

    if (dir)
    {
        char err[256];
        if (!rc_tpl_load(dir, err, sizeof err) && (doRender || doDump))
        {
            fprintf(stderr, "rendercheck: %s\n", err);
            return 2;
        }
    }

    /* BINARY, and this is not a detail: the C runtime opens stdout in text
     * mode and turns every \n the renderer writes into \r\n. The first run
     * of the equality proof reported four mismatches that were all this --
     * the proof comparing the runtime's line endings rather than the
     * renderer's output. Worth the two lines and the comment, because the
     * failure looks exactly like a renderer bug. */
    if (doDump || doRender)
        _setmode(_fileno(stdout), _O_BINARY);

    if (doDump)
    {
        StdoutSink out;
        return rc_tpl_dump(out) ? 0 : 1;
    }

    if (doRender)
    {
        /* Records on stdin, markup on stdout. Read whole: a record is
         * bounded and the caller is a test, so streaming would buy nothing
         * and lose the ability to refuse a truncated body.
         *
         * On the HEAP. As `static char buf[1 << 20]` this was a megabyte of
         * BSS -- committed at load, in the one binary whose static-
         * allocation gate exists to catch exactly that, and it failed it on
         * the first run. */
        const unsigned cap = 1 << 20;
        char *buf = (char *)HeapAlloc(GetProcessHeap(), 0, cap);
        unsigned n;
        StdoutSink out;
        int rc;
        if (!buf)
        {
            fprintf(stderr, "rendercheck: no memory for stdin\n");
            return 2;
        }
        n = (unsigned)fread(buf, 1, cap, stdin);
        rc = rc_render(buf, n, 56, out) ? 0 : 1;
        HeapFree(GetProcessHeap(), 0, buf);
        return rc;
    }

    /* Through a table, not a dozen calls in a row: Watcom inlines a static
     * called once, and inlining all of these would put every case's locals
     * in main's frame at the same time. That is fscheck.cpp:1053-1058's
     * 13,268-byte frame, which steps over the guard page on the guest once
     * the stack has grown past its commit. An indirect call cannot be
     * inlined. */
    {
        static void (* const cases[])() = {
            caseKinds, caseEscaping, caseNewlines, caseSpans, caseWrap,
            casePermission, caseRefusals, caseCounting, caseSinkStops,
            caseTwoSinks
        };
        unsigned k;
        caseTemplates(dir);
        for (k = 0; k < sizeof cases / sizeof cases[0]; k++)
            cases[k]();
    }

    printf("%s\tsummary\t%d failures\n", g_failures ? "FAIL" : "pass",
           g_failures);
    return g_failures ? 1 : 0;
}
