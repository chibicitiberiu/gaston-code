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
    HMODULE dll;
    PFN_GETSAVEFILENAMEA fn;
    OPENFILENAMEA ofn;
    char name[MAX_PATH];
    int ok;

    rc_strf(name, sizeof(name), "%s",
            (suggested && *suggested) ? suggested : "CHAT.HTM");

    dll = LoadLibraryA("comdlg32.dll");
    if (!dll) {
        rc_log("export: comdlg32 not available (%lu)",
               (unsigned long)GetLastError());
        return 0;
    }
    fn = (PFN_GETSAVEFILENAMEA)GetProcAddress(dll, "GetSaveFileNameA");
    if (!fn) {
        rc_log("export: GetSaveFileNameA not found");
        FreeLibrary(dll);
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
        PFN_ERR err = (PFN_ERR)GetProcAddress(dll, "CommDlgExtendedError");
        rc_log("export: no path chosen (ofn=%u, err=%lu)",
               (unsigned)sizeof(ofn),
               err ? (unsigned long)err() : 0UL);
    }
    FreeLibrary(dll);
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
    /* A transfer still open at shutdown is an abandoned one: close the handle
     * and take the temp file with it rather than leaving a half-written
     * .$$$ beside a file the user never got. */
    cleanup();
}

void rc_export::cleanup(void)
{
    if (fp) { fclose(fp); fp = NULL; }
    if (tmp[0]) { DeleteFileA(tmp); tmp[0] = '\0'; }
    dest[0] = '\0';
    got = 0;
    total = 0;
}

int rc_export::begin(HWND owner, const char *suggested)
{
    char path[MAX_PATH];

    if (fp) {
        if (html) html->error("An export is already in progress.");
        return 0;
    }
    if (!ask_for_path(owner, suggested, path, sizeof(path)))
        return 0;

    /* The temp file sits beside the destination, not in %TEMP%: the rename at
     * the end has to be within one volume to be atomic, and the user may well
     * have chosen a floppy or a network drive. */
    /* CHECKED, because at the MAX_PATH boundary the format truncates back to
     * exactly `path` and then tmp == dest. Everything downstream assumes they
     * are different files: fopen(tmp, "wb") would truncate the user's chosen
     * file in place before a byte had arrived, EXPORTEND's DeleteFileA(dest)
     * would delete what had just been written, and every mid-transfer
     * cleanup() does DeleteFileA(tmp) -- i.e. deletes dest. The outcome is
     * losing a pre-existing file the bridge never touched, which is the one
     * thing an export must not do. (audit 01.37) */
    if (!rc_strf(tmp, sizeof(tmp), "%s.$$$", path)) {
        rc_log("export: %s leaves no room for a .$$$ beside it",
               path);
        if (html)
            html->error("That path is too long for this machine to write a "
                        "temporary file beside it. Nothing was written. "
                        "Choose a shorter path or a folder nearer the root.");
        tmp[0] = '\0';
        return 0;
    }
    fp = fopen(tmp, "wb");
    if (!fp) {
        rc_log("export: cannot open %s", tmp);
        if (html)
            html->error("Could not create the file. Check the drive has room "
                        "and is not write-protected.");
        tmp[0] = '\0';
        return 0;
    }
    rc_strf(dest, sizeof(dest), "%s", path);
    got = 0;
    total = 0;
    rc_log("export: writing to %s", tmp);
    return 1;
}

void rc_export::give_up(void)
{
    /* Called on an immediate send failure AND from the disconnect branch in
     * main.cpp, where a transfer may be part-written rather than not started
     * -- so the message says how far it got rather than asserting "before any
     * data arrived", which was only true of the first caller. cleanup()
     * closes the FILE* and removes the .$$$ either way: a half-written export
     * is not a smaller export, it is a broken file. */
    if (fp)
        rc_log("export: abandoned after %lu of %lu bytes",
               (unsigned long)got, (unsigned long)total);
    cleanup();
}

int rc_export::frame(rc_frame *f)
{
    if (strcmp(f->type, "EXPORTSTART") == 0) {
        const char *t = rc_frame_get(f, "total");
        if (!fp) {
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
        if (!fp) return 1;

        /* A chunk that does not continue where the last one stopped means a
         * hole, and a file with a hole in it renders as garbage from that
         * point on with nothing to say why. Refuse the whole export instead.
         * `off` is in DECOMPRESSED bytes, which is what has been written. */
        if (off != got) {
            rc_log("export: chunk at %lu, expected %lu -- abandoning",
                   off, got);
            if (html)
                html->error("The export arrived out of order and has been "
                            "discarded. Nothing was written.");
            cleanup();
            return 1;
        }
        if (f->body_len
            && fwrite(f->body, 1, f->body_len, fp) != f->body_len) {
            rc_log("export: write failed at %lu", got);
            if (html)
                html->error("Writing the file failed -- the drive may be "
                            "full. Nothing was kept.");
            cleanup();
            return 1;
        }
        got += f->body_len;
        return 1;
    }

    if (strcmp(f->type, "EXPORTEND") == 0) {
        const char *st = rc_frame_get(f, "status");
        const char *why = rc_frame_get(f, "reason");
        int ok = (st && strcmp(st, "ok") == 0);
        char msg[320];

        if (!fp) return 1;
        /* CHECKED, because stdio buffers and this is where the last buffer
         * actually reaches the disk.
         *
         * Every fwrite above is checked, but a full floppy or network drive
         * fails inside fclose, which returns EOF -- and that return was
         * discarded. `got == total` still held, because it counts bytes
         * handed to fwrite rather than bytes on disk, so a TRUNCATED file was
         * renamed into the name the user chose. export.h names that exact
         * outcome as the thing the temp file exists to prevent: "they would
         * open it, see most of a conversation, and have no way to tell that
         * the end is missing." */
        {
            int closed = fclose(fp);
            fp = NULL;
            if (closed != 0) {
                rc_log("export: fclose failed after %lu bytes -- out of disk?",
                       (unsigned long)got);
                if (html)
                    html->error("The export could not be written to disk and "
                                "has been discarded. Nothing was written.");
                cleanup();
                return 1;
            }
        }

        if (!ok) {
            rc_log("export: bridge reported failure: %s", why ? why : "?");
            rc_strf(msg, sizeof(msg),
                    "The export did not finish%s%s Nothing was written.",
                    (why && *why) ? ": " : ".", (why && *why) ? why : "");
            if (html) html->error(msg);
            cleanup();
            return 1;
        }
        if (total && got != total) {
            rc_log("export: short by %lu bytes", total - got);
            if (html)
                html->error("The export finished short and has been "
                            "discarded. Nothing was written.");
            cleanup();
            return 1;
        }

        /* MoveFileA refuses to overwrite, and the Save dialog has already
         * asked about an existing file -- so delete first and accept that a
         * failure here leaves the temp file, which is named after the
         * destination and findable. */
        DeleteFileA(dest);
        if (!MoveFileA(tmp, dest)) {
            rc_log("export: rename %s -> %s failed (%lu)", tmp, dest,
                   (unsigned long)GetLastError());
            rc_strf(msg, sizeof(msg),
                    "The conversation was written but could not be renamed. "
                    "It is at %s", tmp);
            if (html) html->error(msg);
            /* NOT cleanup(): that would delete the file this just told the
             * user where to find. */
            fp = NULL; tmp[0] = '\0'; dest[0] = '\0'; got = 0; total = 0;
            return 1;
        }
        rc_strf(msg, sizeof(msg), "Conversation exported to %s (%lu bytes).",
                dest, got);
        rc_log("export: %s", msg);
        if (html) html->notice(msg);
        tmp[0] = '\0';
        cleanup();
        return 1;
    }

    return 0;
}
