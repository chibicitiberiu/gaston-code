/* optsheet.cpp -- see app/optsheet.h. */
#include <windows.h>
#include <commctrl.h>
#include <prsht.h>
#include <stdlib.h>
#include <string.h>

#include "app/optsheet.h"
#include "core/dialogpage.h"
#include "core/dlgtemplate.h"
#include "core/log.h"
#include "core/rc_cpp.h"
#include "core/str.h"
#include "app/modes.h"

/* Page geometry, in dialog units. 230x170 so the sheet still fits a
 * 640x480 screen at Large Fonts (the plan's platform review). */
#define PAGE_W 230
#define PAGE_H 170

namespace {

class Sheet;

/* --- the pages ------------------------------------------------------------ */

class Page : public DialogPage
{
public:
    explicit Page(Sheet *sheet) : m_sheet(sheet) {}
    virtual ~Page() {}
    /* Lay the controls out into `t`. */
    virtual void build(DlgTemplate &t) = 0;

protected:
    void onInit();
    void onApply();
    void text(int id, const char *s) { SetDlgItemTextA(hwnd(), id, s); }
    void getText(int id, char *out, unsigned cap)
    {
        GetDlgItemTextA(hwnd(), id, out, (int)cap);
    }
    /* Say why, and stay on the page with the field in focus. */
    bool refuse(int id, const char *why);
    virtual void load() = 0;
    virtual void store() = 0;

    Sheet *m_sheet;
};

/* Connection: where the bridge is. Changing any of it reconnects. */
class ConnectionPage : public Page
{
public:
    explicit ConnectionPage(Sheet *s) : Page(s) {}
    void build(DlgTemplate &t);

protected:
    void load();
    void store();
    bool onKillActive();
    bool onCommand(WORD id, WORD code, HWND ctl);
};

#define ID_HOST   300
#define ID_PORT   301
#define ID_SECRET 302

class Sheet
{
public:
    Sheet(const ClientConfig &cur, const char *exeDir, OptionsSink *sink)
        : m_work(cur), m_exeDir(exeDir), m_sink(sink), m_inited(0),
          m_applies(0), m_applied(false)
    {
    }

    ClientConfig &work() { return m_work; }
    const char *exeDir() const { return m_exeDir; }
    void pageInited() { m_inited++; }
    /* Every opened page writes its fields, then the copy goes out once. */
    void pageApplied()
    {
        if (++m_applies < m_inited) return;
        m_applies = 0;
        m_applied = true;
        if (m_sink) m_sink->optionsApply(m_work);
    }
    bool applied() const { return m_applied; }

private:
    ClientConfig m_work;
    const char *m_exeDir;
    OptionsSink *m_sink;
    int m_inited;
    int m_applies;
    bool m_applied;
};

void Page::onInit()
{
    m_sheet->pageInited();
    load();
}

void Page::onApply()
{
    store();
    m_sheet->pageApplied();
}

bool Page::refuse(int id, const char *why)
{
    MessageBoxA(hwnd(), why, "Options", MB_OK | MB_ICONEXCLAMATION);
    SetFocus(GetDlgItem(hwnd(), id));
    SendDlgItemMessageA(hwnd(), id, EM_SETSEL, 0, (LPARAM)-1);
    return false;
}

/* --- Connection ----------------------------------------------------------- */

void ConnectionPage::build(DlgTemplate &t)
{
    t.begin(RC_DLGT_PAGE, PAGE_W, PAGE_H, "Connection");
    t.item(0, 7, 8, 216, 18, (WORD)-1, RC_DLGT_STATIC,
           "The bridge this computer talks to. Changing any of these "
           "reconnects.");
    t.item(SS_RIGHT, 7, 34, 40, 10, (WORD)-1, RC_DLGT_STATIC, "&Host:");
    t.item(WS_BORDER | WS_TABSTOP | ES_AUTOHSCROLL, 51, 32, 170, 12,
           ID_HOST, RC_DLGT_EDIT, "");
    t.item(SS_RIGHT, 7, 52, 40, 10, (WORD)-1, RC_DLGT_STATIC, "&Port:");
    t.item(WS_BORDER | WS_TABSTOP | ES_AUTOHSCROLL | ES_NUMBER, 51, 50, 44,
           12, ID_PORT, RC_DLGT_EDIT, "");
    t.item(SS_RIGHT, 7, 70, 40, 10, (WORD)-1, RC_DLGT_STATIC, "&Secret:");
    t.item(WS_BORDER | WS_TABSTOP | ES_AUTOHSCROLL | ES_PASSWORD, 51, 68,
           170, 12, ID_SECRET, RC_DLGT_EDIT, "");
    t.item(0, 51, 84, 170, 18, (WORD)-1, RC_DLGT_STATIC,
           "The secret is the one the bridge was started with.");
}

void ConnectionPage::load()
{
    char port[16];
    const ClientConfig &c = m_sheet->work();
    text(ID_HOST, c.host);
    rc_strf(port, sizeof port, "%ld", c.port);
    text(ID_PORT, port);
    text(ID_SECRET, c.secret);
    SendDlgItemMessageA(hwnd(), ID_HOST, EM_LIMITTEXT, sizeof c.host - 1, 0);
    SendDlgItemMessageA(hwnd(), ID_PORT, EM_LIMITTEXT, 5, 0);
    SendDlgItemMessageA(hwnd(), ID_SECRET, EM_LIMITTEXT,
                        sizeof c.secret - 1, 0);
}

bool ConnectionPage::onKillActive()
{
    char host[64], port[16];
    getText(ID_HOST, host, sizeof host);
    getText(ID_PORT, port, sizeof port);
    if (!host[0]) return refuse(ID_HOST, "Enter a host name or IP address.");
    int p = atoi(port);
    if (p < 1 || p > 65535)
        return refuse(ID_PORT, "The port must be between 1 and 65535.");
    return true;
}

void ConnectionPage::store()
{
    char port[16];
    ClientConfig &c = m_sheet->work();
    getText(ID_HOST, c.host, sizeof c.host);
    getText(ID_PORT, port, sizeof port);
    c.port = atoi(port);
    getText(ID_SECRET, c.secret, sizeof c.secret);
}

bool ConnectionPage::onCommand(WORD id, WORD code, HWND ctl)
{
    (void)ctl;
    if (code == EN_CHANGE &&
        (id == ID_HOST || id == ID_PORT || id == ID_SECRET)) {
        changed();
        return true;
    }
    return false;
}

/* --- a one-line question ---------------------------------------------------- */

struct AskState { char *buf; unsigned cap; };

BOOL CALLBACK askProc(HWND dlg, UINT msg, WPARAM wp, LPARAM lp)
{
    AskState *st = (AskState *)GetWindowLongA(dlg, DWL_USER);
    switch (msg) {
    case WM_INITDIALOG:
        st = (AskState *)lp;
        SetWindowLongA(dlg, DWL_USER, (LONG)st);
        SetDlgItemTextA(dlg, 100, st->buf);
        SendDlgItemMessageA(dlg, 100, EM_LIMITTEXT, st->cap - 1, 0);
        SetFocus(GetDlgItem(dlg, 100));
        SendDlgItemMessageA(dlg, 100, EM_SETSEL, 0, (LPARAM)-1);
        return FALSE;
    case WM_COMMAND:
        if (LOWORD(wp) == IDOK) {
            GetDlgItemTextA(dlg, 100, st->buf, (int)st->cap);
            EndDialog(dlg, 1);
            return TRUE;
        }
        if (LOWORD(wp) == IDCANCEL) { EndDialog(dlg, 0); return TRUE; }
        return FALSE;
    case WM_CLOSE:
        EndDialog(dlg, 0);
        return TRUE;
    }
    return FALSE;
}

/* `buf` holds the suggestion going in and the answer coming out. False on
 * Cancel. Owned by `parent`, which for a page is the SHEET, so the sheet's
 * own buttons are disabled under it. */
bool rc_ask_line(HWND parent, const char *title, const char *prompt,
                 char *buf, unsigned cap)
{
    DlgTemplate t;
    AskState st;
    st.buf = buf;
    st.cap = cap;
    t.begin(RC_DLGT_MODAL, 170, 62, title);
    t.item(0, 7, 7, 156, 10, (WORD)-1, RC_DLGT_STATIC, prompt);
    t.item(WS_BORDER | WS_TABSTOP | ES_AUTOHSCROLL, 7, 19, 156, 12, 100,
           RC_DLGT_EDIT, "");
    t.item(WS_TABSTOP | BS_DEFPUSHBUTTON, 59, 40, 50, 14, IDOK,
           RC_DLGT_BUTTON, "OK");
    t.item(WS_TABSTOP, 113, 40, 50, 14, IDCANCEL, RC_DLGT_BUTTON, "Cancel");
    if (!t.get()) return false;
    HWND owner = GetParent(parent) ? GetParent(parent) : parent;
    return DialogBoxIndirectParamA((HINSTANCE)GetWindowLongA(owner,
                                   GWL_HINSTANCE), t.get(), owner,
                                   (DLGPROC)askProc, (LPARAM)&st) == 1;
}

/* --- Permissions ---------------------------------------------------------- */

#define ID_MODE      400
#define ID_SAVEAS    401
#define ID_DELMODE   402
#define ID_LISTS     403
#define ID_SUMMARY   404
#define ID_AUTOMATE  405
#define ID_TIMEOUT   406
#define ID_CAT0      410            /* + category index, RC_CAT_NAMES order */

/* The combo values, in the order the plan's UX review chose: what happens
 * most freely first, "Off" last and spelled out. */
const char VALUE_LETTERS[4] = { 'y', 'c', 'a', 'o' };
/* SHORT, to fit a 52-DLU combo; the page's summary line says what the
 * checker is ("a second AI ... asks you only when unsure"). */
const char *const VALUE_LABELS[4] = {
    "Allow", "Checker", "Ask me", "Off",
};

/* Where each category's combo sits, and what its row is called. -1 = no
 * label of its own (it shares the row of the grid). */
struct CatPlace { short x, y; };

class PermissionsPage : public Page
{
public:
    explicit PermissionsPage(Sheet *s)
        : Page(s), m_sel(0), m_modified(false), m_loading(false)
    {
        memset(m_values, 'a', sizeof m_values);
    }
    void build(DlgTemplate &t);

protected:
    void load();
    void store();
    bool onKillActive();
    bool onCommand(WORD id, WORD code, HWND ctl);

private:
    int  modeCount() const;
    void fillModes();
    void show(int index);
    void updateEnabled();
    void summary();
    void saveAs();
    void deleteMode();
    const char *modeLabel(int index) const;
    const char *modeValues(int index) const;
    bool isPreset(int index) const { return index < RC_PRESETS_N; }

    int  m_sel;                 /* index: presets first, then user modes */
    char m_values[RC_CATS];     /* what the combos show */
    bool m_modified;            /* a preset edited in place, not yet saved */
    bool m_loading;
};

/* Grid coordinates. Files/run tiers in two columns; the rest in pairs. */
const CatPlace PLACES[RC_CATS] = {
    {  95,  50 }, {  95,  78 }, {  95,  92 },        /* files work/else/sys */
    { 160,  50 }, { 160,  64 }, { 160,  78 }, { 160,  92 },  /* run x4 */
    {  62, 110 }, {  62, 124 },                      /* read, purge */
    { 170, 110 }, { 170, 124 },                      /* see, drive */
    {  62, 138 }, { 170, 138 },                      /* server, web */
};

void PermissionsPage::build(DlgTemplate &t)
{
    t.begin(RC_DLGT_PAGE, PAGE_W, PAGE_H, "Permissions");
    t.item(SS_RIGHT, 4, 6, 24, 10, (WORD)-1, RC_DLGT_STATIC, "&Mode:");
    t.item(CBS_DROPDOWNLIST | WS_VSCROLL | WS_TABSTOP, 30, 4, 80, 120,
           ID_MODE, RC_DLGT_COMBOBOX, "");
    t.item(WS_TABSTOP, 113, 4, 40, 13, ID_SAVEAS, RC_DLGT_BUTTON,
           "Sa&ve As...");
    t.item(WS_TABSTOP, 155, 4, 32, 13, ID_DELMODE, RC_DLGT_BUTTON, "&Delete");
    t.item(0, 7, 20, 216, 18, ID_SUMMARY, RC_DLGT_STATIC, "");
    t.item(0, 95, 41, 60, 8, (WORD)-1, RC_DLGT_STATIC, "Change files");
    t.item(0, 160, 41, 60, 8, (WORD)-1, RC_DLGT_STATIC, "Run programs");
    t.item(0, 7, 52, 86, 8, (WORD)-1, RC_DLGT_STATIC, "In the work folder");
    t.item(0, 7, 66, 86, 8, (WORD)-1, RC_DLGT_STATIC, "Built in the work folder");
    t.item(0, 7, 80, 86, 8, (WORD)-1, RC_DLGT_STATIC, "Other folders");
    t.item(0, 7, 94, 86, 8, (WORD)-1, RC_DLGT_STATIC, "Windows and system");
    t.item(0, 7, 112, 54, 8, (WORD)-1, RC_DLGT_STATIC, "Read files");
    t.item(0, 7, 126, 54, 8, (WORD)-1, RC_DLGT_STATIC, "Delete for good");
    t.item(0, 7, 140, 54, 8, (WORD)-1, RC_DLGT_STATIC, "Server shell");
    t.item(0, 118, 112, 50, 8, (WORD)-1, RC_DLGT_STATIC, "See the screen");
    t.item(0, 118, 126, 50, 8, (WORD)-1, RC_DLGT_STATIC, "Click and type");
    t.item(0, 118, 140, 50, 8, (WORD)-1, RC_DLGT_STATIC, "Use the web");
    for (int c = 0; c < RC_CATS; c++)
        t.item(CBS_DROPDOWNLIST | WS_VSCROLL | WS_TABSTOP, PLACES[c].x,
               PLACES[c].y, c >= 7 ? 52 : 60, 70, (WORD)(ID_CAT0 + c),
               RC_DLGT_COMBOBOX, "");
    t.item(BS_AUTOCHECKBOX | WS_TABSTOP, 7, 155, 150, 10, ID_AUTOMATE,
           RC_DLGT_BUTTON, "Let it see and use this &screen (reconnects)");
    t.item(SS_RIGHT, 158, 156, 40, 8, (WORD)-1, RC_DLGT_STATIC,
           "Ask t&imeout:");
    t.item(WS_BORDER | WS_TABSTOP | ES_NUMBER, 200, 154, 22, 12, ID_TIMEOUT,
           RC_DLGT_EDIT, "");
}

/* Fixed words for the presets -- the bridge has the same sentences in
 * policy.toml, said when a mode is chosen; this is the dialog's shorter
 * version of each. */
const char *presetSummary(const char *id)
{
    if (strcmp(id, "manual") == 0)
        return "Reading and looking just happen. Anything that changes "
               "something on this computer asks you first.";
    if (strcmp(id, "workfolder") == 0)
        return "Changes files and runs trusted programs freely in the work "
               "folder, and uses the web. The rest asks you.";
    if (strcmp(id, "plan") == 0)
        return "Reads and plans, then shows you the plan. Nothing changes "
               "until you approve it. Plan first cannot be edited.";
    if (strcmp(id, "auto") == 0)
        return "Works freely in the work folder; a second AI (the checker) "
               "screens everything else and asks you only when unsure.";
    return "";
}

int PermissionsPage::modeCount() const
{
    return RC_PRESETS_N + m_sheet->work().modeCount;
}

const char *PermissionsPage::modeLabel(int i) const
{
    return isPreset(i) ? RC_PRESETS[i].label
                       : m_sheet->work().modes[i - RC_PRESETS_N].label;
}

const char *PermissionsPage::modeValues(int i) const
{
    return isPreset(i) ? RC_PRESETS[i].values
                       : m_sheet->work().modes[i - RC_PRESETS_N].values;
}

void PermissionsPage::fillModes()
{
    HWND cb = GetDlgItem(hwnd(), ID_MODE);
    SendMessageA(cb, CB_RESETCONTENT, 0, 0);
    for (int i = 0; i < modeCount(); i++)
        SendMessageA(cb, CB_ADDSTRING, 0, (LPARAM)modeLabel(i));
}

void PermissionsPage::load()
{
    const ClientConfig &c = m_sheet->work();
    char num[16];
    m_loading = true;
    for (int cat = 0; cat < RC_CATS; cat++) {
        HWND cb = GetDlgItem(hwnd(), ID_CAT0 + cat);
        /* Reading has no Off: an assistant that cannot look at anything
         * cannot do anything (the plan's UX review). */
        int n = strcmp(RC_CAT_NAMES[cat], "read") == 0 ? 3 : 4;
        for (int v = 0; v < n; v++)
            SendMessageA(cb, CB_ADDSTRING, 0, (LPARAM)VALUE_LABELS[v]);
    }
    fillModes();
    CheckDlgButton(hwnd(), ID_AUTOMATE, c.automation ? BST_CHECKED
                                                     : BST_UNCHECKED);
    if (c.deadlineMin) {
        rc_strf(num, sizeof num, "%ld", c.deadlineMin);
        text(ID_TIMEOUT, num);
    }
    SendDlgItemMessageA(hwnd(), ID_TIMEOUT, EM_LIMITTEXT, 4, 0);
    /* Open on the mode new chats start in, else Ask first. */
    int start = 0;
    for (int i = 0; i < modeCount(); i++) {
        const char *id = isPreset(i) ? RC_PRESETS[i].id
                                     : c.modes[i - RC_PRESETS_N].id;
        if (c.defaultMode[0] && strcmp(id, c.defaultMode) == 0) start = i;
    }
    show(start);
    m_loading = false;
}

void PermissionsPage::show(int index)
{
    m_sel = index;
    m_modified = false;
    memcpy(m_values, modeValues(index), RC_CATS);
    SendDlgItemMessageA(hwnd(), ID_MODE, CB_SETCURSEL, index, 0);
    for (int cat = 0; cat < RC_CATS; cat++) {
        int at = 0;
        for (int v = 0; v < 4; v++)
            if (VALUE_LETTERS[v] == m_values[cat]) at = v;
        SendDlgItemMessageA(hwnd(), ID_CAT0 + cat, CB_SETCURSEL, at, 0);
    }
    summary();
    updateEnabled();
}

void PermissionsPage::summary()
{
    char line[200];
    if (isPreset(m_sel) && m_modified)
        rc_strf(line, sizeof line, "%s, changed. Press Save As to keep "
                "these as a mode of your own; built-in modes stay as they "
                "are.", RC_PRESETS[m_sel].label);
    else if (isPreset(m_sel))
        rc_strcpy(line, sizeof line, presetSummary(RC_PRESETS[m_sel].id));
    else {
        const ModeDef &m = m_sheet->work().modes[m_sel - RC_PRESETS_N];
        const Preset *b = rc_preset(m.base);
        rc_strf(line, sizeof line, "Your own mode, made from %s. Changes "
                "here are kept when you press OK.", b ? b->label : m.base);
    }
    text(ID_SUMMARY, line);
}

void PermissionsPage::updateEnabled()
{
    bool locked = isPreset(m_sel) && RC_PRESETS[m_sel].locked;
    bool screen = IsDlgButtonChecked(hwnd(), ID_AUTOMATE) == BST_CHECKED;
    for (int cat = 0; cat < RC_CATS; cat++) {
        bool on = !locked;
        if (strncmp(RC_CAT_NAMES[cat], "screen_", 7) == 0 && !screen)
            on = false;
        EnableWindow(GetDlgItem(hwnd(), ID_CAT0 + cat), on);
    }
    EnableWindow(GetDlgItem(hwnd(), ID_SAVEAS),
                 !locked && m_sheet->work().modeCount < RC_MODES_MAX);
    EnableWindow(GetDlgItem(hwnd(), ID_DELMODE), !isPreset(m_sel));
}

void PermissionsPage::saveAs()
{
    ClientConfig &c = m_sheet->work();
    char name[RC_MODE_LABEL];
    if (c.modeCount >= RC_MODES_MAX) {
        MessageBoxA(hwnd(), "There are already eight modes of your own. "
                    "Delete one first.", "Options", MB_OK | MB_ICONINFORMATION);
        return;
    }
    rc_strf(name, sizeof name, "My %s", modeLabel(m_sel));
    if (!rc_ask_line(hwnd(), "Save As", "Name for the new mode:", name,
                     sizeof name) || !name[0])
        return;
    ModeDef &m = c.modes[c.modeCount];
    m = ModeDef();
    rc_mode_new_id(m.id, sizeof m.id);
    rc_strcpy(m.label, sizeof m.label, name);
    rc_strcpy(m.base, sizeof m.base,
              isPreset(m_sel) ? RC_PRESETS[m_sel].id
                              : c.modes[m_sel - RC_PRESETS_N].base);
    memcpy(m.values, m_values, RC_CATS);
    c.modeCount++;
    fillModes();
    show(modeCount() - 1);
    changed();
}

void PermissionsPage::deleteMode()
{
    ClientConfig &c = m_sheet->work();
    int u = m_sel - RC_PRESETS_N;
    char q[96];
    if (u < 0) return;
    rc_strf(q, sizeof q, "Delete the mode \"%s\"?", c.modes[u].label);
    if (MessageBoxA(hwnd(), q, "Options", MB_YESNO | MB_ICONQUESTION)
        != IDYES)
        return;
    if (strcmp(c.defaultMode, c.modes[u].id) == 0) c.defaultMode[0] = 0;
    for (int i = u; i + 1 < c.modeCount; i++) c.modes[i] = c.modes[i + 1];
    c.modeCount--;
    fillModes();
    show(0);
    changed();
}

bool PermissionsPage::onCommand(WORD id, WORD code, HWND ctl)
{
    (void)ctl;
    if (m_loading) return false;
    if (id == ID_MODE && code == CBN_SELCHANGE) {
        int to = (int)SendDlgItemMessageA(hwnd(), ID_MODE, CB_GETCURSEL, 0, 0);
        if (to >= 0 && to != m_sel) show(to);
        return true;
    }
    if (id >= ID_CAT0 && id < ID_CAT0 + RC_CATS && code == CBN_SELCHANGE) {
        int cat = id - ID_CAT0;
        int v = (int)SendDlgItemMessageA(hwnd(), id, CB_GETCURSEL, 0, 0);
        if (v < 0 || v > 3) return true;
        m_values[cat] = VALUE_LETTERS[v];
        if (isPreset(m_sel)) {
            m_modified = memcmp(m_values, RC_PRESETS[m_sel].values,
                                RC_CATS) != 0;
        } else {
            m_sheet->work().modes[m_sel - RC_PRESETS_N].values[cat] =
                VALUE_LETTERS[v];
            changed();
        }
        summary();
        return true;
    }
    if (id == ID_SAVEAS && code == BN_CLICKED) { saveAs(); return true; }
    if (id == ID_DELMODE && code == BN_CLICKED) { deleteMode(); return true; }
    if (id == ID_AUTOMATE && code == BN_CLICKED) {
        updateEnabled();
        changed();
        return true;
    }
    if (id == ID_TIMEOUT && code == EN_CHANGE) { changed(); return true; }
    return false;
}

bool PermissionsPage::onKillActive()
{
    char num[16];
    getText(ID_TIMEOUT, num, sizeof num);
    if (num[0] && (atoi(num) < 1 || atoi(num) > 1440))
        return refuse(ID_TIMEOUT, "The ask timeout is in minutes, from 1 to "
                                  "1440 -- or leave it empty for the "
                                  "bridge's ten.");
    if (m_modified) {
        /* The built-in mode was changed in place. Built-ins never change,
         * so either the changes become a mode of their own or they go. */
        int r = MessageBoxA(hwnd(), "You changed a built-in mode. Save the "
                            "changes as a new mode of your own?", "Options",
                            MB_YESNOCANCEL | MB_ICONQUESTION);
        if (r == IDCANCEL) return false;
        if (r == IDYES) saveAs();
        else show(m_sel);
    }
    return true;
}

void PermissionsPage::store()
{
    char num[16];
    ClientConfig &c = m_sheet->work();
    c.automation = IsDlgButtonChecked(hwnd(), ID_AUTOMATE) == BST_CHECKED;
    getText(ID_TIMEOUT, num, sizeof num);
    c.deadlineMin = num[0] ? atoi(num) : 0;
}

/* No "?" on the caption: there is no help to show, and a button that does
 * nothing is worse than no button. Stripped before the sheet is created. */
int CALLBACK sheetCallback(HWND dlg, UINT msg, LPARAM lp)
{
    (void)dlg;
    if (msg == PSCB_PRECREATE && lp)
        ((DLGTEMPLATE *)lp)->style &= ~DS_CONTEXTHELP;
    return 0;
}

}  // namespace

bool rc_options_run(HWND parent, HINSTANCE inst, const ClientConfig &cur,
                    const char *exeDir, OptionsSink *sink, int start)
{
    UniqueObj<Sheet> sheet(new Sheet(cur, exeDir, sink));
    UniqueObj<Page> pages[RC_OPT_COUNT];
    UniqueObj<DlgTemplate> tmpl[RC_OPT_COUNT];
    /* THE V1 TYPE, not just the V1 size. comctl32 steps through this array
     * by each element's dwSize, so a dwSize smaller than the element put
     * the second page in the middle of the first: it was read as garbage
     * and dropped without a word, leaving a one-tab sheet (measured on
     * the VM, 2026-10-04). */
    PROPSHEETPAGEA_V1 psp[RC_OPT_COUNT];
    PROPSHEETHEADERA psh;
    int i;

    pages[RC_OPT_CONNECTION].reset(new ConnectionPage(sheet.get()));
    pages[RC_OPT_PERMISSIONS].reset(new PermissionsPage(sheet.get()));

    memset(psp, 0, sizeof psp);
    for (i = 0; i < RC_OPT_COUNT; i++) {
        tmpl[i].reset(new DlgTemplate());
        pages[i]->build(*tmpl[i].get());
        if (!tmpl[i]->get()) {
            rc_log("options: page %d's template did not fit; not showing", i);
            return false;
        }
        rc_log("options: page %d template %u bytes, %u controls", i,
               tmpl[i]->size(), tmpl[i]->count());
        /* THE V1 SIZE, so a Windows 95 comctl32 4.00 accepts the struct:
         * the fields after pszHeaderTitle did not exist then. */
        psp[i].dwSize = sizeof(PROPSHEETPAGEA_V1);
        psp[i].dwFlags = PSP_DLGINDIRECT;
        psp[i].hInstance = inst;
        psp[i].pResource = tmpl[i]->get();
        psp[i].pfnDlgProc = (DLGPROC)DialogPage::proc;
        psp[i].lParam = (LPARAM)pages[i].get();
    }

    memset(&psh, 0, sizeof psh);
    psh.dwSize = sizeof(PROPSHEETHEADERA_V1);
    /* Apply is SHOWN (no PSH_NOAPPLYNOW): a Win98 property sheet has one,
     * and it is how a setting is tried without closing the sheet. */
    psh.dwFlags = PSH_PROPSHEETPAGE | PSH_USECALLBACK;
    psh.hwndParent = parent;
    psh.hInstance = inst;
    psh.pszCaption = "Options";
    psh.nPages = RC_OPT_COUNT;
    psh.nStartPage = start >= 0 && start < RC_OPT_COUNT ? start : 0;
    psh.ppsp = (LPCPROPSHEETPAGEA)psp;
    psh.pfnCallback = sheetCallback;

    int r = (int)PropertySheetA(&psh);
    if (r < 0)
        rc_log("options: PropertySheet failed (%lu)",
               (unsigned long)GetLastError());
    rc_log("options: closed, %s", sheet->applied() ? "applied" : "unchanged");
    return sheet->applied();
}
