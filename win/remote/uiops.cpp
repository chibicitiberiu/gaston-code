#include "remote/uiops.h"

#include "ui/uiengine.h"
#include "ui/uilink.h"
#include "ui/uicontext.h"
#include "automation/shot.h"
#include "net/caps.h"
#include "wire/link.h"
#include "core/log.h"
#include "core/rc_cpp.h"
#include "core/str.h"

#include <string.h>

/* How many idle passes of the lane's loop before the event feed is stopped.
 * LANE_IDLE_SLICE_MS is 100, so this is about three seconds -- long enough
 * that a caller doing one request after another does not pay to restart it,
 * short enough that a machine nobody is driving is not woken by every window
 * event on it. */
enum { QUIET_TICKS_BEFORE_UNHOOK = 30 };

/* ---- the wire half of the seam --------------------------------------- */

/* The engine's calls as gremote's frames.
 *
 * Where the client hangs the step report on its single UIEND, this sends it
 * as DATA and then a terminator, because `Responder` has no body on `ok`,
 * `err` or `end` -- and that is not an oversight. A non-bulk frame passes no
 * out-queue reserve and RC_SEND_WAIT_UI: those frames are what the reserve
 * exists to protect, and a 32KB report down that path would eat the slot the
 * heartbeat depends on. `data()` is bulk and reserved, so that is where
 * anything large goes.
 */
class RespUiLink : public UiLink
{
public:
    RespUiLink(Responder &r, const UiConfig &cfg, volatile LONG *stop)
        : m_r(&r), m_cfg(&cfg), m_stop(stop)
    {
        m_limits.maxNodes = (int)cfg.maxTreeNodes;
        m_limits.maxNodesTop = (int)cfg.maxTreeNodes;
        m_limits.callMs = (DWORD)cfg.callTimeoutMs;
        m_limits.callMsTop = (DWORD)cfg.callTimeoutMs;
        m_limits.scriptMs = (DWORD)cfg.scriptMs;
        m_limits.scriptMsTop = (DWORD)cfg.scriptMs;
        m_limits.stepMs = (DWORD)cfg.stepMs;
        m_limits.stepMsTop = (DWORD)cfg.stepMs;
    }

    virtual bool end(const char *status, const char *const *keys,
                     const char *const *vals, int nkv)
    {
        /* `ok` is an OK and anything else is an ERR, so a caller branches on
         * the frame type the way it does for every other op here rather than
         * reading a status header this wire does not otherwise have. */
        if (strcmp(status, "ok") == 0)
            return m_r->ok(keys, vals, nkv);
        return m_r->errWith(status, keys, vals, nkv);
    }

    virtual bool records(const void *body, unsigned len, int n)
    {
        const char *k[1];
        const char *v[1];
        char nbuf[16];
        rc_strf(nbuf, sizeof(nbuf), "%d", n);
        k[0] = "n"; v[0] = nbuf;
        return m_r->data(body, len, k, v, 1);
    }

    virtual bool bytes(const void *body, unsigned len)
    {
        return m_r->data(body, len);
    }

    virtual bool report(const void *body, unsigned len)
    {
        const char *k[1];
        const char *v[1];
        k[0] = "steps"; v[0] = "1";
        return m_r->data(body, len, k, v, 1);
    }

    /* NOT the client's RC_MAX_FRAME - 2048, and a live screenshot on the
     * guest is what said so.
     *
     * Every bulk frame here reserves RC_OUTQ_RESERVE for the control lane,
     * so a body has to fit the out queue ALONGSIDE that reserve:
     * 63,488 + 8,192 is 71,680 against a 65,536-byte queue, which no amount
     * of draining can ever make room for. The guest logged exactly that --
     * "send queue full after 5000ms wait, dropping DATA (body=63488,
     * outq_bytes=0, reserve=8192)" -- and SHOT answered `failed` with an
     * empty queue, which is as confusing as it sounds.
     *
     * The client does not meet this because its sends pass no reserve at
     * all; the reserve is a server's problem, and gremote is the server. */
    virtual unsigned bodyCap() const
    {
        return RC_OUTQ_MAX_BYTES - RC_OUTQ_RESERVE - 2048;
    }

    /* LIVE, every request: the tray menu changes this field and the person
     * at the keyboard should not have to reconnect for "off" to mean off. */
    virtual int automationLevel() const { return (int)m_cfg->automation; }

    virtual const UiLimits &limits() const { return m_limits; }
    virtual volatile LONG *stopFlag() { return m_stop; }

private:
    RespUiLink(const RespUiLink &);
    RespUiLink &operator=(const RespUiLink &);

    Responder      *m_r;
    const UiConfig *m_cfg;
    volatile LONG  *m_stop;
    UiLimits        m_limits;
};

/* ---- SHOT ------------------------------------------------------------ */

/* Straight into frames. The image is never held whole: a 640x480 at 8bpp is
 * 300KB and this machine may have 32MB, so the capture is strip-wise and
 * each strip goes out as it is produced. */
class ShotFrames : public ByteSink
{
public:
    ShotFrames(UiLink &link)
        : m_link(link), m_cap(link.bodyCap()),
          m_buf((char *)HeapAlloc(GetProcessHeap(), 0, link.bodyCap())),
          m_len(0), m_sent(0), m_ok(true), m_stopped(false)
    {
    }
    ~ShotFrames() { if (m_buf) HeapFree(GetProcessHeap(), 0, m_buf); }

    bool ready() const { return m_buf != 0; }
    bool ok() const { return m_ok; }
    bool stopped() const { return m_stopped; }
    unsigned sent() const { return m_sent; }

    virtual bool write(const void *data, unsigned len)
    {
        const char *p = (const char *)data;
        volatile LONG *stop = m_link.stopFlag();
        if (stop && *stop)
        {
            m_stopped = true;
            return false;
        }
        while (len)
        {
            unsigned take = m_cap - m_len;
            if (take > len)
                take = len;
            memcpy(m_buf + m_len, p, take);
            m_len += take;
            p += take;
            len -= take;
            if (m_len == m_cap && !flush())
                return false;
        }
        return true;
    }

    bool flush()
    {
        if (!m_len)
            return true;
        if (!m_link.bytes(m_buf, m_len))
        {
            m_ok = false;
            return false;
        }
        m_sent += m_len;
        m_len = 0;
        return true;
    }

private:
    ShotFrames(const ShotFrames &);
    ShotFrames &operator=(const ShotFrames &);

    UiLink  &m_link;
    unsigned m_cap;
    char    *m_buf;
    unsigned m_len;
    unsigned m_sent;
    bool     m_ok;
    bool     m_stopped;
};

void UiOps::shot(Responder &r, rc_frame *f)
{
    RespUiLink link(r, *m_cfg, &m_stopFlag);
    const char *k[4];
    const char *v[4];
    char w[16], h[16], bpp[16], bytes[16];
    ShotOptions o;
    int nkv = 0;

    if (link.automationLevel() == RC_AUTOMATION_NONE)
    {
        r.err("automation_off");
        return;
    }

    o.maxBpp = (int)rc_ui_header_num(f, "bpp", 8, 4, 32);
    o.rect.left   = (LONG)rc_ui_header_num(f, "x", 0, 0, 0x4000);
    o.rect.top    = (LONG)rc_ui_header_num(f, "y", 0, 0, 0x4000);
    o.rect.right  = (LONG)rc_ui_header_num(f, "w", 0, 0, 0x4000);
    o.rect.bottom = (LONG)rc_ui_header_num(f, "h", 0, 0, 0x4000);
    /* A width and a height on the wire, a right and a bottom in a RECT. */
    if (o.rect.right)  o.rect.right  += o.rect.left;
    if (o.rect.bottom) o.rect.bottom += o.rect.top;

    ShotFrames sink(link);
    if (!sink.ready())
    {
        r.err("no_memory");
        return;
    }

    Screenshot shot;
    bool got = shot.capture(o, sink);
    if (got)
        got = sink.flush();

    if (!got)
    {
        if (sink.stopped())
        {
            r.err("cancelled");
            return;
        }
        r.err("failed", sink.ok() ? shot.error() : "connection lost");
        return;
    }

    rc_strf(w, sizeof(w), "%d", shot.width());
    rc_strf(h, sizeof(h), "%d", shot.height());
    rc_strf(bpp, sizeof(bpp), "%d", shot.bpp());
    rc_strf(bytes, sizeof(bytes), "%u", sink.sent());
    k[nkv] = "w";     v[nkv++] = w;
    k[nkv] = "h";     v[nkv++] = h;
    k[nkv] = "bpp";   v[nkv++] = bpp;
    k[nkv] = "bytes"; v[nkv++] = bytes;
    r.ok(k, v, nkv);
}

/* ---- the lane --------------------------------------------------------- */

UiOps::UiOps(Connection &conn, const UiConfig &cfg)
    : m_conn(&conn), m_cfg(&cfg), m_gate(0), m_ctx(0), m_com(false),
      m_stopFlag(0), m_watching(false), m_quietTicks(0)
{
    m_runId[0] = '\0';
    m_stopId[0] = '\0';
}

UiOps::~UiOps()
{
}

bool UiOps::onThreadStart()
{
    MSG msg;

    /* FIRST, and before anything is told this thread exists: a thread that
     * has never called a message function has no queue, and a post to it
     * fails by LOSING the message rather than by failing. */
    PeekMessageA(&msg, NULL, WM_USER, WM_USER, PM_NOREMOVE);

    /* The apartment every MSAA call is made from. S_FALSE means somebody
     * already did it on this thread, which is success. */
    HRESULT hr = CoInitialize(NULL);
    m_com = (hr == S_OK || hr == S_FALSE);
    if (!m_com)
        rc_log("ui: no COM apartment (hr=%08lx); MSAA will be unavailable",
               (unsigned long)hr);

    m_ctx = new UiContext;

    /* Non-fatal, both. Without the deadline a marshalled IAccessible call
     * against a wedged apartment has no timeout to give it; without the feed
     * a wait polls at its backstop instead. Either is worse than having
     * them and better than refusing to serve. */
    if (!m_ctx->deadline.install())
        rc_log("ui: no message filter; MSAA calls are unbounded");
    else
        m_ctx->msaa.setDeadline(&m_ctx->deadline);

    m_ctx->ready = true;
    return true;
}

void UiOps::onThreadStop()
{
    if (m_ctx)
    {
        m_ctx->ready = false;
        if (m_watching)
            m_ctx->watch.stop();
        m_watching = false;
        m_ctx->msaa.setDeadline(0);
        m_ctx->deadline.remove();
        delete m_ctx;
        m_ctx = 0;
    }
    if (m_com)
        CoUninitialize();
    m_com = false;
}

void UiOps::idle()
{
    MSG msg;

    /* Everything this thread does not own: the deadline filter's WM_TIMER
     * and the posted WinEvent callbacks the feed rides on. Drained to EMPTY
     * rather than one a pass, because the next wait would otherwise sleep
     * its whole slice with a message already sitting there. */
    while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE))
    {
        TranslateMessage(&msg);
        DispatchMessageA(&msg);
    }

    if (m_watching && ++m_quietTicks >= QUIET_TICKS_BEFORE_UNHOOK)
    {
        m_ctx->watch.stop();
        m_watching = false;
        m_quietTicks = 0;
    }
}

bool UiOps::cancel(const char *id)
{
    /* SIGNAL ONLY: the lane's own thread may be inside a walk or a capture,
     * and the reader carries the heartbeat. */
    if (!id)
        return false;
    if (m_runId[0] && strcmp(id, m_runId) == 0)
    {
        InterlockedExchange(&m_stopFlag, 1);
        return true;
    }
    rc_strf(m_stopId, sizeof(m_stopId), "%s", id);
    return false;
}

bool UiOps::stop(const char *id)
{
    if (!id)
        return false;
    if (m_runId[0] && strcmp(id, m_runId) == 0)
    {
        InterlockedExchange(&m_stopFlag, 1);
        return true;
    }
    /* Not running yet, or already over. Remember it: handle() starts a
     * request whose id matches already stopped, which is the window a probe
     * on the guest lost eight times out of eight on the client before the
     * same fix went in there. */
    rc_strf(m_stopId, sizeof(m_stopId), "%s", id);
    return false;
}

void UiOps::handle(rc_frame *f)
{
    const char *id = rc_frame_get(f, "id");
    Responder r(*m_conn, id, m_gate);

    if (!m_ctx || !m_ctx->ready)
    {
        r.err("busy");
        rc_frame_delete(f);
        return;
    }

    /* Armed for every op and before the work: a stop that arrived while
     * this request was still queued is honoured here. */
    InterlockedExchange(&m_stopFlag,
                        id && m_stopId[0] && !strcmp(id, m_stopId));
    m_stopId[0] = '\0';
    rc_strf(m_runId, sizeof(m_runId), "%s", id ? id : "");
    m_quietTicks = 0;

    /* The feed is started on the first request that may wait on it and left
     * running until the lane goes quiet. `skipOwnProcess` is ours, which on
     * this program means the hidden App window and the tray. */
    if (!m_watching && m_ctx->watch.start(0, 0, true))
        m_watching = true;

    {
        RespUiLink link(r, *m_cfg, &m_stopFlag);
        if (strcmp(f->type, "TREE") == 0)
        {
            /* `inproc` is REFUSED, not ignored. gremote does not carry the
             * in-process tier -- it needs HOOKDLL.DLL beside the exe and a
             * deny list with no force escape -- and a caller who asked for
             * that tier and silently got the other one would read a full
             * list box as empty. */
            if (rc_ui_header_flag(f, "inproc", false))
                link.end("inproc_unsupported", NULL, NULL, 0);
            else
                rc_ui_tree(link, m_ctx, f);
        }
        else if (strcmp(f->type, "RUN") == 0)
            rc_ui_run(link, m_ctx, f);
        else if (strcmp(f->type, "SHOT") == 0)
            shot(r, f);
        else if (strcmp(f->type, "CLIP") == 0)
            rc_ui_clip(link, f);           /* no context: see uiengine.h */
        else
            r.err("not_implemented");
    }

    m_runId[0] = '\0';
    rc_frame_delete(f);
}

void UiOps::abandon(rc_frame *f)
{
    Responder r(*m_conn, rc_frame_get(f, "id"), m_gate);
    r.err("stopping");
    rc_frame_delete(f);
}
