/* view.tpl: forty short strings, one heap buffer, and the substituter.
 *
 * WHERE THEY LIVE, because all three obvious shapes fail a gate:
 *
 *   static Buf<256> g_tpl[40]   a non-trivial constructor, and `make check`
 *                               fails any static constructor outright -- it
 *                               runs before WinMain and therefore before
 *                               the log exists.
 *   char g_tpl[40][256]         10 KB of BSS, more than twice the image
 *                               slack stage 2 left, against tooldet.cpp's
 *                               measured "2KB of BSS on a machine with 28KB
 *                               free is a real cost".
 *   a local table during load   over stackframes.py's 4,096-byte limit.
 *
 * So: one heap buffer with an offset per key, which is core/textlist.h's
 * shape and was written for exactly this. Re-reading per emit is not an
 * option either -- IniFile::has makes two profile calls and getString a
 * third, on an API that opens and closes the file each time, inside a path
 * budgeted at 5ms per 4KB update.
 *
 * WHAT IniFile CANNOT TELL US, verified against win/core/inifile.cpp rather
 * than assumed. The stage-3 plan assumed it could do all three:
 *
 *   * A MISSING FILE IS INVISIBLE. ok() only checks the path has a
 *     separator, so every getString succeeds with the caller's default and
 *     there is no error() and no key to name. Hence the GetFileAttributesA
 *     below.
 *   * AN EMPTIED VALUE IS A SUCCESS. `para=` reads back present-and-empty
 *     (inifilecheck.cpp:46,170-174), so one stray edit would render every
 *     paragraph as nothing, with no log line. Hence the non-empty pass.
 *   * A MISTYPED SECTION PASSES checkKeys, which enumerates the section's
 *     keys and finds none. The all-present pass closes that too.
 *
 * checkKeys DOES catch the other direction -- an unknown key -- and because
 * it fails the FILE, one typo degrades the whole appearance rather than one
 * block. Worth stating plainly rather than implying it costs one block.
 *
 * `version` is itself a key and is in KEYS, or the shipped file would fail
 * checkKeys on every boot.
 *
 * AND IT MUST BE MEASURED ON THE GUEST. inifile.cpp:50-107 is 58 lines
 * about this exact API: GetPrivateProfileStringA on Windows 98 "will
 * neither hand a control-character default BACK nor READ one out of a
 * file... WINE HANDS IT BACK INTACT, which is why every test passed on
 * three machines and the guest fell over on the first one." A template file
 * is that round trip, carrying quotes, semicolons, equals signs, angle
 * brackets and significant internal spaces. rendercheck --dump-templates
 * exists so the comparison is made on the VM and on the Athlon before the
 * equality proof is trusted.
 */
#include "view/render.h"
#include "view/templates.h"

#include "core/inifile.h"
#include "core/log.h"
#include "core/rc_cpp.h"
#include "core/str.h"

#include <windows.h>
#include <stdlib.h>
#include <string.h>

/* One row per key, in TplKey order. The order is the enum's and the
 * agreement test asserts the two match, so a key added to one and not the
 * other fails loudly instead of rendering as nothing. */
static const char *const KEYS[TPL_COUNT] = {
    "version",
    "para", "heading", "quote", "rule",
    "bullet", "numbered", "item",
    "code", "lang", "pre",
    "table", "row", "cell", "headcell",
    "user", "notice", "error", "thinking", "status",
    "tool", "toolresult", "toolresult_err", "clip",
    "perm", "perm_plain", "permhead_ask", "permhead_view",
    "permlead", "permlead_desc", "permwarn", "permprev", "permpara",
    "permsteps", "permstep", "permtable", "permrow",
    "exporthead", "exporthead_title", "exporthead_when"
};

/* The last resort. Every block as escaped text in a paragraph: ugly, and
 * legible, which is the whole requirement. Not ~80 bytes, as the plan first
 * guessed -- with the log format, the notice and the status-bar message the
 * literals alone exceed that. */
static const char FALLBACK_WRAP[] = "<p>{}</p>";

/* The one per-value bound. 256 rather than a computed number so that the
 * refusal is a stated limit a person can design against, and because
 * IniFile's own per-value limit is cap-1 for whatever cap it is passed. */
#define TPL_VALUE_CAP 256

/* Everything together. ~40 keys of ~40 bytes is under 2KB; the cap refuses
 * a file that has grown into something else. */
#define TPL_BUF_CAP 8192

static char    *g_buf;               /* one allocation, NUL-separated */
static unsigned g_off[TPL_COUNT];    /* offset into g_buf, or NONE */
static unsigned g_len;
static bool     g_degraded = true;   /* until a load says otherwise */
static int      g_version;

#define TPL_NONE 0xFFFFFFFFu

const char *rc_tpl(TplKey k)
{
    if (g_buf && k < TPL_COUNT && g_off[k] != TPL_NONE)
        return g_buf + g_off[k];
    /* Degraded: everything is a paragraph of escaped text. `item` has to
     * stay an <li> or a list becomes paragraphs inside a <ul>, which IE5
     * renders as an empty bullet followed by the text. */
    if (k == TPL_ITEM)
        return "<li>{}</li>";
    return FALLBACK_WRAP;
}

bool rc_tpl_degraded()
{
    return g_degraded;
}

int rc_tpl_version()
{
    return g_version;
}

static void reset()
{
    unsigned i;
    if (g_buf)
    {
        HeapFree(GetProcessHeap(), 0, g_buf);
        g_buf = 0;
    }
    for (i = 0; i < TPL_COUNT; i++)
        g_off[i] = TPL_NONE;
    g_len = 0;
    g_version = 0;
    g_degraded = true;
}

bool rc_tpl_load(const char *exeDir, char *err, unsigned errCap)
{
    Path path;
    unsigned i;

    reset();
    if (err && errCap)
        err[0] = '\0';

    if (!path.join(exeDir, "view.tpl") || !path.ok())
    {
        rc_strf(err, errCap, "the path to view.tpl does not fit");
        return false;
    }

    /* IniFile cannot see this; see the header comment. */
    if (GetFileAttributesA(path.str()) == 0xFFFFFFFF)
    {
        rc_strf(err, errCap, "%s is not there", path.str());
        return false;
    }

    IniFile ini(path.str());
    if (!ini.ok())
    {
        rc_strf(err, errCap, "%s is not a usable path", path.str());
        return false;
    }

    if (!ini.checkKeys("markup", KEYS, TPL_COUNT))
    {
        /* Fails the FILE, not the key: one typo degrades the whole
         * appearance. Said out loud because the plan implied it cost one
         * block. */
        rc_strf(err, errCap, "%s", ini.error());
        return false;
    }

    g_buf = (char *)HeapAlloc(GetProcessHeap(), 0, TPL_BUF_CAP);
    if (!g_buf)
    {
        rc_strf(err, errCap, "no memory for the templates");
        return false;
    }

    for (i = 0; i < TPL_COUNT; i++)
    {
        char value[TPL_VALUE_CAP];
        unsigned n;

        if (!ini.getString("markup", KEYS[i], "", value, sizeof value))
        {
            rc_strf(err, errCap, "%s", ini.error());
            reset();
            return false;
        }
        if (value[0] == '\0')
        {
            /* Present-and-empty reads as a success. One stray edit would
             * otherwise render every paragraph as nothing, silently. */
            rc_strf(err, errCap, "[markup] %s is missing or empty",
                    KEYS[i]);
            reset();
            return false;
        }
        n = (unsigned)strlen(value);
        if (g_len + n + 1 > TPL_BUF_CAP)
        {
            rc_strf(err, errCap, "the templates exceed %d bytes",
                    TPL_BUF_CAP);
            reset();
            return false;
        }
        memcpy(g_buf + g_len, value, n + 1);
        g_off[i] = g_len;
        g_len += n + 1;
    }

    g_version = (int)strtol(g_buf + g_off[TPL_VERSION], 0, 10);
    g_degraded = false;
    return true;
}

bool rc_tpl_dump(ByteSink &out)
{
    unsigned i;
    for (i = 0; i < TPL_COUNT; i++)
    {
        const char *v = rc_tpl((TplKey)i);
        if (!out.write(KEYS[i], (unsigned)strlen(KEYS[i]))
            || !out.write("\t", 1)
            || !out.write(v, (unsigned)strlen(v))
            || !out.write("\n", 1))
            return false;
    }
    return true;
}

/* --- the substituter ---------------------------------------------------
 *
 * Byte by byte, and it NEVER hands a loaded value to rc_strf. sprintf is
 * banned and rc_strf is the only formatter in this tree, and
 * win/app/statusbar.cpp:148 is the precedent for building exactly this
 * markup WITH a format string -- so a designer writing `width:100%` into
 * view.tpl would otherwise be an arbitrary format directive against a
 * mismatched argument list.
 */

static bool emitRange(const char *s, unsigned n, const TplValues *v,
                      ByteSink &out)
{
    unsigned i = 0;
    unsigned run = 0;

    while (i < n)
    {
        if (s[i] == '{')
        {
            unsigned j = i + 1;
            while (j < n && s[j] != '}')
                j++;
            if (j < n)
            {
                unsigned k;
                if (run && !out.write(s + i - run, run))
                    return false;
                run = 0;
                for (k = 0; v && k < v->n; k++)
                {
                    unsigned nameLen = (unsigned)strlen(v->name[k]);
                    if (nameLen == j - i - 1
                        && memcmp(s + i + 1, v->name[k], nameLen) == 0)
                    {
                        if (!out.write(v->value[k],
                                       (unsigned)strlen(v->value[k])))
                            return false;
                        break;
                    }
                }
                /* A hole nothing fills writes nothing. Not an error: a
                 * designer who removes {dropped} from `clip` gets a clip
                 * marker with no number, which is a worse marker and still
                 * a transcript. */
                i = j + 1;
                continue;
            }
            /* An unclosed brace is a literal brace. */
        }
        run++;
        i++;
    }
    return run == 0 || out.write(s + n - run, run);
}

/* Where the children hole is, or the length when there is none. */
static unsigned holeAt(const char *s, unsigned *outLen)
{
    unsigned n = (unsigned)strlen(s);
    unsigned i;
    *outLen = n;
    for (i = 0; i + 1 < n; i++)
        if (s[i] == '{' && s[i + 1] == '}')
            return i;
    return n;
}

bool rc_tpl_open(TplKey k, const TplValues *v, ByteSink &out)
{
    const char *s = rc_tpl(k);
    unsigned n, at = holeAt(s, &n);
    return emitRange(s, at, v, out);
}

bool rc_tpl_close(TplKey k, const TplValues *v, ByteSink &out)
{
    const char *s = rc_tpl(k);
    unsigned n, at = holeAt(s, &n);
    if (at >= n)
        return true;                 /* no hole: open() wrote the lot */
    return emitRange(s + at + 2, n - at - 2, v, out);
}

bool rc_tpl_whole(TplKey k, const TplValues *v, ByteSink &out)
{
    const char *s = rc_tpl(k);
    return emitRange(s, (unsigned)strlen(s), v, out);
}
