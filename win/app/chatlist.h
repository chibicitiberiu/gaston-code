/* The chat list: the left-hand column of past and current conversations.
 *
 * The cleanest cut out of main.c (audit finding 01.1): no g_chat_* global
 * used to be touched outside the lines that moved here. Owns the listbox,
 * its Refresh button, the SESSIONS/SESSION/SESSIONSEND/SWITCHED frame types,
 * and the id-keyed bookkeeping (g_chat_ids, g_chat_is_header, g_chat_group,
 * g_chat_current, g_chat_count -- all folded into this class's own members
 * now) that used to be six main.c globals (audit finding 01.11 counts seven
 * and names a g_chat_widest that does not exist in the code as found; five
 * of the six are these, the sixth is g_chats_oldproc, a member here as well).
 *
 * A CLASS, created by the main window at WM_CREATE and destroyed with
 * `delete` (never a file-scope object -- see core/rc_cpp.h). It reports to a
 * ChatListListener rather than to context-free callbacks, and its listbox
 * subclass is a SubclassedWindow (core/window.h) rather than a static
 * procedure over a singleton (docs/REMOTE.md, C5).
 */
#ifndef RC_CHATLIST_H
#define RC_CHATLIST_H

#include <windows.h>
#include "net/net.h"
#include "net/frameproto.h"
#include "app/sender.h"
#include "core/window.h"

#include <string>
#include <vector>

#ifndef __cplusplus
#error "chatlist.h is C++; the chat list is a class now, see app/htmlview.h"
#endif

#define ID_CHATS   1012
#define ID_REFRESH 1013

/* WM_APP-offset message this module posts to its parent: the listbox
 * subclass -> wnd_proc, "open the highlighted conversation" -- was declared
 * with the rest of main.c's own WM_APP offsets; it lives here now, since it
 * is this module's event, not main.c's. */
#define WM_RC_OPENCHAT (WM_APP + 20)

/* What the chat list asks of whoever owns the window. It does not own these
 * questions: whether a running turn may be abandoned, and what the title and
 * status pane say, are main-window business. */
class ChatListListener
{
public:
    virtual ~ChatListListener() {}

    /* A switch is about to happen: is a turn running, may I? True to
     * proceed; on a decline the list restores its own highlight. */
    virtual bool confirmSwitch() = 0;

    /* The switch was confirmed and is going out on the wire: show that a
     * conversation is loading. */
    virtual void onSwitching() = 0;

    /* A SWITCHED frame has been folded into the list's bookkeeping. `f` is
     * handed on for what else it carries (today `total`): the page total,
     * the title, the status pane and any leftover bar are not the list's
     * business. */
    virtual void onSwitched(rc_frame *f) = 0;
};

/* A CAP on rows from the wire, not storage: the rows live in a vector sized
 * to what the bridge actually sent. Header rows count against it. */
#define MAX_CHATS 64

/* The longest row label this list ever adds. `text[]` in the owner-draw
 * handler is sized from it, because LB_GETTEXT takes no size parameter and
 * must never be handed a buffer smaller than the item it reads. */
#define RC_CHAT_LABEL 160

class ChatList
{
public:
    static ChatList *create(HWND parent, HINSTANCE inst, rc_net *net,
                            HFONT normal, HFONT bold, FrameSender *sender,
                            ChatListListener *listener);
    /* m_listBox's destructor destroys the listbox, which ends the subclass
     * on the way (WM_NCDESTROY): nothing can reach this object afterwards. */
    ~ChatList() {}

    /* x/y/w: the column's position and width. colH: the column's FULL
     * height, list plus the Refresh button plus the gap between them: the
     * list gets colH minus 24px, Refresh the 22px left after a 2px gap. */
    void move(int x, int y, int w, int colH);

    HWND hwnd() const;
    void request();

    /* SESSIONS, SESSION, SESSIONSEND, SWITCHED -- the four frame types this
     * class owns. True if `f->type` was one of those. */
    bool frame(rc_frame *f);

    /* Fill the list from somewhere that is not the wire -- app/cache.cpp
     * replaying what was on screen before the bridge went away. Same three
     * steps the SESSIONS/SESSION/SESSIONSEND frames drive, so a restored
     * list and a live one cannot differ in a detail nobody thought to
     * check. */
    void beginRows();
    void addRow(const char *id, const char *title, const char *group,
                bool current);
    void endRows();

    /* Undo a beginRows() whose batch never finished -- called from the
     * disconnect branch. WM_SETREDRAW(FALSE) is latched by beginRows and
     * cleared ONLY by endRows, so a link that drops between SESSIONS and
     * SESSIONSEND (a wide window for a 64-row list on a slow wire) left the
     * flag down for the rest of the session with the visible list and the
     * backing array disagreeing. */
    void abortRows();

    bool draw(DRAWITEMSTRUCT *dis);
    bool measure(MEASUREITEMSTRUCT *mis);

    /* The WM_RC_OPENCHAT / dblclick / Enter path. */
    void openSelected();

private:
    /* The listbox, subclassed so a plain click opens a conversation -- see
     * chatlist.cpp on why that is a button-up and not LBN_SELCHANGE. A
     * SubclassedWindow, so there is no static trampoline and no s_instance:
     * the old destructor had to un-subclass before clearing the singleton
     * or the listbox, which outlives this object, read freed memory. */
    class ListBox : public SubclassedWindow
    {
    protected:
        LRESULT handle(UINT msg, WPARAM wp, LPARAM lp);
    };

    ChatList(HWND parent, HINSTANCE inst, rc_net *net, HFONT normal,
             HFONT bold, FrameSender *sender, ChatListListener *listener);
    ChatList(const ChatList &);
    ChatList &operator=(const ChatList &);

    int textWidth();
    /* `bold` selects the font the row will actually be DRAWN with: the open
     * conversation is bold and bold is wider, so measuring it as normal let
     * that one row overflow into the client edge. */
    void fitLabel(char *label, unsigned cap, int availPx, bool bold);
    void onSessions(rc_frame *f);
    void onSession(rc_frame *f);
    void onSessionsEnd(rc_frame *f);
    void onSwitchedFrame(rc_frame *f);

    /* One per list-box row, in the same order. A header row has no id; it
     * is drawn differently and refuses to be opened. */
    struct Row
    {
        std::string id;
        bool header;
    };
    std::vector<Row> m_rows;
    std::string m_group;    /* last group emitted, to spot changes */
    int  m_current;         /* row of the conversation that is open */

    ListBox m_listBox;
    HWND m_list;            /* m_listBox's window, for the many SendMessage calls */
    HWND m_refresh;
    HFONT m_fontNormal, m_fontBold;
    rc_net *m_net;
    FrameSender *m_sender;
    ChatListListener *m_listener;
};

#endif
