#include "wire/connection.h"

#include <string.h>
#include "core/log.h"
#include "miniz.h"

/* One queued outbound frame: already encoded, owned by the queue. */
struct Connection::OutItem
{
    OutItem *next;
    unsigned len;
    unsigned sent;
    char data[1];   /* len bytes follow */
};

/* THE LINK'S COMPRESSION STATE.
 *
 * Threads. The inflate half is touched only by the READER, inside
 * inflateBody(). The deflate half and the stats are touched under the
 * connection's lock, by whoever is sending. begin() runs before either
 * thread exists; the two finish calls only once release() has decided no
 * thread can reach them.
 *
 * Every member has one caller and is `inline`: Watcom emits an out-of-line
 * copy of a member defined outside its class even after inlining it (+2 KB
 * of code, measured). */
class Connection::Codec
{
public:
    /* Not optional. `new Codec()` does NOT zero a class like this one under
     * Watcom's C++98: private members make it a non-POD, and C++98 gives a
     * non-POD with no constructor default-initialisation, i.e. heap garbage.
     * begin() then read a garbage infReady, called mz_inflateEnd on a
     * garbage stream and jumped through its garbage zfree -- netcheck died
     * right after "connected". The file static this replaced was zero by
     * being static. */
    Codec()
        : infReady(0),
          infScratch(0),
          defReady(0),
          defScratch(0),
          deflateIn(0),
          deflateOut(0),
          deflateUs(0),
          deflateSamples(0),
          deflateBps(0),
          linkBps(0),
          perfInit(0)
    {
        memset(&inf, 0, sizeof(inf));
        memset(&def, 0, sizeof(def));
        perfFreq.QuadPart = 0;
    }

    /* One persistent inflate stream for the whole connection.
     *
     * The bridge compresses with a persistent deflate stream and Z_SYNC_FLUSH
     * per frame, which keeps the dictionary warm across frames -- worth 1.27x
     * over compressing each frame alone. That only works if this side keeps
     * its state too, so this is deliberately NOT per-frame. Both sides reset
     * on reconnect, in lockstep. */
    mz_stream inf;
    int infReady;

    /* ONE inflate scratch for the whole connection, not one per frame.
     *
     * inflate_body used to HeapAlloc its full output cap for EVERY compressed
     * frame and never shrink it: f->body_len recorded the real length while
     * f->_body_alloc held the entire block until the UI thread freed the
     * frame. With the reader posting up to RC_MAX_INFLIGHT frames before it
     * blocks, that is a per-frame cost measured in hundreds of KB, multiplied
     * by however many frames the UI has not drained -- and the UI thread
     * stalls whenever MSHTML reflows or a folder browser is open. On a 32MB
     * machine that is the largest transient demand in the program, and when
     * HeapAlloc finally failed the reader dropped the LINK rather than slowing
     * down.
     *
     * Now: inflate into this one buffer, then allocate exactly the bytes that
     * came out and copy. Peak concurrent cost becomes one scratch plus the
     * real sizes.
     *
     * Heap rather than BSS so it is not committed at load on a machine where
     * committed-at-load is audited; allocated per connection and released
     * with the stream it serves.
     *
     * 2x RC_MAX_FRAME, not 4x. The cap exists so a corrupt or hostile frame
     * cannot expand without limit, and the real bound is far lower:
     * bridge/wire/link.py caps an UNCOMPRESSED chunk at MAX_FRAME - 1024 =
     * 64,512 bytes before it compresses, so a legitimate inflated body never
     * exceeds ~63KB. 2x leaves more than double the largest value the protocol
     * permits. */
    char *infScratch;

    /* One persistent deflate stream for the whole connection (this side to
     * the peer). */
    mz_stream def;
    int defReady;
    char *defScratch;

    /* Measurement counters for adaptive compression (03.3 / A4). */
    unsigned long deflateIn;
    unsigned long deflateOut;
    unsigned long deflateUs;
    unsigned long deflateSamples;
    unsigned long deflateBps;
    unsigned long linkBps;
    LARGE_INTEGER perfFreq;
    int perfInit;

    enum
    {
        INFLATE_SCRATCH = RC_MAX_FRAME * 2,
        DEFLATE_SCRATCH = RC_MAX_FRAME + 1024
    };

    /* A fresh pair of streams and zeroed stats. */
    void begin(bool withDeflate);
    /* Separate, because their gates differ: see release(). */
    void finishInflate();
    void finishDeflate();

    int inflateBody(rc_frame *f);
    bool shouldCompress(unsigned bodyLen) const;
    /* The compressed bytes, in defScratch, or null if deflate failed. */
    const char *compressBody(const void *body, unsigned bodyLen,
                             unsigned *outLen);
    void takeSample(unsigned long *bytesIn, unsigned long *us);
    /* Public because the outbound half can start LATE, when a peer says it
     * can inflate. Idempotent: it ends a live stream first. */
    void beginDeflate();

private:
    void beginInflate();
    unsigned long elapsedSince(const LARGE_INTEGER &t0);
};

inline void Connection::Codec::beginInflate()
{
    if (infReady)
    {
        mz_inflateEnd(&inf);
        infReady = 0;
    }
    memset(&inf, 0, sizeof(inf));
    if (mz_inflateInit(&inf) == MZ_OK)
        infReady = 1;
    else
        rc_log("net: mz_inflateInit failed");
    if (!infScratch)
    {
        infScratch = (char *)HeapAlloc(GetProcessHeap(), 0,
                                       INFLATE_SCRATCH + 1);
        /* Not fatal here: inflateBody reports the failure per frame, which
         * is where there is a frame to fail. */
        if (!infScratch)
            rc_log("net: no memory for the %u byte inflate scratch",
                   (unsigned)INFLATE_SCRATCH);
    }
}

inline void Connection::Codec::beginDeflate()
{
    if (defReady)
    {
        mz_deflateEnd(&def);
        defReady = 0;
    }
    memset(&def, 0, sizeof(def));
    if (mz_deflateInit(&def, 1) == MZ_OK)
        defReady = 1;
    else
        rc_log("net: mz_deflateInit failed");
    if (!defScratch)
    {
        defScratch = (char *)HeapAlloc(GetProcessHeap(), 0, DEFLATE_SCRATCH);
        if (!defScratch)
            rc_log("net: no memory for the %u byte deflate scratch",
                   (unsigned)DEFLATE_SCRATCH);
    }
}

/* `withDeflate` false starts the inflater only. shouldCompress already
 * refuses while `defReady` is 0, so nothing compresses until beginDeflate()
 * is called -- the gate was there, it just had nobody deciding it late. */
inline void Connection::Codec::begin(bool withDeflate)
{
    beginInflate();
    if (withDeflate)
        beginDeflate();
    deflateIn = 0;
    deflateOut = 0;
    deflateUs = 0;
    deflateSamples = 0;
    deflateBps = 0;
    linkBps = 0;
}

inline void Connection::Codec::finishInflate()
{
    if (infReady)
    {
        mz_inflateEnd(&inf);
        infReady = 0;
    }
    if (infScratch)
    {
        HeapFree(GetProcessHeap(), 0, infScratch);
        infScratch = NULL;
    }
}

inline void Connection::Codec::finishDeflate()
{
    if (defReady)
    {
        mz_deflateEnd(&def);
        defReady = 0;
    }
    if (defScratch)
    {
        HeapFree(GetProcessHeap(), 0, defScratch);
        defScratch = NULL;
    }
}

/* Inflates f->body in place-ish: allocates a new buffer, points body at it,
 * and records it for rc_frame_free. Returns 0 on success. */
inline int Connection::Codec::inflateBody(rc_frame *f)
{
    /* Bounded so a malicious or corrupt frame cannot expand without limit.
     * See the note on infScratch for why this is one shared buffer and why
     * the bound is 2x rather than 4x. */
    unsigned cap = INFLATE_SCRATCH;

    if (!infReady)
    {
        rc_log("net: deflate frame but no inflater");
        return -1;
    }
    if (f->body_len == 0)
        return 0;
    if (!infScratch)
    {
        rc_log("net: no inflate scratch");
        return -1;
    }

    inf.next_in = (const unsigned char *)f->body;
    inf.avail_in = f->body_len;
    inf.next_out = (unsigned char *)infScratch;
    inf.avail_out = cap;

    int rc = mz_inflate(&inf, MZ_SYNC_FLUSH);
    if (rc != MZ_OK && rc != MZ_STREAM_END)
    {
        rc_log("net: mz_inflate failed: %d", rc);
        return -1;
    }
    if (inf.avail_in != 0)
    {
        rc_log("net: inflate left %u bytes unconsumed (output cap hit)",
               inf.avail_in);
        return -1;
    }

    /* EXACTLY what came out. This is the allocation the frame carries until
     * the UI thread frees it, so it is the one that multiplies by the number
     * of frames in flight. */
    unsigned got = cap - inf.avail_out;
    char *out = (char *)HeapAlloc(GetProcessHeap(), 0, got + 1);
    if (!out)
    {
        rc_log("net: no memory for a %u byte inflated body", got);
        return -1;
    }
    memcpy(out, infScratch, got);

    f->_body_alloc = out;
    f->body_len = got;
    out[got] = '\0';
    f->body = out;
    return 0;
}

/* Compress while the first few samples are still coming in, then only while
 * the link is slower than half of what deflate manages: on a fast link the
 * CPU time costs more than the bytes it saves. */
inline bool Connection::Codec::shouldCompress(unsigned bodyLen) const
{
    if (bodyLen < 64 || !defReady || !defScratch)
        return false;
    if (deflateSamples < 5 || deflateBps == 0)
        return true;
    return linkBps < deflateBps / 2;
}

inline unsigned long Connection::Codec::elapsedSince(const LARGE_INTEGER &t0)
{
    unsigned long us;
    if (perfFreq.QuadPart > 0)
    {
        LARGE_INTEGER t1;
        QueryPerformanceCounter(&t1);
        us = (unsigned long)(((t1.QuadPart - t0.QuadPart) * 1000000ULL) /
                             perfFreq.QuadPart);
    }
    else
    {
        us = (GetTickCount() - t0.LowPart) * 1000UL;
    }
    return us ? us : 1;
}

inline const char *Connection::Codec::compressBody(const void *body, unsigned bodyLen,
                               unsigned *outLen)
{
    LARGE_INTEGER t0;

    if (!perfInit)
    {
        if (!QueryPerformanceFrequency(&perfFreq) || perfFreq.QuadPart <= 0)
            perfFreq.QuadPart = 0;
        perfInit = 1;
    }
    if (perfFreq.QuadPart > 0)
        QueryPerformanceCounter(&t0);
    else
        t0.LowPart = GetTickCount();

    def.next_in = (const unsigned char *)body;
    def.avail_in = bodyLen;
    def.next_out = (unsigned char *)defScratch;
    def.avail_out = DEFLATE_SCRATCH;

    int rc = mz_deflate(&def, MZ_SYNC_FLUSH);
    unsigned long us = elapsedSince(t0);

    if (rc != MZ_OK || def.avail_in != 0)
    {
        rc_log("net: mz_deflate failed: %d, avail_in=%u", rc, def.avail_in);
        return NULL;
    }
    *outLen = DEFLATE_SCRATCH - def.avail_out;
    deflateIn += bodyLen;
    deflateOut += *outLen;
    deflateUs += us;
    deflateSamples++;
    if (deflateUs > 0)
        deflateBps = (unsigned long)((double)deflateIn * 1000000.0 / deflateUs);
    return defScratch;
}

inline void Connection::Codec::takeSample(unsigned long *bytesIn, unsigned long *us)
{
    if (bytesIn)
        *bytesIn = deflateIn;
    if (us)
        *us = deflateUs;
    if (deflateUs > 0)
        deflateBps = (unsigned long)((double)deflateIn * 1000000.0 / deflateUs);
    deflateIn = 0;
    deflateUs = 0;
}

const char *rc_state_name(rc_state s)
{
    switch (s)
    {
    case RC_DISCONNECTED:
        return "disconnected";
    case RC_CONNECTING:
        return "connecting";
    case RC_CONNECTED:
        return "connected";
    default:
        return "failed";
    }
}

/* ------------------------------------------------------------ lifetime */

Connection::Connection(FrameSink *sink, bool compress)
    : m_sink(sink),
      m_compress(compress),
      m_deferDeflate(false),
      m_codec(0),
      m_used(false),
      m_wsaUp(false),
      m_lockUp(false),
      m_head(0),
      m_tail(0),
      m_queued(0),
      m_running(0),
      m_rxBytes(0),
      m_txBytes(0),
      m_idleMs(0),
      m_readerJoin(WAIT_OBJECT_0),
      m_senderJoin(WAIT_OBJECT_0)
{
}

/* Reached only when release() returned true, or when nothing was ever
 * started: a Connection that release() refused is leaked, never deleted. So
 * everything here is already gone or was never made, and this is the safety
 * net for a connect() that failed before any thread existed. */
Connection::~Connection()
{
    if (m_reader.started() || m_sender.started())
        rc_log("net: a Connection was deleted with a thread handle open");
    m_sock.reset();
    if (m_lockUp)
    {
        clearQueue();
        DeleteCriticalSection(&m_lock);
    }
    if (m_codec)
    {
        m_codec->finishInflate();
        m_codec->finishDeflate();
        delete m_codec;
    }
    if (m_wsaUp)
        WSACleanup();
}

void Connection::setState(rc_state s)
{
    m_sink->onState(*this, s);
}

/* ------------------------------------------------------------- connect */

/* What connect() and adopt() share: everything but the socket. Reports
 * CONNECTING, and FAILED on the way out of a failure. */
bool Connection::prepare()
{
    /* One connection per object. A reconnect is a new Connection: the old
     * one's threads may still be inside it (release() said so), and
     * re-initialising a held critical section is a fault inside KERNEL32 --
     * which is what three bridge restarts once reproduced. */
    if (m_used)
    {
        rc_log("net: a Connection is single-use; make a new one");
        return false;
    }
    m_used = true;

    setState(RC_CONNECTING);

    InitializeCriticalSection(&m_lock);
    m_lockUp = true;
    /* Auto-reset: the sender takes one item per wake and loops until empty. */
    m_wake.reset(CreateEventA(NULL, FALSE, FALSE, NULL));
    /* Manual-reset would wake every blocked writer at once; auto-reset wakes
     * one, which is what "a slot freed up" means. */
    m_room.reset(CreateEventA(NULL, FALSE, FALSE, NULL));
    if (!m_wake.valid() || !m_room.valid())
    {
        /* A null event makes every wait on it fail at once: the sender
         * would spin. */
        rc_log("net: CreateEvent failed: %lu", GetLastError());
        setState(RC_FAILED);
        return false;
    }

    m_build.reset((char *)HeapAlloc(GetProcessHeap(), 0, BUILD_CAP));
    if (!m_build.valid())
    {
        rc_log("net: no memory for the %u byte build buffer",
               (unsigned)BUILD_CAP);
        setState(RC_FAILED);
        return false;
    }

    WSADATA wsa;
    if (WSAStartup(MAKEWORD(1, 1), &wsa) != 0)
    {
        rc_log("net: WSAStartup failed");
        setState(RC_FAILED);
        return false;
    }
    /* Only now: a failed WSAStartup must not be balanced by a WSACleanup. */
    m_wsaUp = true;
    return true;
}

/* The socket is live: the codec, and sends are accepted from here. */
void Connection::goLive()
{
    if (m_compress)
    {
        m_codec = new Codec;
        m_codec->begin(!m_deferDeflate);
    }
    m_running = 1;
}

bool Connection::connect(const char *host, int port)
{
    if (!prepare())
        return false;

    m_sock.reset(socket(AF_INET, SOCK_STREAM, 0));
    if (!m_sock.valid())
    {
        rc_log("net: socket() failed: %d", WSAGetLastError());
        setState(RC_FAILED);
        return false;
    }

    unsigned long ip = inet_addr(host);
    if (ip == INADDR_NONE)
    {
        struct hostent *he = gethostbyname(host);
        if (!he)
        {
            rc_log("net: cannot resolve %s: %d", host, WSAGetLastError());
            m_sock.reset();
            setState(RC_FAILED);
            return false;
        }
        memcpy(&ip, he->h_addr_list[0], 4);
    }

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons((u_short)port);
    addr.sin_addr.s_addr = ip;

    if (::connect(m_sock.get(), (struct sockaddr *)&addr, sizeof(addr)) ==
        SOCKET_ERROR)
    {
        rc_log("net: connect to %s:%d failed: %d", host, port,
               WSAGetLastError());
        m_sock.reset();
        setState(RC_FAILED);
        return false;
    }
    rc_log("net: connected to %s:%d", host, port);
    goLive();
    return true;
}

bool Connection::adopt(SOCKET accepted)
{
    /* Owned from the first line, so every refusal below closes it. */
    SocketGuard s(accepted);
    if (!s.valid())
    {
        rc_log("net: adopt given no socket");
        return false;
    }
    if (!prepare())
        return false;
    m_sock.reset(s.release());
    goLive();
    return true;
}

/* Both threads come from core/thread.h, which carries what used to be
 * repeated here: _beginthreadex rather than CreateThread, a non-NULL
 * thread-id slot, and the std::bad_alloc catch that keeps an out-of-memory
 * from ending the process with no log. */
bool Connection::start()
{
    /* Zero: the image default, 64 KB committed each (measured). A smaller
     * one is core/thread.h's whole point, but these two are the SHIPPING
     * CLIENT's network threads and their depth runs through onFrame into
     * whatever the sink does, which is the app. gremote's own threads choose
     * a size; these keep what they have had all along until a working-set
     * measurement on the guest says what is safe. */
    m_readerBody.attach(this);
    m_senderBody.attach(this);

    /* The sender first: frames queued before start() (a HELLO) wait for it. */
    if (!m_sender.start(m_senderBody, "the network sender", 0))
    {
        /* Without a sender nothing can put a byte on the wire. Reporting
         * "connected" here would leave the app looking healthy forever. */
        rc_log("net: could not start sender thread");
        m_running = 0;
        return false;
    }
    setState(RC_CONNECTED);
    if (!m_reader.start(m_readerBody, "the network reader", 0))
    {
        rc_log("net: could not start reader thread");
        m_running = 0;
        return false;
    }
    return true;
}

/* -------------------------------------------------------------- reader */

int Connection::recvExact(char *buf, unsigned len)
{
    unsigned got = 0;
    while (got < len)
    {
        /* THE HEARTBEAT, when there is one. The peer's silence is measured
         * HERE, on the reader, from the select() in front of every recv --
         * so it never waits on a lane being free, and a long job on the
         * other side of this program can neither starve it nor keep a dead
         * peer alive. A peer that died without a FIN (a killed process, a
         * pulled cable) would otherwise hold the link until TCP keepalive,
         * about two hours. */
        if (m_idleMs)
        {
            fd_set ready;
            FD_ZERO(&ready);
            FD_SET(m_sock.get(), &ready);
            struct timeval tv;
            tv.tv_sec = (long)(m_idleMs / 1000);
            tv.tv_usec = (long)((m_idleMs % 1000) * 1000);
            int n = select(0, &ready, NULL, NULL, &tv);
            if (n == 0)
            {
                rc_log("net: nothing from the peer for %lums, dropping link",
                       (unsigned long)m_idleMs);
                return -1;
            }
            if (n == SOCKET_ERROR)
                return -1;
        }
        int r = recv(m_sock.get(), buf + got, (int)(len - got), 0);
        if (r == 0 || r == SOCKET_ERROR)
            return -1;   /* peer closed, or the link failed */
        got += (unsigned)r;
    }
    return 0;
}

void Connection::readerLoop()
{
    unsigned char hdr[4];

    while (m_running)
    {
        if (recvExact((char *)hdr, 4) != 0)
            break;

        unsigned len = (unsigned)hdr[0] | ((unsigned)hdr[1] << 8) |
                       ((unsigned)hdr[2] << 16) | ((unsigned)hdr[3] << 24);

        /* A corrupted length prefix must not be able to allocate the machine
         * away. Never try to resync a stream whose framing is already lost. */
        if (len == 0 || len > RC_MAX_FRAME)
        {
            rc_log("net: bad frame length %u (cap %u), dropping link", len,
                   RC_MAX_FRAME);
            break;
        }

        char *payload = (char *)HeapAlloc(GetProcessHeap(), 0, len + 1);
        if (!payload)
        {
            rc_log("net: out of memory for %u byte frame", len);
            break;
        }
        if (recvExact(payload, len) != 0)
        {
            HeapFree(GetProcessHeap(), 0, payload);
            break;
        }
        payload[len] = '\0';
        m_rxBytes += len + 4;

        /* Owns just the rc_frame struct, never `payload` -- that passes into
         * the frame via rc_frame_parse's out->_raw and is freed by
         * rc_frame_free, not by this guard. */
        HeapPtr<rc_frame> f(
            (rc_frame *)HeapAlloc(GetProcessHeap(), 0, sizeof(rc_frame)));
        if (!f.valid())
        {
            HeapFree(GetProcessHeap(), 0, payload);
            break;
        }
        if (rc_frame_parse(payload, len, f.get()) != 0)
        {
            rc_log("net: malformed frame, dropping link");
            HeapFree(GetProcessHeap(), 0, payload);
            break;
        }

        const char *enc = rc_frame_get(f.get(), "enc");
        if (enc && strcmp(enc, "deflate") == 0)
        {
            if (!m_codec)
            {
                rc_log("net: compressed frame on a link that does not "
                       "compress, dropping link");
                rc_frame_free(f.get());
                break;
            }
            if (m_codec->inflateBody(f.get()) != 0)
            {
                rc_log("net: cannot inflate frame, dropping link");
                rc_frame_free(f.get());
                break;
            }
        }

        /* The sink owns it from here, on every path (FrameSink::onFrame). */
        m_sink->onFrame(*this, f.release());
    }

    /* THE LINK IS DEAD, AND SAYING SO IS THIS THREAD'S JOB.
     *
     * Leaving `running` at 1 here was the single worst bug in the four-thread
     * design: the reconnect path would start again with the sender and the
     * RPC worker still alive, which re-initialised a HELD critical section,
     * zeroed the outbound queue from under the sender, and left two sender
     * threads calling send() on one socket. Two concurrent sends interleave,
     * the peer's framing has no resync, so it drops the link -- which
     * reconnects, adding a third sender. */
    LONG wasRunning = m_running;
    m_running = 0;
    setState(wasRunning ? RC_FAILED : RC_DISCONNECTED);
    rc_log("net: reader thread exiting (rx=%lu tx=%lu)", m_rxBytes,
           m_txBytes);
}

void Connection::ReaderLoop::run()
{
    m_conn->readerLoop();
}

/* -------------------------------------------------------------- sender */
/* send() never touches the socket. It builds the frame, puts it on a bounded
 * queue and returns; one sender thread owns the wire.
 *
 * The old version held a CRITICAL_SECTION across a blocking send(), which had
 * two failure modes waiting for a second sender to arrive. A 32KB exec result
 * at 40kbit is ~6.5 seconds with the lock held, and the UI thread sends from
 * Send, Stop, a chat switch and the permission bar -- so the message pump
 * stopped. And releasing the lock mid-frame to fix that would interleave two
 * frames on a stream with no resync, which drops the link. */

/* Drop everything queued. The socket is already going. */
void Connection::clearQueue()
{
    OutItem *it;
    {
        ScopedLock lock(&m_lock);
        it = m_head;
        m_head = m_tail = NULL;
        m_queued = 0;
    }
    while (it)
    {
        OutItem *next = it->next;
        HeapFree(GetProcessHeap(), 0, it);
        it = next;
    }
    if (m_room.valid())
        SetEvent(m_room.get());
}

void Connection::senderLoop()
{
    while (m_running)
    {
        OutItem *it = NULL;
        {
            ScopedLock lock(&m_lock);
            if (m_head)
            {
                it = m_head;
                m_head = it->next;
                if (!m_head)
                    m_tail = NULL;
                m_queued -= it->len;
            }
        }

        if (!it)
        {
            /* Timed rather than infinite: `running` going to 0 must be
             * noticed even if nothing is ever queued again. */
            WaitForSingleObject(m_wake.get(), 200);
            continue;
        }
        /* A writer may have been waiting for space. */
        SetEvent(m_room.get());

        while (it->sent < it->len && m_running)
        {
            SOCKET s = m_sock.get();
            if (s == INVALID_SOCKET)
                break;
            int w = ::send(s, it->data + it->sent, (int)(it->len - it->sent),
                           0);
            if (w == SOCKET_ERROR)
            {
                rc_log("net: send failed: %d", WSAGetLastError());
                m_running = 0;
                break;
            }
            it->sent += (unsigned)w;
            m_txBytes += (unsigned long)w;
        }
        HeapFree(GetProcessHeap(), 0, it);
    }
    clearQueue();
}

void Connection::SenderLoop::run()
{
    m_conn->senderLoop();
}

/* ---------------------------------------------------------------- send */

/* Headers a compressed frame may carry besides enc=deflate. The client's
 * compressible frames carry two or three; HELLO's thirty never compress. */
enum { MAX_SEND_HEADERS = 32 };

int Connection::send(const char *type, const char *const *keys,
                     const char *const *vals, int nkv, const void *body,
                     unsigned bodyLen, int waitTicks, bool compressible,
                     unsigned reserveBytes)
{
    int waited = 0;

    if (!m_running || !m_sock.valid())
        return RC_SEND_DEAD;

    OutItem *it;
    {
        /* The lock covers building INTO the shared buffer and copying OUT of
         * it. Neither can block, which is the property that matters: the
         * wire write happens on the sender thread, from the heap block, with
         * this released. */
        ScopedLock lock(&m_lock);

        /* Backpressure, in bytes. Wait for the sender to drain room before
         * compressing and allocating `it`: if the send then times out with
         * RC_SEND_FULL, no frame was compressed into the stream, so the
         * persistent dictionary is never corrupted by a dropped frame. And
         * the lock is not released after deflating, which keeps the frames
         * in the order their bytes went through the stream. */
        /* Clamped, because an unsigned subtraction that wrapped would turn
         * a reserve into a queue several gigabytes deep. A reserve at or
         * above the whole budget leaves a budget of zero, which refuses
         * every bulk frame -- wrong, but visibly and safely wrong. */
        unsigned budget = reserveBytes < RC_OUTQ_MAX_BYTES
                              ? RC_OUTQ_MAX_BYTES - reserveBytes : 0;

        while (m_queued + bodyLen + 256 > budget && m_running &&
               waited < waitTicks)
        {
            LeaveCriticalSection(&m_lock);
            WaitForSingleObject(m_room.get(), 100);
            waited++;
            EnterCriticalSection(&m_lock);
        }
        if (!m_running)
            return RC_SEND_DEAD;
        if (m_queued + bodyLen + 256 > budget)
        {
            rc_log("net: send queue full after %dms wait, dropping %s "
                   "(body=%u bytes, outq_bytes=%u, reserve=%u)",
                   waited * 100, type, bodyLen, m_queued, reserveBytes);
            return RC_SEND_FULL;
        }

        const void *sendBody = body;
        unsigned sendLen = bodyLen;
        const char *defKeys[MAX_SEND_HEADERS + 1];
        const char *defVals[MAX_SEND_HEADERS + 1];
        const char *const *useKeys = keys;
        const char *const *useVals = vals;
        int useNkv = nkv;

        /* Only with room for the enc header. The old fallback compressed
         * first and then, finding the array full, sent the deflated body
         * WITHOUT enc=deflate -- which the peer reads as plain bytes. */
        if (compressible && nkv <= MAX_SEND_HEADERS && m_codec &&
            m_codec->shouldCompress(bodyLen))
        {
            unsigned defLen;
            const char *defBody = m_codec->compressBody(body, bodyLen, &defLen);
            if (defBody)
            {
                sendBody = defBody;
                sendLen = defLen;

                for (int i = 0; i < nkv; ++i)
                {
                    defKeys[i] = keys[i];
                    defVals[i] = vals[i];
                }
                defKeys[nkv] = "enc";
                defVals[nkv] = "deflate";
                useKeys = defKeys;
                useVals = defVals;
                useNkv = nkv + 1;
            }
        }

        /* THE CONNECTION'S OWN build buffer, under its own lock. It was a
         * function static -- 66,560 bytes committed at load, the largest
         * static in the image, and shared by every Connection while each
         * locked only itself, which two live connections would have raced
         * over. Per connection it is heap, made by connect() and only while
         * a link exists. The largest frame actually built is SYNCENT at
         * 63,488 plus headers, so the + 1024 of slack is about 1020 bytes
         * more than is ever used; BACKLOG.md 2 owns trimming it. */
        char *build = m_build.get();
        int total = rc_frame_build(build, BUILD_CAP, type, useKeys,
                                   useVals, useNkv, sendBody, sendLen);
        if (total < 0)
        {
            rc_log("net: frame too large to build (%s, body=%u)", type,
                   sendLen);
            return RC_SEND_DEAD;
        }
        it = (OutItem *)HeapAlloc(GetProcessHeap(), 0,
                                  sizeof(OutItem) + (unsigned)total);
        if (!it)
        {
            rc_log("net: out of memory queueing %s (%d bytes)", type, total);
            return RC_SEND_DEAD;
        }
        memcpy(it->data, build, (unsigned)total);
        it->len = (unsigned)total;
        it->sent = 0;
        it->next = NULL;

        if (m_tail)
            m_tail->next = it;
        else
            m_head = it;
        m_tail = it;
        m_queued += it->len;
    }

    SetEvent(m_wake.get());
    return RC_SEND_OK;
}

unsigned Connection::outqBytes()
{
    ScopedLockIf lock(&m_lock, m_lockUp);
    return m_queued;
}

void Connection::deferDeflate()
{
    m_deferDeflate = true;
}

void Connection::allowDeflate()
{
    ScopedLock held(&m_lock);
    if (m_codec)
        m_codec->beginDeflate();
}

void Connection::takeDeflateSample(unsigned long *bytesIn, unsigned long *us)
{
    ScopedLockIf lock(&m_lock, m_lockUp);
    if (m_codec)
    {
        m_codec->takeSample(bytesIn, us);
        return;
    }
    if (bytesIn)
        *bytesIn = 0;
    if (us)
        *us = 0;
}

void Connection::setLinkBps(unsigned long bps)
{
    ScopedLockIf lock(&m_lock, m_lockUp);
    if (m_codec)
        m_codec->linkBps = bps;
}

/* ---------------------------------------------------------------- stop */

/* Let what is queued reach the wire. The sender pops an item before writing
 * it, so an empty queue means "handed to send()", not "acknowledged by the
 * peer" -- which is as much as this side can know anyway; signalStop's FIN is
 * what actually flushes. Bounded: a link too slow for a handful of small acks
 * in two seconds is a link that is already gone. */
void Connection::drain(DWORD timeoutMs)
{
    if (!m_lockUp)
        return;
    DWORD waited = 0;
    for (;;)
    {
        bool empty;
        {
            ScopedLock lock(&m_lock);
            empty = m_head == NULL;
        }
        if (empty || !m_running)
            return;
        if (waited >= timeoutMs)
            break;
        Sleep(10);
        waited += 10;
    }
    rc_log("net: outbound queue still had work after %lums of draining",
           (unsigned long)timeoutMs);
}

void Connection::signalStop()
{
    m_running = 0;
    /* Wake anything parked on the queue so it can notice `running`. */
    if (m_wake.valid())
        SetEvent(m_wake.get());
    if (m_room.valid())
        SetEvent(m_room.get());

    /* FIN NOW, CLOSE LATER -- and the gap between those is finding 01.52.
     *
     * The socket is read and written by three threads with no lock: the
     * reader in recvExact, the sender in its write loop, and the owner. The
     * named hazard is the sender snapshotting the descriptor, passing the
     * INVALID_SOCKET test, and being preempted right there while the owner
     * closes it. Win9x recycles descriptors aggressively, so the resumed
     * send() does not fail -- it can land on whatever socket was opened in
     * the meantime and write a frame into it.
     *
     * A lock is the obvious fix and the wrong one: the reader holds the
     * socket across a BLOCKING recv, so any mutex covering it would be held
     * for the length of an idle connection and stopping would wait behind
     * it.
     *
     * Ordering removes the race instead of guarding it. The descriptor is not
     * freed until both threads that can touch it have been joined (release),
     * so there is no window in which a live thread holds a stale one. Only
     * the FIN happens here.
     *
     * The FIN itself stays graceful, and that part is not stylistic: an
     * abrupt RST is reported to crash Win98's Winsock stack outright, taking
     * the machine with it. The FIN is also what unblocks the reader -- the
     * peer sees it, closes, and recv returns 0 -- so it has to be sent before
     * the join rather than after it, or the join waits out its whole timeout
     * on an idle link every single time. */
    if (m_sock.valid())
        shutdown(m_sock.get(), 1 /* SD_SEND */);
}

bool Connection::join(DWORD eachMs)
{
    /* Kept as the wait codes the logs below already print, so a join that
     * timed out reads the same as it always has. */
    if (m_reader.started())
        m_readerJoin = m_reader.join(eachMs) ? WAIT_OBJECT_0 : WAIT_TIMEOUT;
    if (m_sender.started())
        m_senderJoin = m_sender.join(eachMs) ? WAIT_OBJECT_0 : WAIT_TIMEOUT;
    return m_readerJoin == WAIT_OBJECT_0 && m_senderJoin == WAIT_OBJECT_0;
}

/* EVERY RELEASE IS GATED ON WHO COULD STILL BE TOUCHING IT, and the gates
 * differ. A thread that did not join is still in here somewhere; freeing
 * what it holds is a crash later and somewhere else, where leaking it costs
 * nothing at process exit. */
bool Connection::release(bool othersGone)
{
    bool rw = m_readerJoin == WAIT_OBJECT_0;
    bool sw = m_senderJoin == WAIT_OBJECT_0;
    bool all = rw && sw && othersGone;

    /* The descriptor: the reader and the sender are the only threads inside
     * recv() or send() on it. One freed under a live send() is a frame
     * written into whatever the OS handed out next. */
    if (m_sock.valid())
    {
        if (rw && sw)
        {
            m_sock.reset();
        }
        else
        {
            rc_log("net: reader=%lu sender=%lu still running; leaking the "
                   "socket rather than closing it under them",
                   (unsigned long)m_readerJoin, (unsigned long)m_senderJoin);
            m_sock.release();
        }
    }

    /* The lock, the queue and both events: every sender holds the lock,
     * including the ones outside this object. The old code waited 2000ms,
     * DISCARDED the result and deleted the section regardless -- so any exec
     * longer than two seconds meant the worker's next EnterCriticalSection
     * ran on a destroyed object, which on 9x is a GPF dialog at exit. And
     * closing an event under a surviving waiter turns its wait into an
     * instant WAIT_FAILED, i.e. a hot spin. */
    if (m_lockUp)
    {
        if (all)
        {
            clearQueue();
            DeleteCriticalSection(&m_lock);
            m_lockUp = false;
            m_wake.reset();
            m_room.reset();
            m_build.reset();
        }
        else
        {
            rc_log("net: reader=%lu sender=%lu others=%d did not exit; not "
                   "deleting the lock",
                   (unsigned long)m_readerJoin, (unsigned long)m_senderJoin,
                   othersGone ? 1 : 0);
            m_wake.release();
            m_room.release();
            m_build.release();
        }
    }

    /* The codec, in halves (docs/mem-audit F3): the inflate stream and its
     * 128KB scratch are touched only by the READER; the deflate half by
     * whoever sends. */
    if (m_codec)
    {
        if (rw)
            m_codec->finishInflate();
        else
            rc_log("net: reader did not exit; leaking the inflate scratch "
                   "rather than freeing it under a live mz_inflate");
        if (othersGone && sw)
            m_codec->finishDeflate();
        else
            rc_log("net: a sender did not exit; leaking the deflate scratch "
                   "rather than freeing it under a live mz_deflate");
        if (all)
        {
            delete m_codec;
            m_codec = NULL;
        }
    }

    /* Same argument one step wider. WSACleanup tears Winsock down for the
     * whole process, and a surviving reader or sender is still inside
     * recv()/send() on the descriptor just leaked for that very reason. */
    if (m_wsaUp)
    {
        if (rw && sw)
        {
            WSACleanup();
            m_wsaUp = false;
        }
        else
        {
            rc_log("net: reader=%lu sender=%lu still running; skipping "
                   "WSACleanup",
                   (unsigned long)m_readerJoin, (unsigned long)m_senderJoin);
        }
    }
    return all;
}
