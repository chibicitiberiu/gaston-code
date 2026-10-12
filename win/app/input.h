/* Compose and send: the input box, the Send/Stop buttons, and the local echo
 * of what was typed.
 *
 * The last of the §1.2 extraction targets out of main.c to actually move
 * (audit finding 01.59) -- it was never one of the five main.c named when it
 * split bar.cpp/chatlist.cpp/xferstat.cpp/pagebar.cpp/optmenu.cpp out
 * (finding 01.1), so it survived that pass. Same shape as those five: a
 * module plus its own entry points, seamed at the dispatch table
 * render_frame became (finding 01.12).
 *
 * A CLASS NOW, not free functions over file statics (audit finding 01.60,
 * raised by the user): same reasoning and the same shape as app/htmlview.h
 * and the five siblings. Exactly one instance, created explicitly by
 * main.cpp at window-creation time -- never a file-scope object, because a
 * non-trivial constructor there runs before WinMain and so before
 * rc_log_open, and `make -C win check` fails the build on one. See
 * core/rc_cpp.h.
 */
#ifndef RC_INPUT_H
#define RC_INPUT_H

#include <windows.h>
#include "net/net.h"
#include "app/htmlview.h"
#include "app/xferstat.h"
#include "app/sender.h"
#include "core/window.h"

#ifndef __cplusplus
#error "input.h is C++; compose and send are a class now, see app/htmlview.h"
#endif

#define ID_INPUT 1002
#define ID_SEND  1003
#define ID_STOP  1004

/* The compose row's fixed height, and the geometry main.c's layout() needs
 * to know before asking this module to position its own controls -- same
 * relationship as app/bar.h's PERM_H and app/pagebar.h's PAGEBAR_H. The
 * button width is this module's own business and stays in input.cpp. */
#define INPUT_HEIGHT 56

class AttachmentBar;

/* What the compose box asks of whoever owns the window: whether a send is
 * allowed right now (a turn running, a permission bar waiting), and to be
 * told when one went, for the busy state. */
class ComposeListener
{
public:
    virtual ~ComposeListener() {}
    virtual bool isSendBlocked() = 0;
    virtual void onSent() = 0;
    /* Put the transcript back at the live end before a message goes out.
     *
     * Typing is a way of saying "I am done reading back": the reply is
     * going to the end of the transcript and the sender wants to see it.
     * It also has to happen, or the user turn never appears at all -- the
     * bridge streams a block only to a reader who is following live. A
     * no-op when already there, which is every send but one. */
    virtual void returnToLive() = 0;
};

class ComposeBox
{
public:
    static ComposeBox *create(HWND parent, HINSTANCE inst, HFONT font,
                              rc_net *net, rc_html *html, rc_xferstat *xfer,
                              AttachmentBar *pillbar, FrameSender *sender,
                              ComposeListener *listener);
    /* m_edit's destructor destroys the edit control, which ends the
     * subclass on the way (WM_NCDESTROY). */
    ~ComposeBox() {}

    /* cx/y: where the input box starts. cw: the transcript column's width,
     * which bounds the box (Send/Stop sit to its right). winW: the full
     * client width, which is what Send/Stop are measured from. */
    void move(int cx, int y, int cw, int winW);

    /* WM_COMMAND for ID_SEND/ID_STOP. True if `id` was one of those, same
     * contract as the bar's and the page bar's. */
    bool command(int id);

    /* The main window still owns WHEN -- ui_refresh_enables calls these
     * rather than reaching into an HWND this class keeps private. */
    void enableSend(bool enable);
    void enableStop(bool enable);

    /* SetFocus(the box) -- after a send, and to forward WM_SETFOCUS. */
    void focus();

    /* Send `text` as an INPUT, exactly as if it had been typed and Sent:
     * same blocked check, same local echo, same tx accounting, same busy
     * state. True if it actually went.
     *
     * For the Commands menu, which is the first thing that sends a prompt
     * the user did not type. It does NOT touch the input box, so a draft
     * sitting there survives -- the command is a separate message, not an
     * edit to what you were writing. */
    bool sendText(const char *text);

    /* Put `text` in the input box, caret at the end, box focused -- for a
     * Commands entry that takes an argument, where the point is to finish
     * the line yourself.
     *
     * REFUSES rather than overwriting a draft: "the person at the keyboard
     * never loses a byte" applies to a half-written message as much as to a
     * sent one, and a menu that silently ate two paragraphs would be the
     * worst thing on this toolbar. True if the box now holds `text`. */
    bool compose(const char *text);

    HWND box() const { return m_box; }

    /* Is the box empty, and does it have the keyboard?
     *
     * Both exist for the message loop's conditional keys. PgUp/PgDn scroll
     * the transcript, but only when taking them from the edit control costs
     * nothing -- which is exactly when there is nothing to scroll through.
     * And Ctrl+N confirms before discarding a draft, which needs to know
     * whether there IS one. The box HWND stays private; these are the two
     * questions anyone outside actually has about it. */
    bool isEmpty() const;
    bool hasFocus() const;

private:
    /* The edit control, subclassed for Enter-to-send, Ctrl+A and file
     * drops. A SubclassedWindow, so there is no static trampoline and no
     * s_instance to reach this object through. */
    class EditBox : public SubclassedWindow
    {
    public:
        explicit EditBox(ComposeBox *owner) : m_owner(owner) {}

    protected:
        LRESULT handle(UINT msg, WPARAM wp, LPARAM lp);

    private:
        ComposeBox *m_owner;
    };
    friend class EditBox;

    ComposeBox();
    ComposeBox(const ComposeBox &);
    ComposeBox &operator=(const ComposeBox &);

    void doSend();
    void doStop();

    EditBox m_edit;
    HWND m_box;             /* m_edit's window */
    HWND m_sendButton, m_stopButton;
    rc_net *m_net;
    rc_html *m_html;
    rc_xferstat *m_xfer;
    AttachmentBar *m_pillbar;
    FrameSender *m_sender;
    ComposeListener *m_listener;
};

#endif
