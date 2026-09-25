/* pillbar.cpp -- the attachment pill bar sitting above the input box.
 *
 * Displays active staged attachments (workspace files, external files,
 * or screenshots) as small individual pills matching the toolbar split-button style:
 * | file1.txt | X |   | file2.png | X |
 */
#include "app/pillbar.h"
#include "core/str.h"
#include "core/log.h"
#include <string.h>

#include <list>
#include <string>
#include <vector>

static const char BAR_CLASS[] = "GastonAttachmentBar";

/* Cap on attachments sent and not yet fetched by the bridge. */
static const unsigned RC_MAX_TRANSFERS = 4 * RC_MAX_PILLS;

static void check_child(const char *what, HWND h)
{
    if (!h)
        rc_log("pillbar: %s failed to create (GetLastError=%lu)",
               what, (unsigned long)GetLastError());
}

static void format_size(DWORD bytes, char *out, size_t cap)
{
    if (bytes >= 1024 * 1024) {
        unsigned long mb_whole = bytes / (1024 * 1024);
        unsigned long mb_tenth = (bytes % (1024 * 1024)) * 10 / (1024 * 1024);
        rc_strf(out, cap, "%lu.%lu MB", mb_whole, mb_tenth);
    } else if (bytes >= 1024) {
        rc_strf(out, cap, "%lu KB", (unsigned long)(bytes / 1024));
    } else if (bytes > 0) {
        rc_strf(out, cap, "%lu B", (unsigned long)bytes);
    } else {
        rc_strf(out, cap, "0 B");
    }
}

AttachmentBar::AttachmentBar(HWND parent, HINSTANCE inst, HFONT font,
                             AttachmentBarListener *listener)
    : m_parent(parent), m_font(font), m_listener(listener),
      m_uiActive(0), m_pressedIdx(-1), m_pressedState(0)
{
    InitializeCriticalSection(&m_lock);

    WNDCLASSA wc;
    memset(&wc, 0, sizeof(wc));
    wc.style = CS_HREDRAW | CS_VREDRAW;
    wc.hCursor = LoadCursor(NULL, IDC_ARROW);
    wc.hbrBackground = (HBRUSH)(COLOR_BTNFACE + 1);
    if (Window::registerClass(inst, BAR_CLASS, &wc))
        Window::create(0, BAR_CLASS, "", WS_CHILD | WS_CLIPSIBLINGS, 0, 0, 0, 0,
                       parent, NULL, inst);
    check_child("pillbar window", hwnd());
}

AttachmentBar::~AttachmentBar()
{
    clear();
    /* In the derived destructor, so this class's handle() still sees the
     * window's last messages (core/window.h). */
    destroy();
    /* The sync worker that shares m_lock has been joined by now: the
     * connection is stopped before the window that owns this bar. */
    DeleteCriticalSection(&m_lock);
}

AttachmentBar *AttachmentBar::create(HWND parent, HINSTANCE inst, HFONT font,
                                     AttachmentBarListener *listener)
{
    return new AttachmentBar(parent, inst, font, listener);
}

LRESULT AttachmentBar::handle(UINT msg, WPARAM wp, LPARAM lp)
{
    switch (msg) {
    case WM_PAINT: {
        PAINTSTRUCT ps;
        HDC hdc = BeginPaint(hwnd(), &ps);
        paint(hdc);
        EndPaint(hwnd(), &ps);
        return 0;
    }
    case WM_ERASEBKGND: {
        HDC hdc = (HDC)wp;
        RECT rc;
        GetClientRect(hwnd(), &rc);
        FillRect(hdc, &rc, (HBRUSH)(COLOR_BTNFACE + 1));
        return 1;
    }
    case WM_LBUTTONDOWN:
        onLButtonDown((short)LOWORD(lp), (short)HIWORD(lp));
        return 0;
    case WM_MOUSEMOVE:
        onMouseMove((short)LOWORD(lp), (short)HIWORD(lp));
        return 0;
    case WM_LBUTTONUP:
        onLButtonUp((short)LOWORD(lp), (short)HIWORD(lp));
        return 0;
    case WM_SETCURSOR:
        SetCursor(LoadCursor(NULL, IDC_ARROW));
        return TRUE;
    }
    return Window::handle(msg, wp, lp);
}

/* "name (size)", the text a pill shows and is measured by. */
static std::string pillLabel(const PillItem &pill)
{
    char size[32];
    format_size(pill.size, size, sizeof(size));
    return pill.name + " (" + size + ")";
}

void AttachmentBar::paint(HDC hdc)
{
    HFONT old_font = m_font ? (HFONT)SelectObject(hdc, m_font) : NULL;
    SetBkMode(hdc, TRANSPARENT);

    for (unsigned i = 0; i < m_pills.size(); i++) {
        RECT rc_lbl = m_pills[i].rcLabel;
        RECT rc_cls = m_pills[i].rcClose;

        /* 1. Left Box: Label with 3D raised border */
        FillRect(hdc, &rc_lbl, (HBRUSH)(COLOR_BTNFACE + 1));
        DrawEdge(hdc, &rc_lbl, EDGE_RAISED, BF_RECT);

        std::string text = pillLabel(m_pills[i]);
        RECT rc_txt = rc_lbl;
        rc_txt.left += 6;
        rc_txt.right -= 6;
        SetTextColor(hdc, GetSysColor(COLOR_BTNTEXT));
        DrawTextA(hdc, text.c_str(), (int)text.size(), &rc_txt,
                  DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX | DT_END_ELLIPSIS);

        /* 2. Right Box: Close button with 3D raised border (sunken when pressed) */
        FillRect(hdc, &rc_cls, (HBRUSH)(COLOR_BTNFACE + 1));
        bool pressed = m_pressedIdx == (int)i && m_pressedState;
        DrawEdge(hdc, &rc_cls, pressed ? EDGE_SUNKEN : EDGE_RAISED, BF_RECT);
        RECT rc_x = rc_cls;
        if (pressed)
            OffsetRect(&rc_x, 1, 1);
        DrawTextA(hdc, "x", 1, &rc_x, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
    }

    if (old_font) SelectObject(hdc, old_font);
}

void AttachmentBar::onLButtonDown(int x, int y)
{
    POINT pt = { x, y };
    for (unsigned i = 0; i < m_pills.size(); i++) {
        if (PtInRect(&m_pills[i].rcClose, pt)) {
            m_pressedIdx = (int)i;
            m_pressedState = 1;
            SetCapture(hwnd());
            InvalidateRect(hwnd(), &m_pills[i].rcClose, TRUE);
            break;
        }
    }
}

void AttachmentBar::onMouseMove(int x, int y)
{
    if (m_pressedIdx >= 0 && m_pressedIdx < (int)m_pills.size()) {
        POINT pt = { x, y };
        int in_btn = PtInRect(&m_pills[m_pressedIdx].rcClose, pt);
        if (in_btn != m_pressedState) {
            m_pressedState = in_btn;
            InvalidateRect(hwnd(), &m_pills[m_pressedIdx].rcClose, TRUE);
        }
    }
}

void AttachmentBar::onLButtonUp(int x, int y)
{
    if (m_pressedIdx >= 0) {
        int idx = m_pressedIdx;
        int state = m_pressedState;
        m_pressedIdx = -1;
        m_pressedState = 0;
        ReleaseCapture();

        POINT pt = { x, y };
        if (state && idx < (int)m_pills.size() &&
            PtInRect(&m_pills[idx].rcClose, pt)) {
            removePill(idx);
        } else {
            InvalidateRect(hwnd(), NULL, TRUE);
        }
    }
}

void AttachmentBar::updateLayout(int width)
{
    if (m_pills.empty()) return;

    HDC hdc = GetDC(valid() ? hwnd() : m_parent);
    HFONT old_font = m_font ? (HFONT)SelectObject(hdc, m_font) : NULL;

    int cur_x = 2;
    int pill_y = 1;
    int pill_h = PILL_H - 2; /* 20px */
    int close_w = 16;
    int max_x = width - 4;

    for (unsigned i = 0; i < m_pills.size(); i++) {
        std::string text = pillLabel(m_pills[i]);
        SIZE sz = { 0, 0 };
        GetTextExtentPoint32A(hdc, text.c_str(), (int)text.size(), &sz);

        int lbl_w = sz.cx + 14; /* 7px left + 7px right padding */
        if (lbl_w < 36) lbl_w = 36;

        if (cur_x + lbl_w + close_w > max_x) {
            lbl_w = max_x - cur_x - close_w;
            if (lbl_w < 20) lbl_w = 20;
        }

        m_pills[i].rcLabel.left   = cur_x;
        m_pills[i].rcLabel.top    = pill_y;
        m_pills[i].rcLabel.right  = cur_x + lbl_w;
        m_pills[i].rcLabel.bottom = pill_y + pill_h;

        m_pills[i].rcClose.left   = cur_x + lbl_w;
        m_pills[i].rcClose.top    = pill_y;
        m_pills[i].rcClose.right  = cur_x + lbl_w + close_w;
        m_pills[i].rcClose.bottom = pill_y + pill_h;

        cur_x += lbl_w + close_w + 6; /* 6px gap to next pill */
    }

    if (old_font) SelectObject(hdc, old_font);
    ReleaseDC(valid() ? hwnd() : m_parent, hdc);
}

void AttachmentBar::move(int cx, int y, int cw)
{
    if (!valid()) return;
    if (active()) {
        updateLayout(cw);
        MoveWindow(hwnd(), cx, y, cw, PILL_H, TRUE);
        ShowWindow(hwnd(), SW_SHOW);
        InvalidateRect(hwnd(), NULL, TRUE);
    } else {
        ShowWindow(hwnd(), SW_HIDE);
    }
}

bool AttachmentBar::command(int id)
{
    /* Retained for backward compatibility */
    (void)id;
    return false;
}

/* Deletes a staged temp file (a screenshot) that is being dropped. */
static void dropTemp(const PillItem &pill)
{
    if (pill.isTemp && !pill.path.empty())
        DeleteFileA(pill.path.c_str());
}

/* Called with the pills changed: lays the row out again and repaints. */
void AttachmentBar::relayout()
{
    if (valid()) {
        RECT rc;
        GetClientRect(hwnd(), &rc);
        updateLayout(rc.right - rc.left);
        InvalidateRect(hwnd(), NULL, TRUE);
    }
}

int AttachmentBar::removePill(int idx)
{
    {
        ScopedLock lock(&m_lock);
        if (idx < 0 || idx >= (int)m_pills.size()) return 0;
        dropTemp(m_pills[idx]);
        m_pills.erase(m_pills.begin() + idx);
        if (m_pills.empty())
            m_uiActive = 0;
    }
    relayout();
    if (m_listener) m_listener->onAttachmentsChanged();
    return 1;
}

int AttachmentBar::stageWorkspace(const char *relPath, DWORD sizeBytes)
{
    {
        ScopedLock lock(&m_lock);
        std::string path = relPath ? relPath : "";
        for (unsigned i = 0; i < m_pills.size(); i++) {
            if (!m_pills[i].isExternal && m_pills[i].path == path)
                return 1;
        }
        if (m_pills.size() >= RC_MAX_PILLS) return 0;

        PillItem pill;
        pill.name = path;
        pill.path = path;
        pill.size = sizeBytes;
        pill.isExternal = 0;
        pill.isTemp = 0;
        m_pills.push_back(pill);
        m_uiActive = 1;
    }
    relayout();
    return 1;
}

int AttachmentBar::stageExternal(const char *id, const char *absPath,
                                 const char *displayName, DWORD sizeBytes,
                                 int isTemp)
{
    {
        ScopedLock lock(&m_lock);
        std::string path = absPath ? absPath : "";
        if (!isTemp) {
            for (unsigned i = 0; i < m_pills.size(); i++) {
                if (m_pills[i].isExternal && m_pills[i].path == path)
                    return 1;
            }
        }
        if (m_pills.size() >= RC_MAX_PILLS) return 0;

        PillItem pill;
        pill.id = id ? id : "";
        pill.name = displayName ? displayName : path;
        pill.path = path;
        pill.size = sizeBytes;
        pill.isExternal = 1;
        pill.isTemp = isTemp;
        m_pills.push_back(pill);
        m_uiActive = 1;
    }
    relayout();
    return 1;
}

void AttachmentBar::onSend()
{
    {
        ScopedLock lock(&m_lock);
        /* External files are fetched by the bridge after the send (ATTGET),
         * so they move to the transfer queue; workspace files are already
         * on its side. */
        for (unsigned i = 0; i < m_pills.size(); i++) {
            if (!m_pills[i].isExternal) continue;
            /* Bounded: a bridge that never fetches must not grow this
             * without limit. The oldest goes first; its temp file with it. */
            if (m_transferring.size() >= RC_MAX_TRANSFERS) {
                dropTemp(m_transferring.front());
                m_transferring.pop_front();
            }
            m_transferring.push_back(m_pills[i]);
        }
        m_pills.clear();
        m_uiActive = 0;
        m_pressedIdx = -1;
        m_pressedState = 0;
    }
    if (valid())
        ShowWindow(hwnd(), SW_HIDE);
    if (m_listener) m_listener->onAttachmentsChanged();
}

void AttachmentBar::clear()
{
    {
        ScopedLock lock(&m_lock);
        for (unsigned i = 0; i < m_pills.size(); i++)
            dropTemp(m_pills[i]);
        m_pills.clear();
        m_uiActive = 0;
        m_pressedIdx = -1;
        m_pressedState = 0;
        for (std::list<PillItem>::iterator it = m_transferring.begin();
             it != m_transferring.end(); ++it)
            dropTemp(*it);
        m_transferring.clear();
    }
    if (valid())
        ShowWindow(hwnd(), SW_HIDE);
    if (m_listener) m_listener->onAttachmentsChanged();
}

std::string AttachmentBar::workspaceAttachments() const
{
    std::string out;
    for (unsigned i = 0; i < m_pills.size(); i++) {
        if (m_pills[i].isExternal) continue;
        if (!out.empty()) out += '|';
        out += m_pills[i].path;
    }
    return out;
}

void AttachmentBar::externalAttachments(std::string *ids,
                                        std::string *names) const
{
    ids->erase();
    names->erase();
    bool first = true;
    for (unsigned i = 0; i < m_pills.size(); i++) {
        if (!m_pills[i].isExternal) continue;
        if (!first) {
            *ids += '|';
            *names += '|';
        }
        *ids += m_pills[i].id;
        *names += m_pills[i].name;
        first = false;
    }
}

std::string AttachmentBar::echoString() const
{
    if (m_pills.empty()) return std::string();
    std::string out = "[Attached: ";
    for (unsigned i = 0; i < m_pills.size(); i++) {
        if (i > 0) out += ", ";
        out += m_pills[i].name;
    }
    out += ']';
    return out;
}

/* WORKER THREAD. Both tables are shared with the UI thread, so the lookup
 * holds the lock -- it did not, before the tables were containers. */
int AttachmentBar::lookupTransferring(const char *attId, char *outPath,
                                      size_t cap)
{
    if (!attId || !*attId || !outPath || cap == 0) return 0;
    ScopedLock lock(&m_lock);
    for (std::list<PillItem>::iterator it = m_transferring.begin();
         it != m_transferring.end(); ++it) {
        if (it->id == attId)
            return rc_strcpy(outPath, cap, it->path.c_str()) ? 1 : 0;
    }
    for (unsigned i = 0; i < m_pills.size(); i++) {
        if (m_pills[i].isExternal && m_pills[i].id == attId)
            return rc_strcpy(outPath, cap, m_pills[i].path.c_str()) ? 1 : 0;
    }
    return 0;
}

/* WORKER THREAD, under the lock for the same reason. */
void AttachmentBar::finishTransfer(const char *attId)
{
    if (!attId) return;
    ScopedLock lock(&m_lock);
    for (std::list<PillItem>::iterator it = m_transferring.begin();
         it != m_transferring.end(); ++it) {
        if (it->id == attId) {
            dropTemp(*it);
            m_transferring.erase(it);
            break;
        }
    }
}

int AttachmentBar::lookupAttachment(void *ctx, const char *attId,
                                    char *outPath, size_t cap)
{
    return ctx ? ((AttachmentBar *)ctx)->lookupTransferring(attId, outPath,
                                                            cap) : 0;
}

void AttachmentBar::transferDone(void *ctx, const char *attId)
{
    if (ctx)
        ((AttachmentBar *)ctx)->finishTransfer(attId);
}
