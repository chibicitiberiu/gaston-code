/* A typed, refusing reader over one INI file.
 *
 * The rule it enforces is the handshake's: never guess. A key that is absent
 * takes its default; a key that is PRESENT but wrong -- not a number, out of
 * range, too long for its buffer, or not a key the reader knows -- is an
 * error, and the caller refuses to start rather than run on a value nobody
 * wrote. `error()` names the section, the key and what was wrong, ready to log.
 *
 * Built on GetPrivateProfileStringA, which has two behaviours worth knowing
 * and both are refused here rather than documented:
 *
 *   - A path with no directory in it is looked up in the WINDOWS directory,
 *     not the current one. `gremote.ini` would quietly become
 *     `C:\WINDOWS\gremote.ini`. The constructor takes a full path; `ok()` is
 *     false for anything without a separator.
 *   - Inline comments are NOT stripped. `port=7401 ; client` reads as the
 *     string "7401 ; client", which getInt() rejects as not a number. Comments
 *     go on their own line.
 *
 * Holds no handle and allocates only while reading; the Win32 profile API
 * opens and closes the file on every call.
 */
#ifndef RC_INIFILE_H
#define RC_INIFILE_H

#ifndef __cplusplus
#error "core/inifile.h is C++"
#endif

#include "core/rc_cpp.h"

class IniFile
{
public:
    explicit IniFile(const char *path);

    /* The path fit and names a directory. Check before reading. */
    bool ok() const;
    const char *path() const;

    /* Present at all, even as `key=` with nothing after it. */
    bool has(const char *section, const char *key) const;

    /* Each getter: absent -> the default and true. Present and valid -> the
     * value and true. Present and invalid -> false, `error()` says why, and
     * the output is left holding the default. */
    bool getString(const char *section, const char *key, const char *def,
                   char *out, unsigned cap);
    bool getInt(const char *section, const char *key, long def, long lo,
                long hi, long *out);
    bool getBool(const char *section, const char *key, bool def, bool *out);

    /* False if `section` holds a key not in `known`: a typo such as `prot=`
     * would otherwise leave `port` at its default without a word. */
    bool checkKeys(const char *section, const char *const *known,
                   unsigned count);

    /* The last failure, "[section] key: reason". Empty if none. */
    const char *error() const;

private:
    IniFile(const IniFile &);
    IniFile &operator=(const IniFile &);

    bool fail(const char *section, const char *key, const char *why);

    Path m_path;
    bool m_ok;
    Buf<256> m_error;
};

#endif
