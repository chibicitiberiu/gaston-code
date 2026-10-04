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
#include "app/workdir.h"
#include "app/tooldet.h"
#include "wire/frame.h"

#include <vector>

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
          m_applies(0), m_applied(false), m_reconnect(false)
    {
    }
    /* A page changed something HELLO carries outside the configuration. */
    void needReconnect() { m_reconnect = true; }

    ClientConfig &work() { return m_work; }
    const char *exeDir() const { return m_exeDir; }
    OptionsSink *sink() const { return m_sink; }
    void pageInited() { m_inited++; }
    /* Every opened page writes its fields, then the copy goes out once. */
    void pageApplied()
    {
        if (++m_applies < m_inited) return;
        m_applies = 0;
        m_applied = true;
        if (m_sink) m_sink->optionsApply(m_work, m_reconnect);
        m_reconnect = false;
    }
    bool applied() const { return m_applied; }

private:
    ClientConfig m_work;
    const char *m_exeDir;
    OptionsSink *m_sink;
    int m_inited;
    int m_applies;
    bool m_applied;
    bool m_reconnect;
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

/* --- General ----------------------------------------------------------- */

#define ID_GMODE   600
#define ID_GMODEL  601
#define ID_GEFFORT 602
#define ID_GFOLLOW 603
#define ID_GPAGE   604

const char *const EFFORTS[] = { "", "low", "medium", "high", "xhigh", "max" };
const char *const EFFORT_LABELS[] = {
    "(the bridge's)", "Low", "Medium", "High", "Extra high", "Maximum",
};
const char *const PAGE_LABELS[] = { "Small", "Normal", "Large" };

class GeneralPage : public Page
{
public:
    explicit GeneralPage(Sheet *s) : Page(s) {}
    void build(DlgTemplate &t);

protected:
    void load();
    void store();
    bool onCommand(WORD id, WORD code, HWND ctl);

private:
    TextList m_modelIds;
    TextList m_modeIds;
};

void GeneralPage::build(DlgTemplate &t)
{
    t.begin(RC_DLGT_PAGE, PAGE_W, PAGE_H, "General");
    t.item(BS_GROUPBOX, 4, 4, 222, 74, (WORD)-1, RC_DLGT_BUTTON,
           "New chats start with");
    t.item(SS_RIGHT, 10, 19, 44, 8, (WORD)-1, RC_DLGT_STATIC, "M&ode:");
    t.item(CBS_DROPDOWNLIST | WS_VSCROLL | WS_TABSTOP, 58, 17, 160, 100,
           ID_GMODE, RC_DLGT_COMBOBOX, "");
    t.item(SS_RIGHT, 10, 35, 44, 8, (WORD)-1, RC_DLGT_STATIC, "&Model:");
    t.item(CBS_DROPDOWNLIST | WS_VSCROLL | WS_TABSTOP, 58, 33, 160, 100,
           ID_GMODEL, RC_DLGT_COMBOBOX, "");
    t.item(SS_RIGHT, 10, 51, 44, 8, (WORD)-1, RC_DLGT_STATIC, "&Effort:");
    t.item(CBS_DROPDOWNLIST | WS_VSCROLL | WS_TABSTOP, 58, 49, 160, 100,
           ID_GEFFORT, RC_DLGT_COMBOBOX, "");
    t.item(0, 10, 65, 212, 10, (WORD)-1, RC_DLGT_STATIC,
           "The Mode, Model and Effort menus change the open chat only.");
    t.item(BS_GROUPBOX, 4, 84, 222, 66, (WORD)-1, RC_DLGT_BUTTON,
           "The conversation");
    t.item(BS_AUTOCHECKBOX | WS_TABSTOP, 10, 97, 210, 10, ID_GFOLLOW,
           RC_DLGT_BUTTON, "&Follow new text as it arrives");
    t.item(SS_RIGHT, 10, 115, 44, 8, (WORD)-1, RC_DLGT_STATIC, "&Page size:");
    t.item(CBS_DROPDOWNLIST | WS_TABSTOP, 58, 113, 70, 60, ID_GPAGE,
           RC_DLGT_COMBOBOX, "");
    t.item(0, 10, 129, 212, 18, (WORD)-1, RC_DLGT_STATIC,
           "How much of a conversation one page holds. Large needs more "
           "memory; it applies from the next page.");
}

void GeneralPage::load()
{
    const ClientConfig &c = m_sheet->work();
    TextList labels;
    char id[96], label[96];
    unsigned ip = 0, lp = 0;
    int i, at;

    /* Modes: the presets and the person's own, by their labels. */
    m_modeIds.clear();
    at = 0;
    for (i = 0; i < RC_PRESETS_N; i++) {
        m_modeIds.push(RC_PRESETS[i].id);
        SendDlgItemMessageA(hwnd(), ID_GMODE, CB_ADDSTRING, 0,
                            (LPARAM)RC_PRESETS[i].label);
        if (strcmp(c.defaultMode, RC_PRESETS[i].id) == 0) at = i;
    }
    for (i = 0; i < c.modeCount; i++) {
        m_modeIds.push(c.modes[i].id);
        SendDlgItemMessageA(hwnd(), ID_GMODE, CB_ADDSTRING, 0,
                            (LPARAM)c.modes[i].label);
        if (strcmp(c.defaultMode, c.modes[i].id) == 0) at = RC_PRESETS_N + i;
    }
    SendDlgItemMessageA(hwnd(), ID_GMODE, CB_SETCURSEL, at, 0);

    /* Models: what the bridge last offered, plus "the bridge's own". */
    m_modelIds.clear();
    m_modelIds.push("-");
    SendDlgItemMessageA(hwnd(), ID_GMODEL, CB_ADDSTRING, 0,
                        (LPARAM)"(the bridge's)");
    TextList ids;
    if (m_sheet->sink()) m_sheet->sink()->optionsChoices(0, ids, labels);
    at = 0;
    for (i = 1; ids.next(&ip, id, sizeof id) &&
                labels.next(&lp, label, sizeof label); i++) {
        m_modelIds.push(id);
        SendDlgItemMessageA(hwnd(), ID_GMODEL, CB_ADDSTRING, 0,
                            (LPARAM)label);
        if (strcmp(c.defaultModel, id) == 0) at = i;
    }
    if (c.defaultModel[0] && !at) {
        /* Set before, not offered now (no connection yet): keep it. */
        m_modelIds.push(c.defaultModel);
        SendDlgItemMessageA(hwnd(), ID_GMODEL, CB_ADDSTRING, 0,
                            (LPARAM)c.defaultModel);
        at = i;
    }
    SendDlgItemMessageA(hwnd(), ID_GMODEL, CB_SETCURSEL, at, 0);

    at = 0;
    for (i = 0; i < 6; i++) {
        SendDlgItemMessageA(hwnd(), ID_GEFFORT, CB_ADDSTRING, 0,
                            (LPARAM)EFFORT_LABELS[i]);
        if (strcmp(c.defaultEffort, EFFORTS[i]) == 0) at = i;
    }
    SendDlgItemMessageA(hwnd(), ID_GEFFORT, CB_SETCURSEL, at, 0);

    CheckDlgButton(hwnd(), ID_GFOLLOW, c.autoscroll ? BST_CHECKED
                                                    : BST_UNCHECKED);
    for (i = 0; i < 3; i++)
        SendDlgItemMessageA(hwnd(), ID_GPAGE, CB_ADDSTRING, 0,
                            (LPARAM)PAGE_LABELS[i]);
    SendDlgItemMessageA(hwnd(), ID_GPAGE, CB_SETCURSEL,
                        c.pageSize >= 0 && c.pageSize < 3 ? c.pageSize : 1, 0);
}

/* The n'th entry of a TextList, into `out`. */
bool nth(const TextList &l, int n, char *out, unsigned cap)
{
    unsigned pos = 0;
    for (int i = 0; l.next(&pos, out, cap); i++)
        if (i == n) return true;
    out[0] = 0;
    return false;
}

void GeneralPage::store()
{
    ClientConfig &c = m_sheet->work();
    char id[96];
    int at = (int)SendDlgItemMessageA(hwnd(), ID_GMODE, CB_GETCURSEL, 0, 0);
    nth(m_modeIds, at, id, sizeof id);
    rc_strcpy(c.defaultMode, sizeof c.defaultMode, id);
    at = (int)SendDlgItemMessageA(hwnd(), ID_GMODEL, CB_GETCURSEL, 0, 0);
    nth(m_modelIds, at, id, sizeof id);
    rc_strcpy(c.defaultModel, sizeof c.defaultModel,
              strcmp(id, "-") == 0 ? "" : id);
    at = (int)SendDlgItemMessageA(hwnd(), ID_GEFFORT, CB_GETCURSEL, 0, 0);
    rc_strcpy(c.defaultEffort, sizeof c.defaultEffort,
              at >= 0 && at < 6 ? EFFORTS[at] : "");
    c.autoscroll = IsDlgButtonChecked(hwnd(), ID_GFOLLOW) == BST_CHECKED;
    at = (int)SendDlgItemMessageA(hwnd(), ID_GPAGE, CB_GETCURSEL, 0, 0);
    c.pageSize = at >= 0 && at < 3 ? at : RC_PAGE_NORMAL;
}

bool GeneralPage::onCommand(WORD id, WORD code, HWND ctl)
{
    (void)ctl;
    if ((code == CBN_SELCHANGE && id >= ID_GMODE && id <= ID_GPAGE) ||
        (code == BN_CLICKED && id == ID_GFOLLOW)) {
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
bool askLine(HWND parent, const char *title, const char *prompt,
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

/* --- Files ------------------------------------------------------------ */

#define ID_FROOT    700
#define ID_FBROWSE  701
#define ID_FSHARE   702
#define ID_FSWEEP   703
#define ID_FMAXKB   704
#define ID_FIGNORE  705
#define ID_FIGADD   706
#define ID_FIGREM   707
#define ID_FEXIST   708

const long SWEEPS[] = { 0, -1, 1, 5, 15 };
const char *const SWEEP_LABELS[] = {
    "When files change (the bridge's pace)", "Only when files change",
    "Every minute", "Every 5 minutes", "Every 15 minutes",
};
const long MAXKBS[] = { 0, 256, 4096, 16384 };
const char *const MAXKB_LABELS[] = {
    "The bridge's (1 MB)", "256 KB", "4 MB", "16 MB",
};
const char *const EXIST_LABELS[] = {
    "Ask me each time", "Keep theirs", "Replace them", "Merge (assistant)",
};

class FilesPage : public Page
{
public:
    explicit FilesPage(Sheet *s) : Page(s) { m_root[0] = 0; }
    void build(DlgTemplate &t);

protected:
    void load();
    void store();
    bool onKillActive();
    bool onCommand(WORD id, WORD code, HWND ctl);

private:
    void fillIgnore();
    char m_root[128];           /* kept while sharing is off */
};

void FilesPage::build(DlgTemplate &t)
{
    t.begin(RC_DLGT_PAGE, PAGE_W, PAGE_H, "Files");
    t.item(BS_AUTOCHECKBOX | WS_TABSTOP, 7, 6, 216, 10, ID_FSHARE,
           RC_DLGT_BUTTON, "S&hare files with the assistant (reconnects)");
    t.item(0, 7, 20, 120, 8, (WORD)-1, RC_DLGT_STATIC,
           "Each chat gets a folder &under:");
    t.item(WS_BORDER | WS_TABSTOP | ES_AUTOHSCROLL, 7, 30, 160, 12, ID_FROOT,
           RC_DLGT_EDIT, "");
    t.item(WS_TABSTOP, 171, 29, 52, 14, ID_FBROWSE, RC_DLGT_BUTTON,
           "&Browse...");
    t.item(SS_RIGHT, 7, 50, 44, 8, (WORD)-1, RC_DLGT_STATIC, "&Sync:");
    t.item(CBS_DROPDOWNLIST | WS_VSCROLL | WS_TABSTOP, 55, 48, 168, 80,
           ID_FSWEEP, RC_DLGT_COMBOBOX, "");
    t.item(SS_RIGHT, 7, 66, 44, 8, (WORD)-1, RC_DLGT_STATIC, "&Largest:");
    t.item(CBS_DROPDOWNLIST | WS_VSCROLL | WS_TABSTOP, 55, 64, 100, 70,
           ID_FMAXKB, RC_DLGT_COMBOBOX, "");
    t.item(0, 7, 82, 160, 8, (WORD)-1, RC_DLGT_STATIC,
           "&Never copy (patterns, * and ?):");
    t.item(LBS_NOTIFY | WS_VSCROLL | WS_BORDER | WS_TABSTOP |
           LBS_NOINTEGRALHEIGHT, 7, 92, 160, 40, ID_FIGNORE, RC_DLGT_LISTBOX,
           "");
    t.item(WS_TABSTOP, 171, 92, 52, 14, ID_FIGADD, RC_DLGT_BUTTON, "&Add...");
    t.item(WS_TABSTOP, 171, 108, 52, 14, ID_FIGREM, RC_DLGT_BUTTON,
           "&Remove");
    t.item(0, 7, 138, 216, 8, (WORD)-1, RC_DLGT_STATIC,
           "When a new work folder already has files:");
    t.item(CBS_DROPDOWNLIST | WS_TABSTOP, 7, 148, 120, 60, ID_FEXIST,
           RC_DLGT_COMBOBOX, "");
}

void FilesPage::fillIgnore()
{
    char p[264];
    unsigned pos = 0;
    SendDlgItemMessageA(hwnd(), ID_FIGNORE, LB_RESETCONTENT, 0, 0);
    while (m_sheet->work().ignore.next(&pos, p, sizeof p))
        SendDlgItemMessageA(hwnd(), ID_FIGNORE, LB_ADDSTRING, 0, (LPARAM)p);
}

void FilesPage::load()
{
    const ClientConfig &c = m_sheet->work();
    int i, at;
    rc_strcpy(m_root, sizeof m_root, c.syncRoot[0] ? c.syncRoot
                                                   : "C:\\Gaston");
    text(ID_FROOT, m_root);
    SendDlgItemMessageA(hwnd(), ID_FROOT, EM_LIMITTEXT, sizeof m_root - 1, 0);
    CheckDlgButton(hwnd(), ID_FSHARE, c.syncRoot[0] ? BST_CHECKED
                                                    : BST_UNCHECKED);
    EnableWindow(GetDlgItem(hwnd(), ID_FROOT), c.syncRoot[0] != 0);
    EnableWindow(GetDlgItem(hwnd(), ID_FBROWSE), c.syncRoot[0] != 0);
    at = 0;
    for (i = 0; i < 5; i++) {
        SendDlgItemMessageA(hwnd(), ID_FSWEEP, CB_ADDSTRING, 0,
                            (LPARAM)SWEEP_LABELS[i]);
        if (SWEEPS[i] == c.sweepMin) at = i;
    }
    SendDlgItemMessageA(hwnd(), ID_FSWEEP, CB_SETCURSEL, at, 0);
    at = 0;
    for (i = 0; i < 4; i++) {
        SendDlgItemMessageA(hwnd(), ID_FMAXKB, CB_ADDSTRING, 0,
                            (LPARAM)MAXKB_LABELS[i]);
        if (MAXKBS[i] == c.maxFileKb) at = i;
    }
    SendDlgItemMessageA(hwnd(), ID_FMAXKB, CB_SETCURSEL, at, 0);
    for (i = 0; i < 4; i++)
        SendDlgItemMessageA(hwnd(), ID_FEXIST, CB_ADDSTRING, 0,
                            (LPARAM)EXIST_LABELS[i]);
    SendDlgItemMessageA(hwnd(), ID_FEXIST, CB_SETCURSEL,
                        c.existing + 1 >= 0 && c.existing + 1 < 4
                            ? c.existing + 1 : 0, 0);
    fillIgnore();
}

bool FilesPage::onKillActive()
{
    char root[128];
    if (IsDlgButtonChecked(hwnd(), ID_FSHARE) != BST_CHECKED) return true;
    getText(ID_FROOT, root, sizeof root);
    if (!(root[0] && root[1] == ':' && root[2] == '\\'))
        return refuse(ID_FROOT, "The work folder root is a full path, such "
                                "as C:\\Gaston.");
    return true;
}

void FilesPage::store()
{
    ClientConfig &c = m_sheet->work();
    int at;
    if (IsDlgButtonChecked(hwnd(), ID_FSHARE) == BST_CHECKED)
        getText(ID_FROOT, c.syncRoot, sizeof c.syncRoot);
    else
        c.syncRoot[0] = 0;
    at = (int)SendDlgItemMessageA(hwnd(), ID_FSWEEP, CB_GETCURSEL, 0, 0);
    c.sweepMin = at >= 0 && at < 5 ? SWEEPS[at] : 0;
    at = (int)SendDlgItemMessageA(hwnd(), ID_FMAXKB, CB_GETCURSEL, 0, 0);
    c.maxFileKb = at >= 0 && at < 4 ? MAXKBS[at] : 0;
    at = (int)SendDlgItemMessageA(hwnd(), ID_FEXIST, CB_GETCURSEL, 0, 0);
    c.existing = at >= 0 && at < 4 ? at - 1 : RC_EXISTING_ASK;
}

bool FilesPage::onCommand(WORD id, WORD code, HWND ctl)
{
    (void)ctl;
    if (id == ID_FSHARE && code == BN_CLICKED) {
        bool on = IsDlgButtonChecked(hwnd(), ID_FSHARE) == BST_CHECKED;
        EnableWindow(GetDlgItem(hwnd(), ID_FROOT), on);
        EnableWindow(GetDlgItem(hwnd(), ID_FBROWSE), on);
        changed();
        return true;
    }
    if (id == ID_FBROWSE && code == BN_CLICKED) {
        char cur[128], picked[MAX_PATH];
        getText(ID_FROOT, cur, sizeof cur);
        if (rc_workdir_browse(GetParent(hwnd()), cur, picked, sizeof picked)) {
            if (strlen(picked) >= sizeof m_root) {
                MessageBoxA(hwnd(), "That path is too long for a work "
                            "folder root.", "Options", MB_OK);
                return true;
            }
            text(ID_FROOT, picked);
            changed();
        }
        return true;
    }
    if (id == ID_FIGADD && code == BN_CLICKED) {
        char p[200];
        p[0] = 0;
        if (m_sheet->work().ignore.count() >= RC_IGNORE_MAX) {
            MessageBoxA(hwnd(), "That list is full.", "Options", MB_OK);
            return true;
        }
        if (askLine(hwnd(), "Never copy", "A pattern, such as *.bak or "
                    "build/:", p, sizeof p) && p[0]) {
            m_sheet->work().ignore.add(p);
            fillIgnore();
            changed();
        }
        return true;
    }
    if (id == ID_FIGREM && code == BN_CLICKED) {
        char p[264];
        int at = (int)SendDlgItemMessageA(hwnd(), ID_FIGNORE, LB_GETCURSEL,
                                          0, 0);
        if (at >= 0 && SendDlgItemMessageA(hwnd(), ID_FIGNORE, LB_GETTEXT,
                                           at, (LPARAM)p) > 0) {
            m_sheet->work().ignore.remove(p);
            fillIgnore();
            changed();
        }
        return true;
    }
    if ((code == EN_CHANGE && id == ID_FROOT) ||
        (code == CBN_SELCHANGE && (id == ID_FSWEEP || id == ID_FMAXKB ||
                                   id == ID_FEXIST))) {
        changed();
        return true;
    }
    return false;
}

/* --- Tools ------------------------------------------------------------ */

#define ID_TROWS    800
#define ID_TFOOT    801
#define ID_TTOGGLE  802
#define ID_TADD     803
#define ID_TREMOVE  804
#define ID_TRESCAN  805

struct ToolRow
{
    char name[48];
    char evidence[MAX_PATH + 40];
    char trusted[200];
    /* For a new entry: what to write. */
    char env[64];
    char file[MAX_PATH];
    int user, found, enabled, wasEnabled;
    int added, removed;
};

/* The Tools page: what this machine's detection finds, switched on or off,
 * and the person's own entries -- written to tools.ini, which an install
 * never overwrites. A change reconnects: the tool list rides HELLO. */
class ToolsPage : public Page, public ListsView
{
public:
    explicit ToolsPage(Sheet *s) : Page(s), m_dirty(false) {}
    ~ToolsPage() { if (m_sheet->sink()) m_sheet->sink()->listsWatch(0); }
    void build(DlgTemplate &t);
    void listsFrame(void *frame);
    static void sectionSink(void *ctx, const char *name, int user, int found,
                            const char *evidence, int enabled,
                            const char *trusted);

protected:
    void load();
    void store();
    bool onCommand(WORD id, WORD code, HWND ctl);

private:
    void fill();
    void select();
    void add();
    int  current();

    std::vector<ToolRow> m_rows;
    TextList m_known;           /* profile names the bridge has */
    bool m_dirty;
};

void ToolsPage::build(DlgTemplate &t)
{
    t.begin(RC_DLGT_PAGE, PAGE_W, PAGE_H, "Tools");
    t.item(0, 7, 4, 216, 18, (WORD)-1, RC_DLGT_STATIC,
           "Compilers, shells and the like found on this computer. Changes "
           "apply when Gaston reconnects.");
    t.item(LBS_NOTIFY | LBS_USETABSTOPS | WS_VSCROLL | WS_BORDER |
           WS_TABSTOP | LBS_NOINTEGRALHEIGHT, 7, 24, 216, 84, ID_TROWS,
           RC_DLGT_LISTBOX, "");
    t.item(0, 7, 111, 216, 34, ID_TFOOT, RC_DLGT_STATIC, "");
    t.item(WS_TABSTOP, 7, 150, 48, 14, ID_TTOGGLE, RC_DLGT_BUTTON,
           "Turn o&ff");
    t.item(WS_TABSTOP, 59, 150, 40, 14, ID_TADD, RC_DLGT_BUTTON, "&Add...");
    t.item(WS_TABSTOP, 103, 150, 44, 14, ID_TREMOVE, RC_DLGT_BUTTON,
           "&Remove");
    t.item(WS_TABSTOP, 175, 150, 48, 14, ID_TRESCAN, RC_DLGT_BUTTON,
           "Re&scan");
}

void ToolsPage::sectionSink(void *ctx, const char *name, int user, int found,
                            const char *evidence, int enabled,
                            const char *trusted)
{
    ToolsPage *self = (ToolsPage *)ctx;
    ToolRow r;
    memset(&r, 0, sizeof r);
    rc_strcpy(r.name, sizeof r.name, name);
    rc_strcpy(r.evidence, sizeof r.evidence, evidence);
    rc_strcpy(r.trusted, sizeof r.trusted, trusted);
    r.user = user;
    r.found = found;
    r.enabled = r.wasEnabled = enabled;
    self->m_rows.push_back(r);
}

void ToolsPage::fill()
{
    int keep = current();
    SendDlgItemMessageA(hwnd(), ID_TROWS, LB_RESETCONTENT, 0, 0);
    for (unsigned i = 0; i < m_rows.size(); i++) {
        const ToolRow &r = m_rows[i];
        char line[400];
        if (r.removed) continue;
        rc_strf(line, sizeof line, "%s %s\t%s\t%s",
                r.enabled ? "[x]" : "[  ]", r.name,
                r.found ? r.evidence : "not found",
                r.user ? "Your entry" : "Built in");
        int at = (int)SendDlgItemMessageA(hwnd(), ID_TROWS, LB_ADDSTRING, 0,
                                          (LPARAM)line);
        SendDlgItemMessageA(hwnd(), ID_TROWS, LB_SETITEMDATA, at, i);
    }
    if (keep >= 0) {
        int n = (int)SendDlgItemMessageA(hwnd(), ID_TROWS, LB_GETCOUNT, 0, 0);
        for (int j = 0; j < n; j++)
            if ((int)SendDlgItemMessageA(hwnd(), ID_TROWS, LB_GETITEMDATA, j,
                                         0) == keep)
                SendDlgItemMessageA(hwnd(), ID_TROWS, LB_SETCURSEL, j, 0);
    }
    select();
}

int ToolsPage::current()
{
    if (!hwnd()) return -1;
    int at = (int)SendDlgItemMessageA(hwnd(), ID_TROWS, LB_GETCURSEL, 0, 0);
    if (at < 0) return -1;
    return (int)SendDlgItemMessageA(hwnd(), ID_TROWS, LB_GETITEMDATA, at, 0);
}

void ToolsPage::select()
{
    int i = current();
    char foot[600];
    if (i < 0 || i >= (int)m_rows.size()) {
        text(ID_TFOOT, "");
        EnableWindow(GetDlgItem(hwnd(), ID_TTOGGLE), FALSE);
        EnableWindow(GetDlgItem(hwnd(), ID_TREMOVE), FALSE);
        return;
    }
    const ToolRow &r = m_rows[i];
    rc_strf(foot, sizeof foot, "%s -- %s.\r\nFound by: %s\r\n"
            "Trusted programs: %s", r.name,
            m_known.has(r.name) ? "the bridge knows how to use it"
                                : "reported to the bridge only",
            r.found ? r.evidence : "nothing matched on this computer",
            r.trusted[0] ? r.trusted : "none");
    text(ID_TFOOT, foot);
    SetDlgItemTextA(hwnd(), ID_TTOGGLE, r.enabled ? "Turn o&ff" : "Turn o&n");
    EnableWindow(GetDlgItem(hwnd(), ID_TTOGGLE), TRUE);
    EnableWindow(GetDlgItem(hwnd(), ID_TREMOVE), r.user);
}

void ToolsPage::load()
{
    m_rows.clear();
    rc_tooldet_sections(m_sheet->exeDir(), sectionSink, this);
    INT stops[2] = { 70, 180 };
    SendDlgItemMessageA(hwnd(), ID_TROWS, LB_SETTABSTOPS, 2, (LPARAM)stops);
    fill();
    /* Which of these the bridge has a profile for: asked once. */
    if (m_sheet->sink()) {
        const char *k[1] = { "list" };
        const char *v[1] = { "profiles" };
        m_sheet->sink()->listsWatch(this);
        m_sheet->sink()->listsSend(k, v, 1, "", 0);
    }
}

void ToolsPage::listsFrame(void *frame)
{
    rc_frame *f = (rc_frame *)frame;
    const char *list = rc_frame_get(f, "list");
    if (!hwnd() || !list || strcmp(list, "profiles") != 0) return;
    if (strcmp(f->type, "LISTENT") == 0) {
        unsigned off = 0;
        while (off < f->body_len) {
            char name[64];
            unsigned end = off, n = 0;
            while (end < f->body_len && f->body[end] != '\n') end++;
            while (off + n < end && f->body[off + n] != '\t' &&
                   n < sizeof name - 1)
                n++;
            memcpy(name, f->body + off, n);
            name[n] = 0;
            m_known.add(name);
            off = end + 1;
        }
        return;
    }
    select();
}

bool ToolsPage::onCommand(WORD id, WORD code, HWND ctl)
{
    (void)ctl;
    int i = current();
    if (id == ID_TROWS && code == LBN_SELCHANGE) { select(); return true; }
    if (id == ID_TTOGGLE && code == BN_CLICKED && i >= 0) {
        ToolRow &r = m_rows[i];
        if (r.enabled && strcmp(r.name, "command.com") == 0) {
            /* The bridge runs every command through a shell. */
            MessageBoxA(hwnd(), "The command interpreter stays on: every "
                        "command the assistant runs goes through it.",
                        "Options", MB_OK | MB_ICONINFORMATION);
            return true;
        }
        r.enabled = !r.enabled;
        m_dirty = true;
        fill();
        changed();
        return true;
    }
    if (id == ID_TREMOVE && code == BN_CLICKED && i >= 0 && m_rows[i].user) {
        m_rows[i].removed = 1;
        m_dirty = true;
        fill();
        changed();
        return true;
    }
    if (id == ID_TADD && code == BN_CLICKED) { add(); return true; }
    if (id == ID_TRESCAN && code == BN_CLICKED) {
        /* Look again now, and report it at the next connection. */
        m_dirty = true;
        load();
        changed();
        return true;
    }
    return false;
}

void ToolsPage::add()
{
    ToolRow r;
    char trust[200];
    memset(&r, 0, sizeof r);
    if (!askLine(hwnd(), "Add a tool", "A short name (a bridge profile name "
                 "if it has one):", r.name, sizeof r.name) || !r.name[0])
        return;
    askLine(hwnd(), "Add a tool", "A file that is there when it is "
            "installed (full path):", r.file, sizeof r.file);
    askLine(hwnd(), "Add a tool", "An environment variable naming where it "
            "is (or leave empty):", r.env, sizeof r.env);
    trust[0] = 0;
    askLine(hwnd(), "Add a tool", "Its programs that may run without a "
            "second look, comma-separated:", trust, sizeof trust);
    rc_strcpy(r.trusted, sizeof r.trusted, trust);
    r.user = 1;
    r.enabled = r.wasEnabled = 1;
    r.added = 1;
    r.found = r.file[0] && GetFileAttributesA(r.file) != 0xFFFFFFFF;
    rc_strcpy(r.evidence, sizeof r.evidence, r.file);
    m_rows.push_back(r);
    m_dirty = true;
    fill();
    changed();
}

void ToolsPage::store()
{
    char ini[MAX_PATH];
    if (!m_dirty) return;
    if (!rc_strf(ini, sizeof ini, "%s\\tools.ini", m_sheet->exeDir())) return;
    for (unsigned i = 0; i < m_rows.size(); i++) {
        ToolRow &r = m_rows[i];
        if (r.removed) {
            WritePrivateProfileStringA(r.name, NULL, NULL, ini);
            continue;
        }
        if (r.added) {
            char key[16];
            char prog[64];
            char *p;
            char *q;
            int n = 0;
            if (r.env[0]) WritePrivateProfileStringA(r.name, "env1", r.env, ini);
            if (r.file[0]) WritePrivateProfileStringA(r.name, "file1", r.file,
                                                      ini);
            for (p = r.trusted; *p && n < 8; p = q) {
                while (*p == ' ' || *p == ',') p++;
                q = p;
                while (*q && *q != ',') q++;
                unsigned len = (unsigned)(q - p);
                while (len && p[len - 1] == ' ') len--;
                if (!len || len >= sizeof prog) continue;
                memcpy(prog, p, len);
                prog[len] = 0;
                rc_strf(key, sizeof key, "trust%d", ++n);
                WritePrivateProfileStringA(r.name, key, prog, ini);
            }
            r.added = 0;
        }
        if (r.enabled != r.wasEnabled) {
            /* Off is `enabled=0` in the user's file; on is its absence. */
            WritePrivateProfileStringA(r.name, "enabled",
                                       r.enabled ? NULL : "0", ini);
            r.wasEnabled = r.enabled;
        }
    }
    WritePrivateProfileStringA(NULL, NULL, NULL, ini);   /* flush */
    rc_tooldet_forget();
    m_sheet->needReconnect();
    m_dirty = false;
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
    t.item(WS_TABSTOP, 189, 4, 36, 13, ID_LISTS, RC_DLGT_BUTTON, "&Lists...");
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
    if (!askLine(hwnd(), "Save As", "Name for the new mode:", name,
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
    if (id == ID_LISTS && code == BN_CLICKED) {
        const char *mode = isPreset(m_sel) ? RC_PRESETS[m_sel].id
            : m_sheet->work().modes[m_sel - RC_PRESETS_N].id;
        rc_lists_run(hwnd(), m_sheet->work(), m_sheet->exeDir(),
                     m_sheet->sink(), mode);
        /* The lists are saved with the sheet, so the sheet has changed
         * whether or not anything was edited -- Apply lights either way. */
        changed();
        return true;
    }
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
    pages[RC_OPT_GENERAL].reset(new GeneralPage(sheet.get()));
    pages[RC_OPT_FILES].reset(new FilesPage(sheet.get()));
    pages[RC_OPT_PERMISSIONS].reset(new PermissionsPage(sheet.get()));
    pages[RC_OPT_TOOLS].reset(new ToolsPage(sheet.get()));

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

bool rc_ask_line(HWND parent, const char *title, const char *prompt,
                 char *buf, unsigned cap)
{
    return askLine(parent, title, prompt, buf, cap);
}
