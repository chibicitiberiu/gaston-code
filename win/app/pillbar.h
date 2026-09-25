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

#include <list>
#include <string>
#include <vector>

#ifndef __cplusplus
#error "pillbar.h is C++; the pill bar is a class now"
#endif

#define RC_MAX_PILLS 8
#define ID_PILL_CLOSE_BASE 1030
#define ID_PILL_LABEL_BASE 1040

#define PILL_H 22

struct PillItem {
    std::string id;         /* e.g. "att_1", or empty if workspace file */
    std::string name;       /* Display name: "WIN.INI", "src/main.c" */
    std::string path;       /* Full guest path or relative workspace path */
    DWORD size;             /* File size in bytes */
    int   isExternal;       /* 1 if outside workspace (needs ATTGET) */
    int   isTemp;           /* 1 if temp file (e.g. screenshot BMP) */
    RECT  rcLabel;          /* Rect within the bar for the label box */
    RECT  rcClose;          /* Rect within the bar for the close button */

    PillItem() : size(0), isExternal(0), isTemp(0)
    {
        SetRectEmpty(&rcLabel);
        SetRectEmpty(&rcClose);
    }
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

    bool active() const { return m_uiActive && !m_pills.empty(); }
    int  count() const  { return (int)m_pills.size(); }

    int  stageWorkspace(const char *relPath, DWORD sizeBytes = 0);
    int  stageExternal(const char *id, const char *absPath,
                       const char *displayName, DWORD sizeBytes = 0,
                       int isTemp = 0);
    void onSend();
    void clear();

    /* The staged workspace files' paths, '|'-separated, for INPUT. */
    std::string workspaceAttachments() const;
    /* The staged external files' ids and display names, each '|'-separated
     * in the same order, for INPUT. */
    void externalAttachments(std::string *ids, std::string *names) const;
    /* "[Attached: a, b]" for the local echo; empty if nothing is staged. */
    std::string echoString() const;

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
    void relayout();
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

    /* Staged pills. RC_MAX_PILLS is a cap on how many can be staged -- the
     * row has room for about that many -- not storage. */
    std::vector<PillItem> m_pills;
    int m_uiActive;

    int m_pressedIdx;
    int m_pressedState;

    /* Background transfer queue for attachments in flight after send,
     * shared with the sync worker thread under m_lock. */
    std::list<PillItem> m_transferring;
    CRITICAL_SECTION m_lock;
};

#endif
