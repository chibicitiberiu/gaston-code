/* gremote's settings: gremote.ini beside the exe, through libcore's IniFile.
 *
 * The composition root loads this ONCE into per-service structs and hands
 * each service its struct; services never read the INI (docs/REMOTE.md).
 *
 * A value that is present and wrong refuses startup, the same rule the
 * client follows and the handshake follows -- never guess. `secret` is the
 * sharp one: the shipped default is refused on purpose, so a machine nobody
 * has configured does not come up listening with a secret written in a
 * public document.
 *
 * THE FILENAME. This joins the exe's directory with the literal
 * "gremote.ini". That name is 7.3, so it IS an 8.3 name, and FAT ignores
 * case -- GREMOTE.INI off a CD and gremote.ini are the same file, and
 * nothing has to write both.
 *
 * Worth stating because the sibling case looks identical and is not: the
 * client's old retroclaude.ini is ELEVEN characters, has no 8.3 spelling,
 * and an ISO can only carry RETROCL.INI -- a different file, which
 * GetPrivateProfileString does not find, so every key reads back empty and
 * the first thing anyone sees is a complaint about the port. The rule is
 * "longer than 8.3 needs the long name written out", not "ini files do".
 */
#ifndef RC_REMOTE_CONFIG_H
#define RC_REMOTE_CONFIG_H

#ifndef __cplusplus
#error "remote/config.h is C++"
#endif

#include "core/inifile.h"
#include "core/rc_cpp.h"
#include "remote/allow.h"

struct ServerConfig
{
    char bind[64];          /* dotted address; 0.0.0.0 is every interface */
    long port;
    char secret[128];
    char allowText[64];     /* as written, for the log and the error */
    AllowList allow;        /* as parsed */
    long heartbeatS;        /* 0 disables the drop; see the comment in .cpp */
};

/* The machine gremote is running ON, as opposed to the socket it answers on.
 *
 * One setting so far. It is here rather than in ServerConfig because it is not
 * about the connection at all: it changes how the hardware behaves while this
 * program is up, which is a different kind of claim and wants a different
 * section in the file. "Keep the screen on" and "do not run the screen saver"
 * would join it here if they are ever wanted.
 */
struct MachineConfig
{
    /* Refuse the machine's attempts to suspend itself, and keep its idle
     * timer from expiring. OFF by default, deliberately: it changes how the
     * hardware behaves, and an existing install must come up behaving exactly
     * as it did before this option existed. See remote/tray.h for the three
     * mechanisms and which of them this platform actually has. */
    bool preventSleep;
};

struct ExecConfig
{
    /* The ceiling a caller's timeout_ms is clamped to, and it is CLAMPED
     * rather than refused: a caller asking for longer than the machine
     * allows wants the longest it can have, not an error. The clamp is
     * logged, because an unclamped-looking timeout that fires early is the
     * kind of thing that gets blamed on the network. */
    long maxTimeoutMs;
    /* Bytes handed back before the rest is drained and counted. A remote
     * shell streams rather than buffers, so this is larger than the client's
     * 32KB -- that one is sized for a chat window on a 32MB box. */
    long maxOutputKb;
};

class ServerConfigStore
{
public:
    /* <exeDir>\gremote.ini */
    explicit ServerConfigStore(const char *exeDir);

    /* Defaults, then the file, then the checks that are not a single
     * value's business (an unset secret, an unparseable range). A missing
     * file is written with the defaults so there is something to edit --
     * and that file does not start, which is the intent. */
    bool load(ServerConfig *cfg, ExecConfig *exec, MachineConfig *machine);

    const char *error() const;
    const char *path() const;

private:
    ServerConfigStore(const ServerConfigStore &);
    ServerConfigStore &operator=(const ServerConfigStore &);

    bool fail(const char *why);

    Path m_path;        /* must stay above m_ini: constructed first */
    IniFile m_ini;
    char m_error[256];  /* ours, for the checks IniFile cannot make */
};

#endif
