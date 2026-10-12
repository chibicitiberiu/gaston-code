/* The transcript, as an HTML document hosted in MSHTML.
 *
 * Replaces the RichEdit 2.0 view. The measured cost on a 32MB Win98 is about
 * 4MB against a 20MB budget, and it buys padding, borders, background tints,
 * real bulleted lists and tables -- none of which RichEdit 2.0 can do. The
 * engine is already resident because Explorer hosts it for folder Web View, so
 * a second host pays only for its own instance data.
 *
 * The guest still transforms nothing: the bridge sends finished HTML and this
 * puts it in the document. Two containers, because a transcript is both a log
 * and a live thing: #t accumulates finished blocks, #l holds the block still
 * being written and is replaced wholesale as it grows.
 *
 * WHY THIS IS A CLASS AND NOT A HANDLE PLUS NINETEEN FUNCTIONS. It was the
 * latter, and the first thing the user said about this code was that it
 * should not be: "you haven't created a custom class control for the html
 * view, so every time you append text, you also call the scroll down, which
 * is probably not ideal". That is exactly what was wrong -- scroll policy was
 * decided by the caller at nine separate sites, three of them differently --
 * and the vocabulary below (notice/error/user/block) was the fix for it. The
 * class is the rest of that fix: the markup, the escaping, the scroll policy
 * and the COM references are all on the inside now, and a caller cannot
 * reach past them because there is nothing public to reach past.
 */
#ifndef RC_HTMLVIEW_H
#define RC_HTMLVIEW_H

#ifndef __cplusplus
#error "htmlview.h is C++; the view owns COM references and releases them"
#endif

#include <windows.h>

#include "view/render.h"

/* The CSS classes this side emits. They are defined by the BRIDGE's
 * stylesheet (bridge/view/html.py), which the guest receives whole in the DOC
 * frame -- so these three names are a contract between two files in two
 * languages with nothing linking them. Renaming one on the bridge silently
 * unstyles the guest's blocks: no error, no log line, just plain text where
 * there used to be a coloured box.
 *
 * Named here so there is something for a test to compare against, and
 * tests/python/test_guest_css.py asserts every one of them exists in the
 * stylesheet the bridge actually sends. */
/* The two container ids the guest looks up in the document the bridge
 * sends. Same kind of contract as the classes below and the same failure:
 * the bridge writes <div id="t"> and <div id="l"> into its SKELETON, the
 * guest finds them by literal, and nothing links the two. A typo on either
 * side does not error -- find_id returns NULL, every append goes nowhere,
 * and the transcript is simply blank forever.
 *
 * #t accumulates finished blocks; #l holds the block still being written
 * and is replaced wholesale as it grows. */
#define RC_SLOT_DONE   "t"
#define RC_SLOT_LIVE   "l"
/* The status line under both. OPTIONAL, unlike the other two: the bridge's
 * DOC is authoritative and may be an older build than this guest, and a
 * missing status line is a cosmetic loss where a missing #t is a dead
 * transcript. See rc_html::poll. */
#define RC_SLOT_STATUS "s"

#define RC_CSS_STATUS  "status"
#define RC_CSS_NOTICE  "notice"
#define RC_CSS_ERROR   "err"
#define RC_CSS_USER    "you"
/* The echo is written pending and settled when the turn starts. One id,
 * because there is only ever one un-acknowledged message: you cannot send
 * another until this one has been. */
#define RC_CSS_PENDING "you pending"
#define RC_ID_PENDING  "pend"

/* The permission block, before and after it is answered. The bridge writes
 * the block with `id=perm`; settle_perm swaps the class and drops the id, so
 * the next request's block is the only one carrying it. Same shape and same
 * reasoning as the pending echo above. */
#define RC_CSS_PERMDONE "permdone"
#define RC_ID_PERM      "perm"

struct rc_html_impl;

class rc_html {
    /* Everything is behind the pointer, so this header pulls in windows.h and
     * nothing else. ole2.h, mshtml.h and docobj.h together are a few thousand
     * lines, and main.cpp -- the only caller -- needs none of them.
     *
     * The honest cost: this is a second heap block where there was one, plus a
     * dereference per call. Accepted because the view is a singleton -- one
     * block for the whole process life, against putting the entire MSHTML
     * header set in front of main.cpp. It would NOT be the right trade for
     * something allocated per message. */
    rc_html_impl *d;

    rc_html(const rc_html &);
    rc_html &operator=(const rc_html &);
    explicit rc_html(rc_html_impl *impl) : d(impl) {}

public:
    /* Null if the control could not be created. The caller already has to
     * cope with that -- MSHTML not being registered is a real state on a
     * stripped Win98 -- so this stays a return value rather than becoming a
     * half-built object that fails later. */
    static rc_html *create(HWND parent);
    ~rc_html();

    void move(int x, int y, int w, int h);

    /* Navigation is asynchronous. Call this until it returns non-zero;
     * anything appended before then is buffered and flushed when the document
     * arrives. */
    int  poll();

    /* `doc` is the whole skeleton, stylesheet included, as the bridge sent
     * it. */
    void set_document(const char *doc);

    /* Install the cold-start document shipped beside the exe (view.htm).
     *
     * Non-zero if one was found and installed. Without it the transcript is
     * a bare grey rectangle until a bridge answers -- which is exactly when
     * the app most needs somewhere to say "no bridge answered". The file is
     * GENERATED from bridge/view/html.py at package time (tools/gen/mktools-iso.sh)
     * so there is one stylesheet, not two that drift; the bridge's DOC frame
     * still wins at runtime. */
    int  boot_local();

    /* --- what the transcript can be told ---------------------------------
     *
     * These own their markup and their scroll policy, which the CALL SITES
     * used to own between them. Nine of them, three different markup
     * conventions, and two -- the bridge's ERROR frame and the reconnect
     * message -- pushed raw text with no escaping at all, so a '<' in a
     * bridge message or a hostname ended the document early. Scroll was
     * decided per site: two scrolled always, one conditionally, six never,
     * and `autoscroll` was read from the INI, written back, round-trip-tested
     * by inicheck.exe, and consulted by nothing.
     *
     * Text arguments are PLAIN TEXT and are escaped here. `block` is the
     * exception and takes finished markup, because the bridge renders the
     * transcript -- the guest transforms nothing. */
    void notice(const char *text);
    void error(const char *text);
    void user(const char *text);

    /* The echoed message has been acknowledged -- drop its pending styling
     * in place. No-op if there is nothing pending. Called on TURN start. */
    void settle_pending();

    /* Collapse the permission block that is on screen, once it has been
     * answered. A long session used to be mostly amber: every approval
     * stayed a full argument table forever. Called on PERMEND.
     *
     * The LIVE view only. Paging back to a sealed page re-renders what the
     * paginator holds, which is the original block with its table -- that is
     * the record of what was asked and answered, and it is right that it
     * does not shrink. */
    void settle_perm();
    void block(const char *html);          /* finished markup, guest-made */

    /* --- what the bridge actually sends, from stage 3 ---------------------
     *
     * A frame body of zero or more WHOLE records. `records` renders and
     * inserts them ONE AT A TIME, which is the memory mitigation rather
     * than a style: a page replay is up to 90 blocks, and one buffer for
     * all of them would hold the page's markup and the UTF-16 copy MSHTML
     * makes of it at once.
     *
     * `live_records` renders the whole body and replaces #l wholesale,
     * because the bridge re-sends the open block every flush -- inserting
     * per record there would append a fresh copy ten times a second. An
     * empty body is NOT a no-op: it is what clears the slot at turn end.
     *
     * `wrapcol` is the column a literal block's long lines break at. The
     * view passes its own window's; export passes a fixed one. It is a
     * parameter rather than a setting because the two differ and because
     * the bridge no longer knows it. */
    void records(const char *rec, unsigned len, unsigned wrapcol);
    void live_records(const char *rec, unsigned len, unsigned wrapcol);

    /* Honour the user's setting. Off means a block still lands, but the view
     * does not jump -- which is the whole point of the option. */
    void set_autoscroll(int on);

    void live(const char *frag);           /* replaces #l */

    /* --- what the live path costs, per turn -------------------------------
     *
     * Stage 3 moves rendering from the bridge onto this machine, and the live
     * path is where that is expensive: the bridge re-sends the whole open
     * block on every flush (see above), so the guest redoes the work ten
     * times a second for the length of an answer. Whether that fits is a
     * measurement, not an argument, and these two are the instrument.
     *
     * `live_cost` is told what the render half cost; `live` times the DOM
     * half itself; `live_report` logs both and resets, and is called at turn
     * end. The split matters because the question is the RATIO -- a renderer
     * costing a fifth of the DOM write it feeds is free in practice, and one
     * total figure cannot say that.
     *
     * Timing is QueryPerformanceCounter and REFUSES rather than degrading: a
     * 55ms GetTickCount tick cannot resolve the 5ms-per-update floor these
     * numbers are compared against, and a number that cannot see the
     * difference still reads like data. A machine with no usable counter
     * gets the update and byte counts and says it was not timed. */
    void live_cost(unsigned long renderUs);
    void live_report();

    /* Replaces #s, the status line under the transcript. HTML, not text --
     * it carries the spinner's <img>. Pass "" to clear it.
     *
     * The guest composes this, which is not a breach of "the guest
     * transforms nothing": it renders no bridge content, it says what the
     * app itself is doing. Silently a no-op if the document has no #s. */
    void status(const char *frag);
    void clear();
    void scroll_bottom();
    int  at_bottom() const;

    /* Keyboard scrolling. `dir` is -1 for a page up, +1 for a page down;
     * scroll_top() is Ctrl+Home's other half (Ctrl+End is scroll_bottom).
     *
     * A page is the viewport minus an overlap, the way every document reader
     * behaves -- scrolling by the full height loses the line you were
     * reading across the jump. Both go through IHTMLWindow2::scrollBy, which
     * this file already calls, so nothing new is imported or linked. */
    void scroll_page(int dir);
    void scroll_top();

private:
    /* Was rc_html_append, and was public. It is the raw path: no escaping, no
     * scroll policy, straight into #t. Making it public again would re-open
     * the exact hole the vocabulary above closed, because the two unescaped
     * sites reached it directly. `block` is the supported way in. */
    void renavigate();
    void append(const char *frag);
    /* One of the blocks the GUEST composes, escaped here and wrapped in
     * its template from view.tpl. Replaced say()/say_id(), which took a
     * class name and built the <div> with rc_strf -- the markup is in the
     * template file with every other block's now, and the class name is
     * no longer repeated in two languages.
     *
     * The ESCAPING is still this side's and deliberately not the record
     * path's: see rc_wrap_local in win/view/render.h. */
    void say_local(RcLocal kind, const char *text);
    void settle(int was_at_bottom);
};

#endif
