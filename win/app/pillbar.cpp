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

static const char BAR_CLASS[] = "GastonAttachmentBar";

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
      m_count(0), m_uiActive(0), m_pressedIdx(-1), m_pressedState(0),
      m_transferringCount(0)
{
    memset(m_pills, 0, sizeof(m_pills));
    memset(m_transferring, 0, sizeof(m_transferring));
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

void AttachmentBar::paint(HDC hdc)
{
    HFONT old_font = m_font ? (HFONT)SelectObject(hdc, m_font) : NULL;
    SetBkMode(hdc, TRANSPARENT);

    for (int i = 0; i < m_count; i++) {
        RECT rc_lbl = m_pills[i].rcLabel;
        RECT rc_cls = m_pills[i].rcClose;

        /* 1. Left Box: Label with 3D raised border */
        FillRect(hdc, &rc_lbl, (HBRUSH)(COLOR_BTNFACE + 1));
        DrawEdge(hdc, &rc_lbl, EDGE_RAISED, BF_RECT);

        char sz_str[32];
        char text[MAX_PATH + 64];
        format_size(m_pills[i].size, sz_str, sizeof(sz_str));
        rc_strf(text, sizeof(text), "%s (%s)", m_pills[i].name, sz_str);

        RECT rc_txt = rc_lbl;
        rc_txt.left += 6;
        rc_txt.right -= 6;
        SetTextColor(hdc, GetSysColor(COLOR_BTNTEXT));
        DrawTextA(hdc, text, (int)strlen(text), &rc_txt,
                  DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX | DT_END_ELLIPSIS);

        /* 2. Right Box: Close button with 3D raised border (sunken when pressed) */
        FillRect(hdc, &rc_cls, (HBRUSH)(COLOR_BTNFACE + 1));
        int is_pressed = (m_pressedIdx == i && m_pressedState);
        if (is_pressed) {
            DrawEdge(hdc, &rc_cls, EDGE_SUNKEN, BF_RECT);
        } else {
            DrawEdge(hdc, &rc_cls, EDGE_RAISED, BF_RECT);
        }

        RECT rc_x = rc_cls;
        if (is_pressed) {
            OffsetRect(&rc_x, 1, 1);
        }
        DrawTextA(hdc, "x", 1, &rc_x, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
    }

    if (old_font) SelectObject(hdc, old_font);
}

void AttachmentBar::onLButtonDown(int x, int y)
{
    POINT pt = { x, y };
    for (int i = 0; i < m_count; i++) {
        if (PtInRect(&m_pills[i].rcClose, pt)) {
            m_pressedIdx = i;
            m_pressedState = 1;
            SetCapture(hwnd());
            InvalidateRect(hwnd(), &m_pills[i].rcClose, TRUE);
            break;
        }
    }
}

void AttachmentBar::onMouseMove(int x, int y)
{
    if (m_pressedIdx >= 0 && m_pressedIdx < m_count) {
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
        if (state && idx < m_count && PtInRect(&m_pills[idx].rcClose, pt)) {
            removePill(idx);
        } else {
            InvalidateRect(hwnd(), NULL, TRUE);
        }
    }
}

void AttachmentBar::updateLayout(int width)
{
    if (m_count <= 0) return;

    HDC hdc = GetDC(valid() ? hwnd() : m_parent);
    HFONT old_font = m_font ? (HFONT)SelectObject(hdc, m_font) : NULL;

    int cur_x = 2;
    int pill_y = 1;
    int pill_h = PILL_H - 2; /* 20px */
    int close_w = 16;
    int max_x = width - 4;

    for (int i = 0; i < m_count; i++) {
        char sz_str[32];
        char text[MAX_PATH + 64];
        format_size(m_pills[i].size, sz_str, sizeof(sz_str));
        rc_strf(text, sizeof(text), "%s (%s)", m_pills[i].name, sz_str);

        SIZE sz = { 0, 0 };
        GetTextExtentPoint32A(hdc, text, (int)strlen(text), &sz);
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

int AttachmentBar::removePill(int idx)
{
    ScopedLock lock(&m_lock);
    if (idx < 0 || idx >= m_count) return 0;

    if (m_pills[idx].isTemp && m_pills[idx].path[0]) {
        DeleteFileA(m_pills[idx].path);
    }

    for (int i = idx; i < m_count - 1; i++) {
        m_pills[i] = m_pills[i + 1];
    }
    memset(&m_pills[m_count - 1], 0, sizeof(PillItem));

    m_count--;
    if (m_count == 0) {
        m_uiActive = 0;
    }

    if (valid()) {
        RECT rc;
        GetClientRect(hwnd(), &rc);
        updateLayout(rc.right - rc.left);
        InvalidateRect(hwnd(), NULL, TRUE);
    }

    if (m_listener) m_listener->onAttachmentsChanged();
    return 1;
}

int AttachmentBar::stageWorkspace(const char *relPath, DWORD sizeBytes)
{
    ScopedLock lock(&m_lock);
    for (int i = 0; i < m_count; i++) {
        if (!m_pills[i].isExternal && strcmp(m_pills[i].path, relPath) == 0) {
            return 1;
        }
    }
    if (m_count >= RC_MAX_PILLS) return 0;

    int idx = m_count++;
    m_uiActive = 1;
    m_pills[idx].id[0] = '\0';
    rc_strf(m_pills[idx].name, sizeof(m_pills[idx].name), "%s", relPath ? relPath : "");
    rc_strf(m_pills[idx].path, sizeof(m_pills[idx].path), "%s", relPath ? relPath : "");
    m_pills[idx].size = sizeBytes;
    m_pills[idx].isExternal = 0;
    m_pills[idx].isTemp = 0;

    if (valid()) {
        RECT rc;
        GetClientRect(hwnd(), &rc);
        updateLayout(rc.right - rc.left);
        InvalidateRect(hwnd(), NULL, TRUE);
    }
    return 1;
}

int AttachmentBar::stageExternal(const char *id, const char *absPath, const char *displayName,
                               DWORD sizeBytes, int isTemp)
{
    ScopedLock lock(&m_lock);
    if (!isTemp) {
        for (int i = 0; i < m_count; i++) {
            if (m_pills[i].isExternal && strcmp(m_pills[i].path, absPath) == 0) {
                return 1;
            }
        }
    }
    if (m_count >= RC_MAX_PILLS) return 0;

    int idx = m_count++;
    m_uiActive = 1;
    rc_strf(m_pills[idx].id, sizeof(m_pills[idx].id), "%s", id ? id : "");
    rc_strf(m_pills[idx].name, sizeof(m_pills[idx].name), "%s", displayName ? displayName : absPath);
    rc_strf(m_pills[idx].path, sizeof(m_pills[idx].path), "%s", absPath ? absPath : "");
    m_pills[idx].size = sizeBytes;
    m_pills[idx].isExternal = 1;
    m_pills[idx].isTemp = isTemp;

    if (valid()) {
        RECT rc;
        GetClientRect(hwnd(), &rc);
        updateLayout(rc.right - rc.left);
        InvalidateRect(hwnd(), NULL, TRUE);
    }
    return 1;
}

void AttachmentBar::onSend()
{
    ScopedLock lock(&m_lock);
    for (int i = 0; i < m_count; i++) {
        if (m_pills[i].isExternal) {
            if (m_transferringCount < RC_MAX_PILLS) {
                m_transferring[m_transferringCount++] = m_pills[i];
            }
        }
    }
    memset(m_pills, 0, sizeof(m_pills));
    m_count = 0;
    m_uiActive = 0;
    m_pressedIdx = -1;
    m_pressedState = 0;
    if (valid()) {
        ShowWindow(hwnd(), SW_HIDE);
    }
    if (m_listener) m_listener->onAttachmentsChanged();
}

void AttachmentBar::clear()
{
    ScopedLock lock(&m_lock);
    for (int i = 0; i < m_count; i++) {
        if (m_pills[i].isTemp && m_pills[i].path[0]) {
            DeleteFileA(m_pills[i].path);
        }
    }
    memset(m_pills, 0, sizeof(m_pills));
    m_count = 0;
    m_uiActive = 0;
    m_pressedIdx = -1;
    m_pressedState = 0;

    for (int i = 0; i < m_transferringCount; i++) {
        if (m_transferring[i].isTemp && m_transferring[i].path[0]) {
            DeleteFileA(m_transferring[i].path);
        }
    }
    m_transferringCount = 0;

    if (valid()) {
        ShowWindow(hwnd(), SW_HIDE);
    }
    if (m_listener) m_listener->onAttachmentsChanged();
}

int AttachmentBar::getWorkspaceAttachments(char *out, size_t cap) const
{
    if (!out || cap == 0) return 0;
    out[0] = '\0';
    int found = 0;
    size_t used = 0;
    for (int i = 0; i < m_count; i++) {
        if (!m_pills[i].isExternal) {
            if (found > 0) {
                if (used + 1 < cap) {
                    out[used++] = '|';
                    out[used] = '\0';
                }
            }
            size_t plen = strlen(m_pills[i].path);
            if (used + plen < cap) {
                memcpy(out + used, m_pills[i].path, plen);
                used += plen;
                out[used] = '\0';
            }
            found++;
        }
    }
    return found;
}

int AttachmentBar::getExternalAttachments(char *outIds, size_t capIds,
                                         char *outNames, size_t capNames) const
{
    if (!outIds || capIds == 0 || !outNames || capNames == 0) return 0;
    outIds[0] = '\0';
    outNames[0] = '\0';
    int found = 0;
    size_t used_ids = 0;
    size_t used_names = 0;
    for (int i = 0; i < m_count; i++) {
        if (m_pills[i].isExternal) {
            if (found > 0) {
                if (used_ids + 1 < capIds) {
                    outIds[used_ids++] = '|';
                    outIds[used_ids] = '\0';
                }
                if (used_names + 1 < capNames) {
                    outNames[used_names++] = '|';
                    outNames[used_names] = '\0';
                }
            }
            size_t id_len = strlen(m_pills[i].id);
            if (used_ids + id_len < capIds) {
                memcpy(outIds + used_ids, m_pills[i].id, id_len);
                used_ids += id_len;
                outIds[used_ids] = '\0';
            }
            size_t name_len = strlen(m_pills[i].name);
            if (used_names + name_len < capNames) {
                memcpy(outNames + used_names, m_pills[i].name, name_len);
                used_names += name_len;
                outNames[used_names] = '\0';
            }
            found++;
        }
    }
    return found;
}

int AttachmentBar::getEchoString(char *out, size_t cap) const
{
    if (!out || cap == 0) return 0;
    out[0] = '\0';
    if (m_count == 0) return 0;
    rc_strf(out, cap, "[Attached: ");
    size_t used = strlen(out);
    for (int i = 0; i < m_count; i++) {
        if (i > 0 && used + 2 < cap) {
            memcpy(out + used, ", ", 2);
            used += 2;
            out[used] = '\0';
        }
        size_t nlen = strlen(m_pills[i].name);
        if (used + nlen < cap) {
            memcpy(out + used, m_pills[i].name, nlen);
            used += nlen;
            out[used] = '\0';
        }
    }
    if (used + 1 < cap) {
        out[used++] = ']';
        out[used] = '\0';
    }
    return (int)used;
}

int AttachmentBar::lookupTransferring(const char *attId, char *outPath, size_t cap)
{
    if (!attId || !*attId || !outPath || cap == 0) return 0;
    for (int i = 0; i < m_transferringCount; i++) {
        if (strcmp(m_transferring[i].id, attId) == 0) {
            rc_strf(outPath, cap, "%s", m_transferring[i].path);
            return 1;
        }
    }
    for (int i = 0; i < m_count; i++) {
        if (m_pills[i].isExternal && strcmp(m_pills[i].id, attId) == 0) {
            rc_strf(outPath, cap, "%s", m_pills[i].path);
            return 1;
        }
    }
    return 0;
}

void AttachmentBar::finishTransfer(const char *attId)
{
    if (!attId) return;
    for (int i = 0; i < m_transferringCount; i++) {
        if (strcmp(m_transferring[i].id, attId) == 0) {
            if (m_transferring[i].isTemp && m_transferring[i].path[0]) {
                DeleteFileA(m_transferring[i].path);
            }
            for (int j = i; j < m_transferringCount - 1; j++) {
                m_transferring[j] = m_transferring[j + 1];
            }
            m_transferringCount--;
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
