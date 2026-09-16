#include <windows.h>
#include <winsock.h>
#include <process.h>
#include <stdio.h>
#include <string.h>
#include "net.h"
#include "log.h"

/* Cap on frames posted to the UI thread but not yet freed.
 *
 * PostMessage CAN fail -- Win9x message queues are bounded and smaller than
 * NT's -- and a modal dialog running its own message loop is exactly when the
 * reader keeps recv-ing while the UI drains slowly. Without a cap the reader
 * outruns the UI and every failed post leaks. */
#define RC_MAX_INFLIGHT 256

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
            rc_log("net: PostMessage failed, freeing frame (inflight=%ld)",
                   n->inflight);
            rc_frame_free(f);
            HeapFree(GetProcessHeap(), 0, f);
        }
    }

    set_state(n, n->running ? RC_FAILED : RC_DISCONNECTED);
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
    char maxframe[16], ver[16];
    const char *keys[4]; const char *vals[4];
    int nkv = 0;
    unsigned tid;

    n->notify = notify;
    _snprintf(n->host, sizeof(n->host) - 1, "%s", host);
    n->port = port;
    _snprintf(n->secret, sizeof(n->secret) - 1, "%s", secret);
    n->sock = INVALID_SOCKET;
    n->inflight = 0;

    InitializeCriticalSection(&n->send_lock);
    set_state(n, RC_CONNECTING);

    if (WSAStartup(MAKEWORD(1, 1), &wsa) != 0) {
        rc_log("net: WSAStartup failed");
        set_state(n, RC_FAILED);
        return -1;
    }

    if (do_connect(n) != 0) { set_state(n, RC_FAILED); return -1; }

    n->running = 1;

    _snprintf(ver, sizeof(ver), "%d", RC_PROTO_VERSION);
    _snprintf(maxframe, sizeof(maxframe), "%d", RC_MAX_FRAME);
    keys[nkv] = "ver";       vals[nkv++] = ver;
    keys[nkv] = "secret";    vals[nkv++] = n->secret;
    keys[nkv] = "max_frame"; vals[nkv++] = maxframe;
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
    n->thread = (HANDLE)_beginthreadex(NULL, 0, reader_thread, n, 0, &tid);
    if (!n->thread) {
        rc_log("net: _beginthreadex failed");
        set_state(n, RC_FAILED);
        return -1;
    }

    set_state(n, RC_CONNECTED);
    return 0;
}

int rc_net_send(rc_net *n, const char *type,
                const char *const *keys, const char *const *vals, int nkv,
                const void *body, unsigned body_len)
{
    static char buf[RC_MAX_FRAME + 1024];
    int total, sent = 0;

    if (n->sock == INVALID_SOCKET) return -1;

    EnterCriticalSection(&n->send_lock);
    total = rc_frame_build(buf, sizeof(buf), type, keys, vals, nkv,
                           body, body_len);
    if (total < 0) {
        LeaveCriticalSection(&n->send_lock);
        rc_log("net: frame too large to build (%s, body=%u)", type, body_len);
        return -1;
    }
    while (sent < total) {
        int w = send(n->sock, buf + sent, total - sent, 0);
        if (w == SOCKET_ERROR) {
            LeaveCriticalSection(&n->send_lock);
            rc_log("net: send failed: %d", WSAGetLastError());
            return -1;
        }
        sent += w;
    }
    n->tx_bytes += (unsigned long)total;
    LeaveCriticalSection(&n->send_lock);
    return 0;
}

void rc_net_stop(rc_net *n)
{
    n->running = 0;
    if (n->sock != INVALID_SOCKET) {
        /* Graceful FIN before close. An abrupt RST is reported to crash
         * Win98's Winsock stack outright, taking the machine with it -- this
         * is client-side discipline because the server cannot defend itself. */
        shutdown(n->sock, 1 /* SD_SEND */);
        Sleep(50);
        closesocket(n->sock);
        n->sock = INVALID_SOCKET;
    }
    if (n->thread) {
        WaitForSingleObject(n->thread, 2000);
        CloseHandle(n->thread);
        n->thread = NULL;
    }
    DeleteCriticalSection(&n->send_lock);
    WSACleanup();
}
