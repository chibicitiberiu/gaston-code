/* dialogpage.cpp -- see core/dialogpage.h. */
#include <windows.h>
#include <prsht.h>

#include "core/dialogpage.h"

void DialogPage::changed()
{
    if (m_hwnd) PropSheet_Changed(GetParent(m_hwnd), m_hwnd);
}

BOOL CALLBACK DialogPage::proc(HWND dlg, UINT msg, WPARAM wp, LPARAM lp)
{
    DialogPage *self = (DialogPage *)GetWindowLongA(dlg, DWL_USER);

    if (msg == WM_INITDIALOG) {
        const PROPSHEETPAGEA *psp = (const PROPSHEETPAGEA *)lp;
        self = psp ? (DialogPage *)psp->lParam : 0;
        SetWindowLongA(dlg, DWL_USER, (LONG)self);
        if (!self) return FALSE;
        self->m_hwnd = dlg;
        self->onInit();
        return TRUE;
    }
    if (!self) return FALSE;

    switch (msg) {
    case WM_COMMAND:
        return self->onCommand(LOWORD(wp), HIWORD(wp), (HWND)lp) ? TRUE
                                                                : FALSE;
    case WM_NOTIFY: {
        const NMHDR *nm = (const NMHDR *)lp;
        if (nm->code == PSN_KILLACTIVE) {
            SetWindowLongA(dlg, DWL_MSGRESULT,
                           self->onKillActive() ? FALSE : TRUE);
            return TRUE;
        }
        if (nm->code == PSN_APPLY) {
            self->onApply();
            SetWindowLongA(dlg, DWL_MSGRESULT, PSNRET_NOERROR);
            return TRUE;
        }
        break;
    }
    case WM_NCDESTROY:
        self->m_hwnd = 0;
        SetWindowLongA(dlg, DWL_USER, 0);
        return FALSE;
    }
    return self->handle(msg, wp, lp);
}
