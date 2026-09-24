/* turn.h -- Turn state management and frame handlers for Claude turns.
 *
 * Coordinates turn lifecycle (TURN_IDLE -> TURN_THINKING -> TURN_STREAMING),
 * elapsed time tracking, window title updates, and frames:
 * TURN, DOC, TEXT, TOOL.
 */
#ifndef RC_TURN_H
#define RC_TURN_H

#include <windows.h>
#include "net/frameproto.h"
#include "app/statusbar.h"
#include "app/htmlview.h"
#include "app/cache.h"
#include "app/chatlist.h"

#ifndef __cplusplus
#error "turn.h is C++"
#endif

#ifndef RC_TITLE
#define RC_TITLE "Gaston Code"
#endif

typedef void (*rc_turn_busy_fn)(int busy);
typedef void (*rc_turn_refresh_fn)(void);

class rc_turn {
    HWND                m_hwnd;
    rc_html            *m_html;
    rc_statusbar       *m_status;
    rc_cache           *m_cache;
    rc_chats           *m_chats;
    rc_turn_busy_fn     m_busy_fn;
    rc_turn_refresh_fn  m_refresh_fn;

    rc_turn_state       m_state;
    int                 m_active;
    DWORD               m_start_ms;

    rc_turn(const rc_turn &);
    rc_turn &operator=(const rc_turn &);
    rc_turn(HWND hwnd, rc_html *html, rc_statusbar *status,
            rc_cache *cache, rc_chats *chats,
            rc_turn_busy_fn busy_fn, rc_turn_refresh_fn refresh_fn);

public:
    static rc_turn *create(HWND hwnd, rc_html *html, rc_statusbar *status,
                           rc_cache *cache, rc_chats *chats,
                           rc_turn_busy_fn busy_fn, rc_turn_refresh_fn refresh_fn);
    ~rc_turn() {}

    int active() const { return m_active; }
    rc_turn_state state() const { return m_state; }
    DWORD start_ms() const { return m_start_ms; }

    void note_input();
    void think_show();
    void turn_receiving();
    void turn_done();
    void abort();

    void on_turn(rc_frame *f);
    void on_doc(rc_frame *f);
    void on_text(rc_frame *f);
    void on_tool(rc_frame *f);
};

#endif /* RC_TURN_H */
