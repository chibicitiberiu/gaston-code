/* tooldet.cpp -- see app/tooldet.h. */
#include <windows.h>
#include <string.h>

#include "app/tooldet.h"
#include "core/log.h"
#include "core/str.h"

/* Section names come back from GetPrivateProfileStringA in one block. 32
 * sections at up to 32 characters plus terminators does not reach 1500;
 * 2048 leaves room and is checked rather than assumed. FILE-STATIC, not on
 * the stack: with a MAX_PATH value buffer and a MAX_PATH path buffer beside
 * it this frame would be close to the 4096-byte gate `make check` enforces,
 * and that gate exists because a frame past a page steps over the guard. */
static char g_sections[2048];

/* --- reading the INI ------------------------------------------------------
 *
 * `GetPrivateProfileStringA(NULL, NULL, ...)` enumerates section names. Two
 * things about it are easy to get wrong and both fail quietly:
 *
 *   * the block is a run of NUL-separated names ending in a DOUBLE NUL, not
 *     newlines, so it needs its own walker;
 *   * when the buffer is too small it TRUNCATES, appends two NULs, and
 *     returns `nSize - 2`. There is no error. A section lost this way is a
 *     false negative -- the expensive direction, because the whole point of
 *     this module is to stop the assistant going and looking.
 *
 * Hence the explicit comparison against `cap - 2` below.
 */
static int read_sections(const char *ini, char *out, unsigned cap)
{
    DWORD n = GetPrivateProfileStringA(NULL, NULL, "", out, cap, ini);
    if (n == 0) {
        out[0] = '\0';
        out[1] = '\0';
        return 0;
    }
    if (n >= cap - 2) {
        rc_log("tooldet: %s has more sections than %u bytes hold; some were "
               "not read", ini, cap);
        return 0;                       /* refuse a truncated list outright */
    }
    return 1;
}

/* --- the scan ------------------------------------------------------------- */

namespace {

struct scan {
    rc_tooldet_sink sink;
    void           *ctx;
    int             emitted;
    int             stop;
};

int emit(scan *s, const char *name, const char *kind, const char *value)
{
    if (s->stop) return 0;
    if (!s->sink(s->ctx, name, kind, value)) {
        s->stop = 1;
        return 0;
    }
    s->emitted++;
    return 1;
}

/* One section's worth of questions, against one INI. */
void do_section(scan *s, const char *ini, const char *name)
{
    char key[16];
    char val[MAX_PATH];
    int i;

    for (i = 1; i <= RC_TOOLDET_MAX_N && !s->stop; i++) {
        char env[MAX_PATH];
        char line[MAX_PATH + 40];
        rc_strf(key, sizeof(key), "env%d", i);
        GetPrivateProfileStringA(name, key, "", val, sizeof(val), ini);
        if (!val[0]) continue;
        /* The VALUE of the variable, not merely that it is set: `WATCOM` and
         * `INCLUDE` are the two that say where a toolchain actually lives,
         * and an empty one is as informative as an absent one. */
        if (!GetEnvironmentVariableA(val, env, sizeof(env))) continue;
        if (!env[0]) continue;
        if (rc_strf(line, sizeof(line), "%s=%s", val, env))
            emit(s, name, "env", line);
    }

    for (i = 1; i <= RC_TOOLDET_MAX_N && !s->stop; i++) {
        rc_strf(key, sizeof(key), "file%d", i);
        GetPrivateProfileStringA(name, key, "", val, sizeof(val), ini);
        if (!val[0]) continue;
        if (GetFileAttributesA(val) == 0xFFFFFFFF) continue;
        emit(s, name, "file", val);
    }
}

void do_ini(scan *s, const char *ini)
{
    const char *p;
    int n = 0;

    if (GetFileAttributesA(ini) == 0xFFFFFFFF) return;
    if (!read_sections(ini, g_sections, sizeof(g_sections))) return;

    for (p = g_sections; *p && !s->stop; p += strlen(p) + 1) {
        if (++n > RC_TOOLDET_MAX_SECTIONS) {
            rc_log("tooldet: %s has more than %d sections; stopped at [%s]",
                   ini, RC_TOOLDET_MAX_SECTIONS, p);
            return;
        }
        do_section(s, ini, p);
    }
}

}  /* namespace */

int rc_tooldet_run(const char *exe_dir, rc_tooldet_sink sink, void *ctx)
{
    char ini[MAX_PATH];
    char path[MAX_PATH];
    scan s;
    UINT old;

    if (!sink) return -1;
    s.sink = sink; s.ctx = ctx; s.emitted = 0; s.stop = 0;

    /* NO CRITICAL-ERROR DIALOGS, for the whole scan.
     *
     * The paths come from an INI a user can edit, so one of them can name a
     * drive with nothing in it. On Win9x a `GetFileAttributesA` against an
     * empty floppy raises INT 24h and Windows puts up a system-modal "There
     * is no disk in drive A:" -- owned by nothing visible, and this runs on
     * the thread that pumps the message queue, so the app would be frozen
     * behind a dialog nobody can attribute to it.
     *
     * Restored afterwards rather than left set: this is our preference for
     * one scan, not a change to how the process behaves. */
    old = SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX);

    /* PATH unconditionally, because every rule on the bridge wants it and no
     * section has to ask. It is the one variable that says where a toolchain
     * was wired up even when its own variable was never set. */
    if (GetEnvironmentVariableA("PATH", path, sizeof(path)) && path[0]) {
        char line[MAX_PATH + 8];
        if (rc_strf(line, sizeof(line), "PATH=%s", path))
            emit(&s, "machine", "env", line);
    }

    /* OURS FIRST, THE USER'S SECOND, and the order is the point: a section
     * in `tools.ini` is read after the shipped one, so the bridge sees the
     * user's answer last and can prefer it. */
    if (rc_strf(ini, sizeof(ini), "%s\\tools_detect.ini", exe_dir))
        do_ini(&s, ini);
    if (rc_strf(ini, sizeof(ini), "%s\\tools.ini", exe_dir))
        do_ini(&s, ini);

    SetErrorMode(old);
    rc_log("tooldet: %d fact(s)", s.emitted);
    return s.emitted;
}

/* --- the cached form ------------------------------------------------------ */

/* Room for the rows. Sized from the caps rather than guessed: the longest
 * row this can produce is a section name, a kind, a length and a MAX_PATH
 * value, which is under 320 bytes -- so this holds a comfortable 24 of them
 * and refuses the rest loudly. 2KB of BSS on a machine with 28KB free is a
 * real cost and is why there is no array of fixed-size records anywhere in
 * this module: rows are written once, here, and never held twice. */
static char g_rows[2048];
static unsigned g_rows_len;
static int g_done;

namespace {

/* `<name> <kind> <len> <value>\n`, the shape `rc_sync_manifest_row` uses and
 * for the same reason: the value is free-form and may contain spaces
 * (`C:\Program Files\...`), so its byte count is stated rather than inferred
 * from a separator that can appear inside it. */
int row_sink(void *ctx, const char *name, const char *kind, const char *value)
{
    char row[MAX_PATH + 80];
    unsigned n;
    (void)ctx;

    if (!rc_strf(row, sizeof(row), "%s %s %u %s\n",
                 name, kind, (unsigned)strlen(value), value)) {
        rc_log("tooldet: row for [%s] does not fit; dropped", name);
        return 1;                       /* skip this one, keep going */
    }
    n = (unsigned)strlen(row);
    if (g_rows_len + n >= sizeof(g_rows)) {
        rc_log("tooldet: out of room at [%s]; %u bytes reported, rest dropped",
               name, g_rows_len);
        return 0;                       /* stop: a partial list is honest,
                                         * a truncated row is not */
    }
    memcpy(g_rows + g_rows_len, row, n);
    g_rows_len += n;
    g_rows[g_rows_len] = '\0';
    return 1;
}

}  /* namespace */

const char *rc_tooldet_cached(const char *exe_dir, unsigned *len)
{
    if (!g_done) {
        g_done = 1;
        g_rows_len = 0;
        g_rows[0] = '\0';
        rc_tooldet_run(exe_dir, row_sink, NULL);
    }
    if (len) *len = g_rows_len;
    return g_rows;
}
