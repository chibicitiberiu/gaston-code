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
#include "remote/reply.h"
#include "remote/fsops.h"
#include "remote/jobops.h"
#include "remote/lane.h"
#include "wire/connection.h"
#include "wire/listener.h"

/* One connected caller. A Connection is single-use, so this is made fresh
 * per client and destroyed when that client goes. */
class Session : public FrameSink
{
public:
    Session(const ServerConfig &cfg, const ExecConfig &exec,
            unsigned long generation, Processes &procs, App &app);

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
    /* Hand `f` to a lane, or answer busy. Takes ownership either way. */
    void toLane(LaneWorker &lane, rc_frame *f, bool continues);
    void doCancel(Responder &r, rc_frame *f);
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
    /* The lane workers are the Session's, not the service's: they hold a
     * Responder onto this Connection, so they must be stopped and joined
     * before it is released -- which is exactly what release(othersGone)
     * means by "others". */
    JobOps m_jobOps;
    LaneWorker m_job;
    FsOps m_fsOps;
    LaneWorker m_fs;
    volatile LONG m_dead;
    bool m_authed;
};

class ServerService : public Service
{
public:
    /* `app` is passed down to ControlOps so UPDATE can stop this process
     * once it has a replacement running that will put a server back. The
     * long argument for why that is not the remote QUIT docs/REMOTE.md
     * forbids is above ControlOps::update. */
    ServerService(const ServerConfig &cfg, const ExecConfig &exec,
                  Processes &procs, App &app);

    const char *name() const { return "server"; }
    bool init();
    bool start();
    void signalStop();
    bool join(DWORD timeoutMs);

    /* What the tray shows, and the only thing another thread may ask about
     * this service.
     *
     * Four scalars rather than a peek at m_session, because signalStop()'s
     * comment below is a rule and not a note: the accept thread is the only
     * reader or writer of m_session, and a status call that dereferenced it
     * from the main thread would race the teardown it does between accepts.
     * These are written by the accept thread through InterlockedExchange
     * and read raw -- an aligned 32-bit load on x86 is atomic, and the
     * worst a stale read costs is a tooltip that is one state behind. */
    enum Status
    {
        STATUS_BINDING = 0,   /* not started, or in the four-minute retry */
        STATUS_LISTENING,     /* bound, no client */
        STATUS_CONNECTED,     /* a client is live */
        STATUS_DOWN           /* the bind gave up; nothing will arrive */
    };
    Status status() const { return (Status)m_status; }
    /* The port actually bound, which is the configured one unless it was 0. */
    int boundPort() const { return (int)m_boundPort; }
    /* The live client's address, in network order; 0 when there is none.
     * Not a string: inet_ntoa has one static buffer per process and the
     * accept thread is already using it. */
    unsigned long peerAddr() const { return (unsigned long)m_peerAddr; }
    /* Clients taken up since this process started. */
    unsigned long served() const { return (unsigned long)m_served; }

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
    const ExecConfig *m_exec;
    Processes *m_procs;
    App *m_app;
    Listener m_listener;
    Accept m_accept;
    Thread m_thread;
    volatile LONG m_stop;
    /* The snapshot above. Accept-thread writes, any-thread reads. */
    volatile LONG m_status;
    volatile LONG m_boundPort;
    volatile LONG m_peerAddr;
    volatile LONG m_served;
    unsigned long m_generation;
    UniqueObj<Session> m_session;
};

#endif
