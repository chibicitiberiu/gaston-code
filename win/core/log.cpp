#include <windows.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include "core/log.h"
#include "core/str.h"

static HANDLE g_fh = INVALID_HANDLE_VALUE;
static CRITICAL_SECTION g_lock;
static int g_ready = 0;
static char g_path[MAX_PATH];

void rc_log_open(const char *exe_dir)
{
    rc_strf(g_path, sizeof(g_path), "%s\\retroclaude.log", exe_dir);

    InitializeCriticalSection(&g_lock);
    g_ready = 1;

    g_fh = CreateFileA(g_path, GENERIC_WRITE, FILE_SHARE_READ, NULL,
                       CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    rc_log("=== retroclaude starting ===");
}

void rc_log(const char *fmt, ...)
{
    char line[1024];
    char stamp[32];
    SYSTEMTIME st;
    va_list ap;
    int n;
    DWORD written;

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

    if (g_ready) EnterCriticalSection(&g_lock);
    WriteFile(g_fh, stamp, (DWORD)strlen(stamp), &written, NULL);
    WriteFile(g_fh, line, (DWORD)n, &written, NULL);
    /* Flush every line. Slow, but a buffered log loses exactly the lines that
     * matter -- the ones written just before the fault. */
    FlushFileBuffers(g_fh);
    if (g_ready) LeaveCriticalSection(&g_lock);
}

void rc_log_close(void)
{
    if (g_fh != INVALID_HANDLE_VALUE) {
        rc_log("=== retroclaude exiting ===");
        CloseHandle(g_fh);
        g_fh = INVALID_HANDLE_VALUE;
    }
    if (g_ready) { DeleteCriticalSection(&g_lock); g_ready = 0; }
}
