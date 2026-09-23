/* reconnect.h -- Reconnection backoff, retry timers, and connection attempts.
 *
 * Coordinates TIMER_RECONNECT, TIMER_STATUS, and TIMER_HTMLPOLL.
 * Manages exponential backoff (1s -> 15s) and single outage notice logging.
 */
#ifndef RC_RECONNECT_H
#define RC_RECONNECT_H

#include <windows.h>
#include "core/ini.h"
#include "net/net.h"
#include "app/htmlview.h"

#ifndef __cplusplus
#error "reconnect.h is C++"
#endif

#define TIMER_RECONNECT 1
#define TIMER_STATUS    2
#define TIMER_HTMLPOLL  3

typedef void (*rc_refresh_fn)(void);
typedef void (*rc_restore_fn)(void);

class rc_reconnect {
    HWND             m_hwnd;
    rc_net          *m_net;
    const rc_config *m_cfg;
    const char      *m_exe_dir;
    rc_html         *m_html;
    rc_refresh_fn    m_refresh;
    rc_restore_fn    m_restore;
    int              m_backoff_ms;
    int              m_retry_noted;

    rc_reconnect(const rc_reconnect &);
    rc_reconnect &operator=(const rc_reconnect &);
    rc_reconnect(HWND hwnd, rc_net *net, const rc_config *cfg,
                 const char *exe_dir, rc_html *html,
                 rc_refresh_fn refresh, rc_restore_fn restore);

public:
    static rc_reconnect *create(HWND hwnd, rc_net *net, const rc_config *cfg,
                                const char *exe_dir, rc_html *html,
                                rc_refresh_fn refresh, rc_restore_fn restore);
    ~rc_reconnect();

    void try_connect();
    void on_connected();
    void schedule();
    void reset();
    int  handle_timer(WPARAM wp);
};

#endif /* RC_RECONNECT_H */
