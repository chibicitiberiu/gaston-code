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
 * THE FILENAME IS THE LONG ONE. This joins the exe's directory with the
 * literal "gremote.ini", and on FAT that is not the same file as a
 * GREMOTE.INI written from a DOS prompt: GetPrivateProfileString finds no
 * file, every key reads back empty, and the first thing anyone sees is a
 * complaint about the port. Whatever puts this on the guest writes both
 * names.
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

class ServerConfigStore
{
public:
    /* <exeDir>\gremote.ini */
    explicit ServerConfigStore(const char *exeDir);

    /* Defaults, then the file, then the checks that are not a single
     * value's business (an unset secret, an unparseable range). A missing
     * file is written with the defaults so there is something to edit --
     * and that file does not start, which is the intent. */
    bool load(ServerConfig *cfg);

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
