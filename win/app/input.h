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

/* Signature of main.c's ui_send: "send something the user just asked for,
 * and say so if it did not go." Reused rather than duplicated, exactly as
 * app/bar.h's rc_bar_send_fn and app/chatlist.h's rc_chats_send_fn do. */
typedef int (*rc_input_send_fn)(const char *what, const char *type,
                                 const char *const *keys,
                                 const char *const *vals, int nkv,
                                 const void *body, unsigned body_len);

class rc_input {
    HWND         box, send_btn, stop_btn;
    WNDPROC      oldproc;
    rc_net      *net;
    rc_html     *html;
    rc_xferstat *xfer;
    rc_input_send_fn send_fn;
    int        (*blocked_fn)(void);
    void       (*sent_fn)(void);

    /* A static trampoline, and a pointer to the single instance for it to
     * reach through. WNDPROC's signature is fixed by Windows, so a member
     * function cannot be one -- and there is only ever one compose row, which
     * is the same singleton assumption the pre-class g_input_oldproc already
     * made. A plain pointer, so it has no constructor and the static-ctor
     * gate stays green. */
    static rc_input *s_instance;
    static LRESULT CALLBACK box_proc(HWND h, UINT m, WPARAM w, LPARAM l);

    void echo_user_block(const char *text);
    void do_send(void);
    void do_stop(void);

    char         staged_attach[MAX_PATH];

    rc_input(const rc_input &);
    rc_input &operator=(const rc_input &);
    rc_input() : box(0), send_btn(0), stop_btn(0), oldproc(0), net(0),
                 html(0), xfer(0), send_fn(0), blocked_fn(0), sent_fn(0) {
        staged_attach[0] = '\0';
    }

public:
    /* net: the one raw rc_net_send this module makes itself, for INPUT.
     *      do_send has always had its own "Not connected" refusal rather
     *      than going through main.c's ui_send (which would report a
     *      different message), so that is preserved rather than folded into
     *      `send` below.
     * html: user()/error() -- the local echo of a sent message and the
     *      "too long"/"not connected" refusals. May be NULL, as everywhere
     *      else that guards the view; every call here is guarded too.
     * xfer: do_send's own side of the rx/tx counters.
     * send: main.c's ui_send, for INTERRUPT (do_stop).
     * blocked: "is Send currently refused" -- g_turn_active or the bar
     *      blocking it. do_send checks this itself rather than trusting the
     *      button's enabled state, because Enter reaches do_send directly
     *      through box_proc and bypasses whatever EnableWindow last set.
     * sent: called once a typed INPUT has actually gone, after the box is
     *      cleared -- status_refresh plus ui_set_busy(1) today, chrome this
     *      module does not own. */
    static rc_input *create(HWND parent, HINSTANCE inst, HFONT font,
                            rc_net *net, rc_html *html, rc_xferstat *xfer,
                            rc_input_send_fn send, int (*blocked)(void),
                            void (*sent)(void));
    /* Same reason as rc_chats': box_proc dereferences s_instance and the
     * edit control outlives this object during teardown. See chatlist.h. */
    ~rc_input();

    /* cx/y: where the input box starts. cw: the transcript column's width,
     * which bounds the box (Send/Stop sit to its right). win_w: the full
     * client width, which is what Send/Stop are measured from. */
    void move(int cx, int y, int cw, int win_w);

    /* WM_COMMAND for ID_SEND/ID_STOP. Non-zero if `id` was one of those,
     * same contract as the bar's and the page bar's. */
    int  command(int id);

    /* main.cpp still owns WHEN -- ui_refresh_enables calls these rather than
     * reaching into an HWND this class now keeps private. */
    void enable_send(int enable);
    void enable_stop(int enable);

    /* SetFocus(the box) -- after a send, and to forward WM_SETFOCUS. */
    void focus(void);

    /* Send `text` as an INPUT, exactly as if it had been typed and Sent:
     * same blocked check, same local echo, same tx accounting, same busy
     * state. Non-zero if it actually went.
     *
     * For the Commands menu, which is the first thing that sends a prompt
     * the user did not type. It does NOT touch the input box, so a draft
     * sitting there survives -- the command is a separate message, not an
     * edit to what you were writing. */
    int  send_text(const char *text);

    /* Put `text` in the input box, caret at the end, box focused -- for a
     * Commands entry that takes an argument, where the point is to finish
     * the line yourself.
     *
     * REFUSES rather than overwriting a draft: "the person at the keyboard
     * never loses a byte" applies to a half-written message as much as to a
     * sent one, and a menu that silently ate two paragraphs would be the
     * worst thing on this toolbar. Non-zero if the box now holds `text`. */
    int  compose(const char *text);

    /* Stage an attachment: records the relative path to be sent with the next
     * INPUT frame, and reflects it in the input box so the user can see what
     * was attached and type their question around it. */
    int  attach(const char *rel_path);
    const char *get_staged_attach(void) const { return staged_attach; }
    HWND get_box(void) const { return box; }

    /* Is the box empty, and does it have the keyboard?
     *
     * Both exist for the message loop's conditional keys. PgUp/PgDn scroll
     * the transcript, but only when taking them from the edit control costs
     * nothing -- which is exactly when there is nothing to scroll through.
     * And Ctrl+N confirms before discarding a draft, which needs to know
     * whether there IS one. The box HWND stays private; these are the two
     * questions anyone outside actually has about it. */
    int  is_empty(void) const;
    int  has_focus(void) const;
};

#endif
