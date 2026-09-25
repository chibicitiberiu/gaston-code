/* Does IniFile refuse what it says it refuses?
 *
 * Runs the real `win/core/inifile.cpp` against INI files it writes into the
 * current directory, and prints one `verdict<TAB>case<TAB>detail` row per
 * case for tests/python/guest/test_guest_inifile.py to diff.
 *
 * The cases that matter most are the ones GetPrivateProfileString gets
 * wrong on its own: a bare filename read from C:\WINDOWS, an inline comment
 * that becomes part of the value, and a value exactly one byte too long for
 * its buffer, which the API reports identically to one that fits.
 */
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include "core/inifile.h"

static void say(const char *name, bool pass, const char *detail)
{
    printf("%s\t%s\t%s\n", pass ? "pass" : "FAIL", name, detail);
}

static bool writeIni(Path &out, const char *name, const char *body)
{
    char dir[MAX_PATH];
    DWORD n = GetCurrentDirectoryA(sizeof dir, dir);
    if (n == 0 || n >= sizeof dir)
        return false;
    if (!out.join(dir, name))
        return false;
    Handle file(CreateFileA(out.str(), GENERIC_WRITE, 0, 0, CREATE_ALWAYS,
                            FILE_ATTRIBUTE_NORMAL, 0));
    if (!file.valid())
        return false;
    DWORD wrote = 0;
    BOOL good = WriteFile(file.get(), body, (DWORD)strlen(body), &wrote, 0);
    return good && wrote == strlen(body);
}

static const char BODY[] =
    "[server]\r\n"
    "port=7401\r\n"
    "negative=-5\r\n"
    "big=99999999999\r\n"
    "garbage=12abc\r\n"
    "hex=0x10\r\n"
    "blank=\r\n"
    "commented=7401 ; the client uses 7890\r\n"
    "secret=hunter2\r\n"
    "seven=1234567\r\n"
    "eight=12345678\r\n"
    "on=1\r\n"
    "off=0\r\n"
    "yes=yes\r\n"
    "\r\n"
    "[strict]\r\n"
    "port=1\r\n"
    "prot=2\r\n"
    "\r\n"
    "[clean]\r\n"
    "port=1\r\n"
    "Bind=0.0.0.0\r\n";

int main(void)
{
    Path path;
    if (!writeIni(path, "check.ini", BODY))
    {
        say("setup", false, "could not write check.ini");
        return 1;
    }
    IniFile ini(path.str());
    say("full-path-ok", ini.ok(), ini.path());

    /* --- paths ---------------------------------------------------------- */
    {
        IniFile bare("gremote.ini");
        say("bare-name-refused", !bare.ok(), "would read C:\\WINDOWS");
    }
    {
        char many[MAX_PATH + 20];
        memset(many, 'd', sizeof many - 1);
        many[0] = 'C'; many[1] = ':'; many[2] = '\\';
        many[sizeof many - 1] = '\0';
        IniFile tooLong(many);
        say("overlong-path-refused", !tooLong.ok(), "MAX_PATH+20");
    }

    /* --- presence ------------------------------------------------------- */
    say("has-present", ini.has("server", "port"), "port");
    say("has-empty-value", ini.has("server", "blank"), "blank=");
    say("has-absent", !ini.has("server", "nope"), "nope");
    say("has-absent-section", !ini.has("nosuch", "port"), "[nosuch]");

    /* --- strings -------------------------------------------------------- */
    {
        char out[32];
        bool got = ini.getString("server", "secret", "x", out, sizeof out);
        say("string-read", got && strcmp(out, "hunter2") == 0, out);
    }
    {
        char out[32];
        bool got = ini.getString("server", "nope", "fallback", out, sizeof out);
        say("string-absent-default", got && strcmp(out, "fallback") == 0, out);
    }
    {
        /* Seven characters in an eight-byte buffer is an exact fit. */
        char out[8];
        bool got = ini.getString("server", "seven", "", out, sizeof out);
        say("string-exact-fit", got && strcmp(out, "1234567") == 0, out);
    }
    {
        /* Eight is one over, and the API alone cannot tell it from seven. */
        char out[8];
        bool got = ini.getString("server", "eight", "d", out, sizeof out);
        say("string-one-over-refused",
            !got && strcmp(out, "d") == 0 && strstr(ini.error(), "eight"),
            ini.error());
    }
    {
        char out[4];
        bool got = ini.getString("server", "nope", "toolong", out, sizeof out);
        say("default-too-long-refused", !got, ini.error());
    }
    {
        char out[32];
        bool got = ini.getString("server", "blank", "d", out, sizeof out);
        say("string-empty-value", got && out[0] == '\0', "blank=");
    }

    /* --- integers ------------------------------------------------------- */
    {
        long v = 0;
        bool got = ini.getInt("server", "port", 1, 1, 65535, &v);
        say("int-read", got && v == 7401, "7401");
    }
    {
        long v = 0;
        bool got = ini.getInt("server", "nope", 42, 1, 65535, &v);
        say("int-absent-default", got && v == 42, "42");
    }
    {
        long v = 0;
        bool got = ini.getInt("server", "negative", 0, -10, 10, &v);
        say("int-negative", got && v == -5, "-5");
    }
    {
        long v = 0;
        bool got = ini.getInt("server", "port", 1, 1, 1024, &v);
        say("int-out-of-range-refused", !got && v == 1, ini.error());
    }
    {
        long v = 0;
        bool got = ini.getInt("server", "big", 3, 0, 0x7fffffffL, &v);
        say("int-overflow-refused", !got && v == 3, ini.error());
    }
    {
        long v = 0;
        bool got = ini.getInt("server", "garbage", 3, 0, 100000, &v);
        say("int-trailing-garbage-refused", !got && v == 3, ini.error());
    }
    {
        long v = 0;
        bool got = ini.getInt("server", "hex", 3, 0, 100000, &v);
        say("int-hex-refused", !got, ini.error());
    }
    {
        long v = 0;
        bool got = ini.getInt("server", "blank", 3, 0, 100000, &v);
        say("int-empty-refused", !got, ini.error());
    }
    {
        long v = 0;
        bool got = ini.getInt("server", "commented", 3, 1, 65535, &v);
        say("int-inline-comment-refused", !got, ini.error());
    }

    /* --- booleans ------------------------------------------------------- */
    {
        bool a = false, b = true;
        bool gotA = ini.getBool("server", "on", false, &a);
        bool gotB = ini.getBool("server", "off", true, &b);
        say("bool-read", gotA && gotB && a && !b, "on=1 off=0");
    }
    {
        bool v = true;
        bool got = ini.getBool("server", "yes", true, &v);
        say("bool-word-refused", !got && v, ini.error());
    }
    {
        bool v = false;
        bool got = ini.getBool("server", "nope", true, &v);
        say("bool-absent-default", got && v, "true");
    }

    /* --- unknown keys --------------------------------------------------- */
    {
        static const char *const known[] = { "port", "bind" };
        bool got = ini.checkKeys("strict", known, 2);
        say("unknown-key-refused", !got && strstr(ini.error(), "prot") != 0,
            ini.error());
    }
    {
        /* Case-insensitive, as the profile API itself is. */
        static const char *const known[] = { "port", "bind" };
        bool got = ini.checkKeys("clean", known, 2);
        say("known-keys-pass", got, got ? "port, Bind" : ini.error());
    }
    {
        static const char *const known[] = { "port" };
        bool got = ini.checkKeys("nosuch", known, 1);
        say("absent-section-passes", got, "[nosuch]");
    }

    DeleteFileA(path.str());
    return 0;
}
