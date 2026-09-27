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
        return 1;
    }

    if (strcmp(f->type, "EXPORTDAT") == 0) {
        const char *o = rc_frame_get(f, "off");
        unsigned long off = o ? (unsigned long)atol(o) : 0;
        if (!sink.active()) return 1;

        /* `off` is in DECOMPRESSED bytes, which is what has been written. */
        switch (sink.write(off, f->body, f->body_len)) {
        case CHUNK_OK:
            break;
        case CHUNK_OUT_OF_ORDER:
            /* A chunk that does not continue where the last one stopped means
             * a hole, and a file with a hole in it renders as garbage from
             * that point on with nothing to say why. Refuse the whole export
             * instead. */
            rc_log("export: chunk at %lu, expected %lu -- abandoning", off,
                   (unsigned long)sink.received());
            if (html)
                html->error("The export arrived out of order and has been "
                            "discarded. Nothing was written.");
            cleanup("a chunk arrived out of order");
            break;
        default:
            rc_log("export: write failed at %lu",
                   (unsigned long)sink.received());
            if (html)
                html->error("Writing the file failed -- the drive may be "
                            "full. Nothing was kept.");
            cleanup("the write failed");
            break;
        }
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
        if (total && got != total) {
            rc_log("export: short by %lu bytes", total - got);
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
