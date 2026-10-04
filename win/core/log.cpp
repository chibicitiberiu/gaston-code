#include <windows.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include "core/log.h"
#include "core/str.h"
#include "core/rc_cpp.h"

static HANDLE g_fh = INVALID_HANDLE_VALUE;
static CRITICAL_SECTION g_lock;
static int g_ready = 0;
static char g_path[MAX_PATH];
/* A literal from the caller, kept for the exiting banner. Never freed. */
static const char *g_program = "?";

/* What rotate() did, which rc_log_open then reports in the new log. */
enum { ROT_NOTHING = 0, ROT_DONE = 1 };

/* Format "<stamp> <text>\r\n" into `out`, which is `cap` bytes INCLUDING the
 * terminator. Returns the number of bytes to write.
 *
 * ONE buffer and therefore ONE WriteFile. The stamp and the text used to be
 * two buffers written in two calls, which on a process that dies mid-line can
 * leave a timestamp with nothing after it -- and this file's whole job is to
 * be intact after the thing it was describing stopped.
 */
static DWORD rc_log_format(char *out, unsigned cap, const char *fmt, va_list ap)
{
    SYSTEMTIME st;
    unsigned head;
    int n;

    GetLocalTime(&st);
    rc_strf(out, cap, "%02d:%02d:%02d.%03d ",
            st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
    head = (unsigned)strlen(out);

    /* Terminate BEFORE measuring, which is the whole point of rc_strf and was
     * skipped here -- the one place that still called _vsnprintf directly.
     *
     * _vsnprintf returns negative when the result did not fit, and in that
     * case it has not written a NUL anywhere. The old fallback was
     * `n = strlen(line)`, which then scanned past whatever had been written,
     * through the rest of `line` and on into adjacent stack, until it met a
     * zero byte. An over-long log line is a stack over-read in the logger --
     * in the function whose whole job is to be working when nothing else is.
     *
     * The three bytes held back are the CRLF and the NUL. */
    n = _vsnprintf(out + head, cap - head - 3, fmt, ap);
    out[cap - 3] = '\0';
    if (n < 0 || (unsigned)n > cap - head - 3)
        n = (int)strlen(out + head);
    out[head + n] = '\r';
    out[head + n + 1] = '\n';
    return (DWORD)(head + n + 2);
}

/* Write one formatted line to the open handle. 0 if there was none.
 *
 * THE HANDLE IS READ UNDER THE LOCK, and that is the fix.
 *
 * It used to be tested once at the top of rc_log, unlocked, and closed in
 * rc_log_close, also unlocked. A thread past that test could reach WriteFile
 * after the close had happened -- and Win9x recycles handle values
 * aggressively, so the write does not fail, it lands in whatever file got that
 * number next. net.cpp documents the identical hazard for the socket (finding
 * 01.52) and removes it by ordering against a join; there is no join to order
 * against here, so the read lives inside the section. (mem-audit F6) */
static int emit_open(const char *line, DWORD n)
{
    DWORD written;

    ScopedLockIf lock(&g_lock, g_ready);
    if (g_fh == INVALID_HANDLE_VALUE)
        return 0;
    WriteFile(g_fh, line, n, &written, NULL);
    /* Flush every line, and it stays INSIDE the lock deliberately.
     *
     * A buffered log loses exactly the lines that matter, the ones written
     * just before the fault, so the flush is not negotiable. Holding four
     * threads behind a physical flush on a 200MHz machine is a real cost and
     * it was tempting to move this below the Leave -- but that needs the
     * handle to stay valid outside the lock, which is precisely the race the
     * block above removes. A complete log beats a fast one; this file is the
     * only debugging channel the guest has. */
    FlushFileBuffers(g_fh);
    return 1;
}

/* Append one formatted line by REOPENING the file, for rc_log_tail. */
static void emit_closed(const char *line, DWORD n)
{
    DWORD written;

    if (!g_path[0])
        return;
    {
        Handle f(CreateFileA(g_path, GENERIC_WRITE, FILE_SHARE_READ, NULL,
                             OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL));
        if (!f.valid())
            return;
        SetFilePointer(f.get(), 0, NULL, FILE_END);
        WriteFile(f.get(), line, n, &written, NULL);
        FlushFileBuffers(f.get());
    }
}

/* Keep the PREVIOUS run: rename <program>.log to <program>.prv before the
 * open truncates it.
 *
 * WHY THIS EXISTS. The first act of recovering a hung machine is a forced
 * restart, and the first act of the restarted program was CREATE_ALWAYS on its
 * own log -- so the lines written on the way down, the only record of the
 * hang, were destroyed by the act of recovering from it. The user's real
 * Pentium hung during shutdown twice and both reboots learned nothing.
 *
 * WHY ONE GENERATION AND A RENAME, and not the two obvious alternatives:
 *
 *   APPENDING with a separator keeps everything, and everything is what a
 *   1998 IDE platter does not have room for. Capping it means asking the size,
 *   then copying the tail of a file forward and truncating -- extra passes and
 *   a temp file, on the one code path that has to work when the disk is full
 *   and the machine is already in trouble. A trim that goes wrong eats the
 *   evidence it was protecting.
 *
 *   N GENERATIONS means N renames in sequence here, each able to fail halfway
 *   and leave a set nobody can order, for a run nobody has ever wanted: the
 *   evidence is read immediately after the bad run, by rebooting, which is
 *   exactly one run back. Two files, one rename.
 *
 * A rename inside one directory moves a directory entry and copies no data, so
 * unlike a copy-then-truncate it needs no free space -- which matters, because
 * a full disk is one of the states this has to survive.
 *
 * `prev` comes back holding the name, for the report in the new log. */
static int rotate(const char *exe_dir, const char *program,
                  char *prev, unsigned cap, DWORD *err)
{
    *err = 0;

    /* .prv, not .log.old: a name with no 8.3 spelling is a DIFFERENT file
     * from DOS and from the installer, which this project has paid for
     * before. <=8 for the stem (every program here is), 3 for the
     * extension. */
    if (!rc_strf(prev, cap, "%s\\%s.prv", exe_dir, program) ||
        strcmp(prev, g_path) == 0)
    {
        /* Truncated, and a truncated .prv can spell the SAME path as the
         * truncated .log -- in which case the delete below would eat the run
         * we came here to keep. Refuse instead. */
        *err = ERROR_INVALID_NAME;
        return ROT_NOTHING;
    }

    if (GetFileAttributesA(g_path) == (DWORD)-1)
        return ROT_NOTHING;         /* no previous run here; nothing to keep */

    /* MoveFile does not overwrite on 9x, so the stale one goes first. Its
     * failure is not interesting on its own -- MoveFile reports it. */
    DeleteFileA(prev);
    if (MoveFileA(g_path, prev))
        return ROT_DONE;

    /* Rotation failed: a read-only directory, a .prv somebody has open, a
     * log the dying copy of this program still holds. Say so and carry on --
     * the open below is what must not be prevented. */
    *err = GetLastError();
    return ROT_NOTHING;
}

static char g_dir[MAX_PATH];

int rc_log_open_early(const char *dir, const char *program)
{
    g_program = (program && *program) ? program : "?";
    rc_strcpy(g_dir, sizeof(g_dir), dir ? dir : ".");
    rc_strf(g_path, sizeof(g_path), "%s\\%s.log", g_dir, g_program);
    if (!g_ready)
    {
        InitializeCriticalSection(&g_lock);
        g_ready = 1;
    }
    g_fh = CreateFileA(g_path, GENERIC_WRITE, FILE_SHARE_READ, NULL,
                       OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (g_fh == INVALID_HANDLE_VALUE) return -1;
    SetFilePointer(g_fh, 0, NULL, FILE_END);
    rc_log("=== %s starting (pid %lu); this run's log begins after the "
           "instance check ===", g_program, (unsigned long)GetCurrentProcessId());
    return 0;
}

int rc_log_begin_run(void)
{
    if (!g_dir[0]) return -1;
    return rc_log_open(g_dir, g_program);
}

int rc_log_open(const char *exe_dir, const char *program)
{
    char prev[MAX_PATH];
    DWORD err;
    int kept;

    g_program = (program && *program) ? program : "?";
    rc_strf(g_path, sizeof(g_path), "%s\\%s.log", exe_dir, g_program);

    if (!g_ready)
    {
        InitializeCriticalSection(&g_lock);
        g_ready = 1;
    }
    else
    {
        /* A second open in one process. InitializeCriticalSection must NOT
         * run twice on the same section, and the old handle has to go before
         * the rename below can touch the file. */
        ScopedLockIf lock(&g_lock, g_ready);
        if (g_fh != INVALID_HANDLE_VALUE)
        {
            CloseHandle(g_fh);
            g_fh = INVALID_HANDLE_VALUE;
        }
    }

    kept = rotate(exe_dir, g_program, prev, sizeof(prev), &err);

    /* EVERY path above ends here. The rotation is a diagnostic nicety and
     * logging is the diagnostic itself: nothing it does may stop this open or
     * change what it returns. */
    g_fh = CreateFileA(g_path, GENERIC_WRITE, FILE_SHARE_READ, NULL,
                       CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (g_fh == INVALID_HANDLE_VALUE) return -1;
    rc_log("=== %s starting ===", g_program);
    if (kept == ROT_DONE)
        rc_log("log: the previous run is now %s.prv", g_program);
    else if (err)
        rc_log("log: the previous run could NOT be kept as %s.prv "
               "(error %lu); it is gone", g_program, (unsigned long)err);
    return 0;
}

void rc_log(const char *fmt, ...)
{
    char line[1024];
    va_list ap;
    DWORD n;

    /* A HINT, not the decision. Re-tested under the lock in emit_open,
     * because rc_log_close can close the handle between this test and the
     * write. Cheap here and it saves formatting a line nobody will read. */
    if (g_fh == INVALID_HANDLE_VALUE) return;

    va_start(ap, fmt);
    n = rc_log_format(line, sizeof(line), fmt, ap);
    va_end(ap);

    emit_open(line, n);
}

void rc_log_tail(const char *fmt, ...)
{
    char line[1024];
    va_list ap;
    DWORD n;

    va_start(ap, fmt);
    n = rc_log_format(line, sizeof(line), fmt, ap);
    va_end(ap);

    /* Still open on the ordinary path; reopened on the one this exists for. */
    if (!emit_open(line, n))
        emit_closed(line, n);
}

void rc_log_close(void)
{
    if (g_fh == INVALID_HANDLE_VALUE) return;
    rc_log("=== %s exiting ===", g_program);

    /* Close under the lock, so a thread inside rc_log cannot be between its
     * handle test and its WriteFile when this happens. */
    {
        ScopedLockIf lock(&g_lock, g_ready);
        CloseHandle(g_fh);
        g_fh = INVALID_HANDLE_VALUE;
    }

    /* g_lock is NOT deleted, and that is the same trade net.cpp makes for
     * send_lock and rpc.cpp for its own g_lock: this function cannot
     * establish that no other thread is inside rc_log, and destroying a
     * critical section under a live thread is the GPF-at-exit those files
     * describe. Leaking one at process exit costs nothing. It used to be
     * deleted unconditionally, which is the one place this file broke a
     * rule the transport layer states twice. (mem-audit F6)
     *
     * g_ready stays 1 for the same reason -- clearing it would make every
     * later rc_log skip the lock and race the ones already inside it. */
}

void rc_log_mem(const char *when)
{
    MEMORYSTATUS ms;

    memset(&ms, 0, sizeof(ms));
    ms.dwLength = sizeof(ms);
    GlobalMemoryStatus(&ms);
    rc_log("mem [%s]: %luK free of %luK phys, %luK free of %luK page, load %lu%%",
           when,
           (unsigned long)(ms.dwAvailPhys / 1024),
           (unsigned long)(ms.dwTotalPhys / 1024),
           (unsigned long)(ms.dwAvailPageFile / 1024),
           (unsigned long)(ms.dwTotalPageFile / 1024),
           (unsigned long)ms.dwMemoryLoad);
}
