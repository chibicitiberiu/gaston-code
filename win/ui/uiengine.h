/* uiengine.h -- the desktop handlers both programs run.
 *
 * The analogue of `fs/ops.h`: behaviour that used to live in one exe's RPC
 * file, lifted out so a second exe can run it rather than copy it. Each
 * caller supplies a `UiLink` (ui/uilink.h) and keeps its own op names, its
 * own frame types and its own id.
 *
 * Everything here runs on the caller's desktop worker -- a thread with a
 * message queue, an STA, and a pump. See `UiContext` for why that is a
 * contract and not an arrangement.
 */
#ifndef RC_UI_UIENGINE_H
#define RC_UI_UIENGINE_H

#include "wire/frame.h"
#include "automation/steps.h"
#include "automation/tree.h"
#include "ui/uilink.h"
#include "ui/uicontext.h"


/* How many steps one list may carry. Bounded by what the bridge may send
 * (bridge/agent/steps.py's MAX_LINES) inside the frame's own 64KB. */
#define RC_RUN_MAX 64

/* Decode a step list IN PLACE. `body` is the frame's own body and is
 * modified: percent-decoding only ever shrinks, so each Step's strings point
 * straight into it and the body must outlive the Step array. Returns the
 * count, or -1 with a reason in `why`.
 *
 * Exposed for uicheck, which is the only way the sixteen verbs and the
 * escaping are reachable without a socket and a desktop. */
int rc_ui_decode_steps(char *body, unsigned len, Step *out, char *why,
                       unsigned whyCap);

/* ---- TREE ------------------------------------------------------------ */

/* Node records batched into frames. The buffer is HEAP, not a local: the
 * worker thread gets the image's stack commit and a 62KB frame on the stack
 * is the fault that crashed AUTOCHK on the P200.
 *
 * `room()`, `left()` and `took()` exist because this sink has a SECOND
 * writer: the client's in-process tier fills it from its own file
 * (ui/uirpc.cpp), as does the MSAA item walk. Those used to reach in and
 * move `len` and `batch` themselves, which is how the batch count and the
 * buffer get out of step -- and it is what stopped this class moving here
 * at all while the other writer stayed behind. */
struct TreeSink : public NodeSink
{
    TreeSink(UiLink &l, UiContext *c, bool wantMsaa);
    ~TreeSink();

    bool ready() const { return buf != 0; }
    void limit(int n) { cap = n; }
    void flush();

    /* Where the next record goes, how much space is left for it, and how
     * much of it was taken. */
    char    *room()       { return buf + len; }
    unsigned left() const { return frameCap - len; }
    void     took(unsigned w) { len += w; batch++; total++; }

    virtual bool onNode(const Node &n);

    UiLink    &link;
    UiContext *ctx;
    unsigned   frameCap;
    char      *buf;
    unsigned   len;
    int        batch;
    int        total;
    int        enriched;
    int        dropped;
    bool       msaa;
    int        cap;

private:
    TreeSink(const TreeSink &);
    TreeSink &operator=(const TreeSink &);
};

const char *rc_ui_walk_end_name(WalkEnd e);

/* A header as a bounded number, and as a flag. Shared so the two callers
 * cannot disagree about what a missing header means. */
long rc_ui_header_num(rc_frame *f, const char *key, long dflt,
                      long lo, long hi);
bool rc_ui_header_flag(rc_frame *f, const char *key, bool dflt);

/* The walk options every op shares, with the caller's limits applied.
 *
 * Takes the pid to exclude rather than the whole context, because that is
 * all it ever wanted -- and it makes the clamping testable without a COM
 * apartment and a desktop (uicheck). */
void rc_ui_base_options(WalkOptions &o, UiLink &link, DWORD selfPid,
                        rc_frame *f);

/* Resolve `target=` to one window. ANSWERS THE REQUEST ITSELF on any
 * failure and returns NULL, so a caller that gets NULL is done. */
HWND rc_ui_resolve_target(UiLink &link, UiContext *ctx, rc_frame *f,
                          const WalkOptions &o);

/* "More than one matched", with WHO matched. */
void rc_ui_end_ambiguous(UiLink &link, const char *detail, const HWND *cand,
                         int candN);

/* UITREE / TREE: the window tree, or one control's MSAA items under
 * `acc=1`. Refuses `automation_off` at RC_AUTOMATION_NONE. `inproc` is NOT
 * handled here -- see the comment at that branch. */
void rc_ui_tree(UiLink &link, UiContext *ctx, rc_frame *f);

/* ---- RUN ------------------------------------------------------------- */

/* UIRUN / RUN: a list of steps, run in order, stopping at the first
 * failure. The body is the list; `link` gets one report and one terminator.
 * Refuses `automation_off` below RC_AUTOMATION_STEPS, because a step list
 * types and the middle capability value says not to. */
void rc_ui_run(UiLink &link, UiContext *ctx, rc_frame *f);

#endif
