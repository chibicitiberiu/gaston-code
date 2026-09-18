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
 */
#ifndef RC_HTMLVIEW_H
#define RC_HTMLVIEW_H


#ifdef __cplusplus
extern "C" {
#endif
#include <windows.h>

typedef struct rc_html rc_html;

rc_html *rc_html_create(HWND parent);
void     rc_html_destroy(rc_html *v);
void     rc_html_move(rc_html *v, int x, int y, int w, int h);

/* Navigation is asynchronous. Call this until it returns non-zero; anything
 * appended before then is buffered and flushed when the document arrives. */
int      rc_html_poll(rc_html *v);

/* `doc` is the whole skeleton, stylesheet included, as the bridge sent it. */
void     rc_html_set_document(rc_html *v, const char *doc);

/* --- what the transcript can be told -------------------------------------
 *
 * These own their markup and their scroll policy, which the CALL SITES used
 * to own between them. Nine of them, three different markup conventions, and
 * two -- the bridge's ERROR frame and the reconnect message -- pushed raw
 * text with no escaping at all, so a '<' in a bridge message or a hostname
 * ended the document early. Scroll was decided per site: two scrolled always,
 * one conditionally, six never, and `autoscroll` was read from the INI,
 * written back, round-trip-tested by inicheck.exe, and consulted by nothing.
 *
 * Text arguments are PLAIN TEXT and are escaped here. `rc_html_block` is the
 * exception and takes finished markup, because the bridge renders the
 * transcript -- the guest transforms nothing. */
void     rc_html_notice(rc_html *v, const char *text);
void     rc_html_error(rc_html *v, const char *text);
void     rc_html_user(rc_html *v, const char *text);
void     rc_html_block(rc_html *v, const char *html);    /* bridge-rendered */

/* Honour the user's setting. Off means a block still lands, but the view does
 * not jump -- which is the whole point of the option. */
void     rc_html_set_autoscroll(rc_html *v, int on);

void     rc_html_append(rc_html *v, const char *frag);   /* into #t  */
void     rc_html_live(rc_html *v, const char *frag);     /* replaces #l */
void     rc_html_clear(rc_html *v);
void     rc_html_scroll_bottom(rc_html *v);
int      rc_html_at_bottom(rc_html *v);

#ifdef __cplusplus
}
#endif

#endif
