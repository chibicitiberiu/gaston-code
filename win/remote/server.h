/* The listening half: one accept loop, at most one live client.
 *
 * SHAPE (docs/REMOTE.md). gremote listens and the caller connects, the
 * reverse of the client. While a client is live a second connection is
 * accepted, answered `ERR code=busy` and closed gracefully -- it never
 * disturbs the live one. A client that dies without a FIN (a killed python,
 * a pulled cable) would otherwise hold the box until TCP keepalive gives up,
 * about two hours, so the reader's idle timeout drops it after
 * heartbeat_timeout_s and the loop goes back to accepting.
 *
 * WHY THE BIND RETRY IS ON THE THREAD AND NOT IN init(). A restart can land
 * in the peer's TIME_WAIT, and REMOTE.md's answer is to retry for up to
 * 240 seconds rather than reach for SO_REUSEADDR, which on Winsock lets a
 * second process share the port and steal connections. But App runs
 * initialize, then the services, and only THEN enters its message loop -- so
 * four minutes of retrying inside init() or start() would leave a created
 * top-level window not pumping, which on 9x is a shell broadcast timing out
 * and WM_ENDSESSION going unanswered. The listener therefore starts unbound
 * and binds on its own thread.
 *
 * WHO TEARS A DEAD CLIENT DOWN. Not the reader thread that noticed: joining
 * a Connection from inside its own reader is joining yourself. onState only
 * sets a flag, and the accept loop -- which is already awake every 250ms to
 * poll its stop flag -- does the teardown between accepts.
 */
#ifndef RC_REMOTE_SERVER_H
#define RC_REMOTE_SERVER_H

#ifndef __cplusplus
#error "remote/server.h is C++"
#endif

#include "core/app.h"
#include "core/rc_cpp.h"
#include "core/thread.h"
#include "exec/process.h"
#include "remote/config.h"
#include "remote/control.h"
#include "wire/connection.h"
#include "wire/listener.h"

/* One connected caller. A Connection is single-use, so this is made fresh
 * per client and destroyed when that client goes. */
class Session : public FrameSink
{
public:
    Session(const ServerConfig &cfg, unsigned long generation,
            Processes &procs);

    /* Adopt the accepted socket and start the threads. */
    bool begin(SOCKET accepted);

    /* FrameSink, both on the reader thread. */
    void onFrame(Connection &c, rc_frame *f);
    void onState(Connection &c, rc_state s);

    /* Set by onState; read by the accept loop. */
    bool dead() const { return m_dead != 0; }
    void markDead() { InterlockedExchange((LONG *)&m_dead, 1); }

    /* Drain, FIN, join, release. True if everything came back. */
    bool end();

    unsigned long generation() const { return m_generation; }

private:
    Session(const Session &);
    Session &operator=(const Session &);

    void onAuth(rc_frame *f);
    void sendHello();
    /* `code` goes out as ERR code=<code>, and `fatal` ends the session
     * after it -- with the one-second pause the handshake owes a wrong
     * secret, so a wrong guess costs a second and an abrupt RST never
     * reaches Win98's Winsock. */
    void refuse(const char *code, bool fatal);

    const ServerConfig *m_cfg;
    const Processes *m_procs;
    unsigned long m_generation;
    /* Declared before m_control, which is handed a reference to it. */
    Connection m_conn;
    ControlOps m_control;
    volatile LONG m_dead;
    bool m_authed;
};

class ServerService : public Service
{
public:
    ServerService(const ServerConfig &cfg, Processes &procs);

    const char *name() const { return "server"; }
    bool init();
    bool start();
    void signalStop();
    bool join(DWORD timeoutMs);

private:
    class Accept : public Runnable
    {
    public:
        Accept() : m_owner(0) {}
        void attach(ServerService *s) { m_owner = s; }
        void run();
    private:
        ServerService *m_owner;
    };
    friend class Accept;

    ServerService(const ServerService &);
    ServerService &operator=(const ServerService &);

    void acceptLoop();
    bool bindWithRetry();
    void serve(SocketGuard &s);

    const ServerConfig *m_cfg;
    Processes *m_procs;
    Listener m_listener;
    Accept m_accept;
    Thread m_thread;
    volatile LONG m_stop;
    unsigned long m_generation;
    UniqueObj<Session> m_session;
};

#endif
