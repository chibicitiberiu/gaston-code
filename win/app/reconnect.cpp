/* reconnect.cpp -- Reconnection backoff, retry timers, and connection attempts.
 */
#include "app/reconnect.h"
#include "app/htmlview.h"
#include "app/tooldet.h"
#include "app/workdir.h"
#include "core/log.h"
#include "core/str.h"

rc_reconnect::rc_reconnect(HWND hwnd, rc_net *net, const rc_config *cfg,
                           const char *exe_dir, rc_html *html,
                           rc_refresh_fn refresh, rc_restore_fn restore)
    : m_hwnd(hwnd), m_net(net), m_cfg(cfg), m_exe_dir(exe_dir),
      m_html(html), m_refresh(refresh), m_restore(restore),
      m_backoff_ms(1000), m_retry_noted(0)
{
    if (m_hwnd) SetTimer(m_hwnd, TIMER_STATUS, 1000, NULL);
}

rc_reconnect::~rc_reconnect()
{
    if (m_hwnd) {
        KillTimer(m_hwnd, TIMER_STATUS);
        KillTimer(m_hwnd, TIMER_RECONNECT);
        KillTimer(m_hwnd, TIMER_HTMLPOLL);
    }
}

rc_reconnect *rc_reconnect::create(HWND hwnd, rc_net *net, const rc_config *cfg,
                                   const char *exe_dir, rc_html *html,
                                   rc_refresh_fn refresh, rc_restore_fn restore)
{
    return new rc_reconnect(hwnd, net, cfg, exe_dir, html, refresh, restore);
}

void rc_reconnect::try_connect()
{
    const char *tools;
    unsigned tools_len = 0;

    if (m_refresh) m_refresh();
    /* SHARING OFF before saying hello.
     *
     * HELLO carries `sync_root` straight from the live sync layer, and the
     * live value is whatever conversation was open when the last connection
     * ended -- which outlives a bridge restart, because it is held here and
     * not there. So a bridge that had just started was told the guest was
     * mirroring C:\Gaston\OK, picked that tree's baseline, and then ran its
     * first pass against its own whole mirrored directory. The two sides
     * disagreed about which tree they were pairing before a conversation was
     * even open.
     *
     * A connection therefore starts with nothing mirrored, and the bridge
     * names the tree when a conversation commits to one -- the one direction
     * of authority workdir.h describes. */
    rc_workdir_apply("");
    /* Tear the previous attempt down FIRST.
     *
     * rc_net_start is not idempotent: it re-initialises the critical section,
     * zeroes the outbound queue and overwrites the thread handles. Calling it
     * with the sender and worker from a dead connection still alive put two
     * senders on one socket and corrupted the queue. rc_net_stop is a no-op
     * when there is nothing to stop. */
    rc_net_stop(m_net);
    /* WHAT THIS MACHINE HAS, carried on HELLO so the bridge can tell the
     * agent before it writes the system prompt. Cached: this function runs
     * again every few seconds while the bridge is down, and a rescan per
     * retry would walk the FAT on a loop for an answer that cannot change
     * without a restart. The scan itself suppresses critical-error dialogs,
     * which matters because we are on the thread that pumps messages. */
    tools = rc_tooldet_cached(m_exe_dir, &tools_len);
    if (rc_net_start(m_net, m_hwnd, m_cfg->host, m_cfg->port, m_cfg->secret,
                     tools, tools_len) != 0) {
        /* Say it ONCE per outage, not once per retry.
         *
         * Appending on every attempt filled the transcript with hundreds of
         * identical "Cannot reach ... retrying in 15s" lines while the bridge
         * was down -- which is not merely ugly: it is unbounded growth in the
         * one structure this machine cannot afford to grow, on a box measured
         * at 28KB free. The status bar already shows the live state, and that
         * is the right place for something that changes every few seconds. */
        if (!m_retry_noted) {
            char msg[160];
            rc_strf(msg, sizeof(msg), "Cannot reach %s:%d - retrying",
                    m_cfg->host, m_cfg->port);
            /* Escaped now. The host comes from the INI, so this was
             * unescaped user-supplied text going into the document. */
            if (m_html) m_html->notice(msg);
            m_retry_noted = 1;
            /* Once per outage, right after the one notice that is also once
             * per outage -- the retry timer fires every few seconds and
             * redrawing the transcript under someone reading it would be
             * worse than the empty window this replaces. */
            if (m_restore) m_restore();
        }

        /* Backoff on a timer, never a sleeping loop -- the message queue has
         * to keep pumping or the window is a dead rectangle. */
        SetTimer(m_hwnd, TIMER_RECONNECT, (UINT)m_backoff_ms, NULL);
        m_backoff_ms *= 2;
        if (m_backoff_ms > 15000) m_backoff_ms = 15000;
    }
    if (m_refresh) m_refresh();
}

void rc_reconnect::on_connected()
{
    m_backoff_ms = 1000;
    m_retry_noted = 0;
}

void rc_reconnect::schedule()
{
    SetTimer(m_hwnd, TIMER_RECONNECT, (UINT)m_backoff_ms, NULL);
}

void rc_reconnect::reset()
{
    KillTimer(m_hwnd, TIMER_RECONNECT);
    m_backoff_ms = 1000;
    m_retry_noted = 0;
}

int rc_reconnect::handle_timer(WPARAM wp)
{
    if (wp == TIMER_RECONNECT) {
        KillTimer(m_hwnd, TIMER_RECONNECT);
        try_connect();
        return 1;
    } else if (wp == TIMER_STATUS) {
        if (m_refresh) m_refresh();
        return 1;
    } else if (wp == TIMER_HTMLPOLL) {
        if (m_html && m_html->poll()) KillTimer(m_hwnd, TIMER_HTMLPOLL);
        return 1;
    }
    return 0;
}
