/* The Model and Effort menus. See app/optmenu.h for the design. */
#include "app/optmenu.h"
#include "core/str.h"
#include <string.h>

/* The Model and Effort menus are built from what the bridge sends, so the
 * lists can change without rebuilding this binary. */
static HMENU g_menu_model, g_menu_effort;
static char  g_opt_model[MAX_OPTS][64];
static char  g_opt_effort[MAX_OPTS][64];
static int   g_n_model, g_n_effort;
static int   g_opts_open;   /* mid-batch, so the next model row appends */
static HWND  g_owner;
static rc_optmenu_send_fn g_send;

/* Empty a popup without destroying it: the menu bar holds the handle. */
static void clear_popup(HMENU m)
{
    while (DeleteMenu(m, 0, MF_BYPOSITION))
        ;
}

void rc_optmenu_create(HWND owner, HMENU bar, rc_optmenu_send_fn send)
{
    g_owner = owner;
    g_send = send;

    g_menu_model = CreatePopupMenu();
    g_menu_effort = CreatePopupMenu();
    AppendMenuA(g_menu_model, MF_STRING | MF_GRAYED, 0, "(not connected)");
    AppendMenuA(g_menu_effort, MF_STRING | MF_GRAYED, 0, "(not connected)");

    AppendMenuA(bar, MF_POPUP, (UINT_PTR)g_menu_model, "&Model");
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)g_menu_effort, "&Effort");
}

int rc_optmenu_frame(rc_frame *f)
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
        if (kind && strcmp(kind, "model") == 0 && g_opts_open == 0) {
            g_opts_open = 1;
            clear_popup(g_menu_model);
            clear_popup(g_menu_effort);
            g_n_model = 0;
            g_n_effort = 0;
        }
        if (kind && id && label) {
            if (strcmp(kind, "model") == 0 && g_n_model < MAX_OPTS) {
                rc_strf(g_opt_model[g_n_model], 64, "%s", id);
                AppendMenuA(g_menu_model,
                            MF_STRING | (on ? MF_CHECKED : MF_UNCHECKED),
                            IDM_MODEL0 + g_n_model, label);
                g_n_model++;
            } else if (strcmp(kind, "effort") == 0 && g_n_effort < MAX_OPTS) {
                /* A model with no effort levels gets one greyed entry saying
                 * so, rather than a menu offering five settings it does not
                 * have. */
                const char *dis = rc_frame_get(f, "disabled");
                UINT flags = MF_STRING | (on ? MF_CHECKED : MF_UNCHECKED);
                if (dis && *dis == '1') flags |= MF_GRAYED;
                rc_strf(g_opt_effort[g_n_effort], 64, "%s", id);
                AppendMenuA(g_menu_effort, flags,
                            IDM_EFFORT0 + g_n_effort, label);
                g_n_effort++;
            }
        }
        return 1;
    }
    if (strcmp(f->type, "OPTIONSEND") == 0) {
        g_opts_open = 0;
        DrawMenuBar(g_owner);
        return 1;
    }
    return 0;
}

int rc_optmenu_command(int id)
{
    /* Model and effort are numbered ranges rather than fixed ids, because
     * the two lists are sent by the bridge and are not known when this is
     * compiled. */
    if (id >= IDM_MODEL0 && id < IDM_MODEL0 + g_n_model) {
        const char *keys[1], *vals[1];
        keys[0] = "model"; vals[0] = g_opt_model[id - IDM_MODEL0];
        g_send("The model change", "SETMODEL", keys, vals, 1, NULL, 0);
        return 1;
    }
    if (id >= IDM_EFFORT0 && id < IDM_EFFORT0 + g_n_effort) {
        const char *keys[1], *vals[1];
        keys[0] = "effort"; vals[0] = g_opt_effort[id - IDM_EFFORT0];
        g_send("The effort change", "SETEFFORT", keys, vals, 1, NULL, 0);
        return 1;
    }
    return 0;
}
