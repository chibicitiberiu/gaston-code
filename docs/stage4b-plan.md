# Stage 4b — the "agent is driving" indicator

## Context

Stage 4 closed 2026-10-14 (`8853ec6`). The indicator left that stage by review: both candidate homes
put the strip inside Gaston's own window, and a drive calls `SetForegroundWindow` on its target, so
the window saying "the agent is driving" would spend the whole drive behind the application being
driven. `docs/ROADMAP.md:593-633` holds what that review established.

**The requirement, in the user's words:** *"some controls might be hidden by it. Do we make it
transparent to mouse and keyboard? do we hide it when taking screenshots? Window should have an
indication 'Gaston is controlling the desktop' with 2 ways to cancel it, with a global keyboard
shortcut and a button"*.

**Decided by the user:** hidden while a screenshot is taken · up per step list plus a dwell · the
chord is **Ctrl+Alt+Shift+F12** · **"stop should simply cancel the run and act like the 'stop'
button in the ui"**.

**One place this plan extends that instruction, flagged for veto.** The client's Stop button sends
`INTERRUPT` and ends the turn. gremote has no turn and no Stop button — its only stop control is the
tray's "automation: off". So in gremote, Stop cancels **and** sets the automation level to 0, which
is that program's version of the same gesture. Taking the client's answer literally there would ship
a Stop whose effect the next request undoes a second later, which is the thing `BACKLOG.md:2366-2389`
says this feature exists to prevent.

Three review rounds ran against this design — one on the sketch, two on the plan. They falsified
six claims, then eleven more. What survived is below; what they killed is in the next section,
because three of those mistakes are ones a reader would otherwise make again.

---

## What review killed

| was going to be built | why it cannot be |
|---|---|
| the indicator posts `WM_RC_INTERRUPT` to `rc_net::notify` itself | `rc_net` is exe code and `libautomation` **may not know the wire** (CLAUDE.md). The owner passes a `StopSink` instead. |
| `UiContext` owns it | `UiService::init()` runs on the **main** thread (`app/main.cpp:266`), and there is no `uicontext.cpp` — the ctor is in `uiengine.cpp:149`, linked by five targets. |
| `UiOps` owns it in gremote | `UiOps` is a member of `Session` (`remote/server.h:102`), **made fresh per TCP connection**. That is a new thread, a repeat `RegisterClassA` and a fresh `RegisterHotKey` on every reconnect — and a leaked session (`server.cpp:587,600`) keeps the chord for the life of the process. |
| `HTTRANSPARENT` makes it click-through | the `WM_NCHITTEST` walk is documented **thread-scoped**; every window the strip covers is in another process. The step-aside is the design; `HTTRANSPARENT` is at most an optimisation. |
| the denylist goes in `keyDown`/`pressKey`/`typeText` | `typeText` and `pressKey` **have no production caller** (only `autocheck`/`focusprobe`). The live hole is `tap key=f12 mods=7`, and `tapKey` **discards `keyDown`'s return** (`input.cpp:806-820`) — so a guard there would make the step report `ok` having typed nothing. |
| one RAII hide/show helper in `clickAt`, `clickButtonAt`, `buttonDown` | `clickButtonAt` calls `clickAt` internally (`input.cpp:1084`), so the inner destructor shows the strip again **before** the outer `mouse_event`. One bracket per step in `StepRunner` instead. |
| `IsWindowVisible == false` is enough before a capture | it goes false when `ShowWindow` returns, before the windows underneath repaint. Right for hit-testing, **wrong for `BitBlt`**, which reads the framebuffer. |
| INTERRUPT alone stops the agent | `coordinator.py:205-213` kills every pending **exec** and never touches `ui_pending`. `test_server_integration.py:2190-2197` records why the exec loop exists: *"on mcp 1.x the SDK does not cancel an in-flight tool call when a turn is interrupted … Measured on the guest."* A second `ui(run=…)` batched into the same message waits on `guest.ui_lock` and drives after the Stop. |
| the client's Stop can borrow `IDM_SCREEN` | `optionsApply` raises a **modal MessageBox mid-drive** and drops the connection (`mainwindow.cpp:2180-2199`) — asking the person who just hit the panic button a second question. |
| `UiOps::m_ctx` becomes a `UniqueObj` while we are in the file | deliberate: `UiContext` holds apartment-bound COM and a per-thread message filter, and `~UiOps` leaves it alone **so a leaked lane does not free it under a live user**. CLAUDE.md's guard rule and the apartment rule pull opposite ways here. Record, do not change. |
| the strip is draggable | `SetCapture` hands the indicator thread the mouse half of the input queue — the `TrackPopupMenu` hazard with a longer exposure. Right-click jumps it to the other corner instead. |

Three record corrections fall out, independent of whether this ships: **`BACKLOG.md:2516-2521`
states a mitigation that does not exist** ("the ui lane refuses while the menu is up" — nothing
reaches `UiOps`, and there is no log either); **`ROADMAP.md:580` cites `BACKLOG.md:2063`** for "two
programs can drive one desktop", which is `:2488`; and **`PROGRESS.md:6-10`** still says the current
goal is stage 5 and that 4b is unplanned.

---

## The design

**Three types, and the seam is one virtual.**

- `DriveOverlay` — abstract, header-only, in `win/automation/overlay.h`. `shown()`,
  `coversPoint()`, `hideFor(ms)`/`resume()`, `driveStarting(volatile LONG *cancel)`, `driveEnded()`.
  Everything in `libautomation` talks to this and nothing else, so only vtable slots are referenced
  and `indicator.o` stays out of `uicheck`, `autocheck`, `keyprobe` and `focusprobe`.
- `DriveIndicator : public DriveOverlay` — `win/automation/indicator.cpp`, the thread, the window,
  the painting, the chord.
- `StopSink` — abstract, one method, implemented by each program. **The indicator never names the
  wire.**

`UiLink` gains exactly one virtual, `virtual DriveOverlay *overlay() { return 0; }`.
`rc_ui_run` (`uiengine.cpp:854`) reads it once, hands it to `runner.setOverlay()` beside the
existing `setCancel()`, and brackets `runner.run`. `FakeUiLink` inherits the default, so `uicheck`
links nothing new.

**Ownership is the App, in both programs** — `ClientApp` and `GremoteApp`, created in
`initializeServices()` in dependency order and passed down the constructors that need it
(`UiService` → `rc_uirpc_attach` → `NetUiLink`; `ServerService` → `Session` → `UiOps` →
`RespUiLink`). Process-scoped, which is what the window and the chord are. The indicator owns its
own thread, so nothing requires it to be built on the driving thread.

**The thread and the window.** A `Thread` (`core/thread.h`) started on the first drive. The
`Window`-derived object is a **local inside the thread body**, never a member: `DestroyWindow` fails
across threads and neither `~Window` (`window.cpp:20-27`) nor `WindowGuard` (`rc_cpp.h:262-266`)
checks the return, so a member would leave a visible strip whose `GWL_USERDATA` points at freed
memory. Other threads touch a published `volatile HWND` and nothing else. **Quit is
`PostMessage(hwnd, WM_CLOSE)`, never `PostThreadMessage`** — a thread message to a thread that has
not pumped yet is lost, not queued (`uiservice.cpp:46-51`), and that window is wide open between
`Thread::start` and `CreateWindowEx`. `HINSTANCE` is `GetModuleHandleA(NULL)`, as `keyprobe.cpp:86`
already does. A failed `CreateWindowEx` is **latched**: the drive proceeds, the log says so once,
and it is not retried on every subsequent drive.

**The window itself.** `WS_POPUP`, exStyle `WS_EX_TOPMOST | WS_EX_TOOLWINDOW`, `SW_SHOWNOACTIVATE`,
`WM_MOUSEACTIVATE` → `MA_NOACTIVATE`, **no child controls**, `WM_DISPLAYCHANGE` → reposition. Hand
painted (`DrawEdge`, `DrawTextA`, `FillRect`, `COLOR_BTNFACE`) with `GetStockObject(DEFAULT_GUI_FONT)`
— no GDI handle on a 32MB guest's heap — and a width from `GetTextExtentPoint32A`, never laid out by
eye. **Position: bottom right, just above the taskbar**; right-click moves it to the top right, with
no capture and no menu. Never `WS_DISABLED`: `WindowFromPoint` skips a disabled window but the click
still does not fall through, which would turn today's honest `INPUT_OCCLUDED` into a step that
reports `ok` having clicked nothing.

**Two invariants, both load-bearing, both stated because the next person will not guess them.**
*It never activates* — `steps.cpp:464` fails **every** typing step with `refused_self` when the
foreground is ours, so an indicator that can take the foreground breaks the feature it belongs to.
That, plus having no child window to hold focus, **is** the answer to "transparent to keyboard".
*Stop acts on `WM_LBUTTONDOWN`, not `BN_CLICKED`* — `clickAt` calls `SetCursorPos` before every
click (`input.cpp:218`), so on one shared input queue the pointer is being teleported away between
a real button's down and up. A `BN_CLICKED` Stop would fail exactly while the agent is driving,
which is the only time it is on screen. It also means **the chord is the primary route and the
button is the backup**, not the other way round.

**Lifetime.** Show after ~250 ms of driving (a one-step `focus` finishing in 50 ms must not flash a
topmost window and repaint everything under it), minimum visible ~1.5 s, dwell ~3 s after the last
list, all on the indicator's own timer. The chord is registered when the strip appears and released
at the end of the dwell, not held for the life of the process. **Stop bypasses both the dwell and
the minimum**: it repaints to "Stopping…" immediately and goes. A strip that goes on saying "Gaston
is controlling the desktop" for three seconds after the panic button is the opposite of the feature.

**Stop, per program.** Both raise the request's stop flag (`UiLink::stopFlag()`, polled between
steps, inside every wait, at every walk node and capture strip) so the list in flight answers
`cancelled`. Then:
- **client** — `PostMessage(notify, WM_RC_INTERRUPT (WM_APP+3))`, handled on the window thread
  behind the `if (m_input)` guard `mainwindow.cpp:1518` already uses, calling `command(ID_STOP)` →
  `ComposeBox::doStop()` → `INTERRUPT` (`app/input.cpp:187-194`). No modal, no reconnect, no INI
  write. `doStop` is silent when disconnected (`m_sender` null), so the strip says what happened.
- **bridge** — `on_interrupt` must also cancel pending **ui** calls, the way it already kills pending
  execs, and refuse a new ui op while `turn_stopped` is set. Without it the next queued `ui(run=…)`
  drives a second after the Stop.
- **gremote** — the flag, plus `automation = 0` posted to the tray so the tooltip and the menu's
  radio ticks stay true (`tray.cpp:391-404` is the existing writer; writing the field behind its
  back leaves the menu lying). The strip says screen control is now off and where to turn it on.

**Occlusion: the strip steps aside.** One bracket **per action step** in `StepRunner::runOne`, not
three in `Input` — it nests correctly, it is one hide per step instead of three, and `Input` stays
free of the overlay. The hide must land **before** `isOurs(WindowFromPoint(p))` at `input.cpp:1054`
and `:1078`, not merely before the click, or an unpositioned click under the strip is
`INPUT_REFUSED_SELF` before anything moves. The wait is a bounded poll that **pumps**
(`pumpOwnQueue`, `input.cpp:558-575` — out-of-context WinEvent delivery stopped past ~220 unpumped
events, measured on the Athlon), and on timeout the step is **refused rather than clicked blind**,
because the one thing that can block the indicator thread is Win16Mutex, which is also the state in
which a blind click is least predictable. `steps.cpp:767` gets the `isOurs` guard
`StepRunner::run` already uses at `:905`, or the rest of the list anchors `m_expectTop` to our own
strip.

This is an **occlusion fix, not a safety fix**: `clickButtonAt:1073-1079` and `buttonDown:1050-1055`
already test the point, so a single driver cannot click its own Stop today. The mixed case can
(`BACKLOG.md:2488-2514`), and that is recorded rather than guarded.

**Screenshots: hidden, with a settle.** One bracket at the **top of `Screenshot::capture`**, not
around the `BitBlt` at `shot.cpp:133` which is inside the strip loop — and skipped entirely when the
requested rect does not intersect the strip. Hide, `RedrawWindow(RDW_INVALIDATE|RDW_ERASE|
RDW_ALLCHILDREN)`, bounded settle (measured in E2), then capture. Not `RDW_UPDATENOW`: it
`SendMessage`s `WM_PAINT` cross-process and wedges behind a hung app or Win16Mutex. Cropping the
strip's rows out instead was considered and rejected — the strip sits above the taskbar, so a crop
would cut the taskbar out of every screenshot. **The client's own `IDM_ATTSHOT` path
(`app/screenshot.cpp:110`) gets the same bracket**: that image goes to the agent too.

**The chord.** `Ctrl+Alt+Shift+F12` (gaston) and `F11` (gremote) — distinct because whichever
registers first wins and the second fails silently; the strip prints the chord it actually holds, or
says "Stop button only" if registration failed. The denylist goes in **`tapKey`**, on the composed
`mods` plus `m_held`, mirroring the `VK_DELETE` line already at `input.cpp:807-809`, and in
`keyDown` for the four-separate-keydowns route. `typeChars`/`planChar` cannot name `VK_F12`
(`VkKeyScanExA` yields shift state only) — recorded so nobody adds a third check. New
`INPUT_REFUSED_CHORD` → `refused_chord` in `inputStatus` (`steps.cpp:55-69`, whose `default:` is
`"failed"`) with prose in `bridge/prompts/tools.toml`. `MOD_ALT` is 1 in winuser.h and `KEYMOD_ALT`
is 4 here (`input.cpp:430-438`); `MOD_WIN` does not exist on 9x. A person holding Ctrl+Alt+Shift
while the agent taps F12 gets a spurious `refused_chord` — accepted and written down.

**`[ui] indicator`**, default on, on `UiConfig` and `ClientConfig`: `test_ui_lane.py` drives real
step lists through Wine with `DISPLAY` inherited (`conftest.py:110-111`), so without it a test run
puts a real topmost strip on the developer's desktop and grabs a real hotkey. **One test turns it
on** and asserts the window appears; the rest turn it off — a switch whose on-path no test takes is
the failure this project keeps finding.

---

## Measurements, before the design is committed

1. **The trial link — and it must be shape-final, not an empty file.** A stub whose *shape* is the
   real one (both base classes, `registerClass`, `CreateWindowEx`, a `WM_PAINT` that really calls
   `DrawEdge`/`DrawTextA`/`GetTextExtentPoint32A`/`GetStockObject`/`FillRect`, `SetWindowPos`,
   `RegisterHotKey`/`UnregisterHotKey`, a `Thread` with its `GetMessage` loop, the real strings),
   referenced from `uirpc.cpp` **and** `uiops.cpp` where the final design puts the reference. An
   empty TU measures the `-xs` floor and nothing else. Report the delta **per section** the way
   `win/Makefile:191-211` models, because three sections are charged and all three are tight:
   gaston `AUTO` has **150** bytes before its next 512-byte step and `.idata` **146**; gremote
   **330** and **129** — and gremote needs roughly thirteen imports it does not have today
   (`DrawEdge`, `DrawTextA`, `GetTextExtentPoint32A`, `GetStockObject`, `GetSysColor`, `FillRect`,
   `BeginPaint`, `EndPaint`, `InvalidateRect`, `SetWindowPos`, `PostThreadMessageA`,
   `RegisterHotKey`, `UnregisterHotKey`), ~390 bytes, which is one 512-byte charge before a line of
   logic. Against **2,560** bytes of cap slack with **3,360** of DGROUP margin (gaston) and
   **3,072** with ~**2,918** (gremote) — a single `_BSS` page slide exceeds both slacks on its own.
   The same run prints the module list from `uicheck.map` and `autocheck.map`: if `indicator.o` is
   in either, the `DriveOverlay` seam has leaked and the design, not the cap, is wrong.
2. **The guest probe**, headless, one run, `keyprobe`/`focusprobe`-shaped (`Makefile:411-423`):
   `HTTRANSPARENT` and `WindowFromPoint` across threads and across processes, and whether a real
   `mouse_event` reaches the window underneath; **how long after a hide the framebuffer under the
   strip is correct** (the number the screenshot bracket needs — not when `IsWindowVisible` goes
   false); real `WM_TIMER` granularity; `RegisterHotKey` acceptance per modifier/vk and the error
   code when another process owns the chord; that a `SW_SHOWNOACTIVATE` topmost popup never takes
   the foreground, including when clicked; `SetWindowRgn` as the 9x-safe alternative.
3. **The strip's width and position** at 800x600 and 640x480, Small and Large Fonts — computed, then
   seen on the guest once (`research/measurements/largefonts-vm-20261014.txt` is why).

---

## Commits

| | what | tests that move with it |
|---|---|---|
| **E0** | docs: this plan as `docs/stage4b-plan.md`, `ROADMAP.md`'s 4b section rewritten, the three record corrections | — |
| **E1** | the shape-final trial link; go/no-go on budget **and** on the seam | — |
| **E2** | the probe and its guest run | a `probe`-style measurement file |
| **E3** | `DriveOverlay`, `StopSink`, `DriveIndicator`: thread, window, placement, paint, timers, chord, the hide/settle handshake. No callers | audited by no gate until E4 — the `libview` precedent (`Makefile:272-279`), stated |
| **E4** | the client: `ClientApp` owns it, `rc_uirpc_attach` carries it, `NetUiLink::overlay()`, `WM_RC_INTERRUPT` → `doStop` | `msgids.py` (a reserved block, +0x500); `mainwindow.cpp:68`'s now-stale comment; `check-gaston` raise if needed |
| **E5** | gremote: `GremoteApp` owns it, threaded to `RespUiLink`; Stop sets `automation = 0` through the tray; F11 | `test_ui_lane.py`; `check-gremote` raise if needed, with a line in `BACKLOG.md` §29 |
| **E6** | bridge: `on_interrupt` cancels pending ui runs and `turn_stopped` refuses a new one | a test beside `test_stop_kills_a_running_command` (`test_server_integration.py:2190`) |
| **E7** | the step-aside bracket in `StepRunner`, the `m_expectTop` guard | `autocheck` cases + `CASE_COUNT` (`test_guest_automation.py:47`, autocheck's 180) |
| **E8** | the chord denylist in `tapKey` and `keyDown`, `INPUT_REFUSED_CHORD`, `inputStatus`, `tools.toml` | `autocheck` cases ordered **after** the ten `INPUT_REFUSED_SELF` ones (`autocheck.cpp:1272-1325`) + `CASE_COUNT`; a status/prose agreement test on the `test_proc.py:77-89` pattern |
| **E9** | hidden during capture: `Screenshot::capture` and the client's `IDM_ATTSHOT` | |
| **E10** | `[ui] indicator` | `remote/config.cpp` `UI_KEYS` (an unlisted key is refused), `app/config.cpp:19,117,146,302`, `inicheck.cpp:125`'s hand-written comparison, `test_guest_config.py`, the `test_ui_lane.py` INIs + the one on-test |
| **E11** | `uicheck` `refusals()`: the seam is not entered on the three early returns, is entered once around a list that runs, and `driveEnded` runs when the list fails | a counting `FakeUiLink` + `CASE_COUNT` (`test_uirec_agreement.py:72`, uicheck's 57) |
| **E12** | the guest run: placement and width at both font sizes, the chord, Stop by both routes during a real drive, a `SHOT` with the strip up, a full-screen DOS box, two strips at once | `research/measurements/indicator-vm-<date>.txt` + screenshots |
| **E13** | docs: 4b closes — `ROADMAP.md`, `PROGRESS.md` (a 4b section, **and** stage 4's, which `512a93a` never wrote), `BACKLOG.md` §19's `:2366-2389`, `:2488`'s two-driver mechanism, §29, `UIAUTOMATION.md`'s `### Two callers, one engine` + `## Where each piece runs`, `REMOTE.md:275-284,496,539` | — |

`imports.py` needs nothing: every new call is USER32/GDI32, already imported by both exes.

---

## Verification

`make -C win check` (six caps) · `make lint` · `make test-fast` · `make wine-smoke` ·
`pytest tests/python/remote -q`. Build only what changed.

**On the guest (E12), with a pass written down for each:** the strip appears on a real drive and
not on a 50 ms one; it sits where it is supposed to at both font sizes with no clipped text; a
paragraph typed into Notepad under it arrives whole (the keyboard invariant); Stop by button and by
chord each ends the turn, with the strip gone inside a second; a `SHOT` taken while it is up comes
back clean; the chord is refused when the agent taps it; both programs' strips are up at once.

**What a green run will not prove:** the developer's box is not a 640x480 P200, and the two-driver
case is accepted rather than guarded.

---

## Recorded, not fixed

- **A full-screen DOS session owns the display**, so the strip is absent exactly when `DELTREE` is
  being typed, and the chord may not reach a `GetMessage` loop.
- **Two drivers, two strips.** Each is topmost and neither program's `excludePid` excludes the
  other's, so driver A's click under driver B's strip spins `focus()` for `RAISE_SETTLE_MS` and
  returns `INPUT_OCCLUDED`, and A's step-aside cannot hide B's window. One Stop stops one program.
- **The orphan.** `joinOrLeak` abandons unjoined whenever `sessionEnding()` (`core/app.h:304-321`),
  which is every shutdown or logoff — so a shutdown mid-drive always leaves the thread, the window
  and the chord behind. `ExitProcess` follows, so this is a note, not a defect.
- **`UiOps::m_ctx` stays a raw `new`/`delete`**, with the apartment argument written down beside it.

---

## Risks

1. **E1 can end this**, and it now has two ways to: the caps, and `indicator.o` turning up in
   `uicheck.map` or `autocheck.map`. If one half has to go it is **gremote's**, not the client's —
   `remote/config.h:76-83` is explicit that there is nobody at the keyboard there, which is the
   whole premise of that program.
2. **E2 is expected to come back negative on `HTTRANSPARENT`.** The step-aside is budgeted as the
   design; a positive result only removes handshakes.
3. **The step-aside hides on every click under the strip**, and the settle before a capture is a
   measured guess until E12 looks at it. The 250 ms / 1.5 s / 3 s numbers are guesses too, and E12
   is where they earn their place or change.
4. **E6 is in Python, in a file this stage otherwise never touches.** It is also the commit that
   makes Stop true. Skipping it leaves a Stop button that stops one step list.
