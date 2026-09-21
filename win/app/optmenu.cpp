/* The Model and Effort menus. See app/optmenu.h for the design. */
#include "app/optmenu.h"
#include "core/str.h"
#include "core/log.h"
#include "core/rc_cpp.h"
#include <string.h>

/* Empty a popup without destroying it: the menu bar holds the handle. */
static void clear_popup(HMENU m)
{
    while (DeleteMenu(m, 0, MF_BYPOSITION))
        ;
}

rc_optmenu::rc_optmenu(HWND owner_, HMENU bar, rc_optmenu_send_fn send_,
                       rc_optmenu_input_fn input_)
    : n_model(0), n_effort(0), n_mode(0), n_cmd(0), n_work(0), effort_ok(0),
      cur_mode_ix(-1), opts_open(0), owner(owner_), send(send_),
      input(input_)
{
    cur_model[0] = cur_effort[0] = cur_mode[0] = '\0';
    cur_work[0] = cur_work_short[0] = '\0';
    /* All three menus are built from what the bridge sends, so the lists can
     * change without rebuilding this binary -- which matters most for Mode,
     * where WHICH modes are offered is a safety decision made beside the
     * measurement that justifies it (bridge/server.py, MODE_ORDER). */
    menu_model = CreatePopupMenu();
    menu_effort = CreatePopupMenu();
    menu_mode = CreatePopupMenu();
    menu_cmd = CreatePopupMenu();
    menu_work = CreatePopupMenu();

    /* Attach is a DROPDOWN with File and Screenshot in it, as asked for --
     * not a plain button. Both are greyed because neither is built: sending
     * a file and capturing the screen are Phase 5, and a menu entry that
     * silently does nothing is worse than one that says it cannot yet.
     *
     * Built here, once, rather than from an OPTION batch: unlike the model
     * and command lists these two are not the bridge's to choose. They are
     * what this client will be able to do, and the bridge has no opinion. */
    menu_attach = CreatePopupMenu();
    AppendMenuA(menu_attach, MF_STRING | MF_GRAYED, IDM_ATTFILE,
                "&File...");
    AppendMenuA(menu_attach, MF_STRING | MF_GRAYED, IDM_ATTSHOT,
                "&Screenshot of this screen");
    AppendMenuA(menu_model, MF_STRING | MF_GRAYED, 0, "(not connected)");
    AppendMenuA(menu_effort, MF_STRING | MF_GRAYED, 0, "(not connected)");
    AppendMenuA(menu_mode, MF_STRING | MF_GRAYED, 0, "(not connected)");
    AppendMenuA(menu_cmd, MF_STRING | MF_GRAYED, 0, "(not connected)");

    /* Mode first: it decides whether you are asked before a command runs,
     * which outranks which model answers. "Mo&de" rather than "&Mode"
     * because "&Model" already owns Alt+M, and two menus answering the same
     * key means one of them cannot be reached from the keyboard at all. */
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)menu_mode, "Mo&de");
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)menu_model, "&Model");
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)menu_effort, "&Effort");
    /* "Comm&ands": C belongs to Configure and M to Model, so A is what is
     * left that is actually in the word. */
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)menu_cmd, "Comm&ands");
}

rc_optmenu *rc_optmenu::create(HWND owner, HMENU bar,
                               rc_optmenu_send_fn send,
                               rc_optmenu_input_fn input)
{
    return new rc_optmenu(owner, bar, send, input);
}

/* See the declaration in optmenu.h for why this exists and who calls it. */
void rc_optmenu::reset(void)
{
    opts_open = 0;
    n_model = n_effort = n_mode = n_cmd = n_work = 0;
    effort_ok = 0;
    cur_mode_ix = -1;
    cur_model[0] = cur_effort[0] = cur_mode[0] = '\0';
    cur_work[0] = cur_work_short[0] = '\0';
    clear_popup(menu_model);
    clear_popup(menu_effort);
    clear_popup(menu_mode);
    clear_popup(menu_cmd);
    clear_popup(menu_work);
    /* The same placeholders the constructor puts up, because that is exactly
     * the state this returns to: connected to nothing, offering nothing. */
    AppendMenuA(menu_model,  MF_STRING | MF_GRAYED, 0, "(not connected)");
    AppendMenuA(menu_effort, MF_STRING | MF_GRAYED, 0, "(not connected)");
    AppendMenuA(menu_mode,   MF_STRING | MF_GRAYED, 0, "(not connected)");
    AppendMenuA(menu_cmd,    MF_STRING | MF_GRAYED, 0, "(not connected)");
}

int rc_optmenu::frame(rc_frame *f)
{
    if (strcmp(f->type, "OPTION") == 0) {
        const char *kind = rc_frame_get(f, "kind");
        const char *id = rc_frame_get(f, "id");
        const char *label = rc_frame_get(f, "label");
        const char *cur = rc_frame_get(f, "current");
        int on = (cur && *cur == '1');
        /* The first row of a batch -- of ANY kind -- resets the menus.
         *
         * This used to require kind=="model", on the reasoning that the
         * bridge always sends models first, which is true. What it could not
         * survive was the batch being CUT: the bridge sends models, efforts,
         * work, modes, commands, then OPTIONSEND, so a drop anywhere in that
         * window left opts_open latched at 1 with nothing to clear it --
         * main.cpp's disconnect branch reset the bar and the sync PUT and
         * never touched this. On reconnect the whole batch was resent and
         * every row APPENDED: the Model menu listed every model twice, then
         * three times; menu_work gained a row per outage; effort_ok was never
         * cleared, so switching to Haiku left the Effort button claiming a
         * level for a model that has none. After four outages n_model hit
         * MAX_OPTS and rows were dropped -- possibly the one carrying
         * current=1, leaving the toolbar showing the bare noun "Model" for
         * the rest of the session.
         *
         * Keying on "any kind while not mid-batch" keeps the saved frame type
         * and removes the dependency on WHICH row survived the drop.
         * reset() below closes the other half, for the disconnect itself. */
        if (kind && opts_open == 0) {
            opts_open = 1;
            clear_popup(menu_model);
            clear_popup(menu_effort);
            clear_popup(menu_mode);
            clear_popup(menu_cmd);
            clear_popup(menu_work);
            cur_work[0] = cur_work_short[0] = '\0';
            n_model = 0;
            n_effort = 0;
            n_mode = 0;
            n_cmd = 0;
            n_work = 0;
            effort_ok = 0;
            cur_mode_ix = -1;
            cur_model[0] = cur_effort[0] = cur_mode[0] = '\0';
        }
        if (kind && id && label) {
            if (strcmp(kind, "model") == 0 && n_model < MAX_OPTS) {
                /* The MENU says "Haiku -- Haiku 4.5 . Fastest for quick
                 * answers"; cur_model, which is what the toolbar button
                 * shows, stays "Haiku". Four display names alone do not tell
                 * you which model is which, and the description is the
                 * bridge's, not a table here. */
                const char *desc = rc_frame_get(f, "desc");
                char item[160];
                if (desc && *desc)
                    rc_strf(item, sizeof(item), "%s -- %s", label, desc);
                else
                    rc_strf(item, sizeof(item), "%s", label);
                rc_strf(opt_model[n_model], sizeof(opt_model[n_model]), "%s", id);
                if (on) rc_strf(cur_model, sizeof(cur_model), "%s", label);
                AppendMenuA(menu_model,
                            MF_STRING | (on ? MF_CHECKED : MF_UNCHECKED),
                            IDM_MODEL0 + n_model, item);
                n_model++;
            } else if (strcmp(kind, "effort") == 0 && n_effort < MAX_OPTS) {
                /* A model with no effort levels gets one greyed entry saying
                 * so, rather than a menu offering five settings it does not
                 * have. */
                const char *dis = rc_frame_get(f, "disabled");
                UINT flags = MF_STRING | (on ? MF_CHECKED : MF_UNCHECKED);
                if (dis && *dis == '1') flags |= MF_GRAYED;
                else effort_ok = 1;
                rc_strf(opt_effort[n_effort], sizeof(opt_effort[n_effort]), "%s", id);
                if (on) rc_strf(cur_effort, sizeof(cur_effort), "%s", label);
                AppendMenuA(menu_effort, flags,
                            IDM_EFFORT0 + n_effort, label);
                n_effort++;
            } else if (strcmp(kind, "mode") == 0 && n_mode < MAX_OPTS) {
                rc_strf(opt_mode[n_mode], sizeof(opt_mode[n_mode]), "%s", id);
                if (on) {
                    rc_strf(cur_mode, sizeof(cur_mode), "%s", label);
                    cur_mode_ix = n_mode;
                }
                AppendMenuA(menu_mode,
                            MF_STRING | (on ? MF_CHECKED : MF_UNCHECKED),
                            IDM_MODE0 + n_mode, label);
                n_mode++;
            } else if (strcmp(kind, "work") == 0 && n_work < MAX_OPTS) {
                /* COUNTED, like model/effort/mode/command, and for the same
                 * reason: today's bridge sends exactly one work row, so this
                 * branch was bounded by the peer's good behaviour alone --
                 * the assumption the frame parser refuses to make everywhere
                 * else. Each row appends to menu_work, so an unbounded
                 * sender grew the menu without limit. */
                n_work++;
                /* One row, no tick: it is a fact about the conversation
                 * rather than a choice among alternatives.
                 *
                 * The BUTTON carries the full path, because "where do my
                 * files go?" is the question and a leaf answers half of it.
                 * "<unset>" until the conversation has been spoken in --
                 * the folder is named after the conversation and the CLI
                 * names the conversation from its first message. */
                /* REFUSE rather than truncate. A cut path is a DIFFERENT
                 * directory, and this one is about to be created on disk and
                 * mirrored into -- see the note on cur_work in optmenu.h.
                 * Clearing it leaves the menu saying the folder is not set,
                 * which is true of every folder this machine can address. */
                if (!rc_strf(cur_work, sizeof(cur_work), "%s", id)) {
                    rc_log("optmenu: work path is %u bytes, too long for this "
                           "machine -- refusing it rather than using a "
                           "truncated one", (unsigned)strlen(id));
                    cur_work[0] = '\0';
                }
                if (!rc_strf(cur_work_short, sizeof(cur_work_short), "%s",
                             label && *label ? label : "<unset>"))
                    rc_strf(cur_work_short, sizeof(cur_work_short), "<unset>");
                /* The path again, greyed, at the top of the menu.
                 *
                 * The BUTTON carries it too, and on a 640px screen a long
                 * one runs out of room and clips its tail -- measured:
                 * `C:\Gaston\SKYCOLOR` loses its last character. A menu has
                 * the whole width of the screen, so this is where the path
                 * is always readable. Greyed because it is information, not
                 * a command. */
                AppendMenuA(menu_work, MF_STRING | MF_GRAYED, 0,
                            cur_work[0] ? cur_work
                                        : "Not set until this conversation "
                                          "has been used");
                AppendMenuA(menu_work, MF_SEPARATOR, 0, NULL);
                AppendMenuA(menu_work, MF_STRING, IDM_WORKSET,
                            "Change &work directory...");
            } else if (strcmp(kind, "command") == 0 && n_cmd < MAX_OPTS) {
                /* No tick: a command is something you do, not a setting you
                 * are in. The label carries the argument hint and the id is
                 * the bare command -- see bridge/wire/protocol.py option(). */
                const char *hint = rc_frame_get(f, "hint");
                rc_strf(opt_cmd[n_cmd], sizeof(opt_cmd[n_cmd]), "%s", id);
                cmd_arg[n_cmd] = (char)((hint && *hint == '1') ? 1 : 0);
                AppendMenuA(menu_cmd, MF_STRING, IDM_CMD0 + n_cmd, label);
                n_cmd++;
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

void rc_optmenu::cycle_mode()
{
    if (n_mode <= 0) return;
    {
        /* From the ticked one, or from the first when nothing is ticked --
         * which is what a disconnected guest looks like after a reconnect
         * that has not sent options yet. */
        int next = (cur_mode_ix < 0) ? 0 : (cur_mode_ix + 1) % n_mode;
        const char *keys[1], *vals[1];
        keys[0] = "mode"; vals[0] = opt_mode[next];
        send("The permission mode change", "SETMODE", keys, vals, 1, NULL, 0);
    }
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
    if (id >= IDM_CMD0 && id < IDM_CMD0 + n_cmd) {
        /* Sent as a message, because that is what it is -- the CLI reads a
         * slash command out of the prompt text and there is no frame for
         * one. Going through compose-and-send rather than raw is what makes
         * it refuse mid-turn and appear in the transcript. */
        {
            int i = id - IDM_CMD0;
            if (input) {
                if (cmd_arg[i]) {
                    /* Takes an argument, so it goes in the box with a
                     * trailing space -- the menu said it wanted one and this
                     * is where you type it. */
                    char line[80];
                    rc_strf(line, sizeof(line), "%s ", opt_cmd[i]);
                    input(line, 1);
                } else {
                    input(opt_cmd[i], 0);
                }
            }
        }
        return 1;
    }
    return 0;
}
