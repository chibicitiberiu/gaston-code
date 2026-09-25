/* pillbar.h -- the attachment pill bar sitting above the input box.
 *
 * Displays active staged attachments (workspace files, external files,
 * or screenshots) as small individual pills matching the toolbar split-button style:
 * | file1.txt | X |   | file2.png | X |
 * Modeless and a child of the main window, matching app/bar.h and
 * app/pagebar.h. A Window (core/window.h): its own class, no s_instance.
 */
#ifndef RC_PILLBAR_H
#define RC_PILLBAR_H

#include <windows.h>
#include "core/rc_cpp.h"
#include "core/window.h"

#ifndef __cplusplus
#error "pillbar.h is C++; the pill bar is a class now"
#endif

#define RC_MAX_PILLS 8
#define ID_PILL_CLOSE_BASE 1030
#define ID_PILL_LABEL_BASE 1040

#define PILL_H 22

struct PillItem {
    char  id[16];           /* e.g. "att_1", or empty if workspace file */
    char  name[MAX_PATH];   /* Display name: "WIN.INI", "src/main.c" */
    char  path[MAX_PATH];   /* Full guest path or relative workspace path */
    DWORD size;             /* File size in bytes */
    int   isExternal;       /* 1 if outside workspace (needs ATTGET) */
    int   isTemp;           /* 1 if temp file (e.g. screenshot BMP) */
    RECT  rcLabel;          /* Rect within the bar for the label box */
    RECT  rcClose;          /* Rect within the bar for the close button */
};

/* Told whenever the set of staged attachments changes (one detached, all
 * sent or cleared), so the owner can re-run its layout: the bar's row comes
 * and goes with its contents. */
class AttachmentBarListener
{
public:
    virtual ~AttachmentBarListener() {}
    virtual void onAttachmentsChanged() = 0;
};

class AttachmentBar : public Window
{
public:
    static AttachmentBar *create(HWND parent, HINSTANCE inst, HFONT font,
                                 AttachmentBarListener *listener);
    ~AttachmentBar();

    void move(int cx, int y, int cw);
    bool command(int id);      /* Retained for backward-compat routing */

    bool active() const { return m_uiActive && (m_count > 0); }
    int  count() const  { return m_count; }

    int  stageWorkspace(const char *relPath, DWORD sizeBytes = 0);
    int  stageExternal(const char *id, const char *absPath,
                       const char *displayName, DWORD sizeBytes = 0,
                       int isTemp = 0);
    void onSend();
    void clear();

    int getWorkspaceAttachments(char *out, size_t cap) const;
    int getExternalAttachments(char *outIds, size_t capIds,
                               char *outNames, size_t capNames) const;
    int getEchoString(char *out, size_t cap) const;

    /* For the sync worker thread, which reaches this object through
     * rc_syncrpc_set_attachment_hooks with it as the context. Both take the
     * lock; the connection is stopped before this object is destroyed. */
    static int lookupAttachment(void *ctx, const char *attId, char *outPath,
                                size_t cap);
    static void transferDone(void *ctx, const char *attId);

protected:
    LRESULT handle(UINT msg, WPARAM wp, LPARAM lp);

private:
    AttachmentBar(HWND parent, HINSTANCE inst, HFONT font,
                  AttachmentBarListener *listener);
    AttachmentBar(const AttachmentBar &);
    AttachmentBar &operator=(const AttachmentBar &);

    void updateLayout(int width);
    int  removePill(int idx);
    int  lookupTransferring(const char *attId, char *outPath, size_t cap);
    void finishTransfer(const char *attId);

    void paint(HDC hdc);
    void onLButtonDown(int x, int y);
    void onMouseMove(int x, int y);
    void onLButtonUp(int x, int y);

    HWND m_parent;
    HFONT m_font;
    AttachmentBarListener *m_listener;

    PillItem m_pills[RC_MAX_PILLS];
    int m_count;
    int m_uiActive;

    int m_pressedIdx;
    int m_pressedState;

    /* Background transfer queue for attachments in flight after send,
     * shared with the sync worker thread under m_lock. */
    PillItem m_transferring[RC_MAX_PILLS];
    int m_transferringCount;
    CRITICAL_SECTION m_lock;
};

#endif
