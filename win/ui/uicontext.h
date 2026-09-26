/* uicontext.h -- what the desktop worker keeps alive between requests.
 *
 * Every one of these is expensive to build and cheap to keep. `MsaaSource`
 * loads oleacc and initialises COM; `UiWatch` installs two system-wide
 * hooks; `CallDeadline` registers a message filter on the apartment. Doing
 * any of that per request would cost more than the request.
 *
 * They are also all apartment-bound or thread-bound, which is the real
 * reason this is one struct on one thread rather than something a caller
 * passes around: a message filter belongs to the thread that registered it,
 * and `WINEVENT_OUTOFCONTEXT` delivers only to the thread that installed the
 * hook.
 */
#ifndef RC_UI_UICONTEXT_H
#define RC_UI_UICONTEXT_H

#include "automation/tree.h"
#include "automation/msaa.h"
#include "automation/watch.h"
#include "automation/wait.h"
#include "automation/input.h"
#include "automation/calldeadline.h"
#include "ui/inject.h"

class UiContext
{
public:
    UiContext();

    TreeWalker   walker;
    MsaaSource   msaa;
    UiWatch      watch;
    Input        input;
    CallDeadline deadline;
    /* Opened lazily and only when a request asks for it: mapping the
     * section is harmless, but a section that exists and names a target is
     * what any resident copy of the DLL acts on. */
    Injector     injector;

    /* Our own pid, resolved once. Every walk excludes it: the walker sends
     * WM_GETTEXT, and reaching our own windows from in here is reentrancy we
     * can simply decline. */
    DWORD selfPid;

    /* False until the thread has finished its one-time setup. A request that
     * arrives before then is answered `busy` rather than crashing. */
    bool ready;

private:
    UiContext(const UiContext &);
    UiContext &operator=(const UiContext &);
};

#endif
