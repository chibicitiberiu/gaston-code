/* turn.cpp -- Turn state management and frame handlers for Claude turns.
 */
#include "app/turn.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app/reconnect.h"
#include "core/log.h"
#include "core/str.h"

Turn::Turn(HWND hwnd, rc_html *html, rc_statusbar *status,
           rc_cache *cache, ChatList *chats, TurnListener *listener)
    : m_hwnd(hwnd), m_html(html), m_status(status),
      m_cache(cache), m_chats(chats), m_listener(listener),
      m_state(TURN_IDLE), m_active(false), m_startMs(0)
{
}

Turn *Turn::create(HWND hwnd, rc_html *html, rc_statusbar *status,
                   rc_cache *cache, ChatList *chats, TurnListener *listener)
{
    return new Turn(hwnd, html, status, cache, chats, listener);
}

void Turn::thinkShow()
{
    if (m_state != TURN_IDLE) return;
    m_state = TURN_THINKING;
    SetWindowTextA(m_hwnd, RC_TITLE "  -  working...");
    /* The bridge has the message: it started a turn for it. Drop the echo's
     * provisional styling in place -- the block does not move or re-render,
     * it just stops looking unsent. */
    if (m_html) m_html->settle_pending();
}

void Turn::turnReceiving()
{
    if (m_state == TURN_STREAMING) return;
    m_state = TURN_STREAMING;
    SetWindowTextA(m_hwnd, RC_TITLE "  -  receiving...");
}

void Turn::turnDone()
{
    m_state = TURN_IDLE;
    if (m_status) m_status->clear_override();
    SetWindowTextA(m_hwnd, RC_TITLE);
    /* The status line is a statement about right now, so it goes when "right
     * now" stops being true. Leaving "receiving... 41s" under a finished
     * answer made it look as if Claude was still generating. */
    if (m_html) m_html->status("");
}

void Turn::noteInput()
{
    m_active = true;
    m_startMs = GetTickCount();
    thinkShow();
    if (m_listener) m_listener->setBusy(true);
}

void Turn::abort()
{
    m_active = false;
    m_startMs = 0;
    turnDone();
    if (m_listener) m_listener->setBusy(false);
}

void Turn::onTurn(rc_frame *f)
{
    const char *v;
    const char *state = rc_frame_get(f, "state");
    if (state && strcmp(state, "start") == 0) {
        m_active = true;
        if (m_listener) m_listener->setBusy(true);
        m_startMs = GetTickCount();
        if (m_status) m_status->set_pane(2, " Claude is thinking...");
        thinkShow();
    } else {
        turnDone();
        m_active = false;
        if (m_listener) m_listener->setBusy(false);
        /* Now, and only now, re-fetch the list. Sending a message is what
         * makes a conversation the most recent one, so this is where the
         * reordering belongs -- not on merely opening one. */
        if (m_chats) m_chats->request();
        /* state=="error" used to fall into the same branch as a normal
         * end and render identically -- turn_error() (protocol.py) sends
         * `msg` and no `cost_usd`, so "done $?" is what a failed turn
         * looked like. Distinguished now: an error goes to the transcript
         * as an error and the status bar says so, instead of reading like
         * a quiet success. See audit finding 03.50. */
        if (state && strcmp(state, "error") == 0) {
            /* The message is in the BODY, not a header. It used to be a
             * `msg=` header, and the more informative the error the more
             * likely the user got a dropped LINK instead of the text: a
             * multi-line message percent-encodes past RC_MAX_VAL, the guest
             * refuses an over-long header value by design (01.44), and
             * net.cpp treats an unparseable frame as a dead stream. See
             * turn_error() in protocol.py. */
            if (m_html)
                m_html->error((f->body && *f->body)
                              ? f->body
                              : "The turn ended with an error.");
            if (m_status) m_status->set_pane(2, " error");
        } else if (state && strcmp(state, "end") == 0
                   && (v = rc_frame_get(f, "ok")) != NULL
                   && strcmp(v, "0") == 0) {
            /* A TURN THAT CAME BACK NOT-OK IS NOT A QUIET SUCCESS.
             *
             * 03.50 fixed state="error", which is the path an EXCEPTION
             * takes. This is the other one, and it is the one a quota or
             * rate-limit refusal actually takes: the SDK returns normally
             * with ok=0 and nothing streamed, so the turn used to render as
             * `done $?` with an empty transcript -- the user's own report was
             * "i'm just getting interrupted with no message nothing".
             *
             * `subtype` is the SDK's own word for what went wrong and is the
             * only thing here that carries any information, so it is shown
             * rather than swallowed. Empty subtype still gets a sentence:
             * "something failed and we do not know what" is a far better
             * answer than a cost line for a turn that produced nothing. */
            const char *why = rc_frame_get(f, "subtype");
            char line[256];
            rc_strf(line, sizeof(line),
                    "The request did not complete%s%s. Nothing was returned "
                    "-- it may not have run at all.",
                    (why && *why) ? ": " : "", (why && *why) ? why : "");
            if (m_html) m_html->error(line);
            if (m_status) m_status->set_pane(2, " failed");
        } else {
            char buf[96];
            /* Keep the elapsed time. It used to be overwritten by the cost,
             * so the moment a turn ended you lost how long it took -- and
             * "that took 90 seconds" is the more useful of the two when you
             * are deciding whether to ask for something smaller next time. */
            unsigned long secs = m_startMs
                ? (unsigned long)((GetTickCount() - m_startMs) / 1000)
                : 0;
            {
                /* Absent means "leave the gauge alone", not zero. The bridge
                 * omits it when the reading was unavailable, and drawing an
                 * empty pie then would be a confident wrong answer. */
                const char *cp = rc_frame_get(f, "ctx_pct");
                if (cp && *cp && m_status) {
                    int pct = atoi(cp);
                    if (pct >= 0 && pct <= 100) m_status->set_ctx_pct(pct);
                }
            }
            v = rc_frame_get(f, "cost_usd");
            rc_strf(buf, sizeof(buf), " done  %lus  $%s",
                    secs, (v && *v) ? v : "?");
            if (m_status) m_status->set_pane(2, buf);
        }
    }
}

void Turn::onDoc(rc_frame *f)
{
    if (m_html) m_html->set_document(f->body);
    SendMessageA(m_hwnd, WM_SIZE, 0, 0);
    SetTimer(m_hwnd, TIMER_HTMLPOLL, 100, NULL);
}

void Turn::onText(rc_frame *f)
{
    const char *mode = rc_frame_get(f, "mode");
    turnReceiving();
    if (mode && strcmp(mode, "live") == 0) {
        if (m_html) m_html->live(f->body);
    } else {
        if (mode && strcmp(mode, "append") != 0)
            rc_log("TEXT: unrecognized mode=\"%s\" (want \"append\" or "
                   "\"live\"); treating as a finished block", mode);
        if (m_cache) m_cache->add(f->body, f->body_len);
        if (m_html) m_html->block(f->body);
    }
}

void Turn::onTool(rc_frame *f)
{
    (void)f;
    turnReceiving();
}
