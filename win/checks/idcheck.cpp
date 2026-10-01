/* The machine identity: minted once, read back forever.
 *
 * `MachineId` (app/machineid.h) is the only thing that decides whether two
 * guests share a chat list, and every one of its failure modes is silent in
 * the app. A client that re-mints on every launch looks exactly like a
 * client that works, right up to the morning the list is empty; a client
 * that cannot write the file looks the same from the outside as one that
 * can. Neither is visible without running the thing and looking at the
 * disk, which is what this does.
 *
 * Under Wine, because the file half is `fopen`/`fgets` against a real
 * filesystem and the mint half is `CoCreateGuid` out of OLE32 -- a Python
 * reimplementation of either would be testing the reimplementation. What
 * this CANNOT settle is the Win98 platform itself (a read-only medium, a
 * full disk); those are the branches the log lines exist for.
 *
 * Output format is read by tests/python/guest/test_guest_machineid.py.
 */
#include <windows.h>
#include <ole2.h>
#include <stdio.h>
#include <string.h>

#include "app/machineid.h"
#include "core/log.h"
#include "core/rc_cpp.h"

static int fails = 0;

static void ok(const char *what)
{
    printf("ok   %s\n", what);
}

static void bad(const char *what, const char *detail)
{
    printf("FAIL %s -- %s\n", what, detail);
    fails++;
}

static void check(const char *what, bool cond, const char *detail)
{
    if (cond) ok(what); else bad(what, detail);
}

/* The shape the bridge files conversations under: 8-4-4-4-12 lowercase hex.
 * Written out here rather than exported from machineid.cpp on purpose -- a
 * check that shares the function under test agrees with it by construction. */
static bool shaped(const char *s)
{
    unsigned i;
    if (strlen(s) != 36) return false;
    for (i = 0; i < 36; i++)
    {
        if (i == 8 || i == 13 || i == 18 || i == 23)
        {
            if (s[i] != '-') return false;
        }
        else if (!((s[i] >= '0' && s[i] <= '9') ||
                   (s[i] >= 'a' && s[i] <= 'f')))
            return false;
    }
    return true;
}

static void writeFile(const char *path, const char *text)
{
    File f(fopen(path, "wb"));
    if (f.valid()) fputs(text, f.get());
}

static bool exists(const char *path)
{
    return GetFileAttributesA(path) != 0xFFFFFFFF;
}

/* A fresh, empty directory under `root`, so each scenario starts with no
 * machine.id and the previous one's cannot leak into it. */
static void subdir(Path &dir, Path &file, const char *root, const char *name)
{
    dir.join(root, name);
    CreateDirectoryA(dir.str(), NULL);
    file.join(dir.str(), "machine.id");
    DeleteFileA(file.str());
}

int main(int argc, char **argv)
{
    const char *root = (argc > 1) ? argv[1] : ".";
    Path a, aFile, b, bFile;
    char first[64];

    /* Like the app: OLE up before anything asks OLE32 for a GUID. */
    OleInitialize(NULL);
    rc_log_open(root, "idcheck");

    subdir(a, aFile, root, "A");
    subdir(b, bFile, root, "B");

    /* 1. Nothing there -> one is minted, and it lands on disk. */
    {
        MachineId m;
        m.load(a.str());
        check("minted", shaped(m.id()), m.id());
        check("persisted", exists(aFile.str()), aFile.str());
        rc_strf(first, sizeof(first), "%s", m.id());
    }

    /* 2. The whole point: the SECOND launch reads it back rather than
     *    minting a new one. A client that fails this looks identical from
     *    the outside and loses its chat list on every reboot. */
    {
        MachineId m;
        m.load(a.str());
        check("stable across launches", strcmp(m.id(), first) == 0, m.id());
    }

    /* 3. Two machines are two ids. This is the only property the feature
     *    actually needs, and a mint derived from the machine NAME or from a
     *    fresh install's volume serial would fail it. */
    {
        MachineId m;
        m.load(b.str());
        check("distinct per directory", strcmp(m.id(), first) != 0, m.id());
    }

    /* 4. Trailing CRLF and spaces, which is what EDIT.COM leaves behind
     *    when the file is copied to a new install by hand. */
    {
        MachineId m;
        writeFile(aFile.str(), "0123abcd-4567-89ef-0123-456789abcdef  \r\n");
        m.load(a.str());
        check("trailing whitespace trimmed",
              strcmp(m.id(), "0123abcd-4567-89ef-0123-456789abcdef") == 0,
              m.id());
    }

    /* 5. Garbage is REPLACED, not refused. Nobody hand-authors this file,
     *    and a client stuck without an identity until someone deletes a
     *    file they have never heard of is the worse failure. */
    {
        const char *junk[] = {
            "",                                        /* truncated write */
            "not-an-id\r\n",
            "0123ABCD-4567-89EF-0123-456789ABCDEF\r\n", /* uppercase */
            "0123abcd-4567-89ef-0123-456789abcde\r\n",  /* one short */
            "0123abcd45678-9ef-0123-456789abcdef\r\n"   /* dashes moved */
        };
        unsigned i;
        for (i = 0; i < sizeof(junk) / sizeof(junk[0]); i++)
        {
            MachineId m;
            writeFile(aFile.str(), junk[i]);
            m.load(a.str());
            if (!shaped(m.id()))
            {
                bad("garbage replaced", junk[i]);
                break;
            }
            /* ...and the replacement is on disk, or the next launch mints
             * again and we are back to a new machine every morning. */
            MachineId again;
            again.load(a.str());
            if (strcmp(again.id(), m.id()) != 0)
            {
                bad("replacement persisted", junk[i]);
                break;
            }
            if (i + 1 == sizeof(junk) / sizeof(junk[0]))
                ok("garbage replaced and persisted");
        }
    }

    /* 6. A directory that cannot hold the file. The client must still come
     *    up, with no id rather than a volatile one: an id minted and not
     *    written would differ on every launch, which empties the chat list
     *    every morning with nothing to say why. */
    {
        Path gone;
        MachineId m;
        gone.join(root, "NOSUCHDIR");
        m.load(gone.str());
        check("no id when it cannot be written", m.id()[0] == '\0', m.id());
    }

    if (fails)
        printf("\n%d FAILURES\n", fails);
    else
        printf("\nall machine-id properties hold\n");
    rc_log_close();
    OleUninitialize();
    return fails ? 1 : 0;
}
