/* The Mode, Model, Effort and Commands menus: built from what the bridge
 * sends, so the lists can change without rebuilding this binary.
 *
 * Four now, not two. Three are settings with a ticked current value; the
 * fourth is a list of slash commands, which is not a setting at all and has
 * no tick. It lives here anyway because it is the same OPTION frame, the
 * same batch reset, the same id arithmetic and the same WM_COMMAND routing
 * -- a fourth copy of that machinery elsewhere would be the duplication this
 * module exists to have prevented.
 *
 * The last of the three remaining §1.2 extraction targets out of main.c
 * (audit finding 01.58) -- the bar, the chat list and the page bar are
 * already their own files. This is the one the audit called out as most
 * obviously wanting to be one module: the id-range arithmetic in
 * OptionMenus::command and the array fill in OptionMenus::frame have to agree
 * about MAX_OPTS and the base constants, and used to be 970 lines apart in
 * main.c.
 *
 * A CLASS NOW, not a handle plus three free functions (audit finding 01.60):
 * same reasoning as app/htmlview.h, the pattern this module and its five
 * siblings follow. There is exactly one instance, created explicitly by
 * main.cpp's build_menu at menu-build time (never a file-scope object --
 * see core/rc_cpp.h) and destroyed with `delete`.
 */
#ifndef RC_OPTMENU_H
#define RC_OPTMENU_H

#include <windows.h>
#include "net/frameproto.h"
#include "app/sender.h"

#ifndef __cplusplus
#error "optmenu.h is C++; the menus are a class now, see app/htmlview.h"
#endif

/* Model and effort entries are numbered from a base because the lists come
 * from the bridge and are not known at compile time. */
#define IDM_MODEL0  1200      /* .. IDM_MODEL0 + MAX_OPTS */
#define IDM_EFFORT0 1300      /* .. IDM_EFFORT0 + MAX_OPTS */
#define IDM_MODE0   1400      /* .. IDM_MODE0 + MAX_OPTS */
#define IDM_CMD0    1500      /* .. IDM_CMD0 + MAX_OPTS */
#define IDM_WORKOPEN 1598     /* "Open work directory" */
#define IDM_WORKSET 1599      /* "Change work directory..." */
#define IDM_ATTFILE 1600      /* Attach > File... */
#define IDM_ATTSHOT 1601      /* Attach > Screenshot */
#define MAX_OPTS    16

/* "Send this as if it had been typed" -- for the Commands menu, which sends
 * a slash command as a prompt (the CLI reads it out of the message text;
 * there is no frame for one). compose=true puts the text in the input box
 * instead, for a command that takes an argument. True if it went. */
class OptionMenusListener
{
public:
    virtual ~OptionMenusListener() {}
    virtual bool submitCommand(const char *text, bool compose) = 0;
};


class OptionMenus {
    HMENU m_modelMenu, m_effortMenu, m_modeMenu, m_commandMenu, m_workMenu;
    HMENU m_attachMenu;
    char  m_models[MAX_OPTS][64];
    char  m_efforts[MAX_OPTS][64];
    /* Permission mode: Manual / Accept edits / Plan. The list is the
     * BRIDGE'S, not a table here, for the same reason the model list is --
     * `auto` and `bypassPermissions` are withheld for measured reasons that
     * belong beside the measurement (bridge/server.py, MODE_ORDER), not in
     * a menu builder on a 32MB machine. */
    char  m_modes[MAX_OPTS][64];
    /* Slash commands. Not an option -- nothing is ticked and there is no
     * current value -- but the same bridge-driven menu with the same id
     * arithmetic and the same WM_COMMAND routing, so it lives here rather
     * than as a fourth copy of that machinery somewhere else. WHICH commands
     * appear is the bridge's decision (bridge/server.py, COMMAND_MENU): five
     * of the fifty-three the CLI offers. */
    char  m_commands[MAX_OPTS][64];
    /* Does this one take an argument? The bridge says so with a `hint`
     * header, which is the same fact the label's "<optional custom
     * summarization instructions>" shows -- one source, so the label and the
     * behaviour cannot disagree. */
    char  m_commandTakesArg[MAX_OPTS];
    int   m_modelCount, m_effortCount, m_modeCount, m_commandCount;
    /* The LABELS of the checked entries, which is what the toolbar shows.
     * The label is the bridge's displayName ("Haiku"), not the resolved id
     * ("claude-haiku-4-5-20251001") -- _send_options resolves aliases on
     * purpose, and the resolved form is 20+ characters, which does not fit
     * a toolbar button on a 640px screen. */
    char  m_curModel[64], m_curEffort[64], m_curMode[64];
    /* Where this conversation's files land, as the guest spells it: the full
     * path for the menu, the short form the button wears. The bridge sends
     * both because a 640px screen has no room for the long one on a button.
     *
     * MAX_PATH, not 128. The guest declares maxpath=260 at handshake, so the
     * bridge is entitled to send a 260-byte path and does -- it echoes back
     * whatever the user picked through SHBrowseForFolder. At 128 the path was
     * silently cut, and the cut prefix was then handed to rc_workdir_apply,
     * which rc_mkpath's it parents and all and re-points the sync root: the
     * conversation's files were mirrored into a folder the user never named
     * while the one they did name read as empty, which the bridge sees as a
     * mass deletion. `cur_work_short` is a label for a 640px button and stays
     * small, but it is checked too rather than trusted to be short. */
    char  m_curWork[MAX_PATH], m_curWorkShort[128];
    /* Non-zero once the batch has offered at least one effort level that is
     * not greyed out. Haiku has none, and the bridge says so with a single
     * disabled row -- a toolbar button reading "(effort)" forever is worse
     * than no button, so the toolbar hides it. */
    int   m_effortOk;
    /* Which mode row is ticked, so one keystroke can advance to the next.
     * An index rather than a search on `cur_mode`, which holds the LABEL --
     * matching a display string back to its id is the sort of thing that
     * works until a label is reworded. -1 when nothing is ticked. */
    int   m_curModeIx;
    int   m_optsOpen;   /* mid-batch, so the next model row appends */
    int   m_workCount;      /* work rows this batch -- see the cap in frame() */
    HWND  m_owner;
    FrameSender *m_sender;
    OptionMenusListener *m_listener;

    OptionMenus(const OptionMenus &);
    OptionMenus &operator=(const OptionMenus &);
    OptionMenus(HWND owner, HMENU bar, FrameSender *sender,
                OptionMenusListener *listener);

public:
    /* Forget a batch that was CUT. Called from the disconnect branch, beside
     * the bar's hide_all() and the sync PUT's abort: without it a drop between
     * the last OPTION row and OPTIONSEND left the batch latched open, and the
     * resent batch appended to the menus instead of replacing them. Safe to
     * call when no batch is open -- the menus are rebuilt on reconnect, and
     * leaving stale ones up while disconnected would claim a model is
     * selected on a bridge that is not there. */
    void reset();

    /* Creates the Model and Effort popups (each seeded with one greyed
     * "(not connected)" placeholder, exactly as build_menu used to) and
     * appends them to `bar` as "&Model"/"&Effort" -- called from build_menu
     * between Configure and Help, so the menu order this module does not own
     * is unchanged.
     * owner: DrawMenuBar's target -- OPTIONSEND redraws the bar after a
     *     batch.
     * sender: for SETMODEL/SETEFFORT/SETMODE.
     * listener: compose-and-send, for the Commands menu -- see
     *     OptionMenusListener above. */
    static OptionMenus *create(HWND owner, HMENU bar, FrameSender *sender,
                               OptionMenusListener *listener);
    /* menu_work and menu_attach are NOT appended to the menu bar (they hang
     * off toolbar buttons instead), so DestroyMenu(bar) never reaches them
     * and they leaked for the life of the process -- two handles off the
     * fixed 64KB USER heap. The other four ARE on the bar and are destroyed
     * with it; destroying them here too would be a double free. (audit
     * 01.37) */
    ~OptionMenus()
    {
        if (m_workMenu)   DestroyMenu(m_workMenu);
        if (m_attachMenu) DestroyMenu(m_attachMenu);
    }

    /* OPTION, OPTIONSEND -- the two frame types this module owns. Returns
     * non-zero if `f->type` was one of those. */
    bool frame(rc_frame *f);

    /* WM_COMMAND's default case: the IDM_MODEL0/IDM_EFFORT0 ranges. Returns
     * non-zero if `id` fell in either (whether or not it was actually
     * sent). */
    bool command(int id);

    /* For the toolbar, which pops these same menus rather than building its
     * own. One source of truth for what the lists contain and which entry is
     * ticked; the toolbar is another way to reach them, not another copy. */
    HMENU modelMenu() const  { return m_modelMenu; }
    HMENU effortMenu() const { return m_effortMenu; }
    HMENU modeMenu() const   { return m_modeMenu; }
    HMENU commandMenu() const    { return m_commandMenu; }
    HMENU workMenu() const   { return m_workMenu; }
    HMENU attachMenu() const { return m_attachMenu; }
    const char *workLabel() const { return m_curWorkShort; }
    const char *workPath() const  { return m_curWork; }
    const char *modelLabel() const  { return m_curModel; }
    const char *effortLabel() const { return m_curEffort; }
    const char *modeLabel() const   { return m_curMode; }
    bool hasEffort() const { return m_effortOk != 0; }

    /* Advance to the next permission mode and tell the bridge, for the
     * keyboard shortcut. Wraps. Does nothing when the list is empty, which
     * is the disconnected case. The menus and the toolbar update when the
     * bridge answers with a fresh batch -- not optimistically here, because
     * a mode this client did not manage to set must not be shown as set. */
    void cycleMode();
};

#endif
