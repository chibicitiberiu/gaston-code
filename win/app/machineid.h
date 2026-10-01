/* WHICH MACHINE THIS IS -- one random id, minted once, kept beside the exe.
 *
 * The bridge remembers which conversations it started (`mine` in
 * `~/.gaston-code/conversations.json`), and that answer is per-BRIDGE. Point
 * one bridge at two guests and each one's chat list fills with the other's
 * conversations: the other's drive letters, the other's compiler, the other's
 * sync root in every folder path. `bridge/agent/conversations.py` already
 * argues that continuing such a conversation rarely makes sense -- the
 * argument is about a MACHINE, and nothing on the wire said which machine was
 * asking. This is that missing fact; it rides `HELLO` as the `machine`
 * header.
 *
 * NOT the volume serial, and not the machine name. `WIN98` is the name on
 * every fresh install, and a serial changes when the disk is reformatted --
 * which loses the chat list for a machine that is otherwise the same one. A
 * minted id survives everything except deleting the file.
 *
 * WHY A FILE OF ITS OWN rather than a key in gaston.ini. `IniFile` can write
 * (setString/flush, and ConfigStore::save already uses them), so this was a
 * choice, not a limit:
 *
 *   - gaston.ini holds the host, the port and the secret, and a value in it
 *     that is PRESENT BUT WRONG refuses startup by design (app/config.h). A
 *     write interrupted by a power cut on a 1998 box would then take the
 *     whole configuration down, to store a field nothing else reads.
 *   - this is written ONCE, on first run, and never again. gaston.ini is
 *     rewritten whenever a setting changes, so the id would be rewritten with
 *     it every time, for no reason.
 *   - `machine.id` is one line of ASCII, readable and fixable with EDIT.COM.
 *     Moving a machine's identity to a new install is copying one file.
 *
 * The name is 8.3 by construction (`machine` + `id`), so it has no short
 * alias to disagree with and nothing here has to spell one out.
 */
#ifndef RC_MACHINEID_H
#define RC_MACHINEID_H

#ifndef __cplusplus
#error "app/machineid.h is C++"
#endif

#include "core/rc_cpp.h"

class MachineId
{
public:
    /* Buf zeroes itself, so there is nothing to set -- but a class with a
     * private member is a non-POD in C++98, and `new MachineId()` would
     * NOT zero one that had no constructor at all (CLAUDE.md). Defined here
     * rather than in the .cpp because Watcom asks for it (W656). */
    MachineId() {}

    /* Read `<exeDir>\machine.id`, minting and writing one if it is missing
     * or unreadable. NEVER FAILS THE CALLER: a client that cannot have an
     * identity still starts and still works. `id()` is then empty, the
     * `machine` header is not sent, and the bridge falls back to filtering
     * the chat list by `mine` alone -- which is exactly what every client
     * built before this did. Each outcome is logged, because an empty id is
     * otherwise invisible until someone wonders why two machines share a
     * list. */
    void load(const char *exeDir);

    /* The id, or "" if there is none. 36 characters, 8-4-4-4-12 hex. */
    const char *id() const;

private:
    MachineId(const MachineId &);
    MachineId &operator=(const MachineId &);

    bool read(const char *path);
    bool mint(const char *path);

    /* 36 characters and a NUL. Buf sizes to what is there rather than to a
     * MAX_*: anything longer is refused on the way in. */
    Buf<40> m_id;
};

#endif
