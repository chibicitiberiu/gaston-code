#include "app/machineid.h"

#include <objbase.h>
#include <stdio.h>
#include <string.h>

#include "core/log.h"
#include "core/str.h"

/* 8-4-4-4-12, the spelling every other GUID in this system uses -- the
 * bridge's own conversation ids are the same shape, so a `machine` value in
 * a log reads as the same kind of thing. */
#define RC_MACHINE_LEN 36

const char *MachineId::id() const
{
    return m_id.str();
}

/* Exactly 36 characters of lowercase hex with dashes in the four usual
 * places, and nothing else.
 *
 * Strict on purpose. The value goes into a HELLO header, and a header value
 * is the one place a stray byte from the disk reaches the wire; it is also
 * the key the bridge files conversations under, so a value that varies by a
 * trailing space is a second machine. Refusing here means the only ids that
 * ever travel are ones this code minted.
 */
static bool wellFormed(const char *s)
{
    unsigned i;

    if (strlen(s) != RC_MACHINE_LEN)
        return false;
    for (i = 0; i < RC_MACHINE_LEN; i++)
    {
        char c = s[i];
        if (i == 8 || i == 13 || i == 18 || i == 23)
        {
            if (c != '-')
                return false;
        }
        else if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')))
            return false;
    }
    return true;
}

bool MachineId::read(const char *path)
{
    char line[64];
    char *end;

    File f(fopen(path, "r"));
    if (!f.valid())
        return false;
    if (!fgets(line, sizeof(line), f.get()))
        return false;
    /* Trailing CR, LF or spaces. The file is meant to be editable on the
     * guest and EDIT.COM writes CRLF, so a hand-copied id would otherwise
     * fail wellFormed() for a reason nobody could see. */
    end = line + strlen(line);
    while (end > line && (unsigned char)end[-1] <= ' ')
        *--end = '\0';
    if (!wellFormed(line))
        return false;
    return m_id.set(line) != 0;
}

bool MachineId::mint(const char *path)
{
    GUID g;
    char text[64];
    bool wrote;

    /* CoCreateGuid, not GetTickCount and a hash of the machine name. OLE32
     * is already linked and already initialised (app/oleservice.cpp), it is
     * on the import audit's allow-list, and it gives a value that two
     * machines imaged from the same disk at the same minute do not share --
     * which is precisely the pair this id exists to tell apart. */
    if (FAILED(CoCreateGuid(&g)))
    {
        rc_log("machine id: CoCreateGuid failed (%lu)",
               (unsigned long)GetLastError());
        return false;
    }
    rc_strf(text, sizeof(text),
            "%08lx-%04x-%04x-%02x%02x-%02x%02x%02x%02x%02x%02x",
            (unsigned long)g.Data1, (unsigned)g.Data2, (unsigned)g.Data3,
            (unsigned)g.Data4[0], (unsigned)g.Data4[1],
            (unsigned)g.Data4[2], (unsigned)g.Data4[3],
            (unsigned)g.Data4[4], (unsigned)g.Data4[5],
            (unsigned)g.Data4[6], (unsigned)g.Data4[7]);

    {
        File f(fopen(path, "w"));
        if (!f.valid())
        {
            rc_log("machine id: cannot write %s (%lu)", path,
                   (unsigned long)GetLastError());
            return false;
        }
        wrote = fputs(text, f.get()) >= 0 && fputc('\n', f.get()) != EOF
                && fflush(f.get()) == 0;
    }
    if (!wrote)
    {
        rc_log("machine id: %s could not be written", path);
        return false;
    }

    /* ONLY NOW. An id that was minted but not persisted is worse than no id
     * at all: it would be a different id on every launch, so the chat list
     * would come up empty every time the machine was switched on, with
     * nothing to say why. No id means the bridge filters by `mine` alone,
     * which is the behaviour this client has always had. */
    return m_id.set(text) != 0;
}

void MachineId::load(const char *exeDir)
{
    Path path;

    path.join(exeDir, "machine.id");
    if (!path.ok())
    {
        rc_log("machine id: %s is too long a path; this machine will not be "
               "identified to the bridge", exeDir);
        return;
    }
    if (read(path.str()))
    {
        rc_log("machine id: %s", m_id.str());
        return;
    }
    /* Present but not an id: a truncated first write, or an edit that went
     * wrong. Replaced rather than refused -- nobody hand-authors this file,
     * and a client stuck without an identity until someone deletes a file
     * they have never heard of is the worse failure. Said out loud, because
     * the cost is real: conversations filed under the OLD id drop out of
     * this machine's list. */
    if (GetFileAttributesA(path.str()) != 0xFFFFFFFF)
        rc_log("machine id: %s does not hold one; replacing it", path.str());
    if (mint(path.str()))
        rc_log("machine id: minted %s in %s", m_id.str(), path.str());
    else
    {
        m_id.clear();
        rc_log("machine id: none -- the bridge will show every conversation "
               "it started, from whichever machine");
    }
}
