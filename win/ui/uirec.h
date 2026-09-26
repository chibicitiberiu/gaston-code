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
#include "inproc/shm.h"

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

/* Child-index paths out of a depth-first stream of nodes.
 *
 * The bridge walks `parent` up the chain to build a selector, so the paths
 * have to NEST. A first version of the in-process reader keyed the parent
 * on the node's array index, which is not a path: a grandchild came out as
 * "3/0" while node 3's own path was "1/2", the chain broke at the second
 * level, and every selector silently lost its ancestry -- no error, just
 * worse answers.
 *
 * Big enough to want the heap: seventeen 64-byte paths is most of the 4096
 * a stack frame may use before Win98's guard page. */
struct RcPathBuilder
{
    char at[RC_IN_DEPTH_MAX + 1][RC_PATH_MAX];
    long kids[RC_IN_DEPTH_MAX + 2];
};

void rc_path_reset(RcPathBuilder &b);

/* The next node at `depth`, given that nodes arrive depth-first. Fills
 * `path` and `parent`, both at least RC_PATH_MAX. A root's parent is "-". */
void rc_path_next(RcPathBuilder &b, long depth, char *path, char *parent);

/* Bytes written, or 0 if it does not fit. Never a partial record. */
unsigned rc_uirec_window(char *out, unsigned cap, const Node &n,
                         const Enrichment *e);

/* The same shape for an accessible element, which has a child id where a
 * window has a control id and no class at all. */
unsigned rc_uirec_acc(char *out, unsigned cap, const AccNode &n,
                      const char *path, const char *parent);

/* A window as the IN-PROCESS collector saw it. Same record, same reader --
 * the only difference the bridge sees is that `text` is a control's real
 * contents rather than whatever crossed a process boundary, and that a list
 * has its rows. */
unsigned rc_uirec_inproc(char *out, unsigned cap, const RcInNode &n,
                         const char *path, const char *parent);

/* One row of a list, tree or list view. These do not exist out of process
 * on 9x at any clock speed: LB_GETTEXT and friends take a pointer and
 * nothing marshals it. */
unsigned rc_uirec_item(char *out, unsigned cap, const char *text,
                       const char *path, const char *parent, long index);

#endif
