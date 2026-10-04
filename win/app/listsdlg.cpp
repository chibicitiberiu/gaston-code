/* listsdlg.cpp -- the Lists inspector. See rc_lists_run in app/optsheet.h.
 *
 * TRANSPARENCY IS THE POINT (the user's own ask while planning): the person
 * can see every list that decides which row of a mode a call lands in, why
 * each entry is there, and which file says so -- this program's built-ins,
 * their own gaston.ini, or the bridge's shell profile -- and can try a
 * command against all of it before it matters.
 *
 * A LISTBOX with tab stops, not a ListView: the client chose ListBox over
 * ListView for the 16-bit heaps (chatlist.cpp), and comctl32 4.00 has no
 * info tips anyway. The footer shows the selected row in full.
 */
#include <windows.h>
#include <stdlib.h>
#include <string.h>

#include <vector>

#include "app/optsheet.h"
#include "app/protect.h"
#include "core/dlgtemplate.h"
#include "core/log.h"
#include "core/str.h"
#include "wire/frame.h"

namespace {

#define ID_TRY     500
#define ID_TEST    501
#define ID_RESULT  502
#define ID_SHOW    503
#define ID_ROWS    504
#define ID_FOOT    505
#define ID_ADD     506
#define ID_REMOVE  507
#define ID_RESTORE 508

enum { L_PROTECT, L_TRUST, L_DANGER, L_SHELL, L_IGNORE, L_COUNT };

const char *const LIST_NAMES[L_COUNT] = {
    "Protected places", "Programs I trust", "Dangerous programs",
    "Shell rules (the bridge's, read-only)",
    "Files never copied (read-only here)",
};

const char *sourceWords(int src)
{
    switch (src) {
    case RC_SRC_BUILTIN: return "Built in";
    case RC_SRC_USER:    return "Your list";
    case RC_SRC_TOOL:    return "Detected tool";
    case RC_SRC_REMOVED: return "Built in, removed";
    }
    return "";
}

/* Where a finding's rule came from, in words: the bridge tags it "client"
 * for this computer's own lists and "shell:<name>" for its shell rules. */
const char *sourceOf(const char *tag)
{
    static char buf[64];
    if (strcmp(tag, "client") == 0) return "this computer's lists";
    if (strncmp(tag, "shell:", 6) == 0) {
        rc_strf(buf, sizeof buf, "the bridge's %s rules", tag + 6);
        return buf;
    }
    return tag;
}

/* What each value means, and what each category is, in the dialog's words
 * (the Permissions page's rows). */
const char *valueWords(const char *v)
{
    if (!v) return "";
    if (strcmp(v, "allow") == 0) return "it just happens";
    if (strcmp(v, "check") == 0) return "the checker decides";
    if (strcmp(v, "ask") == 0) return "you are asked";
    if (strcmp(v, "off") == 0) return "it is turned off";
    if (strcmp(v, "refuse") == 0) return "it is refused";
    return v;
}

struct Row { char text[400]; int src; char value[264]; };

class ListsDialog : public ListsView
{
public:
    ListsDialog(ClientConfig &work, const char *exeDir, OptionsSink *sink,
                const char *modeId)
        : m_dlg(0), m_work(work), m_exeDir(exeDir), m_sink(sink), m_list(0),
          m_hidden(0)
    {
        rc_strcpy(m_mode, sizeof m_mode, modeId ? modeId : "");
    }

    static BOOL CALLBACK proc(HWND dlg, UINT msg, WPARAM wp, LPARAM lp);
    void listsFrame(void *frame);

private:
    void init();
    void showList(int which);
    void addRow(const char *entry, const char *reason, const char *source,
                int src);
    void select();
    void test();
    void add();
    void remove();
    void restore();
    TextList *addTo();
    TextList *removeFrom();

    HWND m_dlg;
    ClientConfig &m_work;
    const char *m_exeDir;
    OptionsSink *m_sink;
    char m_mode[RC_MODE_ID];
    int m_list;
    int m_hidden;
    std::vector<Row> m_rows;
};

void ListsDialog::addRow(const char *entry, const char *reason,
                         const char *source, int src)
{
    Row r;
    rc_strf(r.text, sizeof r.text, "%s\t%s\t%s", entry, reason, source);
    rc_strcpy(r.value, sizeof r.value, entry);
    r.src = src;
    m_rows.push_back(r);
    SendDlgItemMessageA(m_dlg, ID_ROWS, LB_ADDSTRING, 0, (LPARAM)r.text);
}

void ListsDialog::showList(int which)
{
    std::vector<ListEntry> list;
    m_list = which;
    m_rows.clear();
    m_hidden = 0;
    SendDlgItemMessageA(m_dlg, ID_ROWS, LB_RESETCONTENT, 0, 0);
    SetDlgItemTextA(m_dlg, ID_FOOT, "");
    bool editable = which <= L_DANGER;
    EnableWindow(GetDlgItem(m_dlg, ID_ADD), editable);
    EnableWindow(GetDlgItem(m_dlg, ID_REMOVE), FALSE);
    EnableWindow(GetDlgItem(m_dlg, ID_RESTORE), editable);

    if (which == L_PROTECT) rc_list_protect(m_work, m_exeDir, list);
    else if (which == L_TRUST) rc_list_trust(m_work, m_exeDir, list);
    else if (which == L_DANGER) rc_list_danger(m_work, list);
    for (unsigned i = 0; i < list.size(); i++)
        addRow(list[i].value, list[i].reason, sourceWords(list[i].source),
               list[i].source);

    if (which == L_SHELL || which == L_IGNORE) {
        const char *keys[1] = { "list" };
        const char *vals[1] = { which == L_SHELL ? "shell" : "ignore" };
        if (!m_sink || !m_sink->listsSend(keys, vals, 1, "", 0))
            SetDlgItemTextA(m_dlg, ID_FOOT, "The bridge is not connected, "
                            "so its own lists cannot be shown.");
        else
            SetDlgItemTextA(m_dlg, ID_FOOT, "Asking the bridge...");
    }
}

void ListsDialog::select()
{
    int at = (int)SendDlgItemMessageA(m_dlg, ID_ROWS, LB_GETCURSEL, 0, 0);
    char foot[400];
    if (at < 0 || at >= (int)m_rows.size()) return;
    const Row &r = m_rows[at];
    const char *where =
        r.src == RC_SRC_USER ? "gaston.ini, beside this program" :
        r.src == RC_SRC_TOOL ? "tools_detect.ini / tools.ini (Options > Tools)" :
        r.src >= 0 ? "built into Gaston (app/protect.cpp)" : "";
    rc_strf(foot, sizeof foot, "%s%s%s", r.text, where[0] ? "\r\nDeclared in: "
            : "", where);
    for (char *p = foot; *p; p++) if (*p == '\t') *p = ' ';
    SetDlgItemTextA(m_dlg, ID_FOOT, foot);
    EnableWindow(GetDlgItem(m_dlg, ID_REMOVE),
                 m_list <= L_DANGER && r.src != RC_SRC_REMOVED);
}

TextList *ListsDialog::addTo()
{
    return m_list == L_PROTECT ? &m_work.protectAdd :
           m_list == L_TRUST ? &m_work.trustAdd :
           m_list == L_DANGER ? &m_work.dangerAdd : 0;
}

TextList *ListsDialog::removeFrom()
{
    return m_list == L_PROTECT ? &m_work.protectRem :
           m_list == L_TRUST ? &m_work.trustRem :
           m_list == L_DANGER ? &m_work.dangerRem : 0;
}

void ListsDialog::add()
{
    char entry[200];
    TextList *to = addTo();
    if (!to) return;
    entry[0] = 0;
    const char *prompt =
        m_list == L_PROTECT ? "A place (* within a folder, ** across):" :
        m_list == L_DANGER ? "A program, a space, and why:" :
                             "A program name:";
    if (!rc_ask_line(m_dlg, "Add", prompt, entry, sizeof entry) || !entry[0])
        return;
    if (to->count() >= RC_LIST_MAX) {
        MessageBoxA(m_dlg, "That list is full.", "Lists", MB_OK);
        return;
    }
    to->add(entry);
    showList(m_list);
}

void ListsDialog::remove()
{
    int at = (int)SendDlgItemMessageA(m_dlg, ID_ROWS, LB_GETCURSEL, 0, 0);
    if (at < 0 || at >= (int)m_rows.size()) return;
    const Row &r = m_rows[at];
    if (r.src == RC_SRC_USER) {
        if (addTo()) addTo()->remove(r.value);
    } else {
        /* A built-in safety entry, removed: say what that costs. */
        if (m_list != L_TRUST &&
            MessageBoxA(m_dlg, "This lets the assistant touch it without "
                        "the extra checks this list gives.\r\n\r\nRemove it?",
                        "Lists", MB_YESNO | MB_ICONWARNING) != IDYES)
            return;
        if (removeFrom()) removeFrom()->add(r.value);
    }
    showList(m_list);
}

void ListsDialog::restore()
{
    if (MessageBoxA(m_dlg, "Put this list back as it was built, dropping "
                    "your additions and removals?", "Lists",
                    MB_YESNO | MB_ICONQUESTION) != IDYES)
        return;
    if (addTo()) addTo()->clear();
    if (removeFrom()) removeFrom()->clear();
    showList(m_list);
}

void ListsDialog::test()
{
    char line[260];
    GetDlgItemTextA(m_dlg, ID_TRY, line, sizeof line);
    if (!line[0]) return;
    /* A path when it is one word with a separator; a command otherwise. */
    bool path = !strchr(line, ' ') && (strchr(line, '\\') || strchr(line, ':'));
    PrefsBody body;
    body.text = line;
    body.text += "\n";
    body.text += rc_prefs_body(m_work, m_exeDir);
    const char *keys[2] = { "kind", "mode" };
    const char *vals[2] = { path ? "path" : "exec", m_mode };
    if (!m_sink || !m_sink->listsSend(keys, vals, 2, body.text.c_str(),
                                      (unsigned)body.text.size()))
        SetDlgItemTextA(m_dlg, ID_RESULT, "The bridge is not connected, so "
                        "nothing can be tried now.");
    else
        SetDlgItemTextA(m_dlg, ID_RESULT, "Asking the bridge...");
}

void ListsDialog::listsFrame(void *frame)
{
    rc_frame *f = (rc_frame *)frame;
    const char *list = rc_frame_get(f, "list");
    if (!m_dlg || !list) return;
    if (strcmp(list, "test") == 0 && strcmp(f->type, "LISTEND") == 0) {
        /* The deciding finding is the row whose category the header names. */
        char out[600], rule[300], source[80], body[1024];
        const char *cat = rc_frame_get(f, "cat");
        unsigned n = f->body_len < sizeof body - 1 ? f->body_len
                                                   : sizeof body - 1;
        memcpy(body, f->body, n);
        body[n] = 0;
        rule[0] = source[0] = 0;
        for (char *ln = body; ln && *ln; ) {
            char *nl = strchr(ln, '\n');
            if (nl) *nl = 0;
            char *t1 = strchr(ln, '\t');
            char *t2 = t1 ? strchr(t1 + 1, '\t') : 0;
            char *t3 = t2 ? strchr(t2 + 1, '\t') : 0;
            if (t1 && t2) {
                *t1 = 0; *t2 = 0;
                if (t3) *t3 = 0;
                if (!rule[0] || (cat && strcmp(ln, cat) == 0)) {
                    rc_strcpy(rule, sizeof rule, t1 + 1);
                    rc_strcpy(source, sizeof source, t2 + 1);
                }
            }
            ln = nl ? nl + 1 : 0;
        }
        rc_strf(out, sizeof out, "In \"%s\": %s.%s%s%s%s%s",
                rc_frame_get(f, "mode") ? rc_frame_get(f, "mode") : "",
                valueWords(rc_frame_get(f, "value")),
                rule[0] ? " Why: " : "", rule,
                source[0] ? " (from " : "", sourceOf(source),
                source[0] ? ")" : "");
        SetDlgItemTextA(m_dlg, ID_RESULT, out);
        return;
    }
    int want = m_list == L_SHELL ? 0 : m_list == L_IGNORE ? 1 : -1;
    if (want < 0 || strcmp(list, want ? "ignore" : "shell") != 0) return;
    if (strcmp(f->type, "LISTENT") == 0) {
        char buf[1024];
        unsigned off = 0;
        while (off < f->body_len) {
            unsigned end = off;
            while (end < f->body_len && f->body[end] != '\n') end++;
            unsigned len = end - off < sizeof buf - 1 ? end - off
                                                      : sizeof buf - 1;
            memcpy(buf, f->body + off, len);
            buf[len] = 0;
            char *t1 = strchr(buf, '\t');
            char *t2 = t1 ? strchr(t1 + 1, '\t') : 0;
            if (t1 && t2) {
                *t1 = 0; *t2 = 0;
                addRow(buf, t1 + 1, t2 + 1, -1);
            }
            off = end + 1;
        }
        return;
    }
    const char *hidden = rc_frame_get(f, "hidden");
    char foot[96];
    if (hidden && atoi(hidden) > 0)
        rc_strf(foot, sizeof foot, "%d more not shown.", atoi(hidden));
    else
        rc_strcpy(foot, sizeof foot, "");
    SetDlgItemTextA(m_dlg, ID_FOOT, foot);
}

void ListsDialog::init()
{
    INT stops[2] = { 116, 200 };
    for (int i = 0; i < L_COUNT; i++)
        SendDlgItemMessageA(m_dlg, ID_SHOW, CB_ADDSTRING, 0,
                            (LPARAM)LIST_NAMES[i]);
    SendDlgItemMessageA(m_dlg, ID_SHOW, CB_SETCURSEL, 0, 0);
    SendDlgItemMessageA(m_dlg, ID_ROWS, LB_SETTABSTOPS, 2, (LPARAM)stops);
    SendDlgItemMessageA(m_dlg, ID_TRY, EM_LIMITTEXT, 255, 0);
    if (m_sink) m_sink->listsWatch(this);
    showList(L_PROTECT);
}

BOOL CALLBACK ListsDialog::proc(HWND dlg, UINT msg, WPARAM wp, LPARAM lp)
{
    ListsDialog *self = (ListsDialog *)GetWindowLongA(dlg, DWL_USER);
    switch (msg) {
    case WM_INITDIALOG:
        self = (ListsDialog *)lp;
        SetWindowLongA(dlg, DWL_USER, (LONG)self);
        self->m_dlg = dlg;
        self->init();
        return TRUE;
    case WM_COMMAND:
        if (!self) return FALSE;
        switch (LOWORD(wp)) {
        case IDOK:                  /* Enter: the default button, Test */
            self->test();
            return TRUE;
        case IDCANCEL:
            if (self->m_sink) self->m_sink->listsWatch(0);
            self->m_dlg = 0;
            EndDialog(dlg, 0);
            return TRUE;
        case ID_TEST: self->test(); return TRUE;
        case ID_ADD: self->add(); return TRUE;
        case ID_REMOVE: self->remove(); return TRUE;
        case ID_RESTORE: self->restore(); return TRUE;
        case ID_SHOW:
            if (HIWORD(wp) == CBN_SELCHANGE)
                self->showList((int)SendDlgItemMessageA(dlg, ID_SHOW,
                                                        CB_GETCURSEL, 0, 0));
            return TRUE;
        case ID_ROWS:
            if (HIWORD(wp) == LBN_SELCHANGE) self->select();
            return TRUE;
        }
        return FALSE;
    case WM_CLOSE:
        if (self && self->m_sink) self->m_sink->listsWatch(0);
        if (self) self->m_dlg = 0;
        EndDialog(dlg, 0);
        return TRUE;
    }
    return FALSE;
}

}  // namespace

void rc_lists_run(HWND parent, ClientConfig &work, const char *exeDir,
                  OptionsSink *sink, const char *modeId)
{
    DlgTemplate t;
    ListsDialog d(work, exeDir, sink, modeId);
    t.begin(RC_DLGT_MODAL, 262, 214, "Lists");
    t.item(BS_GROUPBOX, 4, 2, 254, 64, (WORD)-1, RC_DLGT_BUTTON, "Try it");
    t.item(0, 10, 14, 80, 8, (WORD)-1, RC_DLGT_STATIC,
           "&Command or path:");
    t.item(WS_BORDER | WS_TABSTOP | ES_AUTOHSCROLL, 10, 24, 196, 12, ID_TRY,
           RC_DLGT_EDIT, "");
    /* Test is the default: Enter tries the line, it does not close. */
    t.item(WS_TABSTOP | BS_DEFPUSHBUTTON, 210, 23, 42, 14, ID_TEST,
           RC_DLGT_BUTTON, "&Test");
    t.item(0, 10, 39, 242, 24, ID_RESULT, RC_DLGT_STATIC,
           "Type a command, or a path to a file, and press Test to see "
           "which row of the mode it lands in, and why.");
    t.item(0, 6, 72, 22, 8, (WORD)-1, RC_DLGT_STATIC, "&Show:");
    t.item(CBS_DROPDOWNLIST | WS_VSCROLL | WS_TABSTOP, 30, 70, 160, 80,
           ID_SHOW, RC_DLGT_COMBOBOX, "");
    t.item(LBS_NOTIFY | LBS_USETABSTOPS | WS_VSCROLL | WS_BORDER |
           WS_TABSTOP | LBS_NOINTEGRALHEIGHT, 6, 86, 250, 78, ID_ROWS,
           RC_DLGT_LISTBOX, "");
    t.item(0, 6, 167, 250, 24, ID_FOOT, RC_DLGT_STATIC, "");
    t.item(WS_TABSTOP, 6, 194, 44, 14, ID_ADD, RC_DLGT_BUTTON, "&Add...");
    t.item(WS_TABSTOP, 54, 194, 44, 14, ID_REMOVE, RC_DLGT_BUTTON,
           "&Remove");
    t.item(WS_TABSTOP, 102, 194, 70, 14, ID_RESTORE, RC_DLGT_BUTTON,
           "Restore &defaults");
    t.item(WS_TABSTOP, 208, 194, 48, 14, IDCANCEL, RC_DLGT_BUTTON, "Close");
    if (!t.get()) {
        rc_log("lists: template did not fit");
        return;
    }
    HWND owner = GetParent(parent) ? GetParent(parent) : parent;
    DialogBoxIndirectParamA((HINSTANCE)GetWindowLongA(owner, GWL_HINSTANCE),
                            t.get(), owner, (DLGPROC)ListsDialog::proc,
                            (LPARAM)&d);
    if (sink) sink->listsWatch(0);
}
