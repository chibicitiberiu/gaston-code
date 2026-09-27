#include "remote/control.h"

#include <stdlib.h>
#include <string.h>
#include "core/log.h"
#include "core/str.h"
#include "exec/system.h"

/* A DATA body's worth of process lines. Well under the 64KB frame cap, and
 * small enough that a long listing starts arriving while it is still being
 * made rather than after. */
enum { PS_CHUNK = 8192 };

/* On 98, TerminateProcess returning TRUE is not the same as "gone", so a
 * kill waits a moment and says `pending` if the process is still there.
 * Short, because this runs on the reader. */
enum { KILL_WAIT_MS = 250 };

/* Long enough for one small frame to reach the wire on a LAN. ExitWindowsEx
 * does not come back, so an OK still in the out queue is an OK nobody ever
 * sees -- the caller would read the reboot as a crash. */
enum { REBOOT_FLUSH_MS = 2000 };

ControlOps::ControlOps(Processes &procs, Connection &conn)
    : m_procs(&procs), m_conn(&conn)
{
}

void ControlOps::sysinfo(Responder &r)
{
    SystemInfo si;
    char memFree[16], uptime[16], memKb[16];
    const char *keys[3];
    const char *vals[3];

    readSystemInfo(si);
    rc_strf(memKb, sizeof memKb, "%lu", (unsigned long)si.memKb);
    rc_strf(memFree, sizeof memFree, "%lu", (unsigned long)si.memFreeKb);
    /* GetTickCount, so it wraps after 49.7 days. Sent raw: turning it into
     * something a person reads is the caller's job, like everything else. */
    rc_strf(uptime, sizeof uptime, "%lu", (unsigned long)si.uptimeMs);
    keys[0] = "mem_kb";      vals[0] = memKb;
    keys[1] = "mem_free_kb"; vals[1] = memFree;
    keys[2] = "uptime_ms";   vals[2] = uptime;
    /* No USER/GDI free-resource numbers: GetFreeSystemResources is 16-bit
     * only, and reaching for it from here would break this lane's one rule. */
    r.ok(keys, vals, 3);
}

/* Lines of "pid ppid threads exe", flushed to a DATA frame every PS_CHUNK.
 * The same spelling the client's PSLIST uses -- %08lx for the pids because
 * on 9x they are obfuscated pointers with the high bit set, which is not a
 * number anybody should be reading as signed. */
class PsChunks : public ProcessSink
{
public:
    PsChunks(Responder &r) : m_r(&r), m_len(0), m_count(0), m_stopped(false) {}

    bool onProcess(const ProcessInfo &p)
    {
        char line[MAX_PATH + 48];
        unsigned n;

        rc_strf(line, sizeof line, "%08lx %08lx %lu %s\n",
                (unsigned long)p.pid, (unsigned long)p.ppid,
                (unsigned long)p.threads, p.exe);
        n = (unsigned)strlen(line);
        if (m_len + n > sizeof m_buf && !flush())
            return false;
        memcpy(m_buf + m_len, line, n);
        m_len += n;
        m_count++;
        return true;
    }

    bool flush()
    {
        if (!m_len)
            return true;
        if (!m_r->data(m_buf, m_len))
        {
            /* The caller is gone or too far behind. Stop walking rather
             * than finish a listing nobody will read. */
            m_stopped = true;
            return false;
        }
        m_len = 0;
        return true;
    }

    int count() const { return m_count; }
    bool stopped() const { return m_stopped; }

private:
    Responder *m_r;
    char m_buf[PS_CHUNK];
    unsigned m_len;
    int m_count;
    bool m_stopped;
};

void ControlOps::ps(Responder &r)
{
    char count[16];
    const char *keys[1];
    const char *vals[1];

    if (!m_procs->available())
    {
        r.err("no_toolhelp");
        return;
    }
    /* The listing is on the heap, not the stack: PsChunks carries an 8KB
     * buffer and the frame gate refuses any frame reaching one page. */
    UniqueObj<PsChunks> lines(new PsChunks(r));
    if (!lines.valid())
    {
        r.err("no_memory");
        return;
    }
    /* readKinds=false: whether a process is 16-bit costs a header read per
     * entry, and nothing in this listing reports it. */
    m_procs->list(*lines.get(), false);
    lines->flush();
    if (lines->stopped())
        return;     /* the link is gone; END would not reach it either */
    rc_strf(count, sizeof count, "%d", lines->count());
    keys[0] = "count";
    vals[0] = count;
    r.end(keys, vals, 1);
}

void ControlOps::kill(Responder &r, rc_frame *f)
{
    const char *pidText = rc_frame_get(f, "pid");
    const char *force = rc_frame_get(f, "force");
    const char *code = "failed";

    if (!pidText || !*pidText)
    {
        r.err("no_such_pid");
        return;
    }
    /* Base 16, matching what PS prints. */
    DWORD pid = (DWORD)strtoul(pidText, NULL, 16);
    switch (m_procs->kill(pid, force && strcmp(force, "1") == 0,
                          KILL_WAIT_MS))
    {
    case KILL_OK:
        r.ok();
        return;
    case KILL_PENDING:     code = "pending";     break;
    case KILL_DENIED:      code = "denied";      break;
    case KILL_DOS_SESSION: code = "dos_session"; break;
    case KILL_NO_SUCH_PID: code = "no_such_pid"; break;
    case KILL_NO_TOOLHELP: code = "no_toolhelp"; break;
    default:               code = "failed";      break;
    }
    rc_log("control: KILL %s -> %s", pidText, code);
    r.err(code);
}

void ControlOps::reboot(Responder &r, rc_frame *f)
{
    const char *confirm = rc_frame_get(f, "confirm");
    const char *mode = rc_frame_get(f, "mode");
    RebootMode which = REBOOT_RESTART;

    /* Not a default anyone can fall into. This ends every unsaved thing on
     * the machine, and it exists for a client that has wedged. */
    if (!confirm || strcmp(confirm, "yes") != 0)
    {
        r.err("unconfirmed");
        return;
    }
    if (mode && strcmp(mode, "logoff") == 0)
        which = REBOOT_LOGOFF;
    else if (mode && strcmp(mode, "poweroff") == 0)
        which = REBOOT_POWEROFF;
    else if (mode && strcmp(mode, "reboot") != 0 && *mode)
    {
        r.err("bad_mode");
        return;
    }

    rc_log("control: REBOOT mode=%s", mode ? mode : "reboot");
    r.ok();
    /* Before the machine goes, not after: ExitWindowsEx does not return. */
    m_conn->drain(REBOOT_FLUSH_MS);
    if (!requestReboot(which, false))
        rc_log("control: Windows refused the shutdown");
}

bool ControlOps::handle(Responder &r, rc_frame *f)
{
    if (strcmp(f->type, "SYSINFO") == 0)
        sysinfo(r);
    else if (strcmp(f->type, "PS") == 0)
        ps(r);
    else if (strcmp(f->type, "KILL") == 0)
        kill(r, f);
    else if (strcmp(f->type, "REBOOT") == 0)
        reboot(r, f);
    else
        return false;
    return true;
}
