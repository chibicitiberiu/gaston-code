/* libwire end to end: a Connection talking to another Connection that was
 * adopted from a Listener, over loopback, in one process.
 *
 * The real wire/connection.cpp and wire/listener.cpp, run under Wine by
 * tests/python/guest/test_guest_wire.py. Prints `verdict<TAB>case<TAB>detail`
 * rows. This is the server half's first user -- gremote does not exist yet --
 * so it proves adopt(), the idle timeout and the listener before anything
 * depends on them.
 */
#include <windows.h>
#include <winsock.h>
#include <stdio.h>
#include <string.h>
#include <list>
#include "core/rc_cpp.h"
#include "core/log.h"
#include "core/str.h"
#include "wire/connection.h"
#include "wire/listener.h"

static void say(const char *name, bool pass, const char *detail)
{
    printf("%s\t%s\t%s\n", pass ? "pass" : "FAIL", name, detail);
    fflush(stdout);
}

/* Keeps what a Connection delivers, for the main thread to wait on. */
class Recorder : public FrameSink
{
public:
    Recorder() : m_state(RC_DISCONNECTED)
    {
        InitializeCriticalSection(&m_lock);
        m_event.reset(CreateEventA(NULL, FALSE, FALSE, NULL));
    }

    ~Recorder()
    {
        while (!m_frames.empty())
        {
            rc_frame_delete(m_frames.front());
            m_frames.pop_front();
        }
        DeleteCriticalSection(&m_lock);
    }

    void onFrame(Connection &c, rc_frame *f)
    {
        (void)c;
        {
            ScopedLock lock(&m_lock);
            m_frames.push_back(f);
        }
        SetEvent(m_event.get());
    }

    void onState(Connection &c, rc_state s)
    {
        (void)c;
        m_state = s;
        SetEvent(m_event.get());
    }

    /* The next frame, or NULL after `ms`. The caller frees it. */
    rc_frame *next(DWORD ms)
    {
        DWORD start = GetTickCount();
        for (;;)
        {
            {
                ScopedLock lock(&m_lock);
                if (!m_frames.empty())
                {
                    rc_frame *f = m_frames.front();
                    m_frames.pop_front();
                    return f;
                }
            }
            DWORD spent = GetTickCount() - start;
            if (spent >= ms)
                return NULL;
            WaitForSingleObject(m_event.get(), ms - spent);
        }
    }

    bool waitState(rc_state want, DWORD ms)
    {
        DWORD start = GetTickCount();
        while (m_state != want)
        {
            DWORD spent = GetTickCount() - start;
            if (spent >= ms)
                return false;
            WaitForSingleObject(m_event.get(), ms - spent);
        }
        return true;
    }

    rc_state state() const { return m_state; }

private:
    CRITICAL_SECTION m_lock;
    Handle m_event;
    std::list<rc_frame *> m_frames;
    volatile rc_state m_state;
};

/* The four-call stop, as an owner runs it. True if both threads joined and
 * everything was freed. */
static bool stop(Connection &c)
{
    c.drain(1000);
    c.signalStop();
    bool joined = c.join(3000);
    return c.release(true) && joined;
}

/* The close, as two owners run it. `first` starts it: its FIN ends the
 * other end's reader, the other end's owner sees FAILED and runs its own
 * stop, and THAT end's FIN is what ends `first`'s reader. Only then can
 * `first` join. Stopping one end fully before the other reacts is a join
 * that waits out its timeout, by design: a Connection never FINs on its own,
 * because its owner decides when the last acks have left. */
static bool closeBoth(Connection &first, Connection &second,
                      Recorder &secondSink)
{
    first.drain(1000);
    first.signalStop();
    bool heard = secondSink.state() == RC_FAILED ||
                 secondSink.state() == RC_DISCONNECTED ||
                 secondSink.waitState(RC_FAILED, 3000);
    bool secondOk = stop(second);
    bool joined = first.join(3000);
    return first.release(true) && joined && secondOk && heard;
}

static bool sendText(Connection &c, const char *type, const char *body,
                     unsigned len, bool compressible)
{
    return c.send(type, NULL, NULL, 0, body, len, RC_SEND_WAIT_BULK,
                  compressible) == RC_SEND_OK;
}

/* A client and the server Connection it was accepted into. */
struct Pair
{
    Recorder clientSink;
    Recorder serverSink;
    Connection client;
    Connection server;

    Pair(bool clientCompress, bool serverCompress)
        : client(&clientSink, clientCompress),
          server(&serverSink, serverCompress)
    {
    }

    /* Connect through `l`, adopt, set the server's idle timeout, start. */
    bool up(Listener &l, DWORD serverIdleMs)
    {
        if (!client.connect("127.0.0.1", l.port()))
            return false;
        SocketGuard s;
        if (!l.accept(s, 3000))
            return false;
        if (!server.adopt(s.release()))
            return false;
        server.setIdleTimeout(serverIdleMs);
        return client.start() && server.start();
    }
};

static void listenerCases(Listener &l)
{
    char detail[64];

    rc_strf(detail, sizeof detail, "port %d", l.port());
    say("listen-port-zero-picks-one", l.port() > 0, detail);

    {
        /* No SO_REUSEADDR: a second bind of a listening port must fail
         * rather than share it. */
        Listener second;
        bool opened = second.open("127.0.0.1", l.port(), 1);
        say("no-port-sharing", !opened, opened ? "second bind succeeded"
                                               : "refused");
    }

    {
        SocketGuard s;
        DWORD t0 = GetTickCount();
        bool got = l.accept(s, 300);
        DWORD spent = GetTickCount() - t0;
        rc_strf(detail, sizeof detail, "%lums", (unsigned long)spent);
        say("accept-times-out", !got && !s.valid() && spent >= 200 &&
                                    spent < 3000, detail);
    }
}

static void traffic(Listener &l)
{
    char detail[96];
    Pair p(true, true);

    bool up = p.up(l, 0);
    say("connect-adopt-start", up && p.clientSink.waitState(RC_CONNECTED, 2000)
                                   && p.serverSink.waitState(RC_CONNECTED, 2000),
        up ? "" : "see log");
    if (!up)
        return;

    {
        sendText(p.client, "PING", "hello", 5, false);
        rc_frame *f = p.serverSink.next(3000);
        bool ok = f && strcmp(f->type, "PING") == 0 && f->body_len == 5 &&
                  memcmp(f->body, "hello", 5) == 0;
        say("client-to-server", ok, f ? f->type : "nothing arrived");
        rc_frame_delete(f);
    }
    {
        sendText(p.server, "PONG", "world", 5, false);
        rc_frame *f = p.clientSink.next(3000);
        bool ok = f && strcmp(f->type, "PONG") == 0 && f->body_len == 5 &&
                  memcmp(f->body, "world", 5) == 0;
        say("server-to-client", ok, f ? f->type : "nothing arrived");
        rc_frame_delete(f);
    }
    {
        /* Compressible and actually compressed: the frame arrives marked
         * enc=deflate and inflated back to the same bytes. */
        static char body[20000];
        for (unsigned i = 0; i < sizeof body; i++)
            body[i] = (char)('a' + (i / 7) % 26);
        sendText(p.client, "BULK", body, sizeof body, true);
        rc_frame *f = p.serverSink.next(3000);
        const char *enc = f ? rc_frame_get(f, "enc") : NULL;
        bool ok = f && f->body_len == sizeof body &&
                  memcmp(f->body, body, sizeof body) == 0 && enc &&
                  strcmp(enc, "deflate") == 0;
        rc_strf(detail, sizeof detail, "len=%u enc=%s",
                f ? f->body_len : 0, enc ? enc : "-");
        say("deflated-round-trip", ok, detail);
        rc_frame_delete(f);
    }
    {
        /* Near the frame cap, not compressed. */
        static char body[60000];
        for (unsigned i = 0; i < sizeof body; i++)
            body[i] = (char)(i * 131 + 7);
        sendText(p.server, "BIG", body, sizeof body, false);
        rc_frame *f = p.clientSink.next(5000);
        bool ok = f && f->body_len == sizeof body &&
                  memcmp(f->body, body, sizeof body) == 0;
        rc_strf(detail, sizeof detail, "len=%u", f ? f->body_len : 0);
        say("near-cap-frame", ok, detail);
        rc_frame_delete(f);
    }
    {
        char n[16];
        for (int i = 0; i < 50; i++)
        {
            rc_strf(n, sizeof n, "%d", i);
            sendText(p.client, "SEQ", n, (unsigned)strlen(n), false);
        }
        int got = 0;
        bool inOrder = true;
        for (int i = 0; i < 50; i++)
        {
            rc_frame *f = p.serverSink.next(3000);
            if (!f)
                break;
            rc_strf(n, sizeof n, "%d", i);
            if (f->body_len != strlen(n) || memcmp(f->body, n, f->body_len))
                inOrder = false;
            got++;
            rc_frame_delete(f);
        }
        rc_strf(detail, sizeof detail, "%d of 50", got);
        say("fifty-in-order", got == 50 && inOrder, detail);
    }

    say("both-stop-clean", closeBoth(p.client, p.server, p.serverSink), "");
}

static void heartbeat(Listener &l)
{
    char detail[64];
    {
        /* A silent peer is dropped after the idle timeout. */
        Pair p(false, false);
        bool up = p.up(l, 400);
        DWORD t0 = GetTickCount();
        bool dropped = up && p.serverSink.waitState(RC_FAILED, 3000);
        DWORD spent = GetTickCount() - t0;
        rc_strf(detail, sizeof detail, "dropped after %lums",
                (unsigned long)spent);
        say("idle-peer-dropped", dropped && spent >= 300, detail);
        /* The server's owner closes; its FIN tells the client. */
        bool closed = closeBoth(p.server, p.client, p.clientSink);
        say("client-hears-the-drop", closed,
            rc_state_name(p.clientSink.state()));
    }
    {
        /* A peer that keeps talking is kept, however long it talks. */
        Pair p(false, false);
        bool up = p.up(l, 400);
        for (int i = 0; up && i < 15; i++)
        {
            sendText(p.client, "PING", NULL, 0, false);
            Sleep(100);
            rc_frame *f;
            while ((f = p.serverSink.next(0)) != NULL)
                rc_frame_delete(f);
        }
        say("heartbeat-keeps-it", up && p.server.isRunning(),
            rc_state_name(p.serverSink.state()));
        say("heartbeat-pair-stops-clean",
            closeBoth(p.client, p.server, p.serverSink), "");
    }
}

static void refusal(Listener &l)
{
    /* A peer that does not compress refuses a deflated frame and drops the
     * link rather than hand compressed bytes on as if they were plain. */
    Pair p(true, false);
    bool up = p.up(l, 0);
    static char body[4000];
    memset(body, 'z', sizeof body);
    sendText(p.client, "BULK", body, sizeof body, true);
    bool dropped = up && p.serverSink.waitState(RC_FAILED, 3000);
    rc_frame *f = p.serverSink.next(0);
    say("uncompressed-peer-refuses-deflate", dropped && !f,
        f ? "a frame got through" : rc_state_name(p.serverSink.state()));
    rc_frame_delete(f);
    say("refusing-pair-stops-clean",
        closeBoth(p.server, p.client, p.clientSink), "");
}

/* --- the out-queue reserve ------------------------------------------------
 *
 * A server whose control lane answers on the reader thread (docs/REMOTE.md)
 * shares one out queue between a streaming GET and the PONG that keeps the
 * peer from declaring it dead. Before the reserve, whichever got there first
 * could take all 64KB.
 *
 * Driven WITHOUT starting either end's threads: connect() sets the queue up
 * and makes the Connection live, start() is what creates the sender that
 * drains it. So nothing drains, every send is refused or accepted on the
 * arithmetic alone, and there is no race to lose. waitTicks is 0 throughout,
 * because a full queue that nobody is draining will not empty.
 */
static void reserveCases(Listener &l)
{
    static char body[2048];
    static char detail[96];
    Recorder sink;
    Connection c(&sink, false);
    SocketGuard peer;
    int bulk = 0, extra = 0;
    bool controlGotIn = false;

    memset(body, 'x', sizeof body);
    if (!c.connect("127.0.0.1", l.port()) || !l.accept(peer, 3000))
    {
        say("reserve-setup", false, "could not connect or accept");
        return;
    }

    /* Fill until a BULK send is refused. It is refused at
     * RC_OUTQ_MAX_BYTES - RC_OUTQ_RESERVE, not at the top of the queue. */
    while (bulk < 64 &&
           c.send("BULK", NULL, NULL, 0, body, sizeof body, 0, false,
                  RC_OUTQ_RESERVE) == RC_SEND_OK)
        bulk++;
    rc_strf(detail, sizeof detail, "%d frames of %u, reserve %u of %u",
            bulk, (unsigned)sizeof body, (unsigned)RC_OUTQ_RESERVE,
            (unsigned)RC_OUTQ_MAX_BYTES);
    say("reserve-refuses-bulk-before-the-queue-is-full", bulk > 0 && bulk < 64,
        detail);

    /* THE PROPERTY: with bulk shut out, the reply a reader owes still goes. */
    controlGotIn = c.send("PONG", NULL, NULL, 0, NULL, 0, 0, false, 0)
                   == RC_SEND_OK;
    say("the-reserve-is-there-for-the-control-reply", controlGotIn, "");

    /* And the room was real: sends with no reserve keep going until the
     * whole 64KB is gone, which is also the client's unchanged behaviour --
     * every caller in win/net passes no reserve at all. */
    while (extra < 64 &&
           c.send("BULK", NULL, NULL, 0, body, sizeof body, 0, false, 0)
               == RC_SEND_OK)
        extra++;
    rc_strf(detail, sizeof detail, "%d more frames of %u once the reserve "
            "was open", extra, (unsigned)sizeof body);
    say("no-reserve-still-fills-the-whole-queue", extra > 0, detail);

    c.signalStop();
    bool joined = c.join(3000);
    say("an-unstarted-connection-stops-clean", c.release(true) && joined, "");
}

int main(void)
{
    rc_log_open(".");
    Listener l;
    if (!l.open("127.0.0.1", 0, 4))
    {
        say("listen", false, "open failed; see retroclaude.log");
        return 1;
    }
    listenerCases(l);
    traffic(l);
    heartbeat(l);
    refusal(l);
    reserveCases(l);
    l.close();
    rc_log_close();
    return 0;
}
