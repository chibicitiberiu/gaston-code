/* COM ownership, for the one translation unit that hosts MSHTML.
 *
 * NOT in rc_cpp.h on purpose. These need ole2.h and oleauto.h, and every other
 * .cpp in the guest gets by on windows.h alone -- dragging OLE into the common
 * prelude would put a few thousand lines of headers in front of net.cpp and
 * sync.cpp for types they never name.
 *
 * WHY THESE EXIST. htmlview.c released COM interfaces and BSTRs by hand at
 * about twenty sites. None of them leaked as written -- this is not a bug
 * report -- but the shape is the one that breaks when a return is added in
 * the middle, which is precisely what Phases 4-6 will do to this file. The
 * same argument as HeapPtr in net.cpp: the object that owns the reference
 * releases it, on every path out, including the ones nobody wrote yet.
 *
 * CINTERFACE is in force here, so a COM interface is a struct whose first
 * member is `lpVtbl`. Every COM vtable begins with QueryInterface, AddRef and
 * Release in that order, whatever the interface -- that is what makes the cast
 * to IUnknown below legal rather than merely convenient, and it is the same
 * guarantee the C macros IHTMLElement_Release and friends rely on.
 *
 * DIALECT: C++98 as Open Watcom implements it. No exceptions (-xd), so
 * nothing here can throw and no path needs unwinding beyond the destructor.
 */
#ifndef RC_COMPTR_H
#define RC_COMPTR_H

#ifndef __cplusplus
#error "comptr.h is C++ only"
#endif

#include <windows.h>
#include <ole2.h>
#include <oleauto.h>

/* Owns one COM reference and releases it exactly once.
 *
 * Non-copyable: two owners of one reference means a double Release, which on
 * 9x is a fault somewhere else entirely, long after the mistake. Use
 * release() to hand ownership on deliberately, exactly as HeapPtr does.
 */
template <class T>
class ComPtr {
    T *p;
    ComPtr(const ComPtr &);
    ComPtr &operator=(const ComPtr &);
public:
    explicit ComPtr(T *v = 0) : p(v) {}
    ~ComPtr() { reset(); }

    void reset(T *v = 0)
    {
        /* Read the member into a local FIRST. Release can re-enter -- a
         * document releasing its last reference runs teardown that can call
         * back into this window -- and finding a live pointer to a dead
         * object here would be the worst kind of use-after-free to diagnose
         * on a machine with no debugger. */
        T *old = p;
        p = v;
        if (old) {
            IUnknown *u = (IUnknown *)old;
            u->lpVtbl->Release(u);
        }
    }

    /* For the out-parameter shape COM uses everywhere:
     *     QueryInterface(x, &IID_IFoo, (void **)ptr.addr())
     * Releases anything already held, so a guard cannot silently leak by
     * being filled twice. */
    T **addr() { reset(); return &p; }

    T   *get() const   { return p; }
    T   *operator->() const { return p; }
    int  valid() const { return p != 0; }
    T   *release()     { T *t = p; p = 0; return t; }
};

/* Owns one BSTR. SysAllocString/SysFreeString rather than the heap: a BSTR
 * carries its length in the four bytes before the pointer and MUST go back to
 * the OLE allocator. */
class Bstr {
    BSTR b;
    Bstr(const Bstr &);
    Bstr &operator=(const Bstr &);
public:
    explicit Bstr(BSTR v = 0) : b(v) {}
    ~Bstr() { if (b) SysFreeString(b); }

    void reset(BSTR v = 0)
    {
        BSTR old = b;
        b = v;
        if (old) SysFreeString(old);
    }

    BSTR get() const   { return b; }
    int  valid() const { return b != 0; }
    BSTR release()     { BSTR t = b; b = 0; return t; }
};

#endif
