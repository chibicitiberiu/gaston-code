/* The bridge connection, as a service.
 *
 * Reconnector starts the connection from the UI; this service owns stopping
 * it. The composition root registers it after the main window, so App stops
 * it first, while the window its reader thread posts to still exists.
 * rc_net_stop bounds and times its own joins (up to about 42 s) and leaks
 * what did not stop, so join() always reports done.
 *
 * Moves into libwire when net.cpp does (docs/REMOTE.md, step 2).
 */
#ifndef RC_CONNSERVICE_H
#define RC_CONNSERVICE_H

#ifndef __cplusplus
#error "app/connservice.h is C++"
#endif

#include "core/app.h"
#include "net/net.h"

class ConnectionService : public Service
{
public:
    explicit ConnectionService(rc_net *net);

    const char *name() const;
    bool init();
    bool join(DWORD timeoutMs);

private:
    rc_net *m_net;
};

#endif
