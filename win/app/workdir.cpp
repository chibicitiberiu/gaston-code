/* workdir.cpp -- the conversation's work directory. See app/workdir.h. */
#include <windows.h>
#include <objbase.h>
#include <shlobj.h>
#include <string.h>

#include "app/workdir.h"
#include "core/log.h"
#include "core/str.h"
#include "rpc/rpc.h"
#include "sync/sync.h"
#include "sync/syncrpc.h"

/* What is mirrored right now. "" means file sharing is off, which is a real
 * state rather than an error: a conversation shares nothing until it has
 * committed to a folder, which is the moment its first tool runs. */
static char g_current[MAX_PATH];

const char *rc_workdir_current(void) { return g_current; }

int rc_workdir_apply(const char *path)
{
    char want[MAX_PATH];

    rc_strf(want, sizeof(want), "%s", path ? path : "");
    if (strcmp(want, g_current) == 0)
        return want[0] ? 1 : 0;     /* already in force; nothing to do */

    if (!want[0]) {
        /* NOTHING IS MIRRORED YET, which is the normal state of a chat that
         * is only being talked to. Sharing begins when a conversation commits
         * to a folder and not before -- so a browse, a question or a whole
         * conversation with no tool call in it never touches the user's disk,
         * and the two sides cannot disagree about a tree that does not exist.
         *
         * Told to both layers, because a stale root left in either one keeps
         * a watcher on a tree nobody is mirroring. */
        rc_syncrpc_set_root("");
        rc_rpc_set_sync_root("");
        g_current[0] = '\0';
        rc_log("workdir: file sharing is off");
        return 0;
    }

    /* CREATED if it is not there. A work directory is a place that was named,
     * not one anybody is asserting already exists -- the first message in a
     * conversation names a folder that has never existed, and leaving it
     * missing leaves synchronisation dead with "guest scan returned no_root",
     * which is a sentence nobody should have to decode to find out a
     * directory needed making.
     *
     * One level only: CreateDirectory does not make parents, and a missing
     * drive is a different problem that deserves to be reported rather than
     * papered over. */
    if (GetFileAttributesA(want) == 0xFFFFFFFF
        && !CreateDirectoryA(want, NULL)) {
        rc_log("workdir: cannot create %s (%lu)", want,
               (unsigned long)GetLastError());
        return 0;
    }

    /* ORDER MATTERS. recover() puts back anything an interrupted commit left
     * aside, and it has to happen BEFORE the first scan of the new tree --
     * otherwise that scan sees a file missing and reports a deletion nobody
     * made. So: recover, then tell the two layers, then let the watcher
     * follow (rc_rpc_set_sync_root wakes the worker, which re-points it). */
    rc_sync_recover(want);
    rc_syncrpc_set_root(want);
    rc_rpc_set_sync_root(want);
    rc_strf(g_current, sizeof(g_current), "%s", want);
    rc_log("workdir: mirroring %s", g_current);
    return 1;
}

/* --- the folder browser -------------------------------------------------- */

typedef LPITEMIDLIST (WINAPI *PFN_BROWSE)(LPBROWSEINFOA);
typedef BOOL (WINAPI *PFN_GETPATH)(LPCITEMIDLIST, LPSTR);

/* BFFM_INITIALIZED / BFFM_SETSELECTIONA, which the Watcom headers have but
 * which are worth naming here anyway: the callback is the only way to open
 * the browser ON the current folder, and a browser that always opens at the
 * desktop makes the user walk down the tree every time. */
static int CALLBACK browse_init(HWND dlg, UINT msg, LPARAM lp, LPARAM data)
{
    (void)lp;
    if (msg == BFFM_INITIALIZED && data)
        SendMessageA(dlg, BFFM_SETSELECTIONA, TRUE, data);
    return 0;
}

int rc_workdir_browse(HWND owner, const char *start, char *out, unsigned cap)
{
    HMODULE dll;
    PFN_BROWSE browse;
    PFN_GETPATH getpath;
    BROWSEINFOA bi;
    LPITEMIDLIST pidl;
    char display[MAX_PATH];
    char picked[MAX_PATH];
    int ok = 0;

    if (!out || cap < 2) return 0;

    /* LoadLibrary rather than an import: shell32 is on every Windows this
     * targets, so this is not the NT-only rule -- it is the same habit for
     * the same reason. An import that cannot resolve kills the image at load,
     * before WinMain, with no log written, and a folder picker is not worth
     * the program refusing to start on a stripped install. */
    dll = LoadLibraryA("shell32.dll");
    if (!dll) {
        rc_log("workdir: shell32 not available (%lu)",
               (unsigned long)GetLastError());
        return 0;
    }
    browse = (PFN_BROWSE)GetProcAddress(dll, "SHBrowseForFolderA");
    getpath = (PFN_GETPATH)GetProcAddress(dll, "SHGetPathFromIDListA");
    if (!browse || !getpath) {
        rc_log("workdir: no SHBrowseForFolderA in shell32");
        FreeLibrary(dll);
        return 0;
    }

    memset(&bi, 0, sizeof(bi));
    bi.hwndOwner = owner;
    bi.pszDisplayName = display;
    bi.lpszTitle = "Choose the folder this conversation works in.\n"
                   "Files the assistant writes appear there, and files you "
                   "put there reach it.";
    /* BIF_RETURNONLYFSDIRS: My Computer and Control Panel are folders to the
     * shell and not to a filesystem, and a mirror rooted at one of them is
     * not a thing.
     *
     * BIF_NEWDIALOGSTYLE is deliberately NOT set, which costs the "Make New
     * Folder" button: it needs shell32 5.0, and this guest is not the only
     * target. The folder is created for you anyway when the conversation
     * names one, and a typed path was never the point -- see rc_workdir_apply.
     */
    bi.ulFlags = BIF_RETURNONLYFSDIRS;
    if (start && *start) {
        bi.lpfn = browse_init;
        bi.lParam = (LPARAM)start;
    }

    pidl = browse(&bi);
    if (pidl) {
        if (getpath(pidl, picked) && picked[0]) {
            rc_strf(out, cap, "%s", picked);
            ok = 1;
        }
        /* The shell allocated it with the task allocator, so this is how it
         * goes back. ole32 is already linked for MSHTML. */
        CoTaskMemFree(pidl);
    }
    FreeLibrary(dll);
    return ok;
}
