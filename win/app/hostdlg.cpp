/* hostdlg.cpp -- the Server Host dialog. See app/hostdlg.h.
 *
 * The template is assembled by hand into a byte buffer. Two things about
 * that format are easy to get wrong and silently produce a dialog that does
 * not appear, with no error anywhere:
 *
 *   * Every DLGITEMTEMPLATE must start on a DWORD boundary. Miss an
 *     alignment and CreateDialogIndirect reads the next item's style out of
 *     the middle of the previous item's title.
 *   * The strings are UTF-16 even in the ANSI API. DialogBoxIndirectParamA
 *     refers to the parameter block, not to the template's text.
 */
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app/hostdlg.h"
#include "core/log.h"
#include "core/str.h"

#define ID_HOST_EDIT 200
#define ID_PORT_EDIT 201

/* Dialog-unit geometry. Units, not pixels, so this scales with the system
 * font rather than assuming 96 dpi. */
#define DLG_W    186
#define DLG_H     74
#define LABEL_W   28
#define EDIT_X    34
#define EDIT_W   144

namespace {

/* A growing, DWORD-aligning byte buffer. Fixed capacity: the template below
 * is about 400 bytes and cannot grow at runtime, so a heap allocation would
 * add a failure path for no benefit. */
struct Tmpl {
    BYTE  buf[1024];
    unsigned len;

    Tmpl() : len(0) { memset(buf, 0, sizeof(buf)); }

    void bytes(const void *p, unsigned n)
    {
        if (len + n > sizeof(buf)) { len = sizeof(buf) + 1; return; }
        memcpy(buf + len, p, n);
        len += n;
    }
    void w(WORD v)  { bytes(&v, sizeof(v)); }
    void dw(DWORD v) { bytes(&v, sizeof(v)); }
    void ss(short v) { bytes(&v, sizeof(v)); }

    /* An ASCII string as the UTF-16 the template format requires. Our own
     * captions are ASCII by construction; this is not a general converter
     * and must not be used on anything the user or the bridge supplied. */
    void wide(const char *s)
    {
        for (; s && *s; s++) w((WORD)(unsigned char)*s);
        w(0);
    }
    void align(void)
    {
        while (len & 3) {
            if (len >= sizeof(buf)) { len = sizeof(buf) + 1; return; }
            buf[len++] = 0;
        }
    }
    int overflowed(void) const { return len > sizeof(buf); }
};

void add_item(Tmpl &t, DWORD style, short x, short y, short cx, short cy,
              WORD id, WORD cls, const char *text)
{
    t.align();
    t.dw(style | WS_CHILD | WS_VISIBLE);
    t.dw(0);                    /* extended style */
    t.ss(x); t.ss(y); t.ss(cx); t.ss(cy);
    t.w(id);
    t.w(0xFFFF); t.w(cls);      /* the predefined-class form */
    t.wide(text);
    t.w(0);                     /* no creation data */
}

#define CLS_BUTTON 0x0080
#define CLS_EDIT   0x0081
#define CLS_STATIC 0x0082

struct DlgState {
    rc_config *cfg;
    int        changed;
};

INT_PTR CALLBACK proc(HWND dlg, UINT msg, WPARAM wp, LPARAM lp)
{
    DlgState *st = (DlgState *)GetWindowLongA(dlg, DWL_USER);

    switch (msg) {
    case WM_INITDIALOG: {
        char port[16];
        st = (DlgState *)lp;
        SetWindowLongA(dlg, DWL_USER, (LONG)st);
        SetDlgItemTextA(dlg, ID_HOST_EDIT, st->cfg->host);
        rc_strf(port, sizeof(port), "%d", st->cfg->port);
        SetDlgItemTextA(dlg, ID_PORT_EDIT, port);
        /* Cap what can be typed at what the struct can hold, rather than
         * truncating on the way out. rc_config::host is char[64]. */
        SendDlgItemMessageA(dlg, ID_HOST_EDIT, EM_LIMITTEXT,
                            (WPARAM)(sizeof(st->cfg->host) - 1), 0);
        SendDlgItemMessageA(dlg, ID_PORT_EDIT, EM_LIMITTEXT, 5, 0);
        SetFocus(GetDlgItem(dlg, ID_HOST_EDIT));
        return FALSE;           /* focus set above, do not let the dialog */
    }

    case WM_COMMAND:
        if (LOWORD(wp) == IDOK) {
            char host[64], port[16];
            int p;
            GetDlgItemTextA(dlg, ID_HOST_EDIT, host, sizeof(host));
            GetDlgItemTextA(dlg, ID_PORT_EDIT, port, sizeof(port));
            p = atoi(port);

            /* Refuse rather than silently keep the old value: a dialog that
             * accepts what you typed and then does something else is worse
             * than one that argues. */
            if (!host[0]) {
                MessageBoxA(dlg, "Enter a host name or IP address.",
                            "Server Host", MB_OK | MB_ICONEXCLAMATION);
                SetFocus(GetDlgItem(dlg, ID_HOST_EDIT));
                return TRUE;
            }
            if (p < 1 || p > 65535) {
                MessageBoxA(dlg, "The port must be between 1 and 65535.",
                            "Server Host", MB_OK | MB_ICONEXCLAMATION);
                SetFocus(GetDlgItem(dlg, ID_PORT_EDIT));
                return TRUE;
            }
            if (st) {
                st->changed = (strcmp(host, st->cfg->host) != 0
                               || p != st->cfg->port);
                rc_strcpy(st->cfg->host, sizeof(st->cfg->host), host);
                st->cfg->port = p;
            }
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

}  /* namespace */

int rc_hostdlg_run(HWND parent, HINSTANCE inst, rc_config *cfg)
{
    Tmpl t;
    DlgState st;

    st.cfg = cfg;
    st.changed = 0;

    /* DS_SETFONT so the dialog uses MS Sans Serif 8 rather than the system
     * font, matching every other control in the app. */
    t.dw(DS_MODALFRAME | DS_SETFONT | DS_CENTER | WS_POPUP | WS_CAPTION
         | WS_SYSMENU);
    t.dw(0);
    t.w(6);                                     /* control count */
    t.ss(0); t.ss(0); t.ss(DLG_W); t.ss(DLG_H);
    t.w(0);                                     /* no menu */
    t.w(0);                                     /* default class */
    t.wide("Server Host");
    t.w(8);                                     /* point size */
    t.wide("MS Sans Serif");

    add_item(t, SS_RIGHT, 6, 10, LABEL_W, 10, (WORD)-1, CLS_STATIC, "&Host:");
    add_item(t, WS_BORDER | WS_TABSTOP | ES_AUTOHSCROLL,
             EDIT_X, 8, EDIT_W, 12, ID_HOST_EDIT, CLS_EDIT, "");
    add_item(t, SS_RIGHT, 6, 28, LABEL_W, 10, (WORD)-1, CLS_STATIC, "&Port:");
    add_item(t, WS_BORDER | WS_TABSTOP | ES_AUTOHSCROLL | ES_NUMBER,
             EDIT_X, 26, 44, 12, ID_PORT_EDIT, CLS_EDIT, "");
    add_item(t, WS_TABSTOP | BS_DEFPUSHBUTTON,
             DLG_W - 108, 52, 50, 14, IDOK, CLS_BUTTON, "OK");
    add_item(t, WS_TABSTOP | BS_PUSHBUTTON,
             DLG_W - 54, 52, 50, 14, IDCANCEL, CLS_BUTTON, "Cancel");

    if (t.overflowed()) {
        rc_log("hostdlg: template overflowed; not showing");
        return 0;
    }

    {
        INT_PTR r = DialogBoxIndirectParamA(inst, (LPCDLGTEMPLATE)t.buf,
                                            parent, proc, (LPARAM)&st);
        if (r == -1) {
            rc_log("hostdlg: DialogBoxIndirectParam failed (%lu)",
                   (unsigned long)GetLastError());
            return 0;
        }
        if (!r) return 0;
    }
    rc_log("hostdlg: host=%s port=%d changed=%d",
           cfg->host, cfg->port, st.changed);
    return st.changed;
}

/* ------------------------------------------------- the one-line ask box */

namespace {

#define ID_ASK_EDIT 210

struct AskState {
    char    *buf;
    unsigned cap;
};

INT_PTR CALLBACK ask_proc(HWND dlg, UINT msg, WPARAM wp, LPARAM lp)
{
    AskState *st = (AskState *)GetWindowLongA(dlg, DWL_USER);

    switch (msg) {
    case WM_INITDIALOG:
        st = (AskState *)lp;
        SetWindowLongA(dlg, DWL_USER, (LONG)st);
        SetDlgItemTextA(dlg, ID_ASK_EDIT, st->buf);
        /* Capped at what the caller's buffer holds rather than truncated on
         * the way out -- the same reason hostdlg caps the host field. */
        SendDlgItemMessageA(dlg, ID_ASK_EDIT, EM_LIMITTEXT,
                            (WPARAM)(st->cap - 1), 0);
        SetFocus(GetDlgItem(dlg, ID_ASK_EDIT));
        return FALSE;

    case WM_COMMAND:
        if (LOWORD(wp) == IDOK) {
            if (st) GetDlgItemTextA(dlg, ID_ASK_EDIT, st->buf, (int)st->cap);
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

}  /* namespace */

int rc_askdlg_run(HWND parent, HINSTANCE inst, const char *title,
                  const char *note, const char *label,
                  char *buf, unsigned cap)
{
    Tmpl t;
    AskState st;

    if (!buf || cap < 2) return 0;
    st.buf = buf;
    st.cap = cap;

    t.dw(DS_MODALFRAME | DS_SETFONT | DS_CENTER | WS_POPUP | WS_CAPTION
         | WS_SYSMENU);
    t.dw(0);
    t.w(5);                                     /* control count */
    t.ss(0); t.ss(0); t.ss(DLG_W); t.ss(88);
    t.w(0);
    t.w(0);
    t.wide(title ? title : "");
    t.w(8);
    t.wide("MS Sans Serif");

    /* The note spans the width and wraps: it is a sentence, and a static
     * sized to one line would simply clip the rest with nothing to show that
     * it had. */
    /* Four lines of room for the note, measured on the guest rather than
     * guessed: three fit the sentence below at 8pt MS Sans Serif and the
     * fourth is the margin a longer one gets. A static sized to what the
     * text needs today clips the rest with nothing on screen to show it
     * did -- which is exactly what the first version of this dialog did. */
    add_item(t, SS_LEFT, 6, 6, DLG_W - 12, 34, (WORD)-1, CLS_STATIC,
             note ? note : "");
    add_item(t, SS_RIGHT, 6, 46, 40, 10, (WORD)-1, CLS_STATIC,
             label ? label : "");
    add_item(t, WS_BORDER | WS_TABSTOP | ES_AUTOHSCROLL,
             50, 44, DLG_W - 56, 12, ID_ASK_EDIT, CLS_EDIT, "");
    add_item(t, WS_TABSTOP | BS_DEFPUSHBUTTON,
             DLG_W - 108, 66, 50, 14, IDOK, CLS_BUTTON, "OK");
    add_item(t, WS_TABSTOP | BS_PUSHBUTTON,
             DLG_W - 54, 66, 50, 14, IDCANCEL, CLS_BUTTON, "Cancel");

    if (t.overflowed()) {
        rc_log("askdlg: template overflowed; not showing");
        return 0;
    }
    {
        INT_PTR r = DialogBoxIndirectParamA(inst, (LPCDLGTEMPLATE)t.buf,
                                            parent, ask_proc, (LPARAM)&st);
        if (r == -1) {
            rc_log("askdlg: DialogBoxIndirectParam failed (%lu)",
                   (unsigned long)GetLastError());
            return 0;
        }
        return r ? 1 : 0;
    }
}
