/* uirec.h -- one control, as bytes on the wire.
 *
 * Length-prefixed, not separated, and for the same reason `sync.cpp` gives
 * for a manifest row: a separator-based format breaks on the first value
 * that contains the separator. A filename may contain a TAB; a window's
 * text may contain **anything at all**, because an Edit control's contents
 * come back through `WM_GETTEXT` -- newlines, tabs, NULs, whatever somebody
 * typed. A `key=value` line would have been readable right up until the
 * first multi-line text box, and then it would have silently produced extra
 * records rather than failing.
 *
 * A truncated row is therefore not a SHORT row: the prefix stops matching
 * the bytes after it and the parser reads the next record's fields as this
 * one's. So the encoder REFUSES a record that does not fit rather than
 * clamping it, and the caller flushes and retries -- the idiom
 * `syncrpc.cpp`'s manifest already uses.
 *
 * NO HWND. Deliberately, and it is the one rule here worth restating: a
 * handle is meaningless across a session and 9x reuses them, so a handle
 * that escapes to the bridge can be sent back later and click whatever
 * inherited it. The bridge addresses a control by a selector it computed
 * from these fields. See docs/UIAUTOMATION.md, "The node record".
 *
 * The exe's job, not the library's: libautomation knows nothing of the wire.
 */
#ifndef RC_UI_UIREC_H
#define RC_UI_UIREC_H

#include "automation/tree.h"
#include "automation/select.h"
#include "automation/msaa.h"

/* Which kind of thing this record describes. A window is not the only one:
 * a list item, a menu entry and every element of a web page have no HWND,
 * and a painted text run has no object at all. Today the walk emits only
 * 'w'; the others exist so that adding them is a new value in a field the
 * bridge already parses rather than a new wire format. */
#define RC_SRC_WINDOW  'w'
#define RC_SRC_ACC     'a'
#define RC_SRC_GDI     'g'

/* Node state, as one integer, because seven separate "0"/"1" tokens is
 * seven bytes per record for one bit each and a record budget of 62KB. */
#define RC_NF_VISIBLE   0x01u
#define RC_NF_ENABLED   0x02u
#define RC_NF_FOCUSED   0x04u
#define RC_NF_TIMEDOUT  0x08u

/* Bytes written, or 0 if it does not fit. Never a partial record. */
unsigned rc_uirec_window(char *out, unsigned cap, const Node &n,
                         const Enrichment *e);

/* The same shape for an accessible element, which has a child id where a
 * window has a control id and no class at all. */
unsigned rc_uirec_acc(char *out, unsigned cap, const AccNode &n,
                      const char *path, const char *parent);

#endif
