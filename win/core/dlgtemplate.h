/* A DLGTEMPLATE assembled in memory, one heap buffer per dialog.
 *
 * Two copies of this builder lived in hostdlg.cpp and workdlg.cpp, both over
 * a FIXED buffer sized for one small dialog -- safe there because a template
 * is handed to the dialog manager and never read again. A property sheet
 * breaks that: comctl32 creates a page the first time its TAB is selected,
 * so every page's template must stay valid until PropertySheet returns, and
 * one page of the Options dialog is larger than either old buffer. So each
 * template here owns its bytes, on the heap (a page of stack is the frame
 * size `make check` refuses), capped at RC_DLGT_MAX.
 *
 * Two traps of the format, both silent when missed: every DLGITEMTEMPLATE
 * starts on a DWORD boundary, and every string is UTF-16 even in the ANSI
 * API. `wide` converts OUR OWN captions, which are ASCII by construction --
 * never text a person or the bridge supplied.
 */
#ifndef RC_DLGTEMPLATE_H
#define RC_DLGTEMPLATE_H

#ifndef __cplusplus
#error "core/dlgtemplate.h is C++"
#endif

#include <windows.h>

#define RC_DLGT_MAX (8u * 1024u)

/* The predefined control classes, by atom. */
#define RC_DLGT_BUTTON   0x0080
#define RC_DLGT_EDIT     0x0081
#define RC_DLGT_STATIC   0x0082
#define RC_DLGT_LISTBOX  0x0083
#define RC_DLGT_COMBOBOX 0x0085

/* A property-sheet page: a child, a control parent so Tab moves between the
 * page and the sheet's own buttons, and a caption that becomes the tab. */
#define RC_DLGT_PAGE (WS_CHILD | DS_CONTROL | DS_3DLOOK | DS_SETFONT | \
                      WS_CAPTION)
/* A modal dialog of our own. */
#define RC_DLGT_MODAL (DS_MODALFRAME | DS_SETFONT | DS_CENTER | WS_POPUP | \
                       WS_CAPTION | WS_SYSMENU)

class DlgTemplate
{
public:
    DlgTemplate();
    ~DlgTemplate();

    /* The dialog itself. The control count is filled in as items are
     * added, so it cannot disagree with them. */
    void begin(DWORD style, short cx, short cy, const char *title);
    /* One control, of a predefined class... */
    void item(DWORD style, short x, short y, short cx, short cy, WORD id,
              WORD cls, const char *text);
    /* ...or of a registered class by name ("msctls_trackbar32"). */
    void item(DWORD style, short x, short y, short cx, short cy, WORD id,
              const char *cls, const char *text);

    /* NULL if anything did not fit or memory ran out; the dialog is then
     * not shown, and the caller logs why. */
    const DLGTEMPLATE *get() const;

private:
    DlgTemplate(const DlgTemplate &);
    DlgTemplate &operator=(const DlgTemplate &);

    void bytes(const void *p, unsigned n);
    void w(WORD v) { bytes(&v, sizeof v); }
    void dw(DWORD v) { bytes(&v, sizeof v); }
    void wide(const char *s);
    void align();
    void head(DWORD style, short x, short y, short cx, short cy, WORD id);

    BYTE    *m_buf;
    unsigned m_len;
    bool     m_bad;
    WORD     m_count;
};

#endif
