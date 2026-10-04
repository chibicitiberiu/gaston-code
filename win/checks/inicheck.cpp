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
#include "app/workcopy.h"

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
    path.join(dir, "gaston.ini");
    DeleteFileA(path.str());

    /* --- round trip ----------------------------------------------------- */
    {
        /* Every value differs from the default load would substitute, so
         * "the key was never written" cannot pass by coincidence. */
        /* CONSTRUCTED, not memset: ClientConfig holds std::vectors since
         * the Options dialog, and zeroing one is undefined behaviour. Its
         * constructor sets every scalar (Watcom C++98 does not zero a class
         * on `new`, CLAUDE.md). */
        ClientConfig out, in;
        rc_strcpy(out.host, sizeof out.host, "10.11.12.13");
        out.port = 7999;
        rc_strcpy(out.secret, sizeof out.secret, "not-dev-secret");
        out.autoscroll = false;
        out.automation = false;
        rc_strcpy(out.syncRoot, sizeof out.syncRoot, "C:\\WORK\\SUB");
        rc_strcpy(out.defaultModel, sizeof out.defaultModel, "haiku");
        rc_strcpy(out.defaultEffort, sizeof out.defaultEffort, "low");
        rc_strcpy(out.defaultMode, sizeof out.defaultMode, "auto");
        out.pageSize = RC_PAGE_LARGE;
        out.sweepMin = -1;
        out.maxFileKb = 2048;
        out.existing = RC_WORK_MERGE;
        out.ignore.add("*.obj");
        out.ignore.add("a=b");
        out.deadlineMin = 5;
        out.protectAdd.add("D:\\DATA\\**");
        out.protectRem.add("{BOOT}\\*.SYS");
        out.dangerAdd.add("zap wipes things");
        out.dangerRem.add("chkdsk");
        out.trustAdd.add("nasm");
        out.trustRem.add("calc");
        rc_strcpy(out.logDir, sizeof out.logDir, "C:\\LOGS");
        out.cmdLog = true;
        {
            ModeDef &m = out.modes[out.modeCount++];
            rc_strcpy(m.id, sizeof m.id, "u-0a1b2c3d");
            rc_strcpy(m.label, sizeof m.label, "Quiet");
            rc_strcpy(m.base, sizeof m.base, "auto");
            memcpy(m.values, "yccyyccycyocy", RC_CATS);
        }

        ConfigStore store(dir);
        store.save(out);

        ConfigStore reader(dir);
        truth("load", reader.load(&in), reader.error());

        eqS("host", in.host, out.host);
        eqL("port", in.port, out.port);
        eqS("secret", in.secret, out.secret);
        eqL("autoscroll", in.autoscroll ? 1 : 0, out.autoscroll ? 1 : 0);
        eqL("automation", in.automation ? 1 : 0, out.automation ? 1 : 0);
        eqS("sync_root", in.syncRoot, out.syncRoot);
        eqS("model", in.defaultModel, out.defaultModel);
        eqS("effort", in.defaultEffort, out.defaultEffort);
        eqS("mode", in.defaultMode, out.defaultMode);
        eqL("page", in.pageSize, out.pageSize);
        eqL("sweep", in.sweepMin, out.sweepMin);
        eqL("maxkb", in.maxFileKb, out.maxFileKb);
        eqL("existing", in.existing, out.existing);
        eqL("deadline", in.deadlineMin, out.deadlineMin);
        eqS("logdir", in.logDir, out.logDir);
        eqL("cmdlog", in.cmdLog ? 1 : 0, out.cmdLog ? 1 : 0);
        eqL("ignore#", (long)in.ignore.count(), 2L);
        eqS("ignore", in.ignore.text(), "*.obj\na=b");
        eqL("ignore2", in.ignore.has("a=b") ? 1 : 0, 1L);
        eqS("protect+", in.protectAdd.text(), "D:\\DATA\\**");
        eqS("protect-", in.protectRem.text(), "{BOOT}\\*.SYS");
        eqS("danger+", in.dangerAdd.text(), "zap wipes things");
        eqS("danger-", in.dangerRem.text(), "chkdsk");
        eqS("trust+", in.trustAdd.text(), "nasm");
        eqS("trust-", in.trustRem.text(), "calc");
        eqL("modes#", (long)in.modeCount, 1L);
        if (in.modeCount == 1) {
            char vals[RC_CATS + 1];
            memcpy(vals, in.modes[0].values, RC_CATS);
            vals[RC_CATS] = 0;
            eqS("mode.id", in.modes[0].id, "u-0a1b2c3d");
            eqS("mode.label", in.modes[0].label, "Quiet");
            eqS("mode.base", in.modes[0].base, "auto");
            eqS("mode.values", vals, "yccyyccycyocy");
        }

        /* A list saved SHORTER leaves no stale tail, and a deleted mode's
         * section goes with it. */
        out.ignore.remove("a=b");
        out.modeCount = 0;
        store.save(out);
        ClientConfig again;
        ConfigStore reader2(dir);
        truth("reload", reader2.load(&again), reader2.error());
        eqL("shrunk", (long)again.ignore.count(), 1L);
        eqL("mode-gone", (long)again.modeCount, 0L);
        char probe[16];
        GetPrivateProfileStringA("mode.u-0a1b2c3d", "label", "", probe,
                                 sizeof probe, path.str());
        eqS("no-orphan", probe, "");
        DeleteFileA(path.str());
    }

    /* --- a missing file gets the defaults, and is written --------------- */
    {
        ClientConfig cfg;
        ConfigStore store(dir);
        bool loaded = store.load(&cfg);
        truth("defaults", loaded && strcmp(cfg.host, "192.168.1.2") == 0 &&
              cfg.port == 7890 && cfg.autoscroll &&
              /* ON by default. An install that upgrades keeps the five screen
               * tools it already had; see config.h. */
              cfg.automation &&
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
    {
        /* The setting is read, and `off` is a value somebody can actually
         * write -- the whole point of BACKLOG.md 19. */
        writeFile(path.str(), "[ui]\r\nautomation=0\r\n");
        ClientConfig cfg;
        ConfigStore store(dir);
        bool loaded = store.load(&cfg);
        truth("automation-off", loaded && !cfg.automation, store.error());
    }
    {
        /* A list key nobody numbers is refused, like any unknown key: the
         * profile API cannot hold two `add=` lines, so a hand edit that
         * wrote one would lose every entry but the last without a word. */
        writeFile(path.str(), "[protect]\r\nadd=C:\\X\r\n");
        ClientConfig cfg;
        ConfigStore store(dir);
        bool loaded = store.load(&cfg);
        truth("unnumbered", !loaded && strstr(store.error(), "add") != 0,
              store.error());
    }
    {
        writeFile(path.str(), "[modes]\r\norder=u-11111111\r\n"
                              "[mode.u-11111111]\r\nlabel=M\r\n"
                              "read=sometimes\r\n");
        ClientConfig cfg;
        ConfigStore store(dir);
        bool loaded = store.load(&cfg);
        truth("bad-mode-value", !loaded, store.error());
    }
    {
        /* CLAUDE.md: an invalid value refuses startup. A capability that
         * silently fell back to its default on `automation=maybe` would be
         * the permissive reading arrived at in silence -- and it would be
         * arrived at for the one setting whose whole purpose is to say no. */
        writeFile(path.str(), "[ui]\r\nautomation=maybe\r\n");
        ClientConfig cfg;
        ConfigStore store(dir);
        bool loaded = store.load(&cfg);
        truth("automation-invalid",
              !loaded && strstr(store.error(), "automation") != 0,
              store.error());
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
