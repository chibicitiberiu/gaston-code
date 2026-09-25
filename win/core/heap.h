/* Running out of memory. operator new throws std::bad_alloc (heap.cpp); the
 * places that catch it -- App::run and each thread's entry point -- call
 * this, which logs where it happened, tells the user, and ends the process.
 * On a 32 MB machine there is nothing sensible to limp on with. */
#ifndef RC_HEAP_H
#define RC_HEAP_H

#ifndef __cplusplus
#error "core/heap.h is C++"
#endif

void rc_out_of_memory(const char *where);

#endif
