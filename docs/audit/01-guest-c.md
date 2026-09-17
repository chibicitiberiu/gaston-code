# Guest audit — `win/`, architecture and code quality

Scope: `win/*.c` and `win/*.h` as of `main.c` 1783 lines, plus `win/Makefile`.
Not the bridge, not the spike, not `probe.c` (a Phase-0 experiment that is no
longer linked into anything).

This is an architecture review. Bugs are reported where they were found, but
the question being answered is: *is the structure right, is it consistent, will
it survive Phases 4–6, and where has the code drifted from what the comments
and the plan say it does.*

---

## Top findings

1. **`main.c` is nine modules in a trench coat.** Thirteen distinct
   responsibilities share one file, ~60 file-static globals and one 340-line
   `if/else` chain. The seam is already drawn for you — by frame type family —
   and four of the nine come out almost cleanly. (§1)

2. **The HTML view is an API, not a view.** Nine `rc_html_append` sites in
   `main.c`, three different markup conventions between them, two of which
   inject unescaped text into `insertAdjacentHTML`. Scroll policy is decided
   per call site: 2 of 9 scroll unconditionally, 1 scrolls conditionally,
   6 never scroll, and `g_cfg.autoscroll` — which is loaded from the INI and
   round-trip-tested by `inicheck.exe` — is never read by anything. (§2)

3. **The bar is a correct state machine written as four uncoordinated
   functions.** The PERM/HALT collision fix is right, but it is enforced by
   five `if`s spread over `bar_hide`, `perm_hide`, `halt_show`, `perm_show`
   and `layout`, with `g_halt_id[0]` doing double duty as "the halt on screen"
   and "the halt waiting behind the permission bar". Two holes remain today:
   **nothing takes the bar down on disconnect or on session switch**, which
   leaves Send permanently disabled. Phase 6's extra states will land in five
   non-adjacent places. (§3)

4. **`net.c` contains the tool router.** The reader thread hard-codes
   `"EXECKILL"`, `"EXEC"` and `strncmp(type,"SYNC",4)` (`net.c:180–222`), so the
   transport layer knows the tool vocabulary and `net.c ↔ rpc.c` is a real
   include cycle. Every Phase-6 tool adds a branch here. (§5)

5. **`_snprintf` has three idioms across 86 call sites**, and the two unsafe
   ones are the majority: 39 sites use `sizeof(x)-1` + explicit NUL, **15 use
   `sizeof(x)-1` and no NUL**, 32 use the full size with no NUL. Two of the
   15 are reachable from the wire (`g_put.rel`, `g_net.sess`). One helper
   removes the whole class. (§4.1)

6. **`rc_net_send`'s return value is checked at 3 of ~20 call sites** — and the
   long comment explaining *why it must be checked* sits on `ID_PERM_ALLOW`'s
   Continue button, two lines above `perm_reply()`, which does not check. Press
   Allow on a dead link and the bar stays up with no feedback. (§4.2)

7. **Dead and vestigial**: `n->rpcq` (written 5×, read 0×), `n->next_seq`
   (written, never sent — the resume path does not exist), `rc_sync_init()`
   (empty, uncalled), `rc_html_ready()`, `rc_log_path()`, `ID_TRANSCRIPT`,
   `RC_USER_RULE`, `cfg->font_size`, `cfg->autoscroll`, and
   `label_width_px()` + `g_chat_widest` — ~25 lines and one GDI round trip per
   session row whose only consumer is a log line. (§6)

8. **The comment on `rc_net_send`'s build buffer flatly contradicts the next
   line of code** (`net.c:592–596`): it claims there is no shared build buffer
   any more; the buffer is `static char build[RC_MAX_FRAME + 1024]`. (§7.1)

9. **`make check` audits the wrong binary.** `CLAUDE.md` says "`make check`
   audits imports"; the top-level target runs `$(MAKE) -C spike check`, which
   audits `spike/hello98.exe`. The real audit is `make -C win check`. (§7.4)

10. **Phase 5 and Phase 6 have no guest-side outbound path.** Attachments and
    screenshot strips are guest→bridge bulk transfers initiated locally. Today
    the only chunked outbound path is `do_get()` inside `syncrpc.c`, driven by
    a bridge `SYNCGET`, and `rc_rpc_submit`'s family check hard-codes exactly
    two id prefixes (`'x'`, `'s'`). (§8)

---

## 1. `main.c`: what lives there, and what should move

`main.c` is 1783 lines holding thirteen responsibilities. Listed with their
globals and their frame types, because that is where the seams are.

| # | Responsibility | Lines | Owns | Frame types |
|---|---|---|---|---|
| 1 | App globals / window handles | 70–200 | `g_cfg`, `g_net`, `g_html`, 18 `HWND`s, 2 `HFONT`s | — |
| 2 | Status bar + transfer accounting | 228–284 | `g_rx_*`, `g_tx_*`, `g_xfer_ms` | (all, via `render_frame`'s prelude) |
| 3 | Pagination UI | 286–322, 773–799 | `g_view`, `g_total`, `g_is_live`, `g_loading_page` | `PAGESTART` `PAGEEND` `PAGESEALED` `ANCHOR` |
| 4 | Model/Effort menus | 342–377, 647–687 | `g_menu_model/effort`, `g_opt_model/effort`, `g_n_*`, `g_opts_open` | `OPTION` `OPTIONSEND` |
| 5 | Chat list | 379–415, 688–772, 890–974, 1113–1125, `WM_DRAWITEM`/`WM_MEASUREITEM` | `g_chat_ids`, `g_chat_is_header`, `g_chat_group`, `g_chat_current`, `g_chat_count`, `g_chat_widest`, `g_chats_oldproc` | `SESSIONS` `SESSION` `SESSIONSEND` `SWITCHED` |
| 6 | Permission / halt bar | 417–548, 805–861, `ID_PERM_*` | `g_bar_mode`, `g_perm_id`, `g_perm_pending`, `g_halt_id`, `g_halt_text` | `PERMREQ` `PERMSTATE` `PERMWAIT` `PERMEND` `HALT` `HALTEND` |
| 7 | Transcript markup | 1002–1060, + 9 append sites | `esc_html`, `echo_user_block`, `RC_USER_RULE` | `TEXT` `NOTICE` `ERROR` |
| 8 | Compose / send | 1062–1141 | `RC_INPUT_MAX`, `g_input_oldproc` | `INPUT` `INTERRUPT` (outbound) |
| 9 | Turn / "thinking" state | 976–1000 | `g_think_mark`, `g_turn_start_ms`, `g_streaming`, `g_turn_active` | `TURN` `TOOL` |
| 10 | Frame dispatch | 550–888 | — | all 23 |
| 11 | Connection lifecycle | 1349–1385, `WM_RC_STATE` | `g_backoff_ms`, `g_retry_noted` | `READY` |
| 12 | Layout / control creation | 1143–1347 | every geometry constant | — |
| 13 | `WinMain` / process setup | 1661–1783 | single-instance mutex, OLE, accelerators | — |

### 1.1 What should move, concretely

**`chatlist.c` (~400 lines, the cleanest cut).** Takes `chats_request`,
`chats_switch`, `label_width_px`, `chats_text_width`, `fit_label`,
`chats_proc`, the `WM_DRAWITEM`/`WM_MEASUREITEM` bodies (`main.c:1412–1493`),
the four `SESSION*`/`SWITCHED` branches, and all seven `g_chat_*` globals.
Nothing outside those lines touches a `g_chat_*` global — verified. The seam:

```c
/* chatlist.h */
void rc_chats_create(HWND parent, HINSTANCE inst, HFONT normal, HFONT bold);
void rc_chats_move(int x, int y, int w, int h);
int  rc_chats_frame(rc_frame *f);   /* non-zero: handled */
int  rc_chats_draw(DRAWITEMSTRUCT *dis);
int  rc_chats_measure(MEASUREITEMSTRUCT *mis);
void rc_chats_open_selected(void);  /* the WM_RC_OPENCHAT / dblclk / Enter path */
```

Its one dependency out is `rc_net_send` for `GETSESSIONS`/`SWITCH`, and one
callback in — "a switch is about to happen, is a turn running, may I?" — which
today is `chats_switch` reading `g_turn_active` and calling `MessageBoxA`
directly (`main.c:396–407`). Pass that as a `int (*confirm)(const char *why)`
so the module does not own a policy question.

**`optmenu.c` (~90 lines).** `build_menu`'s Model/Effort halves, `clear_popup`,
the `OPTION`/`OPTIONSEND` branches, the `IDM_MODEL0`/`IDM_EFFORT0` range
dispatch in `WM_COMMAND`'s `default:`. This is the module that most obviously
wants to be one: the id-range arithmetic at `main.c:1631–1642` and the array
fill at `main.c:658–681` must agree about `MAX_OPTS` and the base constants,
and they are 970 lines apart.

**`pagebar.c` (~100 lines).** `page_update_ui`, `page_goto`, `page_goto_n`, the
three `PAGE*` branches, the six button ids, and the page-bar strip of
`layout()` (`main.c:1170–1178`).

**`xferstat.c` (~70 lines).** `fmt_bytes`, the progress accounting prelude of
`render_frame` (`main.c:552–572`), and `status_refresh`'s middle pane. Today
that prelude runs before the type is even looked at and mutates four globals;
it belongs behind `rc_xfer_note(f)` / `rc_xfer_text(char *out, unsigned cap)`.

**`bar.c`** — see §3, which argues for more than a move.

**`view` methods into `htmlview.c`** — see §2.

That leaves `main.c` at roughly 600 lines: globals, `layout`,
`create_children`, `wnd_proc`, `try_connect`, compose/send, turn state, and a
dispatch table.

### 1.2 `render_frame` should be a table, not a chain

`main.c:550–888` is a 340-line `if/else` over 23 frame types (`main.c:575, 592,
606, 621, 626, 647, 684, 688, 696, 736, 746, 773, 788, 793, 800, 805, 815, 827,
844, 851, 862, 871`). Consequence, in order of severity:

- It is the reason the file cannot be split: every module's frame handling is
  interleaved in one function body, so moving `chatlist.c` out today means
  leaving four `else if` stubs behind that call into it.
- It costs up to 22 `strcmp`s per frame on a 200MHz Pentium. `TEXT` is third,
  so the hot path is fine; `NOTICE` is 22nd and `PERMEND` 21st.
- A typo in a type string is a silent fall-through to `rc_log("unhandled frame
  type")`, which is exactly the failure mode `net.c:203–210` records for the
  missing `SYNC` route: *"a sync layer that looks wired up on both sides and
  transfers nothing, with no error on either."* The same trap is still armed,
  one layer up.

Shape of the fix — a table each module contributes to:

```c
typedef void (*rc_frame_fn)(rc_frame *f);
static const struct { const char *type; rc_frame_fn fn; } g_dispatch[] = {
    { "TEXT",       on_text      },
    { "SESSION",    rc_chats_on_session },
    ...
};
```

A linear walk of that table is no slower than the chain and is one place to
read. With the table in hand, the "unhandled" branch can also assert at startup
that every type the bridge can send has an entry — `bridge/protocol.py` is the
list.

---

## 2. The htmlview seam

### 2.1 Every markup site in `main.c`

| # | Line | Context | Markup built | Escaped? | Scroll |
|---|---|---|---|---|---|
| 1 | 589 | `READY` | `<div class="notice">Connected to bridge.</div>` | literal | none |
| 2 | 618 | `TEXT` non-live | `f->body` (bridge-built) | bridge | **`if (at_bottom)`** |
| 3 | 784 | `PAGESTART` continued | `<div class="notice">continued from the previous page</div>` | literal | none (`PAGEEND` scrolls later) |
| 4 | 868 | `ERROR` | `"\r\n[%s] %s\r\n"` — **no div, no class, no escaping** | **no** | none |
| 5 | 878 | `NOTICE` | `esc_html` → `<div class="notice">%s</div>` | yes | none |
| 6 | 1055 | `echo_user_block` | `<div class="you">%s</div>` | yes | **unconditional** |
| 7 | 1076 | `do_send` too long | `<div class="err">…</div>` | literal | **unconditional** |
| 8 | 1099 | `do_send` not connected | `<div class="err">Not connected.</div>` | literal | **none** |
| 9 | 1374 | `try_connect` | `"Cannot reach %s:%d - retrying\r\n"` — **no div, no class** | **no** | none |

Plus `rc_html_live` (616), `rc_html_clear` (781) and the two bare
`rc_html_scroll_bottom` at 789 and 1056/1081.

### 2.2 What is actually wrong here

**Scroll policy is accidental.** `rc_html_at_bottom` is consulted at exactly one
of nine sites (`main.c:613`). So: a streamed answer respects where the reader
has scrolled to; an error message, a "too long" refusal and the echo of your own
line yank the view to the bottom regardless; and a `NOTICE`, a "Connected to
bridge", a "Not connected" and a "Cannot reach … retrying" appear off-screen
with no scroll at all if you have paged up. Nobody decided this. Sites 7 and 8
are eleven lines apart in the same function and disagree.

**`g_cfg.autoscroll` is dead.** `ini.c:24` loads it, `ini.c:52` saves it,
`inicheck.c:67` asserts the round trip, `retroclaude.ini:7` sets it — and no
line of code ever reads `cfg.autoscroll`. A Phase 4 settings dialog that exposes
it would ship a checkbox that does nothing.

**Two sites feed non-HTML to an HTML sink.** Sites 4 and 9 build `\r\n`-delimited
plain text and hand it to `rc_html_append`, which calls
`IHTMLElement_insertAdjacentHTML` (`htmlview.c:399`). The `\r\n`s render as
nothing, so `[bad_session] no id` appears glued to the previous block with no
styling. Worse, site 4 interpolates `rc_frame_get(f,"msg")` — a bridge-supplied
string — unescaped. `bridge/protocol.py:203` builds `ERROR` from arbitrary
exception text; a `<` in it silently eats the rest of the transcript block. The
guest's own comment at `main.c:1005` says *"The guest builds exactly one piece
of markup itself — the echo of the user's own line — and it still has to
escape"*. That statement is false by four sites, and two of them do not escape.

**The guest re-implements `bridge/html.py`.** `html.py:114` is
`notice()`, `html.py:118` is `error()`, `html.py:103` is `user_turn()`. The
guest hand-rebuilds all three. That is a small but direct breach of "the guest
transforms nothing", and it has already drifted: `main.c:1019` says the echo
*"must match bridge/markdown.py user_block()"* — the module is `bridge/html.py`
(the `markdown.py` referenced is a `.bak`), and the two **do not match**.
`html.py:102` does `text.strip().splitlines()`; `esc_html` (`main.c:1011`) does
not strip. Send a message with a trailing blank line and the locally-echoed
block and the one the paginator sealed differ — which is exactly the divergence
the comment exists to prevent, visible the moment you page back.

**Silent truncation at site 5.** `char esc[1024], div[1200]` on the `wnd_proc`
stack; `esc_html` stops at `o + 8 < cap` (`main.c:1013`). `echo_user_block`
already learned this lesson and documents it at length (`main.c:1034–1042`) —
the `NOTICE` path never got the fix.

### 2.3 The class the view should be

`htmlview.h` today is a byte pipe: `append`, `live`, `clear`, `scroll_bottom`,
`at_bottom`. Give it the transcript's vocabulary instead, and let it own both
the markup and the scroll decision:

```c
/* htmlview.h */
void rc_html_set_autoscroll(rc_html *v, int on);   /* from cfg.autoscroll */

/* Bridge-authored HTML, appended verbatim. Follows if the reader is at the
 * bottom; never yanks. */
void rc_html_append_block(rc_html *v, const char *html);

/* Guest-authored. Each escapes its argument, wraps it in the agreed class,
 * and always follows -- these are answers to something the user just did. */
void rc_html_append_notice(rc_html *v, const char *text);
void rc_html_append_error (rc_html *v, const char *text);
void rc_html_append_user  (rc_html *v, const char *text);

void rc_html_live(rc_html *v, const char *html);
void rc_html_clear(rc_html *v);
```

`rc_html_at_bottom` and `rc_html_scroll_bottom` become internal. The rule
becomes one sentence each, stated in the header: *bridge blocks follow, guest
messages insist*, and `set_autoscroll(0)` turns following off for both.

What that does to the call sites:

- Site 1 → `rc_html_append_notice(g_html, "Connected to bridge.")`
- Site 2 → `rc_html_append_block(g_html, f->body)` — the `at_bottom`/`scroll`
  dance at 613/619 disappears
- Site 3 → `rc_html_append_notice(g_html, "continued from the previous page")`
- Site 4 → `rc_html_append_error(g_html, msg)` — escaping arrives for free
- Site 5 → `rc_html_append_notice(g_html, f->body)` — the 1024-byte stack
  buffer and the truncation go away, because the escape can size from the input
  inside the view (where `queue_add`'s growable buffer already lives,
  `htmlview.c:248`)
- Site 6 → `rc_html_append_user(g_html, buf)` — the two `HeapAlloc`s at
  `main.c:1046–1053` collapse into the view, and `esc_html`, `echo_user_block`
  and `RC_USER_RULE` leave `main.c` entirely
- Sites 7, 8 → `rc_html_append_error(...)`, and they now behave identically
- Site 9 → `rc_html_append_notice(...)`

Net: `main.c` loses ~80 lines, `esc_html`, one 20KB heap round trip per message,
and every scroll decision. `htmlview.c` gains ~50 lines and becomes the only
file in `win/` that knows what a CSS class is — which is what `htmlview.h:9`
already claims it is.

One further step worth considering: the three `append_*` builders are a mirror
of `bridge/html.py`. If the bridge sent `NOTICE`/`ERROR` as finished HTML the
way it sends `TEXT` (it already has `H.notice`/`H.error` and already routes
most notices through `_write_block`), the guest would need only
`append_block`/`append_user` and the mirror problem is gone for good. Site 5 is
the only `NOTICE` the bridge sends (`server.py:2043`) and it is a fixed string.

---

## 3. The bar state machine

### 3.1 The current machine, extracted

State: `g_bar_mode ∈ {BAR_NONE, BAR_PERM, BAR_HALT}` plus four satellite
variables — `g_perm_pending`, `g_perm_id[80]`, `g_halt_id[80]`,
`g_halt_text[256]`.

| From | Event | Function | To | Notes |
|---|---|---|---|---|
| any | `PERMREQ`/`PERMSTATE` | `perm_show` 511 | `BAR_PERM` | unconditional; preserves `g_halt_id` |
| `NONE` | `HALT` | `halt_show` 474 | `BAR_HALT` | |
| `PERM` | `HALT` | `halt_show` 474 | `PERM` | halt *remembered* in `g_halt_id`/`g_halt_text`, early return at 486 |
| `HALT` | `HALT` (new id) | `halt_show` 474 | `HALT` | **overwrites `g_halt_id` silently** |
| `PERM` | `PERMEND` (id matches) | `perm_hide` 463 | `HALT` if `g_halt_id[0]`, else `NONE` | |
| `HALT` | `HALTEND` (id matches) | `halt_hide` 505 | `NONE` | |
| `PERM` | `HALTEND` (id matches remembered) | 833–841 | `PERM` | clears the remembered halt |
| any | `WM_RC_STATE` disconnect | 1519 | **unchanged** | **hole** |
| any | `SWITCHED` / `NEWSESSION` | 746 / 1548 | **unchanged** | **hole** |

The design decision — a permission takes the bar because it has a deadline; a
halt waits behind it — is right, and the 30-line comment at `main.c:88–117`
earns its place. The problem is not the decision. It is that the decision is
implemented as five conditionals in four functions, with no single place that
can be read to know what the bar is doing.

### 3.2 Concrete defects that remain

**(a) Disconnect leaves the bar up and Send dead.** `WM_RC_STATE` on
`RC_FAILED`/`RC_DISCONNECTED` (`main.c:1517–1522`) calls `ui_set_busy(0)` and
nothing else. `ui_set_busy` computes
`EnableWindow(g_send, (busy || g_perm_pending) ? FALSE : TRUE)` (`main.c:432`),
so with `g_perm_pending` still 1 **Send stays disabled across the whole
outage and after the reconnect.** It is re-enabled only by a `PERMEND` carrying
that exact id. The bridge re-issues live requests as `PERMSTATE` on reconnect
(`main.c:805–813`), but a request the bridge resolved *during* the outage
produces no frame at all, so nothing ever clears it. The bar sits there
offering Allow/Deny for a tool call that no longer exists, and pressing Allow
sends a `PERMREPLY` with a stale id.

**(b) Session switch leaves the bar up.** `SWITCHED` (`main.c:746`) rebuilds the
transcript, resets `g_view`/`g_total` and repaints the chat list, but touches
none of `g_bar_mode`, `g_perm_*` or `g_halt_*`. A permission bar raised by the
previous session survives into the new one.

Both are one function — `bar_hide()` is already written and already does the
right thing (`main.c:443–459`). It needs calling from `WM_RC_STATE`'s failure
branch and from `SWITCHED`. Note that `bar_hide` clears `g_halt_id` only under
`BAR_HALT`, so on disconnect the *remembered* halt survives; for a disconnect
that is arguably correct (the bridge still holds it and will re-raise), but it
needs to be a decision rather than an accident, which is precisely the point.

**(c) `g_halt_id[0]` means two things.** At `main.c:470` it means "a halt is
waiting behind the permission bar"; at `main.c:444–447` it means "a halt is on
screen". The disambiguator is `g_bar_mode`, read three functions away. That is
why the `HALTEND` branch needs the explicit `else` at `main.c:836–841` with a
four-line comment explaining which of the two it is clearing.

**(d) `perm_show` and `halt_show` are unguarded against their own state.** A
second `PERMREQ` while one is showing replaces `g_perm_id` and the first is
never answerable. A second `HALT` does the same. Both are safe *today* only
because the bridge serialises — the guest's own comment at `main.c:848` records
this ("Only the queue depth changed; the request is the same one"). It is an
invariant held by the other side of the wire with nothing on this side
checking it.

**(e) `ui_set_busy` is a setter and a refresher.** `bar_hide` calls
`ui_set_busy(g_turn_active)` (`main.c:458`), i.e. `g_turn_active = g_turn_active`
followed by a recompute. Split it: `ui_set_busy(int)` sets the turn flag,
`ui_refresh_enables(void)` recomputes from `(g_turn_active, g_bar_mode)`.

### 3.3 Make it explicit before Phase 6

Phase 6 adds bar states (a screenshot in progress, a `click`/`send_keys`
confirmation are the obvious candidates). Each new state today requires edits
in five non-adjacent places:

- the `BAR_*` enum (`main.c:120–122`)
- a `*_show` function
- `bar_hide`'s "clear only what THIS bar owned" conditional (`main.c:446`)
- the `ID_PERM_ALLOW` dispatch (`main.c:1526–1544`) — one button, now three
  meanings
- `layout()`'s two branches (`main.c:1156` for the height,
  `main.c:1197` for which buttons get screen space)

None of the five references the others. The shape that fixes this is a table of
bar *descriptors* and one `bar_set(const bar_state *s, const char *id, const
char *text)`:

```c
typedef struct {
    const char *primary;      /* "Allow" / "Continue" / ... */
    const char *secondary;    /* NULL = hidden */
    const char *tertiary;
    void (*on_primary)(const char *id);
    void (*on_secondary)(const char *id);
    void (*on_tertiary)(const char *id);
    int  blocks_send;
    int  priority;            /* who wins the bar; halt < perm today */
} rc_bar_state;
```

`bar_set` becomes the only writer of `g_bar_mode`, it enforces `priority`
(replacing `halt_show`'s early return at 486 and `perm_show`'s implicit win),
it parks the loser on a one-deep pending slot (replacing the overloaded
`g_halt_id`), and `layout` reads `primary/secondary/tertiary != NULL` instead of
switching on the mode. `ID_PERM_ALLOW`'s `if (g_bar_mode == BAR_HALT)` becomes
`g_bar->on_primary(g_bar_id)`. Adding a Phase 6 state is then one table row and
one callback, and `bar_hide()` on disconnect/switch is a single call that
cannot forget a field.

---

## 4. Duplication and inconsistency

### 4.1 The `_snprintf` idiom — three of them, 86 sites

Measured across `main.c net.c rpc.c sync.c syncrpc.c exec.c ini.c log.c
htmlview.c frameproto.c`:

| Form | Count | Safe? |
|---|---|---|
| `_snprintf(x, sizeof(x) - 1, ...)` + explicit `x[sizeof(x)-1] = '\0'` | 39 | yes |
| `_snprintf(x, sizeof(x) - 1, ...)`, no NUL | **15** | **no** |
| `_snprintf(x, sizeof(x), ...)`, no NUL | 32 | **no** |
| `_snprintf(x, cap + 63, ...)` + NUL | 1 | odd but safe |

Microsoft's `_snprintf` (which Open Watcom follows) does not NUL-terminate when
the output exactly fills the count. So the second form leaves `x[sizeof(x)-1]`
at whatever was there before, and the third can fill the buffer entirely.

The 15 in the second group: `main.c:255, 258, 263, 266, 577, 750, 864, 1584,
1597`; `net.c:332, 334, 404`; `syncrpc.c:109, 223, 224`.

Two are reachable from the wire and are real latent bugs, not style:

- **`syncrpc.c:224`** — `_snprintf(g_put.rel, sizeof(g_put.rel) - 1, "%.*s",
  (int)rn, rel)` where `rn` is `atoi` of a wire-supplied length prefix
  (`syncrpc.c:215`). A path of ≥ 259 bytes fills `g_put.rel[MAX_PATH]` with no
  terminator; `g_put` is a file-static struct, so it holds whatever the previous
  transfer left. `rc_sync_begin_write` then calls `strlen(rel)` (`sync.c:391`)
  on an unterminated buffer. The module is otherwise scrupulous about exactly
  this class of failure — `sync.h:21–37` is 17 lines on what a truncated
  working name costs.
- **`main.c:577` / `main.c:750`** — `g_net.sess` from a `READY`/`SWITCHED`
  header, same shape, and `sess` is then sent back in `HELLO` (`net.c:441`).

One helper removes all 47:

```c
/* win/str.h -- the only way a string is copied or formatted into a fixed
 * buffer in this codebase. Always terminates. */
#define RC_FMT(dst, ...)  rc_fmt((dst), sizeof(dst), __VA_ARGS__)
void rc_fmt(char *dst, unsigned cap, const char *fmt, ...);
void rc_cpy(char *dst, unsigned cap, const char *src);   /* the ~35 "%s" cases */
```

Note C89: variadic macros are C99. Open Watcom accepts them, but if strict C89
matters, `rc_fmt(dst, sizeof(dst), ...)` spelled out is still one idiom instead
of three, and a trivial grep can assert that the second argument is always a
`sizeof` with no `- 1`. That grep belongs in `make -C win check` next to the
CMOV gate — mechanical, like `inicheck.exe`, and for the same reason: two bugs
of the "somebody forgot" shape have already shipped here
(`ini.c:25–30`, `win/Makefile:65–67`).

The `+ 4` variants of the same idiom also exist: `sync.c` and `syncrpc.c` both
build `root + '\' + rel` with `_snprintf` then walk the tail converting `/` to
`\` — three times in `syncrpc.c` alone (`289–294`, `357–359`, `416–418`) and
once as `join()` in `sync.c:231`. `join()` is the correct one and is `static`;
`syncrpc.c` open-codes it three times because it cannot see it. Export it.

### 4.2 Error-handling conventions

**`rc_net_send` returns `-1` on a closed link, a full queue or an oversized
frame. It is checked at 3 of ~20 call sites.**

Checked: `main.c:1091` (`INPUT`), `main.c:1538` (`RESUME`), `rpc.c:141`
(`EXECOUT`), `syncrpc.c:437` (`SYNCDAT`).

Unchecked: `page_goto` (303), `chats_request` (381), `chats_switch` (414),
`do_stop` (1106), `perm_reply` (541), `IDM_NEW` (1557), `SETMODEL` (1635),
`SETEFFORT` (1641), `rpc.c:190, 226, 241, 375`, `syncrpc.c:52, 104, 151, 190,
343, 383, 411, 455`.

The inconsistency is not uniform sloppiness — it is *localised*. `main.c:1531`
carries a seven-line comment explaining exactly why an unchecked send is
dangerous here:

> `rc_net_send` returns -1 on a closed link, a full queue or an oversized frame,
> and hiding regardless meant a press that never left the machine looked
> answered

Ten lines later, `perm_reply` (`main.c:535–548`) sends `PERMREPLY` and ignores
the result. The same bar, the other three buttons, the identical failure: press
Allow with the link down, nothing leaves the machine, the bar stays up (it comes
down on `PERMEND`, by design), and there is no "Not sent" message because that
string only exists on the Continue path. The fix was applied to one button.

`chats_switch` is the other visible one: it sets the title to `"loading..."` and
the status pane to `" loading conversation..."` *before* the send
(`main.c:411–414`). If the send fails, both stay until the next `SWITCHED`,
which will never arrive.

The convention worth writing down and enforcing: **a send triggered by a user
gesture must be checked and must undo its own optimistic UI; a send on a
background path logs and continues.** That is three rules' worth of behaviour
currently expressed as one comment on one button.

Other conventions that are inconsistent:

- **`CreateWindowExA` returns are never checked** for the 18 controls in
  `create_children` (`main.c:1214–1347`) — only `rc_html_create` is
  (`main.c:1234`). On a box whose 16-bit USER heap is a fixed 64KB, which this
  codebase cites as a reason three separate times (`main.c:37, 91, 1290`), a
  failed `CreateWindow` is a live possibility, and every subsequent
  `SendMessageA(NULL, ...)` is a silent no-op. At minimum log which one failed;
  the log is the only diagnostic on this platform (`log.h:1–8`).
- **`HeapAlloc` is checked everywhere** — genuinely consistent, good.
- **`rc_frame_get` NULL handling** alternates between the ternary-at-use idiom
  (`main.c:583, 864–866`) and a local with a guard (`main.c:648–651`). Cosmetic,
  but `main.c:583` calls `rc_frame_get(f, "model")` twice in one `rc_log`.

### 4.3 Naming

- **`rc_sync_*` spans two modules.** `rc_sync_scan/stat_file/contained/
  begin_write/commit/recover/is_working_name/init` are in `sync.c`;
  `rc_sync_set_root/root/handle` are in `syncrpc.c`. `main.c` must include both
  headers to configure one string (`main.c:20–21`, used at `main.c:1700–1706`).
  Rename the frame-handling half `rc_syncrpc_*`, or move `set_root`/`root` into
  `sync.c` where the root conceptually belongs.
- **`perm_*` vs `bar_*` vs `halt_*`** for one control group: `perm_layout()`
  lays out the *bar*, not the permission bar (`main.c:435`); `bar_hide()` hides
  either. Pick `bar_` for the shared machinery.
- **`WM_APP` offsets are declared in three styles**: `WM_RC_FRAME`/`WM_RC_STATE`
  in `net.h:35–36`, `WM_RC_OPENCHAT` at `main.c:1119` (mid-file, 400 lines from
  its only use), and a bare `WM_APP + 99` at `main.c:1393` and `main.c:1396`.
  Phase 6 will add cross-thread posts; one block of `#define`s at the top of
  `main.c` with a comment that `WM_APP+1/+2` are net's.
- **`g_think_mark` is a `long` holding only `-1` or `1`** (`main.c:179`), a
  leftover of the RichEdit era when it was a character offset. It is now a
  boolean spelled backwards; with `g_streaming` (`main.c:185`) it is two
  booleans encoding three states (idle / thinking / receiving). One `enum
  { TURN_IDLE, TURN_THINKING, TURN_STREAMING }` is clearer and removes the
  `think_hide()` name, which no longer hides anything (`main.c:987–992`).

---

## 5. Module boundaries and dependencies

Include graph (`.c` → headers it includes, excluding system and `miniz`):

```
main.c      -> frameproto ini log net htmlview syncrpc sync
net.c       -> net caps log rpc exec
rpc.c       -> rpc exec syncrpc sync log
syncrpc.c   -> syncrpc sync log      (syncrpc.h -> net.h)
sync.c      -> sync log
exec.c      -> exec log
ini.c       -> ini log
htmlview.c  -> htmlview log
frameproto.c-> frameproto
```

`log`, `ini`, `htmlview`, `frameproto`, `exec` and `sync` are clean leaves.
Three problems above that line:

### 5.1 `net.c ↔ rpc.c` is a cycle, and the transport owns the tool vocabulary

`net.c` includes `rpc.h`; `rpc.h` includes `net.h`; `rpc.c` calls
`rc_net_send`. The cycle is structural, not accidental — it exists because the
**reader thread routes by frame type**:

- `net.c:180` — `if (strcmp(f->type, "EXECKILL") == 0) { ... rc_exec_cancel(); }`
- `net.c:190` — `if (strcmp(f->type, "EXEC") == 0) { ... rc_rpc_submit(f); }`
- `net.c:212` — `if (strncmp(f->type, "SYNC", 4) == 0) { ... }`

So `net.c` — whose job is "bytes on a socket, framing, threads" — knows what an
EXEC is, knows that cancelling one goes through `exec.c` rather than `rpc.c`,
and knows that `SYNC*` frames need `rc_rpc_syncq_full()` backpressure while
`EXEC` does not. Every Phase 6 tool (`screenshot`, `click`, `send_keys`,
`list_windows`) adds a branch here, in the module the standing rules say should
be the most OS- and feature-neutral thing in `win/`.

The seam: give `rpc.c` a router and let `net.c` ask it a yes/no question.

```c
/* rpc.h */
/* Does the worker want this frame? If it returns non-zero it has TAKEN
 * ownership -- queued it, answered it, or freed it. */
int rc_rpc_route(rc_net *n, rc_frame *f);
```

`net.c:180–222` collapses to:

```c
if (rc_rpc_route(n, f)) continue;
```

`net.c` then includes neither `rpc.h` nor `exec.h`, the cycle becomes a plain
`rpc → net` dependency, and the `EXECKILL`-on-the-reader-thread decision (which
is correct, and well argued at `net.c:182–187`) moves next to the code it is
about. `rc_rpc_syncq_full()` stops being public — the wait belongs inside
`rc_rpc_route`.

### 5.2 `main.c` maintains `net.c`'s ownership invariant

`net.h:9–21` documents a three-way frame-ownership contract and calls it "not
negotiable". Half of it is implemented in `main.c`:

```c
case WM_RC_FRAME: {                     /* main.c:1504 */
    rc_frame *f = (rc_frame *)lp;
    render_frame(f);
    rc_frame_free(f);
    HeapFree(GetProcessHeap(), 0, f);
    InterlockedDecrement(&g_net.inflight);
```

`main.c` therefore has to know that a frame is two allocations, that the second
is a raw `GetProcessHeap()` block, and that `inflight` is the backpressure gate
the reader spins on at `net.c:224`. Get the order wrong or forget the decrement
and the reader wedges at `RC_MAX_INFLIGHT` with no symptom other than a stalled
transcript. One function in `net.c` — `void rc_net_frame_done(rc_net *n,
rc_frame *f)` — puts the whole contract in the module whose header documents it.

Related: `main.c` writes `g_net.sess` directly at `577` and `750` and
`g_net.next_seq` at `579`. The session id is net-layer state (it is sent in
`HELLO`, `net.c:441`) being set by the UI thread from a frame.

### 5.3 One configuration string, three modules

`cfg.sync_root` is read in `main.c` (`1700`), stored in `syncrpc.c`'s `g_root`
(`syncrpc.c:17`) via `rc_sync_set_root`, and read back by `rpc.c`'s
`rpc_watch_start` (`rpc.c:102`) and `rpc_maybe_nudge` (`rpc.c:93`) to decide
whether to start a directory watcher. `rc_sync_recover` in `sync.c` takes the
root as a parameter. So the same string is a global in one module, a parameter
in another, and a dependency of a third. `rpc.c` reaching into `syncrpc.c`'s
state to configure a `kernel32` watcher is the odd one: the watcher is a *sync*
concern that happens to be driven from the worker's wait loop.

### 5.4 Globals that should not be global

Every worker-side module is a hard singleton. Listing them because §8 depends on
it:

| Module | Singleton state |
|---|---|
| `net.c` | `g_inf`, `g_inf_ready` (one inflate stream), `static char build[RC_MAX_FRAME + 1024]` inside `rc_net_send` |
| `rpc.c` | `g_thread`, `g_wake`, `g_lock`, `g_running`, `g_queued`, `g_net`, `g_syncq[24]`, `g_watch`, `p_first_change`… |
| `syncrpc.c` | `g_root`, `g_put`, `g_put_failed` |
| `exec.c` | `g_chunk[8192]`, `g_cancel`, `g_child`, `g_child_lock` |
| `log.c` | `g_fh`, `g_lock`, `g_path` |

`rc_net` is a struct precisely so there can be more than one — `netcheck.c` has
its own — but `rpc.c:45` holds a single `rc_net *g_net`, so a second `rc_net`
would silently share the first's worker. `rc_rpc_start` takes `rc_net *` and
then `return 0`s early if `g_thread` is already set (`rpc.c:338`) **before**
assigning `g_net` (`rpc.c:339`) — so on a reconnect where the worker survived,
`g_net` is stale. It happens to be the same address today because `main.c`'s
`g_net` is itself a global. That is a latent trap rather than a live bug, and
it is exactly the kind Phase 4's "multiple clients" would spring.

`log.c` is fine as a singleton. The rest should at minimum take their state
through the `rc_net *` they are already handed.

---

## 6. Dead and vestigial code

Confirmed by grep across `win/` (excluding `.bak`, `probe.c`, `htmlspike.c`):

**Fields written and never read**

- `net.h:76` `volatile LONG rpcq` — incremented/decremented at `net.c:192, 194,
  216, 218`, `rpc.c:248`, zeroed at `net.c:341`. **Never read.** Its sibling
  `inflight` is read at `net.c:224` as the backpressure gate; `rpcq` has no
  such consumer because the sync FIFO uses `rc_rpc_syncq_full()` instead. Three
  header comments (`net.h:13–17`, `rpc.h:18`, `rpc.c:10`) describe maintaining
  it as a correctness requirement. Either give it a consumer (a second
  backpressure gate, or the `log_mem` line) or delete it and the three
  comments — right now it is five interlocked operations per frame and a
  documented invariant protecting a number nobody looks at.
- `net.h:80` `unsigned next_seq` — set at `main.c:578–579` from `READY`.
  Commented "resume point". Nothing sends `from=`; `main.c:807` says so
  explicitly ("The guest never sends `from=`, so the bridge's replay buffer
  cannot bring the bar back by itself"). The bridge's replay machinery
  (`bridge/replay.py`) exists and `bridge/fakegui.py:207` uses it. So this is
  not dead-by-accident, it is **an unimplemented feature with its state
  plumbing already in place**. Worth a line in `BACKLOG.md` rather than a
  deletion.
- `net.h:78–79` `rx_bytes` / `tx_bytes` — read only by `net.c:259`'s exit log
  and `netcheck.c:113`. The status bar uses `g_rx_done`/`g_tx_done` in `main.c`
  instead. Harmless, but two counters for one thing.

**Functions never called**

- `sync.c:19` `void rc_sync_init(void) { }` — empty body, no call site
  anywhere. Declared at `sync.h:66`.
- `htmlview.c:356` `rc_html_ready` — declared `htmlview.h:28`, never called;
  `rc_html_poll` reads `v->ready` directly.
- `log.c:83` `rc_log_path` — declared `log.h:15`, never called.
- `ini.c:42` `rc_config_save` — called only from `rc_config_load`'s first-run
  branch (`ini.c:39`) and from `inicheck.c`. No UI path writes settings; see
  §8.1.

**`#define`s never used**

- `main.c:24` `ID_TRANSCRIPT` — the RichEdit control's id. `transcript.c` is a
  `.bak`; the id survives.
- `main.c:1009` `RC_USER_RULE` — the row of dashes from the RichEdit
  transcript, with a comment pointing at `bridge/markdown.py` (also a `.bak`).
  `bridge/html.py:98–101` records that the dashes were replaced by a tinted box.

**Computation whose only consumer is a log line**

`g_chat_widest` (`main.c:168`) is commented `/* px, for LB_SETHORIZONTALEXTENT
*/`. `LB_SETHORIZONTALEXTENT` appears nowhere in the codebase — `WS_HSCROLL` was
removed in favour of `fit_label()` ellipsis truncation, documented at
`main.c:1287–1293`. So `label_width_px()` (`main.c:890–911`, 22 lines:
`GetDC` / `SelectObject` / `GetTextExtentPoint32A` / `SelectObject` /
`ReleaseDC`) runs once per session row (`main.c:730`) to feed `main.c:741–743`:

```c
rc_log("chat list: %d sessions, widest %dpx", g_chat_count, g_chat_widest);
```

At the ~36 sessions the comment at `main.c:888` anticipates, that is 36 GDI
round trips per list refresh — and the list is refreshed on every turn end
(`main.c:634`) — for one number in the log. Delete `label_width_px`,
`g_chat_widest` and the `%dpx` from the log line; `fit_label` already does the
measuring that matters, in one `GetTextExtentExPointA` call.

**Vestigial build machinery**

- `Makefile:33–43` — the `lint` target greps `win/` for RichEdit 3.0-only
  messages. There is no RichEdit in `win/` any more.
- `win/transcript.o`, `win/transcript.c.bak.*`, `win/transcript.h.bak.*` — stale
  objects from the RichEdit era still in the tree; `transcript.o` is not in
  `OBJS`.

---

## 7. Comments that no longer describe the code

These matter more than usual here, because the comments in `win/` are the
design record — several are the only place a measured result is written down.

### 7.1 `net.c:592–596` — self-contradicting in three lines

```c
    /* Built on the caller's stack region via a heap block sized to the frame,
     * so there is no shared build buffer to race over any more. */
    static char build[RC_MAX_FRAME + 1024];
```

Neither clause is true: it is a `static`, it is shared, and the next comment
(`net.c:600–601`) says so — *"The build buffer is still shared, but only for the
memcpy that follows"*. The heap block is the queue item, allocated at
`net.c:610`. The design is fine and the locking is correct; the comment is from
a version that was rejected. It is also 66,560 bytes of BSS committed at load,
which `win/Makefile:151` calls out by name as the single largest static
allocation — so this is the comment somebody will read while trying to reclaim
it.

### 7.2 `main.c:1019` — wrong module, and the claim is false

> Must match `bridge/markdown.py` `user_block()`

The module is `bridge/html.py` and the function is `user_turn()`
(`html.py:95–105`). They do not match — see §2.2.

### 7.3 `main.c:1005` — "exactly one piece of markup"

> The guest builds exactly one piece of markup itself — the echo of the user's
> own line

It builds nine (§2.1), four of them with hand-written `<div class="...">`.

### 7.4 `CLAUDE.md` — "`make check` audits imports"

Top-level `Makefile:19–20` is `check: @$(MAKE) -C spike check`, which audits
`spike/hello98.exe` — a Phase-0 hello-world. The audit that matters is
`win/Makefile:126–160` (import table, W-suffix imports, CMOV, stack frames, BSS
totals) and is only reachable as `make -C win check`. Given that the rule it
enforces is the one described as "a single NT-only static import kills the image
at load, before `WinMain`, with no log written", the top-level target should
run both, or `spike`'s should be renamed.

### 7.5 `rpc.h:19` — "Returns 0 if queued"

`rc_rpc_submit` returns 0 when it queues the frame, **and** when it rejects it
for a family mismatch (`rpc.c:421`), **and** when it answers `busy` and drops it
(`rpc.c:458`). The accounting still balances — `drop()` decrements `rpcq` on
every one of those paths — but the documented meaning of the return value is
wrong, and `net.c:193` reads it as "queued or not". Say "0 = the worker has
taken ownership (queued, rejected or dropped); -1 = it has not."

---

## 8. What will resist Phases 4, 5 and 6

### 8.1 Phase 4 — settings dialog

`IDM_HOST` (`main.c:1568–1580`) is a `MessageBox` that prints the current host
and says *"A dialog for this is still to come."* What the dialog will hit:

- `rc_config_save` exists and is wired to nothing (§6). Fine — it is ready.
- **`cfg.autoscroll` and `cfg.font_size` are loaded, saved, round-trip-tested
  and read by nobody.** A dialog that exposes them ships two controls with no
  effect. `autoscroll` has an obvious home (§2.3); `font_size` has none — the
  transcript's font comes from the bridge's stylesheet (`bridge/html.py:333+`),
  which is the right design given "the guest transforms nothing", so `font_size`
  should either be deleted or become a `HELLO` header the bridge folds into the
  stylesheet it sends.
- Changing host/port at runtime means `rc_net_stop` + `rc_net_start`.
  `try_connect` (`main.c:1349`) already does exactly this and documents why the
  order matters (`main.c:1351–1358`). So the dialog's OK handler is
  `rc_config_save(); try_connect(hwnd);` — but `g_backoff_ms` and `g_retry_noted`
  are not reset there, so a manual reconnect after a long outage waits up to 15
  seconds before its first attempt. Small, but it is the first thing a user will
  do after fixing a typo'd host.
- The dialog needs a resource script or hand-built controls. `build_menu`
  (`main.c:342`) establishes the "build in code" precedent and gives the reason;
  a modal `DialogBox` also re-enters the message pump, which `main.c:34–37`
  identifies as the thing the permission bar was made modeless to avoid. Worth
  deciding deliberately: a settings dialog is modal and *should* be, because
  unlike a permission request it is not racing the stream — but the reader
  thread will keep posting `WM_RC_FRAME` into a window running a dialog loop,
  which is precisely the `RC_MAX_INFLIGHT` backpressure case `net.c:14–18`
  describes. That path has probably never been exercised.

### 8.2 Phase 4 — multiple clients

`WinMain:1678–1699` takes a named mutex and refuses a second instance, with a
good reason (the bridge holds one session and refuses a second client with
`busy`). If "multiple clients" means several *guests* against one bridge, none
of this matters and the guest is unaffected. If it means two windows on one
guest, the blockers are, in order: the mutex; then every singleton in §5.4;
then `rpc.c:45`'s single `g_net` (§5.4); then `exec.c`'s single `g_child` /
`g_cancel`, which means "cancel" is global and one window's Stop kills the
other's command.

### 8.3 Phase 5 — attachments

An attachment is a **guest-initiated bulk upload**. Today there is no such path:

- `do_send` (`main.c:1062`) builds the whole message in a `char buf[4096]` stack
  frame and sends one `INPUT` frame. `RC_INPUT_MAX` is 4096 and enforced twice
  (`EM_LIMITTEXT` at `main.c:1246`, re-checked at `main.c:1074`).
- The only chunked outbound code is `do_get` (`syncrpc.c:388–457`), which reads
  8KB at a time and emits `SYNCDAT` frames — **on the RPC worker thread, in
  response to a bridge `SYNCGET`**. Nothing lets the UI thread start a transfer.
- Doing it on the UI thread is not an option: `rc_net_send` blocks up to 5
  seconds on queue backpressure (`net.c:621–627`, 50 × 100ms), so a large
  attachment freezes the pump.
- Doing it on the worker requires a third frame family, and `rc_rpc_submit`
  hard-codes exactly two: `id_matches_family` (`rpc.c:393–398`) accepts only
  `'x'` and `'s'` prefixes, `is_sync` (`rpc.c:384`) is a `strncmp(type,"SYNC",4)`,
  and the queue choice is a binary sync/job decision (`rpc.c:425`). A third
  family touches all three plus `net.c`'s router (§5.1).

The shape that unblocks it: a generic **job** abstraction on the worker —
`rc_rpc_post(rc_job *j)` where a job is `{ family, id, run(), cancel() }` — with
`is_sync`'s FIFO becoming "this family is a stream" and the one-at-a-time slot
becoming "this family is a job". `EXEC`, `SYNC*`, attachments and Phase 6's
tools then all fit without touching `net.c` or the family table.

### 8.4 Phase 6 — `list_windows`, `screenshot`, `click`, `send_keys`

Beyond the routing and family problems above:

- **Thread affinity.** `EnumWindows`/`GetWindowTextA` are safe from the worker.
  `keybd_event`/`mouse_event` are global and safe on 9x. `GetDC(NULL)` + `BitBlt`
  is nominally safe, but the 16-bit GDI heap is the constraint this codebase
  cites repeatedly (`main.c:37, 91, 1290`) and a screen-sized DIB is not a small
  GDI object. Whether the capture belongs on the worker or on the UI thread is a
  real decision that the current structure does not force anyone to make,
  because `net.c`'s router will happily hand a `SCREENSHOT` frame to whichever
  thread the branch is written on.
- **Stack budget.** `sync.c:102–117` records an actual invalid-page-fault: the
  worker gets the image's 64KB stack *commit*, Open Watcom emits `sub esp, N`
  with no stack probe, and an 8KB local under a 62KB caller lands below the
  guard page. Every Phase 6 job that needs a buffer must heap-allocate, and
  the only thing enforcing that today is `tools/stackframes.py` in
  `make -C win check` — which is good, and is the one place this class of bug
  *is* mechanically caught. Keep that target in the loop when Phase 6 lands;
  see §7.4 about it not being reachable from the top-level `make check`.
- **Cancellation is per-process, not per-job.** `rc_exec_cancel` sets one
  `g_cancel` and terminates one `g_child` (`exec.c:68–75`). A `screenshot` in
  progress and an `exec` in progress cannot be cancelled independently, and
  `EXECKILL` on the reader thread (`net.c:180`) calls `rc_exec_cancel()` without
  checking the id against the running command — so an `EXECKILL` for a command
  that already finished kills whatever started since. `rc_exec_arm()` narrows
  the window (`exec.c:63–66`, and the ordering argument at `rpc.c:183–185` is
  careful) but does not close it, because `EXECKILL` carries an `id`
  (`net.c:181` logs it) that nothing compares.
- **Strips.** `screenshot` in strips is the same guest→bridge bulk path as
  attachments (§8.3), with the extra constraint that `RC_MAX_FRAME` is 64KB
  (`frameproto.h:17`) — so a strip is a chunked stream, not a frame, and it
  needs the same `off`/`total` accounting `do_get` already implements. That is
  an argument for generalising `do_get`'s loop out of `syncrpc.c` now, before
  there are three copies of it.

### 8.5 One protocol-level gap worth fixing before any of them

`rc_frame_parse` percent-decodes header values (`frameproto.c:84`), but
**`rc_frame_build` does not percent-encode them** (`frameproto.c:125–130`). The
wire format is `key=value\n`, so any value containing `\n`, `%` or (for the
first `=`) is ambiguous. Today every header the guest sends is either a
compile-time literal, a number, or a value the bridge sent it — and the bridge
*does* encode, so a bridge value containing a newline arrives decoded and is
echoed back raw. `HELLO`'s `os` header interpolates `os.szCSDVersion`
(`net.c:404`) and `sess` echoes a bridge-supplied id (`net.c:441`). No live path
carries a newline, but Phase 6 will add tool arguments and window titles as
header values, and window titles are user-controlled. Add the encoder now; it is
fifteen lines and it mirrors code that already exists.

Related and smaller: `rc_frame_parse` **silently drops** headers past
`RC_MAX_HEADERS` (16) — `frameproto.c:76` skips the store but still advances
`p`. The guest's own `HELLO` sends 24. Nothing inbound is near 16 today, but the
failure mode is "a header the bridge sent was ignored, with no log line", which
is the shape of bug this codebase has been bitten by twice (`ini.c:25–30`).

---

## 9. Smaller bugs found along the way

Not architecture, but found while reading and worth not losing.

1. **`syncrpc.c:449–450` reads uninitialised stack.** In `do_get`, `st` is
   `memset` only inside the `else` branch (`syncrpc.c:420`). When
   `rc_sync_contained(f->body)` fails (`syncrpc.c:414`), `status = "missing"`
   and control falls straight to `_snprintf(crcs, ..., st.crc)` and
   `_snprintf(clss, ..., st.cls ? ...)` with `st` never initialised. The
   `SYNCDATEND` for a rejected path therefore carries a garbage `crc` and a
   garbage `cls`. Move the `memset(&st, 0, sizeof(st))` up to the declaration.

2. **`log.c:101–104` can write past `line[]`.** `_vsnprintf(line,
   sizeof(line) - 3, fmt, ap)` returns `-1` and does **not** NUL-terminate when
   the output is truncated. The next line is `if (n < 0) n = (int)strlen(line);`
   — `strlen` on an unterminated 1021-byte buffer reads into `line[1021..1023]`
   and past the array if those are non-zero, and then `line[n++] = '\r'` writes
   wherever it stopped. The logger is described as the primary diagnostic on
   this platform (`log.h:1–8`), so it faulting on a long line is a bad trade.
   Fix: `line[sizeof(line) - 3] = '\0'; if (n < 0 || n > (int)sizeof(line) - 3)
   n = (int)strlen(line);`.

3. **`rpc.c:456` is dead.** `if (is_sync(f)) reject_sync_busy(f); else
   reject_busy(f);` — the sync path returned at `rpc.c:444`, so `is_sync(f)` is
   always false here. The comment above it ("Answer in the frame family's OWN
   vocabulary") describes an intent the control flow no longer needs. Harmless,
   but it reads as coverage that is not there.

4. **`rpc_watch_start()` races the thread it configures.** `rc_rpc_start`
   launches `rpc_thread` at `rpc.c:350` and calls `rpc_watch_start()` at
   `rpc.c:356`, which writes `g_watch` and the three `GetProcAddress` pointers.
   The thread reads `g_watch` at `rpc.c:287` and `p_next_change` at `rpc.c:298`.
   On a uniprocessor Pentium this will not bite, but the ordering is
   backwards for no reason — call `rpc_watch_start()` before
   `_beginthreadex`.

5. **`rc_rpc_start` sets `g_net` after the early return.** `rpc.c:338–339`:
   `if (g_thread) return 0; g_net = net;`. A reconnect that finds the worker
   still alive keeps the old pointer (§5.4).

6. **`g_lock` in `rpc.c` is never deleted.** `g_ready` is set at `rpc.c:342` and
   never cleared; `rc_rpc_join` closes the thread and the event but not the
   critical section. Leaking one at process exit is harmless and arguably
   deliberate (`net.c:697–701` makes exactly that argument), but it is
   undocumented here where it is documented there.

7. **`EXECKILL` ignores its `id`** (§8.4).

8. **`try_connect` does not reset `g_backoff_ms`** when invoked from a path
   other than the timer (§8.1).

---

## 10. Suggested order

If this were to be acted on, the order that unlocks the most with the least
risk:

1. §9's five small bugs, and §3.2's two bar holes (`bar_hide()` on disconnect
   and on `SWITCHED`). All are small, all are live.
2. §2.3 — the htmlview class. It is self-contained, it deletes code from
   `main.c`, and it makes `autoscroll` mean something.
3. §4.1 — the `rc_fmt`/`rc_cpy` helper plus the grep in `make -C win check`.
   Mechanical, and it closes the `g_put.rel` wire-reachable case.
4. §1.2 — the dispatch table. Nothing can be split out of `main.c` until
   `render_frame` stops being a chain.
5. §1.1 — `chatlist.c`, then `optmenu.c`, `pagebar.c`, `xferstat.c`.
6. §3.3 — the bar descriptor table, before Phase 6 adds states to it.
7. §5.1 — `rc_rpc_route`, before Phase 6 adds branches to `net.c`.
8. §8.3/§8.5 — the job abstraction and the header encoder, as Phase 5 starts.
