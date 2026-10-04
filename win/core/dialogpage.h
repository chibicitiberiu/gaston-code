/* A dialog whose window someone ELSE creates and destroys: the third shape
 * beside core/window.h's Window and SubclassedWindow.
 *
 * A property sheet page is created by comctl32, with comctl32's own
 * CreateDialogIndirectParam, the first time its tab is selected, and is
 * destroyed by comctl32 when the sheet closes. A Window would own an HWND it
 * never created and DestroyWindow it a second time; so this one holds the
 * handle WITHOUT owning it, and forgets it at WM_NCDESTROY.
 *
 * `this` arrives in the PROPSHEETPAGE's lParam at WM_INITDIALOG and is kept
 * in DWL_USER. A page never calls EndDialog -- it is a modeless child of the
 * sheet, and EndDialog on one is undefined.
 */
#ifndef RC_DIALOGPAGE_H
#define RC_DIALOGPAGE_H

#ifndef __cplusplus
#error "core/dialogpage.h is C++"
#endif

#include <windows.h>

class DialogPage
{
public:
    virtual ~DialogPage() {}

    HWND hwnd() const { return m_hwnd; }

    /* The DLGPROC to put in PROPSHEETPAGE.pfnDlgProc, with `this` in its
     * lParam. */
    static BOOL CALLBACK proc(HWND dlg, UINT msg, WPARAM wp, LPARAM lp);

protected:
    DialogPage() : m_hwnd(0) {}

    /* WM_INITDIALOG: fill the controls. */
    virtual void onInit() {}
    /* WM_COMMAND. True if handled. */
    virtual bool onCommand(WORD id, WORD code, HWND ctl)
    { (void)id; (void)code; (void)ctl; return false; }
    /* PSN_KILLACTIVE: false keeps the person on this page (say why first). */
    virtual bool onKillActive() { return true; }
    /* PSN_APPLY: write the controls back. */
    virtual void onApply() {}
    /* Anything else; FALSE for the default. */
    virtual BOOL handle(UINT msg, WPARAM wp, LPARAM lp)
    { (void)msg; (void)wp; (void)lp; return FALSE; }

    /* Light the sheet's Apply button. */
    void changed();

private:
    DialogPage(const DialogPage &);
    DialogPage &operator=(const DialogPage &);

    HWND m_hwnd;
};

#endif
