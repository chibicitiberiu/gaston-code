/* Configure > Options...: one property sheet for every per-client setting
 * (docs/OPTIONS-PLAN.md).
 *
 * The sheet works on a COPY of the configuration. Pages write their fields
 * into the copy when the sheet applies; once every page that was opened has
 * done so, the copy goes to the OptionsSink in one piece -- for OK and for
 * Apply alike -- and the sink saves it, rebuilds the PREFS body and either
 * re-sends it or reconnects. A page never opened keeps the copy's value,
 * which is the live one.
 *
 * WHILE IT IS OPEN the main window is disabled: a permission bar cannot be
 * answered, and its deadline keeps running. Frames still arrive and render,
 * because the sheet's modal loop dispatches them.
 */
#ifndef RC_OPTSHEET_H
#define RC_OPTSHEET_H

#ifndef __cplusplus
#error "app/optsheet.h is C++"
#endif

#include <windows.h>

#include "app/config.h"

/* The pages, in tab order. */
#define RC_OPT_CONNECTION  0
#define RC_OPT_GENERAL     1
#define RC_OPT_FILES       2
#define RC_OPT_PERMISSIONS 3
#define RC_OPT_TOOLS       4
#define RC_OPT_MISC        5
#define RC_OPT_COUNT       6

/* Where the bridge's answers to the Lists inspector go while it is open. */
class ListsView
{
public:
    virtual ~ListsView() {}
    /* A LISTENT or LISTEND (wire/frame.h's rc_frame). */
    virtual void listsFrame(void *frame) = 0;
};

class OptionsSink
{
public:
    virtual ~OptionsSink() {}
    /* The person pressed OK or Apply; `next` is the whole configuration.
     * `reconnect` when something HELLO carries changed outside it -- the
     * tools this machine reports (Options > Tools). */
    virtual void optionsApply(const ClientConfig &next, bool reconnect) = 0;
    /* GETLISTS to the bridge. False when the link cannot take it now. */
    virtual bool listsSend(const char *const *keys, const char *const *vals,
                           int nkv, const char *body, unsigned len) = 0;
    /* Route LISTENT/LISTEND to `view` while it is open; NULL stops. */
    virtual void listsWatch(ListsView *view) = 0;
    /* What the bridge last offered: kind 0 models, 1 modes, as ids and
     * labels in step. Empty before the first connection. */
    virtual void optionsChoices(int kind, TextList &ids,
                                TextList &labels) = 0;
};

/* A one-line question, owned by `parent`'s top-level window. `buf` holds
 * the suggestion going in and the answer coming out; false on Cancel. */
bool rc_ask_line(HWND parent, const char *title, const char *prompt,
                 char *buf, unsigned cap);

/* Options > Permissions > Lists...: every list that decides where a call
 * lands, what each entry means, where it is declared -- and a line to try a
 * command against them. Edits go into `work`, saved with the sheet. */
void rc_lists_run(HWND parent, ClientConfig &work, const char *exeDir,
                  OptionsSink *sink, const char *modeId);

/* Show the sheet, starting on page `start`. True if anything was applied. */
bool rc_options_run(HWND parent, HINSTANCE inst, const ClientConfig &cur,
                    const char *exeDir, OptionsSink *sink, int start);

#endif
