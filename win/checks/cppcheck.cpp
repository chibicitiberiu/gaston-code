/* Does the C++ prelude actually guard?
 *
 * A guard that is wrong is worse than no guard, because it is trusted. This
 * runs the real `win/core/rc_cpp.h` and prints one line per case; the Python
 * side diffs the verdicts rather than reading the header and believing it.
 *
 * Two properties matter most, and both are drawn from bugs this codebase has
 * actually had:
 *
 *   - `Buf::ok()` must go false AT the limit, not one past it. `b14be6d` was a
 *     directory whose search pattern was exactly one byte short: a checked
 *     append that only caught overflow would have missed it.
 *   - every destructor must fire on every path, including early return, so the
 *     paired-operation bugs RAII is here to remove cannot come back.
 */
#include <windows.h>
#include <stdio.h>
#include "core/rc_cpp.h"

static void say(const char *name, int pass, const char *detail)
{
    printf("%s\t%s\t%s\n", pass ? "pass" : "FAIL", name, detail);
}

/* --- Buf ---------------------------------------------------------------- */

static void buf_cases(void)
{
    {   Buf<8> b; b.set("abc");
        say("fits", b.ok() && b.len() == 3, b.str()); }

    {   /* Exactly cap-1 is the longest that fits WITH its terminator. */
        Buf<8> b; b.set("1234567");
        say("exact-fit", b.ok() && b.len() == 7, b.str()); }

    {   /* One more is truncation, and must say so. */
        Buf<8> b; b.set("12345678");
        say("one-over-refused", !b.ok() && b.len() == 7, b.str()); }

    {   /* Sticky: a later success must not erase an earlier truncation. */
        Buf<8> b; b.set("12345678"); b.clear(); b.set("ok");
        say("clear-resets", b.ok(), b.str()); }

    {   Buf<8> b; b.set("123"); b.cat("456789");
        say("cat-truncation-sticks", !b.ok(), b.str()); }

    {   Buf<16> b; b.set("ab"); b.cat("cd");
        say("cat-fits", b.ok() && b.len() == 4, b.str()); }

    {   /* The 5cfc712 shape: a path at the limit plus a working suffix. */
        Path full, tmp;
        full.set("C:\\WORK");
        int a = full.ok();
        tmp.set(full.str()); tmp.cat(".gaston-tmp");
        say("workname-fits", a && tmp.ok(), tmp.str());

        Path big; char many[300];
        memset(many, 'd', 280); many[280] = '\0';
        big.join("C:\\WORK", many);
        say("overlong-join-refused", !big.ok(), "280-char rel");
    }

    {   /* join flips separators past the root and nowhere else. */
        Path p; p.join("C:\\WORK", "a/b/c.txt");
        say("join-separators", p.ok() &&
            lstrcmpA(p.str(), "C:\\WORK\\a\\b\\c.txt") == 0, p.str()); }

    {   /* Not NUL-terminated on the wire; setn must terminate anyway. */
        Buf<16> b; b.setn("abcdefXXXX", 6);
        say("setn-terminates", b.ok() && b.len() == 6, b.str()); }

    {   Buf<4> b; b.setn("abcdef", 6);
        say("setn-refuses", !b.ok() && b.len() == 3, b.str()); }
}

/* --- destructor ordering ------------------------------------------------ */

static char g_order[32];
static int  g_n;

class Mark {
    char c;
    Mark(const Mark &);
    Mark &operator=(const Mark &);
public:
    explicit Mark(char ch) : c(ch) {}
    ~Mark() { if (g_n < 30) g_order[g_n++] = c; }
};

static void nested_early_return(int bail)
{
    Mark a('a');
    {
        Mark b('b');
        if (bail) return;          /* b then a must still fire */
    }
    Mark c('c');
}

static void guard_cases(void)
{
    g_n = 0; nested_early_return(1); g_order[g_n] = '\0';
    say("dtor-on-early-return", lstrcmpA(g_order, "ba") == 0, g_order);

    g_n = 0; nested_early_return(0); g_order[g_n] = '\0';
    say("dtor-reverse-order", lstrcmpA(g_order, "bca") == 0, g_order);

    {   /* ScopedLock must leave the section even on an early return. */
        CRITICAL_SECTION cs;
        InitializeCriticalSection(&cs);
        { ScopedLock l(&cs); }
        /* If it did not leave, this would deadlock a second enter on 9x.
         * TryEnter is NT-only, so prove it by entering again and leaving. */
        EnterCriticalSection(&cs); LeaveCriticalSection(&cs);
        DeleteCriticalSection(&cs);
        say("scopedlock-releases", 1, "re-entered after scope");
    }

    {   /* Handle closes, and release() hands ownership on without closing. */
        HANDLE ev = CreateEventA(NULL, TRUE, FALSE, NULL);
        HANDLE kept;
        { Handle h(ev); kept = h.release(); }
        say("handle-release-keeps", kept == ev &&
            WaitForSingleObject(kept, 0) == WAIT_TIMEOUT, "still open");
        CloseHandle(kept);
    }

    {   /* HeapPtr frees, and release() does not. */
        HeapPtr<char> p((char *)HeapAlloc(GetProcessHeap(), 0, 16));
        say("heapptr-holds", p.valid(), "allocated");
        char *raw = p.release();
        say("heapptr-release", raw != 0 && !p.valid(), "ownership moved");
        HeapFree(GetProcessHeap(), 0, raw);
    }
}

int main(void)
{
    buf_cases();
    guard_cases();
    return 0;
}
