/* cache.cpp -- the transcript that survives being disconnected.
 * See app/cache.h for what this is and, more importantly, what it is not. */
#include <windows.h>
#include <stdio.h>
#include <string.h>

#include "app/cache.h"
#include "app/htmlview.h"
#include "core/log.h"
#include "core/path.h"
#include "core/str.h"
#include "core/rc_cpp.h"

/* The biggest page the guest ever asks the bridge for, plus room. The bridge
 * scales its page budget to the guest's RAM and tells it so at handshake; on
 * a 32MB box that is 16KB. A cached page larger than this is refused rather
 * than read into memory, because the one thing this module must not do is
 * become a reason the transcript stops fitting. */
#define CACHE_PAGE_MAX 65536

/* A session id is a 36-character UUID and the cache lives on a FAT volume, so
 * the directory is a hash of it.
 *
 * FNV-1a rather than anything cleverer: a collision costs one conversation
 * showing another's cached pages until the bridge replaces them, which is
 * ugly and not dangerous, and 32 bits over the few dozen conversations a
 * person has makes it vanishingly unlikely anyway. */
static void key_of(const char *id, char *out, unsigned cap)
{
    unsigned long h = 2166136261UL;
    for (; id && *id; id++) {
        h ^= (unsigned char)*id;
        h *= 16777619UL;
    }
    rc_strf(out, cap, "%08lX", h);
}

rc_cache *rc_cache::create(const char *exe_dir)
{
    rc_cache *p = new rc_cache();
    if (!p) return NULL;

    rc_strf(p->root, sizeof(p->root), "%s\\CACHE",
            (exe_dir && *exe_dir) ? exe_dir : ".");
    if (!rc_mkpath(p->root)) {
        /* Not fatal and not silent. A read-only or full disk means no cache,
         * which costs history after a disconnect and nothing else -- but a
         * cache that quietly does nothing is exactly the kind of thing that
         * gets reported as "it forgot my conversation" a month later. */
        rc_log("cache: cannot create %s (%lu); history will not be kept",
               p->root, (unsigned long)GetLastError());
        return p;
    }
    p->usable = 1;
    rc_log("cache: %s", p->root);
    return p;
}

/* Non-zero if the whole path fit. CHECKED by every caller, because a
 * truncated one is not a shorter path, it is a DIFFERENT file: with an exe
 * directory near 250 characters the "P%03d.HTM" suffix is what gets cut, so
 * P001.HTM and P002.HTM resolve to one name -- page 2 overwrites page 1,
 * page_count() stops at 1, and paging back offline shows the wrong page with
 * nothing to say so. Unlikely on a C:\GASTON install; listed because it is
 * the ignored-return class this codebase set out to close. (audit 01.37) */
int rc_cache::path_for(char *out, unsigned cap, int n) const
{
    return rc_strf(out, cap, "%s\\P%03d.HTM", dir, n);
}

void rc_cache::open_session(const char *session_id)
{
    char key[RC_CACHE_KEY];
    char last[MAX_PATH];

    page_fp.reset();
    if (!usable || !session_id || !*session_id) {
        dir[0] = '\0';
        sess[0] = '\0';
        return;
    }
    if (sess[0] && strcmp(sess, session_id) == 0)
        return;                 /* already open */

    rc_strf(sess, sizeof(sess), "%s", session_id);
    key_of(session_id, key, sizeof(key));
    /* If the directory itself does not fit there is nowhere to cache; say so
     * and stay off rather than writing pages into a truncated path shared
     * with another session. */
    if (!rc_strf(dir, sizeof(dir), "%s\\%s", root, key)) {
        rc_log("cache: %s is too long to hold a session directory", root);
        dir[0] = '\0';
        return;
    }
    if (!rc_mkpath(dir)) {
        rc_log("cache: cannot create %s (%lu)", dir,
               (unsigned long)GetLastError());
        dir[0] = '\0';
        return;
    }

    /* Which conversation to show on a cold start with no bridge. Written
     * here rather than at exit: the app is closed with the X button, and a
     * "save on the way out" that a power cut skips is worse than useless --
     * it would restore whatever was open two sessions ago. */
    rc_strf(last, sizeof(last), "%s\\LAST.TXT", root);
    {
        File fp(fopen(last, "wb"));
        if (fp.valid())
            fwrite(session_id, 1, strlen(session_id), fp.get());
    }
}

void rc_cache::last_session(char *out, unsigned cap) const
{
    char path[MAX_PATH];
    size_t n;

    if (!out || cap == 0) return;
    out[0] = '\0';
    if (!usable) return;
    rc_strf(path, sizeof(path), "%s\\LAST.TXT", root);

    File fp(fopen(path, "rb"));
    if (!fp.valid()) return;
    n = fread(out, 1, cap - 1, fp.get());
    out[n] = '\0';
    /* Written without a terminator, so trim anything a text editor may have
     * added if somebody opened it. */
    while (n && (out[n - 1] == '\n' || out[n - 1] == '\r' || out[n - 1] == ' '))
        out[--n] = '\0';
}

void rc_cache::begin_page(int n)
{
    char path[MAX_PATH];

    page_fp.reset();
    if (!usable || !dir[0] || n < 1) return;

    page_no = n;
    /* A temporary name while it is being written, renamed on end_page. A
     * page half-written when the link dropped must not be readable as a
     * whole one -- the whole point of this module is that what it hands back
     * is what the bridge actually sent. */
    rc_strf(path, sizeof(path), "%s\\P%03d.TMP", dir, n);
    page_fp.reset(fopen(path, "wb"));
    if (!page_fp.valid())
        rc_log("cache: cannot write %s (page %d not kept)", path, n);
}

void rc_cache::add(const void *body, unsigned len)
{
    if (!page_fp.valid() || !body || !len) return;
    if (fwrite(body, 1, len, page_fp.get()) != len) {
        rc_log("cache: write failed on page %d; dropping it", page_no);
        page_fp.reset();
    }
}

void rc_cache::end_page()
{
    char tmp[MAX_PATH], dest[MAX_PATH];

    if (!page_fp.valid()) return;
    page_fp.reset();

    /* Both checked: a truncated pair can alias a DIFFERENT page's file, and
     * the next two lines delete and rename. */
    if (!rc_strf(tmp, sizeof(tmp), "%s\\P%03d.TMP", dir, page_no) ||
        !path_for(dest, sizeof(dest), page_no)) {
        rc_log("cache: page %d path does not fit; dropping it", page_no);
        return;
    }
    DeleteFileA(dest);
    if (!MoveFileA(tmp, dest)) {
        rc_log("cache: rename of page %d failed (%lu)", page_no,
               (unsigned long)GetLastError());
        DeleteFileA(tmp);
    }
}

int rc_cache::load_page(int n, rc_html *html)
{
    char path[MAX_PATH];
    long size;
    char *buf;
    size_t got;

    if (!usable || !dir[0] || !html || n < 1) return 0;
    if (!path_for(path, sizeof(path), n)) return 0;

    File fp(fopen(path, "rb"));
    if (!fp.valid()) return 0;

    fseek(fp.get(), 0, SEEK_END);
    size = ftell(fp.get());
    fseek(fp.get(), 0, SEEK_SET);
    if (size <= 0 || size > CACHE_PAGE_MAX) {
        rc_log("cache: page %d is %ld bytes; refusing to load it", n, size);
        return 0;
    }

    /* The heap, and freed before returning. One page at a time is the same
     * ceiling the live path already lives under; holding two would not be. */
    buf = (char *)HeapAlloc(GetProcessHeap(), 0, (SIZE_T)size + 1);
    if (!buf) return 0;
    got = fread(buf, 1, (size_t)size, fp.get());
    buf[got] = '\0';

    html->block(buf);
    HeapFree(GetProcessHeap(), 0, buf);
    return 1;
}

int rc_cache::page_count() const
{
    char path[MAX_PATH];
    int n = 0;

    if (!usable || !dir[0]) return 0;
    for (;;) {
        if (!path_for(path, sizeof(path), n + 1)) break;
        if (GetFileAttributesA(path) == 0xFFFFFFFF) break;
        n++;
        if (n >= 999) break;    /* P%03d runs out, and so does patience */
    }
    return n;
}

void rc_cache::begin_chats()
{
    char tmp[MAX_PATH];

    chat_fp.reset();
    if (!usable) return;
    rc_strf(tmp, sizeof(tmp), "%s\\CHATS.TMP", root);
    chat_fp.reset(fopen(tmp, "wb"));
    chat_bad = 0;
    /* Logged, unlike before -- every other fopen in this file says when it
     * fails, and a chat cache that silently never starts is a cold start that
     * silently shows nothing. */
    if (!chat_fp.valid()) rc_log("cache: cannot write %s", tmp);
}

void rc_cache::add_chat(const char *line)
{
    if (!chat_fp.valid() || !line) return;
    /* TRACKED, the way rc_cache::add already does it for a page. A disk that
     * fills partway through a 40-conversation list must not end up replacing
     * a complete CHATS.TXT with a truncated one -- the next cold start would
     * show a chat list missing its tail with nothing to say why. */
    if (fputs(line, chat_fp.get()) == EOF || fputc('\n', chat_fp.get()) == EOF)
        chat_bad = 1;
}

void rc_cache::end_chats()
{
    char tmp[MAX_PATH], dest[MAX_PATH];
    int bad;

    if (!chat_fp.valid()) return;
    /* The close is part of the verdict: stdio buffers, so the last write
     * reaches the disk here and nowhere else. release(), not reset(): the
     * guard's own release discards fclose's return, and that return is what
     * tells a full-disk failure apart from a clean close. */
    bad = (fclose(chat_fp.release()) != 0) || chat_bad;
    rc_strf(tmp, sizeof(tmp), "%s\\CHATS.TMP", root);
    rc_strf(dest, sizeof(dest), "%s\\CHATS.TXT", root);
    if (bad) {
        /* Keep the good copy. A stale chat list is a list; a truncated one
         * looks current and is not. */
        rc_log("cache: chat list incomplete, keeping the previous %s", dest);
        DeleteFileA(tmp);
        return;
    }
    DeleteFileA(dest);
    if (!MoveFileA(tmp, dest)) DeleteFileA(tmp);
}

int rc_cache::chats_open()
{
    char path[MAX_PATH];

    chat_fp.reset();
    if (!usable) return 0;
    rc_strf(path, sizeof(path), "%s\\CHATS.TXT", root);
    chat_fp.reset(fopen(path, "rb"));
    return chat_fp.valid() ? 1 : 0;
}

int rc_cache::chats_next(char *out, unsigned cap)
{
    unsigned n;

    if (!chat_fp.valid() || !out || cap < 2) return 0;
    if (!fgets(out, (int)cap, chat_fp.get())) return 0;
    n = (unsigned)strlen(out);
    while (n && (out[n - 1] == '\n' || out[n - 1] == '\r'))
        out[--n] = '\0';
    return 1;
}

void rc_cache::chats_close()
{
    chat_fp.reset();
}
