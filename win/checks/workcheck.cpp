/* The three answers to "these files have the same name", run for real.
 *
 * The copy engine is the one piece of this program that can destroy a file
 * the user made, and its three modes differ ONLY in what happens to the two
 * copies that clash -- which is exactly the kind of difference that reads
 * correct and behaves otherwise. Reading the code does not establish it:
 * `stash` is called with `from` in one mode and `to` in the other, one
 * character apart at the call site, and swapping them would still compile,
 * still copy, and still report success while putting the wrong version live.
 *
 * So this builds real trees on a real filesystem, runs the real
 * `rc_workcopy_run`, and asserts every file's CONTENT afterwards -- on both
 * sides, including the folder that was copied FROM, because "the source is
 * left intact" is what makes a cancelled copy recoverable and nothing else
 * in the build checks it.
 *
 * Prints one line per assertion so a failure names the file and both
 * versions rather than a count. Exit code is the number of failures.
 */
#include <windows.h>
#include <stdio.h>
#include <string.h>

#include "app/workcopy.h"
#include "core/path.h"
#include "core/str.h"

static int g_fail;
static int g_checks;

/* --- a filesystem, made and unmade ---------------------------------------- */

static void put(const char *root, const char *rel, const char *text)
{
    char full[MAX_PATH];
    char dir[MAX_PATH];
    char *cut;
    HANDLE h;
    DWORD wrote;

    rc_strf(full, sizeof(full), "%s\\%s", root, rel);
    rc_strf(dir, sizeof(dir), "%s", full);
    cut = strrchr(dir, '\\');
    if (cut) { *cut = '\0'; rc_mkpath(dir); }

    h = CreateFileA(full, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                    FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) {
        printf("SETUP FAIL  cannot write %s (%lu)\n", full,
               (unsigned long)GetLastError());
        g_fail++;
        return;
    }
    WriteFile(h, text, (DWORD)strlen(text), &wrote, NULL);
    CloseHandle(h);
}

/* The file's contents, or "" when it is not there. Bounded: every file this
 * test writes is a few bytes, and a buffer that cannot overflow is worth
 * more here than the ability to read a large one. */
static const char *slurp(const char *root, const char *rel)
{
    static char buf[256];
    char full[MAX_PATH];
    HANDLE h;
    DWORD got = 0;

    buf[0] = '\0';
    rc_strf(full, sizeof(full), "%s\\%s", root, rel);
    h = CreateFileA(full, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING,
                    FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) return buf;
    ReadFile(h, buf, sizeof(buf) - 1, &got, NULL);
    CloseHandle(h);
    buf[got] = '\0';
    return buf;
}

static void wipe(const char *root)
{
    char pattern[MAX_PATH];
    char child[MAX_PATH];
    WIN32_FIND_DATAA fd;
    HANDLE h;

    rc_strf(pattern, sizeof(pattern), "%s\\*", root);
    h = FindFirstFileA(pattern, &fd);
    if (h == INVALID_HANDLE_VALUE) return;
    do {
        if (fd.cFileName[0] == '.' &&
            (fd.cFileName[1] == '\0' ||
             (fd.cFileName[1] == '.' && fd.cFileName[2] == '\0')))
            continue;
        rc_strf(child, sizeof(child), "%s\\%s", root, fd.cFileName);
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) wipe(child);
        else DeleteFileA(child);
    } while (FindNextFileA(h, &fd));
    FindClose(h);
    RemoveDirectoryA(root);
}

/* --- assertions ----------------------------------------------------------- */

static void is(const char *what, const char *root, const char *rel,
               const char *want)
{
    const char *got = slurp(root, rel);
    g_checks++;
    if (strcmp(got, want) == 0) {
        printf("  ok    %-34s %s\\%s = %s\n", what, root, rel,
               want[0] ? want : "(absent)");
        return;
    }
    printf("  FAIL  %-34s %s\\%s\n        wanted %s\n        got    %s\n",
           what, root, rel, want[0] ? want : "(absent)",
           got[0] ? got : "(absent)");
    g_fail++;
}

/* --- the fixture ---------------------------------------------------------- */

#define SRC  "workcheck.tmp\\src"

/* Named so the expected outcome is legible in the assertions below:
 * SRCONLY is only in the source, DSTONLY only in the destination, and BOTH
 * is in both with DIFFERENT content -- which is the only interesting case,
 * since identical content makes every mode indistinguishable. */
static void build_src(void)
{
    wipe(SRC);
    put(SRC, "BOTH.TXT", "from-source");
    put(SRC, "SRCONLY.TXT", "source-only");
    /* Nested, because `stash` and the copy both have to make parents inside
     * the destination -- and a collision one directory down is where an
     * off-by-one in the staged path would land silently. */
    put(SRC, "SUB\\NEST.TXT", "nested-source");
}

static void build_dst(const char *dst)
{
    wipe(dst);
    put(dst, "BOTH.TXT", "from-dest");
    put(dst, "DSTONLY.TXT", "dest-only");
    put(dst, "SUB\\NEST.TXT", "nested-dest");
}

/* Every mode agrees about these two, and if it ever stops the mode is not
 * what changed. */
static void common(const char *dst)
{
    is("brought across", dst, "SRCONLY.TXT", "source-only");
    is("left alone", dst, "DSTONLY.TXT", "dest-only");
    is("source intact", SRC, "BOTH.TXT", "from-source");
    is("source intact", SRC, "SRCONLY.TXT", "source-only");
    is("source intact", SRC, "SUB\\NEST.TXT", "nested-source");
}

static void run(const char *label, const char *dst, int mode)
{
    rc_worksurvey s;
    unsigned copied = 0, failed = 0;
    int ok;

    printf("\n=== %s ===\n", label);
    build_src();
    build_dst(dst);

    if (!rc_worksurvey_run(SRC, dst, MAX_PATH, &s)) {
        printf("  FAIL  survey refused (%d)\n", s.refusal);
        g_fail++;
        return;
    }
    g_checks++;
    if (s.collisions != 2) {
        printf("  FAIL  survey found %u collisions, wanted 2\n", s.collisions);
        g_fail++;
    } else {
        printf("  ok    survey found both collisions\n");
    }

    ok = rc_workcopy_run(SRC, dst, mode, &s, NULL, NULL, NULL,
                         &copied, &failed);
    g_checks++;
    if (!ok || failed) {
        printf("  FAIL  copy returned %d with %u failures\n", ok, failed);
        g_fail++;
    } else {
        printf("  ok    copy ran to the end (%u copied)\n", copied);
    }
}

int main(void)
{
    rc_mkpath("workcheck.tmp");

    /* KEEP: the destination's copies stay live and nothing is staged,
     * because the ones that lost are still sitting in the source folder.
     * Staging them as well would put a third copy on the disk for no reason
     * and make the folder look like a conflict happened. */
    run("keep what is already there", "workcheck.tmp\\dk", RC_WORK_KEEP);
    is("kept", "workcheck.tmp\\dk", "BOTH.TXT", "from-dest");
    is("kept, nested", "workcheck.tmp\\dk", "SUB\\NEST.TXT", "nested-dest");
    is("nothing staged", "workcheck.tmp\\dk",
       RC_WORK_INCOMING "\\BOTH.TXT", "");
    is("nothing staged", "workcheck.tmp\\dk",
       RC_WORK_REPLACED "\\BOTH.TXT", "");
    common("workcheck.tmp\\dk");

    /* REPLACE: the incoming copies win, and the ones they displaced go to
     * Gaston-Replaced -- the ONE case where a byte would otherwise be
     * destroyed with nowhere else to exist. */
    run("replace with what is brought", "workcheck.tmp\\dr", RC_WORK_REPLACE);
    is("replaced", "workcheck.tmp\\dr", "BOTH.TXT", "from-source");
    is("replaced, nested", "workcheck.tmp\\dr", "SUB\\NEST.TXT",
       "nested-source");
    is("displaced copy kept", "workcheck.tmp\\dr",
       RC_WORK_REPLACED "\\BOTH.TXT", "from-dest");
    is("displaced copy kept, nested", "workcheck.tmp\\dr",
       RC_WORK_REPLACED "\\SUB\\NEST.TXT", "nested-dest");
    is("not the other folder", "workcheck.tmp\\dr",
       RC_WORK_INCOMING "\\BOTH.TXT", "");
    common("workcheck.tmp\\dr");

    /* MERGE: the destination keeps what it has and the incoming copies go
     * where the AGENT can read them. Both versions survive; nobody picks a
     * side. Getting the two folders the wrong way round here would put the
     * incoming copy live and call it a merge. */
    run("let the assistant merge", "workcheck.tmp\\dm", RC_WORK_MERGE);
    is("destination kept live", "workcheck.tmp\\dm", "BOTH.TXT", "from-dest");
    is("destination kept live, nested", "workcheck.tmp\\dm", "SUB\\NEST.TXT",
       "nested-dest");
    is("incoming staged", "workcheck.tmp\\dm",
       RC_WORK_INCOMING "\\BOTH.TXT", "from-source");
    is("incoming staged, nested", "workcheck.tmp\\dm",
       RC_WORK_INCOMING "\\SUB\\NEST.TXT", "nested-source");
    is("not the other folder", "workcheck.tmp\\dm",
       RC_WORK_REPLACED "\\BOTH.TXT", "");
    common("workcheck.tmp\\dm");

    /* A destination several levels below anything that exists. The dialog
     * lets the user type one and says "it will be created"; this is the
     * assertion that it is. */
    printf("\n=== a destination that does not exist yet ===\n");
    {
        rc_worksurvey s;
        unsigned copied = 0, failed = 0;
        build_src();
        wipe("workcheck.tmp\\deep");
        if (!rc_worksurvey_run(SRC, "workcheck.tmp\\deep\\one\\two",
                               MAX_PATH, &s)) {
            printf("  FAIL  survey refused (%d)\n", s.refusal);
            g_fail++;
        } else {
            rc_workcopy_run(SRC, "workcheck.tmp\\deep\\one\\two",
                            RC_WORK_KEEP, &s, NULL, NULL, NULL,
                            &copied, &failed);
            is("made three levels down", "workcheck.tmp\\deep\\one\\two",
               "SRCONLY.TXT", "source-only");
            is("and the nested one", "workcheck.tmp\\deep\\one\\two",
               "SUB\\NEST.TXT", "nested-source");
        }
    }

    /* Copying a tree into its own subdirectory never terminates, so it is
     * refused before anything is enumerated rather than discovered. */
    printf("\n=== refusals the copy must make before touching anything ===\n");
    {
        rc_worksurvey s;
        char under[MAX_PATH];
        rc_strf(under, sizeof(under), "%s\\inner", SRC);
        g_checks++;
        if (rc_worksurvey_run(SRC, under, MAX_PATH, &s)
            || s.refusal != RC_WORK_NESTED) {
            printf("  FAIL  copying into a subdirectory was allowed (%d)\n",
                   s.refusal);
            g_fail++;
        } else {
            printf("  ok    into its own subdirectory: refused\n");
        }
        g_checks++;
        if (rc_worksurvey_run(SRC, "workcheck.tmp", MAX_PATH, &s)
            || s.refusal != RC_WORK_NESTED) {
            printf("  FAIL  copying into its own parent was allowed (%d)\n",
                   s.refusal);
            g_fail++;
        } else {
            printf("  ok    into its own parent: refused\n");
        }
        g_checks++;
        if (rc_worksurvey_run("workcheck.tmp\\nope", "workcheck.tmp\\x",
                              MAX_PATH, &s)
            || s.refusal != RC_WORK_NOSRC) {
            printf("  FAIL  a missing source was accepted (%d)\n", s.refusal);
            g_fail++;
        } else {
            printf("  ok    a source that is not there: refused\n");
        }
    }

    wipe("workcheck.tmp");
    printf("\n%d checks, %d failed\n", g_checks, g_fail);
    return g_fail;
}
