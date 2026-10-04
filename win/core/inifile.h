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
 *
 * WRITES report failure. On read-only or full media WritePrivateProfileString
 * fails, and a settings dialog that closes as if it had been accepted then
 * reads the old values back on the next launch (audit 01.37). Win9x also
 * caches the last profile file it wrote; flush() pushes it to disk.
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

    /* Present at all, even as `key=` with nothing after it. False for a key
     * whose section is not in the file either.
     *
     * ABSENT IS NOT INVALID, and every getter below depends on this one telling
     * the truth about it: an absent key takes its default and is not an error,
     * which is what lets a new optional setting ship without breaking an
     * install whose file predates it. Getting that wrong stopped a real
     * Windows 98 box from starting on 2026-10-01; inifile.cpp's comment on this
     * function has the mechanism and why Wine could not see it. */
    bool has(const char *section, const char *key) const;

    /* Each getter: absent -> the default and true. Present and valid -> the
     * value and true. Present and invalid -> false, `error()` says why, and
     * the output is left holding the default. */
    bool getString(const char *section, const char *key, const char *def,
                   char *out, unsigned cap);
    bool getInt(const char *section, const char *key, long def, long lo,
                long hi, long *out);
    bool getBool(const char *section, const char *key, bool def, bool *out);

    /* Each setter: true if the value reached the profile API. A caller
     * saving several values should attempt all of them and report once, so
     * a partially writable file gets as much as it can hold. */
    bool setString(const char *section, const char *key, const char *value);
    bool setInt(const char *section, const char *key, long value);
    bool setBool(const char *section, const char *key, bool value);

    /* Write the Win9x profile cache for this file to disk. Returns nothing
     * because it cannot know: with all three arguments null,
     * WritePrivateProfileString "always returns FALSE after flushing the
     * cache, regardless of whether the flush succeeds or fails" (MSDN). A
     * failed write is still caught by the setter that made it. */
    void flush();

    /* Remove one key, or a whole section (WritePrivateProfileString with
     * NULL, which Win95 already had). setString cannot do it: it writes ""
     * for a NULL value, which is a key that is PRESENT and empty. */
    bool deleteKey(const char *section, const char *key);
    bool dropSection(const char *section);

    /* False if `section` holds a key not in `known`: a typo such as `prot=`
     * would otherwise leave `port` at its default without a word. */
    bool checkKeys(const char *section, const char *const *known,
                   unsigned count);

    /* The same, for a section of NUMBERED keys: `add01`..`add32` for each
     * prefix. The profile API cannot hold two keys of one name -- a second
     * `add=` overwrites the first -- so a list in an INI is numbered, and
     * this is how an unknown one is still refused. */
    bool checkNumbered(const char *section, const char *const *prefixes,
                       unsigned count, unsigned max);

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
