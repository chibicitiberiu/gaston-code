/* workcopy.cpp -- see app/workcopy.h. */
#include <windows.h>
#include <string.h>

#include "app/workcopy.h"
#include "core/log.h"
#include "core/path.h"
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

/* Is `inner` at or below `outer`, comparing the STRINGS as given? The tail
 * must be empty or start with a separator, not merely match the prefix:
 * C:\GASTON\CHAT1 is not inside C:\GASTON\CHAT12, and a bare prefix test says
 * it is. */
static int inside_raw(const char *outer, const char *inner)
{
    unsigned n = (unsigned)strlen(outer);
    while (n && (outer[n - 1] == '\\' || outer[n - 1] == '/')) n--;
    if (!n) return 0;
    if (strlen(inner) < n) return 0;
    if (!ci_prefix(inner, outer)) return 0;
    return inner[n] == '\0' || inner[n] == '\\' || inner[n] == '/';
}

/* GetShortPathNameA through GetProcAddress, per the standing rule -- it is on
 * every 9x with LFNs, but an import that cannot resolve kills the image at
 * load, and a nesting check is not worth the program refusing to start.
 * Returns non-zero if `out` holds a short form. */
typedef DWORD (WINAPI *PFN_SHORTPATH)(LPCSTR, LPSTR, DWORD);

static int short_form(const char *path, char *out, unsigned cap)
{
    HMODULE k32 = GetModuleHandleA("kernel32.dll");
    PFN_SHORTPATH fn = k32 ? (PFN_SHORTPATH)GetProcAddress(
                                 k32, "GetShortPathNameA") : NULL;
    DWORD n;
    if (!fn) return 0;
    n = fn(path, out, cap);
    return n > 0 && n < cap;
}

/* Is `inner` at or below `outer`, whatever either one is SPELLED as?
 *
 * The check used to be a plain string compare, and the destination is an
 * editable EDIT control -- so a user who typed `C:\GASTON~1\Copy` against a
 * current folder of `C:\Gaston Work` was told the two were unrelated. The
 * copy then wrote into the tree it was reading: `Copy/x` became
 * `C:\GASTON~1\Copy\Copy\x`, the walk descended into its own output, and the
 * destination grew `Copy\Copy\Copy...` until RC_SYNC_MAX_DEPTH or
 * RC_SYNC_MAX_FILES stopped it. Bounded, so nothing hung and nothing was
 * destroyed -- but the destination was ruined, the walk returned -2, and the
 * whole garbage tree was then offered to the bridge. A mapped drive against
 * its UNC path does the same. (audit 01.34)
 *
 * Both forms are compared: full (GetFullPathNameA resolves `.`, `..` and a
 * relative path against the current directory) and short (8.3). Either one
 * matching is a nesting.
 *
 * WHAT THIS STILL DOES NOT CATCH, stated rather than implied: SUBST and
 * network drive mappings are not resolvable on 9x -- there is no
 * QueryDosDevice -- so `X:\` SUBST'd to `C:\Gaston Work` still reads as
 * unrelated. The check is better, not complete. */
static int inside(const char *outer, const char *inner)
{
    char fo[MAX_PATH], fi[MAX_PATH], so[MAX_PATH], si[MAX_PATH];

    if (inside_raw(outer, inner)) return 1;

    if (GetFullPathNameA(outer, sizeof(fo), fo, NULL) &&
        GetFullPathNameA(inner, sizeof(fi), fi, NULL)) {
        if (inside_raw(fo, fi)) return 1;
        if (short_form(fo, so, sizeof(so)) &&
            short_form(fi, si, sizeof(si)) && inside_raw(so, si))
            return 1;
    }
    /* And the short forms of what was actually typed, for the case where the
     * long form does not exist yet (the destination is created later). */
    if (short_form(outer, so, sizeof(so)) &&
        short_form(inner, si, sizeof(si)) && inside_raw(so, si))
        return 1;
    return 0;
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

/* The directory `rel` will live in, under `root`, made if it is not there.
 *
 * `rc_mkpath` does the work; this only has to strip the last component,
 * because the caller is about to create a FILE with that name and making a
 * directory of it first would be the one mistake that cannot be undone by
 * retrying. */
static int make_parents(const char *root, const char *rel)
{
    char full[MAX_PATH];
    char *cut;
    if (!dosjoin(full, sizeof(full), root, rel)) return 0;
    cut = strrchr(full, '\\');
    if (!cut) return 1;                 /* directly in the root, which exists */
    *cut = '\0';
    return rc_mkpath(full);
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
    /* AND THE STAGING PREFIX, because a colliding file is staged under
     * <dst>\Gaston-Replaced\<rel> (or Gaston-Incoming, the same length) and
     * that is sixteen characters longer than the plain destination.
     *
     * Without this reserve the survey passed at 249 of 260 and the COPY then
     * failed per file: dosjoin needed 265, stash returned 0, and the file was
     * counted `failed` with no on_error call, no retry offer and no
     * explanation. workcopy.h promises "every refusal is made while nothing
     * has changed, so a refusal never leaves half a tree behind" -- that one
     * did. The sync layer solves the identical problem with
     * RC_SYNC_WORK_SUFFIX, computed up front and declared to the bridge; this
     * is the same reserve for the same reason. (audit 01.31)
     *
     * Charged unconditionally rather than only when a collision is possible:
     * whether a given file collides is not known until the destination is
     * walked, and a reserve that depends on the answer is a reserve that is
     * missing exactly when it is needed. Sixteen characters of MAX_PATH is a
     * cheaper price than a half-copied tree. */
    need += (unsigned)sizeof(RC_WORK_REPLACED);   /* name + separator */
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

    /* Parents and all: the user can type or browse to a folder several
     * levels below anything that exists, and refusing that would make the
     * dialog's own Browse button able to propose paths it then rejects.
     *
     * AND THIS IS THE ONE THING THE SURVEY CHANGES, against its own header's
     * "Look, and change nothing." Creating the destination is how the survey
     * learns whether it CAN be created, and that answer is what RC_WORK_NODST
     * reports -- there is no non-destructive way to ask Win32 the same
     * question. So the guarantee is narrowed rather than pretended: nothing
     * in the SOURCE is touched, and the only thing that can appear on disk is
     * the empty destination the user typed. A later TOODEEP or TOOBIG refusal
     * can therefore leave an empty tree behind, which is stated here so the
     * rest of the module does not reason from a guarantee that is not
     * whole. (audit 01.37) */
    if (!rc_mkpath(dst)) {
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
     * both, which is the rule the whole operation runs on.
     *
     * FAIL-IF-EXISTS, and then a generation suffix.
     *
     * The third argument was FALSE -- overwrite if present -- in the one
     * folder whose entire job is holding "what was overwritten and would
     * otherwise be gone" (workcopy.h). So a second REPLACE into the same
     * destination overwrote the FIRST one's stash: change into C:\Work with
     * REPLACE, change away, change back with REPLACE again, and
     * C:\Work\Gaston-Replaced\notes.txt -- the only surviving copy of the
     * original -- was destroyed with nothing reported. The mechanism meant to
     * preserve the user's bytes was the thing deleting them, on a path they
     * chose in order to keep them.
     *
     * Same shape as the bridge's own conflict staging, which versions
     * mine.1, mine.2 for exactly this reason. */
    if (CopyFileA(from, aside, TRUE)) return 1;
    if (GetLastError() != ERROR_FILE_EXISTS) return 0;
    {
        char gen[MAX_PATH];
        int n;
        for (n = 1; n <= 99; n++) {
            if (!rc_strf(gen, sizeof(gen), "%s.%d", aside, n)) return 0;
            if (CopyFileA(from, gen, TRUE)) return 1;
            if (GetLastError() != ERROR_FILE_EXISTS) return 0;
        }
    }
    /* Ninety-nine generations of the same path already stashed. Refusing is
     * right: :374's caller treats a 0 here as "do not overwrite the live
     * file", which is the safe direction. */
    rc_log("workcopy: %s has 99 stashed generations; refusing to add another",
           rel);
    return 0;
}

void copy_sink(void *ctx, const char *rel, const rc_sync_stat *st)
{
    copy_ctx *c = (copy_ctx *)ctx;
    char from[MAX_PATH], to[MAX_PATH];
    int exists;

    if (c->cancelled) return;

    if (st->type == 'd') {
        /* The directory itself, parents included -- the walk yields parents
         * first, but leaning on that would make this depend on an ordering
         * it does not control. */
        if (dosjoin(to, sizeof(to), c->dst, rel))
            rc_mkpath(to);
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
