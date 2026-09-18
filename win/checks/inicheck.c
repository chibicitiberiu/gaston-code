/* Asserts that rc_config_save writes every field rc_config_load reads.
 *
 * This is not a hypothetical. font_size was in load() and not in save() for
 * months, and the comment added when that was fixed predicted the sync root
 * would go the same way. It did -- in the other direction: save() wrote
 * [sync] root and load() never read it, so the guest ran with an empty sync
 * root and the whole layer was silently off, with no error anywhere.
 *
 * Reading both functions did not catch it either time. So the check is
 * mechanical: fill a config with values that are distinct from every default,
 * save, load into a zeroed struct, compare field by field. A field added to
 * the struct and to only one of the two functions fails here.
 *
 * Run under Wine, because GetPrivateProfileStringA is the thing being tested
 * -- a reimplementation in Python would be testing the reimplementation.
 */
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include "core/str.h"
#include "core/ini.h"

static int fails = 0;

static void eq_s(const char *what, const char *got, const char *want)
{
    if (strcmp(got, want) == 0) { printf("ok   %-12s %s\n", what, got); return; }
    printf("FAIL %-12s got '%s' want '%s'\n", what, got, want);
    fails++;
}

static void eq_i(const char *what, int got, int want)
{
    if (got == want) { printf("ok   %-12s %d\n", what, got); return; }
    printf("FAIL %-12s got %d want %d\n", what, got, want);
    fails++;
}

int main(int argc, char **argv)
{
    rc_config out, in;
    char path[MAX_PATH];
    const char *dir = argc > 1 ? argv[1] : ".";

    /* Every value differs from the default rc_config_load would substitute, so
     * "the key was never written" cannot pass by coincidence. */
    memset(&out, 0, sizeof(out));
    strcpy(out.host,      "10.11.12.13");
    out.port =            7999;
    strcpy(out.secret,    "not-dev-secret");
    out.font_size =       14;
    out.autoscroll =      0;
    strcpy(out.sync_root, "C:\\WORK\\SUB");

    rc_strf(path, sizeof(path), "%s\\retroclaude.ini", dir);
    DeleteFileA(path);

    rc_config_save(&out, dir);

    memset(&in, 0, sizeof(in));
    rc_config_load(&in, dir);

    eq_s("host",       in.host,      out.host);
    eq_i("port",       in.port,      out.port);
    eq_s("secret",     in.secret,    out.secret);
    eq_i("font_size",  in.font_size, out.font_size);
    eq_i("autoscroll", in.autoscroll, out.autoscroll);
    eq_s("sync_root",  in.sync_root, out.sync_root);

    /* sizeof is the tripwire for a field added to the struct and to neither
     * function: it cannot check the field, but it can refuse to pass quietly. */
    eq_i("sizeof(rc_config)", (int)sizeof(rc_config),
         (int)(64 + sizeof(int) + 128 + sizeof(int) + sizeof(int) + 128));

    DeleteFileA(path);
    if (fails) { printf("\n%d FAILURES\n", fails); return 1; }
    printf("\nall config fields round-trip\n");
    return 0;
}
