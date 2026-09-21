/* A transcript that survives being disconnected.
 *
 * WHAT THIS IS NOT: a second copy of the paginator. app/pagebar.h says "the
 * control holds exactly ONE page ... the whole reason a 32MB box can run
 * this", and that stays true -- the cache is on DISK, one file per page, and
 * exactly one of them is ever in memory, which is the same one the live path
 * already holds. Nothing here raises the high-water mark.
 *
 * WHY IT EXISTS: the bridge owns the transcript, so a guest that starts with
 * the bridge down, or loses it mid-conversation, has nothing at all to show.
 * Not a stale copy, not a warning -- an empty white rectangle and a chat list
 * with no chats in it. On a machine whose network is a 200MHz box's idea of
 * reliable, that is most of the first minute of every session.
 *
 * WHAT IT CACHES: each page of the open conversation as it arrives, and the
 * chat list. Both are exactly what the bridge sent -- "the guest transforms
 * nothing" applies here too, so a cached page is the same bytes MSHTML was
 * given, and rendering it back is the same call.
 *
 * WHAT IT DOES NOT CACHE: the live page while it is still being written. A
 * page is written to disk when the bridge says it is complete (PAGEEND), so
 * a half-streamed answer is never restored as though it were finished.
 *
 * DISK: a page is capped at the guest's page budget (16KB on a 32MB box), so
 * a hundred-page conversation is under two megabytes. Nothing here prunes,
 * which is a decision rather than an omission -- pruning needs a policy for
 * "which conversation is stale", the answer depends on the chat list this
 * same cache is holding, and getting it wrong throws away the one copy of
 * something. Recorded in BACKLOG rather than guessed at.
 */
#ifndef RC_CACHE_H
#define RC_CACHE_H

#ifndef __cplusplus
#error "cache.h is C++"
#endif

#include <windows.h>
#include <stdio.h>
#include "core/rc_cpp.h"   /* File, the RAII guard page_fp/chat_fp use */

class rc_html;

/* 8.3 all the way down: the cache lives on the guest's own FAT volume, and a
 * session id is a 36-character UUID. Directory names are a hash of it. */
#define RC_CACHE_KEY 9      /* 8 hex digits and a terminator */

class rc_cache {
    char root[MAX_PATH];        /* <exe dir>\CACHE */
    char dir[MAX_PATH];         /* root\<key>, the open conversation */
    char sess[64];              /* the id `dir` was derived from */
    File  page_fp;              /* open only between begin_page and end_page */
    File  chat_fp;              /* the chat list, writing or reading */
    int   chat_bad;             /* a write failed; do not rename over the good copy */
    int   page_no;
    int   usable;               /* zero once the disk has said no */

    rc_cache(const rc_cache &);
    rc_cache &operator=(const rc_cache &);
    rc_cache() : page_fp(0), chat_fp(0), chat_bad(0), page_no(0), usable(0)
    { root[0] = dir[0] = sess[0] = '\0'; }

    /* Non-zero if the path fit. Callers check it -- see the note in
     * cache.cpp: a truncated page path aliases another page's file. */
    int path_for(char *out, unsigned cap, int n) const;

public:
    /* `exe_dir` is where retroclaude.exe lives -- the INI and the log are
     * already there, and a cache somewhere else would be a second place to
     * look when clearing one machine's state. */
    static rc_cache *create(const char *exe_dir);
    /* page_fp and chat_fp are File guards: their destructors run right after
     * this one, in declaration order, and close whichever is still open.
     * Nothing left to do here by hand, which is why Watcom wants it inline. */
    ~rc_cache() {}

    /* Which conversation the pages that follow belong to. Creates the
     * directory. An empty id parks the cache: writes are dropped rather than
     * landing in the previous conversation's folder. */
    void open_session(const char *session_id);

    /* The id of the conversation cached most recently, for a cold start with
     * no bridge. Empty if there is none. */
    void last_session(char *out, unsigned cap) const;

    /* One page, written as it arrives. `add` between the two; anything
     * outside a begin/end pair is dropped rather than guessed at. */
    void begin_page(int n);
    void add(const void *body, unsigned len);
    void end_page();

    /* Render a cached page into `html`, or return zero if it is not there.
     * The caller is expected to have cleared the view first, exactly as
     * PAGESTART does on the live path. */
    int  load_page(int n, rc_html *html);

    /* How many pages this conversation has on disk: the highest N for which
     * P<N>.HTM exists, counting from 1. Zero means nothing is cached. */
    int  page_count() const;

    /* The chat list, streamed a row at a time in both directions.
     *
     * STREAMED, not buffered, and the first version of this was not: it held
     * the whole list in a static array, which cost 12.8KB of BSS -- on a
     * machine measured at 28 KB free after a turn, and for data that is
     * being written to a file one line at a time anyway. A line at a time
     * needs 220 bytes of stack and nothing else. */
    void begin_chats();
    void add_chat(const char *line);
    void end_chats();

    /* ... and back. `chats_next` fills `out` with one line, terminator
     * stripped, and returns zero when there are no more. Always close. */
    int  chats_open();
    int  chats_next(char *out, unsigned cap);
    void chats_close();

    int  ok() const { return usable; }
};

#endif
