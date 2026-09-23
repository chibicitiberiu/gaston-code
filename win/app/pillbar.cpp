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

static rc_pillbar *s_instance = NULL;
static CRITICAL_SECTION g_pill_cs;
static int g_pill_cs_init = 0;
static int g_pillbar_class_registered = 0;

static void check_child(const char *what, HWND h)
{
    if (!h)
        rc_log("pillbar: %s failed to create (GetLastError=%lu)",
               what, (unsigned long)GetLastError());
}

static void register_pillbar_class(HINSTANCE inst)
{
    if (!g_pillbar_class_registered) {
        WNDCLASSA wc;
        memset(&wc, 0, sizeof(wc));
        wc.style = CS_HREDRAW | CS_VREDRAW;
        wc.lpfnWndProc = rc_pillbar::bar_proc;
        wc.hInstance = inst;
        wc.hCursor = LoadCursor(NULL, IDC_ARROW);
        wc.hbrBackground = (HBRUSH)(COLOR_BTNFACE + 1);
        wc.lpszClassName = "rc_pillbar";
        if (!RegisterClassA(&wc)) {
            rc_log("pillbar: RegisterClass failed (%lu)", (unsigned long)GetLastError());
        } else {
            g_pillbar_class_registered = 1;
        }
    }
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

rc_pillbar::rc_pillbar(HWND parent, HINSTANCE inst, HFONT font_, void (*on_detach)(void))
    : parent_hwnd(parent), app_inst(inst), font(font_), on_detach_cb(on_detach),
      hwnd_bar(NULL), m_count(0), m_ui_active(0),
      m_pressed_idx(-1), m_pressed_state(0),
      m_transferring_count(0)
{
    memset(m_pills, 0, sizeof(m_pills));
    memset(m_transferring, 0, sizeof(m_transferring));

    register_pillbar_class(inst);

    hwnd_bar = CreateWindowExA(
        0, "rc_pillbar", "",
        WS_CHILD | WS_CLIPSIBLINGS,
        0, 0, 0, 0, parent, NULL, inst, NULL);
    check_child("pillbar window", hwnd_bar);
}

rc_pillbar::~rc_pillbar()
{
    clear();
    if (hwnd_bar) {
        DestroyWindow(hwnd_bar);
        hwnd_bar = NULL;
    }
    s_instance = NULL;
}

rc_pillbar *rc_pillbar::create(HWND parent, HINSTANCE inst, HFONT font, void (*on_detach)(void))
{
    if (!g_pill_cs_init) {
        InitializeCriticalSection(&g_pill_cs);
        g_pill_cs_init = 1;
    }
    rc_pillbar *pb = new rc_pillbar(parent, inst, font, on_detach);
    s_instance = pb;
    return pb;
}

LRESULT CALLBACK rc_pillbar::bar_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    rc_pillbar *self = s_instance;
    switch (msg) {
    case WM_PAINT: {
        PAINTSTRUCT ps;
        HDC hdc = BeginPaint(hwnd, &ps);
        if (self) self->paint(hdc);
        EndPaint(hwnd, &ps);
        return 0;
    }
    case WM_ERASEBKGND: {
        HDC hdc = (HDC)wp;
        RECT rc;
        GetClientRect(hwnd, &rc);
        FillRect(hdc, &rc, (HBRUSH)(COLOR_BTNFACE + 1));
        return 1;
    }
    case WM_LBUTTONDOWN: {
        int x = (short)LOWORD(lp);
        int y = (short)HIWORD(lp);
        if (self) self->on_lbutton_down(x, y);
        return 0;
    }
    case WM_MOUSEMOVE: {
        int x = (short)LOWORD(lp);
        int y = (short)HIWORD(lp);
        if (self) self->on_mouse_move(x, y);
        return 0;
    }
    case WM_LBUTTONUP: {
        int x = (short)LOWORD(lp);
        int y = (short)HIWORD(lp);
        if (self) self->on_lbutton_up(x, y);
        return 0;
    }
    case WM_SETCURSOR: {
        SetCursor(LoadCursor(NULL, IDC_ARROW));
        return TRUE;
    }
    }
    return DefWindowProcA(hwnd, msg, wp, lp);
}

void rc_pillbar::paint(HDC hdc)
{
    HFONT old_font = font ? (HFONT)SelectObject(hdc, font) : NULL;
    SetBkMode(hdc, TRANSPARENT);

    for (int i = 0; i < m_count; i++) {
        RECT rc_lbl = m_pills[i].rc_label;
        RECT rc_cls = m_pills[i].rc_close;

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
        int is_pressed = (m_pressed_idx == i && m_pressed_state);
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

void rc_pillbar::on_lbutton_down(int x, int y)
{
    POINT pt = { x, y };
    for (int i = 0; i < m_count; i++) {
        if (PtInRect(&m_pills[i].rc_close, pt)) {
            m_pressed_idx = i;
            m_pressed_state = 1;
            SetCapture(hwnd_bar);
            InvalidateRect(hwnd_bar, &m_pills[i].rc_close, TRUE);
            break;
        }
    }
}

void rc_pillbar::on_mouse_move(int x, int y)
{
    if (m_pressed_idx >= 0 && m_pressed_idx < m_count) {
        POINT pt = { x, y };
        int in_btn = PtInRect(&m_pills[m_pressed_idx].rc_close, pt);
        if (in_btn != m_pressed_state) {
            m_pressed_state = in_btn;
            InvalidateRect(hwnd_bar, &m_pills[m_pressed_idx].rc_close, TRUE);
        }
    }
}

void rc_pillbar::on_lbutton_up(int x, int y)
{
    if (m_pressed_idx >= 0) {
        int idx = m_pressed_idx;
        int state = m_pressed_state;
        m_pressed_idx = -1;
        m_pressed_state = 0;
        ReleaseCapture();

        POINT pt = { x, y };
        if (state && idx < m_count && PtInRect(&m_pills[idx].rc_close, pt)) {
            remove_pill(idx);
        } else {
            InvalidateRect(hwnd_bar, NULL, TRUE);
        }
    }
}

void rc_pillbar::update_layout(int width)
{
    if (m_count <= 0) return;

    HDC hdc = GetDC(hwnd_bar ? hwnd_bar : parent_hwnd);
    HFONT old_font = font ? (HFONT)SelectObject(hdc, font) : NULL;

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

        m_pills[i].rc_label.left   = cur_x;
        m_pills[i].rc_label.top    = pill_y;
        m_pills[i].rc_label.right  = cur_x + lbl_w;
        m_pills[i].rc_label.bottom = pill_y + pill_h;

        m_pills[i].rc_close.left   = cur_x + lbl_w;
        m_pills[i].rc_close.top    = pill_y;
        m_pills[i].rc_close.right  = cur_x + lbl_w + close_w;
        m_pills[i].rc_close.bottom = pill_y + pill_h;

        cur_x += lbl_w + close_w + 6; /* 6px gap to next pill */
    }

    if (old_font) SelectObject(hdc, old_font);
    ReleaseDC(hwnd_bar ? hwnd_bar : parent_hwnd, hdc);
}

void rc_pillbar::move(int cx, int y, int cw)
{
    if (!hwnd_bar) return;
    if (active()) {
        update_layout(cw);
        MoveWindow(hwnd_bar, cx, y, cw, PILL_H, TRUE);
        ShowWindow(hwnd_bar, SW_SHOW);
        InvalidateRect(hwnd_bar, NULL, TRUE);
    } else {
        ShowWindow(hwnd_bar, SW_HIDE);
    }
}

int rc_pillbar::command(int id)
{
    /* Retained for backward compatibility */
    (void)id;
    return 0;
}

int rc_pillbar::remove_pill(int idx)
{
    ScopedLock lock(&g_pill_cs);
    if (idx < 0 || idx >= m_count) return 0;

    if (m_pills[idx].is_temp && m_pills[idx].path[0]) {
        DeleteFileA(m_pills[idx].path);
    }

    for (int i = idx; i < m_count - 1; i++) {
        m_pills[i] = m_pills[i + 1];
    }
    memset(&m_pills[m_count - 1], 0, sizeof(rc_pill_item));

    m_count--;
    if (m_count == 0) {
        m_ui_active = 0;
    }

    if (hwnd_bar) {
        RECT rc;
        GetClientRect(hwnd_bar, &rc);
        update_layout(rc.right - rc.left);
        InvalidateRect(hwnd_bar, NULL, TRUE);
    }

    if (on_detach_cb) on_detach_cb();
    return 1;
}

int rc_pillbar::stage_workspace(const char *rel_path, DWORD size_bytes)
{
    ScopedLock lock(&g_pill_cs);
    for (int i = 0; i < m_count; i++) {
        if (!m_pills[i].is_external && strcmp(m_pills[i].path, rel_path) == 0) {
            return 1;
        }
    }
    if (m_count >= RC_MAX_PILLS) return 0;

    int idx = m_count++;
    m_ui_active = 1;
    m_pills[idx].id[0] = '\0';
    rc_strf(m_pills[idx].name, sizeof(m_pills[idx].name), "%s", rel_path ? rel_path : "");
    rc_strf(m_pills[idx].path, sizeof(m_pills[idx].path), "%s", rel_path ? rel_path : "");
    m_pills[idx].size = size_bytes;
    m_pills[idx].is_external = 0;
    m_pills[idx].is_temp = 0;

    if (hwnd_bar) {
        RECT rc;
        GetClientRect(hwnd_bar, &rc);
        update_layout(rc.right - rc.left);
        InvalidateRect(hwnd_bar, NULL, TRUE);
    }
    return 1;
}

int rc_pillbar::stage_external(const char *id, const char *abs_path, const char *display_name,
                               DWORD size_bytes, int is_temp)
{
    ScopedLock lock(&g_pill_cs);
    if (!is_temp) {
        for (int i = 0; i < m_count; i++) {
            if (m_pills[i].is_external && strcmp(m_pills[i].path, abs_path) == 0) {
                return 1;
            }
        }
    }
    if (m_count >= RC_MAX_PILLS) return 0;

    int idx = m_count++;
    m_ui_active = 1;
    rc_strf(m_pills[idx].id, sizeof(m_pills[idx].id), "%s", id ? id : "");
    rc_strf(m_pills[idx].name, sizeof(m_pills[idx].name), "%s", display_name ? display_name : abs_path);
    rc_strf(m_pills[idx].path, sizeof(m_pills[idx].path), "%s", abs_path ? abs_path : "");
    m_pills[idx].size = size_bytes;
    m_pills[idx].is_external = 1;
    m_pills[idx].is_temp = is_temp;

    if (hwnd_bar) {
        RECT rc;
        GetClientRect(hwnd_bar, &rc);
        update_layout(rc.right - rc.left);
        InvalidateRect(hwnd_bar, NULL, TRUE);
    }
    return 1;
}

void rc_pillbar::on_send()
{
    ScopedLock lock(&g_pill_cs);
    for (int i = 0; i < m_count; i++) {
        if (m_pills[i].is_external) {
            if (m_transferring_count < RC_MAX_PILLS) {
                m_transferring[m_transferring_count++] = m_pills[i];
            }
        }
    }
    memset(m_pills, 0, sizeof(m_pills));
    m_count = 0;
    m_ui_active = 0;
    m_pressed_idx = -1;
    m_pressed_state = 0;
    if (hwnd_bar) {
        ShowWindow(hwnd_bar, SW_HIDE);
    }
    if (on_detach_cb) on_detach_cb();
}

void rc_pillbar::clear()
{
    ScopedLock lock(&g_pill_cs);
    for (int i = 0; i < m_count; i++) {
        if (m_pills[i].is_temp && m_pills[i].path[0]) {
            DeleteFileA(m_pills[i].path);
        }
    }
    memset(m_pills, 0, sizeof(m_pills));
    m_count = 0;
    m_ui_active = 0;
    m_pressed_idx = -1;
    m_pressed_state = 0;

    for (int i = 0; i < m_transferring_count; i++) {
        if (m_transferring[i].is_temp && m_transferring[i].path[0]) {
            DeleteFileA(m_transferring[i].path);
        }
    }
    m_transferring_count = 0;

    if (hwnd_bar) {
        ShowWindow(hwnd_bar, SW_HIDE);
    }
    if (on_detach_cb) on_detach_cb();
}

int rc_pillbar::get_workspace_attachments(char *out, size_t cap) const
{
    if (!out || cap == 0) return 0;
    out[0] = '\0';
    int found = 0;
    size_t used = 0;
    for (int i = 0; i < m_count; i++) {
        if (!m_pills[i].is_external) {
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

int rc_pillbar::get_external_attachments(char *out_ids, size_t cap_ids,
                                         char *out_names, size_t cap_names) const
{
    if (!out_ids || cap_ids == 0 || !out_names || cap_names == 0) return 0;
    out_ids[0] = '\0';
    out_names[0] = '\0';
    int found = 0;
    size_t used_ids = 0;
    size_t used_names = 0;
    for (int i = 0; i < m_count; i++) {
        if (m_pills[i].is_external) {
            if (found > 0) {
                if (used_ids + 1 < cap_ids) {
                    out_ids[used_ids++] = '|';
                    out_ids[used_ids] = '\0';
                }
                if (used_names + 1 < cap_names) {
                    out_names[used_names++] = '|';
                    out_names[used_names] = '\0';
                }
            }
            size_t id_len = strlen(m_pills[i].id);
            if (used_ids + id_len < cap_ids) {
                memcpy(out_ids + used_ids, m_pills[i].id, id_len);
                used_ids += id_len;
                out_ids[used_ids] = '\0';
            }
            size_t name_len = strlen(m_pills[i].name);
            if (used_names + name_len < cap_names) {
                memcpy(out_names + used_names, m_pills[i].name, name_len);
                used_names += name_len;
                out_names[used_names] = '\0';
            }
            found++;
        }
    }
    return found;
}

int rc_pillbar::get_echo_string(char *out, size_t cap) const
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

int rc_pillbar::lookup_transferring(const char *att_id, char *out_path, size_t cap)
{
    if (!att_id || !*att_id || !out_path || cap == 0) return 0;
    for (int i = 0; i < m_transferring_count; i++) {
        if (strcmp(m_transferring[i].id, att_id) == 0) {
            rc_strf(out_path, cap, "%s", m_transferring[i].path);
            return 1;
        }
    }
    for (int i = 0; i < m_count; i++) {
        if (m_pills[i].is_external && strcmp(m_pills[i].id, att_id) == 0) {
            rc_strf(out_path, cap, "%s", m_pills[i].path);
            return 1;
        }
    }
    return 0;
}

void rc_pillbar::finish_transfer(const char *att_id)
{
    if (!att_id) return;
    for (int i = 0; i < m_transferring_count; i++) {
        if (strcmp(m_transferring[i].id, att_id) == 0) {
            if (m_transferring[i].is_temp && m_transferring[i].path[0]) {
                DeleteFileA(m_transferring[i].path);
            }
            for (int j = i; j < m_transferring_count - 1; j++) {
                m_transferring[j] = m_transferring[j + 1];
            }
            m_transferring_count--;
            break;
        }
    }
}

int rc_pillbar_lookup_attachment(const char *att_id, char *out_path, size_t cap)
{
    if (!g_pill_cs_init || !att_id || !*att_id || !out_path || cap == 0) return 0;
    ScopedLock lock(&g_pill_cs);
    if (s_instance) {
        return s_instance->lookup_transferring(att_id, out_path, cap);
    }
    return 0;
}

void rc_pillbar_transfer_done(const char *att_id)
{
    if (!g_pill_cs_init || !att_id) return;
    ScopedLock lock(&g_pill_cs);
    if (s_instance) {
        s_instance->finish_transfer(att_id);
    }
}
