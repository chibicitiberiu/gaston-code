/* cmdlog.cpp -- see rpc/cmdlog.h. */
#include <windows.h>
#include <string.h>

#include "core/log.h"
#include "core/str.h"
#include "rpc/cmdlog.h"

namespace {

CRITICAL_SECTION g_lock;
LONG  g_init;
char  g_dir[MAX_PATH];
bool  g_on;
DWORD g_swept;                  /* YYYYMMDD of the last retention sweep */

void ensureLock()
{
    /* Once, from whichever thread gets here first. */
    if (InterlockedExchange(&g_init, 1) == 0)
        InitializeCriticalSection(&g_lock);
}

DWORD yyyymmdd(const SYSTEMTIME &t)
{
    return (DWORD)t.wYear * 10000 + t.wMonth * 100 + t.wDay;
}

/* Days older than RC_CMDLOG_DAYS go, once a day. */
void sweep(const char *folder, const SYSTEMTIME &now)
{
    FILETIME ft;
    ULARGE_INTEGER u;
    SYSTEMTIME cut;
    char pattern[MAX_PATH], path[MAX_PATH];
    WIN32_FIND_DATAA fd;

    SystemTimeToFileTime(&now, &ft);
    u.LowPart = ft.dwLowDateTime;
    u.HighPart = ft.dwHighDateTime;
    u.QuadPart -= (ULONGLONG)RC_CMDLOG_DAYS * 24 * 60 * 60 * 10000000;
    ft.dwLowDateTime = u.LowPart;
    ft.dwHighDateTime = u.HighPart;
    FileTimeToSystemTime(&ft, &cut);
    DWORD oldest = yyyymmdd(cut);

    if (!rc_strf(pattern, sizeof pattern, "%s\\*.CMD", folder)) return;
    FindHandle h(FindFirstFileA(pattern, &fd));
    if (!h.valid()) return;
    do {
        DWORD day = 0;
        int digits = 0;
        for (const char *p = fd.cFileName; *p >= '0' && *p <= '9'; p++) {
            day = day * 10 + (DWORD)(*p - '0');
            digits++;
        }
        if (digits == 8 && day < oldest &&
            rc_strf(path, sizeof path, "%s\\%s", folder, fd.cFileName))
            DeleteFileA(path);
    } while (FindNextFileA(h.get(), &fd));
}

}  // namespace

void rc_cmdlog_configure(const char *dir, bool on)
{
    ensureLock();
    EnterCriticalSection(&g_lock);
    rc_strcpy(g_dir, sizeof g_dir, dir ? dir : "");
    g_on = on && g_dir[0];
    LeaveCriticalSection(&g_lock);
}

CmdLog::CmdLog() : m_size(0), m_capped(false) {}

void CmdLog::put(const char *data, unsigned len)
{
    DWORD wrote;
    if (!m_file.valid() || m_capped || !len) return;
    if (m_size + len > RC_CMDLOG_DAY_MAX) {
        static const char full[] =
            "\r\n[the day's command log is full (1 MB); nothing more today]\r\n";
        WriteFile(m_file.get(), full, sizeof full - 1, &wrote, NULL);
        m_capped = true;
        return;
    }
    if (WriteFile(m_file.get(), data, len, &wrote, NULL)) m_size += wrote;
}

void CmdLog::begin(const char *id, const char *command, const char *cwd)
{
    char dir[MAX_PATH], folder[MAX_PATH], path[MAX_PATH], head[400];
    SYSTEMTIME now;
    bool on;

    ensureLock();
    EnterCriticalSection(&g_lock);
    on = g_on;
    rc_strcpy(dir, sizeof dir, g_dir);
    LeaveCriticalSection(&g_lock);
    if (!on) return;

    GetLocalTime(&now);
    if (!rc_strf(folder, sizeof folder, "%s\\CMDLOG", dir)) return;
    CreateDirectoryA(folder, NULL);           /* there already: fine */
    if (g_swept != yyyymmdd(now)) {
        g_swept = yyyymmdd(now);
        sweep(folder, now);
    }
    if (!rc_strf(path, sizeof path, "%s\\%08lu.CMD", folder,
                 (unsigned long)yyyymmdd(now)))
        return;
    m_file.reset(CreateFileA(path, GENERIC_WRITE, FILE_SHARE_READ, NULL,
                             OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL));
    if (!m_file.valid()) {
        rc_log("cmdlog: cannot open %s (%lu)", path,
               (unsigned long)GetLastError());
        return;
    }
    m_size = SetFilePointer(m_file.get(), 0, NULL, FILE_END);
    if (m_size == 0xFFFFFFFF) m_size = 0;
    m_capped = m_size >= RC_CMDLOG_DAY_MAX;
    rc_strf(head, sizeof head, "==== %04u-%02u-%02u %02u:%02u:%02u  %s  in %s"
            "\r\n> ", now.wYear, now.wMonth, now.wDay, now.wHour,
            now.wMinute, now.wSecond, id ? id : "?",
            cwd && *cwd ? cwd : "(the program's own folder)");
    put(head, (unsigned)strlen(head));
    put(command ? command : "", command ? (unsigned)strlen(command) : 0);
    put("\r\n", 2);
}

void CmdLog::chunk(const char *data, unsigned len)
{
    put(data, len);
}

void CmdLog::end(const char *status, unsigned long code, bool truncated)
{
    char tail[160];
    if (!m_file.valid()) return;
    rc_strf(tail, sizeof tail, "\r\n==== %s, exit code %lu%s\r\n\r\n",
            status ? status : "?", code,
            truncated ? ", output cut short" : "");
    /* The ending is written even past the cap: a record that stops without
     * saying how the command ended is the one worth having least. */
    if (m_capped) {
        DWORD wrote;
        WriteFile(m_file.get(), tail, (DWORD)strlen(tail), &wrote, NULL);
    } else {
        put(tail, (unsigned)strlen(tail));
    }
    m_file.reset();
}
