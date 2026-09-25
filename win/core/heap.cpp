/* The one global operator new/delete, over GetProcessHeap().
 *
 * Not the CRT heap: "memory is the binding constraint", and letting `new`
 * reach a second allocator would split the budget in two.
 *
 * ONE definition, here, not inline in a header. It used to be inline in
 * rc_cpp.h, which gave the replacement only to files that included it; with
 * the STL passing strings and vectors between files, memory allocated by one
 * allocator would be freed by the other. The linker takes this object's
 * definitions ahead of the runtime's because libcore is searched first.
 *
 * THROWS std::bad_alloc on failure, as the STL requires (exceptions are on,
 * docs/REMOTE.md). App::run and each thread's entry point catch it, log it
 * and stop; nothing below them needs to check `new` for null any more.
 */
#include <new>
#include <windows.h>
#include "core/heap.h"
#include "core/log.h"
#include "core/str.h"

void *operator new(size_t n)
{
    void *p = HeapAlloc(GetProcessHeap(), 0, n ? n : 1);
    if (!p)
        throw std::bad_alloc();
    return p;
}

void *operator new[](size_t n)
{
    void *p = HeapAlloc(GetProcessHeap(), 0, n ? n : 1);
    if (!p)
        throw std::bad_alloc();
    return p;
}

void operator delete(void *p)
{
    if (p)
        HeapFree(GetProcessHeap(), 0, p);
}

void operator delete[](void *p)
{
    if (p)
        HeapFree(GetProcessHeap(), 0, p);
}

void rc_out_of_memory(const char *where)
{
    char msg[160];
    rc_log("OUT OF MEMORY in %s; exiting", where ? where : "?");
    rc_log_close();
    rc_strf(msg, sizeof msg,
            "The program ran out of memory (%s) and has to close.\n\n"
            "Closing other programs frees memory.", where ? where : "?");
    MessageBoxA(NULL, msg, "Out of memory", MB_OK | MB_ICONSTOP);
    ExitProcess(3);
}
