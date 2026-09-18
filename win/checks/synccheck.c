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
 * bridge/paths.py. Same corpus, same answers, or the build is wrong.
 */
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include "sync/sync.h"

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

int main(int argc, char **argv)
{
    int n;
    if (argc >= 2 && strcmp(argv[1], "--limits") == 0) return limits();
    if (argc < 2) { fprintf(stderr, "usage: synccheck <dir>|--limits\n"); return 2; }
    n = rc_sync_scan(argv[1], emit, NULL);
    if (n == -1) { fprintf(stderr, "no_root\n"); return 3; }
    if (n == -2) { fprintf(stderr, "too_many\n"); return 4; }
    fprintf(stderr, "entries=%d\n", n);
    return 0;
}
