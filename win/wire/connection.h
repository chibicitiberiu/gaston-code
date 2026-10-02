/* One framed TCP link: the socket, a reader and a sender thread, a bounded
 * outbound queue and, optionally, persistent compression. libwire's
 * transport, shared by the client (net/net.cpp, to the bridge) and gremote
 * (to its Python client). What the frames MEAN stays with each program: the
 * client's HELLO, its RPC worker and its window messages are in net.cpp.
 * docs/wire-connection-plan.md has the split and the invariants.
 *
 * THREADS. Three touch a Connection: the owner (connect, start, send, the
 * stop sequence), the reader (FrameSink::onFrame) and the sender (the only
 * thread that writes the socket). Anyone may send(): it builds the frame,
 * queues it and returns. The socket itself has no lock, deliberately; the
 * stop sequence's ORDER is what keeps it safe (see signalStop and release).
 *
 * STOPPING is four calls, because one cannot express it:
 *
 *   drain(ms)         let what is queued reach the wire; still accepting
 *   signalStop()      accept nothing more, wake the threads, send the FIN
 *   join(ms)          wait for the reader, then the sender
 *   release(others)   free what nothing can still touch; false = LEAK this
 *
 * A thread outside the Connection may also be sending (the client's RPC
 * worker answers from its own thread). The owner stops that one FIRST,
 * while sends still work so its last answers go out, and tells release()
 * whether it did stop. A false from release() means a thread is still
 * inside this object: leak it, never delete it.
 *
 * One connection per object. A reconnect is a new Connection, so a thread
 * left behind by the old one never finds its lock re-initialised under it.
 */
#ifndef RC_WIRE_CONNECTION_H
#define RC_WIRE_CONNECTION_H

#ifndef __cplusplus
#error "wire/connection.h is C++"
#endif

#include <windows.h>
#include <winsock.h>
#include "core/rc_cpp.h"
#include "core/thread.h"
#include "wire/frame.h"
#include "wire/link.h"
#include "wire/socket.h"

class Connection;

/* Where a Connection delivers. Both run on the Connection's threads. */
class FrameSink
{
public:
    virtual ~FrameSink() {}

    /* READER THREAD. Owns `f` from here: free it with rc_frame_delete or
     * hand it on. May wait for its own consumer while c.isRunning(), which
     * is ordinary TCP backpressure; must never block on a window (no
     * SendMessage: a window in a modal loop deadlocks 9x). */
    virtual void onFrame(Connection &c, rc_frame *f) = 0;

    /* Whichever thread changed the state. Must not block. */
    virtual void onState(Connection &c, rc_state s) = 0;
};

class Connection
{
public:
    /* `compress`: keep persistent inflate and deflate streams, as the
     * bridge link does. Without it nothing is compressed, a frame marked
     * enc=deflate drops the link, and neither scratch buffer (192 KB
     * between them) is ever allocated. Stores only; cannot fail. */
    Connection(FrameSink *sink, bool compress);
    ~Connection();

    /* Construct with compression available, but do not START the outbound
     * half until the peer has said it can inflate. Call before `start`.
     *
     * For a SERVER that learns what its client can do from the client's
     * first frame. gremote does: the Python declares `deflate=1` on AUTH and
     * gremote answers `HELLO deflate=1`, so a client that never says it
     * keeps a raw link and keeps working. That matters more than it sounds
     * -- a compressing peer talking to a non-compressing one drops the link
     * rather than degrading, and the only way to repair a box reachable by
     * CD is ship(), which needs the link.
     *
     * The INBOUND half still starts, so a peer that compresses is never
     * dropped for it. What waits is the 64KB deflate scratch and the
     * deflate stream, which a raw-link client then never pays for. */
    void deferDeflate();

    /* Turn the outbound half on. Takes the lock, because `m_lock` guards the
     * codec as well as the queue and the sender reads shouldCompress under
     * it -- a flag published without it would still leave mz_deflateInit
     * racing a compressBody. Safe to call more than once. */
    void allowDeflate();

    /* Resolve, connect, and accept sends. Reports CONNECTING, then FAILED
     * on any failure. Whatever it managed to create is released by the
     * normal stop sequence, so call that even after a false. */
    bool connect(const char *host, int port);

    /* The server side: take over a socket Listener::accept produced. Owns
     * it from the call, closing it on any refusal. Reports CONNECTING, then
     * FAILED on failure; the stop sequence releases the rest, as after
     * connect(). */
    bool adopt(SOCKET accepted);

    /* Drop the link when the peer sends nothing for `ms` (0: never, the
     * default). Before start(). The server's heartbeat: the peer sends a
     * frame at least this often (PING when idle). */
    void setIdleTimeout(DWORD ms) { m_idleMs = ms; }

    /* The sender, then CONNECTED, then the reader: a reader that dies at
     * once reports FAILED after CONNECTED, not before it. False: a thread
     * could not start and the link already refuses sends; run the stop
     * sequence. */
    bool start();

    /* Queue a frame. `waitTicks` 100ms slices of waiting for room (the
     * caller's budget: RC_SEND_WAIT_UI or _BULK); `compressible` lets the
     * link deflate it when that pays. RC_SEND_OK / _DEAD / _FULL. */
    /* `reserveBytes` is what this send must leave free in the out queue for
     * somebody else -- RC_OUTQ_RESERVE from a bulk sender on a server whose
     * control lane answers on the reader thread, 0 from that control reply
     * and from everyone in the client, where nothing answers on the reader.
     * A send that would eat into the reserve waits like a full queue and
     * then returns RC_SEND_FULL, rather than taking it. */
    int send(const char *type, const char *const *keys,
             const char *const *vals, int nkv, const void *body,
             unsigned bodyLen, int waitTicks, bool compressible,
             unsigned reserveBytes = 0);

    void drain(DWORD timeoutMs);
    void signalStop();
    /* Each thread gets `eachMs`. True if both are gone. */
    bool join(DWORD eachMs);
    /* `othersGone`: every thread outside this object that might still call
     * send() has been joined. True if everything was freed. */
    bool release(bool othersGone);

    bool isRunning() const { return m_running != 0; }
    /* For a sink that waits on a queue of its own and must give up when
     * the link does. */
    volatile LONG *runningFlag() { return &m_running; }

    unsigned long rxBytes() const { return m_rxBytes; }
    unsigned long txBytes() const { return m_txBytes; }
    unsigned outqBytes();

    /* The client's STAT frame: deflate's measured throughput, and the
     * link's, which decide whether compressing is worth it. */
    void takeDeflateSample(unsigned long *bytesIn, unsigned long *us);
    void setLinkBps(unsigned long bps);

private:
    Connection(const Connection &);
    Connection &operator=(const Connection &);

    struct OutItem;
    /* The largest frame rc_frame_build will make, plus its slack. */
    enum { BUILD_CAP = RC_MAX_FRAME + 1024 };
    class Codec;

    /* Two Runnables rather than one with a flag: the type is what says which
     * loop a Thread is running, and a bool at the call site would not.
     * C++98 gives a nested class no access to its enclosing class's
     * privates, hence the friend declarations -- the same shape App uses for
     * its hidden window. */
    class ReaderLoop : public Runnable
    {
    public:
        ReaderLoop() : m_conn(0) {}
        void attach(Connection *c) { m_conn = c; }
        void run();
    private:
        Connection *m_conn;
    };
    class SenderLoop : public Runnable
    {
    public:
        SenderLoop() : m_conn(0) {}
        void attach(Connection *c) { m_conn = c; }
        void run();
    private:
        Connection *m_conn;
    };
    friend class ReaderLoop;
    friend class SenderLoop;

    void readerLoop();
    void senderLoop();
    int recvExact(char *buf, unsigned len);
    bool prepare();
    void goLive();
    void clearQueue();
    void setState(rc_state s);

    FrameSink *m_sink;
    bool m_compress;
    /* Set by deferDeflate(): begin() then starts the inflater only, and the
     * deflater waits for allowDeflate(). */
    bool m_deferDeflate;
    Codec *m_codec;
    bool m_used;

    bool m_wsaUp;
    bool m_lockUp;
    SocketGuard m_sock;
    ReaderLoop m_readerBody;
    SenderLoop m_senderBody;
    Thread m_reader;
    Thread m_sender;
    CRITICAL_SECTION m_lock;    /* guards the queue and the codec, never the wire */
    OutItem *m_head;
    OutItem *m_tail;
    unsigned m_queued;
    Handle m_wake;              /* the sender waits on this */
    Handle m_room;              /* a full-queue writer waits on this */
    /* Where send() encodes a frame before copying it onto the queue; under
     * m_lock like the queue. BUILD_CAP bytes, allocated by connect(). */
    HeapPtr<char> m_build;
    volatile LONG m_running;
    unsigned long m_rxBytes;
    unsigned long m_txBytes;
    DWORD m_idleMs;
    DWORD m_readerJoin;
    DWORD m_senderJoin;
};

#endif
