#include <windows.h>
#include <winsock.h>
#include <process.h>
#include <stdio.h>
#include <string.h>
#include "net.h"
#include "log.h"
#include "rpc.h"
#include "miniz.h"

/* Cap on frames posted to the UI thread but not yet freed.
 *
 * PostMessage CAN fail -- Win9x message queues are bounded and smaller than
 * NT's -- and a modal dialog running its own message loop is exactly when the
 * reader keeps recv-ing while the UI drains slowly. Without a cap the reader
 * outruns the UI and every failed post leaks. */
#define RC_MAX_INFLIGHT 256

/* One persistent inflate stream for the whole connection.
 *
 * The bridge compresses with a persistent deflate stream and Z_SYNC_FLUSH per
 * frame, which keeps the dictionary warm across frames -- worth 1.27x over
 * compressing each frame alone. That only works if this side keeps its state
 * too, so this is deliberately NOT per-frame. Both sides reset on reconnect,
 * in lockstep. */
static mz_stream g_inf;
static int       g_inf_ready = 0;

static void inflate_begin(void)
{
    if (g_inf_ready) { mz_inflateEnd(&g_inf); g_inf_ready = 0; }
    memset(&g_inf, 0, sizeof(g_inf));
    if (mz_inflateInit(&g_inf) == MZ_OK) g_inf_ready = 1;
    else rc_log("net: mz_inflateInit failed");
}

static void inflate_finish(void)
{
    if (g_inf_ready) { mz_inflateEnd(&g_inf); g_inf_ready = 0; }
}

/* Inflates f->body in place-ish: allocates a new buffer, points body at it,
 * and records it for rc_frame_free. Returns 0 on success. */
static int inflate_body(rc_frame *f)
{
    /* Bounded so a malicious or corrupt frame cannot expand without limit.
     * 16x the wire cap is far more than any real page needs. */
    unsigned cap = RC_MAX_FRAME * 4;
    char *out;
    int rc;

    if (!g_inf_ready) { rc_log("net: deflate frame but no inflater"); return -1; }
    if (f->body_len == 0) return 0;

    out = (char *)HeapAlloc(GetProcessHeap(), 0, cap + 1);
    if (!out) return -1;

    g_inf.next_in   = (const unsigned char *)f->body;
    g_inf.avail_in  = f->body_len;
    g_inf.next_out  = (unsigned char *)out;
    g_inf.avail_out = cap;

    rc = mz_inflate(&g_inf, MZ_SYNC_FLUSH);
    if (rc != MZ_OK && rc != MZ_STREAM_END) {
        rc_log("net: mz_inflate failed: %d", rc);
        HeapFree(GetProcessHeap(), 0, out);
        return -1;
    }
    if (g_inf.avail_in != 0) {
        rc_log("net: inflate left %u bytes unconsumed (output cap hit)",
               g_inf.avail_in);
        HeapFree(GetProcessHeap(), 0, out);
        return -1;
    }

    f->_body_alloc = out;
    f->body_len = cap - g_inf.avail_out;
    out[f->body_len] = '\0';
    f->body = out;
    return 0;
}

const char *rc_state_name(rc_state s)
{
    switch (s) {
    case RC_DISCONNECTED: return "disconnected";
    case RC_CONNECTING:   return "connecting";
    case RC_CONNECTED:    return "connected";
    default:              return "failed";
    }
}

static void set_state(rc_net *n, rc_state s)
{
    n->state = s;
    if (n->notify) PostMessage(n->notify, WM_RC_STATE, (WPARAM)s, 0);
}

static unsigned __stdcall sender_thread(void *arg);

static int recv_exact(SOCKET s, char *buf, unsigned len, volatile LONG *running)
{
    unsigned got = 0;
    while (got < len) {
        int r = recv(s, buf + got, (int)(len - got), 0);
        if (r == 0) return -1;                    /* peer closed */
        if (r == SOCKET_ERROR) {
            if (!*running) return -1;
            return -1;
        }
        got += (unsigned)r;
    }
    return 0;
}

static unsigned __stdcall reader_thread(void *arg)
{
    rc_net *n = (rc_net *)arg;
    unsigned char hdr[4];

    while (n->running) {
        unsigned len;
        char *payload;
        rc_frame *f;

        if (recv_exact(n->sock, (char *)hdr, 4, &n->running) != 0) break;

        len = (unsigned)hdr[0] | ((unsigned)hdr[1] << 8)
            | ((unsigned)hdr[2] << 16) | ((unsigned)hdr[3] << 24);

        /* A corrupted length prefix must not be able to allocate the machine
         * away. Never try to resync a stream whose framing is already lost. */
        if (len == 0 || len > RC_MAX_FRAME) {
            rc_log("net: bad frame length %u (cap %u), dropping link",
                   len, RC_MAX_FRAME);
            break;
        }

        payload = (char *)HeapAlloc(GetProcessHeap(), 0, len + 1);
        if (!payload) { rc_log("net: out of memory for %u byte frame", len); break; }

        if (recv_exact(n->sock, payload, len, &n->running) != 0) {
            HeapFree(GetProcessHeap(), 0, payload);
            break;
        }
        payload[len] = '\0';
        n->rx_bytes += len + 4;

        f = (rc_frame *)HeapAlloc(GetProcessHeap(), 0, sizeof(rc_frame));
        if (!f) { HeapFree(GetProcessHeap(), 0, payload); break; }

        if (rc_frame_parse(payload, len, f) != 0) {
            rc_log("net: malformed frame, dropping link");
            HeapFree(GetProcessHeap(), 0, payload);
            HeapFree(GetProcessHeap(), 0, f);
            break;
        }

        {
            const char *enc = rc_frame_get(f, "enc");
            if (enc && strcmp(enc, "deflate") == 0 && inflate_body(f) != 0) {
                rc_log("net: cannot inflate frame, dropping link");
                rc_frame_free(f);
                HeapFree(GetProcessHeap(), 0, f);
                break;
            }
        }

        /* EXEC goes straight to the RPC worker and never reaches the UI
         * thread. The type is already parsed here, so routing it now saves a
         * hop and a copy -- and, more importantly, `inflight` is the wrong
         * instrument for it: that counter measures UI QUEUE DEPTH for the
         * backpressure gate below, and holding it for the sixty seconds a
         * command might run would conflate two unrelated queues and stall
         * display frames behind a compile. The worker has its own counter. */
        if (strcmp(f->type, "EXEC") == 0) {
            InterlockedIncrement(&n->rpcq);
            if (rc_rpc_submit(f) != 0) {
                InterlockedDecrement(&n->rpcq);
                rc_frame_free(f);
                HeapFree(GetProcessHeap(), 0, f);
            }
            continue;
        }

        /* Backpressure: if the UI is not keeping up, wait for it rather than
         * queueing without bound. Yielding here is correct -- the socket's
         * receive window does the rest. */
        while (n->inflight >= RC_MAX_INFLIGHT && n->running)
            Sleep(10);

        InterlockedIncrement(&n->inflight);
        if (!PostMessage(n->notify, WM_RC_FRAME, 0, (LPARAM)f)) {
            /* The post failed, so the UI thread will never see this frame and
             * will never free it. Freeing it here is the whole reason the
             * inflight counter exists. */
            InterlockedDecrement(&n->inflight);
            rc_log("net: PostMessage(hwnd=%p, WM_RC_FRAME) failed: err=%lu "
                   "(inflight=%ld)", (void *)n->notify, GetLastError(),
                   n->inflight);
            rc_frame_free(f);
            HeapFree(GetProcessHeap(), 0, f);
        }
    }

    /* THE LINK IS DEAD, AND SAYING SO IS THIS THREAD'S JOB.
     *
     * Leaving `running` at 1 here was the single worst bug in the four-thread
     * design: try_connect would call rc_net_start again with the sender and
     * the RPC worker still alive, which re-initialised a HELD critical
     * section, zeroed the outbound queue from under the sender, and left two
     * sender threads calling send() on one socket. Two concurrent sends
     * interleave, the bridge's framing has no resync, so it drops the link --
     * which reconnects, adding a third sender. */
    {
        int was_running = n->running;
        n->running = 0;
        set_state(n, was_running ? RC_FAILED : RC_DISCONNECTED);
    }
    rc_log("net: reader thread exiting (rx=%lu tx=%lu)", n->rx_bytes, n->tx_bytes);
    return 0;
}

static int do_connect(rc_net *n)
{
    struct sockaddr_in addr;
    unsigned long ip;
    struct hostent *he;

    n->sock = socket(AF_INET, SOCK_STREAM, 0);
    if (n->sock == INVALID_SOCKET) {
        rc_log("net: socket() failed: %d", WSAGetLastError());
        return -1;
    }

    ip = inet_addr(n->host);
    if (ip == INADDR_NONE) {
        he = gethostbyname(n->host);
        if (!he) {
            rc_log("net: cannot resolve %s: %d", n->host, WSAGetLastError());
            closesocket(n->sock); n->sock = INVALID_SOCKET;
            return -1;
        }
        memcpy(&ip, he->h_addr_list[0], 4);
    }

    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons((u_short)n->port);
    addr.sin_addr.s_addr = ip;

    if (connect(n->sock, (struct sockaddr *)&addr, sizeof(addr)) == SOCKET_ERROR) {
        rc_log("net: connect to %s:%d failed: %d", n->host, n->port,
               WSAGetLastError());
        closesocket(n->sock); n->sock = INVALID_SOCKET;
        return -1;
    }
    rc_log("net: connected to %s:%d", n->host, n->port);
    return 0;
}

int rc_net_start(rc_net *n, HWND notify, const char *host, int port,
                 const char *secret)
{
    WSADATA wsa;
    char maxframe[16], ver[16], memkb[16], memfree[16], disk[24];
    char osver[96], cpu[64], screen[32];
    /* Sized with room to spare: ver, secret, max_frame, mem_kb and sess is
     * already five, and this array overflowed the moment mem_kb was added. */
    const char *keys[16]; const char *vals[16];
    int nkv = 0;
    unsigned tid;

    n->notify = notify;
    _snprintf(n->host, sizeof(n->host) - 1, "%s", host);
    n->port = port;
    _snprintf(n->secret, sizeof(n->secret) - 1, "%s", secret);
    n->sock = INVALID_SOCKET;
    n->inflight = 0;

    InitializeCriticalSection(&n->send_lock);
    n->outq_head = n->outq_tail = NULL;
    n->outq_bytes = 0;
    n->rpcq = 0;
    /* Auto-reset: the sender takes one item per wake and loops until empty. */
    n->outq_wake = CreateEventA(NULL, FALSE, FALSE, NULL);
    /* Manual-reset would wake every blocked writer at once; auto-reset wakes
     * one, which is what "a slot freed up" means. */
    n->outq_room = CreateEventA(NULL, FALSE, FALSE, NULL);
    /* Mark it needing teardown HERE, not on success.
     *
     * The critical section and both events now exist, and every `return -1`
     * below leaves them behind. Setting this only on success meant a failed
     * attempt -- a bridge that is simply down, which is the common case --
     * left rc_net_stop thinking there was nothing to clean up, so the next
     * try_connect re-initialised a LIVE critical section and overwrote both
     * event handles. That is a fault inside KERNEL32, and it is what three
     * bridge restarts reproduced. */
    n->started = 1;
    set_state(n, RC_CONNECTING);

    if (WSAStartup(MAKEWORD(1, 1), &wsa) != 0) {
        rc_log("net: WSAStartup failed");
        set_state(n, RC_FAILED);
        return -1;
    }

    if (do_connect(n) != 0) { set_state(n, RC_FAILED); return -1; }

    inflate_begin();
    n->running = 1;

    _snprintf(ver, sizeof(ver), "%d", RC_PROTO_VERSION);
    _snprintf(maxframe, sizeof(maxframe), "%d", RC_MAX_FRAME);
    keys[nkv] = "ver";       vals[nkv++] = ver;
    keys[nkv] = "secret";    vals[nkv++] = n->secret;
    keys[nkv] = "max_frame"; vals[nkv++] = maxframe;
    /* What this machine actually is.
     *
     * Two consumers. The bridge sizes pages from mem_kb -- a 128MB box can
     * hold a far bigger page than a 32MB one, and there is no other way for it
     * to know which it is talking to. And the rest goes into the agent's
     * system prompt, because a model that thinks it is on a modern machine
     * cannot make good decisions about which tool to reach for: "just install
     * this 400MB toolchain" is a wrong answer here however right it is
     * elsewhere. */
    {
        MEMORYSTATUS ms;
        OSVERSIONINFOA os;
        SYSTEM_INFO si;
        DWORD spc = 0, bps = 0, freec = 0, totalc = 0;
        HDC dc;

        memset(&ms, 0, sizeof(ms));
        ms.dwLength = sizeof(ms);
        GlobalMemoryStatus(&ms);
        _snprintf(memkb, sizeof(memkb), "%lu",
                  (unsigned long)(ms.dwTotalPhys / 1024));
        keys[nkv] = "mem_kb"; vals[nkv++] = memkb;
        _snprintf(memfree, sizeof(memfree), "%lu",
                  (unsigned long)(ms.dwAvailPhys / 1024));
        keys[nkv] = "mem_free_kb"; vals[nkv++] = memfree;

        memset(&os, 0, sizeof(os));
        os.dwOSVersionInfoSize = sizeof(os);
        if (GetVersionExA(&os)) {
            _snprintf(osver, sizeof(osver) - 1, "Windows %lu.%lu build %lu %s",
                      (unsigned long)os.dwMajorVersion,
                      (unsigned long)os.dwMinorVersion,
                      (unsigned long)(os.dwBuildNumber & 0xFFFF),
                      os.szCSDVersion);
            osver[sizeof(osver) - 1] = '\0';
            keys[nkv] = "os"; vals[nkv++] = osver;
        }

        memset(&si, 0, sizeof(si));
        GetSystemInfo(&si);
        _snprintf(cpu, sizeof(cpu) - 1, "x86 family %u model %u, %lu cpu",
                  (unsigned)si.wProcessorLevel,
                  (unsigned)(si.wProcessorRevision >> 8),
                  (unsigned long)si.dwNumberOfProcessors);
        cpu[sizeof(cpu) - 1] = '\0';
        keys[nkv] = "cpu"; vals[nkv++] = cpu;

        dc = GetDC(NULL);
        if (dc) {
            _snprintf(screen, sizeof(screen) - 1, "%dx%dx%d",
                      GetDeviceCaps(dc, HORZRES), GetDeviceCaps(dc, VERTRES),
                      GetDeviceCaps(dc, BITSPIXEL));
            screen[sizeof(screen) - 1] = '\0';
            keys[nkv] = "screen"; vals[nkv++] = screen;
            ReleaseDC(NULL, dc);
        }

        /* GetDiskFreeSpaceEx is NT4+/OSR2 and a static import of it would kill
         * the image at load on an older 9x. The old call is fine here: the
         * numbers only have to be roughly right. */
        if (GetDiskFreeSpaceA("C:\\", &spc, &bps, &freec, &totalc)) {
            _snprintf(disk, sizeof(disk) - 1, "%lu",
                      (unsigned long)((double)spc * bps * freec / 1024.0));
            disk[sizeof(disk) - 1] = '\0';
            keys[nkv] = "disk_free_kb"; vals[nkv++] = disk;
        }
    }
    if (n->sess[0]) { keys[nkv] = "sess"; vals[nkv++] = n->sess; }

    if (rc_net_send(n, "HELLO", keys, vals, nkv, NULL, 0) != 0) {
        rc_log("net: HELLO failed");
        set_state(n, RC_FAILED);
        return -1;
    }

    /* _beginthreadex, not CreateThread. Open Watcom's multi-threaded CRT
     * allocates its per-thread block here; a raw CreateThread thread never
     * gets one and never releases it. It also takes a non-NULL thread-id
     * slot, which Win95/98 requires -- CreateThread(..., NULL) fails there
     * with ERROR_INVALID_PARAMETER while NT accepts it. */
    /* Order matters. The worker must exist before the reader can route an
     * EXEC to it, or an early frame is dropped silently and the bridge waits
     * out its whole budget for a command nobody ran. */
    rc_rpc_start(n);
    n->sender = (HANDLE)_beginthreadex(NULL, 0, sender_thread, n, 0, &tid);
    if (!n->sender) {
        /* Without a sender nothing can put a byte on the wire -- including the
         * HELLO already queued. Reporting "connected" here would leave the app
         * looking healthy forever. */
        rc_log("net: could not start sender thread");
        rc_rpc_stop();
        rc_rpc_join(2000);
        closesocket(n->sock);
        n->sock = INVALID_SOCKET;
        n->running = 0;
        set_state(n, RC_FAILED);
        return -1;
    }
    n->thread = (HANDLE)_beginthreadex(NULL, 0, reader_thread, n, 0, &tid);
    if (!n->thread) {
        rc_log("net: _beginthreadex failed");
        set_state(n, RC_FAILED);
        return -1;
    }

    set_state(n, RC_CONNECTED);
    return 0;
}

/* ------------------------------------------------------------- sending */
/* rc_net_send no longer touches the socket. It builds the frame, puts it on a
 * bounded queue and returns; one sender thread owns the wire.
 *
 * The old version held a CRITICAL_SECTION across a blocking send(), which had
 * two failure modes waiting for a second sender to arrive. A 32KB exec result
 * at 40kbit is ~6.5 seconds with the lock held, and the UI thread calls this
 * from do_send, do_stop, chats_switch and the permission bar -- so the message
 * pump stopped. And releasing the lock mid-frame to fix that would interleave
 * two frames on a stream with no resync, which drops the link. */

static void outq_free(rc_outq_item *it)
{
    HeapFree(GetProcessHeap(), 0, it);
}

/* Drop everything queued. Called on shutdown; the socket is already going. */
static void outq_clear(rc_net *n)
{
    rc_outq_item *it;
    EnterCriticalSection(&n->send_lock);
    it = n->outq_head;
    n->outq_head = n->outq_tail = NULL;
    n->outq_bytes = 0;
    LeaveCriticalSection(&n->send_lock);
    while (it) {
        rc_outq_item *next = it->next;
        outq_free(it);
        it = next;
    }
    if (n->outq_room) SetEvent(n->outq_room);
}

static unsigned __stdcall sender_thread(void *arg)
{
    rc_net *n = (rc_net *)arg;

    while (n->running) {
        rc_outq_item *it = NULL;

        EnterCriticalSection(&n->send_lock);
        if (n->outq_head) {
            it = n->outq_head;
            n->outq_head = it->next;
            if (!n->outq_head) n->outq_tail = NULL;
            n->outq_bytes -= it->len;
        }
        LeaveCriticalSection(&n->send_lock);

        if (!it) {
            /* Timed wait rather than infinite: `running` going to 0 must be
             * noticed even if nothing is ever queued again. */
            WaitForSingleObject(n->outq_wake, 200);
            continue;
        }
        /* A writer may have been waiting for space. */
        SetEvent(n->outq_room);

        while (it->sent < it->len && n->running) {
            int w;
            SOCKET s = n->sock;
            if (s == INVALID_SOCKET) break;
            w = send(s, it->data + it->sent, (int)(it->len - it->sent), 0);
            if (w == SOCKET_ERROR) {
                rc_log("net: send failed: %d", WSAGetLastError());
                n->running = 0;
                break;
            }
            it->sent += (unsigned)w;
            n->tx_bytes += (unsigned long)w;
        }
        outq_free(it);
    }
    outq_clear(n);
    return 0;
}

int rc_net_send(rc_net *n, const char *type,
                const char *const *keys, const char *const *vals, int nkv,
                const void *body, unsigned body_len)
{
    /* Built on the caller's stack region via a heap block sized to the frame,
     * so there is no shared build buffer to race over any more. */
    static char build[RC_MAX_FRAME + 1024];
    rc_outq_item *it;
    int total;
    int waited = 0;

    if (n->sock == INVALID_SOCKET || !n->running) return -1;

    /* The build buffer is still shared, but only for the memcpy that follows,
     * which cannot block. That is the whole difference. */
    EnterCriticalSection(&n->send_lock);
    total = rc_frame_build(build, sizeof(build), type, keys, vals, nkv,
                           body, body_len);
    if (total < 0) {
        LeaveCriticalSection(&n->send_lock);
        rc_log("net: frame too large to build (%s, body=%u)", type, body_len);
        return -1;
    }
    it = (rc_outq_item *)HeapAlloc(GetProcessHeap(), 0,
                                   sizeof(rc_outq_item) + (unsigned)total);
    if (!it) {
        LeaveCriticalSection(&n->send_lock);
        rc_log("net: out of memory queueing %s (%d bytes)", type, total);
        return -1;
    }
    memcpy(it->data, build, (unsigned)total);
    it->len = (unsigned)total;
    it->sent = 0;
    it->next = NULL;

    /* Backpressure, in bytes. Wait for the sender to drain rather than letting
     * the queue grow without bound on a machine with 28KB free. */
    while (n->outq_bytes + it->len > RC_OUTQ_MAX_BYTES && n->running
           && waited < 50) {
        LeaveCriticalSection(&n->send_lock);
        WaitForSingleObject(n->outq_room, 100);
        waited++;
        EnterCriticalSection(&n->send_lock);
    }
    if (!n->running) {
        LeaveCriticalSection(&n->send_lock);
        outq_free(it);
        return -1;
    }
    if (n->outq_tail) n->outq_tail->next = it;
    else              n->outq_head = it;
    n->outq_tail = it;
    n->outq_bytes += it->len;
    LeaveCriticalSection(&n->send_lock);

    SetEvent(n->outq_wake);
    return 0;
}

void rc_net_stop(rc_net *n)
{
    DWORD rw = WAIT_OBJECT_0, sw = WAIT_OBJECT_0;
    int   rpcj;

    /* Safe to call on a connection that never started, so the reconnect path
     * can call it unconditionally rather than tracking state itself. */
    if (!n->started) return;
    n->started = 0;
    n->running = 0;
    /* Wake anything parked on the queue so it can notice `running`. */
    if (n->outq_wake) SetEvent(n->outq_wake);
    if (n->outq_room) SetEvent(n->outq_room);
    rc_rpc_stop();              /* signals the worker and kills any child */

    if (n->sock != INVALID_SOCKET) {
        /* Graceful FIN before close. An abrupt RST is reported to crash
         * Win98's Winsock stack outright, taking the machine with it -- this
         * is client-side discipline because the server cannot defend itself. */
        shutdown(n->sock, 1 /* SD_SEND */);
        Sleep(50);
        closesocket(n->sock);
        n->sock = INVALID_SOCKET;
    }

    /* Join every thread that can touch send_lock BEFORE deleting it. The old
     * code waited 2000ms, DISCARDED the result and deleted the section
     * regardless -- so any exec longer than two seconds meant the worker's
     * next EnterCriticalSection ran on a destroyed object, which on 9x is a
     * GPF dialog at exit, every time. */
    if (n->thread) {
        rw = WaitForSingleObject(n->thread, 5000);
        CloseHandle(n->thread);
        n->thread = NULL;
    }
    if (n->sender) {
        sw = WaitForSingleObject(n->sender, 5000);
        CloseHandle(n->sender);
        n->sender = NULL;
    }
    rpcj = rc_rpc_join(30000);
    if (!rpcj)
        rc_log("net: RPC worker did not exit");

    /* The worker calls rc_net_send, so it holds send_lock as readily as the
     * other two. An earlier version computed this join and then left it out of
     * the condition, which destroyed the section under a live worker -- the
     * exact GPF-at-exit the rewrite existed to remove. */
    if (rw == WAIT_OBJECT_0 && sw == WAIT_OBJECT_0 && rpcj) {
        outq_clear(n);
        DeleteCriticalSection(&n->send_lock);
    } else {
        /* A thread is still in there somewhere. Leaking a critical section at
         * process exit costs nothing; destroying one out from under a live
         * thread costs the user a crash dialog. */
        rc_log("net: reader=%lu sender=%lu did not exit; not deleting the lock",
               (unsigned long)rw, (unsigned long)sw);
    }

    if (rw == WAIT_OBJECT_0 && sw == WAIT_OBJECT_0 && rpcj) {
        /* Only once nothing can still be waiting on them. Closing these under
         * a surviving thread turns its WaitForSingleObject into an instant
         * WAIT_FAILED, i.e. a hot spin. */
        if (n->outq_wake) { CloseHandle(n->outq_wake); n->outq_wake = NULL; }
        if (n->outq_room) { CloseHandle(n->outq_room); n->outq_room = NULL; }
    }
    inflate_finish();
    WSACleanup();
}
