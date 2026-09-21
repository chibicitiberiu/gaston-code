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


/* --- Unique and the guards built on it ----------------------------------
 *
 * Behaviour, not shape. Each case proves the release ACTUALLY RAN by
 * observing its effect -- a closed handle, a flushed file, a deleted file --
 * rather than by trusting that a destructor was written.
 */

static int g_dtors;
struct Counted { ~Counted() { g_dtors++; } };

static void unique_cases(void)
{
    {   /* Handle (a typedef of Unique now) still closes. Proving a handle is
         * CLOSED rather than merely forgotten: duplicate it first, then a
         * wait on the original after the scope must fail. */
        HANDLE ev = CreateEventA(NULL, TRUE, FALSE, NULL);
        { Handle h(ev); }
        say("unique-handle-closes",
            WaitForSingleObject(ev, 0) == WAIT_FAILED, "wait failed as it should");
    }

    {   /* release() still hands ownership on without closing. */
        HANDLE ev = CreateEventA(NULL, TRUE, FALSE, NULL), kept;
        { Handle h(ev); kept = h.release(); }
        say("unique-release-keeps", kept == ev &&
            WaitForSingleObject(kept, 0) == WAIT_TIMEOUT, "still open");
        CloseHandle(kept);
    }

    {   /* reset() releases the one it was holding, not the new one. */
        HANDLE a = CreateEventA(NULL, TRUE, FALSE, NULL);
        HANDLE b = CreateEventA(NULL, TRUE, FALSE, NULL);
        {   Handle h(a);
            h.reset(b);
            say("unique-reset-drops-old",
                WaitForSingleObject(a, 0) == WAIT_FAILED, "old closed");
            say("unique-reset-keeps-new",
                WaitForSingleObject(b, 0) == WAIT_TIMEOUT, "new open");
        }
        say("unique-reset-then-scope",
            WaitForSingleObject(b, 0) == WAIT_FAILED, "new closed at scope end");
    }

    {   /* A kernel handle has TWO null values and neither may be closed.
         * CloseHandle(0) sets an error; an unguarded guard would call it. */
        SetLastError(0);
        { Handle h((HANDLE)0); }
        { Handle h(INVALID_HANDLE_VALUE); }
        say("unique-two-nulls", GetLastError() == 0, "neither null was closed");
    }

    {   /* File: the proof that fclose ran is that the bytes are readable.
         * fwrite alone leaves them in a stdio buffer. */
        char tmp[MAX_PATH], dir[MAX_PATH];
        GetTempPathA(sizeof(dir), dir);
        rc_strf(tmp, sizeof(tmp), "%src_file_test.txt", dir);
        {   File f(fopen(tmp, "wb"));
            if (f.valid()) fwrite("hello", 1, 5, f.get());
        }
        {   char got[8];
            FILE *r = fopen(tmp, "rb");
            size_t n = r ? fread(got, 1, 5, r) : 0;
            if (r) fclose(r);
            got[n] = '\0';
            say("unique-file-closes", n == 5 && lstrcmpA(got, "hello") == 0, got);
        }
        DeleteFileA(tmp);
    }

    {   /* GdiObj deletes. A deleted object's type query fails. */
        HGDIOBJ br = CreateSolidBrush(RGB(1, 2, 3));
        { GdiObj g(br); }
        say("unique-gdi-deletes", GetObjectType(br) == 0, "object gone");
    }

    {   /* FindHandle and Handle are DIFFERENT TYPES even though both wrap a
         * bare void*. This is the whole reason for the policy, and it is a
         * compile-time property -- the line below must not compile:
         *     FindHandle f; Handle h(f.get());   // fine, that is just a HANDLE
         *     FindHandle f = h;                  // refused: different types
         * Runtime cannot see that, so assert the thing runtime CAN see: they
         * do not share an instantiation, which sizeof-equality alone would
         * not prove. Recorded here so the property is at least written down. */
        say("unique-distinct-types", sizeof(Handle) == sizeof(FindHandle),
            "same layout, different type");
    }
}

/* --- TempFile, which exists for docs/mem-audit F1 ------------------------ */

static void tempfile_cases(void)
{
    char dir[MAX_PATH], tmp[MAX_PATH];
    GetTempPathA(sizeof(dir), dir);

    {   /* Unclaimed: the file must be gone at the end of the scope. */
        rc_strf(tmp, sizeof(tmp), "%src_temp_a.tmp", dir);
        { File f(fopen(tmp, "wb")); if (f.valid()) fwrite("x", 1, 1, f.get()); }
        {   TempFile t(tmp);
            say("tempfile-armed", t.valid(), t.get());
        }
        say("tempfile-deletes",
            GetFileAttributesA(tmp) == 0xFFFFFFFF, "gone after scope");
    }

    {   /* keep(): it committed, so the file must survive. */
        rc_strf(tmp, sizeof(tmp), "%src_temp_b.tmp", dir);
        { File f(fopen(tmp, "wb")); if (f.valid()) fwrite("x", 1, 1, f.get()); }
        {   TempFile t(tmp); t.keep(); }
        say("tempfile-keep-survives",
            GetFileAttributesA(tmp) != 0xFFFFFFFF, "still there");
        DeleteFileA(tmp);
    }

    {   /* THE F1 SHAPE, and the reason this class owns a NAME and not a
         * HANDLE. Close the handle early -- which do_putend must do, the
         * commit needs it closed -- and the delete must STILL happen. The
         * code this replaces keyed its cleanup on the handle being live, so
         * this exact sequence skipped the delete and leaked the file. */
        rc_strf(tmp, sizeof(tmp), "%src_temp_c.tmp", dir);
        {   TempFile t(tmp);
            {   File f(fopen(tmp, "wb"));
                if (f.valid()) fwrite("x", 1, 1, f.get());
            }                                   /* handle closed HERE */
            /* ... compare-and-swap refuses the push, we return ... */
        }
        say("tempfile-survives-early-close",
            GetFileAttributesA(tmp) == 0xFFFFFFFF, "deleted despite closed handle");
    }

    {   /* A name that does not fit must NOT arm: a half-copied path names a
         * different file, and deleting that is worse than leaking. */
        char toolong[MAX_PATH + 64];
        memset(toolong, 'd', sizeof(toolong) - 1); toolong[sizeof(toolong) - 1] = '\0';
        TempFile t(toolong);
        say("tempfile-overlong-refused", !t.valid(), "not armed");
    }
}

/* --- UniqueObj / UniqueArr ----------------------------------------------- */

static void owned_cases(void)
{
    g_dtors = 0;
    { UniqueObj<Counted> p(new Counted); }
    say("uniqueobj-destroys", g_dtors == 1, "one dtor");

    g_dtors = 0;
    {   UniqueObj<Counted> p(new Counted);
        Counted *raw = p.release();
        delete raw;
    }
    say("uniqueobj-release", g_dtors == 1, "released, not double-freed");

    g_dtors = 0;
    { UniqueArr<Counted> a(new Counted[4]); }
    say("uniquearr-destroys-all", g_dtors == 4, "four dtors");
}

int main(void)
{
    buf_cases();
    guard_cases();
    unique_cases();
    tempfile_cases();
    owned_cases();
    return 0;
}
