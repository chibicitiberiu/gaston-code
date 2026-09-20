/* workdlg.cpp -- see app/workdlg.h.
 *
 * The templates are assembled by hand into a byte buffer, the same way
 * app/hostdlg.cpp does and for the same reason: a compiled template is fixed
 * in dialog units at build time and would clip on a 640x480 desktop. Two
 * things about that format are easy to get wrong and silently produce a
 * dialog that never appears, with no error anywhere -- every item must start
 * on a DWORD boundary, and the strings are UTF-16 even through the ANSI API.
 */
#include <windows.h>
#include <commctrl.h>
#include <string.h>

#include "app/workdlg.h"
#include "app/workcopy.h"
#include "app/workdir.h"
#include "core/log.h"
#include "core/str.h"
#include "sync/syncrpc.h"

#define ID_CUR      300
#define ID_SIZE     301
#define ID_REVIEW   302
#define ID_PATH     303
#define ID_BROWSE   304
#define ID_LEAVE    305
#define ID_BRING    306
#define ID_GROUP    307
#define ID_DEST     308

#define ID_CLASH    320
#define ID_KEEP     321
#define ID_REPLACE  322
#define ID_MERGE    323

#define ID_PBAR     340
#define ID_PTEXT    341

#define CLS_BUTTON  0x0080
#define CLS_EDIT    0x0081
#define CLS_STATIC  0x0082

typedef HINSTANCE (WINAPI *PFN_SHEXEC)(HWND, LPCSTR, LPCSTR, LPCSTR,
                                       LPCSTR, INT);

namespace {

/* The one template buffer. Fixed capacity: the templates are under a
 * kilobyte and cannot grow at runtime, so a heap allocation would add a
 * failure path for no benefit.
 *
 * AT FILE SCOPE, not inside Tmpl, and that is not a style choice. Three of
 * these are built in one call chain -- the dialog, then the collision
 * question, then the progress window -- and the compiler inlines all three
 * into `rc_workdlg_run`, which gave it a 6256-byte stack frame. A frame of
 * a page or more steps over the guard page and faults the moment the stack
 * has grown past its commit, which `make check` catches and which would
 * otherwise be a crash with no log on a machine with no debugger.
 *
 * Safe because only one template is ever being ASSEMBLED at a time: each is
 * finished and handed to the dialog manager before the next begins, all on
 * the one UI thread, and nothing reads the bytes after that call returns. */
BYTE g_tmpl[1536];

struct Tmpl {
    BYTE     *buf;
    unsigned  len;

    Tmpl() : buf(g_tmpl), len(0) { memset(g_tmpl, 0, sizeof(g_tmpl)); }

    void bytes(const void *p, unsigned n)
    {
        if (len + n > sizeof(g_tmpl)) { len = sizeof(g_tmpl) + 1; return; }
        memcpy(buf + len, p, n);
        len += n;
    }
    void w(WORD v)   { bytes(&v, sizeof(v)); }
    void dw(DWORD v) { bytes(&v, sizeof(v)); }
    void ss(short v) { bytes(&v, sizeof(v)); }

    /* An ASCII string as the UTF-16 the format requires. Our own captions are
     * ASCII by construction; this is not a general converter and must not be
     * used on anything the user or the bridge supplied. */
    void wide(const char *s)
    {
        for (; s && *s; s++) w((WORD)(unsigned char)*s);
        w(0);
    }
    void align(void)
    {
        while (len & 3) {
            if (len >= sizeof(g_tmpl)) { len = sizeof(g_tmpl) + 1; return; }
            buf[len++] = 0;
        }
    }
    int overflowed(void) const { return len > sizeof(g_tmpl); }
};

void item(Tmpl &t, DWORD style, short x, short y, short cx, short cy,
          WORD id, WORD cls, const char *text)
{
    t.align();
    t.dw(style | WS_CHILD | WS_VISIBLE);
    t.dw(0);
    t.ss(x); t.ss(y); t.ss(cx); t.ss(cy);
    t.w(id);
    t.w(0xFFFF); t.w(cls);
    t.wide(text);
    t.w(0);
}

void head(Tmpl &t, WORD count, short cx, short cy, const char *title)
{
    t.dw(DS_MODALFRAME | DS_SETFONT | DS_CENTER | WS_POPUP | WS_CAPTION
         | WS_SYSMENU);
    t.dw(0);
    t.w(count);
    t.ss(0); t.ss(0); t.ss(cx); t.ss(cy);
    t.w(0);
    t.w(0);
    t.wide(title);
    t.w(8);
    t.wide("MS Sans Serif");
}

/* Let the window repaint and the Cancel button be clickable while a copy is
 * running. A modal loop is not available here -- the copy is a synchronous
 * walk -- so the queue is drained by hand between files, which is the same
 * idiom the transfer code uses and the only one that works without a thread.
 * No thread on purpose: this machine has 28KB free after a turn. */
void pump(void)
{
    MSG m;
    while (PeekMessageA(&m, NULL, 0, 0, PM_REMOVE)) {
        TranslateMessage(&m);
        DispatchMessageA(&m);
    }
}

/* --- the progress window ------------------------------------------------- */

struct Progress {
    HWND dlg;
    int  cancelled;
};

INT_PTR CALLBACK prog_proc(HWND dlg, UINT msg, WPARAM wp, LPARAM lp)
{
    Progress *p = (Progress *)GetWindowLongA(dlg, DWL_USER);

    if (msg == WM_INITDIALOG) {
        SetWindowLongA(dlg, DWL_USER, (LONG)lp);
        return TRUE;
    }
    if (msg == WM_COMMAND && LOWORD(wp) == IDCANCEL) {
        if (p) p->cancelled = 1;
        EnableWindow(GetDlgItem(dlg, IDCANCEL), FALSE);
        SetDlgItemTextA(dlg, ID_PTEXT, "Stopping...");
        return TRUE;
    }
    if (msg == WM_CLOSE) {
        if (p) p->cancelled = 1;
        return TRUE;
    }
    (void)lp;
    return FALSE;
}

/* Modeless, so the message loop keeps pumping. One window covers copying AND
 * the first synchronisation pass that follows it, because from where the user
 * sits that is one wait -- and two bars for one action invite the belief that
 * the first one finishing means it is done. */
HWND progress_open(HWND parent, HINSTANCE inst, Progress *p)
{
    Tmpl t;

    head(t, 3, 210, 56, "Work Directory");
    item(t, 0, 7, 8, 196, 10, ID_PTEXT, CLS_STATIC, "Preparing...");
    item(t, WS_BORDER, 7, 22, 196, 12, ID_PBAR, CLS_STATIC, "");
    item(t, WS_TABSTOP | BS_PUSHBUTTON, 152, 38, 50, 14, IDCANCEL,
         CLS_BUTTON, "Cancel");
    if (t.overflowed()) return NULL;

    p->cancelled = 0;
    p->dlg = CreateDialogIndirectParamA(inst, (LPCDLGTEMPLATE)t.buf,
                                        parent, prog_proc, (LPARAM)p);
    if (!p->dlg) {
        rc_log("workdlg: progress window failed (%lu)",
               (unsigned long)GetLastError());
        return NULL;
    }
    /* The bar replaces the placeholder STATIC rather than being one of the
     * template's items: PROGRESS_CLASS is not a predefined dialog class, and
     * naming it in the template would need the class string inline. This is
     * shorter and it is the ICC_PROGRESS_CLASS registration in WinMain
     * finally being used for something. */
    {
        HWND ph = GetDlgItem(p->dlg, ID_PBAR);
        RECT r;
        if (ph && GetWindowRect(ph, &r)) {
            POINT tl; tl.x = r.left; tl.y = r.top;
            ScreenToClient(p->dlg, &tl);
            DestroyWindow(ph);
            ph = CreateWindowExA(0, PROGRESS_CLASSA, "",
                                 WS_CHILD | WS_VISIBLE,
                                 tl.x, tl.y, r.right - r.left,
                                 r.bottom - r.top, p->dlg,
                                 (HMENU)ID_PBAR, inst, NULL);
            if (ph) SendMessageA(ph, PBM_SETRANGE, 0, MAKELPARAM(0, 100));
        }
    }
    ShowWindow(p->dlg, SW_SHOW);
    pump();
    return p->dlg;
}

void progress_say(Progress *p, const char *text, unsigned done, unsigned total)
{
    HWND bar;
    if (!p || !p->dlg) return;
    SetDlgItemTextA(p->dlg, ID_PTEXT, text);
    bar = GetDlgItem(p->dlg, ID_PBAR);
    if (bar)
        SendMessageA(bar, PBM_SETPOS,
                     (WPARAM)(total ? (done * 100) / total : 0), 0);
    pump();
}

/* --- the collision question ---------------------------------------------- */

struct Clash {
    const rc_worksurvey *s;
    const char          *dst;
};

INT_PTR CALLBACK clash_proc(HWND dlg, UINT msg, WPARAM wp, LPARAM lp)
{
    if (msg == WM_INITDIALOG) {
        Clash *c = (Clash *)lp;
        char text[400];
        unsigned i, n = c->s->collisions;
        char names[256];
        names[0] = '\0';
        for (i = 0; i < n && i < RC_WORK_SHOWN; i++) {
            char one[80];
            rc_strf(one, sizeof(one), "%s%s", i ? ", " : "", c->s->shown[i]);
            rc_strcpy(names + strlen(names), sizeof(names) - strlen(names),
                      one);
        }
        if (n > RC_WORK_SHOWN) {
            char more[40];
            rc_strf(more, sizeof(more), " and %u more", n - RC_WORK_SHOWN);
            rc_strcpy(names + strlen(names), sizeof(names) - strlen(names),
                      more);
        }
        rc_strf(text, sizeof(text),
                "%u file%s already in %s %s the same name%s as %s you are "
                "bringing: %s.\n\nWhat should happen to them?",
                n, n == 1 ? "" : "s", c->dst, n == 1 ? "has" : "have",
                n == 1 ? "" : "s", n == 1 ? "one" : "ones", names);
        SetDlgItemTextA(dlg, ID_CLASH, text);
        return TRUE;
    }
    if (msg == WM_COMMAND) {
        WORD id = LOWORD(wp);
        if (id == ID_KEEP || id == ID_REPLACE || id == ID_MERGE
            || id == IDCANCEL) {
            EndDialog(dlg, id);
            return TRUE;
        }
    }
    if (msg == WM_CLOSE) { EndDialog(dlg, IDCANCEL); return TRUE; }
    (void)lp;
    return FALSE;
}

/* RC_WORK_KEEP / REPLACE / MERGE, or -1 for cancel. */
int ask_clash(HWND parent, HINSTANCE inst, const rc_worksurvey *s,
              const char *dst)
{
    Tmpl t;
    Clash c;
    INT_PTR r;

    c.s = s; c.dst = dst;

    head(t, 5, 252, 128, "Files With The Same Name");
    item(t, 0, 7, 7, 238, 44, ID_CLASH, CLS_STATIC, "");
    /* Push buttons rather than radios and an OK: each of these is a
     * different thing to do, the labels say which, and a radio group would
     * add a click for no information. Keep is first and is the default,
     * because it is the only one that cannot surprise. */
    item(t, WS_TABSTOP | BS_DEFPUSHBUTTON, 7, 56, 238, 14, ID_KEEP,
         CLS_BUTTON, "&Keep the files already there");
    item(t, WS_TABSTOP | BS_PUSHBUTTON, 7, 74, 238, 14, ID_REPLACE,
         CLS_BUTTON, "&Replace them with the ones I am bringing");
    item(t, WS_TABSTOP | BS_PUSHBUTTON, 7, 92, 238, 14, ID_MERGE,
         CLS_BUTTON, "Let the &assistant merge them");
    item(t, WS_TABSTOP | BS_PUSHBUTTON, 195, 110, 50, 14, IDCANCEL,
         CLS_BUTTON, "Cancel");
    if (t.overflowed()) return -1;

    r = DialogBoxIndirectParamA(inst, (LPCDLGTEMPLATE)t.buf, parent,
                                clash_proc, (LPARAM)&c);
    if (r == ID_KEEP)    return RC_WORK_KEEP;
    if (r == ID_REPLACE) return RC_WORK_REPLACE;
    if (r == ID_MERGE)   return RC_WORK_MERGE;
    return -1;
}

/* --- the copy, with its progress and its failures ------------------------ */

struct CopyRun {
    Progress *p;
    HWND      parent;
};

int on_progress(void *ctx, unsigned done, unsigned total, const char *rel)
{
    CopyRun *r = (CopyRun *)ctx;
    char line[160];
    rc_strf(line, sizeof(line), "Copying %u of %u: %s", done, total, rel);
    progress_say(r->p, line, done, total);
    return !r->p->cancelled;
}

int on_error(void *ctx, const char *rel, DWORD err)
{
    CopyRun *r = (CopyRun *)ctx;
    char text[320];
    int answer;

    /* Named, with the reason, and with all three answers. A copy that stops
     * on the first locked file is a copy nobody can finish, and one that
     * skips in silence is worse -- the folder then looks complete and is
     * not. */
    rc_strf(text, sizeof(text),
            "%s could not be copied (error %lu).\n\n"
            "Retry to try it again, Ignore to leave it behind and carry on, "
            "or Cancel to stop here. Nothing in the folder you are leaving "
            "has been touched either way.",
            rel, (unsigned long)err);
    answer = MessageBoxA(r->parent, text, "Work Directory",
                         MB_ABORTRETRYIGNORE | MB_ICONEXCLAMATION);
    if (answer == IDRETRY)  return RC_WORK_RETRY;
    if (answer == IDIGNORE) return RC_WORK_SKIP;
    return RC_WORK_CANCEL;
}

/* Stay up until the new folder has reached the assistant, or until it stops
 * arriving.
 *
 * There is no "the pass is finished" signal to wait for and inventing one
 * would be a wire change for a progress bar. So this watches the one fact
 * the guest already knows -- how many files its own sync layer has finished
 * moving -- and stops when that stops changing. `expect` is what the folder
 * holds, which is the right denominator for the common case and merely an
 * estimate when the bridge also has files to push down; the bar is capped
 * rather than allowed to read past the end.
 *
 * QUIET, not "done": a pass that moves nothing at all is the normal outcome
 * of pointing at an empty folder, and it must not sit here for a minute
 * proving it. The first stretch of quiet is short for that reason, and only
 * lengthens once something has started arriving.
 */
void wait_for_sync(Progress *p, unsigned expect)
{
    unsigned long base = rc_syncrpc_moved();
    unsigned long seen = 0, last = 0;
    DWORD quiet = GetTickCount();
    char line[120];

    for (;;) {
        DWORD idle;
        if (p->cancelled) return;

        seen = rc_syncrpc_moved() - base;
        if (seen != last) { last = seen; quiet = GetTickCount(); }

        if (expect && seen >= expect) return;

        idle = GetTickCount() - quiet;
        /* Twelve seconds once files have started moving -- the bridge sweeps
         * every thirty and a pass can pause mid-way -- and three before,
         * because "nothing to send" is the ordinary answer for an empty
         * folder and waiting twelve seconds to say so is the bar lying about
         * how long the work took. */
        if (idle > (seen ? 12000UL : 3000UL)) return;

        if (expect)
            rc_strf(line, sizeof(line), "Sending to the assistant: %lu of %u",
                    seen, expect);
        else
            rc_strf(line, sizeof(line), "Sending to the assistant: %lu",
                    seen);
        progress_say(p, line, (unsigned)(seen > expect ? expect : seen),
                     expect);
        Sleep(120);
    }
}

/* --- the dialog ---------------------------------------------------------- */

struct State {
    const char *current;
    unsigned    maxpath;
    unsigned    cur_files;
    unsigned long cur_bytes;
    unsigned    dst_files;          /* what the new folder already holds */
    char        chosen[MAX_PATH];
};

void set_size_line(HWND dlg, State *st)
{
    char line[160];
    if (!st->cur_files)
        rc_strcpy(line, sizeof(line), "It holds no files.");
    else
        rc_strf(line, sizeof(line), "It holds %u file%s, %lu KB.",
                st->cur_files, st->cur_files == 1 ? "" : "s",
                (st->cur_bytes + 1023) / 1024);
    SetDlgItemTextA(dlg, ID_SIZE, line);
}

/* What the folder being moved TO already holds, because every one of those
 * files is about to be read and sent. Pointing a chat at a real project is
 * the case this feature exists for and it is minutes on this link, so it is
 * stated before OK rather than discovered afterwards as an unexplained
 * transfer.
 *
 * Counted when the field loses focus and when Browse returns, NOT on every
 * keystroke: this walks a tree on a 200MHz machine, and doing that between
 * the `C` and the `:` would make the dialog unusable. */
void set_dest_line(HWND dlg, State *st)
{
    char want[MAX_PATH];
    char line[200];
    unsigned files = 0;
    unsigned long bytes = 0;

    GetDlgItemTextA(dlg, ID_PATH, want, sizeof(want));
    st->dst_files = 0;
    if (!want[0] || GetFileAttributesA(want) == 0xFFFFFFFF) {
        SetDlgItemTextA(dlg, ID_DEST,
                        want[0] ? "It does not exist yet; it will be created."
                                : "");
        return;
    }
    if (!rc_worksurvey_count(want, &files, &bytes)) {
        SetDlgItemTextA(dlg, ID_DEST, "");
        return;
    }
    st->dst_files = files;
    if (!files)
        rc_strcpy(line, sizeof(line), "It is empty.");
    else
        rc_strf(line, sizeof(line),
                "It holds %u file%s, %lu KB -- all of which will be read and "
                "sent to the assistant.",
                files, files == 1 ? "" : "s", (bytes + 1023) / 1024);
    SetDlgItemTextA(dlg, ID_DEST, line);
}

INT_PTR CALLBACK dlg_proc(HWND dlg, UINT msg, WPARAM wp, LPARAM lp)
{
    State *st = (State *)GetWindowLongA(dlg, DWL_USER);

    switch (msg) {
    case WM_INITDIALOG:
        st = (State *)lp;
        SetWindowLongA(dlg, DWL_USER, (LONG)st);
        SetDlgItemTextA(dlg, ID_CUR, st->current);
        set_size_line(dlg, st);
        SetDlgItemTextA(dlg, ID_PATH, st->current);
        SendDlgItemMessageA(dlg, ID_PATH, EM_LIMITTEXT, MAX_PATH - 1, 0);
        CheckRadioButton(dlg, ID_LEAVE, ID_BRING, ID_LEAVE);
        SetFocus(GetDlgItem(dlg, ID_PATH));
        /* Selected, because focus set by hand skips the selection the dialog
         * manager would have made -- and then typing a new path prepends it
         * to the old one, which is how C:\GastonC:\WORK happened once. */
        SendDlgItemMessageA(dlg, ID_PATH, EM_SETSEL, 0, (LPARAM)-1);
        set_dest_line(dlg, st);
        return FALSE;

    case WM_COMMAND:
        switch (LOWORD(wp)) {
        case ID_REVIEW:
            /* The user's own file manager on the folder they are deciding
             * about, so "review, then decide" is literal rather than an
             * instruction to go and find it.
             *
             * Resolved rather than imported, the same habit rc_workdir_browse
             * follows: shell32 is on every Windows this targets, but an
             * import that cannot resolve kills the image at load, before
             * WinMain, with no log -- and a Review button is not worth the
             * program refusing to start. */
            {
                HMODULE sh = LoadLibraryA("shell32.dll");
                PFN_SHEXEC go = sh ? (PFN_SHEXEC)GetProcAddress(
                                          sh, "ShellExecuteA") : NULL;
                if (go)
                    go(dlg, "open", st->current, NULL, NULL, SW_SHOWNORMAL);
                else
                    MessageBoxA(dlg, st->current, "Work Directory",
                                MB_OK | MB_ICONINFORMATION);
                if (sh) FreeLibrary(sh);
            }
            return TRUE;

        case ID_BROWSE: {
            char picked[MAX_PATH];
            char start[MAX_PATH];
            GetDlgItemTextA(dlg, ID_PATH, start, sizeof(start));
            if (rc_workdir_browse(dlg, start[0] ? start : st->current,
                                  picked, sizeof(picked))) {
                SetDlgItemTextA(dlg, ID_PATH, picked);
                set_dest_line(dlg, st);
            }
            return TRUE;
        }

        case ID_PATH:
            if (HIWORD(wp) == EN_KILLFOCUS) set_dest_line(dlg, st);
            return FALSE;

        case IDOK: {
            char want[MAX_PATH];
            GetDlgItemTextA(dlg, ID_PATH, want, sizeof(want));
            if (!want[0]) {
                MessageBoxA(dlg, "Choose a folder for this conversation to "
                                 "work in.", "Work Directory",
                            MB_OK | MB_ICONEXCLAMATION);
                SetFocus(GetDlgItem(dlg, ID_PATH));
                return TRUE;
            }
            set_dest_line(dlg, st);
            rc_strcpy(st->chosen, sizeof(st->chosen), want);
            EndDialog(dlg, IsDlgButtonChecked(dlg, ID_BRING) ? 2 : 1);
            return TRUE;
        }

        case IDCANCEL:
            EndDialog(dlg, 0);
            return TRUE;
        }
        return FALSE;

    case WM_CLOSE:
        EndDialog(dlg, 0);
        return TRUE;
    }
    (void)lp;
    return FALSE;
}

}  /* namespace */

int rc_workdlg_run(HWND parent, HINSTANCE inst, const char *current,
                   unsigned maxpath, rc_workdlg_propose propose,
                   char *out, unsigned cap)
{
    State st;
    Tmpl t;
    INT_PTR r;

    if (!out || cap < 2) return 0;
    out[0] = '\0';
    st.current = current ? current : "";
    st.maxpath = maxpath ? maxpath : MAX_PATH;
    st.chosen[0] = '\0';
    rc_worksurvey_count(st.current, &st.cur_files, &st.cur_bytes);

    /* NOTHING TO LOSE, NOTHING TO ASK. An unset or empty folder goes straight
     * to the browser: a radio group about zero files is noise, and the extra
     * dialog would be in the way of the common case rather than protecting
     * anything. */
    if (!st.current[0] || !st.cur_files) {
        if (!rc_workdir_browse(parent, st.current, out, cap)) return 0;
        if (propose) propose(out, 0);
        return 1;
    }

    head(t, 13, 252, 174, "Change Work Directory");
    item(t, 0, 7, 7, 238, 9, (WORD)-1, CLS_STATIC,
         "This conversation works in:");
    item(t, SS_PATHELLIPSIS, 12, 19, 168, 9, ID_CUR, CLS_STATIC, "");
    item(t, WS_TABSTOP | BS_PUSHBUTTON, 190, 16, 55, 14, ID_REVIEW,
         CLS_BUTTON, "Re&view...");
    item(t, 0, 12, 32, 168, 9, ID_SIZE, CLS_STATIC, "");
    item(t, 0, 7, 50, 238, 9, (WORD)-1, CLS_STATIC, "&New work directory:");
    item(t, WS_BORDER | WS_TABSTOP | ES_AUTOHSCROLL, 7, 61, 176, 12,
         ID_PATH, CLS_EDIT, "");
    item(t, WS_TABSTOP | BS_PUSHBUTTON, 190, 60, 55, 14, ID_BROWSE,
         CLS_BUTTON, "&Browse...");
    /* Two lines high: "It holds 312 files, 4180 KB -- all of which will be
     * read and sent to the assistant" does not fit on one at this width, and
     * a clipped sentence about a transfer is worse than no sentence. */
    item(t, 0, 12, 76, 233, 18, ID_DEST, CLS_STATIC, "");
    item(t, WS_GROUP | BS_GROUPBOX, 7, 98, 238, 46, ID_GROUP, CLS_BUTTON,
         " The files already there ");
    item(t, WS_TABSTOP | WS_GROUP | BS_AUTORADIOBUTTON, 14, 111, 224, 10,
         ID_LEAVE, CLS_BUTTON,
         "&Leave them where they are (still yours, still there)");
    item(t, WS_TABSTOP | BS_AUTORADIOBUTTON, 14, 125, 224, 10, ID_BRING,
         CLS_BUTTON, "Bring a &copy into the new folder");
    item(t, WS_TABSTOP | BS_DEFPUSHBUTTON, 137, 152, 50, 14, IDOK,
         CLS_BUTTON, "OK");
    item(t, WS_TABSTOP | BS_PUSHBUTTON, 195, 152, 50, 14, IDCANCEL,
         CLS_BUTTON, "Cancel");
    if (t.overflowed()) {
        rc_log("workdlg: template overflowed; not showing");
        return 0;
    }

    r = DialogBoxIndirectParamA(inst, (LPCDLGTEMPLATE)t.buf, parent,
                                dlg_proc, (LPARAM)&st);
    if (r <= 0) return 0;

    rc_strcpy(out, cap, st.chosen);
    if (r == 1) {
        /* LEAVE. Nothing moves on this side; the old folder keeps what it
         * has and stops being mirrored. The bridge starts a fresh folder of
         * its own and the first pass carries up whatever the new one holds
         * -- which for an existing project is the whole project, and is why
         * the bar stays up for it. */
        Progress p;
        rc_log("workdlg: %s -> %s, leaving %u files behind",
               st.current, out, st.cur_files);
        p.dlg = NULL;
        p.cancelled = 0;
        progress_open(parent, inst, &p);
        if (propose) propose(out, 0);
        progress_say(&p, "Sending to the assistant...", 0, st.dst_files);
        EnableWindow(parent, FALSE);
        wait_for_sync(&p, st.dst_files);
        EnableWindow(parent, TRUE);
        if (p.dlg) DestroyWindow(p.dlg);
        SetActiveWindow(parent);
        return 1;
    }

    /* BRING. Everything that can refuse, refuses now -- before a byte is
     * written -- so a refusal never leaves half a tree behind. */
    {
        rc_worksurvey s;
        int mode;
        Progress p;
        CopyRun run;
        unsigned copied = 0, failed = 0;

        if (!rc_worksurvey_run(st.current, out, st.maxpath, &s)) {
            char why[400];
            rc_worksurvey_why(&s, st.current, out, why, sizeof(why));
            MessageBoxA(parent, why, "Work Directory",
                        MB_OK | MB_ICONEXCLAMATION);
            return 0;
        }

        mode = RC_WORK_KEEP;
        if (s.collisions) {
            mode = ask_clash(parent, inst, &s, out);
            if (mode < 0) return 0;
        }

        p.dlg = NULL;
        p.cancelled = 0;
        progress_open(parent, inst, &p);
        run.p = &p;
        run.parent = p.dlg ? p.dlg : parent;
        progress_say(&p, "Copying...", 0, s.files);

        /* The frame is disabled for the duration. `pump` dispatches whatever
         * is in the queue so the bar repaints and Cancel works, which also
         * means every other button on the main window would work -- including
         * a second Change work directory, on top of the copy this one is
         * running. Modal by hand, because there is no modal loop here. */
        EnableWindow(parent, FALSE);
        rc_workcopy_run(st.current, out, mode, &s, on_progress, on_error,
                        &run, &copied, &failed);
        /* THE SAME WINDOW carries straight on into the transfer. From where
         * the user sits this is one wait, and closing the bar when the copy
         * finished would say the operation was over while the files were
         * still crossing the wire. */
        if (!p.cancelled) {
            if (propose) propose(out, mode == RC_WORK_MERGE);
            progress_say(&p, "Sending to the assistant...", 0,
                         st.dst_files + copied);
            wait_for_sync(&p, st.dst_files + copied);
        }
        EnableWindow(parent, TRUE);
        if (p.dlg) DestroyWindow(p.dlg);
        /* Back to the frame, or the taskbar keeps the focus the destroyed
         * window left behind and the app looks inactive. */
        SetActiveWindow(parent);

        /* Say where things went, once, and only when there is something to
         * say. A staged copy nobody is told about is a copy nobody finds --
         * and under "let the assistant merge them" the folder IS the
         * instruction, both to the user and to the agent that will read it. */
        if (failed || s.collisions) {
            char text[400];
            char tail[200];
            tail[0] = '\0';
            if (mode == RC_WORK_MERGE)
                rc_strf(tail, sizeof(tail),
                        "\n\nThe %u incoming file%s that clashed %s in "
                        "%s\\%s, for the assistant to merge.",
                        s.collisions, s.collisions == 1 ? "" : "s",
                        s.collisions == 1 ? "is" : "are", out,
                        RC_WORK_INCOMING);
            else if (mode == RC_WORK_REPLACE)
                rc_strf(tail, sizeof(tail),
                        "\n\nThe %u file%s that %s overwritten %s kept in "
                        "%s\\%s.",
                        s.collisions, s.collisions == 1 ? "" : "s",
                        s.collisions == 1 ? "was" : "were",
                        s.collisions == 1 ? "is" : "are", out,
                        RC_WORK_REPLACED);
            else if (s.collisions)
                rc_strf(tail, sizeof(tail),
                        "\n\nThe %u file%s that clashed %s not brought, and "
                        "%s still in %s.",
                        s.collisions, s.collisions == 1 ? "" : "s",
                        s.collisions == 1 ? "was" : "were",
                        s.collisions == 1 ? "is" : "are", st.current);

            if (failed)
                rc_strf(text, sizeof(text),
                        "%u file%s copied. %u could not be, and %s still in "
                        "%s.%s", copied, copied == 1 ? "" : "s", failed,
                        failed == 1 ? "is" : "are", st.current, tail);
            else
                rc_strf(text, sizeof(text), "%u file%s copied.%s",
                        copied, copied == 1 ? "" : "s", tail);
            MessageBoxA(parent, text, "Work Directory",
                        MB_OK | MB_ICONINFORMATION);
        }
        rc_log("workdlg: %s -> %s, mode %d, copied %u, failed %u",
               st.current, out, mode, copied, failed);
        if (p.cancelled) {
            /* Nothing was proposed, so the conversation is still working
             * where it was -- and the files that did copy are sitting in the
             * folder that was not chosen. Say both, because either one on its
             * own is misleading. */
            char text[320];
            rc_strf(text, sizeof(text),
                    "Stopped. This conversation still works in %s, and %u "
                    "file%s already copied into %s. Nothing there was "
                    "removed.", st.current, copied, copied == 1 ? " was" : "s "
                    "were", out);
            MessageBoxA(parent, text, "Work Directory",
                        MB_OK | MB_ICONINFORMATION);
            out[0] = '\0';
            return 0;
        }
    }
    return 1;
}
