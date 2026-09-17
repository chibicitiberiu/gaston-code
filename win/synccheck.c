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
#include "sync.h"

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

int main(int argc, char **argv)
{
    int n;
    if (argc < 2) { fprintf(stderr, "usage: synccheck <dir>\n"); return 2; }
    n = rc_sync_scan(argv[1], emit, NULL);
    if (n == -1) { fprintf(stderr, "no_root\n"); return 3; }
    if (n == -2) { fprintf(stderr, "too_many\n"); return 4; }
    fprintf(stderr, "entries=%d\n", n);
    return 0;
}
