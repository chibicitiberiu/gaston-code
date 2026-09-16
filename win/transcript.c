#include <windows.h>
#include <richedit.h>
#include <string.h>
#include <stdio.h>
#include "transcript.h"
#include "log.h"

/* Style table. One CHARFORMAT2 per style, built once.
 *
 * Fonts are chosen for a bitmap-font machine: Courier New for code (TrueType,
 * present on stock Win98, scales cleanly) and the system UI font for prose.
 */
typedef struct {
    const char *face;
    DWORD       effects;
    COLORREF    colour;
    int         mono;
} style_def;

static const style_def STYLES[RC_STYLE__COUNT] = {
    /* PLAIN    */ { NULL,          0,         RGB(0, 0, 0),       0 },
    /* CODE     */ { "Courier New", 0,         RGB(0, 0, 128),     1 },
    /* BOLD     */ { NULL,          CFE_BOLD,  RGB(0, 0, 0),       0 },
    /* THINKING */ { NULL,          CFE_ITALIC,RGB(128, 128, 128), 0 },
    /* TOOL     */ { "Courier New", 0,         RGB(0, 100, 0),     1 },
    /* ERROR    */ { NULL,          CFE_BOLD,  RGB(160, 0, 0),     0 },
    /* USER     */ { NULL,          CFE_BOLD,  RGB(0, 0, 160),     0 }
};

static int g_font_height = 0;   /* twips, from the system message font */

static void pick_ui_font(char *face, unsigned n, int *height_twips)
{
    NONCLIENTMETRICSA ncm;
    HDC dc;

    /* Do not hardcode a font and do not test for Tahoma. Ask the box what it
     * actually uses. sizeof(NONCLIENTMETRICS) is Win98-correct only because
     * WINVER is pinned at 0x0400 -- winuser.h adds iPaddedBorderWidth at
     * 0x0600, which would make this call fail on the guest. */
    memset(&ncm, 0, sizeof(ncm));
    ncm.cbSize = sizeof(ncm);
    if (SystemParametersInfoA(SPI_GETNONCLIENTMETRICS, sizeof(ncm), &ncm, 0)) {
        _snprintf(face, n - 1, "%s", ncm.lfMessageFont.lfFaceName);
        face[n - 1] = '\0';
        dc = GetDC(NULL);
        *height_twips = -MulDiv(ncm.lfMessageFont.lfHeight, 1440,
                                GetDeviceCaps(dc, LOGPIXELSY));
        ReleaseDC(NULL, dc);
        if (*height_twips < 120) *height_twips = 160;   /* sanity: ~8pt */
    } else {
        _snprintf(face, n - 1, "%s", "MS Sans Serif");
        *height_twips = 160;
    }
}

int rc_transcript_create(rc_transcript *t, HWND parent, int id, HINSTANCE inst)
{
    memset(t, 0, sizeof(*t));

    /* riched20.dll must be loaded BEFORE CreateWindowEx or the class is not
     * registered and the create returns NULL. There is no InitCommonControls
     * equivalent for RichEdit. */
    t->riched = LoadLibraryA("riched20.dll");
    if (!t->riched) {
        rc_log("transcript: LoadLibrary(riched20.dll) failed: %lu",
               GetLastError());
        return -1;
    }

    t->hwnd = CreateWindowExA(
        WS_EX_CLIENTEDGE, RICHEDIT_CLASSA, "",
        WS_CHILD | WS_VISIBLE | WS_VSCROLL | ES_MULTILINE |
        ES_READONLY | ES_AUTOVSCROLL | ES_NOHIDESEL,
        0, 0, 0, 0, parent, (HMENU)(INT_PTR)id, inst, NULL);

    if (!t->hwnd) {
        rc_log("transcript: CreateWindowEx(RichEdit20A) failed: %lu",
               GetLastError());
        return -1;
    }

    /* Default text limit is 32767 characters, which one long turn exceeds.
     * Pagination keeps the real figure far below this; the cap is a backstop. */
    SendMessageA(t->hwnd, EM_EXLIMITTEXT, 0, (LPARAM)(1024L * 1024L));

    /* Read-only transcript: the undo queue is pure overhead and unbounded
     * memory growth on a 32MB box. */
    SendMessageA(t->hwnd, EM_SETUNDOLIMIT, 0, 0);

    /* No notifications we care about; suppressing them avoids a round trip
     * into our WndProc on every single append. */
    SendMessageA(t->hwnd, EM_SETEVENTMASK, 0, 0);

    SendMessageA(t->hwnd, EM_SETBKGNDCOLOR, 0, (LPARAM)RGB(255, 255, 255));

    {
        char face[LF_FACESIZE];
        pick_ui_font(face, sizeof(face), &g_font_height);
        rc_log("transcript: ui font '%s' %d twips", face, g_font_height);
    }
    return 0;
}

void rc_transcript_destroy(rc_transcript *t)
{
    if (t->hwnd) { DestroyWindow(t->hwnd); t->hwnd = NULL; }
    if (t->riched) { FreeLibrary(t->riched); t->riched = NULL; }
}

int rc_transcript_at_bottom(rc_transcript *t)
{
    SCROLLINFO si;
    memset(&si, 0, sizeof(si));
    si.cbSize = sizeof(si);
    si.fMask = SIF_ALL;
    if (!GetScrollInfo(t->hwnd, SB_VERT, &si)) return 1;
    if (si.nMax <= 0) return 1;
    /* Within one line of the end counts as "at the bottom". */
    return (si.nPos + (int)si.nPage) >= (si.nMax - 1);
}

void rc_transcript_begin(rc_transcript *t)
{
    if (t->batching++) return;
    /* Remember bottom-ness BEFORE appending: autoscrolling a reader who has
     * deliberately scrolled up is the single thing that most makes a streaming
     * UI unusable. */
    t->was_at_bottom = rc_transcript_at_bottom(t);
    SendMessageA(t->hwnd, WM_SETREDRAW, FALSE, 0);
}

void rc_transcript_end(rc_transcript *t)
{
    if (--t->batching > 0) return;
    t->batching = 0;

    SendMessageA(t->hwnd, WM_SETREDRAW, TRUE, 0);

    if (t->was_at_bottom) {
        SendMessageA(t->hwnd, WM_VSCROLL, SB_BOTTOM, 0);
    }
    InvalidateRect(t->hwnd, NULL, TRUE);
    UpdateWindow(t->hwnd);
}

void rc_transcript_append(rc_transcript *t, const char *text, rc_style style)
{
    CHARFORMAT2A cf;
    CHARRANGE cr;
    LONG len;
    const style_def *sd;

    if (!t->hwnd || !text || !*text) return;
    if (style < 0 || style >= RC_STYLE__COUNT) style = RC_STYLE_PLAIN;
    sd = &STYLES[style];

    /* Move an EMPTY selection to the end. Do NOT use (-1,-1): "end of text" is
     * not a documented meaning for it, only "deselect". Compute the length. */
    len = (LONG)SendMessageA(t->hwnd, WM_GETTEXTLENGTH, 0, 0);
    cr.cpMin = len;
    cr.cpMax = len;
    SendMessageA(t->hwnd, EM_EXSETSEL, 0, (LPARAM)&cr);

    /* Set the format of the zero-length selection -- this becomes the "typing"
     * format that EM_REPLACESEL inserts with. There is no single call to
     * append a formatted run in RichEdit 2.0, so mixed formatting is always
     * this alternating pair. */
    memset(&cf, 0, sizeof(cf));
    cf.cbSize = sizeof(cf);
    cf.dwMask = CFM_BOLD | CFM_ITALIC | CFM_COLOR | CFM_SIZE;
    cf.dwEffects = sd->effects;
    cf.crTextColor = sd->colour;
    cf.yHeight = g_font_height ? g_font_height : 160;
    if (sd->face) {
        cf.dwMask |= CFM_FACE;
        _snprintf(cf.szFaceName, LF_FACESIZE - 1, "%s", sd->face);
    }
    SendMessageA(t->hwnd, EM_SETCHARFORMAT, SCF_SELECTION, (LPARAM)&cf);

    /* wParam FALSE: do not add an undo record. */
    SendMessageA(t->hwnd, EM_REPLACESEL, FALSE, (LPARAM)text);
}

void rc_transcript_clear(rc_transcript *t)
{
    SetWindowTextA(t->hwnd, "");
}
