/* One record in, markup out. See view/render.h for the format.
 *
 * ONE TRANSLATION UNIT with the substituter beside it (templates.cpp is the
 * second and last), because -xs costs about 2,144 bytes per TU and
 * docs/PROGRESS.md:4012-4018 is the precedent for absorbing a file rather
 * than paying that twice for tidiness.
 *
 * The parser uses the cursor-by-reference idiom of
 * win/automation/select.cpp:13-53, and dispatches through a function-pointer
 * table keyed by kind. The table is not style: Watcom inlines a static
 * function called once, and inlining a dozen of these would put every
 * emitter's locals in one frame at the same time -- which is
 * win/checks/fscheck.cpp:1053-1058's 13,268-byte frame, caught by
 * stackframes.py. An indirect call cannot be inlined. It is also POD, so it
 * clears the static-constructor gate.
 *
 * NO OUTPUT GOES IN A STACK BUFFER. Everything streams into the ByteSink,
 * which is what keeps this renderer's frames flat whatever a model emits.
 * The two exceptions are bounded and stated where they are: one line buffer
 * at RC_LINE_MAX, and the small value strings a {name} hole takes.
 */
#include "view/render.h"
#include "view/templates.h"

#include "core/log.h"
#include "core/str.h"

#include <stdlib.h>
#include <string.h>

/* --- escaping ----------------------------------------------------------
 *
 * THREE characters, not four. The bridge's esc is html.escape(quote=False)
 * and user_turn('say "hi"') emits a raw quote today -- verified against the
 * running code. A renderer that escaped the double quote would produce
 * &quot; where the oracle produces ", in the one place none of the bridge's
 * five escaping tests looks. B2 compares byte for byte and would catch it;
 * this comment is so nobody "fixes" it first.
 *
 * win/app/htmlview.cpp's esc_into escapes FOUR, and that difference is
 * correct: it escapes text the guest composed itself, which never passes
 * through a record. Its comment saying the two escapers never see the same
 * string stays true after this stage, and gets sharper rather than wronger.
 */
static bool escInto(const char *s, unsigned n, ByteSink &out)
{
    unsigned i, run = 0;
    for (i = 0; i < n; i++)
    {
        const char *rep = 0;
        switch (s[i])
        {
        case '&': rep = "&amp;"; break;
        case '<': rep = "&lt;";  break;
        case '>': rep = "&gt;";  break;
        default:  run++; continue;
        }
        if (run && !out.write(s + i - run, run))
            return false;
        run = 0;
        if (!out.write(rep, (unsigned)strlen(rep)))
            return false;
    }
    return run == 0 || out.write(s + n - run, run);
}

/* --- inline runs -------------------------------------------------------
 *
 * \x01 plus a letter opens, \x02 closes the innermost. A REAL STACK, not a
 * bitmask and not one slot per kind, because a kind nests inside itself:
 * "_a *b* c_" is <i>a <i>b</i> c</i>.
 */
static const char *spanTag(char letter)
{
    switch (letter)
    {
    case 'c': return "code";
    case 's': return "b";
    case 'e': return "i";
    }
    return 0;
}

static bool openTag(const char *tag, ByteSink &out)
{
    return out.write("<", 1) && out.write(tag, (unsigned)strlen(tag))
        && out.write(">", 1);
}

static bool closeTag(const char *tag, ByteSink &out)
{
    return out.write("</", 2) && out.write(tag, (unsigned)strlen(tag))
        && out.write(">", 1);
}

/* One marked line, escaped, with its runs as elements. */
static bool spansInto(const char *s, unsigned n, ByteSink &out)
{
    const char *stack[RC_SPAN_DEPTH];
    unsigned depth = 0;
    unsigned i = 0, run = 0;

    while (i < n)
    {
        char c = s[i];
        if (c == '\x01' && i + 1 < n && spanTag(s[i + 1]))
        {
            if (run && !escInto(s + i - run, run, out))
                return false;
            run = 0;
            if (depth < RC_SPAN_DEPTH)
            {
                /* The bound is CHECKED, not assumed. Three is structural on
                 * the far side; past four the run is simply not opened, so
                 * a malformed record costs styling and never the stack. */
                stack[depth] = spanTag(s[i + 1]);
                if (!openTag(stack[depth], out))
                    return false;
                depth++;
            }
            i += 2;
            continue;
        }
        if (c == '\x02')
        {
            if (run && !escInto(s + i - run, run, out))
                return false;
            run = 0;
            if (depth)
            {
                depth--;
                if (!closeTag(stack[depth], out))
                    return false;
            }
            i++;
            continue;
        }
        if (c == '\x01')
        {
            /* An open with no letter, or a letter nothing knows. Dropped
             * rather than shown: it cannot come from the parser on the far
             * side, so it is a record built by hand or cut in the wrong
             * place, and a stray control byte in the document is worse
             * than a missing emphasis. */
            if (run && !escInto(s + i - run, run, out))
                return false;
            run = 0;
            i++;
            continue;
        }
        run++;
        i++;
    }
    if (run && !escInto(s + n - run, run, out))
        return false;
    /* Anything still open is closed here. A LIVE update carries the open
     * block mid-write, so a truncated run is normal; handing MSHTML an
     * unbalanced <b> leaks the styling into everything appended after it. */
    while (depth)
    {
        depth--;
        if (!closeTag(stack[depth], out))
            return false;
    }
    return true;
}

/* --- the long-line wrap ------------------------------------------------
 *
 * The bridge did this and does not any more: the client knows its window
 * width at runtime and 56 was measured in a 640x480 default that a resize
 * makes wrong. IE5 has neither pre-wrap nor word-wrap, so a <pre> holding
 * one long line cannot wrap at all -- and overflow:auto then draws a
 * horizontal scrollbar inside a box one line tall, on top of the only text
 * there is. Observed on the guest as a block that was nothing but a
 * scrollbar.
 *
 * Breaks at a space in the last third of the line so prose reads normally,
 * hard-breaks otherwise, because a 300-character path with no spaces in it
 * is the case that started this.
 *
 * width 0 means do not wrap, and it is a REFUSAL rather than a loop: with
 * width 0 the "find a space" search is over an empty range, the cut lands
 * at 0 and the line never advances. The bridge helper has the same guard
 * for the same reason.
 */
static bool wrapInto(const char *s, unsigned n, unsigned width, ByteSink &out)
{
    unsigned start = 0;

    if (width == 0)
        return escInto(s, n, out);

    while (start < n)
    {
        unsigned lineEnd = start;
        unsigned cut;
        while (lineEnd < n && s[lineEnd] != '\n')
            lineEnd++;

        while (lineEnd - start > width)
        {
            unsigned lo = start + width * 2 / 3;
            unsigned at = 0;
            unsigned j;
            for (j = start + width; j > lo; j--)
                if (s[j - 1] == ' ')
                {
                    at = j - 1;
                    break;
                }
            if (at == 0 || at <= start)
            {
                cut = start + width;
                if (!escInto(s + start, cut - start, out))
                    return false;
            }
            else
            {
                cut = at;
                if (!escInto(s + start, cut - start, out))
                    return false;
                cut++;          /* the space is consumed by the break */
            }
            if (!out.write("\n", 1))
                return false;
            start = cut;
        }
        if (!escInto(s + start, lineEnd - start, out))
            return false;
        if (lineEnd < n)
        {
            if (!out.write("\n", 1))
                return false;
            lineEnd++;
        }
        start = lineEnd;
    }
    return true;
}

/* --- the parsed record -------------------------------------------------
 *
 * A CURSOR, not a copy, and not an array of lines either. The first shape
 * this was written in held `Line line[2048]` so emitters could index the
 * payload: 24 KB, which is over the stack-frame gate as a local and more
 * than ten times tooldet.cpp's "2KB of BSS is a real cost" as a static. The
 * body is already in memory and the whole point of rendering per record is
 * not to hold a second copy of it.
 *
 * So the record is a kind, up to four attributes, and the bounds of its
 * payload. Emitters walk it with LineIter, which costs sixteen bytes and
 * nothing per line. The two that need to look one line ahead -- b/d and
 * k/v in the permission card -- copy the iterator, which is a struct
 * assignment.
 */
#define MAX_ATTRS 4

struct Attr
{
    const char *name;
    unsigned    nameLen;
    long        value;
};

struct Record
{
    const char *kind;
    unsigned    kindLen;
    Attr        attr[MAX_ATTRS];
    unsigned    attrN;
    const char *body;      /* first payload line */
    const char *bodyEnd;   /* the '.' line */
};

struct LineIter
{
    const char *p;
    const char *end;
    char        tag;
    const char *text;
    unsigned    len;

    void start(const Record &r) { p = r.body; end = r.bodyEnd; }

    bool next()
    {
        const char *lineEnd;
        if (p >= end)
            return false;
        lineEnd = p;
        while (lineEnd < end && *lineEnd != '\n')
            lineEnd++;
        tag = *p;
        /* A tag alone is a line with no payload, and that is load-bearing:
         * a blank line in a pasted message is an empty `t`, and without it
         * the blank lines vanish. "t x" loses its one separating space. */
        if (lineEnd - p >= 2)
        {
            text = p + 2;
            len = (unsigned)(lineEnd - p - 2);
        }
        else
        {
            text = "";
            len = 0;
        }
        p = lineEnd < end ? lineEnd + 1 : end;
        return true;
    }
};

static bool kindIs(const Record &r, const char *name)
{
    unsigned n = (unsigned)strlen(name);
    return r.kindLen == n && memcmp(r.kind, name, n) == 0;
}

static long attrOf(const Record &r, const char *name, long def)
{
    unsigned n = (unsigned)strlen(name);
    unsigned i;
    for (i = 0; i < r.attrN; i++)
        if (r.attr[i].nameLen == n && memcmp(r.attr[i].name, name, n) == 0)
            return r.attr[i].value;
    return def;
}

/* The text of the first line with this tag, or "". One scan, no storage. */
static const char *firstOf(const Record &r, char tag, unsigned *len)
{
    LineIter it;
    it.start(r);
    while (it.next())
        if (it.tag == tag)
        {
            *len = it.len;
            return it.text;
        }
    *len = 0;
    return "";
}

/* --- parsing -----------------------------------------------------------  */

static void skipTo(const char *&p, const char *end, char c)
{
    while (p < end && *p != c)
        p++;
}

static bool parseHead(const char *&p, const char *end, Record &r)
{
    const char *lineEnd = p;
    skipTo(lineEnd, end, '\n');
    if (lineEnd == end)
        return false;                     /* no terminator: refuse */
    if ((unsigned)(lineEnd - p) > 128)
        return false;

    r.kind = p;
    while (p < lineEnd && *p != ' ')
        p++;
    r.kindLen = (unsigned)(p - r.kind);
    if (!r.kindLen)
        return false;

    r.attrN = 0;
    while (p < lineEnd)
    {
        const char *name;
        const char *eq;
        char num[16];
        unsigned nlen;
        while (p < lineEnd && *p == ' ')
            p++;
        if (p >= lineEnd)
            break;
        name = p;
        while (p < lineEnd && *p != '=' && *p != ' ')
            p++;
        if (p >= lineEnd || *p != '=')
            return false;
        eq = p++;
        if (r.attrN >= MAX_ATTRS)
            return false;
        r.attr[r.attrN].name = name;
        r.attr[r.attrN].nameLen = (unsigned)(eq - name);
        nlen = 0;
        while (p < lineEnd && *p != ' ' && nlen + 1 < sizeof num)
            num[nlen++] = *p++;
        num[nlen] = '\0';
        if (!nlen)
            return false;
        r.attr[r.attrN].value = strtol(num, 0, 10);
        r.attrN++;
    }
    p = lineEnd + 1;
    return true;
}

/* False means malformed. `p` is left wherever it stopped. */
static bool parseRecord(const char *&p, const char *end, Record &r)
{
    if (!parseHead(p, end, r))
        return false;

    r.body = p;
    for (;;)
    {
        const char *lineEnd = p;
        if (p >= end)
            return false;                 /* ran out before the dot */
        skipTo(lineEnd, end, '\n');
        if (lineEnd == end)
            return false;
        if (lineEnd - p == 1 && *p == '.')
        {
            r.bodyEnd = p;
            p = lineEnd + 1;
            return true;
        }
        if ((unsigned)(lineEnd - p) > RC_LINE_MAX)
            return false;
        p = lineEnd + 1;
    }
}

/* --- the emitters ------------------------------------------------------ */

struct Ctx
{
    const Record *r;
    unsigned      wrapcol;
    ByteSink     *out;
};

/* Every `t` line, joined. `joiner` is <br> for prose and \n for the kinds
 * that keep their newlines, which is the one difference between the two
 * groups and is worth one parameter rather than two functions. */
static bool prose(const Ctx &c, const char *joiner, bool marked)
{
    LineIter it;
    unsigned shown = 0;
    it.start(*c.r);
    while (it.next())
    {
        if (it.tag != 't')
            continue;
        if (shown && !c.out->write(joiner, (unsigned)strlen(joiner)))
            return false;
        shown++;
        if (marked)
        {
            if (!spansInto(it.text, it.len, *c.out))
                return false;
        }
        else if (!escInto(it.text, it.len, *c.out))
            return false;
    }
    return true;
}

static bool simple(const Ctx &c, TplKey k, const char *joiner, bool marked)
{
    return rc_tpl_open(k, 0, *c.out) && prose(c, joiner, marked)
        && rc_tpl_close(k, 0, *c.out);
}

static bool emitPara(const Ctx &c)  { return simple(c, TPL_PARA, "<br>", true); }
static bool emitQuote(const Ctx &c) { return simple(c, TPL_QUOTE, "<br>", true); }
static bool emitUser(const Ctx &c)  { return simple(c, TPL_USER, "<br>", false); }
static bool emitNotice(const Ctx &c){ return simple(c, TPL_NOTICE, "\n", false); }
static bool emitError(const Ctx &c) { return simple(c, TPL_ERROR, "\n", false); }
static bool emitThink(const Ctx &c) { return simple(c, TPL_THINKING, "\n", false); }

static bool emitRule(const Ctx &c)
{
    return rc_tpl_whole(TPL_RULE, 0, *c.out);
}

static bool emitHeading(const Ctx &c)
{
    char level[4];
    TplValues v;
    long n = attrOf(*c.r, "level", 3);
    if (n < 1) n = 1;
    if (n > 6) n = 6;
    rc_strf(level, sizeof level, "%ld", n);
    v.n = 1;
    v.name[0] = "level";
    v.value[0] = level;
    return rc_tpl_open(TPL_HEADING, &v, *c.out) && prose(c, "<br>", true)
        && rc_tpl_close(TPL_HEADING, &v, *c.out);
}

static bool items(const Ctx &c, TplKey wrap)
{
    LineIter it;
    if (!rc_tpl_open(wrap, 0, *c.out))
        return false;
    it.start(*c.r);
    while (it.next())
    {
        if (it.tag != 't')
            continue;
        if (!rc_tpl_open(TPL_ITEM, 0, *c.out)
            || !spansInto(it.text, it.len, *c.out)
            || !rc_tpl_close(TPL_ITEM, 0, *c.out))
            return false;
    }
    return rc_tpl_close(wrap, 0, *c.out);
}

static bool emitBullet(const Ctx &c)   { return items(c, TPL_BULLET); }
static bool emitNumbered(const Ctx &c) { return items(c, TPL_NUMBERED); }

static bool emitCode(const Ctx &c)
{
    unsigned langLen;
    const char *lang = firstOf(*c.r, 'l', &langLen);
    LineIter it;
    unsigned shown = 0;

    if (!rc_tpl_open(TPL_CODE, 0, *c.out))
        return false;
    if (langLen)
    {
        if (!rc_tpl_open(TPL_LANG, 0, *c.out)
            || !escInto(lang, langLen, *c.out)
            || !rc_tpl_close(TPL_LANG, 0, *c.out))
            return false;
    }
    if (!rc_tpl_open(TPL_PRE, 0, *c.out))
        return false;
    it.start(*c.r);
    while (it.next())
    {
        if (it.tag != 't')
            continue;
        if (shown && !c.out->write("\n", 1))
            return false;
        shown++;
        if (!escInto(it.text, it.len, *c.out))
            return false;
    }
    return rc_tpl_close(TPL_PRE, 0, *c.out)
        && rc_tpl_close(TPL_CODE, 0, *c.out);
}

static bool emitClipMarker(const Ctx &c, long dropped)
{
    char num[16];
    TplValues v;
    rc_strf(num, sizeof num, "%ld", dropped);
    v.n = 1;
    v.name[0] = "dropped";
    v.value[0] = num;
    return rc_tpl_whole(TPL_CLIP, &v, *c.out);
}

static bool emitToolResult(const Ctx &c)
{
    TplKey wrap = attrOf(*c.r, "error", 0) ? TPL_TOOLRESULT_ERR
                                           : TPL_TOOLRESULT;
    long dropped = attrOf(*c.r, "clipped", 0);
    LineIter it;
    unsigned shown = 0;

    if (!rc_tpl_open(wrap, 0, *c.out) || !rc_tpl_open(TPL_PRE, 0, *c.out))
        return false;
    it.start(*c.r);
    while (it.next())
    {
        if (it.tag != 't')
            continue;
        if (shown && !c.out->write("\n", 1))
            return false;
        shown++;
        if (!wrapInto(it.text, it.len, c.wrapcol, *c.out))
            return false;
    }
    if (!rc_tpl_close(TPL_PRE, 0, *c.out))
        return false;
    if (dropped && !emitClipMarker(c, dropped))
        return false;
    return rc_tpl_close(wrap, 0, *c.out);
}

static bool emitClip(const Ctx &c)
{
    return emitClipMarker(c, attrOf(*c.r, "dropped", 0));
}

static bool emitTool(const Ctx &c)
{
    unsigned nameLen;
    const char *name = firstOf(*c.r, 'n', &nameLen);
    char buf[128];
    TplValues v;
    unsigned n = nameLen < sizeof buf - 1 ? nameLen : sizeof buf - 1;

    /* The one value that is copied rather than streamed. A tool NAME is
     * short by construction -- it is an identifier -- and a {name} hole
     * takes a NUL-terminated string. The summary is the children hole and
     * streams, which is why `tool` has both kinds. */
    memcpy(buf, name, n);
    buf[n] = '\0';
    v.n = 1;
    v.name[0] = "name";
    v.value[0] = buf;
    return rc_tpl_open(TPL_TOOL, &v, *c.out) && prose(c, "\n", false)
        && rc_tpl_close(TPL_TOOL, &v, *c.out);
}

static bool emitTable(const Ctx &c)
{
    bool header = attrOf(*c.r, "header", 0) != 0;
    LineIter it;
    bool inRow = false;
    unsigned rowIndex = 0;

    if (!rc_tpl_open(TPL_TABLE, 0, *c.out))
        return false;
    it.start(*c.r);
    while (it.next())
    {
        if (it.tag == 'r')
        {
            if (inRow && !rc_tpl_close(TPL_ROW, 0, *c.out))
                return false;
            if (inRow)
                rowIndex++;
            if (!rc_tpl_open(TPL_ROW, 0, *c.out))
                return false;
            inRow = true;
        }
        else if (it.tag == 'c' && inRow)
        {
            TplKey cell = (header && rowIndex == 0) ? TPL_HEADCELL
                                                    : TPL_CELL;
            if (!rc_tpl_open(cell, 0, *c.out)
                || !spansInto(it.text, it.len, *c.out)
                || !rc_tpl_close(cell, 0, *c.out))
                return false;
        }
    }
    if (inRow && !rc_tpl_close(TPL_ROW, 0, *c.out))
        return false;
    return rc_tpl_close(TPL_TABLE, 0, *c.out);
}

/* The one composite kind, and the one whose payload ORDER matters:
 * render_request puts its argument table last while rules_view puts its
 * paragraphs after the table, so emitting by tag in a fixed order gets one
 * of the two wrong. Consecutive `s` items form one list and consecutive
 * k/v pairs form one table -- one open-container variable each, and a run
 * ends when the tag leaves its category rather than when any other tag
 * arrives, or a step list followed by a table nests the table in the <ol>. */
static bool emitPermission(const Ctx &c)
{
    bool settleable = attrOf(*c.r, "settleable", 1) != 0;
    TplKey wrap = settleable ? TPL_PERM : TPL_PERM_PLAIN;
    bool openList = false, openTable = false;
    LineIter it;

    if (!rc_tpl_open(wrap, 0, *c.out))
        return false;

    it.start(*c.r);
    while (it.next())
    {
        char tag = it.tag;
        const char *text = it.text;
        unsigned len = it.len;
        /* One line of lookahead, by copying the cursor. Two kinds pair --
         * a lead with its description and a table key with its value --
         * and a copied iterator is a struct assignment, which is cheaper
         * than the array of lines this used to need. */
        LineIter ahead = it;
        bool hasAhead = ahead.next();

        if (tag != 's' && openList)
        {
            if (!rc_tpl_close(TPL_PERMSTEPS, 0, *c.out))
                return false;
            openList = false;
        }
        if (tag != 'k' && tag != 'v' && openTable)
        {
            if (!rc_tpl_close(TPL_PERMTABLE, 0, *c.out))
                return false;
            openTable = false;
        }

        switch (tag)
        {
        case 'h':
        {
            /* A head this build has not heard of is simply absent. A bridge
             * newer than the client is the case, and a card missing its
             * heading still says what it is asking. */
            if (len == 3 && memcmp(text, "ask", 3) == 0)
            {
                if (!rc_tpl_whole(TPL_PERMHEAD_ASK, 0, *c.out))
                    return false;
            }
            else if (len == 4 && memcmp(text, "view", 4) == 0)
            {
                if (!rc_tpl_whole(TPL_PERMHEAD_VIEW, 0, *c.out))
                    return false;
            }
            break;
        }
        case 'b':
        {
            /* The lead and its description are one template with two holes,
             * not two templates: the em dash between them cannot be a
             * value's leading character, because GetPrivateProfileStringA
             * strips whitespace from both ends and the space before it
             * would go. A variant is a second key. */
            char lead[256];
            TplValues v;
            unsigned n = len < sizeof lead - 1 ? len : sizeof lead - 1;
            bool paired = hasAhead && ahead.tag == 'd';
            memcpy(lead, text, n);
            lead[n] = '\0';
            /* The lead is marked prose on the bridge and a {name} hole
             * takes a flat string, so the marks are escaped away here
             * rather than drawn. Nothing puts marks in a lead today --
             * render_request passes a clipped title -- and a lead that
             * grew them would want its own children hole. */
            v.n = 1;
            v.name[0] = "lead";
            v.value[0] = lead;
            if (paired)
            {
                if (!rc_tpl_open(TPL_PERMLEAD_DESC, &v, *c.out)
                    || !spansInto(ahead.text, ahead.len, *c.out)
                    || !rc_tpl_close(TPL_PERMLEAD_DESC, &v, *c.out))
                    return false;
                it = ahead;
            }
            else if (!rc_tpl_whole(TPL_PERMLEAD, &v, *c.out))
                return false;
            break;
        }
        case 'd':
            /* A stray description with no lead before it. */
            if (!rc_tpl_open(TPL_PERMPARA, 0, *c.out)
                || !spansInto(text, len, *c.out)
                || !rc_tpl_close(TPL_PERMPARA, 0, *c.out))
                return false;
            break;
        case 'w':
            if (!rc_tpl_open(TPL_PERMWARN, 0, *c.out)
                || !spansInto(text, len, *c.out)
                || !rc_tpl_close(TPL_PERMWARN, 0, *c.out))
                return false;
            break;
        case 'p':
            if (!rc_tpl_open(TPL_PERMPREV, 0, *c.out)
                || !spansInto(text, len, *c.out)
                || !rc_tpl_close(TPL_PERMPREV, 0, *c.out))
                return false;
            break;
        case 't':
            if (!rc_tpl_open(TPL_PERMPARA, 0, *c.out)
                || !spansInto(text, len, *c.out)
                || !rc_tpl_close(TPL_PERMPARA, 0, *c.out))
                return false;
            break;
        case 's':
            if (!openList)
            {
                if (!rc_tpl_open(TPL_PERMSTEPS, 0, *c.out))
                    return false;
                openList = true;
            }
            if (!rc_tpl_open(TPL_PERMSTEP, 0, *c.out)
                || !spansInto(text, len, *c.out)
                || !rc_tpl_close(TPL_PERMSTEP, 0, *c.out))
                return false;
            break;
        case 'k':
        {
            char key[256];
            TplValues v;
            unsigned n = len < sizeof key - 1 ? len : sizeof key - 1;
            bool paired = hasAhead && ahead.tag == 'v';
            memcpy(key, text, n);
            key[n] = '\0';
            if (!openTable)
            {
                if (!rc_tpl_open(TPL_PERMTABLE, 0, *c.out))
                    return false;
                openTable = true;
            }
            v.n = 1;
            v.name[0] = "key";
            v.value[0] = key;
            if (!rc_tpl_open(TPL_PERMROW, &v, *c.out))
                return false;
            if (paired && !spansInto(ahead.text, ahead.len, *c.out))
                return false;
            if (!rc_tpl_close(TPL_PERMROW, &v, *c.out))
                return false;
            if (paired)
                it = ahead;
            break;
        }
        default:
            break;
        }
    }

    if (openList && !rc_tpl_close(TPL_PERMSTEPS, 0, *c.out))
        return false;
    if (openTable && !rc_tpl_close(TPL_PERMTABLE, 0, *c.out))
        return false;
    return rc_tpl_close(wrap, 0, *c.out);
}

static bool emitExportHeading(const Ctx &c)
{
    unsigned titleLen, whenLen;
    const char *title = firstOf(*c.r, 'h', &titleLen);
    const char *when = firstOf(*c.r, 't', &whenLen);

    if (!titleLen)
    {
        title = "Conversation";
        titleLen = 12;
    }
    if (!rc_tpl_open(TPL_EXPORTHEAD, 0, *c.out)
        || !rc_tpl_open(TPL_EXPORTHEAD_TITLE, 0, *c.out)
        || !escInto(title, titleLen, *c.out)
        || !rc_tpl_close(TPL_EXPORTHEAD_TITLE, 0, *c.out))
        return false;
    if (whenLen)
    {
        if (!rc_tpl_open(TPL_EXPORTHEAD_WHEN, 0, *c.out)
            || !escInto(when, whenLen, *c.out)
            || !rc_tpl_close(TPL_EXPORTHEAD_WHEN, 0, *c.out))
            return false;
    }
    return rc_tpl_close(TPL_EXPORTHEAD, 0, *c.out);
}

/* --- dispatch ----------------------------------------------------------- */

struct KindRow
{
    const char *name;
    bool (*emit)(const Ctx &);
};

/* POD, so it clears the static-constructor gate, and indirect, so Watcom
 * cannot inline a dozen emitters into one frame. */
static const KindRow KINDS[] = {
    { "para",          emitPara },
    { "code",          emitCode },
    { "heading",       emitHeading },
    { "bullet",        emitBullet },
    { "numbered",      emitNumbered },
    { "quote",         emitQuote },
    { "rule",          emitRule },
    { "table",         emitTable },
    { "user",          emitUser },
    { "notice",        emitNotice },
    { "error",         emitError },
    { "tool",          emitTool },
    { "toolresult",    emitToolResult },
    { "thinking",      emitThink },
    { "permission",    emitPermission },
    { "clip",          emitClip },
    { "exportheading", emitExportHeading }
};

static bool renderOne(const Record &r, unsigned wrapcol, ByteSink &out)
{
    unsigned i;
    Ctx c;
    c.r = &r;
    c.wrapcol = wrapcol;
    c.out = &out;

    for (i = 0; i < sizeof KINDS / sizeof KINDS[0]; i++)
        if (kindIs(r, KINDS[i].name))
            return KINDS[i].emit(c);

    /* A kind this build has not heard of renders as nothing rather than
     * refusing the frame. It means a bridge newer than the client, and a
     * transcript missing one block beats a transcript that stopped. The
     * bridge's renderer does the same. */
    rc_log("view: unknown block kind \"%.*s\", skipped",
           (int)r.kindLen, r.kind);
    return true;
}

bool rc_render(const char *rec, unsigned len, unsigned wrapcol, ByteSink &out)
{
    const char *p = rec;
    const char *end = rec + len;
    /* A LOCAL, and small enough to be one: a kind, four attributes and two
     * pointers is about 60 bytes. The first version of this held every
     * payload line in an array and had to be static to clear the
     * stack-frame gate, which traded 4 KB of frame for 24 KB of BSS on a
     * machine with 28 KB free. The iterator removed the choice. */
    Record r;

    while (p < end)
    {
        if (!parseRecord(p, end, r))
        {
            rc_log("view: malformed record at offset %u of %u",
                   (unsigned)(p - rec), len);
            return false;
        }
        if (!renderOne(r, wrapcol, out))
            return false;
    }
    return true;
}

int rc_render_count(const char *rec, unsigned len)
{
    const char *p = rec;
    const char *end = rec + len;
    Record r;
    int n = 0;

    while (p < end)
    {
        if (!parseRecord(p, end, r))
            return -1;
        n++;
    }
    return n;
}
