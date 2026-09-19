/* The Model and Effort menus. See app/optmenu.h for the design. */
#include "app/optmenu.h"
#include "core/str.h"
#include "core/rc_cpp.h"
#include <string.h>

/* Empty a popup without destroying it: the menu bar holds the handle. */
static void clear_popup(HMENU m)
{
    while (DeleteMenu(m, 0, MF_BYPOSITION))
        ;
}

rc_optmenu::rc_optmenu(HWND owner_, HMENU bar, rc_optmenu_send_fn send_)
    : n_model(0), n_effort(0), n_mode(0), effort_ok(0), opts_open(0),
      owner(owner_), send(send_)
{
    cur_model[0] = cur_effort[0] = cur_mode[0] = '\0';
    /* All three menus are built from what the bridge sends, so the lists can
     * change without rebuilding this binary -- which matters most for Mode,
     * where WHICH modes are offered is a safety decision made beside the
     * measurement that justifies it (bridge/server.py, MODE_ORDER). */
    menu_model = CreatePopupMenu();
    menu_effort = CreatePopupMenu();
    menu_mode = CreatePopupMenu();
    AppendMenuA(menu_model, MF_STRING | MF_GRAYED, 0, "(not connected)");
    AppendMenuA(menu_effort, MF_STRING | MF_GRAYED, 0, "(not connected)");
    AppendMenuA(menu_mode, MF_STRING | MF_GRAYED, 0, "(not connected)");

    /* Mode first: it decides whether you are asked before a command runs,
     * which outranks which model answers. "Mo&de" rather than "&Mode"
     * because "&Model" already owns Alt+M, and two menus answering the same
     * key means one of them cannot be reached from the keyboard at all. */
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)menu_mode, "Mo&de");
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)menu_model, "&Model");
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)menu_effort, "&Effort");
}

rc_optmenu *rc_optmenu::create(HWND owner, HMENU bar, rc_optmenu_send_fn send)
{
    return new rc_optmenu(owner, bar, send);
}

int rc_optmenu::frame(rc_frame *f)
{
    if (strcmp(f->type, "OPTION") == 0) {
        const char *kind = rc_frame_get(f, "kind");
        const char *id = rc_frame_get(f, "id");
        const char *label = rc_frame_get(f, "label");
        const char *cur = rc_frame_get(f, "current");
        int on = (cur && *cur == '1');
        /* The first model row of a batch resets both menus. The bridge
         * always sends models before efforts, so this is a reliable start
         * marker and saves a frame type. */
        if (kind && strcmp(kind, "model") == 0 && opts_open == 0) {
            opts_open = 1;
            clear_popup(menu_model);
            clear_popup(menu_effort);
            clear_popup(menu_mode);
            n_model = 0;
            n_effort = 0;
            n_mode = 0;
            effort_ok = 0;
            cur_model[0] = cur_effort[0] = cur_mode[0] = '\0';
        }
        if (kind && id && label) {
            if (strcmp(kind, "model") == 0 && n_model < MAX_OPTS) {
                rc_strf(opt_model[n_model], 64, "%s", id);
                if (on) rc_strf(cur_model, sizeof(cur_model), "%s", label);
                AppendMenuA(menu_model,
                            MF_STRING | (on ? MF_CHECKED : MF_UNCHECKED),
                            IDM_MODEL0 + n_model, label);
                n_model++;
            } else if (strcmp(kind, "effort") == 0 && n_effort < MAX_OPTS) {
                /* A model with no effort levels gets one greyed entry saying
                 * so, rather than a menu offering five settings it does not
                 * have. */
                const char *dis = rc_frame_get(f, "disabled");
                UINT flags = MF_STRING | (on ? MF_CHECKED : MF_UNCHECKED);
                if (dis && *dis == '1') flags |= MF_GRAYED;
                else effort_ok = 1;
                rc_strf(opt_effort[n_effort], 64, "%s", id);
                if (on) rc_strf(cur_effort, sizeof(cur_effort), "%s", label);
                AppendMenuA(menu_effort, flags,
                            IDM_EFFORT0 + n_effort, label);
                n_effort++;
            } else if (strcmp(kind, "mode") == 0 && n_mode < MAX_OPTS) {
                rc_strf(opt_mode[n_mode], 64, "%s", id);
                if (on) rc_strf(cur_mode, sizeof(cur_mode), "%s", label);
                AppendMenuA(menu_mode,
                            MF_STRING | (on ? MF_CHECKED : MF_UNCHECKED),
                            IDM_MODE0 + n_mode, label);
                n_mode++;
            }
        }
        return 1;
    }
    if (strcmp(f->type, "OPTIONSEND") == 0) {
        opts_open = 0;
        DrawMenuBar(owner);
        return 1;
    }
    return 0;
}

int rc_optmenu::command(int id)
{
    /* Model and effort are numbered ranges rather than fixed ids, because
     * the two lists are sent by the bridge and are not known when this is
     * compiled. */
    if (id >= IDM_MODEL0 && id < IDM_MODEL0 + n_model) {
        const char *keys[1], *vals[1];
        keys[0] = "model"; vals[0] = opt_model[id - IDM_MODEL0];
        send("The model change", "SETMODEL", keys, vals, 1, NULL, 0);
        return 1;
    }
    if (id >= IDM_EFFORT0 && id < IDM_EFFORT0 + n_effort) {
        const char *keys[1], *vals[1];
        keys[0] = "effort"; vals[0] = opt_effort[id - IDM_EFFORT0];
        send("The effort change", "SETEFFORT", keys, vals, 1, NULL, 0);
        return 1;
    }
    if (id >= IDM_MODE0 && id < IDM_MODE0 + n_mode) {
        const char *keys[1], *vals[1];
        keys[0] = "mode"; vals[0] = opt_mode[id - IDM_MODE0];
        send("The permission mode change", "SETMODE", keys, vals, 1, NULL, 0);
        return 1;
    }
    return 0;
}
