/* ServerService and Session. See server.h for the shape and the three
 * decisions that are easy to get backwards. */
#include "remote/server.h"

#include <winsock.h>
#include <stdlib.h>
#include <string.h>
#include "core/log.h"
#include "core/str.h"
#include "exec/system.h"
#include "remote/optable.h"
#include "remote/reply.h"
#include "wire/link.h"

/* gremote's own protocol version. NOT the client's RC_PROTO_VERSION: this is
 * a separate op namespace on the same frame grammar, and the two must not
 * drift into each other (docs/REMOTE.md). */
enum { RC_REMOTE_PROTO = 1 };

/* How long the accept loop sleeps in select() between polls of its stop
 * flag. Short enough that a stop is prompt and a dead client is noticed
 * within a quarter second of the reader flagging it. */
enum { ACCEPT_SLICE_MS = 250 };

/* REMOTE.md: retry a bind that hits the peer's TIME_WAIT for up to four
 * minutes rather than reach for SO_REUSEADDR. */
enum { BIND_RETRY_MS = 240000, BIND_RETRY_SLICE_MS = 1000 };

/* A wrong secret costs a second, and the FIN goes out before the close --
 * an abrupt RST is reported to crash Win98's Winsock. */
enum { BAD_SECRET_PAUSE_MS = 1000 };

/* ------------------------------------------------------------- Session */

Session::Session(const ServerConfig &cfg, const ExecConfig &exec,
                 unsigned long generation, Processes &procs, App &app)
    : m_cfg(&cfg), m_procs(&procs), m_generation(generation),
      m_conn(this, false), m_control(procs, m_conn, app),
      m_jobOps(exec, m_conn), m_fsOps(m_conn), m_dead(0),
      m_authed(false)
{
    /* Deflate off in v0 (docs/gremote-plan.md): the only large payload is a
     * screenshot and the ui lane is step 6, so the 192KB of persistent
     * scratch buys nothing here. HELLO says deflate=0 and a peer that sends
     * enc=deflate anyway has the link dropped under it, which is
     * Connection's behaviour and not something to soften. */
}

bool Session::begin(SOCKET accepted)
{
    if (!m_conn.adopt(accepted))
        return false;
    m_conn.setIdleTimeout((DWORD)m_cfg->heartbeatS * 1000);
    /* 32 KB. Deeper than the accept loop: ExecJob spawns, waits, and on a
     * stop walks the process tree with Toolhelp. */
    m_jobOps.setGate(&m_job);
    m_fsOps.setGate(&m_fs);
    if (!m_job.start(m_jobOps, "the job lane", 32u * 1024u))
        return false;
    /* 32 KB as well: a directory walk holds a WIN32_FIND_DATA and a Path
     * per level, and the record buffers are on the heap for exactly this
     * reason. */
    if (!m_fs.start(m_fsOps, "the fs lane", 32u * 1024u))
        return false;
    return m_conn.start();
}

void Session::onState(Connection &c, rc_state s)
{
    (void)c;
    /* A flag, never the teardown: this runs on the reader thread, and
     * joining a Connection from inside its own reader is joining yourself.
     * The accept loop does it. */
    if (s == RC_FAILED || s == RC_DISCONNECTED)
        markDead();
}

void Session::refuse(const char *code, bool fatal)
{
    const char *keys[1];
    const char *vals[1];
    keys[0] = "code";
    vals[0] = code;
    /* No reserve: a refusal IS the control reply the reserve exists for. */
    m_conn.send("ERR", keys, vals, 1, NULL, 0, RC_SEND_WAIT_UI, false, 0);
    if (fatal)
    {
        m_conn.drain(BAD_SECRET_PAUSE_MS);
        Sleep(BAD_SECRET_PAUSE_MS);
        markDead();
        m_conn.signalStop();
    }
}

void Session::sendHello()
{
    SystemInfo si;
    char ver[12], memKb[16], memFree[16], acp[12], oemcp[12];
    char os[96], cpu[64], toolhelp[4];
    const char *keys[10];
    const char *vals[10];

    readSystemInfo(si);
    rc_strf(ver, sizeof ver, "%d", (int)RC_REMOTE_PROTO);
    rc_strf(memKb, sizeof memKb, "%lu", (unsigned long)si.memKb);
    rc_strf(memFree, sizeof memFree, "%lu", (unsigned long)si.memFreeKb);
    rc_strf(acp, sizeof acp, "%u", si.acp);
    rc_strf(oemcp, sizeof oemcp, "%u", si.oemcp);
    if (si.osKnown)
        rc_strf(os, sizeof os, "Windows %lu.%lu build %lu%s",
                (unsigned long)si.osMajor, (unsigned long)si.osMinor,
                (unsigned long)si.osBuild, si.osExtra);
    else
        rc_strcpy(os, sizeof os, "unknown");
    rc_strf(cpu, sizeof cpu, "x86 family %u model %u, %lu cpu",
            si.cpuFamily, si.cpuModel, (unsigned long)si.cpuCount);
    rc_strcpy(toolhelp, sizeof toolhelp, m_procs->available() ? "1" : "0");

    keys[0] = "ver";          vals[0] = ver;
    keys[1] = "os";           vals[1] = os;
    keys[2] = "cpu";          vals[2] = cpu;
    keys[3] = "mem_kb";       vals[3] = memKb;
    keys[4] = "mem_free_kb";  vals[4] = memFree;
    keys[5] = "acp";          vals[5] = acp;
    keys[6] = "oemcp";        vals[6] = oemcp;
    /* Capabilities, each a small integer with room to grow rather than a
     * bit field, the same rule the client's HELLO follows. msaa and deflate
     * are 0 because the ui lane is step 6 and compression is off in v0. */
    keys[7] = "msaa";         vals[7] = "0";
    keys[8] = "toolhelp";     vals[8] = toolhelp;
    keys[9] = "deflate";      vals[9] = "0";
    m_conn.send("HELLO", keys, vals, 10, NULL, 0, RC_SEND_WAIT_UI, false, 0);
}

void Session::onAuth(rc_frame *f)
{
    const char *ver = rc_frame_get(f, "ver");
    const char *secret = rc_frame_get(f, "secret");

    if (m_authed)
    {
        refuse("already_authed", false);
        return;
    }
    if (!ver || atoi(ver) != (int)RC_REMOTE_PROTO)
    {
        rc_log("server: AUTH with version %s, wanted %d",
               ver ? ver : "(none)", (int)RC_REMOTE_PROTO);
        refuse("bad_version", true);
        return;
    }
    if (!secret || strcmp(secret, m_cfg->secret) != 0)
    {
        rc_log("server: AUTH with the wrong secret");
        refuse("bad_secret", true);
        return;
    }
    m_authed = true;
    rc_log("server: client %lu authenticated", m_generation);
    sendHello();
}

void Session::onFrame(Connection &c, rc_frame *f)
{
    (void)c;
    if (strcmp(f->type, "AUTH") == 0)
    {
        onAuth(f);
    }
    else if (!m_authed)
    {
        /* Nothing before AUTH, and saying so is worth more than silence:
         * a caller that skipped the handshake would otherwise wait out its
         * whole budget for a reply that was never coming. */
        rc_log("server: %s before AUTH", f->type);
        refuse("not_authed", true);
    }
    else
    {
        Responder r(m_conn, rc_frame_get(f, "id"));
        const GOpRow *row;

        if (strcmp(f->type, "CANCEL") == 0)
        {
            /* The Session's, not the control lane's: it is about the lanes,
             * and the Session is what owns them. Answered through an
             * UNGATED Responder -- the frame carries the target's id, and a
             * gated one would suppress the very ack that says the target is
             * dead. */
            doCancel(r, f);
        }
        else if (strcmp(f->type, "PING") == 0)
        {
            /* The Session's own, not the control lane's: it is about this
             * connection rather than about the machine, and it is what
             * resets the peer's side of the heartbeat. */
            r.ok();
        }
        else if (!gremoteOp(f->type, &row))
        {
            r.err("unknown_op");
        }
        else if (row->lane == G_CONTROL)
        {
            /* Inline, on this thread. See remote/control.h for what that
             * costs and why these ops pay it. */
            if (!m_control.handle(r, f))
                r.err("unknown_op");
        }
        else if (row->lane == G_JOB)
        {
            toLane(m_job, f, row->continues);
            return;             /* the lane owns the frame */
        }
        else
        {
            toLane(m_fs, f, row->continues);
            return;             /* the lane owns the frame */
        }
    }
    rc_frame_delete(f);
}

/* How long the reader will wait for room in a lane's continuation queue
 * before giving up on the transfer. Five seconds of a disk not keeping up
 * is a stall, not a hiccup. */
enum { CONT_WAIT_MS = 5000 };

void Session::toLane(LaneWorker &lane, rc_frame *f, bool continues)
{
    const char *busyOp = "";
    DWORD ageMs = 0;

    if (continues)
    {
        /* A PUT's own chunks. They go to the FIFO, which the reader waits
         * on rather than refusing -- not reading the socket for a moment is
         * what tells the sender to slow down. Refusing here would refuse a
         * transfer its own bytes the first time the platter fell behind. */
        if (lane.submitContinuation(f, CONT_WAIT_MS))
            return;
        Responder r(m_conn, rc_frame_get(f, "id"));
        r.err("busy");
        rc_frame_delete(f);
        return;
    }
    if (lane.submit(f, &busyOp, &ageMs))
        return;                 /* the lane owns it now */

    /* Refused, not queued, and the refusal says what is holding the lane
     * and for how long -- the difference between "try again in a moment"
     * and "something is stuck". */
    {
        Responder r(m_conn, rc_frame_get(f, "id"));
        char age[16];
        const char *keys[2];
        const char *vals[2];
        rc_strf(age, sizeof age, "%lu", (unsigned long)ageMs);
        keys[0] = "op";     vals[0] = busyOp;
        keys[1] = "age_ms"; vals[1] = age;
        r.errWith("busy", keys, vals, 2);
    }
    rc_frame_delete(f);
}

/* 9P's Tflush, copied rather than re-derived (docs/BACKLOG.md 6). The ack
 * means the cancelled request will never be answered, and the caller may
 * reuse its id the moment the ack arrives: a lane runs one request at a
 * time, so nothing bearing that id can start until the old work has cleared.
 *
 * TWO IDS, not one. REMOTE.md's op table says "`CANCEL` | `id` of target",
 * which reads fine until you try it: the ack would come back under the
 * victim's id, land in whatever is waiting for the victim's own reply, and
 * look exactly like the answer this op exists to promise will never come.
 * Tflush carries a tag AND an oldtag for that reason. So `CANCEL` gets an id
 * of its own like every other request, and names its victim in `target=`.
 * The table is updated to match. */
void Session::doCancel(Responder &r, rc_frame *f)
{
    const char *id = rc_frame_get(f, "target");
    CancelResult job, fs;

    /* A different code from `no_such_id`, deliberately: "you did not say
     * what to cancel" and "there is nothing by that name" are different
     * mistakes, and collapsing them cost an hour of reading the right code
     * while the wrong branch was firing. */
    if (!id || !*id)
    {
        r.err("no_target");
        return;
    }
    job = m_job.cancel(id);
    fs = m_fs.cancel(id);
    if (job == CANCEL_NO_SUCH_ID && fs == CANCEL_NO_SUCH_ID)
    {
        /* Nothing by that name. NOT an error worth hiding: a cancel that
         * lost the race against a reply is the ordinary case, and 9P says
         * to ack it anyway -- but the caller asked about a specific id and
         * deserves to know it was already gone. */
        rc_log("cancel: nothing on either lane is called %s", id);
        r.err("no_such_id");
        return;
    }
    rc_log("cancel: %s was %s", id,
           (job == CANCEL_DROPPED || fs == CANCEL_DROPPED) ? "still queued"
                                                           : "running");
    r.ok();
}

bool Session::end()
{
    /* The workers first, while sends still work, so their last answers go
     * out; then the Connection. Releasing it under a live worker would free
     * a buffer that worker is still sending from. */
    m_job.signalStop();
    m_fs.signalStop();
    bool workersGone = m_job.join(5000);
    workersGone = m_fs.join(5000) && workersGone;
    m_conn.drain(1000);
    m_conn.signalStop();
    bool joined = m_conn.join(3000);
    return m_conn.release(workersGone) && joined;
}

/* ------------------------------------------------------- ServerService */

ServerService::ServerService(const ServerConfig &cfg, const ExecConfig &exec,
                             Processes &procs, App &app)
    : m_cfg(&cfg), m_exec(&exec), m_procs(&procs), m_app(&app), m_stop(0),
      m_status(STATUS_BINDING), m_boundPort(0), m_peerAddr(0), m_served(0),
      m_generation(0)
{
}

bool ServerService::init()
{
    /* Nothing to acquire: the socket is bound on the thread, for the reason
     * in server.h. */
    return true;
}

bool ServerService::start()
{
    m_accept.attach(this);
    /* 16 KB. This thread binds, waits in select() and hands a socket on;
     * its deepest call is Session::begin. The frame gate caps any one frame
     * below a page, so this is four maximal frames of room for a call chain
     * three deep. */
    return m_thread.start(m_accept, "the accept loop", 16u * 1024u);
}

void ServerService::signalStop()
{
    /* The flag and nothing else. The accept thread is the only writer of
     * m_session, and reaching in from here to signal the live client would
     * be reading that pointer while the accept thread may be resetting it.
     * It wakes within ACCEPT_SLICE_MS and does the whole teardown itself,
     * which costs a quarter second and buys a shutdown path with no race
     * in it. */
    InterlockedExchange((LONG *)&m_stop, 1);
}

bool ServerService::join(DWORD timeoutMs)
{
    return m_thread.join(timeoutMs);
}

void ServerService::Accept::run()
{
    m_owner->acceptLoop();
}

bool ServerService::bindWithRetry()
{
    DWORD waited = 0;
    /* "" rather than "0.0.0.0" would also mean every interface; the config
     * default spells it out because that is what somebody reading the file
     * needs to see. */
    const char *host = m_cfg->bind[0] ? m_cfg->bind : NULL;

    for (;;)
    {
        if (m_listener.open(host, (int)m_cfg->port, 4))
            return true;
        if (m_stop || waited >= BIND_RETRY_MS)
            return false;
        if (waited == 0)
            rc_log("server: port %ld is busy, retrying for up to %d seconds "
                   "(a restart can land in the peer's TIME_WAIT)",
                   m_cfg->port, (int)(BIND_RETRY_MS / 1000));
        Sleep(BIND_RETRY_SLICE_MS);
        waited += BIND_RETRY_SLICE_MS;
    }
}

/* Answer a second caller without building a Connection for it: one frame
 * straight onto the socket, then a FIN. A whole Connection with two threads
 * for a single refusal would be the expensive way to say no. */
static void refuseBusy(SOCKET s)
{
    char buf[256];
    const char *keys[1];
    const char *vals[1];
    int n;

    keys[0] = "code";
    vals[0] = "busy";
    n = rc_frame_build(buf, sizeof buf, "ERR", keys, vals, 1, NULL, 0);
    if (n > 0)
        send(s, buf, n, 0);
    shutdown(s, 1);
}

void ServerService::serve(SocketGuard &s)
{
    struct sockaddr_in peer;
    int len = sizeof peer;

    if (getpeername(s.get(), (struct sockaddr *)&peer, &len) != 0)
    {
        rc_log("server: refusing a socket with no peer name");
        return;
    }
    /* BEFORE the handshake: a machine outside the range never gets to guess
     * at the secret, and never gets to hold the one client slot while it
     * tries. No frame back -- there is nothing to tell it. */
    if (!m_cfg->allow.allows((unsigned long)peer.sin_addr.s_addr))
    {
        rc_log("server: %s is outside allow=%s", inet_ntoa(peer.sin_addr),
               m_cfg->allowText);
        return;
    }
    if (m_session.valid())
    {
        rc_log("server: %s answered busy; client %lu is live",
               inet_ntoa(peer.sin_addr), m_generation);
        refuseBusy(s.get());
        return;
    }

    m_generation++;
    m_session.reset(new Session(*m_cfg, *m_exec, m_generation,
                                *m_procs, *m_app));
    if (!m_session.valid() || !m_session->begin(s.get()))
    {
        rc_log("server: could not take up the connection from %s",
               inet_ntoa(peer.sin_addr));
        m_session.reset();
        return;
    }
    /* adopt() owns the socket from its first line, including on failure. */
    s.release();
    InterlockedExchange((LONG *)&m_peerAddr, (LONG)peer.sin_addr.s_addr);
    InterlockedExchange((LONG *)&m_served, (LONG)m_generation);
    InterlockedExchange((LONG *)&m_status, STATUS_CONNECTED);
    rc_log("server: client %lu from %s", m_generation,
           inet_ntoa(peer.sin_addr));
}

void ServerService::acceptLoop()
{
    if (!bindWithRetry())
    {
        rc_log("server: could not listen on %s:%ld; nothing will reach this "
               "machine", m_cfg->bind, m_cfg->port);
        InterlockedExchange((LONG *)&m_status, STATUS_DOWN);
        return;
    }
    InterlockedExchange((LONG *)&m_boundPort, (LONG)m_listener.port());
    InterlockedExchange((LONG *)&m_status, STATUS_LISTENING);
    rc_log("server: listening on %s:%d, heartbeat %lds, allow=%s",
           m_cfg->bind, m_listener.port(), m_cfg->heartbeatS,
           m_cfg->allowText[0] ? m_cfg->allowText : "(anywhere)");

    while (!m_stop)
    {
        SocketGuard s;

        /* The teardown of a client the reader flagged, done here rather
         * than there -- see server.h. */
        if (m_session.valid() && m_session->dead())
        {
            rc_log("server: client %lu is gone", m_session->generation());
            if (!m_session->end())
                m_session.release();    /* leak rather than free under it */
            else
                m_session.reset();
            InterlockedExchange((LONG *)&m_peerAddr, 0);
            InterlockedExchange((LONG *)&m_status, STATUS_LISTENING);
        }
        if (m_listener.accept(s, ACCEPT_SLICE_MS))
            serve(s);
    }

    if (m_session.valid())
    {
        if (!m_session->end())
            m_session.release();
        else
            m_session.reset();
    }
    m_listener.close();
    InterlockedExchange((LONG *)&m_peerAddr, 0);
    InterlockedExchange((LONG *)&m_status, STATUS_DOWN);
}
