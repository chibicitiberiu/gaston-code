/* Asserts that ConfigStore::save writes every field ConfigStore::load reads,
 * and that load refuses what it should.
 *
 * The round trip is not a hypothetical. font_size was read and never written
 * for months, and the comment added when that was fixed predicted the sync
 * root would go the same way. It did -- in the other direction: save wrote
 * [sync] root and load never read it, so the guest ran with an empty sync
 * root and the whole layer was silently off, with no error anywhere.
 *
 * Reading both functions did not catch it either time. So the check is
 * mechanical: fill a config with values distinct from every default, save,
 * load into a zeroed struct, compare field by field.
 *
 * Run under Wine, because GetPrivateProfileStringA is part of what is being
 * tested -- a reimplementation in Python would be testing the
 * reimplementation. Output format is read by
 * tests/python/guest/test_guest_config.py.
 */
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include "app/config.h"

static int fails = 0;

static void eqS(const char *what, const char *got, const char *want)
{
    if (strcmp(got, want) == 0)
    {
        printf("ok   %-12s %s\n", what, got);
        return;
    }
    printf("FAIL %-12s got '%s' want '%s'\n", what, got, want);
    fails++;
}

static void eqL(const char *what, long got, long want)
{
    if (got == want)
    {
        printf("ok   %-12s %ld\n", what, got);
        return;
    }
    printf("FAIL %-12s got %ld want %ld\n", what, got, want);
    fails++;
}

static void truth(const char *what, bool cond, const char *detail)
{
    printf("%s %-12s %s\n", cond ? "ok  " : "FAIL", what, detail);
    if (!cond)
        fails++;
}

static bool writeFile(const char *path, const char *body)
{
    Handle file(CreateFileA(path, GENERIC_WRITE, 0, 0, CREATE_ALWAYS,
                            FILE_ATTRIBUTE_NORMAL, 0));
    if (!file.valid())
        return false;
    DWORD wrote = 0;
    return WriteFile(file.get(), body, (DWORD)strlen(body), &wrote, 0) &&
           wrote == strlen(body);
}

int main(int argc, char **argv)
{
    const char *dir = argc > 1 ? argv[1] : ".";
    Path path;
    path.join(dir, "retroclaude.ini");
    DeleteFileA(path.str());

    /* --- round trip ----------------------------------------------------- */
    {
        /* Every value differs from the default load would substitute, so
         * "the key was never written" cannot pass by coincidence. */
        ClientConfig out, in;
        memset(&out, 0, sizeof out);
        rc_strcpy(out.host, sizeof out.host, "10.11.12.13");
        out.port = 7999;
        rc_strcpy(out.secret, sizeof out.secret, "not-dev-secret");
        out.autoscroll = false;
        rc_strcpy(out.syncRoot, sizeof out.syncRoot, "C:\\WORK\\SUB");

        ConfigStore store(dir);
        store.save(out);

        memset(&in, 0, sizeof in);
        ConfigStore reader(dir);
        truth("load", reader.load(&in), reader.error());

        eqS("host", in.host, out.host);
        eqL("port", in.port, out.port);
        eqS("secret", in.secret, out.secret);
        eqL("autoscroll", in.autoscroll ? 1 : 0, out.autoscroll ? 1 : 0);
        eqS("sync_root", in.syncRoot, out.syncRoot);

        /* The tripwire for a field added to the struct and to neither
         * function: it cannot check the field, but it can refuse to pass
         * quietly. 64 + 4 + 128 + 128 + 1, padded to a multiple of 4. */
        eqL("sizeof", (long)sizeof(ClientConfig), 328L);
        DeleteFileA(path.str());
    }

    /* --- a missing file gets the defaults, and is written --------------- */
    {
        ClientConfig cfg;
        ConfigStore store(dir);
        bool loaded = store.load(&cfg);
        truth("defaults", loaded && strcmp(cfg.host, "192.168.1.2") == 0 &&
              cfg.port == 7890 && cfg.autoscroll &&
              strcmp(cfg.syncRoot, "C:\\Gaston") == 0, cfg.host);
        truth("first-save", GetFileAttributesA(path.str()) != 0xFFFFFFFF,
              "written on first run");
        DeleteFileA(path.str());
    }

    /* --- refusals ------------------------------------------------------- */
    {
        /* GetPrivateProfileIntA read this as 78 and connected there. */
        writeFile(path.str(), "[bridge]\r\nport=78g0\r\n");
        ClientConfig cfg;
        ConfigStore store(dir);
        bool loaded = store.load(&cfg);
        truth("bad-port", !loaded && strstr(store.error(), "port") != 0,
              store.error());
    }
    {
        writeFile(path.str(), "[bridge]\r\nprot=7890\r\n");
        ClientConfig cfg;
        ConfigStore store(dir);
        bool loaded = store.load(&cfg);
        truth("typo-key", !loaded && strstr(store.error(), "prot") != 0,
              store.error());
    }
    {
        /* Older clients wrote font_size; real INIs still carry it. */
        writeFile(path.str(), "[ui]\r\nautoscroll=0\r\nfont_size=9\r\n");
        ClientConfig cfg;
        ConfigStore store(dir);
        bool loaded = store.load(&cfg);
        truth("retired-key", loaded && !cfg.autoscroll, store.error());
    }

    DeleteFileA(path.str());
    if (fails)
    {
        printf("\n%d FAILURES\n", fails);
        return 1;
    }
    printf("\nall config fields round-trip\n");
    return 0;
}
