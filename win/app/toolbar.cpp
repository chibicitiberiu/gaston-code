/* toolbar.cpp -- the settings toolbar. See app/toolbar.h. */
#include <windows.h>
#include <commctrl.h>
#include <string.h>

#include "app/toolbar.h"
#include "app/optmenu.h"
#include "app/resource.h"
#include "core/log.h"
#include "core/str.h"
#include "core/rc_cpp.h"

/* Labels are re-written at runtime, so each needs its own buffer that
 * outlives the TB_SETBUTTONINFO call. */
static char g_mode_text[32];
/* A whole path, so it needs a path's worth of room -- the 32 this started
 * at truncated C:\Gaston\LISTFILE's longer siblings silently. */
static char g_work_text[128];
static char g_model_text[48];
static char g_effort_text[32];

rc_toolbar *rc_toolbar::create(HWND parent, HINSTANCE inst, HFONT font,
                               rc_optmenu *opts_)
{
    rc_toolbar *p = new rc_toolbar();
    if (!p) return NULL;
    p->owner = parent;
    p->opts = opts_;

    /* CCS_NOPARENTALIGN | CCS_NORESIZE | CCS_NODIVIDER are MANDATORY here.
     * A toolbar defaults to snapping itself to the top of its parent's
     * client area and redoing that on every parent WM_SIZE, which would undo
     * layout()'s MoveWindow immediately and silently -- the control would
     * simply be somewhere else. */
    p->bar = CreateWindowExA(
        0, TOOLBARCLASSNAME, NULL,
        WS_CHILD | WS_VISIBLE | TBSTYLE_FLAT | TBSTYLE_LIST
        | CCS_NOPARENTALIGN | CCS_NORESIZE | CCS_NODIVIDER
        | TBSTYLE_TOOLTIPS,
        0, 0, 0, 0, parent, (HMENU)0, inst, NULL);
    if (!p->bar) {
        rc_log("toolbar: CreateWindowEx failed (%lu)",
               (unsigned long)GetLastError());
        delete p;
        return NULL;
    }

    SendMessageA(p->bar, TB_BUTTONSTRUCTSIZE, sizeof(TBBUTTON), 0);
    /* The arrow on a dropdown button, so it reads as one rather than as a
     * button with a long caption. */
    SendMessageA(p->bar, TB_SETEXTENDEDSTYLE, 0, TBSTYLE_EX_DRAWDDARROWS);
    if (font) SendMessageA(p->bar, WM_SETFONT, (WPARAM)font, TRUE);

    /* STD_FILENEW for New chat, STD_PROPERTIES for Commands. Only two of
     * comctl32's own bitmaps map to anything here -- there is no gauge,
     * gavel or paperclip in them -- so the rest of this toolbar is text,
     * which is what it wanted anyway.
     *
     * Except the paperclip, which is ours. Added SECOND, because
     * TB_ADDBITMAP appends and returns the index of the first image it
     * added: that index is what the Attach button has to name, and hardcoding
     * 15 (the stock strip's count) would break the day comctl32 ships a
     * sixteenth. */
    {
        TBADDBITMAP ab;
        ab.hInst = HINST_COMMCTRL;
        ab.nID = IDB_STD_SMALL_COLOR;
        SendMessageA(p->bar, TB_ADDBITMAP, 0, (LPARAM)&ab);

        ab.hInst = inst;
        ab.nID = IDB_ATTACH;
        p->clip_ix = (int)SendMessageA(p->bar, TB_ADDBITMAP, 1, (LPARAM)&ab);
        if (p->clip_ix < 0) {
            rc_log("toolbar: the Attach bitmap did not load; using the "
                   "folder from comctl32 instead");
            p->clip_ix = STD_FILEOPEN;
        }
    }

    {
        TBBUTTON b[7];
        memset(b, 0, sizeof(b));

        /* iString is a pointer here (I_IMAGENONE for the bitmap), which is
         * what lets the caption be re-written later without rebuilding the
         * button. */
        b[0].idCommand = ID_TB_MODE;
        b[0].iBitmap   = I_IMAGENONE;
        b[0].fsState   = TBSTATE_ENABLED;
        b[0].fsStyle   = TBSTYLE_DROPDOWN | TBSTYLE_AUTOSIZE;
        b[0].iString   = (INT_PTR)"Mode";

        b[1].idCommand = ID_TB_MODEL;
        b[1].iBitmap   = I_IMAGENONE;
        b[1].fsState   = TBSTATE_ENABLED;
        b[1].fsStyle   = TBSTYLE_DROPDOWN | TBSTYLE_AUTOSIZE;
        b[1].iString   = (INT_PTR)"Model";

        b[2].idCommand = ID_TB_EFFORT;
        b[2].iBitmap   = I_IMAGENONE;
        b[2].fsState   = TBSTATE_ENABLED;
        b[2].fsStyle   = TBSTYLE_DROPDOWN | TBSTYLE_AUTOSIZE;
        b[2].iString   = (INT_PTR)"default";

        b[3].fsStyle   = TBSTYLE_SEP;

        /* A DROPDOWN with File and Screenshot in it, as asked for. Enabled,
         * because the menu is what explains itself -- both entries are
         * greyed, which says "this is where it will be" rather than leaving
         * a dead button with no way to find out what it was for. */
        b[4].idCommand = ID_TB_ATTACH;
        b[4].iBitmap   = p->clip_ix;
        b[4].fsState   = TBSTATE_ENABLED;
        b[4].fsStyle   = TBSTYLE_DROPDOWN | TBSTYLE_AUTOSIZE;
        b[4].iString   = (INT_PTR)"Attach";

        b[5].idCommand = ID_TB_CMD;
        b[5].iBitmap   = STD_PROPERTIES;
        b[5].fsState   = TBSTATE_ENABLED;
        b[5].fsStyle   = TBSTYLE_DROPDOWN | TBSTYLE_AUTOSIZE;
        b[5].iString   = (INT_PTR)"Commands";

        /* LAST, and the five before it are in the order that was asked for:
         * mode, model, effort, attach, commands. This one carries a whole
         * path and is the widest thing on the bar, so it goes where running
         * out of room clips IT rather than everything to its right. */
        b[6].idCommand = ID_TB_WORK;
        b[6].iBitmap   = I_IMAGENONE;
        b[6].fsState   = TBSTATE_ENABLED;
        b[6].fsStyle   = TBSTYLE_DROPDOWN | TBSTYLE_AUTOSIZE;
        b[6].iString   = (INT_PTR)"<unset>";

        SendMessageA(p->bar, TB_ADDBUTTONS, 7, (LPARAM)b);
    }
    /* New chat, on its own bar so it can sit at the other end of the window
     * from the settings -- see the header. Its failure is not fatal: the
     * File menu and Ctrl+N both still start a conversation. */
    p->top = CreateWindowExA(
        0, TOOLBARCLASSNAME, NULL,
        WS_CHILD | WS_VISIBLE | TBSTYLE_FLAT | TBSTYLE_LIST
        | CCS_NOPARENTALIGN | CCS_NORESIZE | CCS_NODIVIDER
        | TBSTYLE_TOOLTIPS,
        0, 0, 0, 0, parent, (HMENU)0, inst, NULL);
    if (p->top) {
        TBBUTTON n;
        TBADDBITMAP ab;
        SendMessageA(p->top, TB_BUTTONSTRUCTSIZE, sizeof(TBBUTTON), 0);
        if (font) SendMessageA(p->top, WM_SETFONT, (WPARAM)font, TRUE);
        ab.hInst = HINST_COMMCTRL;
        ab.nID = IDB_STD_SMALL_COLOR;
        SendMessageA(p->top, TB_ADDBITMAP, 0, (LPARAM)&ab);
        memset(&n, 0, sizeof(n));
        n.idCommand = ID_TB_NEW;
        n.iBitmap   = STD_FILENEW;
        n.fsState   = TBSTATE_ENABLED;
        n.fsStyle   = TBSTYLE_BUTTON | TBSTYLE_AUTOSIZE;
        n.iString   = (INT_PTR)"New chat";
        SendMessageA(p->top, TB_ADDBUTTONS, 1, (LPARAM)&n);
        SendMessageA(p->top, TB_AUTOSIZE, 0, 0);
    } else {
        rc_log("toolbar: New chat bar not created (%lu)",
               (unsigned long)GetLastError());
    }

    /* Straight into the same state an OPTION batch would leave: the Effort
     * button is hidden until a model that has levels is picked, and it has
     * to start hidden rather than appear and vanish on the first frame. */
    p->refresh();
    return p;
}

rc_toolbar::~rc_toolbar()
{
    /* Both windows are children of the frame and Windows destroys them;
     * nothing here is subclassed, so unlike rc_chats and rc_input there is
     * no dangling proc to take down with us. */
}

void rc_toolbar::move(int x, int y, int w)
{
    if (bar) MoveWindow(bar, x, y, w, TOOLBAR_H - 4, TRUE);
}

void rc_toolbar::move_top(int x, int y, int w)
{
    RECT r;
    int bw;

    if (!top) return;
    /* Measured, not assumed: the caption is "New chat" in whatever the
     * system font is, and TB_GETRECT is the only thing that knows how wide
     * that came out. Right-aligned by subtracting it from the far edge --
     * comctl32 has no alignment of its own. */
    if (!SendMessageA(top, TB_GETRECT, (WPARAM)ID_TB_NEW, (LPARAM)&r))
        return;
    bw = r.right - r.left;
    if (bw <= 0 || bw > w) bw = w;
    MoveWindow(top, x + w - bw, y, bw, TOOLBAR_H - 4, TRUE);
}

void rc_toolbar::refresh()
{
    if (!bar || !opts) return;
    {
        const char *d = opts->mode_label();
        const char *m = opts->model_label();
        const char *e = opts->effort_label();
        const char *w = opts->work_label();
        TBBUTTONINFOA bi;

        /* The fallbacks are three different states, not one.
         *
         * Mode and Model: nothing is ticked before the first OPTION batch,
         * i.e. while disconnected. The bare noun reads as "not set yet", and
         * the menu behind it says "(not connected)" in as many words. Mode
         * never falls back once connected -- the bridge always ticks one of
         * the three.
         *
         * Effort: the button only exists at all once a model with levels is
         * picked (see the TB_ENABLEBUTTON below), so an empty label means
         * the CLI's own default is in force -- nothing was chosen and the
         * bridge has no metadata saying which level that is. "default" is
         * what _set_effort already calls that state bridge-side. */
        rc_strf(g_mode_text, sizeof(g_mode_text), "%s",
                (d && *d) ? d : "Mode");
        rc_strf(g_model_text, sizeof(g_model_text), "%s",
                (m && *m) ? m : "Model");
        /* The NOUN when the model has no levels: a greyed button showing
         * "Effort" says the setting does not apply here, where a greyed
         * button showing a value would claim a value is in force. */
        rc_strf(g_effort_text, sizeof(g_effort_text), "%s",
                !opts->has_effort() ? "Effort"
                                    : ((e && *e) ? e : "default"));
        /* THE WHOLE PATH, which is what was asked for and is the
         * right answer: "where do my files go?" is the question,
         * and a leaf on its own answers half of it on a machine
         * where the user is going to go and open that folder.
         * "<unset>" until the conversation has a name to take one
         * from -- the CLI titles it from the first message. */
        rc_strf(g_work_text, sizeof(g_work_text), "%s",
                (w && *w) ? w : "<unset>");

        memset(&bi, 0, sizeof(bi));
        bi.cbSize = sizeof(bi);
        bi.dwMask = TBIF_TEXT;
        bi.pszText = g_mode_text;
        SendMessageA(bar, TB_SETBUTTONINFOA, ID_TB_MODE, (LPARAM)&bi);
        bi.pszText = g_model_text;
        SendMessageA(bar, TB_SETBUTTONINFOA, ID_TB_MODEL, (LPARAM)&bi);
        bi.pszText = g_effort_text;
        SendMessageA(bar, TB_SETBUTTONINFOA, ID_TB_EFFORT, (LPARAM)&bi);
        bi.pszText = g_work_text;
        SendMessageA(bar, TB_SETBUTTONINFOA, ID_TB_WORK, (LPARAM)&bi);

        /* GREYED, not hidden. Haiku has no effort levels and the bridge says
         * so with one greyed menu row; this says the same thing the same
         * way. Hiding it took a slot out of the one row whose whole job is
         * to show what the next message will run with, and moved every
         * button to its right whenever the model changed. */
        SendMessageA(bar, TB_ENABLEBUTTON, ID_TB_EFFORT,
                     MAKELONG(opts->has_effort() ? TRUE : FALSE, 0));
        SendMessageA(bar, TB_AUTOSIZE, 0, 0);
    }
}

int rc_toolbar::pop(int id)
{
    HMENU menu = NULL;
    RECT r;

    if (!bar || !opts) return 0;
    if (id == ID_TB_MODE)        menu = opts->mode_menu();
    else if (id == ID_TB_MODEL)  menu = opts->model_menu();
    else if (id == ID_TB_EFFORT) menu = opts->effort_menu();
    else if (id == ID_TB_WORK)   menu = opts->work_menu();
    else if (id == ID_TB_ATTACH) menu = opts->attach_menu();
    else if (id == ID_TB_CMD)    menu = opts->cmd_menu();
    if (!menu) return 0;

    /* Popped directly under the button, the way a dropdown reads. The menu
     * is the SAME HMENU the menu bar uses, so the tick and the list cannot
     * disagree with what the button says, and the WM_COMMAND ids already
     * route through rc_optmenu::command. */
    SendMessageA(bar, TB_GETRECT, (WPARAM)id, (LPARAM)&r);
    MapWindowPoints(bar, HWND_DESKTOP, (POINT *)&r, 2);
    TrackPopupMenu(menu, TPM_LEFTALIGN | TPM_TOPALIGN, r.left, r.bottom,
                   0, owner, NULL);
    return 1;
}

/* One sentence per button, for the tooltip.
 *
 * The buttons say what they ARE set to -- "Haiku", "Accept edits" -- which is
 * the whole point of the bar and also means nothing on them says what they
 * are FOR. That is what a tooltip is for, and it is the only room on a 640px
 * screen to say it. Attach's is the one that earns its keep: a greyed button
 * with no explanation is just a dead control. */
static const char *tip_for(int id)
{
    switch (id) {
    case ID_TB_MODE:
        return "Whether you are asked before anything runs on this computer";
    case ID_TB_MODEL:
        return "Which model answers";
    case ID_TB_EFFORT:
        return "How hard the model works before replying. Not every model "
               "has this.";
    case ID_TB_WORK:
        return "Where this conversation's files live on this computer";
    case ID_TB_ATTACH:
        return "Send a file or a screenshot to the assistant";
    case ID_TB_CMD:
        return "Commands the assistant understands, such as /compact";
    case ID_TB_NEW:
        return "Start a new conversation (Ctrl+N)";
    }
    return NULL;
}

int rc_toolbar::notify(NMHDR *nh)
{
    if (!nh) return 0;

    /* TTN_NEEDTEXT comes from the TOOLTIP control the toolbar made, not from
     * the toolbar, so the hwndFrom check below would throw it away. It is
     * matched on the code instead, and `idFrom` is the command id. */
    if (nh->code == TTN_NEEDTEXTA) {
        NMTTDISPINFOA *ti = (NMTTDISPINFOA *)nh;
        const char *text = tip_for((int)nh->idFrom);
        if (!text) return 0;
        /* lpszText, not szText: szText is 80 bytes and these do not fit.
         * The string is a literal with static lifetime, which is what
         * lpszText requires -- the control reads it after this returns. */
        ti->lpszText = (LPSTR)text;
        ti->hinst = NULL;
        return 1;
    }

    if (!bar || nh->hwndFrom != bar) return 0;   /* top has no menu */
    if (nh->code != TBN_DROPDOWN) return 0;
    pop((int)((NMTOOLBARA *)nh)->iItem);
    return 1;
}

int rc_toolbar::command(int id)
{
    /* THE WHOLE BUTTON DROPS THE MENU, not just the arrow.
     *
     * With TBSTYLE_EX_DRAWDDARROWS a TBSTYLE_DROPDOWN button splits in two:
     * the arrow sends TBN_DROPDOWN and the rest sends a plain WM_COMMAND.
     * That split is right for a button whose body does something on its own
     * -- Back, in a browser -- and wrong for every button here, where the
     * text IS the current value and there is nothing else it could mean.
     * Left alone, clicking "Commands" or "Haiku" did nothing at all.
     *
     * BTNS_WHOLEDROPDOWN would say this to comctl32 directly, and is NOT
     * used: the constant is gated behind _WIN32_IE >= 0x0500 and this build
     * pins 0x0400, so it would mean hardcoding 0x80 and betting the target
     * has IE5's comctl32. This guest does; a Win98 box with IE4 does not,
     * and the failure would be silent. Catching the WM_COMMAND costs one
     * branch and works on both. */
    if (pop(id)) return 1;

    if (id == ID_TB_NEW) {
        /* Routed to the menu command rather than duplicating it: IDM_NEW
         * already confirms when a turn is running and when the input box
         * holds an unsent draft, and a second path that skipped either would
         * be the bug those confirmations exist to prevent. */
        SendMessageA(owner, WM_COMMAND, IDM_NEW, 0);
        return 1;
    }
    return 0;
}
