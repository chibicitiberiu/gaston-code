/* pillbar.h -- the attachment pill bar sitting above the input box.
 *
 * Displays active staged attachments (workspace files, external files,
 * or screenshots) as small individual pills matching the toolbar split-button style:
 * | file1.txt | X |   | file2.png | X |
 * Modeless and child of g_main, matching app/bar.h and app/pagebar.h.
 */
#ifndef RC_PILLBAR_H
#define RC_PILLBAR_H

#include <windows.h>
#include "core/rc_cpp.h"

#ifndef __cplusplus
#error "pillbar.h is C++; the pill bar is a class now"
#endif

#define RC_MAX_PILLS 8
#define ID_PILL_CLOSE_BASE 1030
#define ID_PILL_LABEL_BASE 1040

#define PILL_H 22

struct rc_pill_item {
    char  id[16];           /* e.g. "att_1", or empty if workspace file */
    char  name[MAX_PATH];   /* Display name: "WIN.INI", "src/main.c" */
    char  path[MAX_PATH];   /* Full guest path or relative workspace path */
    DWORD size;             /* File size in bytes */
    int   is_external;      /* 1 if outside workspace (needs ATTGET) */
    int   is_temp;          /* 1 if temp file (e.g. screenshot BMP) */
    RECT  rc_label;         /* Screen rect within pillbar for label box */
    RECT  rc_close;         /* Screen rect within pillbar for close button */
};

class rc_pillbar {
    HWND      parent_hwnd;
    HINSTANCE app_inst;
    HFONT     font;
    void    (*on_detach_cb)(void);

    HWND         hwnd_bar;
    rc_pill_item m_pills[RC_MAX_PILLS];
    int          m_count;
    int          m_ui_active;

    int          m_pressed_idx;
    int          m_pressed_state;

    /* Background transfer queue for attachments in flight after send */
    rc_pill_item m_transferring[RC_MAX_PILLS];
    int          m_transferring_count;

    void update_layout(int width);
    int  remove_pill(int idx);

    rc_pillbar(const rc_pillbar &);
    rc_pillbar &operator=(const rc_pillbar &);
    rc_pillbar(HWND parent, HINSTANCE inst, HFONT font_, void (*on_detach)(void));

public:
    static rc_pillbar *create(HWND parent, HINSTANCE inst, HFONT font, void (*on_detach)(void));
    ~rc_pillbar();

    static LRESULT CALLBACK bar_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp);

    void move(int cx, int y, int cw);
    int  command(int id);      /* Retained for backward-compat routing */

    int  active() const { return m_ui_active && (m_count > 0); }
    int  count() const  { return m_count; }

    int  stage_workspace(const char *rel_path, DWORD size_bytes = 0);
    int  stage_external(const char *id, const char *abs_path, const char *display_name,
                        DWORD size_bytes = 0, int is_temp = 0);
    void on_send();
    void clear();

    int get_workspace_attachments(char *out, size_t cap) const;
    int get_external_attachments(char *out_ids, size_t cap_ids,
                                 char *out_names, size_t cap_names) const;
    int get_echo_string(char *out, size_t cap) const;

    int  lookup_transferring(const char *att_id, char *out_path, size_t cap);
    void finish_transfer(const char *att_id);

    void paint(HDC hdc);
    void on_lbutton_down(int x, int y);
    void on_mouse_move(int x, int y);
    void on_lbutton_up(int x, int y);
};

/* Global accessor for RPC worker to lookup staged attachments and notify transfer completion */
int  rc_pillbar_lookup_attachment(const char *att_id, char *out_path, size_t cap);
void rc_pillbar_transfer_done(const char *att_id);

#endif
