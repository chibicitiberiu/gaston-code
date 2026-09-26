/* inject.h -- putting our code inside one named process, and no other.
 *
 * The strategic tier and the dangerous one. Everything here is opt-in, off
 * by default, and refuses loudly rather than falling back quietly -- a
 * caller has to be able to tell whether it got the in-process answer or
 * the out-of-process one, because they can differ and only one of them can
 * read a list box.
 *
 * What it buys, and the argument does not depend on any timing measurement:
 * `LB_GETTEXT`, `LVM_GETITEMTEXT` and `TVM_GETITEM` take a pointer, and on
 * 9x nothing marshals it across a process boundary at any clock speed.
 * NT's answer is `VirtualAllocEx` in the target, which 9x does not have.
 * Inside the target there is nothing to place. List, tree and list-view
 * contents are unreachable from outside and ordinary from in here.
 *
 * What it costs is that a mistake lands in somebody else's program. So:
 *
 *   a deny list with NO force escape, unlike KILL -- `REMOTE.md:344` lets
 *   KILL take Explorer with `force=1`, and nothing gets to inject into it;
 *   pid AND basename, because a 9x pid is an obfuscated pointer into the
 *   process database and it is reused;
 *   one thread, via `WH_CALLWNDPROC`, not the system-wide `WH_CBT` that
 *   put Explorer in scope and killed it on 2026-09-26;
 *   and the hook comes out again on every path, including the failures.
 */
#ifndef RC_UI_INJECT_H
#define RC_UI_INJECT_H

#include "core/rc_cpp.h"
#include "inproc/shm.h"

enum InjectResult
{
    INJECT_OK = 0,
    INJECT_DENIED,        /* on the deny list, or ourselves */
    INJECT_NO_DLL,        /* HOOKDLL.DLL is not beside the exe */
    INJECT_NO_WINDOW,     /* nothing to aim a thread hook at */
    INJECT_REFUSED,       /* the system would not install the hook */
    INJECT_NO_REPLY,      /* installed, but our code never ran there */
    INJECT_TRUNCATED,     /* it answered, and says the answer is partial */
    INJECT_FAILED
};

/* True if this executable may be injected into at all. Basename only, case
 * folded. Public because the refusal has to be checkable without doing it. */
bool rc_inject_allowed(const char *exeOrPath);

class Injector
{
public:
    Injector();
    ~Injector();

    /* Map the section and create the two events. The controller owns them;
     * the DLL only ever opens them, so a DLL loaded into a process while
     * nothing is listening simply does nothing. */
    bool open();
    void close();
    bool ready() const { return m_shm != 0; }

    /* Ask one process, named by a window it owns, to describe itself.
     *
     * `root` may be NULL for every top-level window of that process. The
     * hook is installed, the request posted, the answer waited for, and the
     * hook removed before this returns -- our code is never resident in
     * somebody else's program for longer than one call.
     */
    InjectResult collect(HWND anyWindowOfTarget, HWND root, bool wantItems,
                         DWORD deadlineMs);

    const RcInShm *shm() const { return m_shm; }
    const char *lastError() const { return m_err; }

private:
    bool stamp(DWORD pid, const char *exeBase);

    HANDLE   m_map;
    RcInShm *m_shm;
    HANDLE   m_req;
    HANDLE   m_done;
    DWORD    m_seq;
    char     m_err[128];

    Injector(const Injector &);
    Injector &operator=(const Injector &);
};

#endif
