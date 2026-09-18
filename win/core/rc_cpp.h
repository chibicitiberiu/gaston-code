/* The C++ prelude: the small number of types that make a bug class
 * unrepresentable rather than merely auditable.
 *
 * WHY C++ AT ALL. The language was never argued -- PLAN.md's toolchain section
 * is meticulous about -bm, WLINK_LNK and WINVER pinning and silent on this --
 * and C came from retro-agent, a codebase this project meant to lift from and
 * largely did not. Measured against the target: a mixed C/C++ retroclaude.exe
 * imports the same symbols from the same DLLs, has the same CMOV count, and
 * committed-at-load BSS went DOWN. The load-time hazard that would have
 * settled it -- one NT-only import kills the image before WinMain with nothing
 * logged -- is absent.
 *
 * WHAT IT BUYS, concretely. On 2026-09-18 nine defects were fixed in the guest
 * and three were silent truncation: a destroyed file, a vanished subtree, a
 * cut-off message. In each, the information needed to refuse was present at
 * the call site and discarded, because `_snprintf`'s return was ignored at 93
 * of 95 sites and `join()` returned void. `Buf<N>` carries the answer with the
 * string instead of leaving it to be checked.
 *
 * DIALECT: C++98/03 as Open Watcom implements it. No nullptr, no move
 * semantics, no templates beyond the trivial, no exceptions (-xd), no RTTI.
 * Nothing here allocates unless told to.
 *
 * RULE: no file-scope objects with non-trivial constructors. Static ctors DO
 * run under `nt_win` before WinMain, which is before rc_log_open, so anything
 * that logs or touches the heap at that point does so into a void.
 */
#ifndef RC_CPP_H
#define RC_CPP_H

#ifndef __cplusplus
#error "rc_cpp.h is the C++ prelude; C translation units must not include it"
#endif

#include <windows.h>
#include <string.h>

extern "C" {
#include "core/str.h"
}

/* --- allocation ----------------------------------------------------------
 *
 * Over GetProcessHeap(), not the CRT heap. The guest deliberately uses
 * HeapAlloc everywhere -- "memory is the binding constraint" -- and letting
 * `new` reach a second allocator would split the budget in two. Returns null
 * rather than throwing: with -xd and a null return Watcom SKIPS the
 * constructor, so the existing `if (!p) return;` discipline carries over
 * unchanged from the C.
 */
inline void *operator new(size_t n)
{
    return HeapAlloc(GetProcessHeap(), 0, n ? n : 1);
}

inline void *operator new[](size_t n)
{
    return HeapAlloc(GetProcessHeap(), 0, n ? n : 1);
}

inline void operator delete(void *p)
{
    if (p) HeapFree(GetProcessHeap(), 0, p);
}

inline void operator delete[](void *p)
{
    if (p) HeapFree(GetProcessHeap(), 0, p);
}

/* --- a string that knows whether it fits ---------------------------------
 *
 * The whole point is `ok()`. A truncated path is a valid-looking path to a
 * DIFFERENT file, so a caller that cannot tell has already lost: it goes on to
 * stat, open or rename the wrong thing. Sticky, so a sequence of appends
 * reports the truncation that happened three calls ago.
 *
 * Refuses at equality, not just past it. `b14be6d` turned on exactly that: a
 * directory whose search pattern was one byte short lost its entire subtree,
 * and a checked strncat that only caught overflow would have missed it.
 */
template <unsigned N>
class Buf {
    char c[N];
    int  fit;
public:
    Buf() : fit(1) { c[0] = '\0'; }

    /* Whether everything asked for actually landed. Check it. */
    int ok() const { return fit; }
    const char *str() const { return c; }
    char *raw() { return c; }
    unsigned cap() const { return N; }
    unsigned len() const { return (unsigned)strlen(c); }
    int empty() const { return c[0] == '\0'; }

    void clear() { c[0] = '\0'; fit = 1; }

    int set(const char *s) { fit = rc_strcpy(c, N, s) && fit; return fit; }

    int setn(const char *s, unsigned n)
    { fit = rc_strncpy(c, N, s, n) && fit; return fit; }

    int cat(const char *s)
    {
        unsigned l = len();
        if (l >= N - 1) { fit = 0; return 0; }
        fit = rc_strcpy(c + l, N - l, s) && fit;
        return fit;
    }

    /* Join a root and a relative path the way the guest holds them, flipping
     * '/' to '\\' beyond the root. The one place separators convert. */
    int join(const char *root, const char *rel)
    {
        char *q;
        fit = rc_strf(c, N, "%s\\%s", root, rel) && fit;
        for (q = c + strlen(root); *q; q++)
            if (*q == '/') *q = '\\';
        return fit;
    }
};

typedef Buf<MAX_PATH> Path;

/* --- scope guards --------------------------------------------------------
 *
 * Every one of these replaces a paired operation whose second half is missed
 * on an error path. Non-copyable by construction: a copied guard releases
 * twice, which for CloseHandle on a recycled handle is worse than leaking.
 */
class ScopedLock {
    CRITICAL_SECTION *cs;
    ScopedLock(const ScopedLock &);
    ScopedLock &operator=(const ScopedLock &);
public:
    explicit ScopedLock(CRITICAL_SECTION *c) : cs(c) { EnterCriticalSection(cs); }
    ~ScopedLock() { LeaveCriticalSection(cs); }
};

class Handle {
    HANDLE h;
    Handle(const Handle &);
    Handle &operator=(const Handle &);
public:
    explicit Handle(HANDLE v = INVALID_HANDLE_VALUE) : h(v) {}
    ~Handle() { if (h != INVALID_HANDLE_VALUE && h) CloseHandle(h); }
    HANDLE get() const { return h; }
    int valid() const { return h != INVALID_HANDLE_VALUE && h != 0; }
    void reset(HANDLE v)
    {
        if (h != INVALID_HANDLE_VALUE && h) CloseHandle(h);
        h = v;
    }
    /* Hand ownership back to a C caller that will close it itself. */
    HANDLE release() { HANDLE t = h; h = INVALID_HANDLE_VALUE; return t; }
};

class FindHandle {
    HANDLE h;
    FindHandle(const FindHandle &);
    FindHandle &operator=(const FindHandle &);
public:
    explicit FindHandle(HANDLE v = INVALID_HANDLE_VALUE) : h(v) {}
    ~FindHandle() { if (h != INVALID_HANDLE_VALUE) FindClose(h); }
    HANDLE get() const { return h; }
    int valid() const { return h != INVALID_HANDLE_VALUE; }
};

/* A HeapAlloc'd block with one owner. `release()` for the sites that hand the
 * pointer onward -- the RPC worker takes ownership of a frame, and that
 * transfer is deliberate rather than a leak. */
template <class T>
class HeapPtr {
    T *p;
    HeapPtr(const HeapPtr &);
    HeapPtr &operator=(const HeapPtr &);
public:
    explicit HeapPtr(T *v = 0) : p(v) {}
    ~HeapPtr() { if (p) HeapFree(GetProcessHeap(), 0, p); }
    T *get() const { return p; }
    T *operator->() const { return p; }
    int valid() const { return p != 0; }
    T *release() { T *t = p; p = 0; return t; }
};

#endif
