/* uiservice.h -- the thread REMOTE.md has always specified.
 *
 * "ui worker (message queue, CoInitialize'd)". It did not exist: the client
 * had exactly one message loop, the main window's, and that thread also
 * hosts the in-place-active MSHTML transcript view. Three things go wrong
 * if the desktop work is put there and only there -- a tree walk re-enters
 * MSHTML through a same-thread SendMessage, pumping for the event feed
 * re-enters the RPC handler with the next inbound frame, and a wait is
 * seconds long on the thread that repaints.
 *
 * So: one thread, a message queue, COM, and nothing else on it.
 */
#ifndef RC_UI_UISERVICE_H
#define RC_UI_UISERVICE_H

#include "core/app.h"
#include "core/rc_cpp.h"
#include "net/net.h"
#include "ui/uicontext.h"

class UiService : public Service
{
public:
    explicit UiService(rc_net *net);

    const char *name() const;
    bool init();
    bool start();
    void signalStop();
    bool join(DWORD timeoutMs);

private:
    static unsigned __stdcall entry(void *self);
    void body();

    rc_net  *m_net;
    Handle   m_thread;
    DWORD    m_tid;
    UniqueObj<UiContext> m_ctx;

    UiService(const UiService &);
    UiService &operator=(const UiService &);
};

#endif
