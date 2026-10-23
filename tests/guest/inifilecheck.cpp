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
    "Bind=0.0.0.0\r\n"
    "\r\n"
    /* The string the OLD has() used as its absent-marker, as a real value.
     * That implementation read the key, got its own sentinel back and called
     * the key absent -- so a value a user had written was silently replaced by
     * a default. It is here as a VALUE because this is the one shape of the
     * 2026-10-01 guest failure that Wine can actually reproduce: both are the
     * same root cause, a lookup that depends on one particular string
     * surviving a round trip through the profile API. */
    "[sentinel]\r\n"
    "old=\001absent\001\r\n";

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
    /* THE CASE WITH TEETH, and the only one here that fails against the
     * implementation this replaced. A value that happens to equal the marker a
     * sentinel-based has() probes with is reported ABSENT by it. Same root cause
     * as the guest failure: a lookup that only works while one particular string
     * survives a round trip (core/inifile.cpp, has()).
     *
     * It means slightly different things on the two platforms, and both are
     * worth having. On Wine the value round-trips, so this is the sentinel
     * COLLISION: the old code read its own marker back and called the key
     * absent. On Windows 98 the value cannot be read at all -- measured, see
     * below -- so there it is the `key=` case in disguise: a key that exists and
     * whose value the API cannot carry is still PRESENT. */
    say("has-a-value-that-looks-like-a-probe",
        ini.has("sentinel", "old"), "old=\\001absent\\001");
    {
        /* WHAT THIS ASSERTS, AND WHY IT IS NOT A ROUND TRIP ANY MORE.
         *
         * It used to require the bytes back, and that FAILED on real Windows 98
         * SE on 2026-10-01 with `(empty)`:
         * GetPrivateProfileStringA returns zero characters for a value holding a
         * control character. Our code was right and the assertion was wrong --
         * it expected the platform to carry something the platform does not
         * carry, in EITHER direction, which is the same fact that made the old
         * sentinel unable to come back and every absent key look present
         * (research/measurements/inifile-absent-key-p200-20261001.txt).
         *
         * So assert the property that actually matters and is true on both: A
         * KEY THAT EXISTS NEVER READS BACK AS THE CALLER'S DEFAULT. Wine hands
         * over the bytes, 98 hands over an empty string, and neither is
         * "fallback" -- while the old implementation returned exactly
         * "fallback", silently substituting a default for a line somebody had
         * written into the file. The teeth are in that comparison, not in the
         * round trip.
         *
         * The detail column carries what was actually read, so a guest run says
         * `(empty)` and a Wine run shows the bytes. The platform difference stays
         * VISIBLE in the output without either platform failing. */
        char out[32];
        bool got = ini.getString("sentinel", "old", "fallback", out,
                                 sizeof out);
        say("string-present-never-reads-back-as-the-default",
            got && strcmp(out, "fallback") != 0,
            out[0] ? out : "(empty)");
    }

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
    {
        /* THE SHAPE THAT STOPPED THE REAL MACHINE on 2026-10-01: a new optional
         * setting read out of a section the installed file does not have at
         * all. ABSENT IS NOT INVALID -- it takes the documented default and is
         * not an error -- or shipping any new key breaks every install that
         * predates it, which on a Pentium 200 means walking over with a floppy.
         * Both polarities, because a default of false going wrong looks
         * identical to a default of false being honoured. */
        bool t = false, f = true;
        bool gotT = ini.getBool("nosuch", "prevent_sleep", true, &t);
        bool gotF = ini.getBool("nosuch", "prevent_sleep", false, &f);
        say("bool-absent-section-takes-the-default",
            gotT && t && gotF && !f, gotT && gotF ? "true, false" : ini.error());
    }
    {
        long v = 0;
        bool got = ini.getInt("nosuch", "port", 7401, 1, 65535, &v);
        say("int-absent-section-takes-the-default", got && v == 7401, "7401");
    }
    {
        char out[32];
        bool got = ini.getString("nosuch", "bind", "0.0.0.0", out, sizeof out);
        say("string-absent-section-takes-the-default",
            got && strcmp(out, "0.0.0.0") == 0, out);
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

    /* --- writes ---------------------------------------------------------- */
    {
        Path wpath;
        writeIni(wpath, "write.ini", "");
        IniFile out(wpath.str());
        bool wrote = out.setString("bridge", "host", "10.0.2.2") &&
                     out.setInt("bridge", "port", 7890) &&
                     out.setInt("ui", "offset", -3) &&
                     out.setBool("ui", "autoscroll", false);
        out.flush();
        /* Read back through a separate reader, as the next launch would. */
        IniFile back(wpath.str());
        char host[32];
        long port = 0, offset = 0;
        bool scroll = true;
        bool read = back.getString("bridge", "host", "", host, sizeof host) &&
                    back.getInt("bridge", "port", 0, 1, 65535, &port) &&
                    back.getInt("ui", "offset", 0, -10, 10, &offset) &&
                    back.getBool("ui", "autoscroll", true, &scroll);
        say("write-then-read-back",
            wrote && read && strcmp(host, "10.0.2.2") == 0 && port == 7890 &&
            offset == -3 && !scroll, host);
        DeleteFileA(wpath.str());
    }
    {
        IniFile bare("gremote.ini");
        say("write-to-bare-name-refused",
            !bare.setString("a", "b", "c"), bare.error());
    }
    {
        /* A directory that does not exist: the write must fail and say so,
         * not report success for a value that went nowhere. */
        Path missing;
        char dir[MAX_PATH];
        GetCurrentDirectoryA(sizeof dir, dir);
        missing.join(dir, "no-such-dir\\x.ini");
        IniFile nowhere(missing.str());
        say("write-failure-reported",
            !nowhere.setInt("a", "b", 1), nowhere.error());
    }

    DeleteFileA(path.str());
    return 0;
}
