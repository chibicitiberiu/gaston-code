/* export.cpp -- File > Export as HTML. See app/export.h for the design. */
#include <windows.h>
#include <commdlg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app/export.h"
#include "app/htmlview.h"
#include "core/log.h"
#include "core/str.h"
#include "core/rc_cpp.h"
#include "view/render.h"

/* GetSaveFileNameA through LoadLibrary rather than as an import.
 *
 * COMDLG32.DLL is on every Windows this targets, so this is not the
 * NT-only-import rule -- it is the same habit for the same reason: an import
 * that cannot resolve kills the image at load, before WinMain, with no log
 * written. A dialog is worth exactly one feature; it is not worth the whole
 * program refusing to start on a stripped install. Resolved on demand and
 * released afterwards, because it is used once in a while and never in a
 * loop. */
typedef BOOL (WINAPI *PFN_GETSAVEFILENAMEA)(LPOPENFILENAMEA);

/* --- the document the guest now writes ----------------------------------
 *
 * The bridge built this until stage 3 C3 and cannot any more: it has
 * neither the markup nor the stylesheet. The skeleton is compiled in for
 * the same reason the live view's is -- see win/app/htmlview.cpp -- and
 * the two stylesheets are the files beside the exe, copied in.
 */

/* Where a literal block breaks in an EXPORT. Fixed, and not the window's
 * column: a saved file is read somewhere else at some other width, and the
 * one thing worse than a fixed column is one that silently encodes how big
 * the window happened to be when Save was pressed. 76 rather than the
 * view's 56, because a file is read in a browser window and not in a 470px
 * pane. */
#define RC_EXPORT_WRAPCOL 76

static const char EXPORT_HEAD_1[] =
    "<html><head><meta http-equiv=\"Content-Type\" "
    "content=\"text/html; charset=";
static const char EXPORT_HEAD_2[] = "\"><title>";
static const char EXPORT_HEAD_3[] = "</title><style>";
static const char EXPORT_HEAD_4[] = "</style></head><body>";
static const char EXPORT_TAIL[] = "</body></html>";

/* A ByteSink over the ChunkSink, so the renderer can write straight into
 * the file. The offset is the sink's own count, which is why rc_export
 * keeps `recordBytes` separately for the ordering check -- see export.h. */
class ExportSink : public ByteSink
{
public:
    explicit ExportSink(ChunkSink &s) : m_sink(s), m_failed(false) {}

    bool write(const void *data, unsigned len)
    {
        if (m_failed) return false;
        if (m_sink.write((unsigned long)m_sink.received(), data, len)
            != CHUNK_OK) {
            m_failed = true;
            return false;
        }
        return true;
    }

    bool failed() const { return m_failed; }

private:
    ExportSink(const ExportSink &);
    ExportSink &operator=(const ExportSink &);

    ChunkSink &m_sink;
    bool       m_failed;
};

/* Copy one file beside the exe into the sink. The stylesheets, which is
 * the only thing in this path that is read rather than streamed through.
 * Missing is not fatal: an unstyled export is a readable export, and a
 * hand-updated box is exactly where one of these goes missing. */
static bool copy_css(const char *name, ByteSink &out)
{
    char dir[MAX_PATH], path[MAX_PATH];
    char buf[2048];
    size_t got;

    GetModuleFileNameA(NULL, dir, sizeof(dir) - 1);
    { char *slash = strrchr(dir, '\\'); if (slash) *slash = '\0'; }
    rc_strf(path, sizeof(path), "%s\\%s", dir, name);

    File f(fopen(path, "rb"));
    if (!f.valid()) {
        rc_log("export: no %s; the file will be unstyled", name);
        return true;
    }
    while ((got = fread(buf, 1, sizeof buf, f.get())) > 0)
        if (!out.write(buf, (unsigned)got))
            return false;
    return true;
}

int rc_export::writeHead(const char *title)
{
    ExportSink out(sink);
    char charset[24];
    const char *t = (title && *title) ? title : "Conversation";

    /* The same derivation as the live view's: the charset must name the
     * table the bytes were written with, and that is a fact about this
     * machine. */
    rc_strf(charset, sizeof charset, "windows-%u", (unsigned)GetACP());

    out.write(EXPORT_HEAD_1, (unsigned)strlen(EXPORT_HEAD_1));
    out.write(charset, (unsigned)strlen(charset));
    out.write(EXPORT_HEAD_2, (unsigned)strlen(EXPORT_HEAD_2));
    out.write(t, (unsigned)strlen(t));
    out.write(EXPORT_HEAD_3, (unsigned)strlen(EXPORT_HEAD_3));
    /* BOTH sheets, view.css first: export.css is only what the live view
     * has no use for, never a replacement. An export that looks different
     * from what was on screen invites the question of which one is right. */
    if (!copy_css("view.css", out) || !copy_css("export.css", out))
        return 0;
    out.write(EXPORT_HEAD_4, (unsigned)strlen(EXPORT_HEAD_4));
    return !out.failed();
}

static int ask_for_path(HWND owner, const char *suggested,
                        char *out, unsigned cap)
{
    PFN_GETSAVEFILENAMEA fn;
    OPENFILENAMEA ofn;
    char name[MAX_PATH];
    int ok;

    rc_strf(name, sizeof(name), "%s",
            (suggested && *suggested) ? suggested : "CHAT.HTM");

    Dll dll(LoadLibraryA("comdlg32.dll"));
    if (!dll.valid()) {
        rc_log("export: comdlg32 not available (%lu)",
               (unsigned long)GetLastError());
        return 0;
    }
    fn = (PFN_GETSAVEFILENAMEA)GetProcAddress(dll.get(), "GetSaveFileNameA");
    if (!fn) {
        rc_log("export: GetSaveFileNameA not found");
        return 0;
    }

    memset(&ofn, 0, sizeof(ofn));
    /* sizeof, not a literal: the struct grew three fields in Windows 2000 and
     * passing the larger size to a 98 comdlg32 fails the call outright.
     * WINVER is pinned at 0x0400 for this build, so sizeof is the 9x one. */
    ofn.lStructSize = sizeof(ofn);
    ofn.hwndOwner = owner;
    ofn.lpstrFilter = "Web page (*.htm)\0*.htm;*.html\0All files\0*.*\0";
    ofn.lpstrFile = name;
    ofn.nMaxFile = sizeof(name);
    ofn.lpstrTitle = "Export conversation as HTML";
    ofn.lpstrDefExt = "htm";
    ofn.Flags = OFN_OVERWRITEPROMPT | OFN_PATHMUSTEXIST | OFN_HIDEREADONLY;

    ok = fn(&ofn) ? 1 : 0;
    if (!ok) {
        /* Zero means cancelled OR failed, and the two are told apart only by
         * CommDlgExtendedError. Worth logging because the failure mode that
         * matters here -- a wrong lStructSize, CDERR_STRUCTSIZE == 1 -- looks
         * exactly like the user pressing Cancel. */
        typedef DWORD (WINAPI *PFN_ERR)(void);
        PFN_ERR err = (PFN_ERR)GetProcAddress(dll.get(), "CommDlgExtendedError");
        rc_log("export: no path chosen (ofn=%u, err=%lu)",
               (unsigned)sizeof(ofn),
               err ? (unsigned long)err() : 0UL);
    }
    if (!ok) return 0;
    rc_strf(out, cap, "%s", name);
    return 1;
}

rc_export *rc_export::create(rc_html *html_)
{
    rc_export *p = new rc_export();
    if (!p) return NULL;
    p->html = html_;
    return p;
}

rc_export::~rc_export()
{
    /* A transfer still open at shutdown is an abandoned one: the sink closes
     * its handle and takes the temp with it rather than leaving a
     * half-written .$$$ beside a file the user never got. */
    cleanup("the client is closing");
}

/* Export writes beside the destination, never in %TEMP%: the rename at the
 * end has to be within one volume, and the user may well have chosen a floppy
 * or a network drive.
 *
 * aside = false. There is no recovery pass over wherever the person pointed
 * the Save dialog, so a `.$$$-old` left behind by a crash between the two
 * moves would be there forever. The Save dialog has already asked about an
 * existing file, so delete-then-rename is what was agreed to. */
static WorkNames exportNames()
{
    return WorkNames(".$$$", "", 4, false);
}

void rc_export::cleanup(const char *why)
{
    sink.abort(why);
    total = 0;
}

int rc_export::begin(HWND owner, const char *suggested)
{
    char path[MAX_PATH];

    if (sink.active()) {
        if (html) html->error("An export is already in progress.");
        return 0;
    }
    if (!ask_for_path(owner, suggested, path, sizeof(path)))
        return 0;

    /* At the MAX_PATH boundary the temp name truncates back to exactly the
     * destination, and then every downstream step acts on the user's chosen
     * file: the open would truncate it in place before a byte arrived, and
     * every abort would delete it. Losing a pre-existing file the bridge never
     * touched is the one thing an export must not do. beginAbsolute refuses
     * rather than truncating. (audit 01.37) */
    if (!sink.beginAbsolute(path, exportNames())) {
        DWORD err = GetLastError();
        if (err == ERROR_FILENAME_EXCED_RANGE) {
            rc_log("export: %s leaves no room for a .$$$ beside it", path);
            if (html)
                html->error("That path is too long for this machine to write "
                            "a temporary file beside it. Nothing was written. "
                            "Choose a shorter path or a folder nearer the "
                            "root.");
        } else {
            rc_log("export: cannot open a temp beside %s (%lu)", path,
                   (unsigned long)err);
            if (html)
                html->error("Could not create the file. Check the drive has "
                            "room and is not write-protected.");
        }
        return 0;
    }
    total = 0;
    rc_log("export: writing to %s", sink.tempPath());
    return 1;
}

void rc_export::give_up(void)
{
    /* Called on an immediate send failure AND from the disconnect branch in
     * main.cpp, where a transfer may be part-written rather than not started
     * -- so the message says how far it got rather than asserting "before any
     * data arrived", which was only true of the first caller. A half-written
     * export is not a smaller export, it is a broken file. */
    if (sink.active())
        rc_log("export: abandoned after %lu of %lu bytes",
               (unsigned long)sink.received(), (unsigned long)total);
    cleanup("the request did not reach the bridge");
}

int rc_export::frame(rc_frame *f)
{
    if (strcmp(f->type, "EXPORTSTART") == 0) {
        const char *t = rc_frame_get(f, "total");
        if (!sink.active()) {
            /* Nothing asked for this, so nothing is open. Possible after a
             * reconnect that replayed a request, and dropping it is correct:
             * writing to a path the user has not chosen would be worse. */
            rc_log("export: EXPORTSTART with no destination open");
            return 1;
        }
        total = t ? (unsigned long)atol(t) : 0;
        recordBytes = 0;
        wroteHead = 0;
        return 1;
    }

    if (strcmp(f->type, "EXPORTDAT") == 0) {
        const char *o = rc_frame_get(f, "off");
        unsigned long off = o ? (unsigned long)atol(o) : 0;
        ExportSink out(sink);
        if (!sink.active()) return 1;

        /* THE ORDER CHECK, moved rather than lost. It used to be
         * ChunkSink's: `off` was in written bytes and the sink compared it
         * against what it had. From stage 3 C3 this side RENDERS, so the
         * file is markup and the two numbers are different lengths --
         * sink.write is given sink.received() and its check can never
         * fire. win/fs/xfer.h names that check load-bearing ("It is
         * CHECKED, not trusted... Two of the three implementations this
         * replaces did not check"), so it is here instead, against the
         * count of record bytes consumed.
         *
         * A chunk that does not continue where the last stopped means a
         * hole, and a file with a hole renders as garbage from that point
         * with nothing to say why. Refuse the whole export. */
        if (off != recordBytes) {
            rc_log("export: chunk at %lu record bytes, expected %lu -- "
                   "abandoning", off, recordBytes);
            if (html)
                html->error("The export arrived out of order and has been "
                            "discarded. Nothing was written.");
            cleanup("a chunk arrived out of order");
            return 1;
        }

        if (!wroteHead) {
            /* The file's own name for <title>, which is the browser tab
             * and nothing else. The conversation's real title is in the
             * `exportheading` record and becomes the <h1> a line later --
             * but that record has not been rendered yet when the head is
             * written, and carrying the title in an EXPORTSTART header to
             * fix the tab would be a protocol change for a tab. */
            const char *base = strrchr(sink.targetPath(), '\\');
            if (!writeHead(base ? base + 1 : sink.targetPath())) {
                if (html)
                    html->error("Writing the file failed -- the drive may "
                                "be full. Nothing was kept.");
                cleanup("the head could not be written");
                return 1;
            }
            wroteHead = 1;
        }

        /* Rendered straight into the sink: no buffer holds the markup, so
         * the streaming rule survives the move. The bridge chunks on
         * RECORD boundaries, so every byte here is whole records.
         *
         * A FIXED wrapcol, not the window's. An exported file is read
         * somewhere else, at some other width, and the one thing worse
         * than a fixed column is one that silently encodes the size of
         * the window that happened to be open. */
        if (!rc_render(f->body, f->body_len, RC_EXPORT_WRAPCOL, out)) {
            rc_log("export: a record was refused at %lu", recordBytes);
            if (html)
                html->error("The export could not be rendered and has been "
                            "discarded. Nothing was written.");
            cleanup("a record was refused");
            return 1;
        }
        if (out.failed()) {
            rc_log("export: write failed at %lu",
                   (unsigned long)sink.received());
            if (html)
                html->error("Writing the file failed -- the drive may be "
                            "full. Nothing was kept.");
            cleanup("the write failed");
            return 1;
        }
        recordBytes += f->body_len;
        return 1;
    }

    if (strcmp(f->type, "EXPORTEND") == 0) {
        const char *st = rc_frame_get(f, "status");
        const char *why = rc_frame_get(f, "reason");
        int ok = (st && strcmp(st, "ok") == 0);
        unsigned long got = (unsigned long)sink.received();
        char msg[320];
        char where[MAX_PATH];

        if (!sink.active()) return 1;

        if (!ok) {
            rc_log("export: bridge reported failure: %s", why ? why : "?");
            rc_strf(msg, sizeof(msg),
                    "The export did not finish%s%s Nothing was written.",
                    (why && *why) ? ": " : ".", (why && *why) ? why : "");
            if (html) html->error(msg);
            cleanup("the bridge reported a failure");
            return 1;
        }
        /* AGAINST RECORD BYTES, which is what `total` counts. It used to
         * compare sink.received() -- written bytes -- and those were the
         * same number only while the bridge sent the finished document.
         * Comparing them now would fail every export, since markup is
         * longer than the records it came from. */
        if (total && recordBytes != total) {
            rc_log("export: short by %lu record bytes",
                   total - recordBytes);
            if (html)
                html->error("The export finished short and has been "
                            "discarded. Nothing was written.");
            cleanup("the transfer finished short");
            return 1;
        }

        /* The old worry here was stdio: every fwrite was checked, but a full
         * floppy fails inside fclose, and that return was discarded -- so a
         * TRUNCATED file was renamed into the name the user chose, which is
         * the exact outcome the temp file exists to prevent. FileWriter uses
         * WriteFile and buffers nothing, so a full disk fails at the write
         * that fills it and is reported above. The failure mode is gone
         * rather than handled. */
        /* The document's close, before the file is committed. */
        {
            ExportSink out(sink);
            out.write(EXPORT_TAIL, (unsigned)strlen(EXPORT_TAIL));
        }
        got = (unsigned long)sink.received();
        rc_strf(where, sizeof(where), "%s", sink.tempPath());
        rc_strf(msg, sizeof(msg), "Conversation exported to %s (%lu bytes).",
                sink.targetPath(), got);

        if (sink.commit(NULL) != COMMIT_OK) {
            rc_log("export: could not put %s in place (%lu)", where,
                   (unsigned long)GetLastError());
            /* The bytes are written and verified; only the rename failed, and
             * aside=false means the sink deliberately left the temp behind
             * rather than deleting the one copy that exists. Say where it is. */
            rc_strf(msg, sizeof(msg),
                    "The conversation was written but could not be renamed. "
                    "It is at %s", where);
            if (html) html->error(msg);
            total = 0;
            return 1;
        }

        rc_log("export: %s", msg);
        if (html) html->notice(msg);
        total = 0;
        return 1;
    }

    return 0;
}
