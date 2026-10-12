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
 *
 * That sink is fs/xfer.h now, and this is one of its three callers. It went
 * the other way round: BACKLOG 6 named the version that used to live here as
 * the cleanest of the three implementations and the one to extract from.
 *
 * FROM STAGE 3 C3 THE BRIDGE SENDS RECORDS AND THIS SIDE RENDERS THEM. The
 * bridge cannot build the document any more -- it has neither the markup
 * nor the stylesheet -- and the alternative was a second renderer there,
 * which is the thing the whole stage exists to avoid.
 *
 * It does not change the streaming rule: a chunk is rendered and written
 * and forgotten, and the bridge chunks on RECORD boundaries so that a
 * record never arrives in two pieces. What this side now holds for the
 * length of the transfer is the two stylesheets, ~11KB, copied into the
 * document's <head> before the first record.
 *
 * §13 of BACKLOG said export could "assemble what is cached". IT CANNOT:
 * ranges are served on demand and win/app/cache.cpp holds only what
 * arrived, so a cache-assembled export silently omits whatever the reader
 * never paged to -- which is worse than a second renderer, because the
 * output is plausible and incomplete.
 */
#ifndef RC_EXPORT_H
#define RC_EXPORT_H

#ifndef __cplusplus
#error "export.h is C++"
#endif

#include <windows.h>
#include "net/frameproto.h"
#include "fs/xfer.h"        /* the sink this was extracted into */

class rc_html;

class rc_export {
    /* The transfer itself is libfs now. What is left here is the part that is
     * about EXPORT rather than about writing a file: which frames mean what,
     * and what to tell the person when it goes wrong.
     *
     * Export's policy differs from the sync layer's in three ways, and all
     * three are carried by WorkNames and a NULL verifier rather than by a
     * second implementation: the suffix is `.$$$`, there is no checksum to
     * check, and the old file is NOT moved aside -- on a failed rename the
     * temp is left findable and the person is told where it is. */
    ChunkSink sink;
    unsigned long total;    /* RECORD bytes EXPORTSTART said to expect, or 0 */
    /* RECORD bytes consumed, which from stage 3 C3 is NOT what has been
     * written. The bridge sends records and this side renders them, so the
     * file is markup and is a different length -- the two numbers were the
     * same thing when the bridge sent finished HTML.
     *
     * It carries the ordering invariant ChunkSink used to. Writing with
     * sink.write(sink.received(), ...) means the sink's own `off` check can
     * never fire, and win/fs/xfer.h calls that check load-bearing by name:
     * "It is CHECKED, not trusted... Two of the three implementations this
     * replaces did not check." So the check is not lost, it moved: `off` on
     * each EXPORTDAT is compared against this. */
    unsigned long recordBytes;
    int wroteHead;          /* the <html><head> with the stylesheets */
    rc_html *html;          /* notices and errors; may be NULL */

    rc_export(const rc_export &);
    rc_export &operator=(const rc_export &);
    rc_export() : total(0), recordBytes(0), wroteHead(0), html(0) {}

    /* The document's own opening: <html><head> with view.css and
     * export.css inlined, then <body>. Written before the first record.
     *
     * THIS IS THE ONE PLACE THE GUEST HOLDS MORE THAN A CHUNK -- about
     * 11KB of stylesheet, read from disk and written straight out. The
     * header above says "THE GUEST NEVER HOLDS THE DOCUMENT" and that is
     * still true of the transcript, which is what the sentence is about;
     * it holds the two stylesheets for as long as it takes to copy them. */
    int writeHead(const char *title);

    void cleanup(const char *why);

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
    int busy(void) const { return sink.active(); }
};

#endif
