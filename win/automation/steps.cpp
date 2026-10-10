#include "automation/steps.h"
#include "automation/select.h"
#include "core/rc_cpp.h"
#include "core/str.h"

#include <string.h>

const char *const STEP_OK = "ok";

void stepInit(Step &s)
{
    memset(&s, 0, sizeof(s));
    s.kind = STEP_WAIT;
    s.target.sel = s.target.item = s.target.text = "";
    s.in = s.key = s.text = "";
    s.button = BTN_LEFT;
    s.count = 1;
}

bool stepHasTarget(const Step &s)
{
    return s.target.sel[0] || s.target.hasScreen;
}

StepLimits::StepLimits() : scriptMs(120000), stepMs(30000), settleMs(0)
{
}

StepRunner::StepRunner(TreeWalker &walker, MsaaSource *msaa, UiWatch &watch,
                       Input &input, CallDeadline *deadline)
    : m_walker(walker), m_msaa(msaa), m_watch(watch), m_input(input),
      m_deadline(deadline), m_cancel(0), m_expectTop(0),
      m_releasedAtEnd(false)
{
}

static void clearReport(StepReport &r)
{
    r.status = STEP_OK;
    r.detail[0] = '\0';
    r.hit[0] = '\0';
    r.ms = 0;
    r.selStep = -1;
    r.candN = 0;
}

static bool fail(StepReport &r, const char *status, const char *detail)
{
    r.status = status;
    if (detail)
        rc_strf(r.detail, sizeof(r.detail), "%s", detail);
    return false;
}

static const char *inputStatus(InputResult x)
{
    switch (x)
    {
    case INPUT_OK:             return STEP_OK;
    case INPUT_REFUSED_SELF:   return "refused_self";
    case INPUT_NO_WINDOW:      return "no_window";
    case INPUT_NOT_FOREGROUND: return "not_foreground";
    case INPUT_OCCLUDED:       return "occluded";
    case INPUT_BAD_KEY:        return "bad_key";
    case INPUT_BAD_CHAR:       return "bad_char";
    case INPUT_FOCUS_LOST:     return "focus_lost";
    default:                   return "failed";
    }
}

/* "Class "caption"" of a window's top-level, for a report a person reads. */
static void describeTop(HWND h, char *out, unsigned cap)
{
    char cls[48], title[48];
    HWND top = Input::rootOf(h);
    cls[0] = title[0] = '\0';
    out[0] = '\0';
    if (!top || !IsWindow(top))
        return;
    GetClassNameA(top, cls, sizeof(cls));
    GetWindowTextA(top, title, sizeof(title));
    if (title[0])
        rc_strf(out, cap, "%s \"%s\"", cls, title);
    else
        rc_strf(out, cap, "%s", cls);
}

/* ---- resolving ----------------------------------------------------- */

bool StepRunner::resolveOne(const char *text, DWORD budget, HWND &out,
                            StepReport &r)
{
    Selector sel;
    char err[96];
    out = 0;
    if (!text || !text[0])
        return fail(r, "no_target", "no control was named");
    if (!sel.parse(text, err, sizeof(err)))
        return fail(r, "bad_selector", err);

    WalkOptions o = m_base;
    if (!o.deadlineMs || o.deadlineMs > budget)
        o.deadlineMs = budget;
    Resolver res(sel, (m_msaa && m_msaa->available()) ? m_msaa : 0);
    if (m_deadline)
        m_deadline->arm(o.deadlineMs);
    ResolveOutcome got = res.resolve(o, m_walker);
    if (m_deadline)
        m_deadline->disarm();

    if (got == RESOLVE_OK && res.match())
    {
        out = res.match()->hwnd;
        return true;
    }
    if (got == RESOLVE_AMBIGUOUS)
    {
        rc_strf(r.detail, sizeof(r.detail), "%d controls match",
                res.matchCount());
        for (int i = 0; i < res.sampleCount() && i < RC_SAMPLE_MAX; i++)
            r.cand[r.candN++] = res.sample(i)->hwnd;
        r.status = "ambiguous";
        return false;
    }
    if (got == RESOLVE_TRUNCATED)
        return fail(r, "truncated", 0);
    if (got == RESOLVE_NO_MEMORY)
        return fail(r, "no_memory", 0);
    if (sel.stepCount() > 1)
        r.selStep = res.unmatchedStep();
    return fail(r, "not_found", 0);
}

/* An MSAA child by name: a list row, a toolbar button, a desktop icon --
 * the things that have no HWND of their own. */
class ItemSink : public AccSink
{
public:
    ItemSink(const char *pattern, int nth)
        : m_pattern(pattern), m_nth(nth), m_count(0), m_found(false)
    {
        memset(&m_rect, 0, sizeof(m_rect));
        m_names[0] = '\0';
    }

    virtual bool onAcc(const AccNode &n)
    {
        char plain[RC_ENRICH_VAL];
        if (n.depth == 0 || !n.name[0])
            return true;
        selStripMnemonic(n.name, plain, sizeof(plain));
        if (!selGlobMatch(plain, m_pattern))
            return true;
        m_count++;
        if (m_count <= 3)
        {
            unsigned len = (unsigned)strlen(m_names);
            rc_strf(m_names + len, sizeof(m_names) - len, "%s\"%s\"",
                    len ? ", " : "", plain);
        }
        if ((m_nth == 0 && m_count == 1) || m_count == m_nth)
        {
            m_found = n.hasRect;
            m_rect = n.rect;
        }
        /* Unique-or-error needs the whole count; an nth needs only to get
         * there. */
        return m_nth == 0 || m_count < m_nth;
    }

    const char *m_pattern;
    int  m_nth;
    int  m_count;
    bool m_found;
    RECT m_rect;
    char m_names[120];
};

bool StepRunner::itemPoint(HWND h, const StepTarget &t, DWORD budget,
                           int &x, int &y, StepReport &r)
{
    if (!m_msaa || !m_msaa->available())
        return fail(r, "no_msaa", "this machine has no accessibility "
                    "support (oleacc), so items inside a control cannot be "
                    "named; use an offset `at` instead");
    AccWalkOptions o;
    o.want = Selector::WANT_NAME;
    o.maxNodes = 600;
    o.maxDepth = 4;
    o.deadlineMs = budget;
    ItemSink sink(t.item, t.itemNth);
    if (m_deadline)
        m_deadline->arm(budget);
    AccEnd end = m_msaa->enumerateFrom(h, o, sink);
    if (m_deadline)
        m_deadline->disarm();

    if (end == ACC_NO_OBJECT)
        return fail(r, "no_msaa", "that control answers no accessibility "
                    "questions, so its items cannot be named");
    if (sink.m_count == 0 || (t.itemNth && sink.m_count < t.itemNth))
    {
        rc_strf(r.detail, sizeof(r.detail), "no item matches \"%s\"%s",
                t.item, end == ACC_DEADLINE ? " (ran out of time "
                "reading the items)" : "");
        r.status = "no_item";
        return false;
    }
    if (!t.itemNth && sink.m_count > 1)
    {
        rc_strf(r.detail, sizeof(r.detail), "%d items match: %s%s",
                sink.m_count, sink.m_names, sink.m_count > 3 ? ", ..." : "");
        r.status = "ambiguous_item";
        return false;
    }
    if (!sink.m_found)
        return fail(r, "not_visible", "that item has no position on screen");
    x = (sink.m_rect.left + sink.m_rect.right) / 2;
    y = (sink.m_rect.top + sink.m_rect.bottom) / 2;
    return true;
}

/* Find the `nth` (1-based; 0 = unique) occurrence of a glob in `hay`,
 * case-insensitively, as [start, end). A `*` splits the pattern into pieces
 * that must appear in order; `?` is any one character. Returns the number
 * of occurrences found, counting only as far as needed. */
static bool pieceAt(const char *hay, const char *piece, unsigned n)
{
    for (unsigned i = 0; i < n; i++)
    {
        if (!hay[i])
            return false;
        if (piece[i] == '?')
            continue;
        if (CharLowerA((LPSTR)(UINT_PTR)(unsigned char)hay[i])
            != CharLowerA((LPSTR)(UINT_PTR)(unsigned char)piece[i]))
            return false;
    }
    return true;
}

static bool matchFrom(const char *hay, const char *pat, unsigned &endOff)
{
    /* The first piece is anchored at `hay`; each later one may float. */
    const char *h = hay;
    bool anchored = true;
    while (*pat)
    {
        if (*pat == '*')
        {
            anchored = false;
            pat++;
            continue;
        }
        const char *star = strchr(pat, '*');
        unsigned n = star ? (unsigned)(star - pat) : (unsigned)strlen(pat);
        if (anchored)
        {
            if (!pieceAt(h, pat, n))
                return false;
        }
        else
        {
            while (*h && !pieceAt(h, pat, n))
                h++;
            if (!*h)
                return false;
        }
        h += n;
        pat += n;
        anchored = true;
    }
    endOff = (unsigned)(h - hay);
    return true;
}

static int findText(const char *hay, const char *pat, int nth,
                    unsigned &start, unsigned &end)
{
    int count = 0;
    unsigned len = (unsigned)strlen(hay);
    while (*pat == '*')
        pat++;                      /* a leading star changes nothing */
    if (!*pat)
        return 0;
    for (unsigned i = 0; i < len; i++)
    {
        unsigned e;
        if (!matchFrom(hay + i, pat, e) || e == 0)
            continue;
        count++;
        if ((nth == 0 && count == 1) || count == nth)
        {
            start = i;
            end = i + e;
        }
        if (nth && count >= nth)
            return count;
        if (!nth && count > 1)
            return count;
        i += e - 1;
    }
    return count;
}

static bool isPlainEdit(HWND h)
{
    char cls[32];
    if (!GetClassNameA(h, cls, sizeof(cls)))
        return false;
    return lstrcmpiA(cls, "Edit") == 0;
}

/* Character `index`'s top-left in CLIENT coordinates, by EM_POSFROMCHAR --
 * which passes no pointer for a plain Edit and so works across a process
 * boundary. -1 in both when the index is past the end. */
static bool posFromChar(HWND h, unsigned index, DWORD budget, int &x, int &y)
{
    DWORD_PTR res = 0;
    if (!SendMessageTimeoutA(h, EM_POSFROMCHAR, index, 0, SMTO_ABORTIFHUNG,
                             budget, &res))
        return false;
    if ((LONG)res == -1)
        return false;
    x = (short)LOWORD(res);
    y = (short)HIWORD(res);
    return true;
}

bool StepRunner::textPoint(HWND h, const StepTarget &t, DWORD budget,
                           int &x, int &y, StepReport &r)
{
    DWORD_PTR len = 0;
    if (!isPlainEdit(h))
        return fail(r, "not_edit", "`text` finds text inside a plain Edit "
                    "control; for anything else name an `item` or an `at` "
                    "offset");
    if (!SendMessageTimeoutA(h, WM_GETTEXTLENGTH, 0, 0, SMTO_ABORTIFHUNG,
                             budget, &len))
        return fail(r, "failed", "the control did not answer");
    /* Notepad's own limit on 98. A bigger control is read up to here and
     * the text past it cannot be clicked, which is said rather than hidden. */
    if (len > 65535)
        len = 65535;
    HeapPtr<char> buf((char *)HeapAlloc(GetProcessHeap(), 0, len + 1));
    if (!buf.valid())
        return fail(r, "no_memory", 0);
    DWORD_PTR got = 0;
    buf.get()[0] = '\0';
    if (!SendMessageTimeoutA(h, WM_GETTEXT, len + 1, (LPARAM)buf.get(),
                             SMTO_ABORTIFHUNG, budget, &got))
        return fail(r, "failed", "the control did not answer");
    buf.get()[got <= len ? got : len] = '\0';

    unsigned start = 0, end = 0;
    int n = findText(buf.get(), t.text, t.textNth, start, end);
    if (n == 0 || (t.textNth && n < t.textNth))
    {
        rc_strf(r.detail, sizeof(r.detail), "the control does not contain "
                "\"%s\"", t.text);
        r.status = "no_text_match";
        return false;
    }
    if (!t.textNth && n > 1)
    {
        rc_strf(r.detail, sizeof(r.detail), "\"%s\" occurs more than once; "
                "say which with text_nth", t.text);
        r.status = "ambiguous_text";
        return false;
    }

    /* The line's height, from the control's own font, so the click lands in
     * the middle of the line rather than on its top pixel. GDI objects are
     * system-wide on 9x, so another process's font can be measured here. */
    int lineH = 16;
    {
        HFONT font = (HFONT)SendMessageA(h, WM_GETFONT, 0, 0);
        HDC dc = GetDC(NULL);
        if (dc)
        {
            HGDIOBJ old = font ? SelectObject(dc, font) : 0;
            TEXTMETRICA tm;
            if (GetTextMetricsA(dc, &tm))
                lineH = tm.tmHeight;
            if (old)
                SelectObject(dc, old);
            ReleaseDC(NULL, dc);
        }
    }

    int cx = 0, cy = 0;
    unsigned at = t.edgeEnd ? end : start;
    if (!posFromChar(h, at, budget, cx, cy))
    {
        /* Past the last character: EM_POSFROMCHAR has nothing there, so
         * measure from the last one and step over its width. */
        if (!at || !posFromChar(h, at - 1, budget, cx, cy))
            return fail(r, "not_visible", "that text has no position");
        cx += 6;
    }
    RECT client;
    GetClientRect(h, &client);
    if (cx < client.left || cx >= client.right || cy < client.top
        || cy + lineH / 2 >= client.bottom)
        return fail(r, "not_visible", "that text is scrolled out of view; "
                    "nothing scrolls for you -- move the caret there first");
    POINT p;
    p.x = cx + 1;
    p.y = cy + lineH / 2;
    ClientToScreen(h, &p);
    x = p.x;
    y = p.y;
    return true;
}

bool StepRunner::pointFor(const StepTarget &t, DWORD budget, HWND &expect,
                          int &x, int &y, StepReport &r)
{
    expect = 0;
    if (t.hasScreen)
    {
        x = t.screenX;
        y = t.screenY;
        if (t.sel[0] && !resolveOne(t.sel, budget, expect, r))
            return false;
        return true;
    }
    HWND h;
    if (!resolveOne(t.sel, budget, h, r))
        return false;
    expect = h;
    if (t.item[0])
        return itemPoint(h, t, budget, x, y, r);
    if (t.text[0])
        return textPoint(h, t, budget, x, y, r);
    RECT wr;
    if (!GetWindowRect(h, &wr))
        return fail(r, "no_window", 0);
    if (t.hasAt)
    {
        x = wr.left + t.atX;
        y = wr.top + t.atY;
    }
    else
    {
        x = (wr.left + wr.right) / 2;
        y = (wr.top + wr.bottom) / 2;
    }
    return true;
}

/* ---- the keyboard's destination ------------------------------------ */

bool StepRunner::keyboardTargetOk(const Step &s, DWORD budget, StepReport &r)
{
    HWND fg = GetForegroundWindow();
    HWND want = 0;
    if (s.in[0])
    {
        if (!resolveOne(s.in, budget, want, r))
            return false;
        m_expectTop = Input::rootOf(want);
    }
    if (Input::isOurs(fg))
        return fail(r, "refused_self", "the window in front is this "
                    "program's own; focus the one to type into first");
    if (!m_expectTop)
        return true;
    for (HWND h = Input::rootOf(fg); h; h = GetWindow(h, GW_OWNER))
        if (h == m_expectTop)
        {
            /* AND THE FOCUS INSIDE IT, which until 2026-10-11 nothing did.
             * `in` resolved a control and then only checked that the
             * control's WINDOW was in front; the keystrokes went to
             * whichever child already had the keyboard focus. With the
             * client in front and its focus on the transcript view, a step
             * list typed a whole prompt into nothing and reported `ok`
             * (BACKLOG 33) -- which is the one outcome `typeText`'s
             * two-pass validation exists to prevent, arriving by a
             * different door. Reported as a failure when it does not take,
             * because the alternative is the silence that hid it. */
            if (want && !m_input.focusChild(want))
                return fail(r, "not_foreground", "the control would not "
                            "take the keyboard focus, so nothing was typed");
            return true;
        }
    describeTop(fg, r.hit, sizeof(r.hit));
    r.status = "focus_lost";
    rc_strf(r.detail, sizeof(r.detail), "the window in front changed to %s",
            r.hit[0] ? r.hit : "something else");
    return false;
}

/* ---- waits ---------------------------------------------------------- */

bool StepRunner::doWait(const Step &s, DWORD budget, StepReport &r)
{
    UiWaiter waiter(m_watch, m_walker,
                    (m_msaa && m_msaa->available()) ? m_msaa : 0);
    WaitReport rep;
    waiter.setCancel(m_cancel);

    if (s.kind == STEP_IDLE)
    {
        waiter.forIdle(s.ms ? s.ms : 300, budget, rep);
        r.ms = rep.ms;
        if (rep.end == UIWAIT_CANCELLED)
            return fail(r, "cancelled", 0);
        /* No event feed means nothing to be quiet about; not an error. */
        if (rep.polled)
            rc_strf(r.detail, sizeof(r.detail), "no event feed; did not wait");
        else if (rep.end == UIWAIT_TIMEOUT)
            return fail(r, "timeout", "the screen never went quiet");
        return true;
    }

    Selector sel;
    char err[96];
    if (!sel.parse(s.target.sel, err, sizeof(err)))
        return fail(r, "bad_selector", err);
    WalkOptions o = m_base;
    if (!o.deadlineMs || o.deadlineMs > budget)
        o.deadlineMs = budget;
    if (m_deadline)
        m_deadline->arm(budget + o.deadlineMs);
    if (s.kind == STEP_WAITFOR)
        waiter.forControl(sel, o, budget, rep);
    else if (s.kind == STEP_WAITGONE)
        waiter.forGone(sel, o, budget, rep);
    else
        waiter.forText(sel, s.text, o, budget, rep);
    if (m_deadline)
        m_deadline->disarm();
    r.ms = rep.ms;

    switch (rep.end)
    {
    case UIWAIT_ALREADY:
    case UIWAIT_SETTLED:
        /* A window that was waited for is the window typing is meant for
         * next -- the Save As dialog after Ctrl+S -- BUT ONLY IF IT IS IN
         * FRONT. A menu or a tooltip is a window that never takes the
         * foreground: its owner keeps it, keystrokes reach the menu through
         * the owner, and making the menu the expected window refused the
         * Escape meant to close it (measured on the VM, 2026-10-02). */
        if (s.kind != STEP_WAITGONE && rep.haveNode && rep.node.hwnd)
        {
            HWND top = Input::rootOf(rep.node.hwnd);
            if (top && top == Input::rootOf(GetForegroundWindow()))
                m_expectTop = top;
            describeTop(rep.node.hwnd, r.hit, sizeof(r.hit));
        }
        return true;
    case UIWAIT_CANCELLED:
        return fail(r, "cancelled", 0);
    case UIWAIT_AMBIGUOUS:
        rc_strf(r.detail, sizeof(r.detail), "%d controls match",
                rep.matchCount);
        for (int i = 0; i < rep.sampleN; i++)
            r.cand[r.candN++] = rep.samples[i];
        r.status = "ambiguous";
        return false;
    case UIWAIT_TRUNCATED:
        return fail(r, "truncated", 0);
    default:
        if (s.kind == STEP_WAITGONE)
            rc_strf(r.detail, sizeof(r.detail), "still there after %lums",
                    (unsigned long)rep.ms);
        else if (s.kind == STEP_WAITTEXT && rep.everMatched)
            rc_strf(r.detail, sizeof(r.detail), "the control is there but "
                    "never said \"%s\" in %lums", s.text,
                    (unsigned long)rep.ms);
        else
            rc_strf(r.detail, sizeof(r.detail), "waited %lums; %s",
                    (unsigned long)rep.ms,
                    rep.everMatched ? "it appeared and then went"
                                    : "it never appeared");
        r.status = "timeout";
        return false;
    }
}

/* A window OWNED by `top` and visible -- the "save changes?" a close
 * raises. */
struct OwnedFind
{
    HWND owner;
    HWND found;
};

static BOOL CALLBACK findOwned(HWND h, LPARAM lp)
{
    OwnedFind *f = (OwnedFind *)lp;
    if (IsWindowVisible(h) && GetWindow(h, GW_OWNER) == f->owner)
    {
        f->found = h;
        return FALSE;
    }
    return TRUE;
}

bool StepRunner::doClose(const Step &s, DWORD budget, StepReport &r)
{
    HWND h;
    if (!resolveOne(s.target.sel, budget, h, r))
        return false;
    HWND top = Input::rootOf(h);
    describeTop(top, r.hit, sizeof(r.hit));
    InputResult x = m_input.closeWindow(top);
    if (x != INPUT_OK)
        return fail(r, inputStatus(x), 0);

    DWORD t0 = GetTickCount();
    while ((long)(GetTickCount() - t0) < (long)budget)
    {
        if (!IsWindow(top) || !IsWindowVisible(top))
        {
            r.ms = GetTickCount() - t0;
            return true;
        }
        OwnedFind f;
        f.owner = top;
        f.found = 0;
        EnumWindows(findOwned, (LPARAM)&f);
        if (f.found)
        {
            char what[96];
            describeTop(f.found, what, sizeof(what));
            /* The window it raised becomes where typing goes: answering the
             * prompt is the obvious next list. */
            m_expectTop = f.found;
            rc_strf(r.detail, sizeof(r.detail), "it asked first: %s", what);
            r.status = "prompted";
            r.ms = GetTickCount() - t0;
            return false;
        }
        if (cancelled())
            return fail(r, "cancelled", 0);
        m_watch.settle(50);
    }
    r.ms = GetTickCount() - t0;
    return fail(r, "still_open", "the window did not close and asked "
                "nothing");
}

void StepRunner::settle(DWORD left)
{
    if (!m_limits.settleMs || !left)
        return;
    UiWaiter waiter(m_watch, m_walker, 0);
    WaitReport rep;
    DWORD budget = m_limits.settleMs * 4;
    if (budget > left)
        budget = left;
    waiter.setCancel(m_cancel);
    waiter.forIdle(m_limits.settleMs, budget, rep);
}

/* ---- one step ------------------------------------------------------- */

/* Which part of a window's frame a point is on, for the parts where a
 * click does something other than reach the program's content. */
static const char *frameArea(LRESULT ht)
{
    switch (ht)
    {
    case HTMENU:     return "menu bar";
    case HTCAPTION:  return "title bar";
    case HTSYSMENU:  return "system menu";
    case HTCLOSE:    return "close box";
    case HTMINBUTTON:return "minimise box";
    case HTMAXBUTTON:return "maximise box";
    case HTVSCROLL:  return "vertical scroll bar";
    case HTHSCROLL:  return "horizontal scroll bar";
    case HTLEFT: case HTRIGHT: case HTTOP: case HTBOTTOM:
    case HTTOPLEFT: case HTTOPRIGHT: case HTBOTTOMLEFT: case HTBOTTOMRIGHT:
                     return "border";
    default:         return 0;
    }
}

/* WHAT A POINTER STEP LANDED ON: the top-level window, the control under
 * the point when that is not the window itself, and the part of the frame
 * when the point is on one. The top-level alone said "Notepad" for a drag
 * that started on Notepad's MENU BAR -- a live session's guessed pixels
 * (2026-10-02) -- and the agent, told every step was ok, reported text it
 * had never typed. Saying "menu bar" is what lets it notice. */
static void hitAt(int x, int y, char *out, unsigned cap)
{
    POINT p;
    p.x = x;
    p.y = y;
    HWND w = WindowFromPoint(p);
    describeTop(w, out, cap);
    if (!w)
        return;
    unsigned len = (unsigned)strlen(out);
    HWND top = Input::rootOf(w);
    if (w != top)
    {
        char cls[40];
        cls[0] = '\0';
        GetClassNameA(w, cls, sizeof(cls));
        rc_strf(out + len, cap - len, " > %s", cls);
        len = (unsigned)strlen(out);
    }
    DWORD_PTR ht = HTNOWHERE;
    if (SendMessageTimeoutA(w, WM_NCHITTEST, 0, MAKELPARAM(x, y),
                            SMTO_ABORTIFHUNG, 200, &ht))
    {
        const char *area = frameArea((LRESULT)ht);
        if (area)
            rc_strf(out + len, cap - len, " (%s)", area);
    }
}

static HKL foregroundLayout(void)
{
    HWND fg = GetForegroundWindow();
    return GetKeyboardLayout(fg ? GetWindowThreadProcessId(fg, NULL) : 0);
}

bool StepRunner::runOne(const Step &s, DWORD budget, StepReport &r)
{
    InputResult x;
    switch (s.kind)
    {
    case STEP_FOCUS:
    {
        HWND h;
        if (!resolveOne(s.target.sel, budget, h, r))
            return false;
        x = m_input.focus(h);
        if (x != INPUT_OK)
            return fail(r, inputStatus(x), 0);
        m_expectTop = Input::rootOf(h);
        describeTop(h, r.hit, sizeof(r.hit));
        return true;
    }
    case STEP_CLICK:
    case STEP_MOVE:
    case STEP_BTNDOWN:
    case STEP_BTNUP:
    {
        HWND expect = 0;
        int px = 0, py = 0;
        bool positioned = stepHasTarget(s);
        if (positioned && !pointFor(s.target, budget, expect, px, py, r))
            return false;
        if (s.kind == STEP_CLICK)
        {
            x = m_input.clickButtonAt(expect, px, py, s.button,
                                      s.count < 1 ? 1 : s.count);
            if (x == INPUT_OCCLUDED)
                rc_strf(r.detail, sizeof(r.detail), "covered by %s",
                        m_input.blocker());
            if (x != INPUT_OK)
                return fail(r, inputStatus(x), 0);
            hitAt(px, py, r.hit, sizeof(r.hit));
            if (expect)
                m_expectTop = Input::rootOf(expect);
            else
            {
                POINT p;
                p.x = px;
                p.y = py;
                m_expectTop = Input::rootOf(WindowFromPoint(p));
            }
            return true;
        }
        if (positioned)
        {
            if (expect && s.kind == STEP_BTNDOWN)
            {
                x = m_input.clickAt(expect, px, py, true);
                if (x != INPUT_OK)
                    return fail(r, inputStatus(x), 0);
            }
            m_input.moveTo(px, py);
            hitAt(px, py, r.hit, sizeof(r.hit));
        }
        if (s.kind == STEP_BTNDOWN)
            x = m_input.buttonDown(s.button);
        else if (s.kind == STEP_BTNUP)
            x = m_input.buttonUp(s.button);
        else
            x = INPUT_OK;
        return x == INPUT_OK ? true : fail(r, inputStatus(x), 0);
    }
    case STEP_MOVEBY:
    {
        POINT p;
        GetCursorPos(&p);
        m_input.moveTo(p.x + s.dx, p.y + s.dy);
        hitAt(p.x + s.dx, p.y + s.dy, r.hit, sizeof(r.hit));
        return true;
    }
    case STEP_CLOSE:
        return doClose(s, budget, r);
    case STEP_KEYDOWN:
    case STEP_KEYUP:
    {
        KeySpec k;
        if (!Input::keyByName(s.key, k))
        {
            rc_strf(r.detail, sizeof(r.detail), "%s", s.key);
            r.status = "bad_key";
            return false;
        }
        /* Releasing is always allowed: refusing an `up` because focus moved
         * would leave the key down, which is the worse outcome. */
        if (s.kind == STEP_KEYDOWN && !keyboardTargetOk(s, budget, r))
            return false;
        x = s.kind == STEP_KEYDOWN ? m_input.keyDown(k) : m_input.keyUp(k);
        return x == INPUT_OK ? true : fail(r, inputStatus(x), 0);
    }
    case STEP_TAP:
    {
        if (!keyboardTargetOk(s, budget, r))
            return false;
        KeySpec k;
        if (Input::keyByName(s.key, k))
            x = m_input.tapKey(k, s.mods);
        else if (s.key[0] && !s.key[1])
            x = m_input.tapChar((unsigned char)s.key[0], s.mods,
                                foregroundLayout());
        else
        {
            rc_strf(r.detail, sizeof(r.detail), "%s", s.key);
            r.status = "bad_key";
            return false;
        }
        if (x == INPUT_BAD_KEY)
            rc_strf(r.detail, sizeof(r.detail), "%s", s.key);
        return x == INPUT_OK ? true : fail(r, inputStatus(x), 0);
    }
    case STEP_TYPE:
    {
        unsigned sent = 0, badAt = 0;
        if (!keyboardTargetOk(s, budget, r))
            return false;
        HKL hkl = foregroundLayout();
        x = m_input.typeChars(s.text, s.textLen, hkl,
                              m_expectTop, sent, badAt);
        if (x == INPUT_BAD_CHAR)
            rc_strf(r.detail, sizeof(r.detail), "character 0x%02X at %u "
                    "cannot be typed on layout %08lX",
                    (unsigned)(unsigned char)s.text[badAt], badAt,
                    (unsigned long)(DWORD)(UINT_PTR)hkl);
        else if (x == INPUT_FOCUS_LOST)
        {
            describeTop(GetForegroundWindow(), r.hit, sizeof(r.hit));
            rc_strf(r.detail, sizeof(r.detail), "%u of the characters went "
                    "in before the window in front changed to %s", sent,
                    r.hit[0] ? r.hit : "something else");
        }
        else
            rc_strf(r.detail, sizeof(r.detail), "%u keystrokes", sent);
        return x == INPUT_OK ? true : fail(r, inputStatus(x), 0);
    }
    case STEP_WAIT:
    {
        DWORD t0 = GetTickCount();
        DWORD want = s.ms < budget ? s.ms : budget;
        while ((long)(GetTickCount() - t0) < (long)want)
        {
            if (cancelled())
                return fail(r, "cancelled", 0);
            DWORD left = want - (GetTickCount() - t0);
            m_watch.settle(left < 100 ? left : 100);
        }
        r.ms = GetTickCount() - t0;
        if (s.ms > budget)
            return fail(r, "timeout", "the pause is longer than the time "
                        "left");
        return true;
    }
    case STEP_WAITFOR:
    case STEP_WAITGONE:
    case STEP_WAITTEXT:
    case STEP_IDLE:
        return doWait(s, budget, r);
    }
    return fail(r, "failed", "unknown step");
}

static bool isAction(StepKind k)
{
    return k == STEP_FOCUS || k == STEP_CLICK || k == STEP_TAP
        || k == STEP_TYPE || k == STEP_BTNUP || k == STEP_CLOSE;
}

int StepRunner::run(const Step *steps, int count, const WalkOptions &base,
                    const StepLimits &limits, StepSink &sink,
                    StepReport &failed)
{
    m_base = base;
    m_limits = limits;
    m_releasedAtEnd = false;
    clearReport(failed);

    /* Until a step says otherwise, input is meant for whatever is in front
     * now -- unless that is us, in which case for nothing until a focus or
     * click names a window. */
    HWND fg = GetForegroundWindow();
    m_expectTop = (fg && !Input::isOurs(fg)) ? Input::rootOf(fg) : 0;

    DWORD t0 = GetTickCount();
    int failedAt = -1;
    for (int i = 0; i < count; i++)
    {
        const Step &s = steps[i];
        StepReport r;
        clearReport(r);

        long left = (long)limits.scriptMs - (long)(GetTickCount() - t0);
        if (cancelled())
            fail(r, "cancelled", 0);
        else if (left <= 0)
            fail(r, "script_timeout", 0);
        else
        {
            DWORD budget = s.timeoutMs ? s.timeoutMs : limits.stepMs;
            if ((long)budget > left)
                budget = (DWORD)left;
            DWORD s0 = GetTickCount();
            if (runOne(s, budget, r) && isAction(s.kind))
            {
                long after = (long)limits.scriptMs
                           - (long)(GetTickCount() - t0);
                settle(after > 0 ? (DWORD)after : 0);
            }
            if (!r.ms)
                r.ms = GetTickCount() - s0;
        }

        sink.onStep(i, s, r);
        if (r.status != STEP_OK)
        {
            failed = r;
            failedAt = i;
            break;
        }
    }

    if (m_input.anyHeld())
    {
        m_releasedAtEnd = true;
        m_input.releaseAll();
    }
    return failedAt;
}
