#include "automation/calldeadline.h"

/* Behind WINVER >= 0x0500 in some SDKs and the build pins 0x0400. */
#ifndef PENDINGMSG_CANCELCALL
#define PENDINGMSG_CANCELCALL    0
#define PENDINGMSG_WAITNOPROCESS 1
#define PENDINGMSG_WAITDEFPROCESS 2
#endif
#ifndef SERVERCALL_ISHANDLED
#define SERVERCALL_ISHANDLED 0
#endif

static const GUID RC_IID_IMessageFilter =
    { 0x00000016, 0x0000, 0x0000,
      { 0xc0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46 } };

/* And IUnknown's, for the same reason the one above is here -- which it was
 * not, and that cost 31,520 bytes in four exes. Watcom ships uuid.lib as a
 * SINGLE object module, so the one `IID_IUnknown` in QueryInterface below
 * pulled all 1,970 GUIDs into gremote, uicheck, autocheck and the rest, with
 * the linker's own map marking 1,969 of them unreferenced. Measured
 * 2026-10-11 (research/measurements/imagebytes-host-20261011.txt). The RC_
 * prefix is required, not tidiness: unknwn.h already declares the name as
 * `EXTERN_C const IID`, so a static of the same name would not compile. */
static const GUID RC_IID_IUnknown =
    { 0x00000000, 0x0000, 0x0000,
      { 0xc0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46 } };

CallDeadline::CallDeadline()
{
    m_prev = 0;
    m_installed = false;
    m_armed = false;
    m_until = 0;
    m_timer = 0;
    m_refs = 1;
    m_cancels = 0;
}

bool CallDeadline::install()
{
    if (m_installed)
        return true;
    /* CoRegisterMessageFilter hands back whoever was registered before.
     * Keeping it and putting it back is not politeness: the client's own
     * OLE control may have one, and dropping it on the floor changes how
     * the transcript view behaves under a busy server. */
    if (CoRegisterMessageFilter(this, &m_prev) != S_OK)
        return false;
    m_installed = true;
    return true;
}

void CallDeadline::remove()
{
    if (!m_installed)
        return;
    disarm();
    IMessageFilter *mine = 0;
    CoRegisterMessageFilter(m_prev, &mine);
    if (m_prev)
        m_prev = 0;
    m_installed = false;
}

void CallDeadline::arm(DWORD ms)
{
    DWORD until = GetTickCount() + ms;
    /* The EARLIEST wins. A nested arm may shorten an outer budget and must
     * never extend it -- an inner call that quietly reset the clock is how
     * an outer deadline stops meaning anything. */
    if (m_armed && (long)(until - m_until) > 0)
        return;
    m_until = until;
    if (m_armed)
        return;
    m_armed = true;
    /* A thread timer, not a window timer: this runs on a worker that owns no
     * window. WM_TIMER lands in the thread queue, which is all COM needs to
     * come and ask us what to do. */
    m_timer = SetTimer(NULL, 0, RC_DEADLINE_TICK_MS, NULL);
}

void CallDeadline::disarm()
{
    if (m_timer)
        KillTimer(NULL, m_timer);
    m_timer = 0;
    m_armed = false;
}

bool CallDeadline::expired() const
{
    if (!m_armed)
        return false;
    return (long)(GetTickCount() - m_until) >= 0;
}

HRESULT STDMETHODCALLTYPE CallDeadline::QueryInterface(REFIID riid,
                                                       void **ppv)
{
    if (!ppv)
        return E_POINTER;
    if (IsEqualGUID(riid, RC_IID_IUnknown)
        || IsEqualGUID(riid, RC_IID_IMessageFilter))
    {
        *ppv = (void *)this;
        AddRef();
        return S_OK;
    }
    *ppv = 0;
    return E_NOINTERFACE;
}

ULONG STDMETHODCALLTYPE CallDeadline::AddRef()
{
    return (ULONG)++m_refs;
}

ULONG STDMETHODCALLTYPE CallDeadline::Release()
{
    /* Deliberately never deletes: this object is a member of the service
     * that owns it and its lifetime is that service's. */
    if (m_refs > 0)
        --m_refs;
    return (ULONG)m_refs;
}

DWORD STDMETHODCALLTYPE CallDeadline::HandleInComingCall(
    DWORD callType, HTASK htaskCaller, DWORD tickCount,
    LPINTERFACEINFO lpInterfaceInfo)
{
    (void)callType; (void)htaskCaller; (void)tickCount; (void)lpInterfaceInfo;
    /* We are a client here, not a server. Refusing incoming calls would
     * break the OLE control in the same apartment. */
    return SERVERCALL_ISHANDLED;
}

DWORD STDMETHODCALLTYPE CallDeadline::RetryRejectedCall(
    HTASK htaskCallee, DWORD tickCount, DWORD rejectType)
{
    (void)htaskCallee; (void)tickCount; (void)rejectType;
    /* -1 means give up rather than retry. A server that rejected us once is
     * busy, and a walk has a deadline; anything >= 0 is a retry delay and
     * would turn one slow control into a stalled walk. */
    return (DWORD)-1;
}

DWORD STDMETHODCALLTYPE CallDeadline::MessagePending(
    HTASK htaskCallee, DWORD tickCount, DWORD pendingType)
{
    (void)htaskCallee; (void)tickCount; (void)pendingType;
    if (expired())
    {
        m_cancels++;
        /* The call fails with RPC_E_CALL_CANCELED, which every caller in
         * this library already treats as "this window has no answer" -- the
         * same path as a window with no accessible object at all. */
        return PENDINGMSG_CANCELCALL;
    }
    /* Do NOT dispatch while we wait. Dispatching here re-enters our own
     * window procedures from inside somebody else's outgoing call, which is
     * the reentrancy this whole threading arrangement exists to avoid. */
    return PENDINGMSG_WAITNOPROCESS;
}
