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

OptionMenus::OptionMenus(HWND owner, HMENU bar, FrameSender *sender,
                         OptionMenusListener *listener)
    : m_workCount(0), m_effortOk(0), m_curModeIx(-1), m_optsOpen(0),
      m_owner(owner), m_sender(sender), m_listener(listener)
{
    /* All three menus are built from what the bridge sends, so the lists can
     * change without rebuilding this binary -- which matters most for Mode,
     * where WHICH modes are offered is a safety decision made beside the
     * measurement that justifies it (bridge/server.py, MODE_ORDER). */
    m_modelMenu = CreatePopupMenu();
    m_effortMenu = CreatePopupMenu();
    m_modeMenu = CreatePopupMenu();
    m_commandMenu = CreatePopupMenu();
    m_workMenu = CreatePopupMenu();

    /* Attach is a DROPDOWN with File and Screenshot in it, as asked for --
     * not a plain button. Both are greyed because neither is built: sending
     * a file and capturing the screen are Phase 5, and a menu entry that
     * silently does nothing is worse than one that says it cannot yet.
     *
     * Built here, once, rather than from an OPTION batch: unlike the model
     * and command lists these two are not the bridge's to choose. They are
     * what this client will be able to do, and the bridge has no opinion. */
    m_attachMenu = CreatePopupMenu();
    AppendMenuA(m_attachMenu, MF_STRING, IDM_ATTFILE,
                "&File...");
    AppendMenuA(m_attachMenu, MF_STRING, IDM_ATTSHOT,
                "&Screenshot of this screen");
    AppendMenuA(m_modelMenu, MF_STRING | MF_GRAYED, 0, "(not connected)");
    AppendMenuA(m_effortMenu, MF_STRING | MF_GRAYED, 0, "(not connected)");
    AppendMenuA(m_modeMenu, MF_STRING | MF_GRAYED, 0, "(not connected)");
    AppendMenuA(m_commandMenu, MF_STRING | MF_GRAYED, 0, "(not connected)");

    /* Mode first: it decides whether you are asked before a command runs,
     * which outranks which model answers. "Mo&de" rather than "&Mode"
     * because "&Model" already owns Alt+M, and two menus answering the same
     * key means one of them cannot be reached from the keyboard at all. */
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)m_modeMenu, "Mo&de");
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)m_modelMenu, "&Model");
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)m_effortMenu, "&Effort");
    /* "Comm&ands": C belongs to Configure and M to Model, so A is what is
     * left that is actually in the word. */
    AppendMenuA(bar, MF_POPUP, (UINT_PTR)m_commandMenu, "Comm&ands");
}

OptionMenus *OptionMenus::create(HWND owner, HMENU bar, FrameSender *sender,
                                 OptionMenusListener *listener)
{
    return new OptionMenus(owner, bar, sender, listener);
}

/* See the declaration in optmenu.h for why this exists and who calls it. */
void OptionMenus::reset()
{
    m_optsOpen = 0;
    m_models.clear();
    m_efforts.clear();
    m_modes.clear();
    m_commands.clear();
    m_workCount = 0;
    m_effortOk = 0;
    m_curModeIx = -1;
    m_curModel.erase();
    m_curEffort.erase();
    m_curMode.erase();
    m_curWork.erase();
    m_curWorkShort.erase();
    clear_popup(m_modelMenu);
    clear_popup(m_effortMenu);
    clear_popup(m_modeMenu);
    clear_popup(m_commandMenu);
    clear_popup(m_workMenu);
    /* The same placeholders the constructor puts up, because that is exactly
     * the state this returns to: connected to nothing, offering nothing. */
    AppendMenuA(m_modelMenu,  MF_STRING | MF_GRAYED, 0, "(not connected)");
    AppendMenuA(m_effortMenu, MF_STRING | MF_GRAYED, 0, "(not connected)");
    AppendMenuA(m_modeMenu,   MF_STRING | MF_GRAYED, 0, "(not connected)");
    AppendMenuA(m_commandMenu,    MF_STRING | MF_GRAYED, 0, "(not connected)");
}

bool OptionMenus::frame(rc_frame *f)
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
        if (kind && m_optsOpen == 0) {
            m_optsOpen = 1;
            clear_popup(m_modelMenu);
            clear_popup(m_effortMenu);
            clear_popup(m_modeMenu);
            clear_popup(m_commandMenu);
            clear_popup(m_workMenu);
            m_curWork.erase();
            m_curWorkShort.erase();
            m_models.clear();
            m_efforts.clear();
            m_modes.clear();
            m_commands.clear();
            m_workCount = 0;
            m_effortOk = 0;
            m_curModeIx = -1;
            m_curModel.erase();
            m_curEffort.erase();
            m_curMode.erase();
        }
        if (kind && id && label) {
            if (strcmp(kind, "model") == 0 && m_models.size() < MAX_OPTS) {
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
                if (on) m_curModel = label;
                AppendMenuA(m_modelMenu,
                            MF_STRING | (on ? MF_CHECKED : MF_UNCHECKED),
                            IDM_MODEL0 + m_models.size(), item);
                m_models.push_back(id);
            } else if (strcmp(kind, "effort") == 0 && m_efforts.size() < MAX_OPTS) {
                /* A model with no effort levels gets one greyed entry saying
                 * so, rather than a menu offering five settings it does not
                 * have. */
                const char *dis = rc_frame_get(f, "disabled");
                UINT flags = MF_STRING | (on ? MF_CHECKED : MF_UNCHECKED);
                if (dis && *dis == '1') flags |= MF_GRAYED;
                else m_effortOk = 1;
                if (on) m_curEffort = label;
                AppendMenuA(m_effortMenu, flags,
                            IDM_EFFORT0 + m_efforts.size(), label);
                m_efforts.push_back(id);
            } else if (strcmp(kind, "mode") == 0 && m_modes.size() < MAX_OPTS) {
                if (on) {
                    m_curMode = label;
                    m_curModeIx = (int)m_modes.size();
                }
                AppendMenuA(m_modeMenu,
                            MF_STRING | (on ? MF_CHECKED : MF_UNCHECKED),
                            IDM_MODE0 + m_modes.size(), label);
                m_modes.push_back(id);
            } else if (strcmp(kind, "work") == 0 && m_workCount < MAX_OPTS) {
                /* COUNTED, like model/effort/mode/command, and for the same
                 * reason: today's bridge sends exactly one work row, so this
                 * branch was bounded by the peer's good behaviour alone --
                 * the assumption the frame parser refuses to make everywhere
                 * else. Each row appends to menu_work, so an unbounded
                 * sender grew the menu without limit. */
                m_workCount++;
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
                if (strlen(id) >= MAX_PATH) {
                    rc_log("optmenu: work path is %u bytes, too long for this "
                           "machine -- refusing it rather than using a "
                           "truncated one", (unsigned)strlen(id));
                    m_curWork.erase();
                } else {
                    m_curWork = id;
                }
                m_curWorkShort = label && *label ? label : "<unset>";
                /* The path again, greyed, at the top of the menu.
                 *
                 * The BUTTON carries it too, and on a 640px screen a long
                 * one runs out of room and clips its tail -- measured:
                 * `C:\Gaston\SKYCOLOR` loses its last character. A menu has
                 * the whole width of the screen, so this is where the path
                 * is always readable. Greyed because it is information, not
                 * a command. */
                AppendMenuA(m_workMenu, MF_STRING | MF_GRAYED, 0,
                            !m_curWork.empty() ? m_curWork.c_str()
                                        : "Not set until this conversation "
                                          "has been used");
                AppendMenuA(m_workMenu, MF_SEPARATOR, 0, NULL);
                AppendMenuA(m_workMenu, !m_curWork.empty() ? MF_STRING : MF_GRAYED,
                            IDM_WORKOPEN, "&Open work directory");
                AppendMenuA(m_workMenu, MF_STRING, IDM_WORKSET,
                            "Change &work directory...");
            } else if (strcmp(kind, "command") == 0 && m_commands.size() < MAX_OPTS) {
                /* No tick: a command is something you do, not a setting you
                 * are in. The label carries the argument hint and the id is
                 * the bare command -- see bridge/wire/protocol.py option(). */
                const char *hint = rc_frame_get(f, "hint");
                Command command;
                command.id = id;
                command.takesArg = hint && *hint == '1';
                AppendMenuA(m_commandMenu, MF_STRING,
                            IDM_CMD0 + m_commands.size(), label);
                m_commands.push_back(command);
            }
        }
        return true;
    }
    if (strcmp(f->type, "OPTIONSEND") == 0) {
        m_optsOpen = 0;
        DrawMenuBar(m_owner);
        return true;
    }
    return false;
}

void OptionMenus::cycleMode()
{
    if (m_modes.empty()) return;
    {
        /* From the ticked one, or from the first when nothing is ticked --
         * which is what a disconnected guest looks like after a reconnect
         * that has not sent options yet. */
        int next = (m_curModeIx < 0) ? 0 : (m_curModeIx + 1) % (int)m_modes.size();
        const char *keys[1], *vals[1];
        keys[0] = "mode"; vals[0] = m_modes[next].c_str();
        m_sender->sendFrame("The permission mode change", "SETMODE", keys, vals, 1, NULL, 0);
    }
}

bool OptionMenus::command(int id)
{
    /* Model and effort are numbered ranges rather than fixed ids, because
     * the two lists are sent by the bridge and are not known when this is
     * compiled. */
    if (id >= IDM_MODEL0 && id < IDM_MODEL0 + (int)m_models.size()) {
        const char *keys[1], *vals[1];
        keys[0] = "model"; vals[0] = m_models[id - IDM_MODEL0].c_str();
        m_sender->sendFrame("The model change", "SETMODEL", keys, vals, 1, NULL, 0);
        return true;
    }
    if (id >= IDM_EFFORT0 && id < IDM_EFFORT0 + (int)m_efforts.size()) {
        const char *keys[1], *vals[1];
        keys[0] = "effort"; vals[0] = m_efforts[id - IDM_EFFORT0].c_str();
        m_sender->sendFrame("The effort change", "SETEFFORT", keys, vals, 1, NULL, 0);
        return true;
    }
    if (id >= IDM_MODE0 && id < IDM_MODE0 + (int)m_modes.size()) {
        const char *keys[1], *vals[1];
        keys[0] = "mode"; vals[0] = m_modes[id - IDM_MODE0].c_str();
        m_sender->sendFrame("The permission mode change", "SETMODE", keys, vals, 1, NULL, 0);
        return true;
    }
    if (id >= IDM_CMD0 && id < IDM_CMD0 + (int)m_commands.size()) {
        /* Sent as a message, because that is what it is -- the CLI reads a
         * slash command out of the prompt text and there is no frame for
         * one. Going through compose-and-send rather than raw is what makes
         * it refuse mid-turn and appear in the transcript. */
        {
            int i = id - IDM_CMD0;
            if (m_listener) {
                if (m_commands[i].takesArg) {
                    /* Takes an argument, so it goes in the box with a
                     * trailing space -- the menu said it wanted one and this
                     * is where you type it. */
                    std::string line = m_commands[i].id + " ";
                    m_listener->submitCommand(line.c_str(), true);
                } else {
                    m_listener->submitCommand(m_commands[i].id.c_str(), false);
                }
            }
        }
        return true;
    }
    return false;
}
