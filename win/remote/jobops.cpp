#include "remote/jobops.h"

#include <stdlib.h>
#include <string.h>
#include "core/log.h"
#include "core/str.h"
#include "exec/process.h"
#include "remote/reply.h"

/* Bytes per DATA frame. Output is handed over as it arrives rather than at
 * the end, so a command that prints for a minute is watchable, and the
 * guest never holds more than this of it. */
enum { EXEC_CHUNK = 8192 };

/* A command line long enough for anything COMMAND.COM will take, and short
 * enough that refusing beyond it is obviously right rather than arbitrary. */
enum { EXEC_CMD_MAX = 2048 };

JobOps::JobOps(const ExecConfig &cfg, Connection &conn)
    : m_cfg(&cfg), m_conn(&conn)
{
}

/* Each chunk of the child's output, as it comes. The bytes are the console
 * codepage and they leave UNTOUCHED -- HELLO said which codepage, and the
 * guest transforms nothing. */
class ExecOut : public OutputSink
{
public:
    explicit ExecOut(Responder &r) : m_r(&r), m_gone(false) {}

    bool onOutput(const char *data, unsigned len)
    {
        if (m_r->data(data, len))
            return true;
        /* Nobody will read this again: the link is gone, or the caller is
         * so far behind that a bulk frame could not get room inside its
         * budget. Returning false ends the job with EXEC_SINK_GONE instead
         * of running it to its timeout for no one. */
        m_gone = true;
        return false;
    }

    bool gone() const { return m_gone; }

private:
    Responder *m_r;
    bool m_gone;
};

void JobOps::exec(Responder &r, rc_frame *f)
{
    const char *timeout = rc_frame_get(f, "timeout_ms");
    const char *cwd = rc_frame_get(f, "cwd");
    const char *shell = rc_frame_get(f, "shell");
    char command[EXEC_CMD_MAX];
    ExecOptions opt;
    ExecResult res;
    char exitCode[16], truncated[4], timedOut[4], viaShell[4], bytes[16];
    const char *keys[5];
    const char *vals[5];

    if (f->body_len == 0)
    {
        r.err("no_command");
        return;
    }
    if (f->body_len >= sizeof command)
    {
        r.err("too_long");
        return;
    }
    rc_strncpy(command, sizeof command, f->body, f->body_len);

    opt.shell = shell;
    opt.cwd = cwd;
    opt.timeoutMs = timeout ? (DWORD)strtoul(timeout, NULL, 10) : 20000u;
    /* CLAMPED, not refused, and said out loud. A caller asking for longer
     * than this machine allows wants the longest it can have -- but an
     * unclamped-looking timeout that fires early gets blamed on the
     * network, so the log records which one actually applied. */
    if (opt.timeoutMs > (DWORD)m_cfg->maxTimeoutMs)
    {
        rc_log("job: timeout_ms %lu clamped to %ld",
               (unsigned long)opt.timeoutMs, m_cfg->maxTimeoutMs);
        opt.timeoutMs = (DWORD)m_cfg->maxTimeoutMs;
    }
    opt.maxOutput = (DWORD)m_cfg->maxOutputKb * 1024u;

    ExecOut sink(r);
    ExecJob job;
    if (!job.run(command, opt, sink, res) &&
        res.status == EXEC_SPAWN_FAILED)
    {
        char detail[32];
        rc_strf(detail, sizeof detail, "%lu", (unsigned long)res.error);
        r.err("spawn_failed", detail);
        return;
    }
    if (sink.gone())
    {
        /* No END: there is nobody to send it to, and saying so in the log
         * is the only useful thing left. */
        rc_log("job: the caller went away during EXEC");
        return;
    }

    rc_strf(exitCode, sizeof exitCode, "%lu", (unsigned long)res.exitCode);
    rc_strf(bytes, sizeof bytes, "%lu", (unsigned long)res.bytes);
    rc_strcpy(truncated, sizeof truncated, res.truncated ? "1" : "0");
    rc_strcpy(timedOut, sizeof timedOut,
              res.status == EXEC_TIMEOUT ? "1" : "0");
    /* A fact, not a verdict: COMMAND.COM /c on 98 exits 0 whatever happens. */
    rc_strcpy(viaShell, sizeof viaShell, res.viaShell ? "1" : "0");
    keys[0] = "exit";      vals[0] = exitCode;
    keys[1] = "bytes";     vals[1] = bytes;
    keys[2] = "truncated"; vals[2] = truncated;
    keys[3] = "timed_out"; vals[3] = timedOut;
    keys[4] = "via_shell"; vals[4] = viaShell;
    r.end(keys, vals, 5);
}

void JobOps::launch(Responder &r, rc_frame *f)
{
    const char *cwd = rc_frame_get(f, "cwd");
    const char *show = rc_frame_get(f, "show");
    char command[EXEC_CMD_MAX];
    DWORD pid = 0, err = 0;
    char pidText[16];
    const char *keys[1];
    const char *vals[1];

    if (f->body_len == 0)
    {
        r.err("no_command");
        return;
    }
    if (f->body_len >= sizeof command)
    {
        r.err("too_long");
        return;
    }
    rc_strncpy(command, sizeof command, f->body, f->body_len);

    /* Nothing is inherited: a launched program outlives this connection by
     * design, and one holding our listening socket would keep the port
     * after gremote itself had gone. */
    if (!Processes::launch(command, cwd, show ? atoi(show) : SW_SHOWNORMAL,
                           pid, err))
    {
        char detail[32];
        rc_strf(detail, sizeof detail, "%lu", (unsigned long)err);
        r.err("spawn_failed", detail);
        return;
    }
    /* Hex, like every other pid on this wire. */
    rc_strf(pidText, sizeof pidText, "%08lx", (unsigned long)pid);
    keys[0] = "pid";
    vals[0] = pidText;
    r.ok(keys, vals, 1);
}

void JobOps::handle(rc_frame *f)
{
    Responder r(*m_conn, rc_frame_get(f, "id"));
    if (strcmp(f->type, "EXEC") == 0)
        exec(r, f);
    else if (strcmp(f->type, "LAUNCH") == 0)
        launch(r, f);
    else
        r.err("unknown_op");
    rc_frame_delete(f);
}

void JobOps::abandon(rc_frame *f)
{
    Responder r(*m_conn, rc_frame_get(f, "id"));
    r.err("stopping");
    rc_frame_delete(f);
}
