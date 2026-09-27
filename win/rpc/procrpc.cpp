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
    keys[1] = "status";    vals[1] = ok ? "ok" : "failed";
    keys[2] = "count";     vals[2] = count;
    keys[3] = "truncated"; vals[3] = trunc;
    rc_net_send(net, "PSRESULT", keys, vals, 4, buf.get(), lines.m_len);
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

