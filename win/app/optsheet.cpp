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
    PROPSHEETPAGEA psp[RC_OPT_COUNT];
    PROPSHEETHEADERA psh;
    int i;

    pages[RC_OPT_CONNECTION].reset(new ConnectionPage(sheet.get()));

    memset(psp, 0, sizeof psp);
    for (i = 0; i < RC_OPT_COUNT; i++) {
        tmpl[i].reset(new DlgTemplate());
        pages[i]->build(*tmpl[i].get());
        if (!tmpl[i]->get()) {
            rc_log("options: page %d's template did not fit; not showing", i);
            return false;
        }
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
    psh.ppsp = psp;
    psh.pfnCallback = sheetCallback;

    int r = (int)PropertySheetA(&psh);
    if (r < 0)
        rc_log("options: PropertySheet failed (%lu)",
               (unsigned long)GetLastError());
    rc_log("options: closed, %s", sheet->applied() ? "applied" : "unchanged");
    return sheet->applied();
}
