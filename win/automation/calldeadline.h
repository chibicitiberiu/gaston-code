/* calldeadline.h -- the only thing that can interrupt a blocked MSAA call.
 *
 * Every budget elsewhere in this library bounds `SendMessageTimeout`, which
 * takes a timeout and was never the problem. The call that has no timeout to
 * give is the marshalled one: `IAccessible::get_accName` against a process
 * wedged inside its own apartment blocks, and neither a deadline check
 * before it nor one after it can do anything about that.
 *
 * COM's own answer is a message filter. While an outgoing call is
 * outstanding and a message arrives on the calling thread, COM asks the
 * filter's `MessagePending` what to do, and `PENDINGMSG_CANCELCALL` makes
 * the call fail with `RPC_E_CALL_CANCELED` instead of waiting forever.
 *
 * The catch, and the reason `REMOTE.md:372` mentions a timer in the same
 * breath: `MessagePending` is only consulted when a message actually
 * arrives. On an idle worker thread nothing arrives, so nothing asks, so
 * nothing cancels. The filter posts itself a timer while it is armed.
 *
 * Not `IsHungAppWindow`, which is Windows 2000 and later.
 */
#ifndef RC_AUTOMATION_CALLDEADLINE_H
#define RC_AUTOMATION_CALLDEADLINE_H

#include <windows.h>
#include <objidl.h>

/* How often the filter pokes its own queue while armed. Fast enough that a
 * deadline is honoured to about a tick, slow enough to be nothing: 55ms is
 * the clock's own resolution on 9x, so anything under it buys no accuracy
 * and costs real wakeups on a 200MHz machine. */
#define RC_DEADLINE_TICK_MS 55

class CallDeadline : public IMessageFilter
{
public:
    CallDeadline();

    /* Registers on the CALLING thread, which must be the one that makes the
     * MSAA calls -- a message filter is per apartment, and the apartment is
     * the thread. False if COM refused or one is already installed by
     * somebody we should not displace. */
    bool install();
    void remove();

    /* Arm before a batch of MSAA calls, disarm after. Nested arms take the
     * EARLIEST deadline: an inner budget may shorten an outer one, never
     * extend it. */
    void arm(DWORD ms);
    void disarm();

    bool armed() const   { return m_armed; }
    bool expired() const;

    /* Calls this filter actually cancelled. Zero on a healthy desktop, and
     * the number `autocheck` and the guest run report. */
    int  cancels() const { return m_cancels; }

    /* IUnknown. This object is a MEMBER of the service that owns it, never
     * heap-allocated, so Release does NOT delete -- the count exists so COM
     * is satisfied and so a leak shows up as a non-zero value at teardown. */
    virtual HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void **ppv);
    virtual ULONG   STDMETHODCALLTYPE AddRef();
    virtual ULONG   STDMETHODCALLTYPE Release();

    /* IMessageFilter */
    virtual DWORD STDMETHODCALLTYPE HandleInComingCall(
        DWORD callType, HTASK htaskCaller, DWORD tickCount,
        LPINTERFACEINFO lpInterfaceInfo);
    virtual DWORD STDMETHODCALLTYPE RetryRejectedCall(
        HTASK htaskCallee, DWORD tickCount, DWORD rejectType);
    virtual DWORD STDMETHODCALLTYPE MessagePending(
        HTASK htaskCallee, DWORD tickCount, DWORD pendingType);

private:
    IMessageFilter *m_prev;     /* whatever was there; put back on remove */
    bool     m_installed;
    bool     m_armed;
    DWORD    m_until;
    UINT_PTR m_timer;
    long     m_refs;
    int      m_cancels;

    CallDeadline(const CallDeadline &);
    CallDeadline &operator=(const CallDeadline &);
};

#endif
