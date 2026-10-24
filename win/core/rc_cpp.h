/* The C++ prelude: the small number of types that make a bug class
 * unrepresentable rather than merely auditable.
 *
 * WHY C++ AT ALL. The language was never argued -- PLAN.md's toolchain section
 * is meticulous about -bm, WLINK_LNK and WINVER pinning and silent on this --
 * and C came from retro-agent, a codebase this project meant to lift from and
 * largely did not. Measured against the target: a mixed C/C++ gaston.exe
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
 * semantics, no RTTI. Exceptions ARE on (-xs, since 2026-09-25) so Open
 * Watcom's STL -- std::vector, std::string, std::wstring -- can be used
 * where a fixed table or hand-built string was the alternative; operator new
 * throws std::bad_alloc (core/heap.cpp), caught at App::run and at each
 * thread's entry point. Nothing here allocates unless told to.
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
#include <stdio.h>   /* FILE, for the File guard below */

extern "C" {
#include "core/str.h"
}

/* --- allocation ----------------------------------------------------------
 *
 * operator new/delete are defined once, in core/heap.cpp: over
 * GetProcessHeap(), throwing std::bad_alloc on failure.
 */

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

/* A lock taken only if the section is actually there.
 *
 * Two modules initialise their CRITICAL_SECTION lazily -- log.cpp and
 * rpc.cpp, both keyed on a `g_ready` int -- because the rule at the top of
 * this file forbids a file-scope object with a constructor, so there is no
 * way to have one ready before WinMain. Both wrote the pair out longhand:
 *
 *     if (g_ready) EnterCriticalSection(&g_lock);
 *     ...
 *     if (g_ready) LeaveCriticalSection(&g_lock);
 *
 * which reads the flag TWICE. Anything that clears it in between -- and
 * rc_log_close used to do exactly that -- leaves the section entered and
 * never left. Capturing the decision once, at construction, is the whole
 * point of this class; the destructor cannot disagree with the constructor
 * about whether a lock was taken.
 */
class ScopedLockIf {
    CRITICAL_SECTION *cs;
    ScopedLockIf(const ScopedLockIf &);
    ScopedLockIf &operator=(const ScopedLockIf &);
public:
    ScopedLockIf(CRITICAL_SECTION *c, int ready) : cs(ready ? c : 0)
    { if (cs) EnterCriticalSection(cs); }
    ~ScopedLockIf() { if (cs) LeaveCriticalSection(cs); }
    /* Whether it actually holds anything -- for the caller that wants to
     * skip work rather than do it unlocked. */
    int held() const { return cs != 0; }
};

/* --- one owner, any resource --------------------------------------------
 *
 * `Handle` and `FindHandle` used to be written out longhand here, and the
 * moment a `FILE *` wanted the same treatment it was going to be a third
 * copy of the same eight lines. This is that shape once, with the release
 * supplied as a policy.
 *
 * WHY A POLICY AND NOT A FUNCTION POINTER, which is the obvious alternative
 * and was measured against this one. Win32's handle types are NOT distinct:
 * `windef.h:159` makes `HGDIOBJ` a bare `void *`, and `HANDLE` is `void *`
 * too, so `Unique<HGDIOBJ>` and `Unique<HANDLE>` would be THE SAME TYPE and
 * the compiler would happily let a brush reach `CloseHandle`. The policy is
 * part of the type, so `Handle` and `FindHandle` stay distinct even though
 * both wrap the identical `void *`. Measured cost of that safety, across six
 * guarded resources with twelve exit paths: 432 bytes against 352 for a
 * type-erased `void *` class and 416 for a function-pointer member. Eighty
 * bytes to make the mix-up unrepresentable.
 *
 * (A function pointer as a NON-TYPE template parameter -- `Unique<T, &fn>`,
 * which would cost nothing per object -- does not compile on this front end:
 * `E333: cannot convert argument to type specified in function prototype`.
 * Don't spend an afternoon rediscovering that.)
 *
 * A policy is three statics. `live()` rather than a comparison against
 * `none()` because a kernel handle has TWO null values -- `CreateFile`
 * returns `INVALID_HANDLE_VALUE` and `CreateEvent` returns 0 -- and the
 * longhand `Handle` this replaces already had to test both.
 *
 * WHAT DOES NOT FIT HERE, deliberately. A resource whose release needs more
 * than the resource itself: `ReleaseDC` wants the HWND as well as the HDC,
 * and deleting a temp file wants its PATH, not a handle. Those get their own
 * small classes rather than being bent through this one -- see the note at
 * the end of this file.
 *
 * Non-copyable, like every guard above: a copied guard releases twice, and
 * `CloseHandle` on a recycled handle is worse than leaking.
 */
template <class T, class P>
class Unique {
    T v;
    Unique(const Unique &);
    Unique &operator=(const Unique &);
public:
    explicit Unique(T x = P::none()) : v(x) {}
    ~Unique() { reset(); }

    /* Read the member into a local FIRST, exactly as ComPtr::reset does:
     * a release can re-enter -- closing a window runs its WM_DESTROY, which
     * can reach back in here -- and finding a live pointer to a dead object
     * would be the worst kind of use-after-free to diagnose on a machine
     * with no debugger. */
    void reset(T x = P::none())
    {
        T old = v;
        v = x;
        if (P::live(old)) P::release(old);
    }

    T   get() const   { return v; }
    int valid() const { return P::live(v); }
    /* Hand ownership on deliberately -- to a caller that will close it, or
     * to a C API that takes it over. */
    T   release()     { T t = v; v = P::none(); return t; }
};

/* The policies. One per Win32 release call, named for the resource rather
 * than for the function, because the typedef below is what gets read. */

struct P_KernelHandle {
    static HANDLE none() { return INVALID_HANDLE_VALUE; }
    static int  live(HANDLE h) { return h != INVALID_HANDLE_VALUE && h != 0; }
    static void release(HANDLE h) { CloseHandle(h); }
};

struct P_SearchHandle {
    static HANDLE none() { return INVALID_HANDLE_VALUE; }
    static int  live(HANDLE h) { return h != INVALID_HANDLE_VALUE; }
    static void release(HANDLE h) { FindClose(h); }
};

struct P_GdiObject {
    static HGDIOBJ none() { return 0; }
    static int  live(HGDIOBJ o) { return o != 0; }
    static void release(HGDIOBJ o) { DeleteObject(o); }
};

struct P_MemoryDC {
    static HDC  none() { return 0; }
    static int  live(HDC d) { return d != 0; }
    static void release(HDC d) { DeleteDC(d); }
};

struct P_StdioFile {
    static FILE *none() { return 0; }
    static int  live(FILE *f) { return f != 0; }
    static void release(FILE *f) { fclose(f); }
};

struct P_Library {
    static HMODULE none() { return 0; }
    static int  live(HMODULE m) { return m != 0; }
    static void release(HMODULE m) { FreeLibrary(m); }
};

struct P_Menu {
    static HMENU none() { return 0; }
    static int  live(HMENU m) { return m != 0; }
    static void release(HMENU m) { DestroyMenu(m); }
};

struct P_Window {
    static HWND none() { return 0; }
    static int  live(HWND w) { return w != 0; }
    static void release(HWND w) { DestroyWindow(w); }
};

/* The names the code should actually use. `Handle` and `FindHandle` keep the
 * spelling and the whole surface they had as hand-written classes, so this
 * replaced them without touching a single call site. */
typedef Unique<HANDLE,  P_KernelHandle> Handle;
typedef Unique<HANDLE,  P_SearchHandle> FindHandle;
typedef Unique<HGDIOBJ, P_GdiObject>    GdiObj;
typedef Unique<HDC,     P_MemoryDC>     MemoryDC;
typedef Unique<FILE *,  P_StdioFile>    File;
typedef Unique<HMODULE, P_Library>      Dll;
typedef Unique<HMENU,   P_Menu>         MenuGuard;
typedef Unique<HWND,    P_Window>       WindowGuard;

/* --- the two that own a C++ object --------------------------------------
 *
 * Separate from Unique because `operator->` and `operator[]` are what make
 * them worth having, and neither belongs on a HANDLE.
 *
 * Both route through the overloaded `operator delete` at the top of this
 * file, so they free to GetProcessHeap like everything else -- the budget
 * stays in one allocator.
 *
 * A NOTE ON HOW MUCH THESE ARE WORTH HERE, because it is not much and
 * pretending otherwise would be dishonest: the guest has TWELVE `delete`
 * sites and ZERO `new[]`/`delete[]`. Ten of the twelve are the module
 * singletons torn down in `main.cpp`'s WM_DESTROY, in a fixed order that is
 * load-bearing -- `g_html` goes first and three live objects hold a
 * non-owning pointer to it -- and a guard would destroy in reverse
 * DECLARATION order, which is not the same order. So UniqueObj is for the
 * local-scope cases (`toolbar.cpp:44`, `htmlview.cpp:421` are the two today)
 * and UniqueArr has no caller at all yet. They are here because they are six
 * lines each and because `new[]` with no guard is how the first one goes
 * wrong, not because there is a queue of sites waiting.
 */
template <class T>
class UniqueObj {
    T *p;
    UniqueObj(const UniqueObj &);
    UniqueObj &operator=(const UniqueObj &);
public:
    explicit UniqueObj(T *v = 0) : p(v) {}
    ~UniqueObj() { reset(); }
    void reset(T *v = 0) { T *old = p; p = v; if (old) delete old; }
    T   *get() const { return p; }
    T   *operator->() const { return p; }
    int  valid() const { return p != 0; }
    T   *release() { T *t = p; p = 0; return t; }
};

template <class T>
class UniqueArr {
    T *p;
    UniqueArr(const UniqueArr &);
    UniqueArr &operator=(const UniqueArr &);
public:
    explicit UniqueArr(T *v = 0) : p(v) {}
    ~UniqueArr() { reset(); }
    void reset(T *v = 0) { T *old = p; p = v; if (old) delete [] old; }
    T   *get() const { return p; }
    T   &operator[](unsigned i) const { return p[i]; }
    int  valid() const { return p != 0; }
    T   *release() { T *t = p; p = 0; return t; }
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
    ~HeapPtr() { reset(); }
    /* Local first, as Unique::reset does. */
    void reset(T *v = 0)
    {
        T *old = p;
        p = v;
        if (old) HeapFree(GetProcessHeap(), 0, old);
    }
    T *get() const { return p; }
    T *operator->() const { return p; }
    int valid() const { return p != 0; }
    T *release() { T *t = p; p = 0; return t; }
};

/* --- the ones that need more than the resource itself --------------------
 *
 * `Unique` covers a resource whose release takes only the resource. These
 * three do not, which is why they are classes rather than policies. Each
 * exists because the audit in notes/mem-audit found the hand-written form of
 * it already going wrong, or one edit away from it.
 */

/* GetDC/ReleaseDC. The release needs the WINDOW as well as the DC, which is
 * why this cannot be a policy over HDC. Distinct from `MemoryDC` above on
 * purpose: a CreateCompatibleDC must be DeleteDC'd and a GetDC must be
 * ReleaseDC'd, the two are not interchangeable, and both are `HDC`. */
class WindowDC {
    HWND w;
    HDC  d;
    WindowDC(const WindowDC &);
    WindowDC &operator=(const WindowDC &);
public:
    explicit WindowDC(HWND h) : w(h), d(GetDC(h)) {}
    ~WindowDC() { if (d) ReleaseDC(w, d); }
    HDC get() const { return d; }
    int valid() const { return d != 0; }
};

/* SelectObject, which is a SWAP and not an acquisition: it hands back the
 * object that was there, and leaving that unrestored is a GDI leak owned by
 * somebody else's DC. Restoring it is the second half of a pair, which is
 * exactly the shape that gets missed on a path added later. */
class Selected {
    HDC     d;
    HGDIOBJ prev;
    Selected(const Selected &);
    Selected &operator=(const Selected &);
public:
    Selected(HDC dc, HGDIOBJ next) : d(dc), prev(SelectObject(dc, next)) {}
    ~Selected() { if (d && prev) SelectObject(d, prev); }
    HGDIOBJ previous() const { return prev; }
};

/* A file that should not survive unless it is committed.
 *
 * THIS ONE IS A BUG FIX WAITING FOR ITS CALL SITE, not a convenience. The
 * sync layer writes to `<name>.gaston-tmp` and then either commits it or
 * must remove it, and the removal is currently keyed on the temp file's
 * HANDLE still being open (`syncrpc.cpp`'s put_fail). `do_putend` closes
 * that handle before the compare-and-swap check -- it has to, the commit
 * needs it closed -- and every rejection path after that point therefore
 * skips the delete and leaves the file in the user's tree. An ordinary edit
 * conflict leaks one every time. See notes/mem-audit/02-findings.md F1.
 *
 * So this owns the NAME, not the handle. Closing the handle early cannot
 * disarm it; only `keep()` can, at the one point the file stops being ours.
 */
class TempFile {
    char path[MAX_PATH];
    int  armed;
    TempFile(const TempFile &);
    TempFile &operator=(const TempFile &);
public:
    TempFile() : armed(0) { path[0] = '\0'; }
    explicit TempFile(const char *p) : armed(0)
    {
        path[0] = '\0';
        if (p && *p) armed = rc_strcpy(path, sizeof(path), p);
    }

    /* For an owner that learns the name after it is constructed -- a writer
     * forms the temp's name inside begin(), not in its own constructor. Arms
     * only if the name fits, same as the constructor. */
    int arm(const char *p)
    {
        discard();
        if (p && *p) armed = rc_strcpy(path, sizeof(path), p);
        return armed;
    }

    /* Delete it now rather than at end of scope. */
    void discard()
    {
        if (armed) DeleteFileA(path);
        armed = 0;
        path[0] = '\0';
    }
    ~TempFile() { if (armed) DeleteFileA(path); }

    /* It committed; the file on disk is somebody else's now. */
    void keep() { armed = 0; }
    const char *get() const { return path; }
    /* False if the name did not fit, which means nothing was armed and the
     * caller must not proceed as though a temp file exists. */
    int valid() const { return armed; }
};

/* Restores a file's read-only attribute on exit from a scope (05.4).
 *
 * `rc_sync_commit` clears FILE_ATTRIBUTE_READONLY so DeleteFileA/MoveFileA
 * can replace the file, but if the commit fails (-3 or -4) the attribute
 * must be put back -- otherwise an edit conflict or busy file silently
 * strips the user's write protection with nothing reported.
 */
class ReadOnlyGuard {
    const char *path;
    DWORD orig;
    int armed;
    ReadOnlyGuard(const ReadOnlyGuard &);
    ReadOnlyGuard &operator=(const ReadOnlyGuard &);
public:
    ReadOnlyGuard(const char *p, DWORD attr) : path(p), orig(attr), armed(0)
    {
        if (path && orig != 0xFFFFFFFF && (orig & FILE_ATTRIBUTE_READONLY)) {
            if (SetFileAttributesA(path, orig & ~FILE_ATTRIBUTE_READONLY))
                armed = 1;
        }
    }
    ~ReadOnlyGuard()
    {
        if (armed)
            SetFileAttributesA(path, orig);
    }
    void disarm() { armed = 0; }
};

#endif
