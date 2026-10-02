/* Processes on this machine, for the bridge: PSLIST and PSKILL.
 *
 * Its own file rather than rpc.cpp's, so the exec status vocabulary that
 * rpc.cpp speaks (and tests/python/wire/test_exec_status.py pins) stays
 * separate from this one.
 */
#include <windows.h>
#include <stdlib.h>
#include <string.h>
#include "rpc/procrpc.h"
#include "exec/process.h"
#include "core/log.h"
#include "core/str.h"
#include "core/rc_cpp.h"

/* --- processes (IMMEDIATE lane, reader thread) ---------------------------
 *
 * libexec's Processes, answered where EXECKILL is: a hung program is found
 * and ended while an EXEC is still stuck in the job slot. Nothing here may
 * block -- a Toolhelp snapshot is kernel32 alone, the listing reads no exe
 * headers, and a kill waits at most PS_KILL_WAIT_MS before saying
 * `pending` (on 98, TerminateProcess returning TRUE is not "gone"). */

#define PS_BODY_CAP    (48u * 1024u)
#define PS_KILL_WAIT_MS 250

/* One line per process: pid ppid threads exe. */
class PsLines : public ProcessSink
{
public:
    PsLines(char *buf, unsigned cap) : m_buf(buf), m_cap(cap), m_len(0),
                                       m_count(0), m_truncated(0) {}
    bool onProcess(const ProcessInfo &p)
    {
        char line[MAX_PATH + 48];
        rc_strf(line, sizeof(line), "%08lx %08lx %lu %s\n",
                (unsigned long)p.pid, (unsigned long)p.ppid,
                (unsigned long)p.threads, p.exe);
        unsigned n = (unsigned)strlen(line);
        if (m_len + n > m_cap) { m_truncated = 1; return false; }
        memcpy(m_buf + m_len, line, n);
        m_len += n;
        m_count++;
        return true;
    }
    char *m_buf;
    unsigned m_cap, m_len;
    int m_count, m_truncated;
};

void rc_procrpc_list(rc_net *net, const rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *keys[4]; const char *vals[4];
    char count[16], trunc[4];
    if (!id) { rc_log("rpc: PSLIST with no id -- dropped"); return; }
    keys[0] = "id"; vals[0] = id;
    Processes ps;
    if (!ps.init()) {
        keys[1] = "status"; vals[1] = "no_toolhelp";
        rc_net_send(net, "PSRESULT", keys, vals, 2, NULL, 0);
        return;
    }
    HeapPtr<char> buf((char *)HeapAlloc(GetProcessHeap(), 0, PS_BODY_CAP));
    if (!buf.valid()) {
        keys[1] = "status"; vals[1] = "no_memory";
        rc_net_send(net, "PSRESULT", keys, vals, 2, NULL, 0);
        return;
    }
    PsLines lines(buf.get(), PS_BODY_CAP);
    bool ok = ps.list(lines, false);
    rc_strf(count, sizeof(count), "%d", lines.m_count);
    rc_strf(trunc, sizeof(trunc), "%d", lines.m_truncated);

    /* MEMORY, NOW -- not HELLO's figure from when the client started. On a
     * 32MB box the question "why is it slow" is usually answered by the swap
     * line, and an agent deciding what to end needs the number as it is
     * while it decides. GlobalMemoryStatus: everything here, every OS. */
    MEMORYSTATUS ms;
    char memKb[16], memFree[16], swapKb[16], swapFree[16], load[8];
    ms.dwLength = sizeof(ms);
    GlobalMemoryStatus(&ms);
    rc_strf(memKb, sizeof(memKb), "%lu", (unsigned long)(ms.dwTotalPhys / 1024));
    rc_strf(memFree, sizeof(memFree), "%lu",
            (unsigned long)(ms.dwAvailPhys / 1024));
    rc_strf(swapKb, sizeof(swapKb), "%lu",
            (unsigned long)(ms.dwTotalPageFile / 1024));
    rc_strf(swapFree, sizeof(swapFree), "%lu",
            (unsigned long)(ms.dwAvailPageFile / 1024));
    rc_strf(load, sizeof(load), "%lu", (unsigned long)ms.dwMemoryLoad);

    const char *k[9]; const char *v[9];
    k[0] = "id";           v[0] = id;
    k[1] = "status";       v[1] = ok ? "ok" : "failed";
    k[2] = "count";        v[2] = count;
    k[3] = "truncated";    v[3] = trunc;
    k[4] = "mem_kb";       v[4] = memKb;
    k[5] = "mem_free_kb";  v[5] = memFree;
    k[6] = "swap_kb";      v[6] = swapKb;
    k[7] = "swap_free_kb"; v[7] = swapFree;
    k[8] = "mem_load";     v[8] = load;
    rc_net_send(net, "PSRESULT", k, v, 9, buf.get(), lines.m_len);
}

/* PSLAUNCH: start a program and DO NOT wait for it.
 *
 * What `exec` cannot do. A live session (2026-10-02) ran `exec notepad`:
 * EXEC waits for the process it started, so the turn sat out the whole
 * timeout -- and then the timeout killed the Notepad it had just opened.
 * Processes::launch hands the program nothing of ours (no handles, no
 * console) and returns its pid at once. On the reader, like PSLIST: a
 * CreateProcess is all it is. */
void rc_procrpc_launch(rc_net *net, const rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *command = rc_frame_get(f, "command");
    const char *cwd = rc_frame_get(f, "cwd");
    const char *showText = rc_frame_get(f, "show");
    const char *keys[4]; const char *vals[4];
    char pidText[16], errText[16];
    DWORD pid = 0, err = 0;
    int show = SW_SHOWNORMAL;
    int n = 0;

    if (!id) { rc_log("rpc: PSLAUNCH with no id -- dropped"); return; }
    if (showText && !strcmp(showText, "min")) show = SW_SHOWMINNOACTIVE;
    else if (showText && !strcmp(showText, "max")) show = SW_SHOWMAXIMIZED;
    keys[n] = "id"; vals[n++] = id;
    if (!command || !*command) {
        keys[n] = "status"; vals[n++] = "bad_command";
    } else if (!Processes::launch(command, cwd && *cwd ? cwd : NULL, show,
                                  pid, err)) {
        rc_strf(errText, sizeof(errText), "%lu", (unsigned long)err);
        keys[n] = "status"; vals[n++] =
            err == ERROR_FILE_NOT_FOUND || err == ERROR_PATH_NOT_FOUND
                ? "not_found"
            : err == ERROR_DIRECTORY ? "bad_cwd"
            : err == ERROR_FILENAME_EXCED_RANGE ? "too_long" : "failed";
        keys[n] = "error"; vals[n++] = errText;
    } else {
        rc_strf(pidText, sizeof(pidText), "%08lX", (unsigned long)pid);
        keys[n] = "status"; vals[n++] = "ok";
        keys[n] = "pid";    vals[n++] = pidText;
    }
    rc_log("rpc: PSLAUNCH %s -> %s", id, vals[1]);
    rc_net_send(net, "PSLAUNCHED", keys, vals, n, NULL, 0);
}

void rc_procrpc_kill(rc_net *net, const rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    const char *pidh = rc_frame_get(f, "pid");
    const char *force = rc_frame_get(f, "force");
    const char *keys[2]; const char *vals[2];
    const char *status = "failed";
    if (!id) { rc_log("rpc: PSKILL with no id -- dropped"); return; }
    if (!pidh || !*pidh) {
        status = "no_such_pid";
    } else {
        Processes ps;
        if (!ps.init()) {
            status = "no_toolhelp";
        } else {
            DWORD pid = (DWORD)strtoul(pidh, NULL, 16);
            switch (ps.kill(pid, force && strcmp(force, "1") == 0,
                            PS_KILL_WAIT_MS)) {
            case KILL_OK:          status = "ok";          break;
            case KILL_PENDING:     status = "pending";     break;
            case KILL_DENIED:      status = "denied";      break;
            case KILL_DOS_SESSION: status = "dos_session"; break;
            case KILL_NO_SUCH_PID: status = "no_such_pid"; break;
            case KILL_NO_TOOLHELP: status = "no_toolhelp"; break;
            default:               status = "failed";      break;
            }
        }
    }
    rc_log("rpc: PSKILL %s pid=%s -> %s", id, pidh ? pidh : "-", status);
    keys[0] = "id";     vals[0] = id;
    keys[1] = "status"; vals[1] = status;
    rc_net_send(net, "PSKILLRESULT", keys, vals, 2, NULL, 0);
}

