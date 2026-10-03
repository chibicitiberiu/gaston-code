/* statusbar.cpp -- Status bar control and indicator panes implementation.
 */
#include "app/statusbar.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app/htmlview.h"
#include "app/resource.h"
#include "app/xferstat.h"
#include "core/log.h"
#include "core/str.h"

static HICON load_ico(HINSTANCE inst, int id, int s)
{
    return (HICON)LoadImageA(inst, MAKEINTRESOURCE(id), IMAGE_ICON, s, s, LR_DEFAULTCOLOR);
}

rc_statusbar::rc_statusbar(HWND hwnd, HINSTANCE inst)
    : m_hwnd(hwnd), m_ico_shown(NULL), m_ctx_pct(-1), m_bar_since(0)
{
    int s = GetSystemMetrics(SM_CXSMICON);
    int i;

    m_ico_ok   = load_ico(inst, IDI_NET_OK, s);
    m_ico_warn = load_ico(inst, IDI_NET_WARN, s);
    m_ico_bad  = load_ico(inst, IDI_NET_BAD, s);
    if (!m_ico_ok || !m_ico_warn || !m_ico_bad)
        rc_log("status: connection icons failed to load");

    for (i = 0; i <= RC_PIE_STEPS; i++)
        m_ico_pie[i] = load_ico(inst, IDI_PIE0 + i, s);

    m_status_override[0] = '\0';
}

rc_statusbar *rc_statusbar::create(HWND parent, HINSTANCE inst)
{
    HWND hwnd = CreateWindowExA(0, STATUSCLASSNAMEA, "",
                                WS_CHILD | WS_VISIBLE | SBARS_SIZEGRIP |
                                SBARS_TOOLTIPS,
                                0, 0, 0, 0, parent, (HMENU)ID_STATUS, inst, NULL);
    if (!hwnd) return NULL;

    {
        static const int parts[4] = { 150, 300, 560, -1 };
        SendMessageA(hwnd, SB_SETPARTS, 4, (LPARAM)parts);
    }
    SendMessageA(hwnd, SB_SETTIPTEXTA, 1,
                 (LPARAM)"Bytes moving between this machine and the bridge, "
                         "and how fast");
    SendMessageA(hwnd, SB_SETTIPTEXTA, 2,
                 (LPARAM)"What the current request is doing, how long it has "
                         "taken and what it cost");
    SendMessageA(hwnd, SB_SETTIPTEXTA, 3,
                 (LPARAM)"How full the conversation's context window is "
                         "-- known once a request has finished");

    return new rc_statusbar(hwnd, inst);
}

int rc_statusbar::height() const
{
    if (!m_hwnd) return 0;
    RECT srect;
    GetWindowRect(m_hwnd, &srect);
    return srect.bottom - srect.top;
}

void rc_statusbar::resize()
{
    if (m_hwnd) SendMessageA(m_hwnd, WM_SIZE, 0, 0);
}

void rc_statusbar::set_pane(int pane, const char *text)
{
    if (m_hwnd) SendMessageA(m_hwnd, SB_SETTEXTA, (WPARAM)pane, (LPARAM)text);
}

void rc_statusbar::set_override(const char *text)
{
    if (text && *text) {
        rc_strf(m_status_override, sizeof(m_status_override), "%s", text);
    } else {
        m_status_override[0] = '\0';
    }
}

void rc_statusbar::clear_override()
{
    m_status_override[0] = '\0';
}

void rc_statusbar::refresh(rc_state net_state, const char *host, int port,
                           rc_xferstat *xfer, int turn_active,
                           DWORD turn_start_ms, int turn_state,
                           int bar_active, rc_html *html)
{
    char buf[160];

    {
        HICON want = (net_state == RC_CONNECTED) ? m_ico_ok
                   : (net_state == RC_CONNECTING) ? m_ico_warn
                   : m_ico_bad;
        if (want && want != m_ico_shown) {
            SendMessageA(m_hwnd, SB_SETICON, 0, (LPARAM)want);
            m_ico_shown = want;
        }
    }
    rc_strf(buf, sizeof(buf), " %s:%d", host ? host : "", port);
    set_pane(0, buf);

    if (m_ctx_pct >= 0) {
        int step = (m_ctx_pct * RC_PIE_STEPS + 50) / 100;
        if (step < 0) step = 0;
        if (step > RC_PIE_STEPS) step = RC_PIE_STEPS;
        if (m_ico_pie[step])
            SendMessageA(m_hwnd, SB_SETICON, 3, (LPARAM)m_ico_pie[step]);
        rc_strf(buf, sizeof(buf), " %d%%", m_ctx_pct);
        set_pane(3, buf);
        rc_strf(buf, sizeof(buf), "Context window %d%% full", m_ctx_pct);
        SendMessageA(m_hwnd, SB_SETTIPTEXTA, 3, (LPARAM)buf);
    }
    rc_strf(buf, sizeof(buf),
            "%s  -  %s:%d.  Double-click to change the server.",
            rc_state_name(net_state), host ? host : "", port);
    SendMessageA(m_hwnd, SB_SETTIPTEXTA, 0, (LPARAM)buf);

    if (xfer) xfer->text(buf, sizeof(buf));
    else      buf[0] = '\0';
    set_pane(1, buf);

    if (!bar_active) m_bar_since = 0;
    else if (!m_bar_since) m_bar_since = GetTickCount();
    if (turn_active && turn_start_ms) {
        const char *what = bar_active ? "waiting for you..."
                         : (m_status_override[0]) ? m_status_override
                         : turn_state == TURN_STREAMING
                               ? "receiving..." : "Claude is thinking...";
        DWORD since = bar_active ? m_bar_since : turn_start_ms;
        unsigned long secs =
            (unsigned long)((GetTickCount() - since) / 1000);
        rc_strf(buf, sizeof(buf), " %s %lus", what, secs);
        set_pane(2, buf);

        if (html) {
            char row[256];
            rc_strf(row, sizeof(row),
                    "<div class=\"" RC_CSS_STATUS "\">"
                    "<img src=\"spin.gif\" width=\"16\" height=\"16\" alt=\"\">"
                    "%s %lus</div>", what, secs);
            html->status(row);
        }
    }
}

int rc_statusbar::notify(NMHDR *nh, HWND parent)
{
    if (nh && nh->hwndFrom == m_hwnd && nh->code == NM_DBLCLK) {
        if (((NMMOUSE *)nh)->dwItemSpec == 0) {
            SendMessageA(parent, WM_COMMAND, IDM_HOST, 0);
            return 1;
        }
    }
    return 0;
}
