/* uilink.h -- the seam between a ui handler and whichever wire it answers on.
 *
 * `win/ui/uiengine.cpp` holds TREE, its MSAA item listing and the UIRUN step
 * list. Two programs run those handlers: the client, which answers
 * `UIENT`/`UIDAT`/`UIEND` through `rc_net`, and `gremote.exe`, which answers
 * `DATA`/`OK`/`ERR` through a `Responder` (docs/REMOTE.md step 6). Neither
 * vocabulary appears above this interface, which is the point: REMOTE.md is
 * explicit that the two op namespaces must not drift into each other, and a
 * shared handler that knew either spelling is how they would.
 *
 * It is the engine's whole view of its caller. Everything the handlers used
 * to read off `rc_net *` -- where to send, how big a frame may be, whether
 * automation is on, what the limits are -- comes through here now.
 */
#ifndef RC_UI_UILINK_H
#define RC_UI_UILINK_H

#ifndef __cplusplus
#error "ui/uilink.h is C++"
#endif

#include <windows.h>

/* The numbers a caller is allowed to have an opinion about.
 *
 * These were literals inside the handlers, which was fine while the client
 * was the only caller: its ceilings are compiled in and its INI has no say.
 * gremote's `[ui]` section does, so a shared handler that kept the literals
 * would clamp against the wrong ones and make that section dead config.
 *
 * Only defaults and CEILINGS. The floors stay compiled in: they are sanity
 * bounds on a number off the wire, not policy a deployment gets to choose,
 * and a configurable floor is a way to ask for a 1ms walk deadline.
 */
struct UiLimits
{
    /* TREE */
    int   maxNodes,  maxNodesTop;     /* nodes in one walk */
    DWORD callMs,    callMsTop;       /* one WM_GETTEXT */
    DWORD treeMs,    treeMsTop;       /* the whole walk */
    /* RUN */
    DWORD scriptMs,  scriptMsTop;     /* the whole step list */
    DWORD stepMs,    stepMsTop;       /* a step that names no timeout */

    /* The client's values, which were the only ones until 2026-10-02 --
     * `base_options` and the `headerNum` calls in the step adapter. Inline so
     * this header stands alone, and a constructor rather than relying on a
     * brace initialiser because this is C++98 and a non-POD with no
     * constructor is heap garbage whatever the `()` says (see CLAUDE.md). */
    UiLimits()
        : maxNodes(400),    maxNodesTop(4000),
          callMs(200),      callMsTop(2000),
          treeMs(8000),     treeMsTop(60000),
          scriptMs(120000), scriptMsTop(600000),
          stepMs(30000),    stepMsTop(120000)
    {
    }
};

class DriveOverlay;

class UiLink
{
public:
    virtual ~UiLink() {}

    /* EXACTLY ONE of these ends every request, refusals included. Silence
     * costs the caller its whole timeout and tells it nothing, which is the
     * same duty `reject_busy` has on the job lane.
     *
     * HEADERS ONLY, and that is forced rather than chosen. gremote's
     * `Responder` has no body parameter on `ok`, `err` or `end` -- only on
     * `data` -- because a non-bulk frame passes no out-queue reserve and
     * `RC_SEND_WAIT_UI`: those frames are what the reserve exists to
     * protect. A 32KB step report down that path would eat the slot the
     * heartbeat depends on. So a body goes through `report()`. */
    virtual bool end(const char *status, const char *const *keys,
                     const char *const *vals, int nkv) = 0;

    /* A batch of node records, `n` of them, already encoded by uirec. */
    virtual bool records(const void *body, unsigned len, int n) = 0;

    /* Raw bytes of a payload -- a screenshot, or the clipboard's text. The
     * BULK channel: UIDAT on the client, a reserved DATA on gremote. Anything
     * that can be large belongs here and not on `end()`, which carries no
     * body, nor on `report()`, which the client buffers onto its terminator. */
    virtual bool bytes(const void *body, unsigned len) = 0;

    /* The step list's per-step report. The client buffers it and hangs it on
     * its single UIEND, which is the shape `bridge/agent/steps.py` already
     * parses; gremote sends it as a DATA frame before the terminator. Either
     * way it is called at most once per request, before `end()`. */
    virtual bool report(const void *body, unsigned len) = 0;

    /* How many body bytes one frame may carry. Was `RC_MAX_FRAME - 2048`
     * spelled twice inside the handlers. */
    virtual unsigned bodyCap() const = 0;

    /* RC_AUTOMATION_NONE / _SCREEN / _STEPS, read LIVE on every request so
     * turning the capability off takes effect on the next request instead of
     * the next connection.
     *
     * An int and not a bool: SCREEN means "read the screen but do not drive
     * it", so a bool would let a step list type into a box configured not to
     * be typed into. The client never emits that middle value -- it sends
     * STEPS or NONE -- but gremote's `[ui] automation` is an integer off a
     * config file, so this is the first caller that can. */
    virtual int automationLevel() const = 0;

    virtual const UiLimits &limits() const = 0;

    /* Raised from another thread to stop the work in flight: between steps,
     * inside any wait, at every node of a walk and every strip of a capture.
     * Null when this caller has no cancel. */
    virtual volatile LONG *stopFlag() = 0;

    /* The strip that tells the person at the keyboard that the desktop is
     * being driven, or null where there is nobody to tell -- a check, a
     * probe, a box configured without one.
     *
     * Not pure, and that is the point: the default keeps every caller that
     * has no indicator from naming DriveIndicator, and wlink pulls a
     * library member in on any symbol reference. Through this the engine
     * calls nothing but vtable slots, so the window, the thread and the
     * paint code stay out of uicheck, autocheck and the probes. */
    virtual DriveOverlay *overlay() { return 0; }
};

#endif
