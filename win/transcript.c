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

/* Suppressing redraw around a batch of appends is an optimisation; drawing the
 * text at all is not. Kept as a switch because separating the two is what
 * found the priming bug above -- with it off, the control was still blank,
 * which ruled out redraw batching as the cause. */
static int g_batch_redraw = 1;

static void prime(rc_transcript *t);

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
        /* NOT ES_READONLY at create time. A read-only RichEdit silently
         * ignores EM_REPLACESEL, which is the only way to append a formatted
         * run -- the control stays empty and nothing reports an error.
         * Read-only is applied with EM_SETREADONLY below and lifted around
         * each append batch instead.
         *
         * Wine does not enforce this, so `make wine-smoke` passed throughout
         * and the transcript was blank the first time it ran on real Win98.
         * A reminder that the Wine tier checks code sanity, never behaviour. */
        WS_CHILD | WS_VISIBLE | WS_VSCROLL | ES_MULTILINE |
        ES_AUTOVSCROLL | ES_NOHIDESEL,
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

    /* TEMPORARY: a distinctive background answers "is the white rectangle we
     * are looking at actually this control?" without a debugger. */
    SendMessageA(t->hwnd, EM_SETBKGNDCOLOR, 0, (LPARAM)RGB(255, 255, 255));



    /* The transcript is not user-editable; see the note at CreateWindowEx for
     * why this is a message and not a window style. */
    SendMessageA(t->hwnd, EM_SETREADONLY, TRUE, 0);

    {
        char face[LF_FACESIZE];
        pick_ui_font(face, sizeof(face), &g_font_height);
        rc_log("transcript: hwnd=%p ui font '%s' %d twips",
               (void *)t->hwnd, face, g_font_height);
    }
    prime(t);
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
    t->appends = 0;
    t->len_at_begin = (long)SendMessageA(t->hwnd, WM_GETTEXTLENGTH, 0, 0);
    SendMessageA(t->hwnd, EM_SETREADONLY, FALSE, 0);
    if (g_batch_redraw) SendMessageA(t->hwnd, WM_SETREDRAW, FALSE, 0);
}

void rc_transcript_end(rc_transcript *t)
{
    if (--t->batching > 0) return;
    t->batching = 0;

    if (g_batch_redraw) SendMessageA(t->hwnd, WM_SETREDRAW, TRUE, 0);
    SendMessageA(t->hwnd, EM_SETREADONLY, TRUE, 0);

    /* A swallowed append is invisible by definition: no error, no exception,
     * just a control that stays empty. Say so once per batch rather than
     * leaving the next person to diagnose a blank window by screenshot. */
    if (t->appends) {
        long now = (long)SendMessageA(t->hwnd, WM_GETTEXTLENGTH, 0, 0);
        if (now == t->len_at_begin)
            rc_log("transcript: %d appends changed nothing (len still %ld) -- "
                   "the control is refusing EM_REPLACESEL", t->appends, now);
        else if (!t->diag_done) {
            /* Once per session: says whether the text is actually in the
             * control and where the control is. Cheap, and it is what
             * separated "not inserted" from "inserted but never painted"
             * when the transcript came up blank on real Win98. */
            RECT r;
            char head[48];
            t->diag_done = 1;
            memset(head, 0, sizeof(head));
            SendMessageA(t->hwnd, WM_GETTEXT, sizeof(head) - 1, (LPARAM)head);
            GetWindowRect(t->hwnd, &r);
            rc_log("transcript: first content len=%ld vis=%d rect=%ld,%ld %ldx%ld"
                   " head=[%s]",
                   now, IsWindowVisible(t->hwnd) ? 1 : 0,
                   (long)r.left, (long)r.top,
                   (long)(r.right - r.left), (long)(r.bottom - r.top), head);
        }
    }

    if (t->was_at_bottom) {
        SendMessageA(t->hwnd, WM_VSCROLL, SB_BOTTOM, 0);
    }
    InvalidateRect(t->hwnd, NULL, TRUE);
    UpdateWindow(t->hwnd);
}

/* Apply a style to the current selection. With a zero-length selection this
 * sets the "typing" format that EM_REPLACESEL then inserts with: there is no
 * single call to append a formatted run in RichEdit 2.0, so mixed formatting
 * is always this alternating pair. */
static void apply_format(rc_transcript *t, const style_def *sd)
{
    CHARFORMAT2A cf;

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

    if (!SendMessageA(t->hwnd, EM_SETCHARFORMAT, SCF_SELECTION, (LPARAM)&cf)
        && !t->fmt_warned) {
        t->fmt_warned = 1;
        rc_log("transcript: EM_SETCHARFORMAT rejected, cbSize=%u",
               (unsigned)sizeof(CHARFORMAT2A));
    }
}

void rc_transcript_append(rc_transcript *t, const char *text, rc_style style)
{
    CHARRANGE cr;
    LONG len;
    const style_def *sd;

    if (!t->hwnd) { rc_log("transcript: append with no control"); return; }
    if (!text || !*text) return;
    t->appends++;
    if (style < 0 || style >= RC_STYLE__COUNT) style = RC_STYLE_PLAIN;
    sd = &STYLES[style];

    /* The first run after creation (or after a clear) goes in with WM_SETTEXT,
     * and that is load-bearing rather than a shortcut.
     *
     * On Win98's riched20, a RichEdit that has never held text does not lay
     * out or paint what EM_REPLACESEL puts into it. Every call reports
     * success, WM_GETTEXTLENGTH and WM_GETTEXT show the text really is there,
     * the window is visible with a sane formatting rectangle -- and the client
     * area stays blank forever. Priming with an EMPTY string is not enough:
     * the control needs actual characters. Once primed, every later
     * EM_REPLACESEL paints normally.
     *
     * Wine's riched20 paints without this, so `make wine-smoke` passed the
     * whole time and the transcript was blank the first time it ran on real
     * Win98. Do not "tidy this away". */
    /* Move an EMPTY selection to the end. Do NOT use (-1,-1): "end of text"
     * is not a documented meaning for it, only "deselect". */
    len = (LONG)SendMessageA(t->hwnd, WM_GETTEXTLENGTH, 0, 0);
    cr.cpMin = len;
    cr.cpMax = len;
    SendMessageA(t->hwnd, EM_EXSETSEL, 0, (LPARAM)&cr);
    apply_format(t, sd);
    /* wParam FALSE: do not add an undo record. */
    SendMessageA(t->hwnd, EM_REPLACESEL, FALSE, (LPARAM)text);

}

/* Give the control real text, once, with redraw enabled.
 *
 * On Win98's riched20 a RichEdit that has never held text does not lay out or
 * paint what EM_REPLACESEL puts into it. Every call reports success,
 * WM_GETTEXTLENGTH and WM_GETTEXT show the text really is there, the window is
 * visible with a sane formatting rectangle -- and the client area stays blank
 * forever. Two details, both found the hard way:
 *
 *   * an EMPTY WM_SETTEXT does not count; it needs actual characters, which
 *     is why the priming newline is left in place rather than deleted;
 *   * it must happen with redraw ENABLED, so priming from inside an append
 *     batch (which runs under WM_SETREDRAW FALSE) does nothing.
 *
 * Hence: prime at creation and after every clear, never mid-batch. Wine's
 * riched20 paints without any of this, so the Wine tier passed throughout and
 * the transcript was blank the first time it ran on real Win98.
 */
static void prime(rc_transcript *t)
{
    SendMessageA(t->hwnd, EM_SETREADONLY, FALSE, 0);
    SendMessageA(t->hwnd, WM_SETREDRAW, TRUE, 0);
    SetWindowTextA(t->hwnd, "\r\n");
    /* Setting the text is not enough; the control has to actually PAINT once
     * before it will render anything appended later. Without this, a page
     * load (clear -> prime -> begin -> append -> end) primes the control and
     * then disables redraw before any paint happens, and the page comes up
     * blank exactly as an unprimed control does. */
    InvalidateRect(t->hwnd, NULL, TRUE);
    UpdateWindow(t->hwnd);
    SendMessageA(t->hwnd, EM_SETREADONLY, TRUE, 0);
    t->primed = 1;
}

void rc_transcript_clear(rc_transcript *t)
{
    prime(t);
}
