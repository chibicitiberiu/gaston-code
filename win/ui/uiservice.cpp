#include "ui/uiservice.h"
#include "ui/uirpc.h"
#include "core/log.h"

#include <process.h>
#include <new>

UiContext::UiContext()
{
    selfPid = GetCurrentProcessId();
    ready = false;
}

UiService::UiService(rc_net *net)
    : m_net(net), m_tid(0)
{
}

const char *UiService::name() const
{
    return "desktop";
}

bool UiService::init()
{
    m_ctx.reset(new UiContext());
    return m_ctx.valid() != 0;
}

unsigned __stdcall UiService::entry(void *self)
{
    /* The only place that catches it, as in rpc.cpp: operator new throws and
     * nothing between here and there uses exceptions for control flow, so a
     * bad_alloc arriving anywhere in a request unwinds to exactly this
     * point. Letting it leave the thread entry terminates the process. */
    try
    {
        ((UiService *)self)->body();
    }
    catch (const std::bad_alloc &)
    {
        rc_log("desktop: out of memory, the worker is stopping");
    }
    return 0;
}

void UiService::body()
{
    UiContext *ctx = m_ctx.get();
    MSG msg;

    /* Force the queue into existence BEFORE anything is told this thread
     * exists. PostThreadMessage to a thread that has never called a message
     * function fails, and it fails by losing the message rather than by
     * blocking, so a request posted in that window would simply never be
     * answered. */
    PeekMessageA(&msg, NULL, WM_USER, WM_USER, PM_NOREMOVE);

    /* Apartment-threaded, on the thread that will make every COM call.
     * S_FALSE means somebody got here first, which is fine. */
    HRESULT hr = CoInitialize(NULL);
    bool com = (hr == S_OK || hr == S_FALSE);

    /* The deadline that can interrupt a call already in flight. Without it
     * an MSAA property against a wedged apartment blocks this thread for as
     * long as that application stays wedged -- which on 9x can be until it
     * is killed. */
    if (com && ctx->deadline.install())
        ctx->msaa.setDeadline(&ctx->deadline);
    else
        rc_log("desktop: no message filter; MSAA calls are unbounded");

    /* The event feed, skipping our own process so the client does not wake
     * on its own repaints. Not fatal if it refuses: the waits fall back to
     * polling and say so in their report. */
    if (!ctx->watch.start(0, 0, true))
        rc_log("desktop: no event feed; waits will poll");

    rc_uirpc_attach(GetCurrentThreadId(), m_net, ctx);
    ctx->ready = true;
    rc_log("desktop: worker up (msaa=%d, feed=%d)",
           ctx->msaa.available() ? 1 : 0, ctx->watch.running() ? 1 : 0);

    while (GetMessageA(&msg, NULL, 0, 0) > 0)
    {
        if (msg.message == RC_WM_UI_QUIT)
            break;
        if (msg.message == RC_WM_UI_REQUEST)
        {
            rc_frame *f = (rc_frame *)msg.lParam;
            if (f)
            {
                rc_uirpc_handle(m_net, ctx, f);
                rc_frame_delete(f);
            }
            continue;
        }
        /* Anything else is the system's -- WM_TIMER from the deadline
         * filter, and the posted WinEvent callbacks the feed rides on.
         * Dispatching is what makes both of those work. */
        TranslateMessage(&msg);
        DispatchMessageA(&msg);
    }

    /* Detach FIRST, so nothing new is posted to a queue nobody will read. */
    rc_uirpc_detach();
    ctx->ready = false;

    /* Drain what is already queued and answer it, rather than letting the
     * bridge wait out a timeout per request. Same duty as rpc.cpp's
     * shutdown drain. */
    while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE))
        if (msg.message == RC_WM_UI_REQUEST && msg.lParam)
        {
            rc_frame *f = (rc_frame *)msg.lParam;
            rc_uirpc_refuse(m_net, f, "stopping");
            rc_frame_delete(f);
        }

    ctx->watch.stop();
    ctx->deadline.remove();
    ctx->msaa.setDeadline(0);
    if (com)
        CoUninitialize();
    rc_log("desktop: worker down");
}

bool UiService::start()
{
    unsigned tid = 0;
    /* _beginthreadex and never CreateThread: Open Watcom's per-thread CRT
     * data is set up by the former, and wire/connection.cpp records a real
     * machine refusing CreateThread with a NULL thread-id argument. */
    m_thread.reset((HANDLE)_beginthreadex(NULL, 0, entry, this, 0, &tid));
    if (!m_thread.valid())
    {
        rc_log("desktop: could not start the worker thread");
        return false;
    }
    m_tid = tid;
    return true;
}

void UiService::signalStop()
{
    if (m_tid)
        PostThreadMessageA(m_tid, RC_WM_UI_QUIT, 0, 0);
}

bool UiService::join(DWORD timeoutMs)
{
    if (!m_thread.valid())
        return true;
    if (WaitForSingleObject(m_thread.get(), timeoutMs) != WAIT_OBJECT_0)
        return false;
    m_thread.reset();
    m_tid = 0;
    return true;
}
