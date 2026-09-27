/* The client's settings: gaston.ini beside the exe, read and written
 * through libcore's IniFile.
 *
 * Replaces rc_config and core/ini.cpp (docs/REMOTE.md, C3). The difference
 * that matters: a value that is present but wrong -- a port that is not a
 * number, a key nobody reads -- now REFUSES startup with a message naming it,
 * where GetPrivateProfileIntA used to turn `port=78g0` into 78 and connect
 * to the wrong place without a word.
 *
 * Not the registry: an INI is inspectable with EDIT.COM on the guest itself.
 */
#ifndef RC_CONFIG_H
#define RC_CONFIG_H

#ifndef __cplusplus
#error "app/config.h is C++"
#endif

#include "core/inifile.h"
#include "core/rc_cpp.h"

struct ClientConfig
{
    char host[64];
    long port;
    char secret[128];
    char syncRoot[128];     /* "" disables sync entirely */
    bool autoscroll;
};

class ConfigStore
{
public:
    /* <exeDir>\gaston.ini */
    explicit ConfigStore(const char *exeDir);

    /* Defaults, then the file. A missing file is written with the defaults
     * so there is something to edit. False if a present value is invalid:
     * the caller refuses to start and shows error(). */
    bool load(ClientConfig *cfg);

    /* Every field is attempted even after one fails, so a partially
     * writable file gets as much as it can hold; false if any failed. */
    bool save(const ClientConfig &cfg);

    const char *error() const;
    const char *path() const;

private:
    ConfigStore(const ConfigStore &);
    ConfigStore &operator=(const ConfigStore &);

    Path m_path;     /* must stay above m_ini: initialised first */
    IniFile m_ini;
};

#endif
