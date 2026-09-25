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

#include "app/title.h"

/* What a turn asks of whoever owns the input. One method, where a bare
 * function pointer used to be: it had no context, so it could only reach
 * the compose box and the Stop button through globals. */
class TurnListener
{
public:
    virtual ~TurnListener() {}
    /* A turn started (true) or ended (false): enable Stop, disable Send. */
    virtual void setBusy(bool busy) = 0;
};

class Turn
{
public:
    static Turn *create(HWND hwnd, rc_html *html, rc_statusbar *status,
                        rc_cache *cache, ChatList *chats,
                        TurnListener *listener);
    ~Turn() {}

    bool active() const { return m_active; }
    rc_turn_state state() const { return m_state; }
    DWORD startMs() const { return m_startMs; }

    void noteInput();
    void thinkShow();
    void turnReceiving();
    void turnDone();
    void abort();

    void onTurn(rc_frame *f);
    void onDoc(rc_frame *f);
    void onText(rc_frame *f);
    void onTool(rc_frame *f);

private:
    Turn(HWND hwnd, rc_html *html, rc_statusbar *status, rc_cache *cache,
         ChatList *chats, TurnListener *listener);
    Turn(const Turn &);
    Turn &operator=(const Turn &);

    HWND m_hwnd;
    rc_html *m_html;
    rc_statusbar *m_status;
    rc_cache *m_cache;
    ChatList *m_chats;
    TurnListener *m_listener;

    rc_turn_state m_state;
    bool m_active;
    DWORD m_startMs;
};

#endif /* RC_TURN_H */
