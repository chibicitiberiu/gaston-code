/* reconnect.h -- Reconnector: backoff, retry timers, and connection attempts.
 *
 * Coordinates TIMER_RECONNECT, TIMER_STATUS, and TIMER_HTMLPOLL.
 * Manages exponential backoff (1s -> 15s) and single outage notice logging.
 */
#ifndef RC_RECONNECT_H
#define RC_RECONNECT_H

#include <windows.h>
#include "app/config.h"
#include "net/net.h"
#include "app/htmlview.h"

#ifndef __cplusplus
#error "reconnect.h is C++"
#endif

#define TIMER_RECONNECT 1
#define TIMER_STATUS    2
#define TIMER_HTMLPOLL  3

/* What the reconnector asks of whoever owns the window. An interface, not
 * two bare function pointers: those had no context, so they could only
 * reach state through globals. */
class ReconnectListener
{
public:
    virtual ~ReconnectListener() {}

    /* Repaint the status bar from the connection state. */
    virtual void refreshStatus() = 0;
    /* Show the cached transcript while the bridge is unreachable. */
    virtual void restoreCache() = 0;
    /* Between tearing the old link down and starting the new one: the only
     * moment settings the HELLO carries -- automation, the work root, the
     * PREFS body -- can change without a thread reading them mid-write
     * (rc_syncrpc's home is unlocked, mem-audit F7). */
    virtual void beforeStart() {}
};

class Reconnector
{
public:
    static Reconnector *create(HWND hwnd, rc_net *net, const ClientConfig *cfg,
                               const char *exeDir, rc_html *html,
                               ReconnectListener *listener);
    ~Reconnector();

    void tryConnect();
    void onConnected();
    void schedule();
    void reset();
    /* True if `timerId` was one of the three this class owns. */
    bool handleTimer(WPARAM timerId);

private:
    Reconnector(HWND hwnd, rc_net *net, const ClientConfig *cfg,
                const char *exeDir, rc_html *html,
                ReconnectListener *listener);
    Reconnector(const Reconnector &);
    Reconnector &operator=(const Reconnector &);

    HWND m_hwnd;
    rc_net *m_net;
    const ClientConfig *m_cfg;
    const char *m_exeDir;
    rc_html *m_html;
    ReconnectListener *m_listener;
    int m_backoffMs;
    bool m_retryNoted;
};

#endif /* RC_RECONNECT_H */
