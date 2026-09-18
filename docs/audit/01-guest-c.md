# 01 — Guest audit: win/, architecture and code quality
#
# Audit date 2026-09-18. The full original report is preserved below as
# context. Findings are the lines at the end: one per finding, prefixed
# "Fixed:" once the thing it describes has actually been dealt with.
#
## Guest audit — `win/`, architecture and code quality
#
# Scope: `win/*.c` and `win/*.h` as of `main.c` 1783 lines, plus `win/Makefile`.
# Not the bridge, not the spike, not `probe.c` (a Phase-0 experiment that is no
# longer linked into anything).
#
# This is an architecture review. Bugs are reported where they were found, but
# the question being answered is: *is the structure right, is it consistent, will
# it survive Phases 4–6, and where has the code drifted from what the comments
# and the plan say it does.*
#
# ---
#
### Top findings
#
##
##
# 0. **Unbounded string functions on data that arrives from the wire.**
#    *(Raised by the user during the audit, not by the sweep -- and the sweep
#    had missed the worst instance.)* `strcpy`, `strcat`, `sprintf` and their
#    relatives write until a NUL that may not be there. `win/net/frameproto.c`
#    -- the frame parser, the first code untrusted bytes touch -- had two
#    `strcpy` calls and the one `_snprintf` the earlier `sizeof(x) - 1` sweep
#    never saw, because its bound is a macro (`RC_MAX_VAL - 1`) rather than
#    `sizeof`. The two `strcpy`s were guarded on the line above, so neither
#    could overflow as written; that is check-then-copy in two statements,
#    which is the shape that breaks when someone moves one of them.
#
#    The follow-on the user also named: the *reading* family (`strcmp`,
#    `strlen`, `strchr`) reads until a NUL too, so it is only safe if every
#    operand is guaranteed terminated. That is a property of the WRITES, not
#    of the reads -- which is why it is recorded here rather than as 109
#    separate findings.
#
# 1. **`main.c` is nine modules in a trench coat — and the plan said so.**
#    Thirteen distinct responsibilities share one file, ~60 file-static globals
#    and one 340-line `if/else` chain. `docs/PLAN.md:1259-1277` specifies
#    `w9x.c`, `dialogs.c`, `transcript.c` and `input.c`; none exist, `transcript.c`
#    was deleted with the RichEdit view, and `main.c` absorbed the menus, both
#    bars and the chat list. **No `PROGRESS.md` entry records that consolidation
#    as a decision** — every other structural change in this project has one.
#    The seam is already drawn for you, by frame type family. (§1)
#
##
# 2. **The HTML view is an API, not a view.** Nine `rc_html_append` sites in
#    `main.c`, three different markup conventions between them, two of which
#    inject unescaped text into `insertAdjacentHTML`. Scroll policy is decided
#    per call site: 2 of 9 scroll unconditionally, 1 scrolls conditionally,
#    6 never scroll, and `g_cfg.autoscroll` — which is loaded from the INI and
#    round-trip-tested by `inicheck.exe` — is never read by anything. (§2)
#
# 3. **The bar is a correct state machine written as four uncoordinated
#    functions.** The PERM/HALT collision fix is right, but it is enforced by
#    five `if`s spread over `bar_hide`, `perm_hide`, `halt_show`, `perm_show`
#    and `layout`, with `g_halt_id[0]` doing double duty as "the halt on screen"
#    and "the halt waiting behind the permission bar". Two holes remain today:
#    **nothing takes the bar down on disconnect or on session switch**, which
#    leaves Send permanently disabled. Phase 6's extra states will land in five
#    non-adjacent places. (§3)
#
# 4. **`net.c` contains the tool router.** The reader thread hard-codes
#    `"EXECKILL"`, `"EXEC"` and `strncmp(type,"SYNC",4)` (`net.c:180–222`), so the
#    transport layer knows the tool vocabulary and `net.c ↔ rpc.c` is a real
#    include cycle. Every Phase-6 tool adds a branch here. (§5)
#
# 5. **`_snprintf` has three idioms across 86 call sites, and `BACKLOG.md`'s
#    description of them is wrong.** `docs/BACKLOG.md:163-167` says "Every call
#    in the guest uses `sizeof(x) - 1` plus an explicit NUL, which prevents the
#    overrun". Measured: 39 do, **15 use `sizeof(x)-1` with no NUL**, and 32 use
#    the full size with no NUL. So the planned sweep is scoped to the wrong
#    defect — it looks for silent truncation, and would walk past the missing
#    terminators. Two of the 15 are reachable from the wire (`g_put.rel`,
#    `g_net.sess`). (§4.1)
#
##
# 6. **`rc_net_send`'s return value is checked at 3 of ~20 call sites** — and the
#    long comment explaining *why it must be checked* sits on `ID_PERM_ALLOW`'s
#    Continue button, two lines above `perm_reply()`, which does not check. Press
#    Allow on a dead link and the bar stays up with no feedback. (§4.2)
#
##
# 7. **Dead and vestigial**: `n->rpcq` (written 5×, read 0×), `n->next_seq`
#    (written, never sent — the resume path does not exist), `rc_sync_init()`
#    (empty, uncalled), `rc_html_ready()`, `rc_log_path()`, `ID_TRANSCRIPT`,
#    `RC_USER_RULE`, `cfg->font_size`, `cfg->autoscroll`, and
#    `label_width_px()` + `g_chat_widest` — ~25 lines and one GDI round trip per
#    session row whose only consumer is a log line. (§6)
#
##
# 8. **The comment on `rc_net_send`'s build buffer flatly contradicts the next
#    line of code** (`net.c:601–605`): it claims there is no shared build buffer
#    any more; the buffer is `static char build[RC_MAX_FRAME + 1024]`. (§7.1)
#
# 9. **`make check` audits the wrong binary.** `CLAUDE.md` says "`make check`
#    audits imports"; the top-level target runs `$(MAKE) -C spike check`, which
#    audits `spike/hello98.exe`. The real audit is `make -C win check`. (§7.4)
#
# 10. **Phase 5 and Phase 6 have no guest-side outbound path, and the bar cannot
#     render what Phase 6 needs.** Attachments and screenshot strips are
#     guest→bridge bulk transfers initiated locally; the only chunked outbound
#     code is `do_get()` inside `syncrpc.c`, driven by a bridge `SYNCGET`, and
#     `rc_rpc_submit`'s family check hard-codes exactly two id prefixes (`'x'`,
#     `'s'`). Separately, `docs/PLAN.md:1092-1097` requires the permission bar to
#     show a multi-step action summary; `g_perm_text` is a single-line
#     `SS_ENDELLIPSIS` static in a bar of fixed height. Neither is hard; both are
#     in the way. (§8)
#
# ---
#
### 1. `main.c`: what lives there, and what should move
#
#### 1.0 The split was designed and then not built
#
# `docs/PLAN.md:1259-1277` gives the intended layout:
#
# ```
#   win/                        # [R2] one binary, one target
#     frameproto.h  net.c  ini.c  log.c  w9x.c
#     main.c  transcript.c  dialogs.c
#     rpc.c  exec.c  input.c   # Phase 3 / Phase 6, same exe
# ```
#
# As built: `w9x.c`, `dialogs.c` and `input.c` never appeared; `transcript.c` was
# deleted when the view moved to MSHTML (`docs/PROGRESS.md:821`) and its
# contents went into `main.c` rather than into `htmlview.c`; `sync.c`,
# `syncrpc.c`, `htmlview.c` and `caps.h` arrived unplanned and are, notably, the
# *well-factored* part of the tree. `main.c` grew from "window, edit box, Send,
# reader thread" (`docs/PLAN.md:586-588`) to 1783 lines owning the menu bar, two
# modal-ish bars, the chat list and the status bar.
#
# That is ordinary accretion and not a crisis. What makes it worth the first
# section is that **it is the only large structural change in the project with no
# entry in `PROGRESS.md`**. The RichEdit→MSHTML reversal has 60 lines of
# justification (`docs/PROGRESS.md:819-885`); the fourth thread has an entry; the
# manifest v2 format has one. The consolidation of `main.c` has none, so there is
# nothing recording whether it was a decision or a drift — and `PLAN.md` still
# names `dialogs.c` as the home for the Phase 4 settings dialog and `input.c` as
# the home for Phase 6's `click`/`send_keys`.
#
#### 1.1 What lives there now
#
# `main.c` is 1783 lines holding thirteen responsibilities. Listed with their
# globals and their frame types, because that is where the seams are.
#
# | # | Responsibility | Lines | Owns | Frame types |
# |---|---|---|---|---|
# | 1 | App globals / window handles | 70–200 | `g_cfg`, `g_net`, `g_html`, 18 `HWND`s, 2 `HFONT`s | — |
# | 2 | Status bar + transfer accounting | 228–284 | `g_rx_*`, `g_tx_*`, `g_xfer_ms` | (all, via `render_frame`'s prelude) |
# | 3 | Pagination UI | 286–322, 773–799 | `g_view`, `g_total`, `g_is_live`, `g_loading_page` | `PAGESTART` `PAGEEND` `PAGESEALED` `ANCHOR` |
# | 4 | Model/Effort menus | 342–377, 647–687 | `g_menu_model/effort`, `g_opt_model/effort`, `g_n_*`, `g_opts_open` | `OPTION` `OPTIONSEND` |
# | 5 | Chat list | 379–415, 688–772, 890–974, 1113–1125, `WM_DRAWITEM`/`WM_MEASUREITEM` | `g_chat_ids`, `g_chat_is_header`, `g_chat_group`, `g_chat_current`, `g_chat_count`, `g_chat_widest`, `g_chats_oldproc` | `SESSIONS` `SESSION` `SESSIONSEND` `SWITCHED` |
# | 6 | Permission / halt bar | 417–548, 805–861, `ID_PERM_*` | `g_bar_mode`, `g_perm_id`, `g_perm_pending`, `g_halt_id`, `g_halt_text` | `PERMREQ` `PERMSTATE` `PERMWAIT` `PERMEND` `HALT` `HALTEND` |
# | 7 | Transcript markup | 1002–1060, + 9 append sites | `esc_html`, `echo_user_block`, `RC_USER_RULE` | `TEXT` `NOTICE` `ERROR` |
# | 8 | Compose / send | 1062–1141 | `RC_INPUT_MAX`, `g_input_oldproc` | `INPUT` `INTERRUPT` (outbound) |
# | 9 | Turn / "thinking" state | 976–1000 | `g_think_mark`, `g_turn_start_ms`, `g_streaming`, `g_turn_active` | `TURN` `TOOL` |
# | 10 | Frame dispatch | 550–888 | — | all 23 |
# | 11 | Connection lifecycle | 1349–1385, `WM_RC_STATE` | `g_backoff_ms`, `g_retry_noted` | `READY` |
# | 12 | Layout / control creation | 1143–1347 | every geometry constant | — |
# | 13 | `WinMain` / process setup | 1661–1783 | single-instance mutex, OLE, accelerators | — |
#
#### 1.2 What should move, concretely
#
# **`chatlist.c` (~400 lines, the cleanest cut).** Takes `chats_request`,
# `chats_switch`, `label_width_px`, `chats_text_width`, `fit_label`,
# `chats_proc`, the `WM_DRAWITEM`/`WM_MEASUREITEM` bodies (`main.c:1412–1493`),
# the four `SESSION*`/`SWITCHED` branches, and all seven `g_chat_*` globals.
# Nothing outside those lines touches a `g_chat_*` global — verified. The seam:
#
# ```c
# /* chatlist.h */
# void rc_chats_create(HWND parent, HINSTANCE inst, HFONT normal, HFONT bold);
# void rc_chats_move(int x, int y, int w, int h);
# int  rc_chats_frame(rc_frame *f);   /* non-zero: handled */
# int  rc_chats_draw(DRAWITEMSTRUCT *dis);
# int  rc_chats_measure(MEASUREITEMSTRUCT *mis);
# void rc_chats_open_selected(void);  /* the WM_RC_OPENCHAT / dblclk / Enter path */
# ```
#
# Its one dependency out is `rc_net_send` for `GETSESSIONS`/`SWITCH`, and one
# callback in — "a switch is about to happen, is a turn running, may I?" — which
# today is `chats_switch` reading `g_turn_active` and calling `MessageBoxA`
# directly (`main.c:396–407`). Pass that as a `int (*confirm)(const char *why)`
# so the module does not own a policy question.
#
# **`optmenu.c` (~90 lines).** `build_menu`'s Model/Effort halves, `clear_popup`,
# the `OPTION`/`OPTIONSEND` branches, the `IDM_MODEL0`/`IDM_EFFORT0` range
# dispatch in `WM_COMMAND`'s `default:`. This is the module that most obviously
# wants to be one: the id-range arithmetic at `main.c:1631–1642` and the array
# fill at `main.c:658–681` must agree about `MAX_OPTS` and the base constants,
# and they are 970 lines apart.
#
# **`pagebar.c` (~100 lines).** `page_update_ui`, `page_goto`, `page_goto_n`, the
# three `PAGE*` branches, the six button ids, and the page-bar strip of
# `layout()` (`main.c:1170–1178`).
#
# **`xferstat.c` (~70 lines).** `fmt_bytes`, the progress accounting prelude of
# `render_frame` (`main.c:552–572`), and `status_refresh`'s middle pane. Today
# that prelude runs before the type is even looked at and mutates four globals;
# it belongs behind `rc_xfer_note(f)` / `rc_xfer_text(char *out, unsigned cap)`.
#
# **`bar.c`** — see §3, which argues for more than a move.
#
# **`view` methods into `htmlview.c`** — see §2.
#
# That leaves `main.c` at roughly 600 lines: globals, `layout`,
# `create_children`, `wnd_proc`, `try_connect`, compose/send, turn state, and a
# dispatch table.
#
#### 1.3 `render_frame` should be a table, not a chain
#
# `main.c:550–888` is a 340-line `if/else` over 23 frame types (`main.c:575, 592,
# 606, 621, 626, 647, 684, 688, 696, 736, 746, 773, 788, 793, 800, 805, 815, 827,
# 844, 851, 862, 871`). Consequence, in order of severity:
#
# - It is the reason the file cannot be split: every module's frame handling is
#   interleaved in one function body, so moving `chatlist.c` out today means
#   leaving four `else if` stubs behind that call into it.
# - It costs up to 22 `strcmp`s per frame on a 200MHz Pentium. `TEXT` is third,
#   so the hot path is fine; `NOTICE` is 22nd and `PERMEND` 21st.
# - A typo in a type string is a silent fall-through to `rc_log("unhandled frame
#   type")`, which is exactly the failure mode `net.c:203–210` records for the
#   missing `SYNC` route: *"a sync layer that looks wired up on both sides and
#   transfers nothing, with no error on either."* The same trap is still armed,
#   one layer up.
#
# Shape of the fix — a table each module contributes to:
#
# ```c
# typedef void (*rc_frame_fn)(rc_frame *f);
# static const struct { const char *type; rc_frame_fn fn; } g_dispatch[] = {
#     { "TEXT",       on_text      },
#     { "SESSION",    rc_chats_on_session },
#     ...
# };
# ```
#
# A linear walk of that table is no slower than the chain and is one place to
# read. With the table in hand, the "unhandled" branch can also assert at startup
# that every type the bridge can send has an entry — `bridge/protocol.py` is the
# list.
#
# ---
#
### 2. The htmlview seam
#
#### 2.1 Every markup site in `main.c`
#
# | # | Line | Context | Markup built | Escaped? | Scroll |
# |---|---|---|---|---|---|
# | 1 | 589 | `READY` | `<div class="notice">Connected to bridge.</div>` | literal | none |
# | 2 | 618 | `TEXT` non-live | `f->body` (bridge-built) | bridge | **`if (at_bottom)`** |
# | 3 | 784 | `PAGESTART` continued | `<div class="notice">continued from the previous page</div>` | literal | none (`PAGEEND` scrolls later) |
# | 4 | 868 | `ERROR` | `"\r\n[%s] %s\r\n"` — **no div, no class, no escaping** | **no** | none |
# | 5 | 878 | `NOTICE` | `esc_html` → `<div class="notice">%s</div>` | yes | none |
# | 6 | 1055 | `echo_user_block` | `<div class="you">%s</div>` | yes | **unconditional** |
# | 7 | 1076 | `do_send` too long | `<div class="err">…</div>` | literal | **unconditional** |
# | 8 | 1099 | `do_send` not connected | `<div class="err">Not connected.</div>` | literal | **none** |
# | 9 | 1374 | `try_connect` | `"Cannot reach %s:%d - retrying\r\n"` — **no div, no class** | **no** | none |
#
# Plus `rc_html_live` (616), `rc_html_clear` (781) and the two bare
# `rc_html_scroll_bottom` at 789 and 1056/1081.
#
#### 2.2 What is actually wrong here
#
# **Scroll policy is accidental.** `rc_html_at_bottom` is consulted at exactly one
# of nine sites (`main.c:613`). So: a streamed answer respects where the reader
# has scrolled to; an error message, a "too long" refusal and the echo of your own
# line yank the view to the bottom regardless; and a `NOTICE`, a "Connected to
# bridge", a "Not connected" and a "Cannot reach … retrying" appear off-screen
# with no scroll at all if you have paged up. Nobody decided this. Sites 7 and 8
# are eleven lines apart in the same function and disagree.
#
# **`g_cfg.autoscroll` is dead.** `ini.c:24` loads it, `ini.c:52` saves it,
# `inicheck.c:67` asserts the round trip, `retroclaude.ini:7` sets it — and no
# line of code ever reads `cfg.autoscroll`. A Phase 4 settings dialog that exposes
# it would ship a checkbox that does nothing.
#
# **Two sites feed non-HTML to an HTML sink.** Sites 4 and 9 build `\r\n`-delimited
# plain text and hand it to `rc_html_append`, which calls
# `IHTMLElement_insertAdjacentHTML` (`htmlview.c:399`). The `\r\n`s render as
# nothing, so `[bad_session] no id` appears glued to the previous block with no
# styling. Worse, site 4 interpolates `rc_frame_get(f,"msg")` — a bridge-supplied
# string — unescaped. `bridge/protocol.py:203` builds `ERROR` from arbitrary
# exception text; a `<` in it silently eats the rest of the transcript block. The
# guest's own comment at `main.c:1005` says *"The guest builds exactly one piece
# of markup itself — the echo of the user's own line — and it still has to
# escape"*. That statement is false by four sites, and two of them do not escape.
#
# **The guest re-implements `bridge/html.py`.** `html.py:114` is
# `notice()`, `html.py:118` is `error()`, `html.py:103` is `user_turn()`. The
# guest hand-rebuilds all three. That is a small but direct breach of "the guest
# transforms nothing", and it has already drifted: `main.c:1019` says the echo
# *"must match bridge/markdown.py user_block()"* — the module is `bridge/html.py`
# (the `markdown.py` referenced is a `.bak`), and the two **do not match**.
# `html.py:102` does `text.strip().splitlines()`; `esc_html` (`main.c:1011`) does
# not strip. Send a message with a trailing blank line and the locally-echoed
# block and the one the paginator sealed differ — which is exactly the divergence
# the comment exists to prevent, visible the moment you page back.
#
# **Silent truncation at site 5.** `char esc[1024], div[1200]` on the `wnd_proc`
# stack; `esc_html` stops at `o + 8 < cap` (`main.c:1013`). `echo_user_block`
# already learned this lesson and documents it at length (`main.c:1034–1042`) —
# the `NOTICE` path never got the fix.
#
#### 2.3 The class the view should be
#
# `htmlview.h` today is a byte pipe: `append`, `live`, `clear`, `scroll_bottom`,
# `at_bottom`. Give it the transcript's vocabulary instead, and let it own both
# the markup and the scroll decision:
#
# ```c
# /* htmlview.h */
# void rc_html_set_autoscroll(rc_html *v, int on);   /* from cfg.autoscroll */
#
# /* Bridge-authored HTML, appended verbatim. Follows if the reader is at the
#  * bottom; never yanks. */
# void rc_html_append_block(rc_html *v, const char *html);
#
# /* Guest-authored. Each escapes its argument, wraps it in the agreed class,
#  * and always follows -- these are answers to something the user just did. */
# void rc_html_append_notice(rc_html *v, const char *text);
# void rc_html_append_error (rc_html *v, const char *text);
# void rc_html_append_user  (rc_html *v, const char *text);
#
# void rc_html_live(rc_html *v, const char *html);
# void rc_html_clear(rc_html *v);
# ```
#
# `rc_html_at_bottom` and `rc_html_scroll_bottom` become internal. The rule
# becomes one sentence each, stated in the header: *bridge blocks follow, guest
# messages insist*, and `set_autoscroll(0)` turns following off for both.
#
# What that does to the call sites:
#
# - Site 1 → `rc_html_append_notice(g_html, "Connected to bridge.")`
# - Site 2 → `rc_html_append_block(g_html, f->body)` — the `at_bottom`/`scroll`
#   dance at 613/619 disappears
# - Site 3 → `rc_html_append_notice(g_html, "continued from the previous page")`
# - Site 4 → `rc_html_append_error(g_html, msg)` — escaping arrives for free
# - Site 5 → `rc_html_append_notice(g_html, f->body)` — the 1024-byte stack
#   buffer and the truncation go away, because the escape can size from the input
#   inside the view (where `queue_add`'s growable buffer already lives,
#   `htmlview.c:248`)
# - Site 6 → `rc_html_append_user(g_html, buf)` — the two `HeapAlloc`s at
#   `main.c:1046–1053` collapse into the view, and `esc_html`, `echo_user_block`
#   and `RC_USER_RULE` leave `main.c` entirely
# - Sites 7, 8 → `rc_html_append_error(...)`, and they now behave identically
# - Site 9 → `rc_html_append_notice(...)`
#
# Net: `main.c` loses ~80 lines, `esc_html`, one 20KB heap round trip per message,
# and every scroll decision. `htmlview.c` gains ~50 lines and becomes the only
# file in `win/` that knows what a CSS class is — which is what `htmlview.h:9`
# already claims it is.
#
# One further step worth considering: the three `append_*` builders are a mirror
# of `bridge/html.py`. If the bridge sent `NOTICE`/`ERROR` as finished HTML the
# way it sends `TEXT` (it already has `H.notice`/`H.error` and already routes
# most notices through `_write_block`), the guest would need only
# `append_block`/`append_user` and the mirror problem is gone for good. Site 5 is
# the only `NOTICE` the bridge sends (`server.py:2043`) and it is a fixed string.
#
# ---
#
### 3. The bar state machine
#
#### 3.1 The current machine, extracted
#
# State: `g_bar_mode ∈ {BAR_NONE, BAR_PERM, BAR_HALT}` plus four satellite
# variables — `g_perm_pending`, `g_perm_id[80]`, `g_halt_id[80]`,
# `g_halt_text[256]`.
#
# | From | Event | Function | To | Notes |
# |---|---|---|---|---|
# | any | `PERMREQ`/`PERMSTATE` | `perm_show` 511 | `BAR_PERM` | unconditional; preserves `g_halt_id` |
# | `NONE` | `HALT` | `halt_show` 474 | `BAR_HALT` | |
# | `PERM` | `HALT` | `halt_show` 474 | `PERM` | halt *remembered* in `g_halt_id`/`g_halt_text`, early return at 486 |
# | `HALT` | `HALT` (new id) | `halt_show` 474 | `HALT` | **overwrites `g_halt_id` silently** |
# | `PERM` | `PERMEND` (id matches) | `perm_hide` 463 | `HALT` if `g_halt_id[0]`, else `NONE` | |
# | `HALT` | `HALTEND` (id matches) | `halt_hide` 505 | `NONE` | |
# | `PERM` | `HALTEND` (id matches remembered) | 833–841 | `PERM` | clears the remembered halt |
# | any | `WM_RC_STATE` disconnect | 1519 | **unchanged** | **hole** |
# | any | `SWITCHED` / `NEWSESSION` | 746 / 1548 | **unchanged** | **hole** |
#
# The design decision — a permission takes the bar because it has a deadline; a
# halt waits behind it — is right, and the 30-line comment at `main.c:88–117`
# earns its place. The problem is not the decision. It is that the decision is
# implemented as five conditionals in four functions, with no single place that
# can be read to know what the bar is doing.
#
#### 3.2 Concrete defects that remain
#
# **(a) Disconnect leaves the bar up and Send dead.** `WM_RC_STATE` on
# `RC_FAILED`/`RC_DISCONNECTED` (`main.c:1517–1522`) calls `ui_set_busy(0)` and
# nothing else. `ui_set_busy` computes
# `EnableWindow(g_send, (busy || g_perm_pending) ? FALSE : TRUE)` (`main.c:432`),
# so with `g_perm_pending` still 1 **Send stays disabled across the whole
# outage and after the reconnect.** It is re-enabled only by a `PERMEND` carrying
# that exact id. The bridge re-issues live requests as `PERMSTATE` on reconnect
# (`main.c:805–813`), but a request the bridge resolved *during* the outage
# produces no frame at all, so nothing ever clears it. The bar sits there
# offering Allow/Deny for a tool call that no longer exists, and pressing Allow
# sends a `PERMREPLY` with a stale id.
#
# **(b) Session switch leaves the bar up.** `SWITCHED` (`main.c:746`) rebuilds the
# transcript, resets `g_view`/`g_total` and repaints the chat list, but touches
# none of `g_bar_mode`, `g_perm_*` or `g_halt_*`. A permission bar raised by the
# previous session survives into the new one.
#
# Both are one function — `bar_hide()` is already written and already does the
# right thing (`main.c:443–459`). It needs calling from `WM_RC_STATE`'s failure
# branch and from `SWITCHED`. Note that `bar_hide` clears `g_halt_id` only under
# `BAR_HALT`, so on disconnect the *remembered* halt survives; for a disconnect
# that is arguably correct (the bridge still holds it and will re-raise), but it
# needs to be a decision rather than an accident, which is precisely the point.
#
# **(c) `g_halt_id[0]` means two things.** At `main.c:470` it means "a halt is
# waiting behind the permission bar"; at `main.c:444–447` it means "a halt is on
# screen". The disambiguator is `g_bar_mode`, read three functions away. That is
# why the `HALTEND` branch needs the explicit `else` at `main.c:836–841` with a
# four-line comment explaining which of the two it is clearing.
#
# **(d) `perm_show` and `halt_show` are unguarded against their own state.** A
# second `PERMREQ` while one is showing replaces `g_perm_id` and the first is
# never answerable. A second `HALT` does the same. Both are safe *today* only
# because the bridge serialises — the guest's own comment at `main.c:848` records
# this ("Only the queue depth changed; the request is the same one"). It is an
# invariant held by the other side of the wire with nothing on this side
# checking it.
#
# **(e) `ui_set_busy` is a setter and a refresher.** `bar_hide` calls
# `ui_set_busy(g_turn_active)` (`main.c:458`), i.e. `g_turn_active = g_turn_active`
# followed by a recompute. Split it: `ui_set_busy(int)` sets the turn flag,
# `ui_refresh_enables(void)` recomputes from `(g_turn_active, g_bar_mode)`.
#
#### 3.3 Make it explicit before Phase 6
#
# Phase 6 adds bar states (a screenshot in progress, a `click`/`send_keys`
# confirmation are the obvious candidates). Each new state today requires edits
# in five non-adjacent places:
#
# - the `BAR_*` enum (`main.c:120–122`)
# - a `*_show` function
# - `bar_hide`'s "clear only what THIS bar owned" conditional (`main.c:446`)
# - the `ID_PERM_ALLOW` dispatch (`main.c:1526–1544`) — one button, now three
#   meanings
# - `layout()`'s two branches (`main.c:1156` for the height,
#   `main.c:1197` for which buttons get screen space)
#
# None of the five references the others. The shape that fixes this is a table of
# bar *descriptors* and one `bar_set(const bar_state *s, const char *id, const
# char *text)`:
#
# ```c
# typedef struct {
#     const char *primary;      /* "Allow" / "Continue" / ... */
#     const char *secondary;    /* NULL = hidden */
#     const char *tertiary;
#     void (*on_primary)(const char *id);
#     void (*on_secondary)(const char *id);
#     void (*on_tertiary)(const char *id);
#     int  blocks_send;
#     int  priority;            /* who wins the bar; halt < perm today */
# } rc_bar_state;
# ```
#
# `bar_set` becomes the only writer of `g_bar_mode`, it enforces `priority`
# (replacing `halt_show`'s early return at 486 and `perm_show`'s implicit win),
# it parks the loser on a one-deep pending slot (replacing the overloaded
# `g_halt_id`), and `layout` reads `primary/secondary/tertiary != NULL` instead of
# switching on the mode. `ID_PERM_ALLOW`'s `if (g_bar_mode == BAR_HALT)` becomes
# `g_bar->on_primary(g_bar_id)`. Adding a Phase 6 state is then one table row and
# one callback, and `bar_hide()` on disconnect/switch is a single call that
# cannot forget a field.
#
# ---
#
### 4. Duplication and inconsistency
#
#### 4.1 The `_snprintf` idiom — three of them, 86 sites
#
# Measured across `main.c net.c rpc.c sync.c syncrpc.c exec.c ini.c log.c
# htmlview.c frameproto.c`:
#
# | Form | Count | Safe? |
# |---|---|---|
# | `_snprintf(x, sizeof(x) - 1, ...)` + explicit `x[sizeof(x)-1] = '\0'` | 39 | yes |
# | `_snprintf(x, sizeof(x) - 1, ...)`, no NUL | **15** | **no** |
# | `_snprintf(x, sizeof(x), ...)`, no NUL | 32 | **no** |
# | `_snprintf(x, cap + 63, ...)` + NUL | 1 | odd but safe |
#
# Microsoft's `_snprintf` (which Open Watcom follows) does not NUL-terminate when
# the output exactly fills the count. So the second form leaves `x[sizeof(x)-1]`
# at whatever was there before, and the third can fill the buffer entirely.
#
# The 15 in the second group: `main.c:255, 258, 263, 266, 577, 750, 864, 1584,
# 1597`; `net.c:332, 334, 404`; `syncrpc.c:109, 223, 224`.
#
# Two are reachable from the wire and are real latent bugs, not style:
#
# - **`syncrpc.c:224`** — `_snprintf(g_put.rel, sizeof(g_put.rel) - 1, "%.*s",
#   (int)rn, rel)` where `rn` is `atoi` of a wire-supplied length prefix
#   (`syncrpc.c:215`). A path of ≥ 259 bytes fills `g_put.rel[MAX_PATH]` with no
#   terminator; `g_put` is a file-static struct, so it holds whatever the previous
#   transfer left. `rc_sync_begin_write` then calls `strlen(rel)` (`sync.c:391`)
#   on an unterminated buffer. The module is otherwise scrupulous about exactly
#   this class of failure — `sync.h:21–37` is 17 lines on what a truncated
#   working name costs.
# - **`main.c:577` / `main.c:750`** — `g_net.sess` from a `READY`/`SWITCHED`
#   header, same shape, and `sess` is then sent back in `HELLO` (`net.c:441`).
#
# One helper removes all 47:
#
# ```c
# /* win/str.h -- the only way a string is copied or formatted into a fixed
#  * buffer in this codebase. Always terminates. */
##define RC_FMT(dst, ...)  rc_fmt((dst), sizeof(dst), __VA_ARGS__)
# void rc_fmt(char *dst, unsigned cap, const char *fmt, ...);
# void rc_cpy(char *dst, unsigned cap, const char *src);   /* the ~35 "%s" cases */
# ```
#
# Note C89: variadic macros are C99. Open Watcom accepts them, but if strict C89
# matters, `rc_fmt(dst, sizeof(dst), ...)` spelled out is still one idiom instead
# of three, and a trivial grep can assert that the second argument is always a
# `sizeof` with no `- 1`. That grep belongs in `make -C win check` next to the
# CMOV gate — mechanical, like `inicheck.exe`, and for the same reason: two bugs
# of the "somebody forgot" shape have already shipped here
# (`ini.c:25–30`, `win/Makefile:65–67`).
#
# The `+ 4` variants of the same idiom also exist: `sync.c` and `syncrpc.c` both
# build `root + '\' + rel` with `_snprintf` then walk the tail converting `/` to
# `\` — three times in `syncrpc.c` alone (`289–294`, `357–359`, `416–418`) and
# once as `join()` in `sync.c:231`. `join()` is the correct one and is `static`;
# `syncrpc.c` open-codes it three times because it cannot see it. Export it.
#
#### 4.2 Error-handling conventions
#
# **`rc_net_send` returns `-1` on a closed link, a full queue or an oversized
# frame. It is checked at 3 of ~20 call sites.**
#
# Checked: `main.c:1091` (`INPUT`), `main.c:1538` (`RESUME`), `rpc.c:141`
# (`EXECOUT`), `syncrpc.c:437` (`SYNCDAT`).
#
# Unchecked: `page_goto` (303), `chats_request` (381), `chats_switch` (414),
# `do_stop` (1106), `perm_reply` (541), `IDM_NEW` (1557), `SETMODEL` (1635),
# `SETEFFORT` (1641), `rpc.c:190, 226, 241, 375`, `syncrpc.c:52, 104, 151, 190,
# 343, 383, 411, 455`.
#
# The inconsistency is not uniform sloppiness — it is *localised*. `main.c:1531`
# carries a seven-line comment explaining exactly why an unchecked send is
# dangerous here:
#
# > `rc_net_send` returns -1 on a closed link, a full queue or an oversized frame,
# > and hiding regardless meant a press that never left the machine looked
# > answered
#
# Ten lines later, `perm_reply` (`main.c:535–548`) sends `PERMREPLY` and ignores
# the result. The same bar, the other three buttons, the identical failure: press
# Allow with the link down, nothing leaves the machine, the bar stays up (it comes
# down on `PERMEND`, by design), and there is no "Not sent" message because that
# string only exists on the Continue path. The fix was applied to one button.
#
# `chats_switch` is the other visible one: it sets the title to `"loading..."` and
# the status pane to `" loading conversation..."` *before* the send
# (`main.c:411–414`). If the send fails, both stay until the next `SWITCHED`,
# which will never arrive.
#
# The convention worth writing down and enforcing: **a send triggered by a user
# gesture must be checked and must undo its own optimistic UI; a send on a
# background path logs and continues.** That is three rules' worth of behaviour
# currently expressed as one comment on one button.
#
# Other conventions that are inconsistent:
#
# - **`CreateWindowExA` returns are never checked** for the 18 controls in
#   `create_children` (`main.c:1214–1347`) — only `rc_html_create` is
#   (`main.c:1234`). On a box whose 16-bit USER heap is a fixed 64KB, which this
#   codebase cites as a reason three separate times (`main.c:37, 91, 1290`), a
#   failed `CreateWindow` is a live possibility, and every subsequent
#   `SendMessageA(NULL, ...)` is a silent no-op. At minimum log which one failed;
#   the log is the only diagnostic on this platform (`log.h:1–8`).
# - **`HeapAlloc` is checked everywhere** — genuinely consistent, good.
# - **`rc_frame_get` NULL handling** alternates between the ternary-at-use idiom
#   (`main.c:583, 864–866`) and a local with a guard (`main.c:648–651`). Cosmetic,
#   but `main.c:583` calls `rc_frame_get(f, "model")` twice in one `rc_log`.
#
#### 4.3 Naming
#
# - **`rc_sync_*` spans two modules.** `rc_sync_scan/stat_file/contained/
#   begin_write/commit/recover/is_working_name/init` are in `sync.c`;
#   `rc_sync_set_root/root/handle` are in `syncrpc.c`. `main.c` must include both
#   headers to configure one string (`main.c:20–21`, used at `main.c:1700–1706`).
#   Rename the frame-handling half `rc_syncrpc_*`, or move `set_root`/`root` into
#   `sync.c` where the root conceptually belongs.
# - **`perm_*` vs `bar_*` vs `halt_*`** for one control group: `perm_layout()`
#   lays out the *bar*, not the permission bar (`main.c:435`); `bar_hide()` hides
#   either. Pick `bar_` for the shared machinery.
# - **`WM_APP` offsets are declared in three styles**: `WM_RC_FRAME`/`WM_RC_STATE`
#   in `net.h:35–36`, `WM_RC_OPENCHAT` at `main.c:1119` (mid-file, 400 lines from
#   its only use), and a bare `WM_APP + 99` at `main.c:1393` and `main.c:1396`.
#   Phase 6 will add cross-thread posts; one block of `#define`s at the top of
#   `main.c` with a comment that `WM_APP+1/+2` are net's.
# - **`g_think_mark` is a `long` holding only `-1` or `1`** (`main.c:179`), a
#   leftover of the RichEdit era when it was a character offset. It is now a
#   boolean spelled backwards; with `g_streaming` (`main.c:185`) it is two
#   booleans encoding three states (idle / thinking / receiving). One `enum
#   { TURN_IDLE, TURN_THINKING, TURN_STREAMING }` is clearer and removes the
#   `think_hide()` name, which no longer hides anything (`main.c:987–992`).
#
# ---
#
### 5. Module boundaries and dependencies
#
# Include graph (`.c` → headers it includes, excluding system and `miniz`):
#
# ```
# main.c      -> frameproto ini log net htmlview syncrpc sync
# net.c       -> net caps log rpc exec
# rpc.c       -> rpc exec syncrpc sync log
# syncrpc.c   -> syncrpc sync log      (syncrpc.h -> net.h)
# sync.c      -> sync log
# exec.c      -> exec log
# ini.c       -> ini log
# htmlview.c  -> htmlview log
# frameproto.c-> frameproto
# ```
#
# `log`, `ini`, `htmlview`, `frameproto`, `exec` and `sync` are clean leaves.
# Three problems above that line:
#
#### 5.1 `net.c ↔ rpc.c` is a cycle, and the transport owns the tool vocabulary
#
# `net.c` includes `rpc.h`; `rpc.h` includes `net.h`; `rpc.c` calls
# `rc_net_send`. The cycle is structural, not accidental — it exists because the
# **reader thread routes by frame type**:
#
# - `net.c:180` — `if (strcmp(f->type, "EXECKILL") == 0) { ... rc_exec_cancel(); }`
# - `net.c:190` — `if (strcmp(f->type, "EXEC") == 0) { ... rc_rpc_submit(f); }`
# - `net.c:212` — `if (strncmp(f->type, "SYNC", 4) == 0) { ... }`
#
# So `net.c` — whose job is "bytes on a socket, framing, threads" — knows what an
# EXEC is, knows that cancelling one goes through `exec.c` rather than `rpc.c`,
# and knows that `SYNC*` frames need `rc_rpc_syncq_full()` backpressure while
# `EXEC` does not. Every Phase 6 tool (`screenshot`, `click`, `send_keys`,
# `list_windows`) adds a branch here, in the module the standing rules say should
# be the most OS- and feature-neutral thing in `win/`.
#
# The seam: give `rpc.c` a router and let `net.c` ask it a yes/no question.
#
# ```c
# /* rpc.h */
# /* Does the worker want this frame? If it returns non-zero it has TAKEN
#  * ownership -- queued it, answered it, or freed it. */
# int rc_rpc_route(rc_net *n, rc_frame *f);
# ```
#
# `net.c:180–222` collapses to:
#
# ```c
# if (rc_rpc_route(n, f)) continue;
# ```
#
# `net.c` then includes neither `rpc.h` nor `exec.h`, the cycle becomes a plain
# `rpc → net` dependency, and the `EXECKILL`-on-the-reader-thread decision (which
# is correct, and well argued at `net.c:182–187`) moves next to the code it is
# about. `rc_rpc_syncq_full()` stops being public — the wait belongs inside
# `rc_rpc_route`.
#
#### 5.2 `main.c` maintains `net.c`'s ownership invariant
#
# `net.h:9–21` documents a three-way frame-ownership contract and calls it "not
# negotiable". Half of it is implemented in `main.c`:
#
# ```c
# case WM_RC_FRAME: {                     /* main.c:1504 */
#     rc_frame *f = (rc_frame *)lp;
#     render_frame(f);
#     rc_frame_free(f);
#     HeapFree(GetProcessHeap(), 0, f);
#     InterlockedDecrement(&g_net.inflight);
# ```
#
# `main.c` therefore has to know that a frame is two allocations, that the second
# is a raw `GetProcessHeap()` block, and that `inflight` is the backpressure gate
# the reader spins on at `net.c:224`. Get the order wrong or forget the decrement
# and the reader wedges at `RC_MAX_INFLIGHT` with no symptom other than a stalled
# transcript. One function in `net.c` — `void rc_net_frame_done(rc_net *n,
# rc_frame *f)` — puts the whole contract in the module whose header documents it.
#
# Related: `main.c` writes `g_net.sess` directly at `577` and `750` and
# `g_net.next_seq` at `579`. The session id is net-layer state (it is sent in
# `HELLO`, `net.c:441`) being set by the UI thread from a frame.
#
#### 5.3 One configuration string, three modules
#
# `cfg.sync_root` is read in `main.c` (`1700`), stored in `syncrpc.c`'s `g_root`
# (`syncrpc.c:17`) via `rc_sync_set_root`, and read back by `rpc.c`'s
# `rpc_watch_start` (`rpc.c:102`) and `rpc_maybe_nudge` (`rpc.c:93`) to decide
# whether to start a directory watcher. `rc_sync_recover` in `sync.c` takes the
# root as a parameter. So the same string is a global in one module, a parameter
# in another, and a dependency of a third. `rpc.c` reaching into `syncrpc.c`'s
# state to configure a `kernel32` watcher is the odd one: the watcher is a *sync*
# concern that happens to be driven from the worker's wait loop.
#
#### 5.4 Globals that should not be global
#
# Every worker-side module is a hard singleton. Listing them because §8 depends on
# it:
#
# | Module | Singleton state |
# |---|---|
# | `net.c` | `g_inf`, `g_inf_ready` (one inflate stream), `static char build[RC_MAX_FRAME + 1024]` inside `rc_net_send` |
# | `rpc.c` | `g_thread`, `g_wake`, `g_lock`, `g_running`, `g_queued`, `g_net`, `g_syncq[24]`, `g_watch`, `p_first_change`… |
# | `syncrpc.c` | `g_root`, `g_put`, `g_put_failed` |
# | `exec.c` | `g_chunk[8192]`, `g_cancel`, `g_child`, `g_child_lock` |
# | `log.c` | `g_fh`, `g_lock`, `g_path` |
#
# `rc_net` is a struct precisely so there can be more than one — `netcheck.c` has
# its own — but `rpc.c:45` holds a single `rc_net *g_net`, so a second `rc_net`
# would silently share the first's worker. `rc_rpc_start` takes `rc_net *` and
# then `return 0`s early if `g_thread` is already set (`rpc.c:338`) **before**
# assigning `g_net` (`rpc.c:339`) — so on a reconnect where the worker survived,
# `g_net` is stale. It happens to be the same address today because `main.c`'s
# `g_net` is itself a global. That is a latent trap rather than a live bug, and
# it is exactly the kind Phase 4's "multiple clients" would spring.
#
# `log.c` is fine as a singleton. The rest should at minimum take their state
# through the `rc_net *` they are already handed.
#
# ---
#
### 6. Dead and vestigial code
#
# Confirmed by grep across `win/` (excluding `.bak`, `probe.c`, `htmlspike.c`):
#
# **Fields written and never read**
#
# - `net.h:76` `volatile LONG rpcq` — incremented/decremented at `net.c:192, 194,
#   216, 218`, `rpc.c:248`, zeroed at `net.c:341`. **Never read.** Its sibling
#   `inflight` is read at `net.c:224` as the backpressure gate; `rpcq` has no
#   such consumer because the sync FIFO uses `rc_rpc_syncq_full()` instead. Three
#   header comments (`net.h:13–17`, `rpc.h:18`, `rpc.c:10`) describe maintaining
#   it as a correctness requirement. Either give it a consumer (a second
#   backpressure gate, or the `log_mem` line) or delete it and the three
#   comments — right now it is five interlocked operations per frame and a
#   documented invariant protecting a number nobody looks at.
# - `net.h:80` `unsigned next_seq` — set at `main.c:578–579` from `READY`.
#   Commented "resume point". Nothing sends `from=`; `main.c:807` says so
#   explicitly ("The guest never sends `from=`, so the bridge's replay buffer
#   cannot bring the bar back by itself"). The bridge's replay machinery
#   (`bridge/replay.py`) exists and `bridge/fakegui.py:207` uses it. So this is
#   not dead-by-accident, it is **an unimplemented feature with its state
#   plumbing already in place** — and it is already logged as such at
#   `docs/BACKLOG.md:212-218`, which makes the sharper point: *"`win/net.h`'s
#   `next_seq` is parsed and never read, which makes the feature look
#   implemented."* Unlike `rpcq`, this one should stay until replay is retired or
#   finished; it just wants a `/* not sent yet -- see BACKLOG */` on the field.
# - `net.h:78–79` `rx_bytes` / `tx_bytes` — read only by `net.c:259`'s exit log
#   and `netcheck.c:113`. The status bar uses `g_rx_done`/`g_tx_done` in `main.c`
#   instead. Harmless, but two counters for one thing.
#
# **Functions never called**
#
# - `sync.c:19` `void rc_sync_init(void) { }` — empty body, no call site
#   anywhere. Declared at `sync.h:66`.
# - `htmlview.c:356` `rc_html_ready` — declared `htmlview.h:28`, never called;
#   `rc_html_poll` reads `v->ready` directly.
# - `log.c:83` `rc_log_path` — declared `log.h:15`, never called.
# - `ini.c:42` `rc_config_save` — called only from `rc_config_load`'s first-run
#   branch (`ini.c:39`) and from `inicheck.c`. No UI path writes settings; see
#   §8.1.
#
# **`#define`s never used**
#
# - `main.c:24` `ID_TRANSCRIPT` — the RichEdit control's id. `transcript.c` is a
#   `.bak`; the id survives.
# - `main.c:1009` `RC_USER_RULE` — the row of dashes from the RichEdit
#   transcript, with a comment pointing at `bridge/markdown.py` (also a `.bak`).
#   `bridge/html.py:98–101` records that the dashes were replaced by a tinted box.
#
# **Computation whose only consumer is a log line**
#
# `g_chat_widest` (`main.c:168`) is commented `/* px, for LB_SETHORIZONTALEXTENT
# */`. `LB_SETHORIZONTALEXTENT` appears nowhere in the codebase — `WS_HSCROLL` was
# removed in favour of `fit_label()` ellipsis truncation, documented at
# `main.c:1287–1293`. So `label_width_px()` (`main.c:890–911`, 22 lines:
# `GetDC` / `SelectObject` / `GetTextExtentPoint32A` / `SelectObject` /
# `ReleaseDC`) runs once per session row (`main.c:730`) to feed `main.c:741–743`:
#
# ```c
# rc_log("chat list: %d sessions, widest %dpx", g_chat_count, g_chat_widest);
# ```
#
# At the ~36 sessions the comment at `main.c:888` anticipates, that is 36 GDI
# round trips per list refresh — and the list is refreshed on every turn end
# (`main.c:634`) — for one number in the log. Delete `label_width_px`,
# `g_chat_widest` and the `%dpx` from the log line; `fit_label` already does the
# measuring that matters, in one `GetTextExtentExPointA` call.
#
# **Vestigial build machinery**
#
# - `Makefile:33–43` — the `lint` target greps `win/` for RichEdit 3.0-only
#   messages. There is no RichEdit in `win/` any more.
# - `win/transcript.o`, `win/transcript.c.bak.*`, `win/transcript.h.bak.*` — stale
#   objects from the RichEdit era still in the tree; `transcript.o` is not in
#   `OBJS`.
#
# ---
#
### 7. Comments that no longer describe the code
#
# These matter more than usual here, because the comments in `win/` are the
# design record — several are the only place a measured result is written down.
#
#### 7.1 `net.c:601–605` — self-contradicting in three lines
#
# ```c
#     /* Built on the caller's stack region via a heap block sized to the frame,
#      * so there is no shared build buffer to race over any more. */
#     static char build[RC_MAX_FRAME + 1024];
# ```
#
# Neither clause is true: it is a `static`, it is shared, and the next comment
# (`net.c:609–610`) says so — *"The build buffer is still shared, but only for the
# memcpy that follows"*. The heap block is the queue item, allocated at
# `net.c:635`. The design is fine and the locking is correct; the comment is from
# a version that was rejected. It is also 66,560 bytes of BSS committed at load,
# which `win/Makefile:151` calls out by name as the single largest static
# allocation — so this is the comment somebody will read while trying to reclaim
# it.
#
#### 7.2 `main.c:1019` — wrong module, and the claim is false
#
# > Must match `bridge/markdown.py` `user_block()`
#
# The module is `bridge/html.py` and the function is `user_turn()`
# (`html.py:95–105`). They do not match — see §2.2.
#
#### 7.3 `main.c:1005` — "exactly one piece of markup"
#
# > The guest builds exactly one piece of markup itself — the echo of the user's
# > own line
#
# It builds nine (§2.1), four of them with hand-written `<div class="...">`.
#
#### 7.4 `CLAUDE.md` — "`make check` audits imports"
#
# Top-level `Makefile:19–20` is `check: @$(MAKE) -C spike check`, which audits
# `spike/hello98.exe` — a Phase-0 hello-world. The audit that matters is
# `win/Makefile:126–160` (import table, W-suffix imports, CMOV, stack frames, BSS
# totals) and is only reachable as `make -C win check`. Given that the rule it
# enforces is the one described as "a single NT-only static import kills the image
# at load, before `WinMain`, with no log written", the top-level target should
# run both, or `spike`'s should be renamed.
#
#### 7.5 `main.c:962–978` — a 17-line note about a control that no longer exists
#
# The comment explaining why the thinking indicator is not in the transcript is
# entirely about RichEdit:
#
# > deleting with `EM_REPLACESEL` puts Win98's riched20 back into the state where
# > it stops painting altogether […] Measured twice
#
# `riched20` is gone. The derived rule was written down as a standing one —
# `docs/PROGRESS.md:964-967`, *"nothing that has to disappear again goes in the
# transcript"* — but the mechanism that forced it does not apply to MSHTML, where
# `#l` is *designed* to be replaced wholesale (`rc_html_live`, `htmlview.c:415`)
# and `rc_html_clear` exists. So the constraint is now a choice, and it should be
# restated as one or dropped. `g_think_mark` being a `long` that only ever holds
# `-1` or `1` (§4.3) is the fossil of the offset it used to be.
#
# The measurement in that comment is worth keeping — it is the kind of thing this
# codebase is right to record — but it belongs in `PROGRESS.md` next to the rest
# of the RichEdit corpus, not in a live source file as justification for current
# behaviour.
#
#### 7.6 `rpc.h:19` — "Returns 0 if queued"
#
# `rc_rpc_submit` returns 0 when it queues the frame, **and** when it rejects it
# for a family mismatch (`rpc.c:421`), **and** when it answers `busy` and drops it
# (`rpc.c:458`). The accounting still balances — `drop()` decrements `rpcq` on
# every one of those paths — but the documented meaning of the return value is
# wrong, and `net.c:193` reads it as "queued or not". Say "0 = the worker has
# taken ownership (queued, rejected or dropped); -1 = it has not."
#
# ---
#
### 8. What will resist Phases 4, 5 and 6
#
# Requirements below are from `docs/PLAN.md` and `docs/BACKLOG.md`; the
# resistance is from the tree.
#
#### 8.1 Phase 4 — the settings dialog (`docs/PLAN.md:891-895`)
#
# > **Configure -> Server Host...** is a message box telling the user to edit the
# > file. It wants a real dialog — `wrc` is available for a resource, or a
# > runtime `DialogBoxIndirectParam` template.
#
# `IDM_HOST` (`main.c:1568–1580`) is that message box. What the dialog hits:
#
# - **There is no `dialogs.c`** and no `.rc` in `win/`. `win/Makefile` has a
#   `WRC` variable (`win/Makefile:12`) that is never used by any rule. So the
#   first dialog also brings the first resource step into the build — which is the
#   same prerequisite R5 (icons) needs (`docs/PLAN.md:1188-1194`), and the two
#   should land together.
# - **`cfg.autoscroll` and `cfg.font_size` are loaded, saved, round-trip-tested
#   and read by nobody** (§6). A dialog that exposes them ships two controls with
#   no effect. `autoscroll` has an obvious home (§2.3). `font_size` has none —
#   the transcript's font comes from the bridge's stylesheet
#   (`bridge/html.py:333+`), which is correct under "the guest transforms
#   nothing", so `font_size` should either go or become a `HELLO` header the
#   bridge folds into the stylesheet it sends.
# - **`rc_config_save` is only called when the INI does not exist** (`ini.c:38`).
#   A dialog's OK button is its second caller ever, and the first one that runs
#   against an existing file. Nothing is wrong with `rc_config_save` — but note
#   that `syncrpc.c:125-128` already documents the consequence of that
#   call pattern for a *generated* value, and R7 needs exactly such a value
#   (§8.2).
# - **Re-dialling after an edit.** `try_connect` (`main.c:1349`) already does
#   `rc_net_stop` + `rc_net_start` in the right order and says why
#   (`main.c:1351–1358`), so the OK handler is `rc_config_save(); try_connect();`.
#   But `g_backoff_ms` and `g_retry_noted` are not reset there, so a manual
#   reconnect after a long outage waits up to 15 seconds before its first attempt
#   — which is the first thing a user does after fixing a typo'd host.
# - **A modal re-enters the pump, and that is the argument the bar was built to
#   avoid** (`docs/PLAN.md:722-735`, restated at `main.c:34–37`). For a settings
#   dialog a modal is defensible — unlike a permission request it is not racing
#   the stream. But the reader thread keeps posting `WM_RC_FRAME` into a window
#   running a dialog loop, which is precisely the `RC_MAX_INFLIGHT` backpressure
#   case `net.c:14–18` describes and which has, as far as the code shows, never
#   been exercised. Worth deciding on purpose and worth testing with the bridge
#   streaming.
# - The rest of the Phase 4 list lands in `main.c` too and compounds the same
#   problem: **Export as HTML** (`docs/PLAN.md:897-899`) is a second consumer of
#   chunked transfer plus a Save-As; the **permission rule table**
#   (`docs/PLAN.md:901-902`) is a new read-only list surface; **R8's Command
#   menu** (`docs/PLAN.md:1252-1257`) is a menu that does `SetWindowText` on the
#   input box and must respect `EM_LIMITTEXT`/`RC_INPUT_MAX`; surfacing each
#   model's `description` (`docs/PLAN.md:875-877`) does not fit
#   `g_opt_model[16][64]` or a plain `AppendMenuA` label. Every one of those is
#   another 60–150 lines into a file that is already 1783.
#
#### 8.2 Phase 4 / R7 — multiple clients (`docs/PLAN.md:1232-1250`)
#
# The guest-side requirement is precise:
#
# > `client_id` sent in HELLO and **persisted in the guest's INI** so a reconnect
# > is recognised as the same client rather than a new one.
#
# Three concrete blockers:
#
# 1. **The INI has no write-back path for a generated-once value.**
#    `rc_config_save` runs only on first run (`ini.c:38`). A `client_id` generated
#    at startup and not written would be regenerated every launch, and every
#    reconnect would look like a different client — which is *exactly* the failure
#    `syncrpc.c:125-128` already reasoned about for the tree id and solved by
#    deriving it from the volume serial instead. Either R7 derives `client_id` the
#    same way (volume serial + something), or `rc_config_load` gains a "generate
#    and persist if absent" path. Worth deciding before the bridge side is built,
#    because the two answers put the identity in different places.
# 2. **The single-instance mutex** (`main.c:1688`,
#    `"RetroClaudeSingleInstance"`) forbids a second window on one guest. R7 as
#    specified is about several *guests*, so this may be fine — but the mutex name
#    and the `FindWindowA("RetroClaudeWnd")` raise (`main.c:1690`) are the two
#    places that would have to change, and the comment justifying the mutex
#    (`main.c:1678–1687`) cites the very rejection rule R7 narrows.
# 3. **Every worker-side module is a hard singleton** (§5.4). If "multiple
#    clients" ever means two connections in one process, `rpc.c`'s single
#    `g_net`, `exec.c`'s single `g_child`/`g_cancel` and `syncrpc.c`'s single
#    `g_root`/`g_put` all break, and `exec.c`'s global cancel means one window's
#    Stop kills the other's command.
#
#### 8.3 Phase 5 — attachments (`docs/PLAN.md:913-929`)
#
# The plan's central constraint is helpful here:
#
# > **Attachments never transit guest memory.** The GUI sends a *path*; the
# > bridge reads it and base64s it.
#
# So the common case is a path-bearing frame and costs the guest nothing. The
# resistance is in the other three bullets:
#
# - **`docs/PLAN.md:920-921`: "A file on the guest's local `C:\` is the only case
#   needing a chunked upload — progress bar and Cancel required."** Neither
#   exists. `InitCommonControlsEx` requests `ICC_BAR_CLASSES` only
#   (`main.c:1705`); a `PROGRESS_CLASS` control needs `ICC_PROGRESS_CLASS`, so the
#   first progress bar is also a change to process init. There is no Cancel
#   button anywhere and no `CANCEL\nid=` frame (`docs/PLAN.md:283-285`) on the
#   guest side.
# - **There is no guest-initiated bulk path at all.** The only chunked outbound
#   code is `do_get` (`syncrpc.c:388–457`) — 8KB reads emitting `SYNCDAT`, on the
#   RPC worker, *in response to a bridge `SYNCGET`*. Nothing lets the UI thread
#   start a transfer. Doing it on the UI thread is not an option: `rc_net_send`
#   blocks up to 5 seconds under queue backpressure (`net.c:646–652`, 50 × 100ms),
#   which `docs/BACKLOG.md:35-38` already lists as a known defect — *"The UI
#   thread can block up to 5 s in `rc_net_send` … The whole point of the sender
#   thread was that no caller blocks on the wire; this is the residue."* An
#   attachment upload makes that residue a freeze.
# - **A third frame family does not fit `rpc.c`.** `id_matches_family`
#   (`rpc.c:393–398`) accepts exactly two prefixes, `'x'` and `'s'`; `is_sync`
#   (`rpc.c:384`) is `strncmp(type,"SYNC",4)`; the queue choice is a binary
#   sync-or-job decision (`rpc.c:425`). A third family touches all three, plus
#   `net.c`'s router (§5.1).
# - **`docs/BACKLOG.md:223-227` is already flagged as "a blocker for any new sync
#   frame"**: `reject_sync_busy` answers *every* full-queue `SYNC*` frame with a
#   two-header `SYNCPUTACK`, but `SYNCSCAN` and `SYNCGET` are awaited as six- and
#   four-tuples on the bridge, surfacing as "sync pass aborted: not enough values
#   to unpack". See §9.9 — the same shape bites an attachment family.
# - **The toolbar.** `docs/PLAN.md:915-916` wants `TBSTYLE_DROPDOWN` with a
#   default item. There is no toolbar in `main.c`, and the `_WIN32_IE` pin
#   (`win/Makefile:27`, `docs/PLAN.md:491-494`) constrains which styles are
#   available. Note this bullet still says *"Attach from P:\\"* and *"the bridge
#   reads them off the share directly"* — the Samba share was replaced by the
#   sync layer in R6, so the *default item* in that dropdown is now "attach from
#   the mirrored folder", i.e. from `cfg.sync_root`. That is a doc drift
#   (`docs/PLAN.md:287-290`, `:113`, `:1296` carry the same residue) but it
#   changes what the guest has to send.
#
# The shape that unblocks all of it: a generic **job** abstraction on the worker —
# `rc_rpc_post(rc_job *j)` with `{ family, id, run(), cancel() }` — where
# `is_sync`'s FIFO becomes "this family is a stream" and the one-at-a-time slot
# becomes "this family is a job". `EXEC`, `SYNC*`, attachments and Phase 6's tools
# then all fit without touching `net.c` or the family table.
#
#### 8.4 Phase 6 — `list_windows`, `screenshot`, `click`, `send_keys`
#
# **The bar cannot render what `send_action_sequence` needs.**
# `docs/PLAN.md:1092-1097` makes this an explicit requirement:
#
# > The cost is that **the bar must render a multi-step summary on a 640x480
# > screen**, so the sequence needs a compact human rendering
# > (`type "hello" -> click [OK] -> wait "Notepad"`), not a JSON dump.
#
# `g_perm_text` is a single-line `STATIC` with `SS_LEFTNOWORDWRAP | SS_ENDELLIPSIS`
# (`main.c:1293–1295`) in a bar of fixed height `PERM_H 26` (`main.c:69`), sized
# right-to-left so the label takes whatever the buttons leave (`main.c:1203–1205`).
# A three-step sequence will be ellipsised to `type "hel...`. The bar needs a
# variable height and a multi-line control, which means `PERM_H` stops being a
# constant and `layout()`'s `ybar` arithmetic (`main.c:1156`) has to ask the bar
# how tall it is — an argument for the descriptor table in §3.3, which can carry
# a height.
#
# **There is a third bar state named in the docs and designed nowhere.**
# `docs/PLAN.md:1125-1128`:
#
# > A **"the agent is driving, hands off" modal state** is probably the answer,
# > and it is not designed yet.
#
# That is the fourth thing `g_bar_mode` will have to be, after `NONE`, `PERM` and
# `HALT`, and the one most likely to need to *block input to our own window*
# rather than merely disable Send.
#
# **`net.c` gains a branch per tool** (§5.1). `list_windows`, `screenshot`,
# `click`, `send_keys`, `send_action_sequence`, `launch`, `get_clipboard`,
# `set_clipboard` (`docs/PLAN.md:1104-1115`) are eight new frame types, all routed
# on the reader thread today.
#
# **Thread affinity is not forced by the structure.** `EnumWindows` /
# `SendMessage(WM_GETTEXT)` are fine from the worker. `keybd_event` /
# `mouse_event` are global. But `docs/PLAN.md:1053-1056` requires
# `AttachThreadInput` before a cross-thread `SetFocus` — and which thread "ours"
# is matters for `docs/PLAN.md:1056-1058`'s rule, *"Never send keys to our own
# window. Compare the target's thread against ours and refuse."* With the router
# in `net.c` there is nothing making that decision explicit; whoever writes the
# branch picks the thread.
#
# **Stack budget.** `sync.c:102–117` records an actual invalid-page-fault: the
# worker gets the image's 64KB stack *commit*, Open Watcom emits `sub esp, N` with
# no stack probe, and an 8KB local under a 62KB caller lands below the guard page.
# Every Phase 6 job needing a buffer must heap-allocate. The only thing enforcing
# that is `tools/stackframes.py` in `make -C win check` — which is the one place
# this class of bug *is* caught mechanically, and which §7.4 notes is not
# reachable from the top-level `make check`.
#
# **Strips are the same missing outbound path as attachments.**
# `docs/PLAN.md:994-998` — *"A full-screen DIB is 307KB and there are 28KB free.
# So capture proceeds in strips of N scanlines into a small fixed buffer, each
# strip compressed and sent as it is taken."* That is `do_get`'s loop with a
# different source. Generalising `do_get`'s `total`/`off` chunking out of
# `syncrpc.c` now, before there are three copies of it, is the cheapest thing on
# this list. Note also that `docs/PLAN.md:924-929` (Phase 5's "attach a screenshot
# of my desktop") and Phase 6's capture are *explicitly the same primitive* —
# "whichever lands first builds it" — so wherever it goes, it must not go inside
# `syncrpc.c` or inside `main.c`.
#
# **Cancellation is per-process, not per-job.** `rc_exec_cancel` sets one
# `g_cancel` and terminates one `g_child` (`exec.c:68–75`). A `screenshot` and an
# `exec` cannot be cancelled independently. And `EXECKILL` on the reader thread
# (`net.c:180`) calls `rc_exec_cancel()` **without comparing its `id`** against the
# running command — `net.c:181` logs the id and then discards it — so an
# `EXECKILL` for a command that has already finished kills whatever started since.
# `rc_exec_arm()` narrows the window (`exec.c:63–66`, and `rpc.c:183–185`'s
# ordering argument is careful) but does not close it.
#
#### 8.5 One protocol-level gap worth closing before any of them
#
# `rc_frame_parse` percent-decodes header values (`frameproto.c:84`), but
# **`rc_frame_build` does not percent-encode them** (`frameproto.c:125–130`). The
# wire format is `key=value\n`, so a value containing `\n` or `%` is ambiguous.
# Today every header the guest sends is a compile-time literal, a number, or a
# value the bridge sent — and the bridge *does* encode, so a bridge value
# containing a newline arrives decoded and is echoed back raw. `HELLO`'s `os`
# header interpolates `os.szCSDVersion` (`net.c:404`) and `sess` echoes a
# bridge-supplied id (`net.c:441`). No live path carries a newline. But Phase 6
# adds **window titles and control text as header values**
# (`docs/PLAN.md:970-973` requires HWND, child-index path, control id and class
# per node), and window titles are user-controlled. Add the encoder now; it is
# fifteen lines and mirrors code that already exists.
#
# Two smaller ones in the same file:
#
# - `rc_frame_parse` **silently drops** headers past `RC_MAX_HEADERS` (16) —
#   `frameproto.c:76` skips the store but still advances `p`. The guest's own
#   `HELLO` sends 24 in the other direction. Nothing inbound is near 16 today,
#   but the failure mode is "a header the bridge sent was ignored, with no log
#   line", which is the shape this codebase has been bitten by twice
#   (`ini.c:25–30`).
# - `RC_MAX_VAL` is 256 and `cwd` travels as a header
#   (`docs/BACKLOG.md:100-102`, `:180-185`), so it is silently clipped and then
#   handed to `CreateProcessA` (`exec.c:180`) with no validation — the command
#   runs in a *different existing directory* rather than failing. Already logged;
#   noted here because the same clipping now applies to anything Phase 6 puts in
#   a header.
#
### 9. Smaller bugs found along the way
#
# Not architecture, but found while reading and worth not losing.
#
# 1. **`syncrpc.c:449–450` reads uninitialised stack.** In `do_get`, `st` is
#    `memset` only inside the `else` branch (`syncrpc.c:420`). When
#    `rc_sync_contained(f->body)` fails (`syncrpc.c:414`), `status = "missing"`
#    and control falls straight to `_snprintf(crcs, ..., st.crc)` and
#    `_snprintf(clss, ..., st.cls ? ...)` with `st` never initialised. The
#    `SYNCDATEND` for a rejected path therefore carries a garbage `crc` and a
#    garbage `cls`. Move the `memset(&st, 0, sizeof(st))` up to the declaration.
#
# 2. **`log.c:101–104` can write past `line[]`.** `_vsnprintf(line,
#    sizeof(line) - 3, fmt, ap)` returns `-1` and does **not** NUL-terminate when
#    the output is truncated. The next line is `if (n < 0) n = (int)strlen(line);`
#    — `strlen` on an unterminated 1021-byte buffer reads into `line[1021..1023]`
#    and past the array if those are non-zero, and then `line[n++] = '\r'` writes
#    wherever it stopped. The logger is described as the primary diagnostic on
#    this platform (`log.h:1–8`), so it faulting on a long line is a bad trade.
#    Fix: `line[sizeof(line) - 3] = '\0'; if (n < 0 || n > (int)sizeof(line) - 3)
#    n = (int)strlen(line);`.
#
# 3. **`rpc.c:456` is dead.** `if (is_sync(f)) reject_sync_busy(f); else
#    reject_busy(f);` — the sync path returned at `rpc.c:444`, so `is_sync(f)` is
#    always false here. The comment above it ("Answer in the frame family's OWN
#    vocabulary") describes an intent the control flow no longer needs. Harmless,
#    but it reads as coverage that is not there.
#
# 4. **`rpc_watch_start()` races the thread it configures.** `rc_rpc_start`
#    launches `rpc_thread` at `rpc.c:350` and calls `rpc_watch_start()` at
#    `rpc.c:356`, which writes `g_watch` and the three `GetProcAddress` pointers.
#    The thread reads `g_watch` at `rpc.c:287` and `p_next_change` at `rpc.c:298`.
#    On a uniprocessor Pentium this will not bite, but the ordering is
#    backwards for no reason — call `rpc_watch_start()` before
#    `_beginthreadex`.
#
# 5. **`rc_rpc_start` sets `g_net` after the early return.** `rpc.c:338–339`:
#    `if (g_thread) return 0; g_net = net;`. A reconnect that finds the worker
#    still alive keeps the old pointer (§5.4).
#
# 6. **`g_lock` in `rpc.c` is never deleted.** `g_ready` is set at `rpc.c:342` and
#    never cleared; `rc_rpc_join` closes the thread and the event but not the
#    critical section. Leaking one at process exit is harmless and arguably
#    deliberate (`net.c:708–710` makes exactly that argument), but it is
#    undocumented here where it is documented there.
#
# 7. **`EXECKILL` ignores its `id`** (§8.4).
#
# 8. **`try_connect` does not reset `g_backoff_ms`** when invoked from a path
#    other than the timer (§8.1).
#
# 9. **`reject_sync_busy` answers the wrong shape for `SYNCSCAN`/`SYNCGET`.**
#    `rpc.c:367–376` sends a two-header `SYNCPUTACK` for *any* `SYNC*` frame it
#    cannot take — including the shutdown drain (`rpc.c:319`) and the full-queue
#    path (`rpc.c:439`). The bridge awaits `SYNCEND` (six headers) for a scan and
#    `SYNCDATEND` (four) for a get, so both surface as "sync pass aborted: not
#    enough values to unpack". Already recorded at `docs/BACKLOG.md:223-227`, and
#    listed there as *"a blocker for any new sync frame"* — repeated here because
#    it is the same structural problem as §5.1: the rejection path knows one
#    frame family's vocabulary while the router accepts four.
#
# 10. **The shutdown drain's acks never leave the machine.** `rpc.c:310–311`
#     promises *"Anything still queued at shutdown is answered, not silently
#     dropped — the bridge is holding a future for it."* It cannot be: `rc_net_stop`
#     sets `n->running = 0` (`net.c:665`) *before* calling `rc_rpc_stop`
#     (`net.c:669`), and `rc_net_send` bails on `!n->running` (`net.c:608`). Every
#     `reject_busy` / `reject_sync_busy` on that path returns `-1` unchecked.
#     Recorded at `docs/BACKLOG.md:228-235`; noted here because the comment is
#     still there asserting the opposite, which is §7's category.
#
# 11. **`n->sock` is read and written across four threads with no lock** — 14
#     accesses, including `recv_exact`'s two and the TOCTOU guard at `net.c:608`.
#     `rc_net_stop` closes it while the sender may have captured it into a local
#     (`net.c:580`), and Winsock recycles handle values.
#     `docs/BACKLOG.md:39-40`, `:228-235`.
#
# 12. **Inconsistent product name inside `main.c`.** `RC_TITLE` is
#     `"Gaston Code"` (`main.c:173`) and is used in seven places, but two
#     `MessageBox` captions are the literal `"RetroClaude"` — `main.c:1236`
#     ("Could not create the HTML view") and `main.c:1730` ("OLE could not
#     start"). Both are the *startup failure* boxes, i.e. the two a first-time
#     user is most likely to see. (The window class, mutex, INI, log and binary
#     names are all still `retroclaude*`, which is deliberate-looking and fine —
#     but these two are user-visible strings.)
#
# 13. **`g_doc_sent` is per-process, so a reconnect keeps a stale transcript.**
#     `main.c:76` / `main.c:596–597`: the `DOC` skeleton is installed once and
#     never again. `READY` updates `g_view` and `g_total` (`main.c:580–581`) but
#     does not re-fetch the page, so after a reconnect the window shows the old
#     content under a new page number. Already at `docs/BACKLOG.md:206-211`, which
#     notes the fix is one `page_goto("live")` in the `READY` handler. Listed here
#     because it is also the only global in `main.c` whose lifetime is
#     deliberately the *process* rather than the connection, and nothing says so
#     at the declaration.
#
# ---
#
### 10. Suggested order
#
# If this were to be acted on, the order that unlocks the most with the least
# risk:
#
# 1. §9.1, §9.2, §9.4, §9.5 and §9.12, plus §3.2's two bar holes (`bar_hide()`
#    on disconnect and on `SWITCHED`). All are small, all are live, none needs a
#    design decision. §9.9, §9.10, §9.11 and §9.13 are already in `BACKLOG.md`
#    and should be closed on its schedule, not this one.
# 2. §2.3 — the htmlview class. It is self-contained, it deletes code from
#    `main.c`, and it makes `autoscroll` mean something.
# 3. §4.1 — the `rc_fmt`/`rc_cpy` helper plus the grep in `make -C win check`.
#    Mechanical, and it closes the `g_put.rel` wire-reachable case.
# 4. §1.3 — the dispatch table. Nothing can be split out of `main.c` until
#    `render_frame` stops being a chain.
# 5. §1.2 — `chatlist.c`, then `optmenu.c`, `pagebar.c`, `xferstat.c`.
# 6. §3.3 — the bar descriptor table, before Phase 6 adds states to it.
# 7. §5.1 — `rc_rpc_route`, before Phase 6 adds branches to `net.c`.
# 8. §8.3/§8.5 — the job abstraction and the header encoder, as Phase 5 starts.
#
#
# ======================================================================
# FINDINGS. A line without "Fixed:" is outstanding work.
# ======================================================================
#
Fixed: 01.0b THE READING FAMILY, VERIFIED SITE BY SITE rather than argued about in aggregate. My first answer to this was a proof sketch -- "termination is a property of the writes, so the 109 reads are safe as a consequence" -- which is sound and was not verification. Three Sonnet agents checked all 111 sites individually (45 app, 33 sync, 33 net/core/checks), tracing every operand to its origin. The argument held for 107 of them; it missed four real defects, every one invisible to it. (1) win/core/log.c called _vsnprintf DIRECTLY -- the one place that skipped rc_strf -- and its `if (n < 0) n = strlen(line)` fallback ran on a buffer _vsnprintf leaves unterminated when the result does not fit, so an over-long log line was a stack over-read in the logger, the function whose job is to work when nothing else does. (2) win/net/net.c passed rc_frame_get(f,"id") straight into a %s with no NULL guard, and nothing requires EXECKILL to carry an id. (3) rc_sync_begin_write returned on BOTH refusal paths without writing tmp_out, so callers read uninitialised stack -- including synccheck.c, the binary that verifies that very function; that one I introduced myself two commits earlier. (4) g_chat_ids was filled by _snprintf(dst, 63, ...) into a 64-byte row, terminated only because index 63 is BSS zero that nothing writes -- an unstated invariant, one memset away from the exact bug. All four fixed; the brittle unguarded subtraction in recover_dir is now guarded too. The parser itself got a clean bill: rc_frame_parse never advances p past end, every segment is NUL-terminated in place before it is read, and there is no remote read overflow.
Fixed: 01.0 UNBOUNDED STRING FUNCTIONS on data that arrives from the wire. Raised by the user during the audit; the automated sweep had missed the worst instance. WHAT WAS WRONG: frameproto.c -- the frame parser, the first code untrusted bytes touch -- carried two strcpy calls and the one _snprintf the earlier `sizeof(x) - 1` sweep never saw, because its bound is a macro (RC_MAX_VAL - 1) rather than sizeof. Both strcpy sites were guarded on the line above, so neither could overflow as written, but that is check-then-copy in two separate statements. WHAT WAS DONE, and why "no usage remains unverified" is a claim rather than a hope: (a) every write-family call in the guest is now rc_strcpy/rc_strncpy/rc_strf, which always terminate and return whether they fit -- the last four were literal copies in inicheck.c and probe.c, converted despite being provably safe, so that the gate below needs no exceptions; (b) the two strncat sites in sync.cpp, whose failure mode is a search pattern with "\\*" missing rather than an error -- the b14be6d mechanism -- now refuse instead; (c) the header VALUE path in frameproto.c drops the frame rather than clipping at 255, and the bridge refuses to send an over-long cwd so a dropped frame is not the user's first symptom; (d) `make -C win check` greps for the whole family and FAILS the build, verified by reintroducing a strcpy and watching it fail. THE READING FAMILY (strcmp/strlen/strchr, 109 sites) is safe as a consequence rather than by inspection: it is only unsafe on an unterminated operand, and the three sources of operands are all terminated by construction -- f->body because net.c:143 allocates len+1 and writes payload[len]='\0', f->type and every header value because frameproto.c now fills them with rc_strcpy, and every local buffer because the writes above were converted. That is why this is one finding and not 109.
Fixed: 01.2 THE HTML VIEW WAS AN API, NOT A VIEW -- nine rc_html_append sites in main.c, three markup conventions between them, scroll policy decided per call site (two always, one conditionally, six never), and cfg.autoscroll loaded from the INI, written back, round-trip-tested by inicheck.exe and read by nothing. Two sites pushed UNESCAPED text into insertAdjacentHTML: the bridge's ERROR frame (its `code` and `msg`, which carry guest-controlled text and exception strings) and the reconnect message (the host, from the INI). Fixed: the view owns its vocabulary -- rc_html_notice/error/user/block -- each escaping its own text and applying one scroll policy in one place, with rc_html_block the single path that takes finished markup because the bridge renders the transcript and the guest transforms nothing. esc_html moved out of main.c entirely, gained `"` to its escape set, and echo_user_block collapsed from twenty lines of heap juggling to one call. rc_html_set_autoscroll makes the setting do something for the first time.
Fixed: 01.6 rc_net_send's RETURN CHECKED AT 3 OF ~20 SITES, with the seven-line comment explaining why it must be checked sitting above the Continue button while perm_reply -- the same bar, the other three buttons -- did not check at all. A lost PERMREPLY is the worst: the user pressed Allow, the model waits, and the bridge auto-denies ten minutes later with nothing on screen saying the answer never left the machine. A lost INTERRUPT is a Stop button that does nothing on a turn that keeps running and keeps costing. Fixed: ui_send() checks, logs, and puts an error in the transcript naming what did not happen; applied to the permission answers, Stop, conversation switching, the model and effort changes, and starting a new conversation. INPUT and RESUME already checked.
Fixed: 01.7 DEAD AND VESTIGIAL CODE, verified live before deleting anything rather than deleted on the audit's word. Three of the nine items were not a plain delete. `n->next_seq` is the one this audit argued to KEEP, tied to the unfinished resume feature and `bridge/replay.py` -- removed anyway, on instruction, because retiring the guest's inert copy touches neither `replay.py` nor the `seq` stamping: the bridge still sends `next_seq` on READY, the guest now just does not parse it, so the feature can still be finished from the bridge side without this field coming back first. `cfg->font_size` was the delicate one `inicheck.c` exists to catch: removed from the struct, `load()`, `save()`, and the test's field list and `sizeof` tripwire together, so the round trip still covers every remaining field (host, port, secret, autoscroll, sync_root). `cfg->autoscroll`, named in the same list below, is NOT touched -- 01.2 gave it a reader (`rc_html_set_autoscroll`) in a parallel pass, so it is live. `label_width_px()` + `g_chat_widest` fed one log line at the cost of ~36 GDI round trips per chat-list refresh; `LB_SETHORIZONTALEXTENT` is nowhere in the tree and `fit_label()` already does the measuring that matters. `rpcq` and the three comments defending it as an ownership counter are gone; the real three-way frame-ownership rule they were wrapped around is kept, just no longer pinned to a counter nobody read. `rc_sync_init()`, `rc_html_ready()`, `rc_log_path()`, `ID_TRANSCRIPT` and `RC_USER_RULE` had no call sites and no defending comments, and came out clean. `make -C win check` stayed green throughout (CMOV, unbounded strings, static constructors, stack frames) and all 2108 Python tests still pass; committed-at-load fell from 286,928 to 286,404 bytes (-524).
Fixed: 01.8 THE BUILD-BUFFER COMMENTS CONTRADICTED THE CODE AND EACH OTHER -- one said "there is no shared build buffer to race over any more" directly above the shared static; the other, twelve lines down, said it was "still shared, but only for the memcpy". The second was right. Fixed: both rewritten to say what send_lock actually covers, and to record that the buffer's 1024 bytes of slack are about 1020 more than rc_frame_build can ever use -- while deliberately NOT shrinking it, because BACKLOG §2 owns the memory pass and a buffer resized by reasoning rather than measurement is how the guard-page faults started.
#
01.1 main.c is THIRTEEN RESPONSIBILITIES IN ONE FILE and the plan said so: PLAN.md:1259-1277 specifies w9x.c, dialogs.c, transcript.c and input.c; none exist, transcript.c was deleted with the RichEdit view, and main.c absorbed the menus, both bars and the chat list. ~60 file-static globals. No PROGRESS.md entry records that consolidation as a decision, which every other structural change in this project has. The cleanest first cut is the chat list: no g_chat_* global is touched outside it.
Fixed: 01.3 THE BAR IS A CORRECT STATE MACHINE WRITTEN AS FOUR UNCOORDINATED FUNCTIONS. The PERM/HALT collision fix is right but is enforced by five ifs spread over bar_hide, perm_hide, halt_show, perm_show and layout, with g_halt_id[0] doing double duty as "the halt on screen" and "the halt waiting behind the permission bar". Two holes remain: nothing takes the bar down on disconnect or on session switch, and because ui_set_busy ORs g_perm_pending, Send stays disabled across the outage and after reconnect. THE TWO LIVE HOLES ARE FIXED: bar_hide() now runs on RC_FAILED/RC_DISCONNECTED and on SWITCHED. Neither did before, and because ui_set_busy ORs g_perm_pending into Send's enabled state, a disconnection while a prompt or halt was on screen left Send disabled across the entire outage AND after reconnecting, with a bar whose buttons answered a request the bridge had already abandoned -- the app looked wedged. Safe to drop locally because the bridge restores both on reattach (_reissue_prompt, _reissue_halt), so whatever is genuinely outstanding comes back and whatever is not, does not. On SWITCHED the session is rebuilt and any pending prompt is denied as "rebuild", so the bar was offering Allow/Deny for a request that no longer existed on a conversation the user had left. The structural remainder is tracked as 01.55 rather than left inside a line marked Fixed.
Fixed: 01.4 net.c CONTAINED THE TOOL ROUTER. The reader thread hard-codes EXEC/EXECKILL/SYNC dispatch at net.c:180-222, which makes a real net<->rpc cycle and means every Phase 6 tool adds a branch to the networking layer. Fixed: the vocabulary moved to rc_rpc_route() in the rpc layer, which returns TAKEN (consumed and freed) or PASS (belongs to the UI thread); net.c now contains no tool name at all -- grep for EXEC or SYNC in it returns nothing -- so the transport no longer knows the names of the things it carries and Phase 6's eight tools add no branch to the socket loop. The two load-bearing comments moved with the code: EXECKILL is answered on the reader thread because queueing it behind the worker that is running the command it cancels would never be looked at, and SYNC backpressure happens BEFORE submit because not reading the socket is the only pushback that costs no memory. main.c still maintains net's frame-ownership invariant by hand through WM_RC_FRAME; that half is untouched and stays outstanding as part of 01.1.
01.10 PHASE 5 AND PHASE 6 HAVE NO GUEST-SIDE OUTBOUND PATH, and the bar cannot render what Phase 6 needs. There is no guest-initiated chunked upload (Phase 5 attachments, Phase 6 screenshot strips); ICC_PROGRESS_CLASS is never registered although the plan mandates a progress bar; wrc is defined in win/Makefile and unused, so there is no dialogs.c for Phase 4's Server Host dialog; and PLAN.md:1092-1097 requires the bar to render a multi-step action summary while g_perm_text is a single-line SS_ENDELLIPSIS static at fixed height 26, about 45 characters at 640x480.
01.11 (§1.2) main.c's split has five concrete, scoped extraction targets that nothing has moved into yet: chatlist.c (~400 lines -- chats_request, chats_switch, label_width_px, chats_text_width, fit_label, chats_proc, the WM_DRAWITEM/WM_MEASUREITEM bodies at main.c:1412-1493, the four SESSION*/SWITCHED branches, all seven g_chat_* globals; no code outside those lines touches a g_chat_* global) with its own header (rc_chats_create/move/frame/draw/measure/open_selected) and a confirm callback replacing chats_switch's direct MessageBoxA call at main.c:396-407; optmenu.c (~90 lines -- build_menu's Model/Effort halves, clear_popup, the OPTION/OPTIONSEND branches, the IDM_MODEL0/IDM_EFFORT0 range dispatch, whose id-range arithmetic at main.c:1631-1642 and array fill at main.c:658-681 are 970 lines apart and must agree on MAX_OPTS); pagebar.c (~100 lines -- page_update_ui, page_goto, page_goto_n, the three PAGE* branches, the six button ids, layout()'s page-bar strip at main.c:1170-1178); xferstat.c (~70 lines -- fmt_bytes, render_frame's progress-accounting prelude at main.c:552-572, status_refresh's middle pane); and bar.c (see 01.15, which needs more than a move).
01.12 (§1.3) render_frame (main.c:550-888) is a 340-line if/else chain over 23 frame types that (a) is the reason main.c cannot be split, since every module's frame handling is interleaved in one function body, (b) costs up to 22 strcmp calls per frame on a 200MHz Pentium (NOTICE is 22nd, PERMEND 21st; TEXT is 3rd so the hot path is fine), and (c) makes a typo'd type string a silent fall-through to "unhandled frame type" with no error -- the exact failure mode net.c:203-210 already documents for the missing SYNC route ("a sync layer that looks wired up on both sides and transfers nothing, with no error on either"), just one layer up; fix is a static table of {type, handler} pairs that each module contributes rows to, letting a startup assertion check every bridge-sendable type (from bridge/protocol.py) has an entry.
01.13 (§3.2d) perm_show and halt_show (main.c) are unguarded against their own re-entrant state: a second PERMREQ while one is already showing silently replaces g_perm_id and the first request becomes unanswerable, and a second HALT does the same to g_halt_id; today this is safe only because the bridge serialises requests, an invariant enforced on the other side of the wire with nothing on the guest checking it (the guest's own comment at main.c:848 records the assumption).
01.14 (§3.2e) ui_set_busy (main.c) is both a setter and a refresher -- bar_hide calls ui_set_busy(g_turn_active) at main.c:458, i.e. g_turn_active = g_turn_active followed by a recompute of Send's enabled state; split it into ui_set_busy(int) that only sets the turn flag and ui_refresh_enables(void) that recomputes from (g_turn_active, g_bar_mode), so callers that just need a recompute stop having to pass the current value back in.
01.15 (§3.3) Adding a new bar state (Phase 6's screenshot-in-progress and click/send_keys confirmation are the named candidates) today requires synchronized edits in five non-adjacent places -- the BAR_* enum (main.c:120-122), a new *_show function, bar_hide's "clear only what THIS bar owned" conditional (main.c:446), the ID_PERM_ALLOW dispatch where one button now has three meanings (main.c:1526-1544), and layout()'s two branches for bar height (main.c:1156) and button space (main.c:1197) -- none of the five references the others; the proposed fix is a table of bar descriptors (primary/secondary/tertiary labels, their callbacks, blocks_send, priority) plus one bar_set() that becomes the sole writer of g_bar_mode, enforces priority in place of halt_show's early return and perm_show's implicit win, and parks the loser on a one-deep pending slot instead of the overloaded g_halt_id.
Fixed: 01.16 (§4.1) docs/BACKLOG.md MISDESCRIBED THE GUEST'S _snprintf USAGE, claiming "every call in the guest uses sizeof(x) - 1 plus an explicit NUL" and that only win/exec.c checked the return. Verified against the current tree: there are no live _snprintf calls left at all -- the two remaining matches are inside comments describing the old bug -- and everything routes through rc_strf/rc_strcpy/rc_strncpy, which always terminate and return whether the string fit. The bullet is struck through per this file's existing convention and replaced with three things: the corrected historical claim with the audit's own 39/15/32/1 split across the 86 sites that existed then, what is true now, and what is NOT closed -- 8 of 105 call sites check the return, so silent truncation is still structurally possible even though the unterminated-read class is gone. The distinction matters and the old bullet blurred it.
01.17 (§4.1) sync.c and syncrpc.c each build "root + '\' + rel" with _snprintf and then walk the tail converting '/' to '\', open-coded three separate times in syncrpc.c (289-294, 357-359, 416-418) plus once more as sync.c's own join() at sync.c:231; join() is the correct, already-written version but is declared static so syncrpc.c cannot see it and reimplements it three times instead -- export it.
Fixed: 01.18 (§4.2) CreateWindowExA's RETURN WAS NEVER CHECKED for any control in create_children -- on a platform with no debugger and no console, a failed control is an invisible gap in the window. A check_child() helper now logs the control's name and GetLastError for each. Note the finding's count was stale: there are 16 CreateWindowExA calls today, not 18, which is reported rather than quietly matched. Deliberately no fallback or disabling of dependent controls -- that is a design decision, and this closes only the invisibility.
Fixed: 01.19 (§4.2) rc_frame_get's NULL HANDLING WAS INCONSISTENT BETWEEN CALL SITES -- roughly 24 used a local variable and three used a ternary at the point of use, and the READY handler's version called rc_frame_get TWICE for "model" in one rc_log. All three converted to the dominant idiom; READY's reuses the local it already had, which removes the double lookup as a side effect. rc_frame_get itself untouched.
01.20 (§4.3) rc_sync_* naming spans two modules with no boundary marked in the name -- rc_sync_scan/stat_file/contained/begin_write/commit/recover/is_working_name/init live in sync.c while rc_sync_set_root/root/handle live in syncrpc.c, so main.c has to include both headers to configure one string (main.c:20-21, used at main.c:1700-1706); rename the frame-handling half rc_syncrpc_*, or move set_root/root into sync.c where the root conceptually belongs.
Fixed: 01.21 (§4.3) ONE CONTROL GROUP WAS NAMED THREE WAYS -- perm_layout() actually lays out the WHOLE bar for both modes and is called from bar_hide, halt_show and perm_show, while bar_hide already used the shared prefix. Renamed bar_layout with all three call sites updated. Local to main.cpp, no header involved.
Fixed: 01.22 (§4.3) WM_APP OFFSETS WERE DECLARED IN THREE DIFFERENT STYLES -- WM_RC_OPENCHAT defined 400 lines from its only use, and a bare `WM_APP + 99` appearing twice with no name at all. One #define block near the top of main.cpp now holds WM_RC_CONNECT (the bare one, named) and WM_RC_OPENCHAT, with a comment recording that net.h owns WM_APP+1 and +2 for its own messages so the two files cannot collide. net.h's definitions were referenced, not moved or duplicated.
Fixed: 01.23 (§4.3) g_think_mark WAS A long HOLDING ONLY -1 OR 1, a RichEdit-era leftover, sitting beside g_streaming as a second boolean describing the same thing. Both are gone, replaced by one `typedef enum { TURN_IDLE, TURN_THINKING, TURN_STREAMING } rc_turn_state;` and a single g_turn_state, with every call site updated. think_hide() was renamed turn_receiving() because it no longer hides anything -- it advances the state -- and the old name described a RichEdit operation that does not happen any more.
Fixed: 01.24 (§5.2) main.c HAND-MAINTAINED net.h's THREE-WAY FRAME-OWNERSHIP CONTRACT -- the WM_RC_FRAME handler did the two frees plus the inflight decrement in hand-written order, which is the contract restated by convention at the one place most likely to get it wrong. Now one rc_net_frame_done(n, f) call, defined next to the rule it enforces. It is a static __inline in net.h rather than a function in net.cpp, and the comment says why. The STRUCTURAL remainder -- inflight is still a plain struct member any includer can reach -- is NOT closed and is tracked as 01.57. Also stale in the original: next_seq no longer exists on rc_net at all.
01.25 (§5.3) cfg.sync_root is one configuration string spread across three modules with three different access patterns -- read in main.c (main.c:1700), stored as syncrpc.c's global g_root (syncrpc.c:17) via rc_sync_set_root, read back by rpc.c's rpc_watch_start (rpc.c:102) and rpc_maybe_nudge (rpc.c:93) to decide whether to start a directory watcher, and taken as a plain parameter by sync.c's rc_sync_recover; rpc.c reaching into syncrpc.c's state to configure a kernel32 watcher for what is really a sync concern is the odd one out.
01.26 (§5.4) Every worker-side module (net.c, rpc.c, syncrpc.c, exec.c) is a hard singleton -- g_inf/g_inf_ready/build[] in net.c; g_thread/g_wake/g_lock/g_running/g_queued/g_net/g_syncq[24]/g_watch in rpc.c; g_root/g_put/g_put_failed in syncrpc.c; g_chunk[8192]/g_cancel/g_child/g_child_lock in exec.c -- even though rc_net is a struct specifically so there can be more than one (netcheck.c already has its own); a second rc_net would today silently share the first's worker, because rpc.c:45 holds a single rc_net *g_net (see also 01.48, the same module's g_net-after-early-return bug).
Fixed: 01.27 (§6) net.h's rx_bytes/tx_bytes DUPLICATE main.c's g_rx_done/g_tx_done -- WRONG, and collapsing them as the finding suggests would have broken one of the two. rx_bytes/tx_bytes are CUMULATIVE RAW SOCKET counters for the whole connection, incremented on every read and write, and rpc.cpp differentiates them to produce the STAT bandwidth figure -- that needs a monotonic total. g_rx_done/g_tx_done are PER-TRANSFER progress-bar counters in decompressed application bytes, reset to zero after 1200ms of quiet and again at the start of every send -- that needs to be resettable. No single counter can do both jobs, and nothing reads one that also reads the other. Two counters with different semantics and similar names, not a duplication. Closed as incorrect.
Fixed: 01.28 (§6) VESTIGIAL BUILD MACHINERY -- STALE on both halves, verified rather than assumed. The `make lint` grep-for-RichEdit-messages half was really fixed in c4ea5c3 (and the fix went further than the finding asked: the grep had silently stopped covering the whole tree when the guest became C++). The stale transcript.o and transcript.c/.h .bak files do not exist anywhere under win/ -- searched the whole tree by name and by extension; the only .bak hits left are VM disk snapshots under vm/.
Fixed: 01.29 (§7.2) THE COMMENT AT main.c:1019 NAMED THE WRONG BRIDGE MODULE AND ITS CLAIM WAS FALSE. It said the user-echo markup "must match bridge/markdown.py user_block()"; that module is a .bak and that function does not exist -- it is bridge/view/html.py's user_turn(). Comment fixed to name the right module and function. Checking the claim also showed the two genuinely do NOT match, which is a live rendering bug and is tracked separately as 01.56 rather than fixed quietly from one side.
Fixed: 01.30 (§7.3) THE COMMENT AT main.c:1005 CLAIMING "the guest builds exactly one piece of markup itself" -- STALE. The comment and the esc_html() function it sat above were deleted together in 40d68bd when the notice/error/user/block vocabulary replaced hand-built <div> strings. Verified with `git log --all -p -S` on the quoted text rather than by grepping the current file and inferring, because absence in the working tree does not tell you whether something was fixed or never existed.
Fixed: 01.31 (§7.4) THE TOP-LEVEL make check AUDITED THE WRONG BINARY: Makefile's check: rule ran $(MAKE) -C spike check, which audits spike/hello98.exe, a Phase-0 hello-world, while the audit that actually matters -- import table, W-suffix imports, CMOV, stack frames, BSS totals -- was only reachable as make -C win check. So CLAUDE.md's "make check audits imports" was true of a target nobody's changes went through. Closed by the repo cleanup that deleted spike/: the top-level rule is now `check: $(MAKE) -C win check`, verified by `make -n check` expanding to the win/ target and by spike/ no longer existing, so CLAUDE.md's sentence is now true of the target people actually run.
Fixed: 01.32 (§7.5) THE SEVENTEEN-LINE COMMENT ABOVE think_show() JUSTIFIED KEEPING "thinking" OUT OF THE TRANSCRIPT because EM_REPLACESEL wedges riched20 -- a constraint belonging to a renderer that no longer exists, sitting above code that is still correct for an entirely different reason. Rewritten: the riched20 wedge is past tense, MSHTML's live()/clear() are named as why the constraint cannot recur, and the choice is restated as the design decision it actually is -- transient status does not belong in something you scroll back through or export. The original measurement is pointed at PROGRESS rather than repeated as live justification.
Fixed: 01.33 (§7.6) rpc.h's "Returns 0 if queued" WAS INACCURATE -- 0 is also returned when the frame is dropped for a family mismatch and when it is busy-rejected, both of which free it. Rewritten to state the real contract, which is about OWNERSHIP rather than about queueing: 0 means the worker took the frame and the caller must not touch it again; -1 means the frame is still the caller's to free, and happens only when the worker was never started or has been told to stop.
01.34 (§8.1) Phase 4's settings dialog (PLAN.md:891-895; today just a message box at IDM_HOST, main.c:1568-1580) has more blocking it than the missing dialogs.c/.rc/WRC infrastructure already noted in 01.10: rc_config_save is today called only from rc_config_load's first-run branch (ini.c:38-39) and from inicheck.c, so a dialog's OK button would be its first-ever call against an EXISTING INI file; the natural OK handler rc_config_save(); try_connect(); does not reset g_backoff_ms/g_retry_noted, so a manual reconnect right after fixing a typo'd host still waits up to the full ~15s backoff before its first attempt; a modal dialog re-enters the message pump while the reader thread keeps posting WM_RC_FRAME, which is exactly the RC_MAX_INFLIGHT backpressure case net.c:14-18 describes and which, as far as the code shows, has never been exercised against a live stream; and the rest of the Phase 4 list (Export as HTML, the permission rule table, R8's Command menu, per-model description text) all land in main.c too, each another 60-150 lines into a file already at 1783.
01.35 (§8.2) Phase 4/R7 "multiple clients" (PLAN.md:1232-1250) requires a client_id sent in HELLO and persisted in the guest's INI so a reconnect is recognised as the same client, and has three concrete guest-side blockers: (1) the INI has no write-back path for a generated-once value -- rc_config_save only runs on first run (ini.c:38), so an id generated at startup and never written would regenerate every launch, unlike the tree id syncrpc.c:125-128 already solved by deriving it from the volume serial, and R7 needs the same kind of decision made before the bridge side is built; (2) the single-instance mutex "RetroClaudeSingleInstance" (main.c:1688) and the FindWindowA("RetroClaudeWnd") raise (main.c:1690) forbid a second window on one guest, which may be fine if R7 means multiple guests rather than multiple windows per guest but needs deciding; (3) every worker-side module is a hard singleton (01.26) -- rpc.c's single g_net, exec.c's single g_child/g_cancel and syncrpc.c's single g_root/g_put would all break if "multiple clients" ever means two connections in one process, and exec.c's global cancel means one window's Stop would kill the other's command.
01.36 (§8.3) A third frame family (Phase 5 attachments, and Phase 6 screenshot strips) does not fit rpc.c's model: id_matches_family (rpc.c:393-398) accepts exactly two prefixes ('x' and 's'), is_sync (rpc.c:384) is a strncmp(type,"SYNC",4) check, and the worker's queue choice is a binary sync-or-job decision (rpc.c:425), so a third family touches all three plus net.c's router; separately there is no toolbar in main.c for PLAN.md:915-916's TBSTYLE_DROPDOWN attach control, and the plan text describing its default item ("Attach from P:\", "the bridge reads them off the share directly") is stale -- the Samba share was replaced by the sync layer in R6, so the default item is actually "attach from the mirrored folder" (cfg.sync_root), a doc drift also present at PLAN.md:287-290, :113, :1296; the proposed fix is a generic job abstraction on the worker (rc_rpc_post(rc_job *j) with {family, id, run(), cancel()}) so EXEC, SYNC*, attachments and Phase 6's tools all fit without net.c or the family table changing again.
01.37 (§8.4) PLAN.md:1125-1128 names a bar state that is "probably the answer" and "not designed yet" -- a "the agent is driving, hands off" modal state for Phase 6 -- which would be the fourth value of g_bar_mode after NONE/PERM/HALT and the first one that likely needs to block input to the guest's own window rather than merely disable Send; nothing in the current bar machinery anticipates this.
01.38 (§8.4) Phase 6's window/input tools have no structural guarantee of correct thread affinity: EnumWindows and SendMessage(WM_GETTEXT) are fine from the worker thread, but keybd_event/mouse_event are global, PLAN.md:1053-1056 requires AttachThreadInput before a cross-thread SetFocus, and PLAN.md:1056-1058's rule ("never send keys to our own window -- compare the target's thread against ours and refuse") has nothing in the router enforcing it -- with the tool dispatch living in rpc.c's router (post-01.4 fix), whoever writes each new branch has to remember to make that comparison themselves.
01.39 (§8.4/§7.4) Every Phase 6 job that needs a buffer must heap-allocate rather than use a stack local, because sync.c:102-117 already recorded an actual invalid-page-fault where the worker's 64KB stack commit plus Open Watcom's unprobed sub esp, N let an 8KB local under a 62KB caller land below the guard page; the only thing that catches this mechanically is tools/stackframes.py inside make -C win check, which per 01.31/CLAUDE.md is not reachable from the top-level make check.
01.40 (§8.4) The only chunked outbound code in the guest is do_get (syncrpc.c:388-457, 8KB reads emitting SYNCDAT on the RPC worker, driven by a bridge SYNCGET) -- nothing lets the UI thread initiate a transfer, and doing it on the UI thread is not an option since rc_net_send can block up to 5s under queue backpressure (net.c:646-652, already flagged at BACKLOG.md:35-38); Phase 5 attachment uploads and Phase 6 screenshot strips are explicitly the same primitive (PLAN.md:924-929, "whichever lands first builds it"), so do_get's total/off chunking loop should be generalised out of syncrpc.c now, before there are three separate copies of it.
01.41 (§8.4/§9.7) Cancellation is per-process, not per-job: rc_exec_cancel sets one g_cancel and terminates one g_child (exec.c:68-75), so a screenshot and an exec could never be cancelled independently once Phase 6 adds jobs; and today EXECKILL on the reader thread (net.c:180-181, now routed through rc_rpc_route per 01.4) calls rc_exec_cancel() without comparing its id against the running command -- the id is logged and then discarded -- so an EXECKILL for a command that has already finished kills whatever started since; rc_exec_arm() (exec.c:63-66) narrows the race but does not close it.
Fixed: 01.42 (§8.5) rc_frame_parse PERCENT-DECODED HEADER VALUES AND rc_frame_build NEVER ENCODED THEM, so frameproto.h's "header values are percent-encoded" was true in exactly one direction for the whole of Phases 1-3. The guest sent raw bytes to a bridge that decodes everything it receives: a value of "50%41" arrived as "50A", silently and self-consistently, and a value containing LF ended the header line so whatever followed was parsed as another header -- a frame could arrive carrying an `id` nobody sent, and `id` is what routes an ack. Not hypothetical: these values come from the INI (sync_root), from GetVersionExA and from the filesystem. Fixed with append_quoted() encoding %, LF and CR only, which keeps the parser at strchr. Keys stay raw deliberately, because they are all literals chosen in this tree and encoding them would turn a malformed key into something that quietly parses. Proven by win/checks/framecheck.cpp against the real frameproto.o and, more to the point, by tests/python/test_guest_frame.py feeding the SHIPPING builder's bytes to bridge/wire/protocol.py's own reader -- the two implementations checked against each other rather than each against itself. Mutation-tested: reverting the encoder fails 5 of 11 checks. That run corrected me on the finding itself -- "C:\100%WORK" round-trips fine unencoded because %WO is not hex, so only %-plus-two-hex-digits corrupts, and the obvious-looking test case is nearly worthless.
Fixed: 01.43 (§8.5) rc_frame_parse SILENTLY DROPPED ANY HEADER PAST RC_MAX_HEADERS (16) -- `if (out->nheaders < RC_MAX_HEADERS)` with no else, while the two checks immediately below it already refused the whole frame for an over-long key or an over-long value. So the one case that lost data in silence was the one where the data was perfectly well-formed and there was merely too much of it, and if the lost header was `id` an ack went to nobody and the far side waited out its whole timeout. It is a refusal now, symmetric with its neighbours. Safe to be strict rather than raising the limit: the bridge's largest frame carries six headers, and tests/python/test_guest_frame.py counts them out of protocol.py's Frame(...) literals by AST rather than trusting that number, so adding a seventeenth fails in the test instead of dropping a live link. framecheck.cpp pins both the refusal and the off-by-one (exactly sixteen still parses).
Fixed: 01.44 (§8.5) RC_MAX_VAL IS 256 AND cwd TRAVELS AS A HEADER VALUE -- this was already closed under 01.0 and the finding was STALE when written. Verified rather than assumed: frameproto.cpp's value path is `if (!rc_strcpy(h->val, RC_MAX_VAL, eq + 1)) return -1;`, which drops the frame, and the comment above it records the reasoning (a clipped cwd names a DIFFERENT, existing directory and CreateProcessA runs the command somewhere nobody chose). The bridge also refuses to send an over-long cwd. What was missing was a test, so the property was true by inspection only; framecheck.cpp's overlong-value-refused now asserts it against the shipping binary.
Fixed: 01.45 (§9.1) do_get LEFT ITS LOCAL rc_stat UNINITIALISED ON THE REJECTED-PATH BRANCH: the memset ran only inside the branch that stats a real file, so the rc_sync_contained failure branch fell through and built the crc and cls headers out of whatever was on the stack, then SENT them. Fixed by moving the memset above both branches, before either can be taken.
Fixed: 01.46 (§9.3) THE `if (is_sync(f))` IN rc_rpc_submit's BUSY PATH WAS DEAD -- the sync branch always returns earlier, so is_sync(f) is necessarily false by the time that line runs. Removed; the busy path calls reject_busy unconditionally and the comment now explains why that is the whole story.
Fixed: 01.47 (§9.4) rpc_watch_start() RACED THE THREAD IT CONFIGURES: _beginthreadex launched rpc_thread BEFORE rpc_watch_start wrote g_watch and three GetProcAddress pointers the new thread reads. Fixed by ordering -- the writes now happen before the thread exists, so the happens-before is structural rather than lucky. No regression test, and the reason is stated rather than glossed: it is a timing race with no reliable observable, and a test that cannot fail is worse than none.
Fixed: 01.48 (§5.4/§9.5) rc_rpc_start SET g_net AFTER ITS OWN EARLY RETURN -- `if (g_thread) return 0; g_net = net;` -- so a reconnect that found the worker already running kept the previous rc_net pointer. Fixed by assigning before the check. No regression test, and again the reason is honest: there is exactly one rc_net instance in the process today (01.26), so nothing can currently distinguish a stale pointer from a current one. The test becomes possible when 01.26 does.
Fixed: 01.49 (§9.6) g_lock IS NEVER DELETED -- and after checking, it MUST NOT BE, so this closes as a documented deliberate leak rather than as a deletion. The reader thread enters g_lock through rc_rpc_submit and rc_rpc_syncq_full, and rc_rpc_join has no visibility into whether that thread's own join (done by net.cpp, its caller) succeeded. Deleting unconditionally is exactly the destroy-under-a-live-thread GPF that net.cpp's send_lock comment already exists to avoid, and that shipped once. The leak is now documented at rc_rpc_join with that reasoning, mirroring net.cpp's. A critical section leaked at process exit costs nothing; one destroyed under a live thread costs the user a crash dialog.
Fixed: 01.50 (§9.9) reject_sync_busy ANSWERED EVERY SYNC* FRAME WITH SYNCPUTACK REGARDLESS OF TYPE -- STALE, already closed by the lane-table work in bf2ce55, which made the ack type follow the request type (SYNCSCAN->SYNCEND, SYNCGET->SYNCDATEND, SYNCMKDIR->SYNCMKDIRACK). Verified in the current source rather than taken on trust, and the bridge half is closed too: the futures resolve through .get() with defaults rather than positional unpacking, so a mismatched ack is a log line and not a ValueError out to connection teardown.
Fixed: 01.51 (§9.10) THE SHUTDOWN DRAIN'S ACKS NEVER LEFT THE MACHINE, contradicting rpc.cpp's own comment that anything still queued at shutdown is "answered, not silently dropped -- the bridge is holding a future for it". rc_net_stop cleared n->running as its second statement, before rc_rpc_stop(), and rc_net_send bails on !n->running -- so every ack the drain composed returned -1, unchecked. The flag meant two things at once: "the link is usable" and "accept new work". Separated by SEQUENCE rather than by adding a second flag: phase 1 stops and JOINS the worker while running is still 1 and the socket is still open, so the drain can send; phase 2 waits up to 2s for the outbound queue to empty; phase 3 clears running, closes the socket and joins the reader and sender. The 30s worker join moved earlier rather than growing, and the condition feeding the delete-the-critical-section decision is unchanged, because deleting send_lock under a live thread is the GPF-at-exit this file has been burned by twice. MEASURED ON THE REAL MACHINE with the counterfactual, via the new tools/drainprobe.py: 600 SYNCGETs queued, app closed mid-flight. BEFORE, 460 answers, ZERO busy acks, and the link dropping mid-frame with 140 requests unanswered. AFTER, 526 answers including exactly 24 busy acks arriving immediately before the close -- 24 is RPC_SYNCQ_MAX, the whole queue drained and answered. And the app still exits with no GPF dialog, which was the real risk of reordering shutdown on this platform.
01.52 (§9.11) n->sock is read and written across four threads with no lock protecting it -- 14 access sites including recv_exact's two and the TOCTOU guard at net.c:608 -- so rc_net_stop can close the socket while the sender thread has already captured its value into a local (net.c:580), and Winsock is free to recycle that handle value to something else in the meantime; already recorded at docs/BACKLOG.md:39-40 and :228-235.
Fixed: 01.53 (§9.12) THE PRODUCT NAME WAS INCONSISTENT INSIDE main.cpp -- RC_TITLE is "Gaston Code" and is used everywhere except two MessageBoxA captions that still said "RetroClaude". Both now pass RC_TITLE. Worth more than a cosmetic note because of WHICH two: they are the startup failures ("Could not create the HTML view.", "OLE could not start."), so the user met the old name at exactly the moment something had gone wrong and they were trying to work out what they were running.
Fixed: 01.54 (§9.13) g_doc_sent IS PER-PROCESS, SO A RECONNECT DID NOT GET A FRESH TRANSCRIPT: DOC is sent once per CONNECTION by the bridge and the guest's latch swallowed it on every reconnect, while READY updated the page counters from its headers without re-requesting any content -- so the previous page's text sat under the new page's number. Nothing looks wrong in that state, which is what makes it bad. Fixed as BACKLOG already prescribed, with one page_goto("live") in the READY handler: it clears through the existing PAGESTART path and repopulates from what the bridge holds now, and is a no-op on a genuinely fresh session. g_doc_sent itself is left alone. NOT yet verified on the real machine.
01.55 THE BAR IS STILL FIVE IFs ACROSS FOUR FUNCTIONS, with g_halt_id[0] doing double duty as "the halt on screen" and "the halt waiting behind the permission bar". The two live holes are closed (bar_hide now runs on RC_FAILED/RC_DISCONNECTED and on SWITCHED), but the structure that produced them is unchanged: Phase 6's extra states would still land in five non-adjacent places in bar_hide, perm_hide, halt_show, perm_show and layout. Wants the bar descriptor table, and it is the same object as 04.6's "the permission bar must become a stack, not a mode" -- which records that two modes arbitrated pairwise have already cost four data-loss bugs. Split out of 01.3, which was marked "Fixed: (in part)" and therefore invisible to the tracking grep.
01.56 THE LOCAL USER ECHO AND THE BRIDGE'S SEALED COPY RENDER THE SAME MESSAGE DIFFERENTLY: bridge/view/html.py's user_turn() does text.strip().splitlines() before escaping (html.py:102) and the guest's esc_into() does not strip, so a message with a trailing blank line is one line taller in the echo the user sees immediately than in the paginated copy they scroll back to. Found while fixing 01.29's false comment. Closing it means either stripping in the guest's say()/echo path or not stripping in user_turn(), which is a decision about which side owns the whitespace rule -- and it must not be taken quietly from one side, because the whole point of the comment was that the two are supposed to agree.
01.57 net.h's `inflight` IS STILL A PLAIN STRUCT MEMBER any includer can read or write, so the three-way frame-ownership contract is enforced by one helper being the convenient path rather than by the counter being unreachable. rc_net_frame_done() (01.24) means main.cpp no longer hand-orders the two frees and the decrement, which was the site most likely to get it wrong, but anything that includes net.h can still touch n->inflight directly and nothing would notice. Closing it means the counter living behind net.cpp's own functions, which is part of the same change 01.26 wants (every worker-side module is a hard singleton) rather than a separate edit.
