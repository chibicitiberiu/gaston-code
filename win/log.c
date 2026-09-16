#include <windows.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include "log.h"

static HANDLE g_fh = INVALID_HANDLE_VALUE;
static CRITICAL_SECTION g_lock;
static int g_ready = 0;
static char g_path[MAX_PATH];

void rc_log_open(const char *exe_dir)
{
    _snprintf(g_path, sizeof(g_path) - 1, "%s\\retroclaude.log", exe_dir);
    g_path[sizeof(g_path) - 1] = '\0';

    InitializeCriticalSection(&g_lock);
    g_ready = 1;

    g_fh = CreateFileA(g_path, GENERIC_WRITE, FILE_SHARE_READ, NULL,
                       CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    rc_log("=== retroclaude starting ===");
}

const char *rc_log_path(void) { return g_path; }

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
    _snprintf(stamp, sizeof(stamp), "%02d:%02d:%02d.%03d ",
              st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);

    va_start(ap, fmt);
    n = _vsnprintf(line, sizeof(line) - 3, fmt, ap);
    va_end(ap);
    if (n < 0) n = (int)strlen(line);
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
