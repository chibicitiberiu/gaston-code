/* The toolbar under the input box.
 *
 * It exists to answer one question the menus could not: at the moment you
 * press Send, what settings is this going to run with? A menu you have to
 * open to see a tick mark does not answer it. So the button's TEXT IS THE
 * VALUE -- "Haiku", "medium" -- and the dropdown is how you change it.
 *
 * LAYOUT, and why it is this shape. Seven labelled dropdowns were costed at
 * roughly 662px against a 640px screen, which does not fit. Three changes
 * bring it to about 367:
 *
 *   * the value alone is the label -- no "Model:" prefix
 *   * no image on the dropdowns that carry text; they have no sensible icon
 *     and the 16px bitmap plus padding is ~26px each that buys nothing
 *   * the bridge's displayName, not the resolved model id, which is 20+
 *     characters (_send_options resolves aliases deliberately; showing the
 *     short name does not undo that, it just stops shouting it)
 *
 * TWO BARS, not one. The settings bar under the input box carries Mode,
 * Model, Effort, Attach and Commands; a second one-button bar carries New
 * chat at the FAR RIGHT OF THE TOP ROW, beside the page buttons. They are different
 * kinds of thing -- one is "what will the next message run with", the other
 * is "start over" -- and putting the destructive one at the other end of the
 * window is the point rather than a layout accident. comctl32 has no
 * right-alignment, so move_top() measures and places it.
 *
 * Mode is leftmost on the settings bar because it decides whether you are
 * asked before a command runs, which outranks which model answers.
 *
 * ATTACH IS PRESENT AND DISABLED. What it does is Phase 5 -- reading a file
 * out of the mirrored folder, and capturing the screen -- and neither exists
 * yet. It is here rather than absent because this toolbar is a statement
 * about what the application can be told to do, and a capability that is
 * coming reads better greyed than missing: greyed says "not yet", absent
 * says nothing at all. Disabled rather than a dropdown of greyed items, so
 * it cannot be clicked into a menu that answers nothing.
 *
 * Commands and New chat carry an image and a label both, unlike the state
 * dropdowns: they are verbs, the comctl32 standard bitmap for each is
 * unambiguous, and the two together cost ~52px of the budget above.
 *
 * The Effort button GREYS itself when the chosen model has no effort levels
 * -- Haiku has none, and the bridge says so with one greyed menu row. An
 * earlier version HID it, which was wrong twice over: the row that exists to
 * answer "what will this run with?" had a slot silently missing from it, and
 * the button reappearing on a model change moved everything to its right.
 * Greyed is the standard way to say "not applicable here", it keeps the row
 * the same shape, and the menu behind it still explains itself.
 *
 * TBSTYLE_WRAPABLE is deliberately NOT used. With the value as the label the
 * button widths change whenever a value does, so the wrap point could flip
 * between one and two rows mid-conversation, re-running layout() and
 * resizing the MSHTML control under the reader.
 *
 * It sits BELOW the input row, which knowingly amends a rule written in
 * blood: "The input row NEVER moves" (main.cpp), added after an earlier bar
 * drawn below it covered the Stop button -- the one control you need while a
 * request is pending. That was a z-order bug; this is arithmetic, the input
 * row moves up as a whole, and Stop is positioned from the same `y`. The
 * permission bar still docks above the input, so nothing here collides with
 * it.
 */
#ifndef RC_TOOLBAR_H
#define RC_TOOLBAR_H

#ifndef __cplusplus
#error "toolbar.h is C++"
#endif

#include <windows.h>
#include "net/frameproto.h"

class rc_optmenu;

/* Its height, which layout() must reserve. 22 for the control plus a
 * gutter; measured against the 640x480 budget, this costs the transcript
 * about two lines and nothing else has to shrink. */
#define TOOLBAR_H 26

/* Command ids. Above the bar/pagebar/input ranges and below IDM_MODEL0. */
#define ID_TB_MODE   1019
#define ID_TB_MODEL  1020
#define ID_TB_EFFORT 1021
#define ID_TB_WORK   1022
#define ID_TB_ATTACH 1023
#define ID_TB_CMD    1024
#define ID_TB_NEW    1025

class rc_toolbar {
    HWND bar;       /* the settings bar, under the input box */
    HWND top;       /* New chat, right-aligned on the page-bar row */
    /* Where the paperclip landed in the toolbar's image list. Not a
     * constant: TB_ADDBITMAP appends after comctl32's stock strip and
     * returns the index, and the strip's size is not ours to assume. */
    int  clip_ix;
    rc_optmenu *opts;
    HWND owner;

    rc_toolbar(const rc_toolbar &);
    rc_toolbar &operator=(const rc_toolbar &);
    rc_toolbar() : bar(0), top(0), clip_ix(0), opts(0), owner(0) {}

public:
    /* `opts_` is borrowed, not owned: the toolbar pops that module's menus
     * rather than building its own, so there is one list and one tick. */
    static rc_toolbar *create(HWND parent, HINSTANCE inst, HFONT font,
                              rc_optmenu *opts_);
    ~rc_toolbar();

    void move(int x, int y, int w);

    /* The New chat bar, right-aligned inside [x, x+w) on the page-bar row.
     * Separate from move() because the two bars are on opposite sides of the
     * window and layout() knows both rows; this class knows neither. */
    void move_top(int x, int y, int w);

    /* Re-read the current model/effort from the option menus and re-label.
     * Called on OPTIONSEND, which is when those can have changed. */
    void refresh();

    /* Drop `id`'s menu under its button. Non-zero if `id` was one of the
     * dropdowns. Both entry points below go through it, because a click on
     * the arrow and a click on the text must do the same thing -- see
     * command(). */
    int pop(int id);

    /* WM_NOTIFY. Non-zero if this was ours (TBN_DROPDOWN on our bar). */
    int notify(NMHDR *nh);

    /* WM_COMMAND. Non-zero if `id` was one of ours. */
    int command(int id);

    HWND hwnd() const { return bar; }
};

#endif
