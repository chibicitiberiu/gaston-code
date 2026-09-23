/* SYNC* frame handling, on the RPC worker thread.
 *
 * Streaming chunks are state machine input, not jobs, which is why the router
 * in rpc.c sends them here rather than through the one-at-a-time gate. One
 * transfer is in flight at a time; a second gets SYNCPUTACK status=busy in the
 * sync vocabulary, never EXECRESULT.
 */

#include <windows.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "sync/syncrpc.h"
#include "sync/sync.h"
#include "core/log.h"
#include "core/path.h"
#include "core/str.h"
#include "core/rc_cpp.h"

/* What is mirrored RIGHT NOW -- this conversation's folder, or "" while
 * nothing is being shared. Bridge-directed; see app/workdir.h. */
/* GUARDED, because it is written on the UI thread and read on the RPC worker.
 *
 * rc_syncrpc_set_root runs from on_optmenu -> rc_workdir_apply on an
 * OPTIONSEND naming a different folder, and from try_connect on every
 * reconnect attempt -- both on the window thread. do_scan, do_put, do_putend,
 * do_mkdir and do_get all read it on the worker. An rc_strf into g_root
 * interleaved with the worker's rc_sync_join produces a path spliced from the
 * old root and the new, and the worker then CreateFileA(CREATE_ALWAYS)s,
 * DeleteFileA's and MoveFileA's at it. rpc.cpp keeps its own copy of the same
 * string and has always guarded it with g_lock; this side did not.
 *
 * Readers take a snapshot rather than holding the lock across the file I/O:
 * the value is a path, the copy is cheap, and holding a lock across a floppy
 * write is how the UI thread ends up waiting on a disk. */
static char             g_root[MAX_PATH];
static CRITICAL_SECTION g_root_lock;
static int              g_root_lock_ready = 0;

/* Idempotent, and the check-then-act is safe because of a real ordering
 * guarantee rather than by luck: the FIRST call is rc_syncrpc_set_home from
 * WinMain (app/main.cpp), long before try_connect creates any thread. The
 * later callers -- rc_syncrpc_set_root from the UI thread, root_snapshot
 * from the RPC worker -- therefore always find g_root_lock_ready already 1.
 *
 * That ordering is load-bearing. If set_home ever stops being called at
 * start-up, two threads can reach InitializeCriticalSection on the same
 * object, which is undefined rather than merely wasteful.
 *
 * Not a static constructor: those run before WinMain and therefore before the
 * log exists (win/core/rc_cpp.h). */
static void root_lock_init(void)
{
    if (!g_root_lock_ready) {
        InitializeCriticalSection(&g_root_lock);
        g_root_lock_ready = 1;
    }
}

/* A caller's own copy of the live root. Returns 1 if there is one. */
static int root_snapshot(char *out, unsigned cap)
{
    int got;
    root_lock_init();
    {
        ScopedLock lock(&g_root_lock);
        got = rc_strf(out, cap, "%s", g_root);
    }
    return got && out[0];
}

/* What this machine is CONFIGURED to share, from the INI. A fact about the
 * computer rather than about the conversation, which is why it is the one of
 * the two that goes in HELLO: the live tree is chosen by the bridge, so
 * telling the bridge about it is telling it what it already decided -- and
 * telling it a STALE one is worse than useless. A conversation folder applied
 * before a bridge restart outlives that restart here and not there, so a
 * freshly started bridge was once told the machine was mirroring
 * C:\Gaston\OK and paired that baseline against its own whole tree. */
static char g_home[MAX_PATH];

void rc_syncrpc_set_root(const char *root)
{
    root_lock_init();
    {
        ScopedLock lock(&g_root_lock);
        rc_strf(g_root, sizeof(g_root), "%s", root ? root : "");
    }
}

const char *rc_syncrpc_root(void) { return g_root; }

/* Under the SAME lock as g_root, seventeen lines above, and for the same
 * reason spelled out there: written on the UI thread when options are
 * applied, read on another thread while HELLO's headers are built. It had
 * none of that hardening. Sharing g_root_lock rather than adding a second
 * one keeps the ordering trivially free of deadlock -- there is only ever
 * one lock in this module. See docs/mem-audit F7. */
void rc_syncrpc_set_home(const char *home)
{
    root_lock_init();
    {
        ScopedLock lock(&g_root_lock);
        rc_strf(g_home, sizeof(g_home), "%s", home ? home : "");
    }
}

/* A caller's own copy, like root_snapshot. The bare-pointer accessor this
 * replaces handed out the buffer itself, so a reader could be walking it
 * while the UI thread overwrote it. */
int rc_syncrpc_home_copy(char *out, unsigned cap)
{
    int got;
    root_lock_init();
    {
        ScopedLock lock(&g_root_lock);
        got = rc_strf(out, cap, "%s", g_home);
    }
    return got && out[0];
}

/* How many files this layer has finished moving, in either direction, since
 * the process started. Monotonic and never reset: a caller that wants "how
 * many since I started watching" takes a reading first and subtracts, which
 * is the only shape that cannot be confused by two watchers.
 *
 * Written on the RPC WORKER thread and read on the UI thread, which is safe
 * without a lock for exactly one reason: it is a single aligned 32-bit word,
 * so a reader sees either the old value or the new one and never half of
 * each. The worst outcome is a progress bar one file behind. */
static volatile unsigned long g_moved;

unsigned long rc_syncrpc_moved(void) { return g_moved; }

/* --- the manifest -------------------------------------------------------- */
/*
 * Entries are length-prefixed, not TAB-separated: TAB and newline are both
 * legal in a Linux filename, and a separator-based format breaks on the first
 * one. Batches are bounded by the 64KB frame, never by a fixed entry count.
 */
typedef struct {
    rc_net *net;
    char    id[64];
    char    buf[RC_MAX_FRAME - 2048];
    unsigned len;
    int     n;
    int     total;
} manifest_ctx;

static void manifest_flush(manifest_ctx *m)
{
    const char *keys[2];
    const char *vals[2];
    char n[16];

    if (!m->len) return;
    rc_strf(n, sizeof(n), "%d", m->n);
    keys[0] = "id"; vals[0] = m->id;
    keys[1] = "n";  vals[1] = n;
    rc_net_send(m->net, "SYNCENT", keys, vals, 2, m->buf, m->len);
    m->len = 0;
    m->n = 0;
}

static void manifest_add(void *ctx, const char *rel, const rc_sync_stat *st)
{
    manifest_ctx *m = (manifest_ctx *)ctx;
    unsigned n;

    /* One producer, in sync.cpp, which synccheck --wire also uses -- so the
     * format the tests check is literally the format that goes on the wire. */
    n = rc_sync_manifest_row(m->buf + m->len, sizeof(m->buf) - m->len, rel, st);
    if (!n) {
        manifest_flush(m);
        n = rc_sync_manifest_row(m->buf + m->len, sizeof(m->buf) - m->len,
                                 rel, st);
        if (!n) return;                          /* absurdly long */
    }
    m->len += n;
    m->n++;
    m->total++;
}

static void do_scan(rc_net *net, rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *keys[5];
    const char *vals[5];
    char files[16], tree[64], sid[64];
    char root[MAX_PATH];
    const char *status = "ok";
    manifest_ctx *m;
    int n, total = 0;

    /* One snapshot for the whole scan, so the manifest, the tree id and the
     * `root=` header all describe the SAME tree even if the UI thread
     * re-points the mirror while the walk is running. */
    root_snapshot(root, sizeof(root));

    /* On the heap. manifest_ctx holds a ~62KB frame buffer, and the RPC worker
     * thread only has the image's 64KB stack commit -- so keeping it here left
     * rc_sync_stat_file's 8KB frame, called from inside the scan, to land past
     * the guard page and fault. See the long note in sync.c. */
    m = (manifest_ctx *)HeapAlloc(GetProcessHeap(), 0, sizeof(manifest_ctx));
    if (!m) {
        const char *k[2]; const char *v[2];
        k[0] = "id";     v[0] = id ? id : "?";
        k[1] = "status"; v[1] = "too_many";   /* "abort the pass", which is right */
        rc_log("sync: no memory for a manifest buffer");
        rc_net_send(net, "SYNCEND", k, v, 2, NULL, 0);
        return;
    }
    memset(m, 0, sizeof(*m));
    m->net = net;
    rc_strf(m->id, sizeof(m->id), "%s", id ? id : "");

    if (!root[0]) {
        status = "no_root";
        n = 0;
    } else {
        n = rc_sync_scan(root, manifest_add, m);
        if (n == -1)      status = "no_root";
        else if (n == -2) status = "too_many";
    }
    manifest_flush(m);
    total = m->total;
    rc_strf(sid, sizeof(sid), "%s", m->id);
    HeapFree(GetProcessHeap(), 0, m);

    /* The tree id is derived, never generated and stored: rc_config_save is
     * only ever called when the INI does not exist, so a generated id would be
     * regenerated on every launch and every reconnect would look like a
     * different machine. A volume serial is free and stable. */
    {
        DWORD serial = 0;
        char rootdir[8];
        rc_strf(rootdir, sizeof(rootdir), "%c:\\", root[0] ? root[0] : 'C');
        GetVolumeInformationA(rootdir, NULL, 0, &serial, NULL, NULL, NULL, 0);
        rc_strf(tree, sizeof(tree), "%08lX-%s",
                  (unsigned long)serial, root);
    }
    rc_strf(files, sizeof(files), "%d", total);

    /* The root travels explicitly. The bridge has to know it to apply the
     * MAX_PATH test, which is on the FULL path the guest would hold -- and
     * that test has to run bridge-side, before a push, because the guest
     * cannot create the file in order to report the problem. It was hard-coded
     * to "C:\\WORK" on the bridge, so any other root silently measured against
     * the wrong prefix. */
    keys[0] = "id";      vals[0] = sid;
    keys[1] = "status";  vals[1] = status;
    keys[2] = "files";   vals[2] = files;
    keys[3] = "tree_id"; vals[3] = tree;
    keys[4] = "root";    vals[4] = root;
    rc_net_send(net, "SYNCEND", keys, vals, 5, NULL, 0);
    rc_log("sync: scan %s -> %s, %d entries", sid, status, total);
}

/* --- receiving a file ---------------------------------------------------- */

static struct {
    char   id[64];
    char   rel[MAX_PATH];
    char   tmp[MAX_PATH];
    /* THE ROOT THIS TRANSFER BEGAN AGAINST, captured with the rest of it.
     *
     * No race is needed for this one. do_put's first chunk opens
     * <root A>\<rel>.gaston-tmp and stores the full temp path here; if the
     * work directory changes before SYNCPUTEND -- an ordinary OPTIONSEND
     * between two chunks -- do_putend used to call rc_sync_commit with the
     * LIVE root, so `full` became <root B>\<rel> and MoveFileA moved the file
     * out of tree A into tree B, or failed and left an orphaned .gaston-tmp
     * in A that the next scan offered to the bridge as a new file.
     *
     * The id, the relative path and the temp name were already captured for
     * the life of the transfer; the root was the one piece looked up fresh. */
    char   root[MAX_PATH];
    /* Where the first chunk's file bytes begin, as the parser measured it. */
    unsigned data_off;
    HANDLE h;
    int    active;
} g_put;

/* The transfer that most recently failed. A put is a STREAM -- chunks, then
 * SYNCPUTEND -- so once one chunk fails, everything else carrying that id
 * arrives at a state machine that is no longer active. Answering each of them
 * sends one SYNCPUTACK per remaining frame; the bridge's future is resolved by
 * the first and logs "SYNCPUTACK for s8, which nobody awaits" for the rest.
 * Harmless here, but it is wire traffic and log noise proportional to file
 * size, and it hides a genuine stray ack in the same message. Remember the id
 * and stay quiet for it. */
static char g_put_failed[64];

static void put_fail(rc_net *net, const char *id, const char *why)
{
    const char *keys[2];
    const char *vals[2];

    /* Already answered for this transfer; the rest of the stream is noise. */
    if (id && strcmp(g_put_failed, id) == 0) return;
    if (id) {
        rc_strf(g_put_failed, sizeof(g_put_failed), "%s", id);
    }
    rc_log("sync: put %s failed: %s", id ? id : "(none)", why);

    keys[0] = "id";     vals[0] = id;
    keys[1] = "status"; vals[1] = why;
    rc_net_send(net, "SYNCPUTACK", keys, vals, 2, NULL, 0);
    if (g_put.h != INVALID_HANDLE_VALUE && g_put.h != NULL) {
        CloseHandle(g_put.h);
        DeleteFileA(g_put.tmp);
    }
    g_put.h = INVALID_HANDLE_VALUE;
    g_put.active = 0;
}

/* A refusal AFTER do_putend has closed the handle.
 *
 * put_fail's own cleanup is keyed on `g_put.h` being live, and do_putend
 * closes that handle before the compare-and-swap check -- it has to, the
 * commit needs it closed. So from that CloseHandle onward put_fail's
 * DeleteFileA can never fire, and every refusal in the compare-and-swap
 * block used to leave a fully-written .gaston-tmp in the user's tree. That
 * is the ORDINARY path, not a corner: the whole point of if_crc is to refuse
 * a push when the user edited the file, so an edit conflict leaked one every
 * time. sync.h's own note records the same outcome arriving by a different
 * route ("the bridge's tree grew `café.md.gaston-tmp` ... and nothing ever
 * removed it").
 *
 * Keyed on the NAME, because that is the thing that still exists once the
 * handle is gone. See docs/mem-audit F1. */
static void putend_fail(rc_net *net, const char *id, const char *why)
{
    if (g_put.tmp[0]) {
        DeleteFileA(g_put.tmp);
        g_put.tmp[0] = '\0';
    }
    put_fail(net, id, why);
}

/* 01.26: g_put is exactly the kind of worker-side singleton that finding
 * named -- and looking for what would break it turned up a real bug, not the
 * hypothetical one the finding asked about. A SYNCPUT stream that is cut off
 * by a dropped connection (no SYNCPUTEND, no failing WriteFile) leaves
 * g_put.active latched at 1 forever: nothing on the reconnect path ever
 * called put_fail or do_putend for it. do_put's very first check on the next
 * transfer -- any transfer, any id, any file -- is `if (g_put.active)
 * put_fail(..., "busy")`, so one interrupted PUT wedges every PUT after the
 * reconnect for the rest of the process's life. This is the same shape as
 * 01.48 (a singleton that breaks on RECONNECT, not on a second connection).
 *
 * ONLY a flag, set here on the UI thread. g_put.h is a live HANDLE the RPC
 * worker thread can be mid-WriteFile on; closing it from here would be a new
 * race worse than the bug (this file has no lock protecting g_put, and one
 * is not worth adding for a rare event -- see g_running/g_cancel for the
 * same "cross-thread flag, no lock" idiom already shipped in rpc.c/exec.c).
 * do_put's own off==0 branch -- the only place that already runs on the
 * worker thread before touching g_put for a new transfer -- does the actual
 * cleanup the next time a PUT arrives. */
static volatile LONG g_put_aborted;

void rc_syncrpc_abort(void)
{
    g_put_aborted = 1;
}

static void do_put(rc_net *net, rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *off = rc_frame_get(f, "off");
    DWORD wrote = 0;

    if (!id) {
        /* NAMED, not dropped in silence. The bridge allocated a future for
         * this frame and will wait out its whole budget -- 120s for an exec,
         * 300s for a put or get, 600s for a scan -- with nothing anywhere
         * saying why. rpc.cpp already explains this exact cost for a WRONGLY
         * SHAPED ack and treats it as the reason lane-specific ack types
         * exist; a frame answered with nothing at all is worse and was the
         * one case nothing logged. There is no id to answer to, so a log
         * line is all that is available. (audit 01.37) */
        rc_log("%s: frame carries no id -- dropped, and the bridge will wait "
               "out its timeout", f->type);
        return;
    }

    if (off && atol(off) == 0) {
        /* First chunk: open. */
        g_put_failed[0] = '\0';     /* a new transfer, so answer again */
        if (g_put_aborted) {
            /* rc_syncrpc_abort() ran on the UI thread since the last frame
             * this worker handled -- the connection that owned the previous
             * PUT dropped mid-stream, with no SYNCPUTEND to close it out.
             * Clean up the abandoned temp file here, on the worker thread,
             * where g_put.h has always safely lived, instead of trusting the
             * "busy" check below to reject every transfer forever. */
            if (g_put.h != INVALID_HANDLE_VALUE && g_put.h != NULL) {
                CloseHandle(g_put.h);
                DeleteFileA(g_put.tmp);
            }
            g_put.h = INVALID_HANDLE_VALUE;
            g_put.active = 0;
            g_put_aborted = 0;
        }
        if (g_put.active) { put_fail(net, id, "busy"); return; }

        /* ONE parser, shared with `synccheck --putpath`. See its comment. */
        switch (rc_syncrpc_put_path(f->body, f->body_len,
                                    g_put.rel, sizeof(g_put.rel),
                                    &g_put.data_off)) {
        case RC_PUTPATH_OK:      break;
        case RC_PUTPATH_TOOLONG: put_fail(net, id, "too_long"); return;
        default:                 put_fail(net, id, "bad_path"); return;
        }
        rc_strf(g_put.id, sizeof(g_put.id), "%s", id);

        /* Captured HERE, once, and used by every later frame of this
         * transfer -- see the note on g_put.root. */
        if (!root_snapshot(g_put.root, sizeof(g_put.root))) {
            put_fail(net, id, "no_root");
            return;
        }
        g_put.h = rc_sync_begin_write(g_put.root, g_put.rel,
                                      g_put.tmp, sizeof(g_put.tmp));
        if (g_put.h == INVALID_HANDLE_VALUE) {
            /* Reporting every open failure as `readonly` sent a real
             * investigation after Win98 file permissions when the actual cause
             * was a missing parent directory -- CreateFileA returns
             * ERROR_PATH_NOT_FOUND for that, and the bridge had simply never
             * sent the SYNCMKDIR. Pass the distinction on. */
            DWORD err = GetLastError();
            const char *why = "readonly";
            if (err == ERROR_PATH_NOT_FOUND)      why = "no_parent";
            else if (err == ERROR_DISK_FULL)      why = "no_space";
            else if (err == ERROR_SHARING_VIOLATION ||
                     err == ERROR_LOCK_VIOLATION) why = "locked";
            else if (err == ERROR_FILENAME_EXCED_RANGE ||
                     err == ERROR_BUFFER_OVERFLOW) why = "too_long";
            else if (err == ERROR_INVALID_NAME)   why = "bad_path";
            rc_log("sync: cannot open %s for writing: err=%lu (%s)",
                   g_put.tmp, (unsigned long)err, why);
            put_fail(net, id, why);
            return;
        }
        g_put.active = 1;
    }
    if (!g_put.active || strcmp(g_put.id, id) != 0) {
        put_fail(net, id, "busy");
        return;
    }
    {
        /* From the parser, not recomputed: see rc_syncrpc_put_path. Only
         * the FIRST chunk carries the prefix; later chunks are all data. */
        unsigned skip = (off && atol(off) == 0) ? g_put.data_off : 0;
        if (f->body_len > skip) {
            if (!WriteFile(g_put.h, f->body + skip,
                           f->body_len - skip, &wrote, NULL)) {
                put_fail(net, id, "no_space");
            }
        }
    }
}

static void do_putend(rc_net *net, rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *crc = rc_frame_get(f, "crc");
    const char *cls = rc_frame_get(f, "cls");
    const char *ifc = rc_frame_get(f, "if_crc");
    const char *keys[3];
    const char *vals[3];
    char gotcrc[16];
    rc_sync_stat cur;
    char full[MAX_PATH];
    int rc;

    if (!id || !g_put.active || strcmp(g_put.id, id) != 0) {
        put_fail(net, id ? id : "?", "busy");
        return;
    }
    CloseHandle(g_put.h);
    g_put.h = INVALID_HANDLE_VALUE;

    /* COMPARE AND SWAP. The manifest is a snapshot taken over seconds; a save
     * landing after the scanner walked past this file would otherwise be
     * overwritten with nothing reported. Re-read the target NOW and refuse if
     * it is not what the bridge decided against. */
    if (ifc && *ifc) {
        /* A truncated join here runs the precondition against a DIFFERENT
         * file, so the guard that exists to stop a concurrent local edit being
         * overwritten would pass or fail on the wrong evidence -- and passing
         * is the direction that loses the user's save. Audit 01.61 closed this
         * class for do_get and do_mkdir and missed this third site. */
        if (!rc_sync_join(full, sizeof(full), g_put.root, g_put.rel)) {
            putend_fail(net, id, "too_long");
            return;
        }
        memset(&cur, 0, sizeof(cur));
        rc_sync_stat_file(full, &cur);
        if (strcmp(ifc, "absent") == 0) {
            /* Anything at all at the path contradicts "absent". */
            if (cur.type != 0) { putend_fail(net, id, "changed"); return; }
        } else if (cur.type == 'u') {
            putend_fail(net, id, "locked");
            return;
        } else if (cur.type != 'f') {
            /* The bridge expected a file with a specific checksum and the path
             * now holds a directory, or nothing at all because the user
             * deleted it between the scan and here. Both mean the world moved
             * under the decision, which is exactly what `changed` is for.
             * Falling through instead would commit over it. */
            putend_fail(net, id, "changed");
            return;
        } else if (RC_CRC_FOR(cur, cls && *cls ? *cls : RC_CLS_TEXT)
                   != (unsigned long)strtoul(ifc, NULL, 10)) {
            /* Compare-and-swap against the checksum the BRIDGE's class
             * selects. Using our own classification here compared the output
             * of one function against the other whenever the bridge had a
             * `-text` override for the path, so every push was refused as
             * `changed` and the file never moved again. */
            putend_fail(net, id, "changed");
            return;
        }
    }

    rc = rc_sync_commit(g_put.root, g_put.rel, g_put.tmp,
                        crc ? strtoul(crc, NULL, 10) : 0,
                        cls && *cls ? *cls : RC_CLS_TEXT);
    g_put.active = 0;
    if (rc == 0) g_moved++;          /* one file landed; see rc_syncrpc_moved */

    if (rc == -2) { put_fail(net, id, "crc_mismatch"); return; }
    /* -4 is the working-name reserve, and it is NOT `no_space`: the disk is
     * fine and retrying will never help. The bridge stops pushing the path and
     * reports it instead, which is the only useful answer. */
    if (rc == -4) { put_fail(net, id, "too_long");     return; }
    /* -1 (the just-verified temp file no longer stats as a plain file) and -3
     * (the final MoveFileA into place failed) are not disk-space problems
     * either -- both point at something else holding the path right after we
     * let go of it, which is the same "try again" shape as the file being
     * open for read in do_get. `no_space` used to be the catch-all for every
     * non-zero rc_sync_commit that was not -2 or -4, which sent the model
     * looking at free disk space for a cause that was actually a lock. */
    if (rc == -1 || rc == -3) { put_fail(net, id, "locked");    return; }
    if (rc != 0)  { put_fail(net, id, "no_space");     return; }

    /* The bridge advances its baseline from THIS crc, never from a re-scan --
     * a re-scan could pick up a write the agent made during the transfer and
     * then the next pass would pull over it. */
    rc_strf(gotcrc, sizeof(gotcrc), "%lu",
              crc ? strtoul(crc, NULL, 10) : 0UL);
    keys[0] = "id";     vals[0] = id;
    keys[1] = "status"; vals[1] = "ok";
    keys[2] = "crc";    vals[2] = gotcrc;
    rc_net_send(net, "SYNCPUTACK", keys, vals, 3, NULL, 0);
    rc_log("sync: wrote %s", g_put.rel);
}

static void do_mkdir(rc_net *net, rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *keys[2];
    const char *vals[2];
    const char *status = "ok";
    char full[MAX_PATH];
    char root[MAX_PATH];

    if (!rc_sync_contained(f->body)) status = "bad_path";
    /* rc_sync_contained checks the SHAPE of the relative path and says nothing
     * about its length, and f->body is bounded only by RC_MAX_FRAME -- 64KB,
     * not MAX_PATH. An unchecked join here writes a valid-looking path to a
     * DIFFERENT directory, which is the whole reason rc_sync_join stopped
     * returning void. The bridge's own P.check gates this for a conforming
     * bridge; rc_sync_contained's comment says why that is not the test to
     * lean on -- this function exists for the case where the bridge is the
     * thing that is wrong, and it cannot rest on a guarantee made by the party
     * it is checking. */
    /* Snapshot, not the live global: a work-directory change on the UI thread
     * mid-join splices a path out of two roots (see the note on g_root). */
    else if (!root_snapshot(root, sizeof(root)))
        status = "no_root";
    else if (!rc_sync_join(full, sizeof(full), root, f->body))
        status = "too_long";
    else {
        DWORD was = GetFileAttributesA(full);
        if (was != 0xFFFFFFFF)
            status = (was & FILE_ATTRIBUTE_DIRECTORY) ? "exists" : "is_file";
        /* PARENTS AND ALL. The bridge's `order()` puts a directory's own
         * MKDIR ahead of anything under it, so a conforming bridge never
         * needs this -- and that is exactly the guarantee this file is not
         * allowed to lean on, for the same reason rc_sync_contained does not
         * lean on the bridge checking its own paths. A missing parent used
         * to fail the whole subtree and set off the bridge's failed_dirs
         * cascade; now it is made. */
        else if (!rc_mkpath(full)) {
            DWORD e = GetLastError();
            DWORD a = GetFileAttributesA(full);
            if (e == ERROR_ALREADY_EXISTS)
                status = (a != 0xFFFFFFFF && (a & FILE_ATTRIBUTE_DIRECTORY))
                         ? "exists" : "is_file";
            else if (e == ERROR_PATH_NOT_FOUND)
                /* Not an ordering problem any more -- rc_mkpath would have
                 * made a missing parent. What is left is a root that cannot
                 * be reached at all: a drive that is not there. */
                status = "no_parent";
            else if (e == ERROR_FILENAME_EXCED_RANGE ||
                     e == ERROR_BUFFER_OVERFLOW)
                status = "too_long";
            else if (e == ERROR_DISK_FULL)
                status = "no_space";
            else
                status = "bad_path";
            rc_log("sync: mkdir %s failed: err=%lu (%s)", full,
                   (unsigned long)e, status);
        }
    }
    keys[0] = "id";     vals[0] = id ? id : "?";
    keys[1] = "status"; vals[1] = status;
    rc_net_send(net, "SYNCMKDIRACK", keys, vals, 2, NULL, 0);
}

/* --- sending a file ------------------------------------------------------ */

static void do_get(rc_net *net, rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *keys[4];
    const char *vals[4];
    char full[MAX_PATH], total[16], offs[16], crcs[16], clss[4];
    char root[MAX_PATH];
    /* Zeroed HERE, not only on the branch that reads a real file: every
     * refusal branch below (bad_path, too_long, missing, is_directory,
     * locked) falls straight through to the crc/cls _snprintf calls without
     * ever computing either, and uninitialised locals would have sent the
     * bridge whatever garbage was on the worker's stack. crc=0, cls=0 (which
     * prints as RC_CLS_TEXT below) is what the bridge has always been sent on
     * those branches; preserved exactly. */
    unsigned long crc = 0;
    char cls = 0;
    HANDLE h;
    rc_sync_scanner *scan;
    /* Heap, for the same reason as do_scan's manifest: this runs on the RPC
     * worker, which only has the image's 64KB stack commit underneath
     * whatever else is on it. rc_sync_scanner is heap-allocated internally
     * too (see sync.h) -- between the two, nothing this function needs is on
     * the stack. */
    char *buf;
    DWORD attr;
    DWORD size = 0, got = 0, sent = 0;
    const char *status = "ok";

    if (!id) {
        /* NAMED, not dropped in silence. The bridge allocated a future for
         * this frame and will wait out its whole budget -- 120s for an exec,
         * 300s for a put or get, 600s for a scan -- with nothing anywhere
         * saying why. rpc.cpp already explains this exact cost for a WRONGLY
         * SHAPED ack and treats it as the reason lane-specific ack types
         * exist; a frame answered with nothing at all is worse and was the
         * one case nothing logged. There is no id to answer to, so a log
         * line is all that is available. (audit 01.37) */
        rc_log("%s: frame carries no id -- dropped, and the bridge will wait "
               "out its timeout", f->type);
        return;
    }
    buf = (char *)HeapAlloc(GetProcessHeap(), 0, RC_SYNC_CHUNK);
    if (!buf) {
        const char *k[2]; const char *v[2];
        k[0] = "id";     v[0] = id;
        k[1] = "status"; v[1] = "locked";   /* retryable, and honest: we could
                                             * not read it right now */
        rc_log("sync: no memory for a get buffer");
        rc_net_send(net, "SYNCDATEND", k, v, 2, NULL, 0);
        return;
    }
    /* `bad_path`, matching do_put (syncrpc.c) and do_mkdir below -- not
     * `missing`. The cause is identical (rc_sync_contained refused the
     * relative path) and used to be reported as `missing` here alone, which
     * reads as "the user deleted the file" and would send the bridge down the
     * wrong branch entirely: `missing` is something a retry can never fix,
     * `bad_path` is something the bridge sent that it should not have. */
    if (!rc_sync_contained(f->body)) status = "bad_path";
    /* The same refusal as do_mkdir above, and the consequence here is worse: a
     * truncated join stats and OPENS a different existing file, and its bytes
     * go back to the bridge acked as the path that was asked for -- so the
     * bridge writes one file's contents over another's, with a crc that
     * matches what it received. */
    /* Snapshot, not the live global: a work-directory change on the UI thread
     * mid-join splices a path out of two roots (see the note on g_root). */
    else if (!root_snapshot(root, sizeof(root)))
        status = "no_root";
    else if (!rc_sync_join(full, sizeof(full), root, f->body))
        status = "too_long";
    else {
        /* What the path IS, cheaply and WITHOUT reading content -- the same
         * three outcomes rc_sync_stat_file's own attribute check gives (see
         * there for why a directory or a share-locked file must never read
         * back as `missing`), but without the full read that function does
         * afterwards: do_get is about to read the file itself, once, while
         * streaming it, and cannot afford rc_sync_stat_file's pass first. */
        attr = GetFileAttributesA(full);
        if (attr == 0xFFFFFFFF)              status = "missing";
        else if (attr & FILE_ATTRIBUTE_DIRECTORY) status = "is_directory";
        else {
            h = CreateFileA(full, GENERIC_READ,
                            FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                            OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
            if (h == INVALID_HANDLE_VALUE) status = "locked";
            else {
                scan = rc_sync_scanner_begin();
                if (!scan) {
                    /* Out of memory is not "the file is fine" here either --
                     * same answer rc_sync_scanner_begin's own comment gives. */
                    status = "locked";
                } else {
                    /* The `total` header goes out on the FIRST SYNCDAT chunk,
                     * so the size must be known before streaming starts.
                     * GetFileSize on the handle we already hold is cheap (no
                     * content read) and 9x-safe. */
                    size = GetFileSize(h, NULL);
                    rc_strf(total, sizeof(total), "%lu", (unsigned long)size);
                    while (ReadFile(h, buf, RC_SYNC_CHUNK, &got, NULL) && got) {
                        /* Classify and checksum this SAME chunk instead of
                         * reading the file a second time to do it -- the
                         * whole point of do_get having its own scan. */
                        rc_sync_scanner_feed(scan, buf, got);
                        rc_strf(offs, sizeof(offs), "%lu", (unsigned long)sent);
                        keys[0] = "id";    vals[0] = id;
                        keys[1] = "total"; vals[1] = total;
                        keys[2] = "off";   vals[2] = offs;
                        if (rc_net_send(net, "SYNCDAT", keys, vals, 3,
                                        buf, got) != 0) {
                            status = "locked";   /* the link went; stop cleanly */
                            break;
                        }
                        sent += got;
                    }
                    /* Always finish -- it frees `scan`. The crc/cls it
                     * produces on an aborted (send-failed) transfer are only
                     * ever looked at by the bridge when status == "ok", which
                     * this branch never sets, so a partial scan there is
                     * harmless. */
                    rc_sync_scanner_finish(scan, &cls, &crc);
                }
                CloseHandle(h);
            }
        }
    }

    rc_strf(crcs, sizeof(crcs), "%lu", crc);
    rc_strf(clss, sizeof(clss), "%c", cls ? cls : RC_CLS_TEXT);
    if (strcmp(status, "ok") == 0) g_moved++;   /* one file went the other way */
    keys[0] = "id";     vals[0] = id;
    keys[1] = "status"; vals[1] = status;
    keys[2] = "crc";    vals[2] = crcs;
    keys[3] = "cls";    vals[3] = clss;
    rc_net_send(net, "SYNCDATEND", keys, vals, 4, NULL, 0);
    HeapFree(GetProcessHeap(), 0, buf);
}

static rc_att_lookup_fn g_att_lookup = NULL;
static rc_att_done_fn   g_att_done = NULL;

void rc_syncrpc_set_attachment_lookup(rc_att_lookup_fn fn)
{
    g_att_lookup = fn;
}

void rc_syncrpc_set_attachment_done(rc_att_done_fn fn)
{
    g_att_done = fn;
}

static void do_attget(rc_net *net, rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *att = rc_frame_get(f, "att");
    const char *keys[4];
    const char *vals[4];
    char full[MAX_PATH], total[16], offs[16], crcs[16], clss[4];
    unsigned long crc = 0;
    char cls = 0;
    HANDLE h;
    rc_sync_scanner *scan;
    char *buf;
    DWORD attr;
    DWORD size = 0, got = 0, sent = 0;
    const char *status = "ok";

    if (!id) {
        rc_log("sync: ATTGET frame carries no id");
        return;
    }

    buf = (char *)HeapAlloc(GetProcessHeap(), 0, RC_SYNC_CHUNK);
    if (!buf) {
        const char *k[2]; const char *v[2];
        k[0] = "id";     v[0] = id;
        k[1] = "status"; v[1] = "locked";
        rc_log("sync: no memory for an attget buffer");
        rc_net_send(net, "SYNCDATEND", k, v, 2, NULL, 0);
        return;
    }

    full[0] = '\0';
    if (!att || !g_att_lookup || !g_att_lookup(att, full, sizeof(full)) || !full[0]) {
        status = "bad_path";
    } else {
        attr = GetFileAttributesA(full);
        if (attr == 0xFFFFFFFF)                   status = "missing";
        else if (attr & FILE_ATTRIBUTE_DIRECTORY) status = "is_directory";
        else {
            h = CreateFileA(full, GENERIC_READ,
                            FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                            OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
            if (h == INVALID_HANDLE_VALUE) status = "locked";
            else {
                scan = rc_sync_scanner_begin();
                if (!scan) {
                    status = "locked";
                } else {
                    size = GetFileSize(h, NULL);
                    rc_strf(total, sizeof(total), "%lu", (unsigned long)size);
                    while (ReadFile(h, buf, RC_SYNC_CHUNK, &got, NULL) && got) {
                        rc_sync_scanner_feed(scan, buf, got);
                        rc_strf(offs, sizeof(offs), "%lu", (unsigned long)sent);
                        keys[0] = "id";    vals[0] = id;
                        keys[1] = "total"; vals[1] = total;
                        keys[2] = "off";   vals[2] = offs;
                        if (rc_net_send(net, "SYNCDAT", keys, vals, 3, buf, got) != 0) {
                            status = "locked";
                            break;
                        }
                        sent += got;
                    }
                    rc_sync_scanner_finish(scan, &cls, &crc);
                }
                CloseHandle(h);
            }
        }
    }

    rc_strf(crcs, sizeof(crcs), "%lu", crc);
    rc_strf(clss, sizeof(clss), "%c", cls ? cls : RC_CLS_TEXT);
    if (strcmp(status, "ok") == 0) g_moved++;
    keys[0] = "id";     vals[0] = id;
    keys[1] = "status"; vals[1] = status;
    keys[2] = "crc";    vals[2] = crcs;
    keys[3] = "cls";    vals[3] = clss;
    rc_net_send(net, "SYNCDATEND", keys, vals, 4, NULL, 0);
    if (g_att_done && att) g_att_done(att);
    HeapFree(GetProcessHeap(), 0, buf);
}

void rc_syncrpc_handle(rc_net *net, rc_frame *f)
{
    if      (strcmp(f->type, "SYNCSCAN")   == 0) do_scan(net, f);
    else if (strcmp(f->type, "SYNCPUT")    == 0) do_put(net, f);
    else if (strcmp(f->type, "SYNCPUTEND") == 0) do_putend(net, f);
    else if (strcmp(f->type, "SYNCMKDIR")  == 0) do_mkdir(net, f);
    else if (strcmp(f->type, "SYNCGET")    == 0) do_get(net, f);
    else if (strcmp(f->type, "ATTGET")     == 0) do_attget(net, f);
    else rc_log("sync: unhandled frame %s", f->type);
}
