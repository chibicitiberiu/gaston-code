/* File > Export as HTML: the whole conversation, written to a file here.
 *
 * THE GUEST NEVER HOLDS THE DOCUMENT. A transcript is hundreds of kilobytes
 * and this machine has tens free after a turn, so every chunk goes from the
 * socket to the file and is forgotten. That is the same rule win/sync's
 * do_put already follows, and the reason this is a streaming sink rather than
 * "ask for the HTML and save it".
 *
 * THE PATH IS CHOSEN BEFORE THE REQUEST IS SENT, which is the one ordering
 * decision worth knowing about. A Save dialog is modal, and a modal on this
 * thread still pumps WM_RC_FRAME -- the reader thread keeps posting and
 * render_frame keeps running. So a dialog opened when the data starts
 * arriving would be dispatching EXPORTDAT frames with no file open, and the
 * export would silently lose its first chunks. Ask first, then ask the
 * bridge.
 *
 * Written to a TEMPORARY file beside the destination and renamed on a clean
 * END. A transfer that dies halfway otherwise leaves a truncated file with
 * the name the user chose, which is worse than no file at all -- they would
 * open it, see most of a conversation, and have no way to tell that the end
 * is missing.
 */
#ifndef RC_EXPORT_H
#define RC_EXPORT_H

#ifndef __cplusplus
#error "export.h is C++"
#endif

#include <windows.h>
#include <stdio.h>
#include "net/frameproto.h"

class rc_html;

class rc_export {
    FILE *fp;
    char  dest[MAX_PATH];
    char  tmp[MAX_PATH];
    unsigned long got;      /* decompressed bytes written so far */
    unsigned long total;    /* what EXPORTSTART said to expect, or 0 */
    rc_html *html;          /* notices and errors; may be NULL */

    rc_export(const rc_export &);
    rc_export &operator=(const rc_export &);
    rc_export() : fp(0), got(0), total(0), html(0) { dest[0] = tmp[0] = 0; }

    void cleanup(void);

public:
    static rc_export *create(rc_html *html);
    ~rc_export();

    /* Ask for a destination and open the temp file. Non-zero if the caller
     * should now send EXPORT. Zero means the user cancelled or the file
     * could not be opened, and nothing was sent. */
    int begin(HWND owner, const char *suggested);

    /* Undo `begin` without having received anything -- for a send that did
     * not reach the bridge. */
    void give_up(void);

    /* EXPORTSTART / EXPORTDAT / EXPORTEND. Non-zero if `f` was one of them. */
    int frame(rc_frame *f);

    /* Is a transfer in progress? Export refuses to start a second one: two
     * sinks would interleave into each other's files. */
    int busy(void) const { return fp != 0; }
};

#endif
