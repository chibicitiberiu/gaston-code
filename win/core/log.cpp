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

int rc_log_open(const char *exe_dir)
{
    rc_strf(g_path, sizeof(g_path), "%s\\retroclaude.log", exe_dir);

    InitializeCriticalSection(&g_lock);
    g_ready = 1;

    g_fh = CreateFileA(g_path, GENERIC_WRITE, FILE_SHARE_READ, NULL,
                       CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (g_fh == INVALID_HANDLE_VALUE) return -1;
    rc_log("=== retroclaude starting ===");
    return 0;
}

void rc_log(const char *fmt, ...)
{
    char line[1024];
    char stamp[32];
    SYSTEMTIME st;
    va_list ap;
    int n;
    DWORD written;

    /* A HINT, not the decision. Re-tested under the lock below, because
     * rc_log_close can close the handle between this test and the write.
     * Cheap here and it saves formatting a line nobody will read. */
    if (g_fh == INVALID_HANDLE_VALUE) return;

    GetLocalTime(&st);
    rc_strf(stamp, sizeof(stamp), "%02d:%02d:%02d.%03d ",
              st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);

    va_start(ap, fmt);
    n = _vsnprintf(line, sizeof(line) - 3, fmt, ap);
    va_end(ap);
    /* Terminate BEFORE measuring, which is the whole point of rc_strf and was
     * skipped here -- the one place that still called _vsnprintf directly.
     *
     * _vsnprintf returns negative when the result did not fit, and in that
     * case it has not written a NUL anywhere. The old fallback was
     * `n = strlen(line)`, which then scanned past whatever had been written,
     * through the rest of `line` and on into adjacent stack, until it met a
     * zero byte. An over-long log line is a stack over-read in the logger --
     * in the function whose whole job is to be working when nothing else is. */
    line[sizeof(line) - 3] = '\0';
    if (n < 0 || (unsigned)n > sizeof(line) - 3)
        n = (int)strlen(line);
    line[n++] = '\r';
    line[n++] = '\n';

    /* THE HANDLE IS NOW READ UNDER THE LOCK, and that is the fix.
     *
     * It used to be tested once at the top of this function, unlocked, and
     * closed in rc_log_close, also unlocked. A thread past that test could
     * reach WriteFile after the close had happened -- and Win9x recycles
     * handle values aggressively, so the write does not fail, it lands in
     * whatever file got that number next. net.cpp documents the identical
     * hazard for the socket (finding 01.52) and removes it by ordering
     * against a join; there is no join to order against here, so the read
     * moves inside the section that already exists. (mem-audit F6) */
    {
    ScopedLockIf lock(&g_lock, g_ready);
    if (g_fh != INVALID_HANDLE_VALUE) {
        WriteFile(g_fh, stamp, (DWORD)strlen(stamp), &written, NULL);
        WriteFile(g_fh, line, (DWORD)n, &written, NULL);
        /* Flush every line, and it stays INSIDE the lock deliberately.
         *
         * A buffered log loses exactly the lines that matter, the ones
         * written just before the fault, so the flush is not negotiable.
         * Holding four threads behind a physical flush on a 200MHz machine
         * is a real cost and it was tempting to move this below the Leave --
         * but that needs the handle to stay valid outside the lock, which is
         * precisely the race the block above just removed. A complete log
         * beats a fast one; this file is the only debugging channel the
         * guest has. */
        FlushFileBuffers(g_fh);
    }
    }
}

void rc_log_close(void)
{
    if (g_fh == INVALID_HANDLE_VALUE) return;
    rc_log("=== retroclaude exiting ===");

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
