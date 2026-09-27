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
#include "fs/file.h"
#include "fs/path.h"
#include "fs/reader.h"
#include "fs/writer.h"

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
            caseReader, caseWalk, caseStat
        };
        unsigned i;
        for (i = 0; i < sizeof(cases) / sizeof(cases[0]); i++)
            cases[i]();
    }

    printf("%s\tsummary\t%d failures\n", g_failures ? "FAIL" : "pass",
           g_failures);
    return g_failures ? 1 : 0;
}
