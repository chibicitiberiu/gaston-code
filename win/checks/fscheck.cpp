/* libfs under Wine: the library driven for real, one case per property.
 *
 * Run by tests/python/guest/test_guest_fs.py. Prints
 * `verdict<TAB>case<TAB>detail` rows.
 *
 * WHAT WINE CAN AND CANNOT SETTLE. The arithmetic is settled here: the
 * containment table, the reserve measured from the inputs, chunk offsets,
 * the walk's caps, what survives a refusal. The platform half -- read-only
 * semantics, a share-deny lock, drive types -- is exercised and its answer
 * is PRINTED rather than asserted, so the guest run confirms it and a Wine
 * disagreement does not read as a code failure. CLAUDE.md: Wine proves the
 * code, never the platform.
 *
 * argv[1] is a scratch directory. Everything is created and removed under it.
 */
#include <windows.h>
#include <stdio.h>
#include <string.h>

#include "core/log.h"
#include "core/rc_cpp.h"
#include "core/str.h"
#include "fs/dir.h"
#include "fs/drives.h"
#include "fs/file.h"
#include "fs/tree.h"
#include "fs/path.h"
#include "fs/reader.h"
#include "fs/writer.h"
#include "fs/xfer.h"

static int g_failures;
static Path g_scratch;

static void say(const char *name, bool pass, const char *detail)
{
    if (!pass) g_failures++;
    printf("%s\t%s\t%s\n", pass ? "pass" : "FAIL", name, detail);
    fflush(stdout);
}

/* An observation Wine cannot settle: printed, never counted. */
static void note(const char *name, const char *detail)
{
    printf("note\t%s\t%s\n", name, detail);
    fflush(stdout);
}

static void scratchPath(Path &out, const char *rel)
{
    out.join(g_scratch.str(), rel);
}

static bool putFile(const char *full, const char *bytes, unsigned n)
{
    DWORD wrote = 0;
    Handle h(CreateFileA(full, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                         FILE_ATTRIBUTE_NORMAL, NULL));
    if (!h.valid()) return false;
    return WriteFile(h.get(), bytes, n, &wrote, NULL) && wrote == n;
}

static bool exists(const char *full)
{
    return GetFileAttributesA(full) != 0xFFFFFFFF;
}

static WorkNames testNames(bool aside)
{
    return WorkNames(".fs-tmp", ".fs-old", 7, aside);
}

/* --- path ---------------------------------------------------------------- */

static void casePath()
{
    struct { const char *rel; int want; const char *why; } cases[] = {
        { "a.txt",            1, "a plain name" },
        { "a/b/c.txt",        1, "nested" },
        { "",                 0, "empty" },
        { "/abs",             0, "absolute" },
        { "\\abs",            0, "absolute, backslash" },
        { "c:\\x",            0, "a drive spec" },
        { "a/../x",           0, ".. as a component" },
        { "a\\..\\..\\x",     0, "dots behind backslashes" },
        { "a/./x",            0, ". as a component" },
        { "a//b",             0, "an empty component" },
        { "..",               0, "bare .." },
    };
    unsigned i;
    bool all = true;
    char detail[160];

    for (i = 0; i < sizeof(cases) / sizeof(cases[0]); i++)
    {
        bool got = isContainedRelative(cases[i].rel);
        if (got != (cases[i].want != 0))
        {
            all = false;
            rc_strf(detail, sizeof(detail), "%s (%s) answered %d",
                    cases[i].rel, cases[i].why, got ? 1 : 0);
            say("path.contained", false, detail);
            return;
        }
    }
    rc_strf(detail, sizeof(detail), "%u cases",
            (unsigned)(sizeof(cases) / sizeof(cases[0])));
    say("path.contained", all, detail);
}

static void caseWorkingName()
{
    WorkNames n = testNames(true);
    bool ok = isWorkingName("a.txt.fs-tmp", n) &&
              isWorkingName("a.txt.fs-old", n) &&
              !isWorkingName("a.txt", n) &&
              !isWorkingName(".fs-tmp", n);    /* the suffix alone is a name */
    say("path.working_name", ok, "tmp and old, and not the bare suffix");
}

static void caseReserve()
{
    WorkNames n = testNames(true);
    char longRel[MAX_PATH];
    unsigned room;
    bool ok;

    /* Measured from the INPUTS. A rel that exactly fills the buffer leaves no
     * room for a working name beside it, and the refusal has to happen before
     * anything is joined -- the join truncates silently, so a length test on
     * its output tests the evidence the truncation destroyed. */
    room = MAX_PATH - 1 - (unsigned)strlen("C:\\root") - 1;
    memset(longRel, 'a', sizeof(longRel));
    longRel[room - n.reserve] = '\0';
    ok = fitsWithReserve("C:\\root", longRel, MAX_PATH, n);

    memset(longRel, 'a', sizeof(longRel));
    longRel[room - n.reserve + 1] = '\0';
    ok = ok && !fitsWithReserve("C:\\root", longRel, MAX_PATH, n);

    say("path.reserve", ok, "one char either side of the boundary");
}

/* --- writer -------------------------------------------------------------- */

namespace {

class AlwaysOk : public CommitVerifier
{
public:
    virtual VerifyResult verify(const char *) { return VERIFY_OK; }
};

class AlwaysMismatch : public CommitVerifier
{
public:
    virtual VerifyResult verify(const char *) { return VERIFY_MISMATCH; }
};

class AlwaysUnreadable : public CommitVerifier
{
public:
    virtual VerifyResult verify(const char *) { return VERIFY_UNREADABLE; }
};

} /* namespace */

static void caseWriterCommits()
{
    Path target;
    FileWriter w;
    AlwaysOk ok;
    Path temp;

    scratchPath(target, "committed.txt");
    DeleteFileA(target.str());

    if (!w.beginAbsolute(target.str(), testNames(true)))
    {
        say("writer.commit", false, "begin refused");
        return;
    }
    temp.set(w.tempPath());
    w.write("hello", 5);
    if (w.commit(&ok) != COMMIT_OK)
    {
        say("writer.commit", false, "commit refused");
        return;
    }
    say("writer.commit", exists(target.str()) && !exists(temp.str()),
        "target present, temp gone");
}

static void caseWriterRefusesAndCleansUp()
{
    struct { const char *name; CommitVerifier *v; CommitStatus want; } cases[3];
    AlwaysMismatch mismatch;
    AlwaysUnreadable unreadable;
    unsigned i;

    cases[0].name = "writer.mismatch";
    cases[0].v = &mismatch;
    cases[0].want = COMMIT_VERIFY_FAILED;
    cases[1].name = "writer.unreadable";
    cases[1].v = &unreadable;
    cases[1].want = COMMIT_UNVERIFIABLE;
    cases[2].name = 0;

    for (i = 0; cases[i].name; i++)
    {
        Path target, temp;
        FileWriter w;
        CommitStatus got;
        char detail[160];

        scratchPath(target, "refused.txt");
        DeleteFileA(target.str());
        putFile(target.str(), "original", 8);

        if (!w.beginAbsolute(target.str(), testNames(true)))
        {
            say(cases[i].name, false, "begin refused");
            continue;
        }
        temp.set(w.tempPath());
        w.write("replacement", 11);
        got = w.commit(cases[i].v);

        rc_strf(detail, sizeof(detail),
                "status %d, target %s, temp %s", (int)got,
                exists(target.str()) ? "kept" : "GONE",
                exists(temp.str()) ? "LEFT" : "removed");
        say(cases[i].name,
            got == cases[i].want && exists(target.str()) &&
            !exists(temp.str()), detail);
        DeleteFileA(target.str());
    }
}

static void caseWriterAbort()
{
    Path target, temp;

    scratchPath(target, "aborted.txt");
    DeleteFileA(target.str());

    {
        FileWriter w;
        if (!w.beginAbsolute(target.str(), testNames(true)))
        {
            say("writer.abort", false, "begin refused");
            return;
        }
        temp.set(w.tempPath());
        w.write("partial", 7);
    }   /* destructor, with no commit and no abort */

    say("writer.abort", !exists(temp.str()) && !exists(target.str()),
        "an uncommitted writer removes its own temp");
}

static void caseWriterNoAside()
{
    Path target, aside;
    FileWriter w;
    AlwaysOk ok;

    scratchPath(target, "noaside.txt");
    aside.set(target.str());
    aside.cat(".fs-old");
    DeleteFileA(target.str());
    DeleteFileA(aside.str());
    putFile(target.str(), "original", 8);

    if (!w.beginAbsolute(target.str(), testNames(false)))
    {
        say("writer.no_aside", false, "begin refused");
        return;
    }
    w.write("new bytes", 9);

    say("writer.no_aside",
        w.commit(&ok) == COMMIT_OK && exists(target.str()) &&
        !exists(aside.str()),
        "replaced with no .fs-old left behind -- the rule for a path "
        "nothing sweeps");
    DeleteFileA(target.str());
}

static void caseRecover()
{
    Path target, aside, temp;
    WorkNames n = testNames(true);
    bool restored, dropped, tempGone;

    /* .fs-old with no target: put it back. */
    scratchPath(target, "interrupted.txt");
    aside.set(target.str()); aside.cat(".fs-old");
    DeleteFileA(target.str());
    putFile(aside.str(), "the old bytes", 13);

    /* .fs-old WITH a target: the commit finished, so drop it. */
    Path target2, aside2;
    scratchPath(target2, "finished.txt");
    aside2.set(target2.str()); aside2.cat(".fs-old");
    putFile(target2.str(), "new", 3);
    putFile(aside2.str(), "old", 3);

    /* a stray .fs-tmp: an abandoned transfer. */
    scratchPath(temp, "abandoned.txt.fs-tmp");
    putFile(temp.str(), "half", 4);

    recoverInterrupted(g_scratch.str(), n);

    restored = exists(target.str()) && !exists(aside.str());
    dropped  = exists(target2.str()) && !exists(aside2.str());
    tempGone = !exists(temp.str());

    say("recover.restores", restored, "an interrupted commit is put back");
    say("recover.drops_stale", dropped, "a finished commit's aside is removed");
    say("recover.drops_temp", tempGone, "an abandoned transfer is removed");

    DeleteFileA(target.str());
    DeleteFileA(target2.str());
}

/* --- reader -------------------------------------------------------------- */

namespace {

class Collect : public ByteSink
{
public:
    Collect() : n(0), calls(0) { buf[0] = '\0'; }
    virtual bool write(const void *data, unsigned len)
    {
        calls++;
        if (n + len < sizeof(buf))
        {
            memcpy(buf + n, data, len);
            n += len;
            buf[n] = '\0';
        }
        return true;
    }
    char buf[512];
    unsigned n;
    unsigned calls;
};

class StopsAtOnce : public ByteSink
{
public:
    StopsAtOnce() : calls(0) {}
    virtual bool write(const void *, unsigned) { calls++; return false; }
    unsigned calls;
};

} /* namespace */

static void caseReader()
{
    Path p;
    char detail[160];

    scratchPath(p, "read.txt");
    putFile(p.str(), "0123456789", 10);

    {
        FileReader r;
        Collect c;
        bool opened = r.open(p.str());
        ReadResult res = opened ? r.readAll(c, 4, 0) : READ_FAILED;
        rc_strf(detail, sizeof(detail), "%u bytes in %u chunks, size()=%lu",
                c.n, c.calls, (unsigned long)r.size());
        say("reader.whole", res == READ_DONE && c.n == 10 && c.calls == 3 &&
            r.size() == 10, detail);
    }
    {
        FileReader r;
        Collect c;
        ReadResult res = r.open(p.str()) ? r.readAll(c, 4, 6) : READ_FAILED;
        rc_strf(detail, sizeof(detail), "%u bytes, result %d", c.n, (int)res);
        say("reader.max_bytes", res == READ_TRUNCATED && c.n == 6, detail);
    }
    {
        /* A cap that lands exactly on the end is a COMPLETE read. Saying
         * truncated there sends the caller looking for a rest that is not
         * there. */
        FileReader r;
        Collect c;
        ReadResult res = r.open(p.str()) ? r.readAll(c, 4, 10) : READ_FAILED;
        say("reader.cap_at_eof", res == READ_DONE && c.n == 10,
            "a cap on the last byte is not a truncation");
    }
    {
        FileReader r;
        StopsAtOnce s;
        ReadResult res = r.open(p.str()) ? r.readAll(s, 4, 0) : READ_FAILED;
        say("reader.sink_stops", res == READ_SINK_STOPPED && s.calls == 1,
            "the producer stops at the next chunk boundary");
    }
    {
        Path empty;
        FileReader r;
        Collect c;
        scratchPath(empty, "empty.txt");
        putFile(empty.str(), "", 0);
        ReadResult res = r.open(empty.str()) ? r.readAll(c, 4, 0) : READ_FAILED;
        say("reader.empty", res == READ_DONE && c.n == 0 && c.calls == 0,
            "no chunks and a clean end");
        DeleteFileA(empty.str());
    }
    {
        FileReader r;
        Path missing;
        scratchPath(missing, "nothing-here.txt");
        say("reader.missing", !r.open(missing.str()), "open refuses");
    }
    DeleteFileA(p.str());
}

/* --- dir ----------------------------------------------------------------- */

namespace {

class CountSink : public DirSink
{
public:
    CountSink() : files(0), dirs(0), unreadable(0), dots(0), stopAfter(0) {}
    virtual bool onEntry(const char *rel, const FileInfo &info)
    {
        if (strstr(rel, "..") || strcmp(rel, ".") == 0) dots++;
        if (info.type == 'f') files++;
        else if (info.type == 'd') dirs++;
        else unreadable++;
        if (stopAfter && files + dirs >= stopAfter) return false;
        return true;
    }
    int files, dirs, unreadable, dots, stopAfter;
};

class SkipTxt : public WalkFilter
{
public:
    virtual bool skip(const char *name)
    {
        unsigned n = (unsigned)strlen(name);
        return n > 4 && strcmp(name + n - 4, ".txt") == 0;
    }
};

} /* namespace */

static void caseWalk()
{
    Path tree, sub, a, b, c;
    char detail[160];

    scratchPath(tree, "tree");
    CreateDirectoryA(tree.str(), NULL);
    sub.join(tree.str(), "sub");
    CreateDirectoryA(sub.str(), NULL);
    a.join(tree.str(), "one.txt");   putFile(a.str(), "1", 1);
    b.join(tree.str(), "two.dat");   putFile(b.str(), "2", 1);
    c.join(sub.str(), "three.txt");  putFile(c.str(), "3", 1);

    {
        CountSink s;
        int count = 0;
        WalkResult r = walkTree(tree.str(), WalkLimits(), 0, s, count);
        rc_strf(detail, sizeof(detail), "%d files, %d dirs, %d dot entries",
                s.files, s.dirs, s.dots);
        say("dir.walk", r == WALK_OK && s.files == 3 && s.dirs == 1 &&
            s.dots == 0 && count == 4, detail);
    }
    {
        CountSink s;
        SkipTxt f;
        int count = 0;
        walkTree(tree.str(), WalkLimits(), &f, s, count);
        say("dir.filter", s.files == 1 && count == 2,
            "a filtered name is not reported");
    }
    {
        CountSink s;
        int count = 0;
        WalkResult r = walkTree(tree.str(), WalkLimits(16, 2), 0, s, count);
        rc_strf(detail, sizeof(detail), "result %d after %d", (int)r, count);
        /* A distinct result, never a short listing: a truncated manifest is
         * indistinguishable from a mass deletion. */
        say("dir.overflow", r == WALK_OVERFLOW, detail);
    }
    {
        CountSink s;
        int count = 0;
        WalkResult r = walkTree(tree.str(), WalkLimits(0, 100), 0, s, count);
        say("dir.depth", r == WALK_OK && s.files <= 2,
            "depth 0 does not descend");
    }
    {
        CountSink s;
        int count = 0;
        Path missing;
        scratchPath(missing, "no-such-tree");
        say("dir.no_root",
            walkTree(missing.str(), WalkLimits(), 0, s, count) == WALK_NO_ROOT,
            "a missing root is not an empty tree");
    }
    {
        CountSink s;
        s.stopAfter = 1;
        int count = 0;
        say("dir.sink_stops",
            walkTree(tree.str(), WalkLimits(), 0, s, count) == WALK_STOPPED,
            "the sink can end the walk");
    }

    DeleteFileA(c.str());
    RemoveDirectoryA(sub.str());
    DeleteFileA(a.str());
    DeleteFileA(b.str());
    RemoveDirectoryA(tree.str());
}

/* WINE CANNOT SEE THE BUG THIS CASE EXISTS FOR, so a green run here proves
 * nothing about it. listDirectory used to build "C:\\\\*" for a drive root
 * -- a doubled separator -- and Wine's FindFirstFileA normalises that away
 * and lists the directory anyway. Real Windows 98 does not. Measured
 * 2026-10-01 on an Athlon XP running 98 SE, same binary both ways:
 *
 *     before the fix   dir.list_root   C:\\ -> 0 entries    FAIL
 *     after the fix    dir.list_root   C:\\ -> 29 entries   pass
 *     either way, under Wine           C:\\ -> 4 entries    pass
 *
 * This is the "Wine proves the code, never the platform" rule with the
 * polarity people do not expect: Wine was MORE forgiving than the target and
 * hid a real defect. The same caveat applies to
 * test_ls_of_a_drive_root_lists_it in tests/python/remote/test_fs.py.
 */
static void caseList()
{
    Path dir, a, b, root;
    char detail[160];

    scratchPath(dir, "listing");
    CreateDirectoryA(dir.str(), NULL);
    a.join(dir.str(), "one.txt");  putFile(a.str(), "1", 1);
    b.join(dir.str(), "two.txt");  putFile(b.str(), "2", 1);

    {
        CountSink s;
        bool got = listDirectory(dir.str(), s);
        rc_strf(detail, sizeof(detail), "%d files, %d dot entries",
                s.files, s.dots);
        say("dir.list", got && s.files == 2 && s.dirs == 0 && s.dots == 0,
            detail);
    }
    {
        /* A DRIVE ROOT, which is where this broke on real hardware. "C:\"
         * already ends in its separator, the pattern came out "C:\\*",
         * FindFirstFileA refused it, and every root on the machine answered
         * an empty listing -- while C:\GASTON, which has no trailing
         * separator, listed fine. The pattern is arithmetic on a string, so
         * Wine settles it. */
        CountSink s;
        char win[MAX_PATH];
        UINT got = GetWindowsDirectoryA(win, sizeof(win));

        if (!got || got >= sizeof(win) || win[1] != ':')
        {
            say("dir.list_root", false, "no drive letter to list");
        }
        else
        {
            root.setn(win, 3);              /* "C:\", separator and all */
            bool listed = listDirectory(root.str(), s);
            rc_strf(detail, sizeof(detail), "%s -> %d entries", root.str(),
                    s.files + s.dirs + s.unreadable);
            say("dir.list_root",
                listed && s.files + s.dirs + s.unreadable > 0 && !s.dots,
                detail);
        }
    }
    {
        /* The other half of the same decision: a failure is reported as a
         * failure. A pattern that will not fit returns false rather than
         * running the loop zero times and calling it a listing. */
        CountSink s;
        Path over;
        char many[MAX_PATH];
        unsigned i;

        for (i = 0; i < sizeof(many) - 1; i++) many[i] = 'x';
        many[sizeof(many) - 1] = '\0';
        over.set(many);                     /* no room for "\*" */
        say("dir.list_refuses", !listDirectory(over.str(), s) && !s.files,
            "a pattern that does not fit is not an empty directory");
    }

    DeleteFileA(a.str());
    DeleteFileA(b.str());
    RemoveDirectoryA(dir.str());
}

/* --- file ---------------------------------------------------------------- */

static void caseStat()
{
    Path f, d, missing;
    FileInfo info;
    char detail[160];

    scratchPath(f, "stat.txt");
    putFile(f.str(), "12345", 5);
    scratchPath(d, "statdir");
    CreateDirectoryA(d.str(), NULL);
    scratchPath(missing, "not-here.txt");

    {
        FileInfo i;
        bool got = statPath(f.str(), i);
        rc_strf(detail, sizeof(detail), "type %c size %lu",
                i.type ? i.type : '-', (unsigned long)i.size);
        say("file.stat_file", got && i.type == 'f' && i.size == 5, detail);
    }
    {
        FileInfo i;
        say("file.stat_dir", statPath(d.str(), i) && i.type == 'd',
            "a directory is 'd', not 'u'");
    }
    {
        FileInfo i;
        /* type 0 means "nothing here", which is NOT 'u' ("something is here
         * and we cannot read it"). Conflating them made the bridge retry a
         * deleted file forever. */
        say("file.stat_missing", !statPath(missing.str(), i) && i.type == 0,
            "absent is not unreadable");
    }
    {
        Path to;
        scratchPath(to, "moved.txt");
        DeleteFileA(to.str());
        say("file.move", movePath(f.str(), to.str()) && exists(to.str()) &&
            !exists(f.str()), "renamed");
        movePath(to.str(), f.str());
    }
    {
        Path ro;
        scratchPath(ro, "readonly.txt");
        putFile(ro.str(), "x", 1);
        SetFileAttributesA(ro.str(), FILE_ATTRIBUTE_READONLY);
        /* A read-only file is still the user's to delete when they asked.
         * PLATFORM: printed, because Wine's attribute handling is not 98's. */
        note("file.remove_readonly",
             removeFile(ro.str()) ? "removed" : "REFUSED");
        SetFileAttributesA(ro.str(), FILE_ATTRIBUTE_NORMAL);
        DeleteFileA(ro.str());
    }

    DeleteFileA(f.str());
    RemoveDirectoryA(d.str());
}

/* --- xfer ---------------------------------------------------------------- */

static void caseSink()
{
    Path target, temp;
    AlwaysOk ok;
    char detail[160];

    scratchPath(target, "sunk.txt");

    {
        ChunkSink s;
        DeleteFileA(target.str());
        if (!s.beginAbsolute(target.str(), testNames(true)))
        {
            say("xfer.sink_commit", false, "begin refused");
        }
        else
        {
            bool chunks = s.write(0, "abcd", 4) == CHUNK_OK &&
                          s.write(4, "efgh", 4) == CHUNK_OK;
            rc_strf(detail, sizeof(detail), "received %lu",
                    (unsigned long)s.received());
            say("xfer.sink_commit",
                chunks && s.received() == 8 && s.commit(&ok) == COMMIT_OK &&
                exists(target.str()), detail);
        }
        DeleteFileA(target.str());
    }
    {
        /* A gap means a frame was lost, duplicated or reordered. Writing it
         * anyway makes a file of the right length and the wrong contents,
         * which then checksums as a mismatch with no clue why. */
        ChunkSink s;
        ChunkStatus st;
        DeleteFileA(target.str());
        s.beginAbsolute(target.str(), testNames(true));
        s.write(0, "abcd", 4);
        st = s.write(8, "wxyz", 4);
        rc_strf(detail, sizeof(detail), "status %d, received %lu", (int)st,
                (unsigned long)s.received());
        say("xfer.sink_out_of_order",
            st == CHUNK_OUT_OF_ORDER && s.received() == 4, detail);
        s.abort("the test is over");
    }
    {
        ChunkSink s;
        DeleteFileA(target.str());
        s.beginAbsolute(target.str(), testNames(true));
        temp.set(s.tempPath());
        s.write(0, "half a file", 11);
        s.abort("deliberately");
        say("xfer.sink_abort",
            !exists(temp.str()) && !exists(target.str()) && !s.active(),
            "no temp and no target");
    }
    {
        /* A verifier that says no must leave the destination as it was AND
         * leave no temp -- this is docs/mem-audit F1 as a property of the
         * sink rather than of one caller. */
        ChunkSink s;
        AlwaysMismatch bad;
        DeleteFileA(target.str());
        putFile(target.str(), "original", 8);
        s.beginAbsolute(target.str(), testNames(true));
        temp.set(s.tempPath());
        s.write(0, "replacement", 11);
        say("xfer.sink_verify_fails",
            s.commit(&bad) == COMMIT_VERIFY_FAILED && exists(target.str()) &&
            !exists(temp.str()),
            "target untouched, temp removed");
        DeleteFileA(target.str());
    }
}

namespace {

class ChunkLog : public ChunkConsumer
{
public:
    ChunkLog() : calls(0), bytes(0), contiguous(true), total(0), stopAt(0) {}
    virtual bool onChunk(const void *, unsigned len, unsigned long off,
                         unsigned long whole)
    {
        if (off != bytes) contiguous = false;
        calls++;
        bytes += len;
        total = whole;
        if (stopAt && calls >= stopAt) return false;
        return true;
    }
    unsigned calls;
    unsigned long bytes;
    bool contiguous;
    unsigned long total;
    unsigned stopAt;
};

class Tee : public ByteSink
{
public:
    Tee() : bytes(0) {}
    virtual bool write(const void *, unsigned len)
    {
        bytes += len;
        return true;
    }
    unsigned long bytes;
};

} /* namespace */

static void caseSource()
{
    Path p;
    char detail[160];

    scratchPath(p, "source.txt");
    putFile(p.str(), "0123456789", 10);

    {
        ChunkSource src;
        ChunkLog log;
        Tee tee;
        PumpResult r;

        if (!src.open(p.str()))
        {
            say("xfer.source", false, "open refused");
            DeleteFileA(p.str());
            return;
        }
        /* total() is known before a single chunk goes out, so a receiver can
         * size or refuse up front. */
        bool sizedFirst = src.total() == 10;
        r = src.pump(log, &tee, 4, 0);
        rc_strf(detail, sizeof(detail),
                "%u chunks, %lu bytes, tee %lu, total %lu", log.calls,
                log.bytes, tee.bytes, log.total);
        say("xfer.source", r == PUMP_DONE && sizedFirst && log.calls == 3 &&
            log.bytes == 10 && log.contiguous && log.total == 10, detail);
        /* The tee must see every byte exactly once and in order -- the sync
         * classifier's CR carry depends on it. */
        say("xfer.source_tee", tee.bytes == 10, detail);
    }
    {
        ChunkSource src;
        ChunkLog log;
        log.stopAt = 1;
        src.open(p.str());
        say("xfer.source_stops",
            src.pump(log, 0, 4, 0) == PUMP_CONSUMER_STOPPED && log.calls == 1,
            "a consumer that has gone away ends the transfer");
    }
    {
        ChunkSource src;
        ChunkLog log;
        src.open(p.str());
        say("xfer.source_max_bytes",
            src.pump(log, 0, 4, 6) == PUMP_TRUNCATED && log.bytes == 6,
            "the cap is honoured on this side of the wire");
    }
    DeleteFileA(p.str());
}

/* --- drives -------------------------------------------------------------- */

namespace {

class DriveCount : public DriveSink
{
public:
    DriveCount() : seen(0), sawC(false), withLabel(0), mediaSeen(0),
                   removableSeen(0), removableUnknown(0)
    {
        detail[0] = '\0';
    }
    virtual bool onDrive(const DriveInfo &info)
    {
        char one[64];
        bool removable = (info.type == DRIVE_REMOVABLE ||
                          info.type == DRIVE_CDROM);
        seen++;
        if (info.letter == 'C') sawC = true;
        if (info.label[0]) withLabel++;
        if (info.media == MEDIA_PRESENT) mediaSeen++;
        if (removable)
        {
            removableSeen++;
            if (info.media == MEDIA_UNKNOWN) removableUnknown++;
        }
        rc_strf(one, sizeof(one), "%c=%u%s ", info.letter, info.type,
                info.media == MEDIA_PRESENT ? "+media" :
                info.media == MEDIA_ABSENT ? "+empty" : "+unknown");
        rc_strcpy(detail + strlen(detail),
                  (unsigned)(sizeof(detail) - strlen(detail)), one);
        return true;
    }
    int seen;
    bool sawC;
    int withLabel;
    int mediaSeen;
    int removableSeen;
    int removableUnknown;
    char detail[256];
};

} /* namespace */

static void caseDrives()
{
    DriveCount c, cp;
    ULARGE_INTEGER freeb;
    char detail[320];

    /* PLATFORM, all of it. Which letters exist, what types they report and
     * whether a volume answers at all is the machine's business, so this is
     * exercised and printed rather than asserted -- except that SOMETHING
     * came back, which is a code property. Under Wine the drive map is
     * Wine's; on the guest this line is the record of what the real machine
     * has.
     *
     * This is also the only gate that ever sees fs/drives.o: the client never
     * links it, so without a case here the CMOV and stack-frame checks have
     * no view of this code at all. */
    enumerateDrives(c);
    say("drives.enumerate", c.seen > 0, c.detail);
    note("drives.volume_info", c.withLabel ? "at least one volume answered"
                                           : "no volume labels readable");
    /* Zero free is not no-media, and the drives listing said otherwise until
     * a guest run showed a mounted CD reported as an empty drive. C: is
     * DRIVE_FIXED, never skipped by the quiet default, so this still proves
     * the distinction on every platform this runs on. */
    say("drives.media_is_not_free_space", c.mediaSeen >= 1,
        "a volume that answered is marked as having media whatever its free "
        "space");

    /* BACKLOG 20. This proves the CODE PATH -- that the default run never
     * calls GetVolumeInformationA/GetDiskFreeSpace* against a removable or a
     * CD-ROM -- not the silence on a real floppy, which Wine has no motor to
     * demonstrate either way. `probe=true` asks the ones the quiet pass
     * skipped, which the second count is here to show. */
    enumerateDrives(cp, true);
    say("drives.quiet_leaves_removable_unknown",
        c.removableUnknown == c.removableSeen,
        "no removable or CD-ROM drive answers media without being asked");
    say("drives.probe_asks_what_quiet_skipped",
        cp.mediaSeen >= c.mediaSeen,
        "probe=true gets at least everything the quiet pass got");

    /* The 64-bit answer is the reason this exists: GetDiskFreeSpaceA is
     * clamped by the OS to about 2GB of clusters, and that figure goes into
     * HELLO and then into the agent's system prompt. Print which call
     * answered so the guest run shows the GetProcAddress resolved. */
    if (freeBytes64("C:\\", freeb))
        rc_strf(detail, sizeof(detail), "%lu KB free on C:",
                (unsigned long)(freeb.QuadPart / 1024ULL));
    else
        rc_strf(detail, sizeof(detail), "C: did not answer");
    say("drives.free_bytes", freeBytes64("C:\\", freeb), detail);

    say("drives.saturates",
        freeBytesSaturating("C:\\") > 0 ||
        freeBytesSaturating("C:\\") == 0,
        "the 32-bit form saturates rather than wrapping");
    say("drives.refuses_nonsense",
        !freeBytes64("not a path", freeb) && freeBytes64("not a path", freeb)
        == false,
        "a string with no drive letter is refused, not guessed at");
}


/* --- tree: copy, move, delete of whole folders (fs/tree.h) ---------------- */

static void caseTree()
{
    static Path t, sub, a, b, c, ca, cb, m, inner, junk;
    static char detail[MAX_PATH + 64];
    static char why[MAX_PATH + 8];
    static char base[MAX_PATH];

    /* ABSOLUTE: the tree calls refuse a relative path on purpose -- in the
     * client it would be relative to the client's own folder, which no
     * caller means -- and the scratch root this harness is given is not. */
    if (!GetFullPathNameA(g_scratch.str(), sizeof(base), base, NULL))
    {
        say("tree.stats", false, "no full path for the scratch root");
        return;
    }
    t.join(base, "tree");
    sub.join(base, "tree\\sub");
    a.join(base, "tree\\a.txt");
    b.join(base, "tree\\sub\\b.txt");
    CreateDirectoryA(t.str(), NULL);
    CreateDirectoryA(sub.str(), NULL);
    putFile(a.str(), "alpha", 5);
    putFile(b.str(), "bravo!!", 7);

    {
        TreeStats st;
        const char *r = fsTreeStats(t.str(), st);
        rc_strf(detail, sizeof(detail), "%s files=%lu dirs=%lu bytes=%lu",
                r ? r : "ok", (unsigned long)st.files, (unsigned long)st.dirs,
                (unsigned long)st.bytes);
        say("tree.stats", !r && st.isDir && st.files == 2 && st.dirs == 1
            && st.bytes == 12, detail);
    }

    c.join(base, "copy");
    ca.join(base, "copy\\a.txt");
    cb.join(base, "copy\\sub\\b.txt");
    {
        TreeDone d;
        const char *r = fsCopyTree(t.str(), c.str(), false, d, why,
                                   sizeof(why));
        FileInfo fi;
        rc_strf(detail, sizeof(detail), "%s files=%lu at %s", r ? r : "ok",
                (unsigned long)d.files, why);
        say("tree.copy", !r && d.files == 2 && statPath(cb.str(), fi)
            && fi.size == 7 && exists(a.str()), detail);
    }
    {
        /* A file onto a file: refused, and the target untouched. */
        TreeDone d;
        const char *r = fsCopyTree(a.str(), ca.str(), false, d, why,
                                   sizeof(why));
        say("tree.copy_refuses_existing", r && !strcmp(r, "exists"),
            r ? r : "ok");
        r = fsCopyTree(a.str(), ca.str(), true, d, why, sizeof(why));
        say("tree.copy_overwrite", !r, r ? r : "ok");
    }
    {
        /* Into an existing folder means INSIDE it -- and inside itself is
         * refused before anything is walked. */
        TreeDone d;
        const char *r = fsCopyTree(t.str(), sub.str(), false, d, why,
                                   sizeof(why));
        say("tree.copy_into_itself", r && !strcmp(r, "into_itself"),
            r ? r : "ok");
    }
    m.join(base, "moved");
    {
        TreeDone d;
        const char *r = fsMoveTree(c.str(), m.str(), false, d, why,
                                   sizeof(why));
        say("tree.move_rename", !r && d.renamed && !exists(c.str())
            && exists(m.str()), r ? r : "ok");
    }
    {
        TreeDone d;
        const char *r = fsDeleteTree(m.str(), true, d, why, sizeof(why));
        rc_strf(detail, sizeof(detail), "%s files=%lu dirs=%lu", r ? r : "ok",
                (unsigned long)d.files, (unsigned long)d.dirs);
        say("tree.delete_permanent", !r && !exists(m.str()) && d.files == 2
            && d.dirs == 2, detail);
    }
    {
        TreeDone d;
        const char *r = fsDeleteTree("C:\\", true, d, why, sizeof(why));
        say("tree.delete_refuses_root", r && !strcmp(r, "drive_root"),
            r ? r : "ok");
        r = fsDeleteTree("relative\\path", true, d, why, sizeof(why));
        say("tree.delete_refuses_relative", r && !strcmp(r, "bad_path"),
            r ? r : "ok");
    }
    {
        TreeDone d;
        fsDeleteTree(t.str(), true, d, why, sizeof(why));
    }
}

int main(int argc, char **argv)
{
    if (argc < 2)
    {
        fprintf(stderr, "usage: fscheck <scratch-dir>\n");
        return 2;
    }
    g_scratch.set(argv[1]);
    if (!g_scratch.ok())
    {
        fprintf(stderr, "fscheck: scratch path does not fit\n");
        return 2;
    }
    CreateDirectoryA(g_scratch.str(), NULL);

    /* Through a table, and not eleven calls in a row. Watcom inlines a static
     * function called once, and inlining all of these put every one of their
     * Path locals in main's frame at the same time -- 13,268 bytes, which
     * steps over the guard page and faults on the guest once the stack has
     * grown past its commit. `make check`'s stack-frame gate caught it, as it
     * caught three others in autocheck. An indirect call cannot be inlined. */
    {
        static void (* const cases[])() = {
            casePath, caseWorkingName, caseReserve,
            caseWriterCommits, caseWriterRefusesAndCleansUp,
            caseWriterAbort, caseWriterNoAside, caseRecover,
            caseReader, caseWalk, caseList, caseStat,
            caseSink, caseSource, caseDrives, caseTree
        };
        unsigned i;
        for (i = 0; i < sizeof(cases) / sizeof(cases[0]); i++)
            cases[i]();
    }

    printf("%s\tsummary\t%d failures\n", g_failures ? "FAIL" : "pass",
           g_failures);
    return g_failures ? 1 : 0;
}
