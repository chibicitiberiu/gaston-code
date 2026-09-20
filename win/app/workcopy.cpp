/* workcopy.cpp -- see app/workcopy.h. */
#include <windows.h>
#include <string.h>

#include "app/workcopy.h"
#include "core/log.h"
#include "core/str.h"
#include "sync/sync.h"

/* --- shared -------------------------------------------------------------- */

/* Case-insensitive, because the filesystem is: `C:\Projects` and
 * `c:\projects` are one directory, and a containment check that says
 * otherwise is a containment check that can be walked past by holding down
 * shift. There is no portable stricmp here that the build agrees on, so it
 * is four lines. */
static int ci_prefix(const char *big, const char *small_)
{
    unsigned i;
    for (i = 0; small_[i]; i++) {
        char a = big[i], b = small_[i];
        if (a >= 'a' && a <= 'z') a = (char)(a - 32);
        if (b >= 'a' && b <= 'z') b = (char)(b - 32);
        if (a == '/') a = '\\';
        if (b == '/') b = '\\';
        if (a != b) return 0;
    }
    return 1;
}

/* Is `inner` at or below `outer`? The tail must be empty or start with a
 * separator, not merely match the prefix: C:\GASTON\CHAT1 is not inside
 * C:\GASTON\CHAT12, and a bare prefix test says it is. */
static int inside(const char *outer, const char *inner)
{
    unsigned n = (unsigned)strlen(outer);
    while (n && (outer[n - 1] == '\\' || outer[n - 1] == '/')) n--;
    if (!n) return 0;
    if (strlen(inner) < n) return 0;
    if (!ci_prefix(inner, outer)) return 0;
    return inner[n] == '\0' || inner[n] == '\\' || inner[n] == '/';
}

/* `<root>\<rel>` with the walk's '/' turned back into the separator Windows
 * wants. Non-zero if it fit. */
static int dosjoin(char *out, unsigned cap, const char *root, const char *rel)
{
    char *p;
    if (!rc_strf(out, cap, "%s\\%s", root, rel)) return 0;
    for (p = out; *p; p++)
        if (*p == '/') *p = '\\';
    return 1;
}

/* Every parent of `rel` under `root`, made in order. CreateDirectory does not
 * make parents and a copy into a tree two deep fails on the first file
 * otherwise -- with ERROR_PATH_NOT_FOUND, which reads like a missing source. */
static int make_parents(const char *root, const char *rel)
{
    char full[MAX_PATH];
    char *p;
    if (!dosjoin(full, sizeof(full), root, rel)) return 0;
    for (p = full + strlen(root) + 1; *p; p++) {
        if (*p != '\\') continue;
        *p = '\0';
        if (GetFileAttributesA(full) == 0xFFFFFFFF
            && !CreateDirectoryA(full, NULL)
            && GetLastError() != ERROR_ALREADY_EXISTS) {
            *p = '\\';
            return 0;
        }
        *p = '\\';
    }
    return 1;
}

/* --- free space ---------------------------------------------------------- */

typedef BOOL (WINAPI *PFN_FREEEX)(LPCSTR, PULARGE_INTEGER, PULARGE_INTEGER,
                                  PULARGE_INTEGER);

/* Room on the volume `path` sits on, in bytes, or 0 when it cannot be told.
 *
 * GetDiskFreeSpaceExA through GetProcAddress rather than an import: it
 * arrived in Win95 OSR2, so it is on this machine, but the standing rule is
 * that an import which cannot resolve kills the image at load with no log --
 * and a free-space check is not worth the program refusing to start. The
 * older call is the fallback and is only good to 2GB, which is why it is the
 * fallback rather than the answer. */
static unsigned long free_bytes(const char *path)
{
    char root[8];
    HMODULE k32;
    PFN_FREEEX ex;

    /* "C:\" -- the volume, not the directory. GetDiskFreeSpace wants a root. */
    if (!path || !path[0] || path[1] != ':') return 0;
    root[0] = path[0]; root[1] = ':'; root[2] = '\\'; root[3] = '\0';

    k32 = GetModuleHandleA("kernel32.dll");
    ex = k32 ? (PFN_FREEEX)GetProcAddress(k32, "GetDiskFreeSpaceExA") : NULL;
    if (ex) {
        ULARGE_INTEGER avail, total, freeb;
        if (ex(root, &avail, &total, &freeb)) {
            /* The caller's budget is a DWORD of bytes, so saturate rather
             * than wrap: a 4GB+ free volume must not come back as "nearly
             * full" because the high word was dropped. */
            if (avail.HighPart) return 0xFFFFFFFFUL;
            return avail.LowPart;
        }
    }
    {
        DWORD spc = 0, bps = 0, freec = 0, totalc = 0;
        if (GetDiskFreeSpaceA(root, &spc, &bps, &freec, &totalc)) {
            double b = (double)spc * (double)bps * (double)freec;
            if (b >= 4294967295.0) return 0xFFFFFFFFUL;
            return (unsigned long)b;
        }
    }
    return 0;
}

/* --- the survey ---------------------------------------------------------- */

namespace {

struct survey_ctx {
    rc_worksurvey *s;
    const char    *dst;
    unsigned       maxpath;
};

void survey_sink(void *ctx, const char *rel, const rc_sync_stat *st)
{
    survey_ctx *c = (survey_ctx *)ctx;
    rc_worksurvey *s = c->s;
    unsigned need;
    char full[MAX_PATH];

    /* An entry we could not read is not a reason to refuse the whole copy --
     * it is one file that will be reported when it fails, the same as a
     * locked one. Counting it keeps the totals honest. */
    if (st->type == 'd') { s->dirs++; return; }

    /* root + '\' + rel + NUL, measured from the INPUTS rather than from the
     * joined string: the join truncates silently and would hide the very
     * thing being measured. This is the check most likely to fire -- a name
     * that fits under C:\Gaston\OK need not fit under C:\Projects\Retro\Src. */
    need = (unsigned)strlen(c->dst) + 1 + (unsigned)strlen(rel) + 1;
    if (need > s->longest) {
        s->longest = need;
        rc_strcpy(s->deep, sizeof(s->deep), rel);
    }
    if (need > c->maxpath && s->refusal == RC_WORK_OK)
        s->refusal = RC_WORK_TOODEEP;

    s->files++;
    s->bytes += st->size;

    if (dosjoin(full, sizeof(full), c->dst, rel)
        && GetFileAttributesA(full) != 0xFFFFFFFF) {
        if (s->collisions < RC_WORK_SHOWN)
            rc_strcpy(s->shown[s->collisions], sizeof(s->shown[0]), rel);
        s->collisions++;
    }
}

}  /* namespace */

namespace {

struct count_ctx { unsigned files; unsigned long bytes; };

void count_sink(void *ctx, const char *rel, const rc_sync_stat *st)
{
    count_ctx *c = (count_ctx *)ctx;
    (void)rel;
    if (st->type == 'd') return;
    c->files++;
    c->bytes += st->size;
}

}  /* namespace */

int rc_worksurvey_count(const char *dir, unsigned *files, unsigned long *bytes)
{
    count_ctx c;
    int n;

    c.files = 0; c.bytes = 0;
    if (files) *files = 0;
    if (bytes) *bytes = 0;
    if (!dir || !*dir) return 0;
    n = rc_sync_scan(dir, count_sink, &c);
    if (n < 0) return 0;
    if (files) *files = c.files;
    if (bytes) *bytes = c.bytes;
    return 1;
}

int rc_worksurvey_run(const char *src, const char *dst, unsigned maxpath,
                      rc_worksurvey *out)
{
    survey_ctx c;
    int n;

    if (!out) return 0;
    memset(out, 0, sizeof(*out));
    if (!src || !*src || !dst || !*dst) { out->refusal = RC_WORK_NOSRC; return 0; }

    if (GetFileAttributesA(src) == 0xFFFFFFFF) {
        out->refusal = RC_WORK_NOSRC;
        return 0;
    }

    /* EITHER WAY ROUND. Copying a tree into its own subdirectory never
     * terminates -- the walk keeps finding what the copy has just written --
     * and copying a parent over a child is the same mistake wearing the
     * other face. Refused before anything is enumerated, because by the time
     * the walk notices it has already written into the tree it is reading. */
    if (inside(src, dst) || inside(dst, src)) {
        out->refusal = RC_WORK_NESTED;
        return 0;
    }

    if (GetFileAttributesA(dst) == 0xFFFFFFFF && !CreateDirectoryA(dst, NULL)) {
        out->refusal = RC_WORK_NODST;
        return 0;
    }

    c.s = out; c.dst = dst; c.maxpath = maxpath ? maxpath : MAX_PATH;
    n = rc_sync_scan(src, survey_sink, &c);
    if (n == -1) { out->refusal = RC_WORK_NOSRC; return 0; }
    if (n == -2) { out->refusal = RC_WORK_TOOMANY; return 0; }

    if (out->refusal == RC_WORK_OK) {
        unsigned long room = free_bytes(dst);
        /* Only when it could be told. A volume whose free space is
         * unreadable is not a volume that is full, and refusing on a failed
         * query would make the feature unavailable rather than safe. */
        if (room && out->bytes > room)
            out->refusal = RC_WORK_TOOBIG;
    }

    rc_log("workcopy: %s -> %s: %u files, %lu bytes, %u collisions, "
           "longest %u, refusal %d",
           src, dst, out->files, out->bytes, out->collisions,
           out->longest, out->refusal);
    return out->refusal == RC_WORK_OK;
}

void rc_worksurvey_why(const rc_worksurvey *s, const char *src,
                       const char *dst, char *out, unsigned cap)
{
    if (!out || !cap) return;
    out[0] = '\0';
    if (!s) return;
    switch (s->refusal) {
    case RC_WORK_OK:
        break;
    case RC_WORK_NOSRC:
        rc_strf(out, cap, "%s is not there any more, so there is nothing to "
                          "bring across.", src ? src : "The folder");
        break;
    case RC_WORK_NESTED:
        rc_strf(out, cap, "%s and %s are inside one another. Copying a folder "
                          "into itself never finishes.",
                src ? src : "?", dst ? dst : "?");
        break;
    case RC_WORK_TOODEEP:
        rc_strf(out, cap, "%s would need a path of %u characters under %s, "
                          "which is longer than this machine allows. Choose a "
                          "folder closer to the root of the drive.",
                s->deep, s->longest, dst ? dst : "?");
        break;
    case RC_WORK_TOOBIG:
        rc_strf(out, cap, "These files need %lu KB and there is not that much "
                          "room on the drive %s is on.",
                (s->bytes + 1023) / 1024, dst ? dst : "?");
        break;
    case RC_WORK_TOOMANY:
        rc_strf(out, cap, "%s holds more files than this machine will "
                          "synchronise. Point the conversation at a smaller "
                          "folder.", src ? src : "That folder");
        break;
    case RC_WORK_NODST:
        rc_strf(out, cap, "%s could not be created or written to.",
                dst ? dst : "That folder");
        break;
    default:
        rc_strf(out, cap, "The files could not be brought across.");
        break;
    }
}

/* --- the copy ------------------------------------------------------------ */

namespace {

struct copy_ctx {
    const char  *src;
    const char  *dst;
    int          mode;
    rc_workprog  prog;
    rc_workerr   onerr;
    void        *user;
    unsigned     total;
    unsigned     done;
    unsigned     copied;
    unsigned     failed;
    int          cancelled;
};

/* Put a copy that did not win under <dst>\<folder>\<rel>. Non-zero if it is
 * safely out of the way. */
int stash(copy_ctx *c, const char *folder, const char *rel, const char *from)
{
    char relstage[MAX_PATH];
    char aside[MAX_PATH];

    if (!rc_strf(relstage, sizeof(relstage), "%s\\%s", folder, rel)) return 0;
    if (!make_parents(c->dst, relstage)) return 0;
    if (!dosjoin(aside, sizeof(aside), c->dst, relstage)) return 0;
    /* CopyFile rather than MoveFile: under MERGE `from` is the incoming file
     * and is still wanted where it is, and under REPLACE the target is about
     * to be overwritten in place anyway. Copying leaves the source intact in
     * both, which is the rule the whole operation runs on. */
    return CopyFileA(from, aside, FALSE) ? 1 : 0;
}

void copy_sink(void *ctx, const char *rel, const rc_sync_stat *st)
{
    copy_ctx *c = (copy_ctx *)ctx;
    char from[MAX_PATH], to[MAX_PATH];
    int exists;

    if (c->cancelled) return;

    if (st->type == 'd') {
        if (dosjoin(to, sizeof(to), c->dst, rel))
            make_parents(c->dst, rel), CreateDirectoryA(to, NULL);
        return;
    }

    c->done++;
    if (c->prog && !c->prog(c->user, c->done, c->total, rel)) {
        c->cancelled = 1;
        return;
    }

    if (!dosjoin(from, sizeof(from), c->src, rel)
        || !dosjoin(to, sizeof(to), c->dst, rel)) {
        c->failed++;
        return;
    }

    exists = GetFileAttributesA(to) != 0xFFFFFFFF;
    if (exists) {
        if (c->mode == RC_WORK_KEEP) return;      /* still in the old folder */
        if (c->mode == RC_WORK_MERGE) {
            /* The destination keeps what it has and the incoming copy is put
             * where the AGENT CAN READ IT -- an ordinary folder inside the
             * mirror, because a hidden one the sync layer ignores would make
             * this answer do nothing at all. Nothing is destroyed on either
             * side, which is the whole point of it. */
            if (!stash(c, RC_WORK_INCOMING, rel, from)) c->failed++;
            else c->copied++;
            return;
        }
        /* REPLACE: the target's own copy would be destroyed and has nowhere
         * else to exist, so it goes aside first. */
        if (!stash(c, RC_WORK_REPLACED, rel, to)) { c->failed++; return; }
    }

    for (;;) {
        if (!make_parents(c->dst, rel)) { c->failed++; return; }
        if (CopyFileA(from, to, FALSE)) { c->copied++; return; }
        {
            DWORD err = GetLastError();
            int what = c->onerr ? c->onerr(c->user, rel, err) : RC_WORK_SKIP;
            if (what == RC_WORK_RETRY) continue;
            if (what == RC_WORK_CANCEL) { c->cancelled = 1; c->failed++; return; }
            c->failed++;
            return;
        }
    }
}

}  /* namespace */

int rc_workcopy_run(const char *src, const char *dst, int on_collision,
                    const rc_worksurvey *plan,
                    rc_workprog prog, rc_workerr onerr, void *ctx,
                    unsigned *copied, unsigned *failed)
{
    copy_ctx c;
    int n;

    if (copied) *copied = 0;
    if (failed) *failed = 0;
    if (!src || !*src || !dst || !*dst) return 0;

    c.src = src; c.dst = dst; c.mode = on_collision;
    c.prog = prog; c.onerr = onerr; c.user = ctx;
    c.total = plan ? plan->files : 0;
    c.done = c.copied = c.failed = 0;
    c.cancelled = 0;

    /* The tree is walked a SECOND time rather than remembered from the
     * survey. A list of three hundred paths is seventy-odd kilobytes and
     * this machine has twenty-eight free after a turn -- so the walk, which
     * is bounded and already written, is cheaper than the list. The cost is
     * that the two passes can disagree if something changes underneath;
     * every way they can is a per-file failure the user is asked about. */
    n = rc_sync_scan(src, copy_sink, &c);

    if (copied) *copied = c.copied;
    if (failed) *failed = c.failed;
    rc_log("workcopy: copied %u, failed %u, cancelled %d (scan %d)",
           c.copied, c.failed, c.cancelled, n);
    return !c.cancelled && n >= 0;
}
