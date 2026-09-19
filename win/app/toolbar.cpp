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
        | CCS_NOPARENTALIGN | CCS_NORESIZE | CCS_NODIVIDER,
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

    /* STD_FILENEW for New chat. Two of comctl32's own bitmaps map to
     * anything here -- there is no gauge, gavel or paperclip in them -- so
     * the rest of this toolbar is text, which is what it wanted anyway. */
    {
        TBADDBITMAP ab;
        ab.hInst = HINST_COMMCTRL;
        ab.nID = IDB_STD_SMALL_COLOR;
        SendMessageA(p->bar, TB_ADDBITMAP, 0, (LPARAM)&ab);
    }

    {
        TBBUTTON b[4];
        memset(b, 0, sizeof(b));

        /* iString is a pointer here (I_IMAGENONE for the bitmap), which is
         * what lets the caption be re-written later without rebuilding the
         * button. */
        b[0].idCommand = ID_TB_MODEL;
        b[0].iBitmap   = I_IMAGENONE;
        b[0].fsState   = TBSTATE_ENABLED;
        b[0].fsStyle   = TBSTYLE_DROPDOWN | TBSTYLE_AUTOSIZE;
        b[0].iString   = (INT_PTR)"Model";

        b[1].idCommand = ID_TB_EFFORT;
        b[1].iBitmap   = I_IMAGENONE;
        b[1].fsState   = TBSTATE_ENABLED;
        b[1].fsStyle   = TBSTYLE_DROPDOWN | TBSTYLE_AUTOSIZE;
        b[1].iString   = (INT_PTR)"default";

        b[2].fsStyle   = TBSTYLE_SEP;

        b[3].idCommand = ID_TB_NEW;
        b[3].iBitmap   = STD_FILENEW;
        b[3].fsState   = TBSTATE_ENABLED;
        b[3].fsStyle   = TBSTYLE_BUTTON | TBSTYLE_AUTOSIZE;
        b[3].iString   = (INT_PTR)"New chat";

        SendMessageA(p->bar, TB_ADDBUTTONS, 4, (LPARAM)b);
    }
    /* Straight into the same state an OPTION batch would leave: the Effort
     * button is hidden until a model that has levels is picked, and it has
     * to start hidden rather than appear and vanish on the first frame. */
    p->refresh();
    return p;
}

rc_toolbar::~rc_toolbar()
{
    /* The window is a child of the frame and Windows destroys it; nothing
     * here is subclassed, so unlike rc_chats and rc_input there is no
     * dangling proc to take down with us. */
}

void rc_toolbar::move(int x, int y, int w)
{
    if (bar) MoveWindow(bar, x, y, w, TOOLBAR_H - 4, TRUE);
}

void rc_toolbar::refresh()
{
    if (!bar || !opts) return;
    {
        const char *m = opts->model_label();
        const char *e = opts->effort_label();
        TBBUTTONINFOA bi;

        /* The fallbacks are two different states, not one.
         *
         * Model: nothing is ticked before the first OPTION batch, i.e. while
         * disconnected. The bare noun reads as "not set yet", and the menu
         * behind it says "(not connected)" in as many words.
         *
         * Effort: the button only exists at all once a model with levels is
         * picked (see the TB_HIDEBUTTON below), so an empty label here means
         * the CLI's own default is in force -- nothing was chosen and the
         * bridge has no metadata saying which level that is. "default" is
         * what _set_effort already calls that state bridge-side. */
        rc_strf(g_model_text, sizeof(g_model_text), "%s",
                (m && *m) ? m : "Model");
        rc_strf(g_effort_text, sizeof(g_effort_text), "%s",
                (e && *e) ? e : "default");

        memset(&bi, 0, sizeof(bi));
        bi.cbSize = sizeof(bi);
        bi.dwMask = TBIF_TEXT;
        bi.pszText = g_model_text;
        SendMessageA(bar, TB_SETBUTTONINFOA, ID_TB_MODEL, (LPARAM)&bi);
        bi.pszText = g_effort_text;
        SendMessageA(bar, TB_SETBUTTONINFOA, ID_TB_EFFORT, (LPARAM)&bi);

        /* Haiku has no effort levels, and the bridge says so with a single
         * greyed row. A button that can only ever read "(effort)" is worse
         * than no button, so it goes away until a model that has them is
         * picked. The menu bar's Effort popup stays -- it explains itself
         * with the greyed entry; a 56px toolbar button cannot. */
        SendMessageA(bar, TB_HIDEBUTTON, ID_TB_EFFORT,
                     MAKELONG(opts->has_effort() ? FALSE : TRUE, 0));
        SendMessageA(bar, TB_AUTOSIZE, 0, 0);
    }
}

int rc_toolbar::notify(NMHDR *nh)
{
    if (!nh || !bar || nh->hwndFrom != bar) return 0;
    if (nh->code != TBN_DROPDOWN) return 0;

    {
        NMTOOLBARA *tb = (NMTOOLBARA *)nh;
        HMENU menu = NULL;
        RECT r;

        if (tb->iItem == ID_TB_MODEL)       menu = opts ? opts->model_menu()
                                                        : NULL;
        else if (tb->iItem == ID_TB_EFFORT) menu = opts ? opts->effort_menu()
                                                        : NULL;
        if (!menu) return 1;

        /* Popped directly under the button, the way a dropdown reads. The
         * menu is the SAME HMENU the menu bar uses, so the tick and the list
         * cannot disagree with what the button says, and the WM_COMMAND ids
         * already route through rc_optmenu::command. */
        SendMessageA(bar, TB_GETRECT, (WPARAM)tb->iItem, (LPARAM)&r);
        MapWindowPoints(bar, HWND_DESKTOP, (POINT *)&r, 2);
        TrackPopupMenu(menu, TPM_LEFTALIGN | TPM_TOPALIGN, r.left, r.bottom,
                       0, owner, NULL);
    }
    return 1;
}

int rc_toolbar::command(int id)
{
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
