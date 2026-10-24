# Reading and driving the Win98 desktop

How `libautomation` works: what a node record holds and where each field
comes from, how controls are addressed, what the platform allows, and which
side of the wire does what.

## The node

One record per window. Flat, streamed, parent by reference — the bridge
assembles the tree, per `REMOTE.md`'s rule that the walk streams records
rather than building one.

```
id        0/3/1        child-index path from the root of this walk
hwnd      0x00A3       this session only, meaningless across one
parent    0/3
class     Edit         GetClassName
ctrl      1001         GetDlgCtrlID, stable inside a dialog
rect      12,40,300,60
visible   1            IsWindowVisible
enabled   1            IsWindowEnabled
focused   1            GetFocus
text      Untitled     SendMessageTimeout(WM_GETTEXT)
role      42           MSAA, OBJID_CLIENT
name      Document     MSAA
value     hello        MSAA — an Edit's contents
action    Press        MSAA accDoDefaultAction name
```

The first nine come from Win32 and are effectively free: enumeration and
`GetClassName` measured at 0 ms per walk over 20 repeats. `text` costs a
cross-process `SendMessageTimeout`. The last four cost an `IAccessible` per
node and are fetched **lazily** — see Enrichment.

### Three sources, in cost order

1. **Win32.** `EnumWindows`/`GetWindow(GW_CHILD)`, class, rect, style,
   visible, enabled, ctrl id. Free, always available, works on apps that
   have never heard of accessibility.
2. **`WM_GETTEXT` via `SendMessageTimeout`.** Cross-process and marshalled
   by USER32, unlike `GetWindowText` which returns only a caption.
   **`LB_GETTEXT`, `LVM_GETITEMTEXT` and `TVM_GETITEM` are not marshalled**
   and `VirtualAllocEx` does not exist on 9x, so list *contents* are never
   a message problem.
3. **MSAA, `OBJID_CLIENT`.** Roles, values, default actions. This is where
   a list box's items live, and where a checkbox's checked state lives.

The injected DLL is a fourth source and is not in the default path. See
Injection.

## Identity and selectors

HWNDs are stable within a session and meaningless across one. So a node is
addressed by a **selector**, resolved fresh on each call.

CSS-like, not XPath. The measured tree is 4 to 6 deep, so XPath's axes buy
nothing, and the thing writing these selectors is a language model, for
which CSS is far better represented than XPath in anything to do with UI.

```
Notepad                      class name
#1001                        control id (GetDlgCtrlID)
Notepad > Edit               direct child
Notepad Edit                 any descendant
[name="Save"]                MSAA name, exact
[text^="Untitled"]           WM_GETTEXT, prefix;  $= suffix, *= contains
[role=pushbutton]            MSAA role, by name
:visible :enabled :focused   state
:nth(2)                      ordinal among the matches so far
```

So `Notepad > Edit:visible`, or `[role=pushbutton][name="OK"]`.

**A selector that matches more than one node is an error**, reported with
the count and the first few matches. It does not pick the first. The whole
point of a control tree over a screenshot is that it is exact, and silently
clicking one of four buttons called "OK" throws that away. Ambiguity is
resolved by the caller adding `:nth(n)` deliberately.

Attribute matching against `name`, `role`, `value` forces MSAA enrichment
for the candidate nodes only, not the whole tree.

## Enrichment without wrecking the machine

**Walk from one root.** One `AccessibleObjectFromWindow` per root buys the
whole subtree, and oleacc's own helper beats an explicit enumerator. It also
returns *more* than the window tree does, because the accessible tree holds
simple elements with no HWND -- which is where list and menu items live.

**Ask Win32 first.** A control's class and style bits are free, and for a
Button the style *is* the role. That classifies about half the windows on a
dialog at roughly a quarter of MSAA's per-node cost. The unclassified
remainder is custom classes, which is exactly where MSAA earns its price.

MSAA costs roughly as much as everything else in a walk put together, and
returned a name for well under half the nodes on a plain Win32 desktop. So:

- **Never on the whole tree.** Enrich the nodes a selector is actually
  deciding between, and the node a caller asked about.
- **`OBJID_CLIENT`.** `OBJID_WINDOW` is the frame and carries nothing: ask
  for it and every node on the desktop comes back as role 9, which reads
  exactly like "MSAA knows nothing on this machine".
- **Visible windows only.** Not an optimisation — walking hidden windows
  left the Start menu stuck open, painted but owned by nothing, and only a
  click on Start itself would clear it. A tool that reports what is on
  screen must not change what is on screen.

  It is also not a cost but a hang. On a Pentium 200 the same desktop walks
  in about 90 ms visible-only and takes **155 seconds** with hidden windows
  included, because asking oleacc about a hidden window can block without
  bound. MSAA itself is only about 3-4x a Win32-only walk; this is a
  different thing entirely. It also halves the work: 45 of
  83 nodes on one measured desktop were invisible.

## Hung windows

Every cross-process read can block on the target's message loop. `WM_GETTEXT`
goes through `SendMessageTimeout` with `SMTO_ABORTIFHUNG`, and the budget
matters more than expected: the same 45-node walk took 72 ms at a 200 ms
budget and 485 ms at 2000 ms **with zero timeouts either way**. The cost
tracks the budget handed over, not how fast the target answers. So: pass the
smallest budget that works.

When a window does fail to answer, the failure is a property of its
**thread**, not that window — so mark the thread and deprioritise
everything on it:

1. normal — nothing known against it
2. later — another thread of a process that has already stalled once
3. last — the thread that actually stalled

One wedged app with thirty controls then costs one timeout, not thirty. On
9x the middle tier is worth having: a 16-bit app holding Win16Mutex blocks
USER and GDI for every thread in the system, so "another thread of the same
process" really is evidence.

The walk is bounded by a **total deadline**, not a node count.
`max_tree_nodes` survives only as a guard against a cyclic `GW_HWNDNEXT`
chain; an idle desktop measured 44 nodes against a proposed cap of 2000, so
the cap was never the thing doing the work.

## Change detection

WinEvents, and they are better than the filesystem watcher this project
already has: `FindFirstChangeNotification` only says something under here
changed, so the sync layer treats polling as the mechanism and the watcher
is an accelerator. WinEvents say **what** changed, **which window**, and
**when**. For the UI, subscription is the mechanism and a rescan is the
fallback.

`SetWinEventHook(..., WINEVENT_OUTOFCONTEXT)`, which delivers on our own
thread through our own message queue. No DLL, no code in anyone else's
process, and nothing to go wrong inside an app we do not own. It needs a
message pump, which the ui thread has anyway.

The tree is **invalidated by event, not by timer**. `OBJ_CREATE`,
`OBJ_DESTROY`, `OBJ_SHOW` and `OBJ_HIDE` dirty the structure;
`OBJ_NAMECHANGE` and `OBJ_VALUECHANGE` dirty one node's text;
`SYS_FOREGROUND` and `OBJ_FOCUS` move a cursor that costs nothing to track.

## Synchronisation

Built on that feed, which 20-of-20 completeness makes safe to wait on. The
ordering is not negotiable:

**Subscribe, then check, then wait.** Checking first races: the thing can
appear between the check and the subscription, and the wait then blocks for
its full timeout on a condition that is already true. This is the single
easiest way to build a primitive that hangs rarely, which is the worst
failure schedule there is.

```
WaitForControl(selector, timeout)   CREATE, SHOW, NAMECHANGE
WaitForText(selector, match, timeout)  NAMECHANGE, VALUECHANGE
WaitForGone(selector, timeout)      DESTROY, HIDE
WaitForIdle(pid, timeout)           WaitForInputIdle — USER32
```

`WaitForInputIdle` is present on Windows 98, but it lives in **USER32, not
KERNEL32**, which is where every example puts it.

Every one returns why it ended — matched, timed out, or the target died —
never a bare boolean. A timeout returns the tree as it stood, so the caller
can see what was there instead.

## Input

`keybd_event` and `mouse_event`. `SendInput` exists on 98 but not on 95 or
NT4 before SP3, so it is resolved with `GetProcAddress` and preferred when
present — it delivers a batch atomically, which matters on a machine a
person may also be touching.

Two rules from prior analysis, both corrections of earlier wrong versions:

- `AttachThreadInput` before a cross-thread `SetFocus`, and **detach
  afterwards**, or the worker permanently inherits the target's input-queue
  state.
- "Never type into our own window" compares
  `GetWindowThreadProcessId(target)`'s **process** against ours and refuses
  the whole process. Comparing the HWND misses a child control, which is a
  different HWND on the same thread.

After `FOCUS`, verify `GetForegroundWindow()` is the target and fail if not,
so `TYPE` never types into the wrong place. Win98 **does** have a
foreground lock — measured at 15000 ms — and
`SPI_SETFOREGROUNDLOCKTIMEOUT` = 0 is accepted and read back as 0, so the
startup mitigation works and `AttachThreadInput` is the fallback, not the
primary.

**Activation is the only route.** `SetForegroundWindow` works from a
background process with no window of its own, and the raised window really
is on top. `BringWindowToTop` and `SetWindowPos(HWND_TOP, SWP_NOACTIVATE)`
are ignored across a process boundary — so the two "quiet raise" calls a
contributor reaches for first do nothing here.

`click` resolves a selector to its rect centre and then performs the same
real `mouse_event` as a coordinate click — one execution path, not two —
then checks `WindowFromPoint` and refuses if something else is on top.

## Screenshots

Capture at `min(screen_bpp, max_bpp)`, wrap in a BMP, deflate, send. The
bridge does depth reduction, palette work, diffing, OCR and PNG.

Region capture is the default; full screen is the exception.

**The wire compresses at level 1.** Deflate beats GDI's own RLE at every
depth where both exist, so there is no `enc=rle8`; and level 6 would be a
2.2x pessimisation on a 200 MHz CPU, where compression is slower than the
10 Mbit link. `max_bpp` stays as a quality knob — 4bpp for driving, native
for looking — and never as a codec choice.

## ImageClick

Give it a bitmap and a tolerance, get a click. It is the bottom of the
ladder, below OCR, for custom-drawn things no tree can see — and the
measurements say it is needed less often than expected, since the Start
button turned out to be a `role=pushbutton` with a `Press` action and a
name, addressable as `[name="Start"]`.

**The scan runs on the bridge.** The guest captures and clicks. A template
search is a transform, the bridge has the memory and the CPU, and the P200
does not. 0.16 measures the guest-side cost anyway, so the decision rests

## Injection

It works, and it is not the default path.

A hook needs its proc in a DLL. With the proc in an EXE, Win98 installs the
hook, **returns a valid handle, and never calls it** — so a return-value
check proves nothing here. With a DLL, three processes mapped it and two ran
the proc.

**`WH_GETMESSAGE` is the hook type that works.** Of the four tried
(`WH_CALLWNDPROC`, `WH_GETMESSAGE`, `WH_CBT`, `WH_CALLWNDPROCRET`) only
`WH_GETMESSAGE` ever delivered; the other three installed cleanly and
produced nothing. It also fires inside `GetMessage`, so the controller has
to *post* as well as send — a sent message alone never wakes it. A named file mapping carries data back, since a DLL's
globals are per-process.

What that buys, now that WinEvents have taken change detection off the list:

- **Text that messages cannot reach.** List box, list view and tree view
  contents, where 9x marshals nothing and `VirtualAllocEx` does not exist.
  Inside the process those are ordinary sends.
- **Readiness**, more precisely than `WaitForInputIdle`.
- **GDI text**, by patching the target's import table — the text as drawn,
  for owner-drawn controls that answer no message at all. **Eight entry
  points**: `TextOutA`/`W` and `ExtTextOutA`/`W` from GDI32, and
  `DrawTextA`/`W` and `DrawTextExA`/`W` from USER32 (`win/inproc/hookdll.c`).
  The W forms and `DrawText*` matter because MSHTML — the one target this
  exists for — draws through `TextOutW`, `ExtTextOutW`, `DrawTextW` and
  `DrawTextA`, and an A-only patch would have captured a web page in part.
  Partial capture is worse than none, because it looks like an answer.

**Unpatch on `DLL_PROCESS_DETACH`, always.** A patched import table points
into this DLL. Remove the hook and the system unmaps us from every process
it injected us into, and that table now points at nothing. The host does not
fall over then — it falls over the next time it draws a character, long
after we are gone, and the crash is filed against it.

**Patching the EXE's import table sees only what the EXE itself draws.**
Notepad's text is painted by the EDIT control inside USER32 and never passes
through `NOTEPAD.EXE`'s imports. That is not a gap to close: reaching it
would mean patching USER32, which on 9x lives in shared memory above 2GB and
is therefore every process on the machine. Standard controls are already
readable by message and by MSAA. This tier exists for the ones that are
not, and it reaches exactly those.

What it costs, against rules that already exist. `REMOTE.md` says no lib
creates windows or owns a message loop, and an injected DLL is code running
inside someone else's message dispatch, which is the most re-entrant place
in the system. `CLAUDE.md` says the guest transforms nothing with exactly
two named exceptions. Memory is per-process and multiplied by every process
on the machine. And an import the DLL cannot resolve crashes **them**, not
us.

So: **opt-in, per-process, for a named target**, never desktop-wide, and
never in the default `TREE` path. Enforced, not merely stated: the shared
mapping carries a `patch_pid` and the controller sets it before `LoadLibrary`,
because `DllMain` decides inside that call. The first version patched every
process the hook reached, which is how Explorer came to be in scope. The tree gets what Win32 and MSAA give
it; injection is what you reach for when a specific application has been
established to be opaque.

## Where each piece runs

| | guest | bridge |
|---|---|---|
| enumerate, read, click, type | ✓ | |
| MSAA enrichment | ✓ | |
| capture, deflate | ✓ | |
| selector matching | ✓ | |
| tree assembly, diffing, OCR, PNG, template search | | ✓ |
| saying so on screen, and stopping it | ✓ | |

The last row is stage 4b and it is the one thing here that is the guest's
ALONE. The bridge cannot say the desktop is being driven -- it is not the
machine with the desktop -- and the person who wants it to stop is sitting in
front of that machine, not this one. So the strip, its Stop button and its
global chord all live in `win/automation/indicator.cpp`, and what reaches the
bridge is the ordinary `cancelled` the caller would have got from a `STOP`.

Selector matching on the guest needs justifying against "the guest
transforms nothing", and the argument is that it is a **filter, not a
transform**: the records it emits are unchanged, it just emits fewer. The
alternative is streaming 83 records per event across a link the project
budgets at 5 KB/s. The compiled selector goes down as a small struct, the
same shape as the sync layer's existing filters.

## The tool surface

`click`, `keys` and `focus` are gone. A client that declares `automation=2`
gets three screen tools:

```
ui     scope, max_nodes, msaa, wait_for, ... ; items_of=<selector>
input  steps=[...], timeout, settle
shot   x, y, w, h
```

**`input` is a JSON list run in order on the guest** (`win/automation/
steps.h`, wire op `UIRUN`), stopping at the first failure. A string types
text with `[Chord]` names; an object is one action: focus, click, close,
down/up (keys and mouse buttons), move, move_by, wait, wait_for, wait_gone,
wait_text, idle. The waits are steps now, not parameters -- the list is the
"do a thing, then look" loop in one round trip.

**Targets, not pixels**, in that order: a control by selector; an `item`
inside it (an MSAA child by name -- list rows, tree nodes, toolbar buttons,
desktop icons); `text` inside a plain Edit (EM_POSFROMCHAR); an `at` offset
from its corner; `screen` pixels last. `ui items_of=<selector>` lists one
control's items, each ending `item="..."` in the form to copy -- measured
answering on both machines for the desktop icons, an Explorer file list, its
folder tree and its toolbar (`research/measurements/input-and-ops-real-hw-
20261002.txt`).

**Matching is loose by default.** `~=` is a case-insensitive glob; a mnemonic
`&` is never text; `*` is any class; `wait_text` and `item`/`text` targets
follow the same rules. More than one match is an error that names up to three
candidates with their windows and suggests an ancestor selector -- never a
guess, and `:nth` only as a last resort.

**The guest keeps three promises.** Nothing stays held: whatever a list
pressed and did not release is released when it ends, fails or is cancelled
(measured: a list that failed with Shift down left the next "x" lowercase).
Typing goes where it was meant: before every key and character the
foreground must be the window the list put in front, or `focus_lost` stops
it. Every step is bounded: 30s per step (120s max), 2 minutes per list (10
max), and `UICANCEL` stops it inside any wait.

**Characters are typed on the TARGET's keyboard layout**: the key
VkKeyScanEx names only if ToAsciiEx agrees it produces that character, a dead
key then Space, else Alt+Numpad 0nnn (`research/measurements/keyboard-layouts-
vm-20261002.txt`). Key names are a frozen, versioned table
(`bridge/agent/keynames.py`, HELLO `keys=`).

## The node record, and why it has three identity fields

There are **three kinds of thing** this library can find, and only the first
is a window:

| `src` | what it is | has | found by |
|---|---|---|---|
| `win` | a real HWND | class, ctrl id, style, text | the window walk |
| `acc` | an accessible element with no window of its own | role, name, value, a parent and a child id | `MsaaSource::enumerateFrom` |
| `gdi` | a text run somebody painted | the string and where it landed | the in-process GDI capture, section D |

A list box's items, a menu's entries and every element of a web page are
`acc`. Probe 0.13f counted 228 accessible nodes against 48 windows on one
desktop and 251 against 50 on another — **that surplus is most of what an
agent wants to click**, and it has no HWND at all. A tree of a web page is
entirely `acc`: the page is one window.

So the record carries all three identities and says which one is real:

```
src     win                  win | acc | gdi
path    0/3/1                child-index path from the root of this walk
hwnd    0x00A34C             src=win only; absent otherwise
child   7                    src=acc only; the id its parent answers for
parent  0/3
class   Button               src=win only
role    pushbutton           either tier; win derives it from class+style free
name    OK                   MSAA
text    OK                   WM_GETTEXT
rect    12,40,300,60
state   focusable
action  press
sel     #32770[text="Save As"] > Button[text="OK"]
```

**`sel` is computed by the bridge, and it is the only thing a caller may
address a control with.** Not `hwnd`: an HWND is meaningless across a session
and 9x reuses them, so a handle that escapes can click whatever inherited it
— silently, on somebody's real desktop. Not `path` either: it survives a
repaint and not a reorder. A stale `sel` is a clean "not found".

Computing it on the bridge keeps the rule that the guest transforms nothing,
and it is the right place for the other reason too: the bridge has the whole
tree in hand and can tell whether `Button[text="OK"]` is unique or needs a
parent step in front of it. The guest, streaming one record at a time, cannot.

What is **not** settled, and is deliberately left open: the selector grammar
has no way to name an `acc` element yet, so those records are readable and
not yet clickable. That is a grammar addition (a step that carries a child
id) and it can happen without touching the record format, which is the whole
point of doing this in this order.

## Where this runs in the client, and what can block it

**A dedicated ui worker, never the paint thread.** `REMOTE.md` already says
so ("ui worker (message queue, `CoInitialize`d)") and the plan quietly
reinterpreted it as the main window thread because that thread happens to
have both. It also has the transcript view: an in-place-active MSHTML
control, `OleInitialize`d for it. Three things go wrong there and only there.

A walk sends `WM_GETTEXT` to every window it finds, and a same-thread
`SendMessage` is a direct call — so walking our own process from inside an
RPC handler re-enters MSHTML during dispatch. `WalkOptions::excludePid` now
exists for exactly this, and the client passes its own pid.

`UiWatch::settle()` dispatches the whole thread queue, which on the main
thread re-enters window procs and can re-enter the RPC handler with the next
inbound frame while a ui op is still in flight. On its own thread it
dispatches only its own queue, which holds nothing but the event feed.

And a wait is seconds long by design. The main thread is the client's
repaint.

So the ui work is a `Service` under `App`'s lifecycle per the project's
standing rules — `init`, `start`, `signalStop`, `join`, created in
`initializeServices()` — owning one thread that creates a message queue,
calls `CoInitialize`, and does nothing else.

**A deadline that can actually interrupt MSAA.** Every budget in the library
so far bounds `SendMessageTimeout`, which was already bounded. The call that
is not is the marshalled `IAccessible` property: `get_accName` against a
process wedged in its own STA blocks with no timeout parameter to give it.
`REMOTE.md:372` names the only mechanism that works — an `IMessageFilter` on
the ui thread whose `MessagePending` returns `PENDINGMSG_CANCELCALL` past the
deadline, with a `SetTimer` so it is called at all. Never `IsHungAppWindow`,
which is 2000+.

### Two callers, one engine

The handlers are no longer the client's. `gremote.exe` answers the same three
ops on its own wire, so `win/ui/uirpc.cpp`'s TREE and RUN moved into
`win/ui/uiengine.cpp` behind an abstract `UiLink` (`win/ui/uilink.h`) and each
exe supplies the framing: the client's `NetUiLink` sends `UIENT`/`UIDAT`/
`UIEND` through `rc_net`, gremote's `RespUiLink` sends `DATA`/`OK`/`ERR`
through a `Responder`. Nothing above the seam spells either vocabulary.

What the engine may now assume about its caller, because it is the only thing
two different programs can both promise:

- **A thread with a message queue, an STA, and a pump.** Both callers force
  the queue into existence before publishing the thread, `CoInitialize` on it,
  and dispatch anything they do not own -- the client in `UiService::body`,
  gremote in `UiOps::onThreadStart` plus the drain in `LaneHandler::idle`.
  Drop the dispatch and `CallDeadline` silently stops working, because
  `MessagePending` is only consulted when a message arrives.
- **A `UiContext` built and destroyed on that same thread.** Every member is
  apartment- or thread-bound; this was already true and is now a contract
  rather than an arrangement.
- **Limits from the caller, not from the file.** The clamps used to be
  literals inside `base_options` and `StepLimits`. They come through
  `UiLink::limits()` now, so the client keeps its compiled-in ceilings and
  gremote's `[ui]` section is not dead config.
- **An automation LEVEL, not a flag.** `UiLink::automationLevel()` returns the
  capability enum, because `RC_AUTOMATION_SCREEN` means read-only and a bool
  would let a step list type into a box configured not to be driven. The
  client never emits that middle value; gremote's ini can.
- **A stop flag it must poll.** `UiLink::stopFlag()` is read between steps,
  inside every wait, at every node of a walk and at every strip of a capture.
  `NodeSink::onNode` and `ByteSink::write` already returned `bool` for this.
- **No `Injector`.** The in-process tier is the client's alone: the member
  moved out of `UiContext` and the `inproc` branch out of the shared `do_tree`
  into each exe's own dispatcher, so gremote neither links `inject.cpp` nor
  pretends to answer for it -- it refuses `inproc` rather than silently
  serving the out-of-process answer, which would read a full list box as
  empty.

- **A driving indicator, or none.** `UiLink::overlay()` (2026-10-14, stage
  4b) returns the strip that says "Gaston is controlling the desktop", or
  null where there is nobody to tell -- a check, a probe, a box whose
  `[ui] indicator` is 0. `rc_ui_run` raises it around the step runner and
  nowhere else, so a request refused at the gates above never raises a window
  claiming the desktop is being driven; the runner gets the same pointer,
  because the strip is a topmost window of our own and a click underneath it
  has to be let through.

  It is the ONE virtual here with a default implementation, and that is
  deliberate: `libautomation` is linked by uicheck, autocheck, keyprobe and
  focusprobe as well as by the two programs, and wlink pulls a library member
  in on any symbol reference. Everything in the engine and the step runner
  calls `DriveOverlay`, which is abstract and header-only
  (`win/automation/overlay.h`), so the window, the thread and the paint code
  stay out of four images that could never raise them. The same rule in the
  other direction is why Stop is a `StopSink` the owning program implements:
  what Stop DOES is an INTERRUPT frame in the client and the tray's
  "automation off" in gremote, and a library may know neither.

`SHOT` is deliberately **not** shared: 55 lines of header clamping over a sink
that has to be per-exe anyway, and a virtual call to save it costs more than
the duplication. Both copies gained the same four lines in stage 4b, which is
the duplication being paid rather than regretted: the strip is taken out of
the frame before the capture and put back after.

## The in-process tier, as built

Section D. Opt-in per request, off by default, and it does **not** fall back
quietly -- `status=` always says which tier answered, because the two give
different answers and only one of them can read a list box. A caller that
asked for the in-process one and silently got the other would conclude a
list is empty.

| | file |
|---|---|
| the backchannel layout, shared by all three sides | `win/inproc/shm.h` |
| the collector that runs inside the target | `win/inproc/collect.c` |
| the DLL: hook, IAT patch, request service | `win/inproc/hookdll.c` |
| the controller: deny list, targeted hook, teardown | `win/ui/inject.*` |
| reachable as `ui(inproc=true)` | `bridge/agent/tools.py` |

**Why it exists, in one line that depends on no measurement.**
`LB_GETTEXT`, `LVM_GETITEMTEXT` and `TVM_GETITEM` take a pointer, and on 9x
nothing marshals it across a process boundary at any clock speed. NT's
answer is `VirtualAllocEx` in the target, which 9x does not have. Inside the
target there is nothing to place. List, tree and list-view contents are
unreachable from outside and ordinary from in here.

The *timing* argument that was once also made for this tier is retracted --
see "And this document broke its own rule". It was never needed.

**What keeps it from being a liability:**

- **One thread**, via `WH_GETMESSAGE` aimed at the target's own. Not a
  system-wide `WH_CBT`, which reaches every process that sees a CBT event --
  that is how Explorer came to be in scope, and how Explorer came to die.
- **A deny list with no force escape**, compared case-insensitively, which
  is the comparison that matters -- `strcmp` would let `explorer.exe`
  through. `KILL` in gremote has a `force=1`; there is deliberately no
  equivalent here, because
  a bad kill is immediate and visible where a bad injection shows up later
  and belongs to the host.
- **Pid and basename.** A 9x pid is an obfuscated pointer into the process
  database and it is reused, so a target that exits can hand its pid to
  something else. A process we cannot name is refused: if we cannot say what
  it is, we cannot say it is not the shell.
- **Never resident.** The hook goes in, the request is served, the hook
  comes out, and the section stops naming anybody -- all inside one call.
- **Nothing on the collector's path allocates**, uses the CRT, uses COM, or
  blocks without a timeout. It runs on a thread borrowed from somebody
  else's message dispatch, and while it runs their program is not painting.
- **`LB_GETTEXT` is asked its length first.** It writes the item and takes
  no capacity, so the only defence against a long row is refusing it. Getting
  that wrong is a stack overwrite inside another program.

## Testing

**Wine settles the grammar and nothing else.** It proves the C logic, the
data structures and the cross-language agreements. It does not prove the
platform, and three divergences are known:

- its oleacc returns the generic client object for every window class, so
  roles and names look wrong in a way the real 98 does not;
- it emits `EVENT_OBJECT_CREATE` and never `EVENT_OBJECT_DESTROY` for a
  hidden popup, which is what the wait backstop exists for;
- it honours `WINEVENT_SKIPOWNPROCESS` in name only.

Anything about timing, the foreground lock, MSAA costs or hidden windows has
to come off real hardware.
