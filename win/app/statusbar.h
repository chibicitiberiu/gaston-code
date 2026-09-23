/* statusbar.h -- Status bar control and indicator panes.
 *
 * Four panes:
 *   0: Connection state icon and host:port with tooltip (double-click opens Server Host dialog).
 *   1: Traffic stats (rx/tx counters + speed from rc_xferstat).
 *   2: Turn activity / elapsed seconds / override text.
 *   3: Context window gauge (pie icon + percentage).
 *
 * A class now, following the pattern of bar.h and xferstat.h.
 */
#ifndef RC_STATUSBAR_H
#define RC_STATUSBAR_H

#include <windows.h>
#include <commctrl.h>
#include "app/resource.h"
#include "net/net.h"

#ifndef __cplusplus
#error "statusbar.h is C++"
#endif

#ifndef SBARS_TOOLTIPS
#define SBARS_TOOLTIPS 0x0800L
#endif

#define ID_STATUS 1005

typedef enum { TURN_IDLE, TURN_THINKING, TURN_STREAMING } rc_turn_state;

class rc_xferstat;
class rc_html;

class rc_statusbar {
    HWND  m_hwnd;
    HICON m_ico_ok;
    HICON m_ico_warn;
    HICON m_ico_bad;
    HICON m_ico_shown;
    HICON m_ico_pie[RC_PIE_STEPS + 1];
    int   m_ctx_pct;
    char  m_status_override[128];

    rc_statusbar(const rc_statusbar &);
    rc_statusbar &operator=(const rc_statusbar &);
    rc_statusbar(HWND hwnd, HINSTANCE inst);

public:
    static rc_statusbar *create(HWND parent, HINSTANCE inst);
    ~rc_statusbar() {}

    HWND hwnd() const { return m_hwnd; }
    int height() const;
    void resize();
    void set_pane(int pane, const char *text);
    void set_override(const char *text);
    void clear_override();
    const char *get_override() const { return m_status_override; }
    void set_ctx_pct(int pct) { m_ctx_pct = pct; }
    int ctx_pct() const { return m_ctx_pct; }

    void refresh(rc_state net_state, const char *host, int port,
                 rc_xferstat *xfer, int turn_active, DWORD turn_start_ms,
                 int turn_state, int bar_active, rc_html *html);

    int notify(NMHDR *nh, HWND parent);
};

#endif /* RC_STATUSBAR_H */
