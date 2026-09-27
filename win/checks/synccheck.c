/* Cross-checks the guest's classifier against the bridge's.
 *
 * The two sides each compute a class and a checksum, and if they ever disagree
 * about which function to apply they compare the outputs of different functions
 * FOREVER -- the file never converges and every pass rewrites it. That makes
 * "do these two implementations agree" the single most important property in
 * the sync layer, and it cannot be established by reading both.
 *
 * So: this walks a directory and prints one line per file. tests/python runs it
 * under Wine over a generated corpus and diffs the output against
 * bridge/files/paths.py. Same corpus, same answers, or the build is wrong.
 */
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include <io.h>
#include <fcntl.h>
#include "sync/sync.h"
#include "core/str.h"


static void emit(void *ctx, const char *rel, const rc_sync_stat *st)
{
    (void)ctx;
    /* Both checksums, because both now cross the wire and either can be the
     * one a path is compared under -- `.gitattributes` decides, not the
     * content. A raw checksum that is right only for the files the classifier
     * already called byte-exact would pass every test here and still make
     * every `-text` override a permanent disagreement. */
    printf("%c\t%c\t%lu\t%lu\t%lu\t%lu\t%s\n",
           st->type, st->cls ? st->cls : '-',
           (unsigned long)st->size, st->crc, st->crc_norm, st->crc_raw, rel);
}

/* The working-name reserve, asserted against the real rc_sync_begin_write.
 *
 * This cannot be checked by walking a directory: it needs a path longer than
 * the filesystem will let anyone create, and Wine does not enforce MAX_PATH
 * the way Win98 does anyway. But the refusal is computed from the LENGTHS of
 * root and rel before anything is opened, so calling it with a long `rel` and
 * a root that does not exist tests exactly the branch that matters -- no file
 * is ever created on the accepted side either, because the parent is missing.
 *
 * Prints one line per length so the Python side can diff the boundary against
 * bridge/caps.py rather than trusting a number written twice.
 */
static int limits(void)
{
    static char rel[512];
    char tmp[MAX_PATH];
    const char *root = "C:\\rc-nonexistent";
    unsigned n;
    HANDLE h;

    for (n = 230; n <= 260; n++) {
        memset(rel, 'd', n);
        rel[n] = '\0';
        SetLastError(0);
        h = rc_sync_begin_write(root, rel, tmp, sizeof(tmp));
        /* `refused` means we declined on length. Anything else is the open
         * failing for an ordinary reason (the root does not exist), which is
         * the ACCEPTED answer for this test -- we only care where the length
         * rule draws its line. */
        printf("%u\t%s\t%lu\n", (unsigned)(strlen(root) + 1 + n),
               (h == INVALID_HANDLE_VALUE &&
                GetLastError() == ERROR_FILENAME_EXCED_RANGE)
                   ? "refused" : "allowed",
               (unsigned long)strlen(tmp));
        if (h != INVALID_HANDLE_VALUE) CloseHandle(h);
    }
    return 0;
}

/* Containment, asserted against the real rc_sync_contained.
 *
 * This cannot be checked by walking a directory either: the inputs are paths
 * a correct bridge never sends, and the whole point of the function is what
 * happens when the bridge is the thing that is wrong. Prints one line per
 * case so the Python side diffs the verdicts rather than trusting a comment.
 */

/* The do_put path parse, over bodies shaped like the ones the bridge really
 * sends: "<len> <rel>" with the file's own first chunk glued straight on.
 *
 * This is the composition nothing tested. rc_sync_contained was covered by
 * --contained above with bare paths, and it was correct; the bug was that
 * do_put handed it a pointer running into the file CONTENT, so any push whose
 * first chunk held `//` was refused forever as bad_path. `fakegui.py` answers
 * SYNCPUT with a canned status without parsing the body, so nothing on the
 * Python side could reach it either.
 *
 * Prints "<verdict>\t<path-or-empty>\t<data-offset>\t<label>". */
static int putpath(void)
{
    static const struct { const char *label; const char *path;
                          const char *data; } cases[] = {
        /* The finding: ordinary source files whose first bytes hold "//". */
        { "cpp-line-comment",  "src/main.cpp", "// a C++ comment\n" },
        { "js-spdx",           "web/app.js",   "// SPDX-License-Identifier\n" },
        { "url-in-text",       "notes.txt",    "see http://example.com\n" },
        { "c-block-comment",   "src/main.c",   "/* a C comment */\n" },
        { "plain-ascii",       "readme.md",    "no slashes here\n" },
        { "dotdot-in-content", "Makefile",     "CFLAGS=-I/../include\n" },
        { "backslashes",       "a.txt",        "C:\\\\Windows\\\\System\n" },
        { "empty-content",     "b.txt",        "" },
        /* The path itself is still checked -- escapes must still be refused. */
        { "escape-path",       "../etc/x",     "hello\n" },
        { "absolute-path",     "/etc/x",       "hello\n" },
        { "drive-path",        "C:\\\\x",       "hello\n" },
        { NULL, NULL, NULL }
    };
    char body[4096];
    char out[MAX_PATH];
    int i;

    for (i = 0; cases[i].label; i++) {
        unsigned off = 0, blen;
        int rc, n;
        /* rc_strf, not sprintf: `make check`'s unbounded-string gate refuses
         * the overflow family everywhere in win/, tests included -- and it is
         * right to, because a harness that overflows its own buffer reports a
         * failure in the code under test. */
        rc_strf(body, sizeof(body), "%u %s",
                (unsigned)strlen(cases[i].path), cases[i].path);
        n = (int)strlen(body);
        rc_strf(body + n, sizeof(body) - n, "%s", cases[i].data);
        blen = (unsigned)strlen(body);
        rc = rc_syncrpc_put_path(body, blen, out, sizeof(out), &off);
        printf("%s\t%s\t%u\t%s\n",
               rc == RC_PUTPATH_OK      ? "ok"      :
               rc == RC_PUTPATH_TOOLONG ? "too_long":
               rc == RC_PUTPATH_BADPATH ? "bad_path": "no_sep",
               out, off, cases[i].label);
    }

    /* A body with no separator at all: the prefix-walk must refuse rather
     * than step one byte past the allocation. */
    {
        unsigned off = 0;
        int rc;
        rc_strf(body, sizeof(body), "12345");
        rc = rc_syncrpc_put_path(body, 5, out, sizeof(out), &off);
        printf("%s\t%s\t%u\t%s\n",
               rc == RC_PUTPATH_OK ? "ok" : "no_sep", out, off, "no-separator");
    }
    /* A length the bridge declares that is larger than the frame. */
    {
        unsigned off = 0;
        int rc, n;
        rc_strf(body, sizeof(body), "900 short.txt");
        n = (int)strlen(body);
        rc = rc_syncrpc_put_path(body, (unsigned)n, out, sizeof(out), &off);
        printf("%s\t%s\t%u\t%s\n",
               rc == RC_PUTPATH_OK ? "ok" : "no_sep", out, off,
               "length-past-end");
    }
    return 0;
}

static int contained(void)
{
    static const char *cases[] = {
        /* must be REFUSED */
        "..", "../x", "a/../../x", ".", "./x", "a/./b",
        "a\\..\\..\\x",          /* the hole: no '/' at all */
        "..\\x", "a\\..", "a/..\\b", "a\\../b",
        "/abs", "\\abs", "C:\\x", "a//b", "a\\\\b", "",
        /* must be ALLOWED */
        "a", "a/b", "a/b/c.txt", "..a", "a..", "a/..b", "dot.dot/x",
        NULL
    };
    int i;
    for (i = 0; cases[i]; i++)
        printf("%s\t%s\n", rc_sync_contained(cases[i]) ? "allow" : "refuse",
               cases[i]);
    return 0;
}

/* Emit the manifest EXACTLY as it goes on the wire.
 *
 * The tab-separated `emit` above proves the classifier and the checksums and
 * never touches the wire format -- which is why a field could be added on one
 * side and pass the whole suite. This goes through rc_sync_manifest_row, the
 * same function do_scan uses, so the Python side can parse this output with
 * the BRIDGE's own parser and a divergence fails the build. */
static void emit_wire(void *ctx, const char *rel, const rc_sync_stat *st)
{
    char row[MAX_PATH + 160];
    unsigned n;
    (void)ctx;
    n = rc_sync_manifest_row(row, sizeof(row), rel, st);
    if (n) fwrite(row, 1, n, stdout);
}


/* Does rc_sync_commit clean up its temp file on EVERY refusal?
 *
 * It did not. Four of the five failure returns deleted the temp first and
 * the fifth -- the `got.type != 'f'` verification at the top -- did not, so
 * a transfer that could not be re-stat'd right after its own CloseHandle
 * left a fully-written .gaston-tmp in the user's tree. The scanner skips
 * those names, so nothing reported it and nothing removed it until the next
 * rc_sync_recover on start-up. See docs/mem-audit F2.
 *
 * Prints one row per case: case<TAB>rc<TAB>temp-still-there. The Python side
 * asserts the temp is gone wherever it CAN be gone -- which is not every
 * case, and the exceptions are the interesting part:
 *
 *   crc_mismatch   the ordinary refusal. Deletable, must be gone.
 *   too_long       refused before anything is opened, so there is no temp.
 *   vanished       the temp is removed underneath us, which is the only way
 *                  to reach the `type != 'f'` branch with a deletable file
 *                  -- it is already gone, so this case proves the branch is
 *                  REACHABLE and does not fault, not that it deletes.
 *   locked         stat cannot open it, so type is 'u' and the branch is
 *                  taken with the file still present -- and DeleteFileA on
 *                  a file somebody else holds open legitimately fails. The
 *                  temp survives, correctly, and the row says so.
 *   ok             the success path: temp gone, target in place.
 */
static int commitcases(const char *root)
{
    char tmp[MAX_PATH];
    HANDLE h;
    DWORD wrote;
    int rc;

    /* crc_mismatch: a complete temp, committed against a checksum it cannot
     * possibly match. */
    h = rc_sync_begin_write(root, "cc_crc.txt", tmp, sizeof(tmp));
    if (h == INVALID_HANDLE_VALUE) { fprintf(stderr, "no_root\n"); return 3; }
    WriteFile(h, "hello", 5, &wrote, NULL);
    CloseHandle(h);
    rc = rc_sync_commit(root, "cc_crc.txt", tmp, 1uL, RC_CLS_TEXT);
    printf("crc_mismatch\t%d\t%d\n", rc,
           GetFileAttributesA(tmp) != 0xFFFFFFFF);

    /* vanished: reach the verification branch with nothing at the path. */
    h = rc_sync_begin_write(root, "cc_gone.txt", tmp, sizeof(tmp));
    if (h != INVALID_HANDLE_VALUE) {
        WriteFile(h, "hello", 5, &wrote, NULL);
        CloseHandle(h);
        DeleteFileA(tmp);
        rc = rc_sync_commit(root, "cc_gone.txt", tmp, 0uL, RC_CLS_TEXT);
        printf("vanished\t%d\t%d\n", rc,
               GetFileAttributesA(tmp) != 0xFFFFFFFF);
    }

    /* locked: held open with no sharing, so rc_sync_stat_file reports 'u'. */
    h = rc_sync_begin_write(root, "cc_lock.txt", tmp, sizeof(tmp));
    if (h != INVALID_HANDLE_VALUE) {
        HANDLE keep;
        WriteFile(h, "hello", 5, &wrote, NULL);
        CloseHandle(h);
        keep = CreateFileA(tmp, GENERIC_READ, 0, NULL, OPEN_EXISTING,
                           FILE_ATTRIBUTE_NORMAL, NULL);
        rc = rc_sync_commit(root, "cc_lock.txt", tmp, 0uL, RC_CLS_TEXT);
        printf("locked\t%d\t%d\n", rc,
               GetFileAttributesA(tmp) != 0xFFFFFFFF);
        if (keep != INVALID_HANDLE_VALUE) CloseHandle(keep);
        DeleteFileA(tmp);
    }

    /* ok: the success path, so a regression in the cleanup cannot pass by
     * deleting everything unconditionally. */
    h = rc_sync_begin_write(root, "cc_ok.txt", tmp, sizeof(tmp));
    if (h != INVALID_HANDLE_VALUE) {
        rc_sync_stat st;
        char full[MAX_PATH];
        WriteFile(h, "hello", 5, &wrote, NULL);
        CloseHandle(h);
        memset(&st, 0, sizeof(st));
        rc_sync_stat_file(tmp, &st);
        rc = rc_sync_commit(root, "cc_ok.txt", tmp,
                            RC_CRC_FOR(st, RC_CLS_TEXT), RC_CLS_TEXT);
        rc_sync_join(full, sizeof(full), root, "cc_ok.txt");
        printf("ok\t%d\t%d\ttarget=%d\n", rc,
               GetFileAttributesA(tmp) != 0xFFFFFFFF,
               GetFileAttributesA(full) != 0xFFFFFFFF);
        DeleteFileA(full);
    }

    /* readonly_ok: target exists with FILE_ATTRIBUTE_READONLY, commit succeeds,
     * read-only attribute must be restored on the newly-committed file (05.4). */
    {
        char full[MAX_PATH];
        HANDLE hro;
        DWORD ro_attr;
        rc_sync_join(full, sizeof(full), root, "cc_rook.txt");
        hro = CreateFileA(full, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                          FILE_ATTRIBUTE_NORMAL, NULL);
        if (hro != INVALID_HANDLE_VALUE) {
            WriteFile(hro, "old", 3, &wrote, NULL);
            CloseHandle(hro);
            SetFileAttributesA(full, FILE_ATTRIBUTE_READONLY);
        }
        h = rc_sync_begin_write(root, "cc_rook.txt", tmp, sizeof(tmp));
        if (h != INVALID_HANDLE_VALUE) {
            rc_sync_stat st;
            WriteFile(h, "new", 3, &wrote, NULL);
            CloseHandle(h);
            memset(&st, 0, sizeof(st));
            rc_sync_stat_file(tmp, &st);
            rc = rc_sync_commit(root, "cc_rook.txt", tmp,
                                RC_CRC_FOR(st, RC_CLS_TEXT), RC_CLS_TEXT);
            ro_attr = GetFileAttributesA(full);
            printf("readonly_ok\t%d\t%d\n", rc,
                   (ro_attr != 0xFFFFFFFF && (ro_attr & FILE_ATTRIBUTE_READONLY)) ? 1 : 0);
            if (ro_attr != 0xFFFFFFFF)
                SetFileAttributesA(full, ro_attr & ~FILE_ATTRIBUTE_READONLY);
            DeleteFileA(full);
        }
    }

    /* readonly_fail: target exists with FILE_ATTRIBUTE_READONLY, but is held open
     * so MoveFileA in commit fails (-3). Read-only attribute MUST be restored
     * on failure (05.4) -- previously it was left stripped. */
    {
        char full[MAX_PATH];
        HANDLE hro, keep;
        DWORD ro_attr;
        rc_sync_join(full, sizeof(full), root, "cc_rofail.txt");
        hro = CreateFileA(full, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                          FILE_ATTRIBUTE_NORMAL, NULL);
        if (hro != INVALID_HANDLE_VALUE) {
            WriteFile(hro, "old", 3, &wrote, NULL);
            CloseHandle(hro);
            SetFileAttributesA(full, FILE_ATTRIBUTE_READONLY);
        }
        h = rc_sync_begin_write(root, "cc_rofail.txt", tmp, sizeof(tmp));
        if (h != INVALID_HANDLE_VALUE) {
            rc_sync_stat st;
            WriteFile(h, "new", 3, &wrote, NULL);
            CloseHandle(h);
            memset(&st, 0, sizeof(st));
            rc_sync_stat_file(tmp, &st);
            keep = CreateFileA(full, GENERIC_READ, 0, NULL, OPEN_EXISTING,
                               FILE_ATTRIBUTE_NORMAL, NULL);
            rc = rc_sync_commit(root, "cc_rofail.txt", tmp,
                                RC_CRC_FOR(st, RC_CLS_TEXT), RC_CLS_TEXT);
            if (keep != INVALID_HANDLE_VALUE) CloseHandle(keep);
            ro_attr = GetFileAttributesA(full);
            printf("readonly_fail\t%d\t%d\n", rc,
                   (ro_attr != 0xFFFFFFFF && (ro_attr & FILE_ATTRIBUTE_READONLY)) ? 1 : 0);
            if (ro_attr != 0xFFFFFFFF)
                SetFileAttributesA(full, ro_attr & ~FILE_ATTRIBUTE_READONLY);
            DeleteFileA(full);
        }
    }
    return 0;
}

/* The scanner's normalisation buffer must hold a chunk PLUS the CR carried
 * in from the one before it: that CR is emitted ahead of the new chunk's
 * first byte, so a full chunk that does not itself end in CR normalises to
 * RC_SYNC_CHUNK + 1 bytes. With the buffer one byte short, that last byte
 * went past the scanner's heap block, into the next block's header, and on
 * the real machine a SYNCGET flood died in KERNEL32's heap code
 * (drainprobe, 2026-09-25).
 *
 * HeapValidate cannot see it under Wine, which rounds a block up and so
 * gives the stray byte slack to land in. So the block is made to own the
 * bytes past the struct: grown by 16 with HeapReAlloc (the struct holds no
 * pointers into itself, so a move is harmless, and finish frees whatever
 * pointer it is handed), with a canary at the struct's old end. Any write
 * there is a write past the scanner. Fed through the public scanner, the way
 * do_get uses it. Prints one verdict row. */
static int scanner(void)
{
    static unsigned char chunk[RC_SYNC_CHUNK];
    rc_sync_scanner *s;
    unsigned char *end;
    SIZE_T size;
    char cls;
    unsigned long crc;

    s = rc_sync_scanner_begin();
    if (!s) { printf("FAIL\tscanner\tno memory\n"); return 1; }
    size = HeapSize(GetProcessHeap(), 0, s);
    {
        rc_sync_scanner *grown = (rc_sync_scanner *)HeapReAlloc(
            GetProcessHeap(), 0, s, size + 16);
        if (size == (SIZE_T)-1 || !grown) {
            rc_sync_scanner_finish(s, NULL, NULL);
            printf("FAIL\tscanner\tcannot grow the block to watch it\n");
            return 1;
        }
        s = grown;
    }
    end = (unsigned char *)s + size;
    memset(end, 0xA5, 16);

    memset(chunk, 'A', sizeof chunk);
    chunk[RC_SYNC_CHUNK - 1] = '\r';               /* carried into the next */
    rc_sync_scanner_feed(s, chunk, RC_SYNC_CHUNK);
    memset(chunk, 'A', sizeof chunk);              /* no LF: the CR is lone */
    rc_sync_scanner_feed(s, chunk, RC_SYNC_CHUNK);

    if (end[0] != 0xA5) {
        rc_sync_scanner_finish(s, &cls, &crc);
        printf("FAIL\tscanner\twrote past its %lu byte block\n",
               (unsigned long)size);
        return 1;
    }
    rc_sync_scanner_finish(s, &cls, &crc);
    printf("pass\tscanner\t%lu byte block, nothing written past it\n",
           (unsigned long)size);
    return 0;
}

int main(int argc, char **argv)
{
    int n;
    if (argc >= 3 && strcmp(argv[1], "--wire") == 0) {
        /* BINARY stdout. In text mode the CRT turns every '\n' the row
         * terminator into "\r\n", which is exactly the corruption this
         * check exists to detect -- the output would no longer be the bytes
         * that go on the wire, and the comparison would be against a format
         * nothing actually sends. */
        _setmode(_fileno(stdout), _O_BINARY);
        n = rc_sync_scan(argv[2], emit_wire, NULL);
        if (n == -1) { fprintf(stderr, "no_root\n"); return 3; }
        if (n == -2) { fprintf(stderr, "too_many\n"); return 4; }
        fprintf(stderr, "entries=%d\n", n);
        return 0;
    }
    if (argc >= 2 && strcmp(argv[1], "--limits") == 0) return limits();
    if (argc >= 2 && strcmp(argv[1], "--contained") == 0) return contained();
    if (argc >= 2 && strcmp(argv[1], "--putpath") == 0) return putpath();
    if (argc >= 2 && strcmp(argv[1], "--scanner") == 0) return scanner();
    if (argc >= 3 && strcmp(argv[1], "--commit") == 0) return commitcases(argv[2]);
    /* 03.14: the chunk-boundary corpus in test_guest_agreement.py used to
     * hardcode byte offsets 8191/8190 rather than deriving them from
     * RC_SYNC_CHUNK, so a change to the chunk size would leave the
     * carry-logic test quietly testing the OLD boundary while it kept
     * passing. This prints the real constant so the corpus can be built
     * around it instead. */
    if (argc >= 2 && strcmp(argv[1], "--chunk") == 0) {
        printf("%d\n", (int)RC_SYNC_CHUNK);
        return 0;
    }
    if (argc < 2) { fprintf(stderr, "usage: synccheck <dir>|--wire <dir>|--limits|--contained|--putpath|--commit <dir>|--chunk|--scanner\n"); return 2; }
    n = rc_sync_scan(argv[1], emit, NULL);
    if (n == -1) { fprintf(stderr, "no_root\n"); return 3; }
    if (n == -2) { fprintf(stderr, "too_many\n"); return 4; }
    fprintf(stderr, "entries=%d\n", n);
    return 0;
}
