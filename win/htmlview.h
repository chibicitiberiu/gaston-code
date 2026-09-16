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

#include <windows.h>

typedef struct rc_html rc_html;

rc_html *rc_html_create(HWND parent);
void     rc_html_destroy(rc_html *v);
void     rc_html_move(rc_html *v, int x, int y, int w, int h);

/* Navigation is asynchronous. Call this until it returns non-zero; anything
 * appended before then is buffered and flushed when the document arrives. */
int      rc_html_poll(rc_html *v);
int      rc_html_ready(rc_html *v);

/* `doc` is the whole skeleton, stylesheet included, as the bridge sent it. */
void     rc_html_set_document(rc_html *v, const char *doc);

void     rc_html_append(rc_html *v, const char *frag);   /* into #t  */
void     rc_html_live(rc_html *v, const char *frag);     /* replaces #l */
void     rc_html_clear(rc_html *v);
void     rc_html_scroll_bottom(rc_html *v);
int      rc_html_at_bottom(rc_html *v);

#endif
