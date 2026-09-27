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

Session::Session(const ServerConfig &cfg, unsigned long generation,
                 Processes &procs)
    : m_cfg(&cfg), m_procs(&procs), m_generation(generation),
      m_conn(this, false), m_control(procs, m_conn), m_dead(0),
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

        if (strcmp(f->type, "PING") == 0)
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
        else
        {
            /* The job and fs lanes arrive next. Answered rather than
             * dropped meanwhile, for the same reason as everything else
             * here: silence costs the caller its whole budget. */
            r.err("not_implemented");
        }
    }
    rc_frame_delete(f);
}

bool Session::end()
{
    m_conn.drain(1000);
    m_conn.signalStop();
    bool joined = m_conn.join(3000);
    /* othersGone: there is no lane worker yet, so nothing outside the
     * Connection can still be inside send(). The lanes make this a real
     * question and they will answer it by being stopped first. */
    return m_conn.release(true) && joined;
}

/* ------------------------------------------------------- ServerService */

ServerService::ServerService(const ServerConfig &cfg, Processes &procs)
    : m_cfg(&cfg), m_procs(&procs), m_stop(0), m_generation(0)
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
    m_session.reset(new Session(*m_cfg, m_generation, *m_procs));
    if (!m_session.valid() || !m_session->begin(s.get()))
    {
        rc_log("server: could not take up the connection from %s",
               inet_ntoa(peer.sin_addr));
        m_session.reset();
        return;
    }
    /* adopt() owns the socket from its first line, including on failure. */
    s.release();
    rc_log("server: client %lu from %s", m_generation,
           inet_ntoa(peer.sin_addr));
}

void ServerService::acceptLoop()
{
    if (!bindWithRetry())
    {
        rc_log("server: could not listen on %s:%ld; nothing will reach this "
               "machine", m_cfg->bind, m_cfg->port);
        return;
    }
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
}
