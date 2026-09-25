#include "core/inifile.h"

#include <errno.h>
#include <stdlib.h>
#include <string.h>

/* What `has()` asks for as the default. No real value starts with a control
 * character, so getting this back means the key is absent. */
static const char ABSENT[] = "\001absent\001";

/* Longest integer text accepted, sign included. A long is at most 11. */
static const unsigned INT_TEXT = 16;

/* Key list for checkKeys(). Enough for a section of a few dozen keys; a
 * section that overflows it is reported, not truncated into a false pass. */
static const unsigned KEY_LIST = 2048;

IniFile::IniFile(const char *path)
    : m_ok(false)
{
    m_path.set(path ? path : "");
    m_ok = m_path.ok() && !m_path.empty() &&
           (strchr(m_path.str(), '\\') != 0 || strchr(m_path.str(), '/') != 0);
}

bool IniFile::ok() const
{
    return m_ok;
}

const char *IniFile::path() const
{
    return m_path.str();
}

const char *IniFile::error() const
{
    return m_error.str();
}

bool IniFile::fail(const char *section, const char *key, const char *why)
{
    m_error.clear();
    rc_strf(m_error.raw(), m_error.cap(), "[%s] %s: %s", section, key, why);
    return false;
}

bool IniFile::has(const char *section, const char *key) const
{
    char probe[sizeof ABSENT + 1];
    if (!m_ok)
        return false;
    GetPrivateProfileStringA(section, key, ABSENT, probe, sizeof probe,
                             m_path.str());
    return strcmp(probe, ABSENT) != 0;
}

bool IniFile::getString(const char *section, const char *key,
                        const char *def, char *out, unsigned cap)
{
    if (!out || cap == 0)
        return fail(section, key, "no output buffer");
    if (!rc_strcpy(out, cap, def ? def : ""))
        return fail(section, key, "default does not fit its buffer");
    if (!m_ok)
        return fail(section, key, "no usable ini path");
    if (!has(section, key))
        return true;

    /* One byte more than the caller's buffer. GetPrivateProfileString
     * returns cap-1 both for a value that exactly fits and for one it cut
     * short, so reading into cap+1 is the only way to tell them apart. */
    UniqueArr<char> tmp(new char[cap + 1]);
    if (!tmp.valid())
        return fail(section, key, "out of memory");
    DWORD n = GetPrivateProfileStringA(section, key, "", tmp.get(), cap + 1,
                                       m_path.str());
    if (n >= cap)
        return fail(section, key, "value too long");
    rc_strcpy(out, cap, tmp.get());
    return true;
}

bool IniFile::getInt(const char *section, const char *key, long def,
                     long lo, long hi, long *out)
{
    char text[INT_TEXT];
    char *end;
    long value;

    if (out)
        *out = def;
    if (!out)
        return fail(section, key, "no output");
    if (!has(section, key))
        return m_ok ? true : fail(section, key, "no usable ini path");
    if (!getString(section, key, "", text, sizeof text))
        return fail(section, key, "not a whole number");

    /* Decimal digits with an optional leading minus, nothing else. strtol
     * alone would take "12abc" as 12 and "  7" as 7, and " 0x10" as 0. */
    const char *p = text;
    if (*p == '-')
        p++;
    if (!*p)
        return fail(section, key, "not a whole number");
    for (; *p; p++)
    {
        if (*p < '0' || *p > '9')
            return fail(section, key, "not a whole number");
    }
    errno = 0;
    value = strtol(text, &end, 10);
    if (errno == ERANGE)
        return fail(section, key, "out of range");
    if (value < lo || value > hi)
    {
        char why[64];
        rc_strf(why, sizeof why, "out of range %ld..%ld", lo, hi);
        return fail(section, key, why);
    }
    *out = value;
    return true;
}

bool IniFile::getBool(const char *section, const char *key, bool def,
                      bool *out)
{
    long value;
    if (out)
        *out = def;
    if (!out)
        return fail(section, key, "no output");
    if (!getInt(section, key, def ? 1 : 0, 0, 1, &value))
        return fail(section, key, "must be 0 or 1");
    *out = value != 0;
    return true;
}

bool IniFile::setString(const char *section, const char *key,
                        const char *value)
{
    if (!m_ok)
        return fail(section, key, "no usable ini path");
    if (!WritePrivateProfileStringA(section, key, value ? value : "",
                                    m_path.str()))
    {
        char why[48];
        rc_strf(why, sizeof why, "write failed, error %lu", GetLastError());
        return fail(section, key, why);
    }
    return true;
}

bool IniFile::setInt(const char *section, const char *key, long value)
{
    char text[INT_TEXT];
    rc_strf(text, sizeof text, "%ld", value);
    return setString(section, key, text);
}

bool IniFile::setBool(const char *section, const char *key, bool value)
{
    return setString(section, key, value ? "1" : "0");
}

void IniFile::flush()
{
    if (m_ok)
        WritePrivateProfileStringA(0, 0, 0, m_path.str());
}

bool IniFile::checkKeys(const char *section, const char *const *known,
                        unsigned count)
{
    if (!m_ok)
        return fail(section, "*", "no usable ini path");

    UniqueArr<char> list(new char[KEY_LIST]);
    if (!list.valid())
        return fail(section, "*", "out of memory");

    /* NULL key: every key in the section, NUL-separated, double-NUL at the
     * end. Returns size-2 when the list did not fit. */
    DWORD n = GetPrivateProfileStringA(section, 0, "", list.get(), KEY_LIST,
                                       m_path.str());
    if (n >= KEY_LIST - 2)
        return fail(section, "*", "too many keys to check");

    bool good = true;
    for (const char *k = list.get(); *k && good; k += strlen(k) + 1)
    {
        bool found = false;
        for (unsigned i = 0; i < count && !found; i++)
        {
            if (lstrcmpiA(k, known[i]) == 0)
                found = true;
        }
        if (!found)
            good = fail(section, k, "unknown key");
    }
    return good;
}
