# Reading and driving the Win98 desktop

Design for `libautomation`, written after the Phase 6 probes rather than
before them. Every number here was measured on the guest; where a number is
from the emulator and not the P200 it says so, because the emulator runs on
a modern CPU and its milliseconds are a statement about a Ryzen.

`docs/PLAN.md` Phase 6 has the product argument for why the control tree
beats a screenshot. This file is the mechanism.

## What the probes settled

| | result | probe |
|---|---|---|
| `oleacc.dll` on Win98 SE | present, `AccessibleObjectFromWindow` works | 0.13 |
| MSAA object to ask for | **`OBJID_CLIENT`**, never `OBJID_WINDOW` | 0.13c |
| `SetWinEventHook` | present, delivers **cross-process, no DLL** | 0.15/0.15b |
| WinEvent completeness | **20 of 20** creates produced their event | 0.15c |
| WinEvent granularity | 11 distinct semantic types in one app lifetime | 0.15c |
| system-wide hook, proc in an EXE | installs, **delivers nothing** | 0.15b |
| system-wide hook, proc in a DLL | **injects**; 3 processes mapped it | 0.15d |
| cross-process channel | named file mapping, works from inside the DLL | 0.15d |
| `WaitForInputIdle` | present — in **USER32**, not KERNEL32 | 0.15 |
| walking hidden windows | **moves the desktop**; walk visible only | 0.13c |

Two of those are traps rather than features and are worth stating twice.
`SetWindowsHookEx` returns a valid handle for a hook proc in an EXE and then
never calls it, so a return-value check proves nothing. And asking MSAA for
`OBJID_WINDOW` reports role 9 for every node on the desktop, which reads
exactly like "MSAA knows nothing here".

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
   a message problem (`PLAN.md`).
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

Measured on the P200, and the ordering is not what the emulator said.

| traversal | us/node |
|---|---|
| `AccessibleObjectFromWindow` per HWND | 8500 |
| one root + `AccessibleChildren` | **2078** |
| one root + `IEnumVARIANT::Next` | 2555 |

**Walk from one root.** That is the 4x, and it is the single
`AccessibleObjectFromWindow` that buys it, not the batching call underneath
-- oleacc's own helper beat an explicit enumerator here, where the emulator
had said the reverse. It also returns more: 228 nodes against 48 windows,
because the accessible tree holds simple elements that have no HWND, which
is where list and menu items live.

**Ask Win32 first.** A control's class and style bits are free, and for a
Button the style IS the role. With a dialog on screen that classified 46 of
82 windows at 731 us/node against MSAA's 3048 for the same set. The
unclassified remainder is custom classes, which is exactly where MSAA earns
its cost.

**Treat a single measurement as noise.** MSAA came out at 18.3, 8.5, 5.7 and
3.0 us/node across four runs on the same machine, depending on what was on
screen. Ratios measured inside one run are the only comparable thing.

**And this document broke its own rule.** Corrected 2026-09-26, after a
review went back to the raw files.

The claim was "MSAA does not get faster on a faster machine" -- 250 ms on
the P200 against 289 ms on a machine with four times the inflate throughput
-- and the whole case for in-process injection was hung on it. Those two
numbers are probe 0.13g on two different runs, on desktops with different
window counts (82 against 79) and different applications open. That is
exactly the cross-run ratio the paragraph above forbids.

Every other MSAA pair in the same two files says the opposite:

| probe | P200 | XP 2000+ | ratio |
|---|---|---|---|
| 0.13b `E + msaa`, the whole walk | 467 ms / 82 | **88 ms / 79** | **5.3x** |
| 0.13f A, per-HWND | 8500 us/node | 4460 us/node | 1.9x |
| 0.13f C, `IEnumVARIANT` | 2555 us/node | 463 us/node | 5.5x |
| 0.13e A, create + release | 10 ms | 1 ms | ~10x |

0.13b E and 0.13g measure nearly the same thing and disagree by five times.
Worse, 0.13f A and 0.13g are both per-HWND `AccessibleObjectFromWindow` **on
the same machine in the same session** and differ by 2.8x. The honest
reading is that MSAA does scale with the processor, roughly two to five
times, and that 0.13g on the P200 caught an unusually light desktop.

So **"the boundary, not the processor" is not established**, and nothing
should be built on it until a probe runs 0.13f and 0.13g back to back in one
invocation, on a scripted identical set of windows, and reports the ratio
measured *inside* that run.

What this does NOT weaken is the reason to get inside a process at all:
`LB_GETTEXT`, `LVM_GETITEMTEXT` and `TVM_GETITEM` are **not marshalled on
9x at any clock speed**. List, tree and list-view contents are unreachable
from outside however fast the machine is. That is a capability argument, it
depends on no timing, and it is the one to lead with.


MSAA costs roughly as much as everything else in a walk put together, and
returned a name for well under half the nodes on a plain Win32 desktop. So:

- **Never on the whole tree.** Enrich the nodes a selector is actually
  deciding between, and the node a caller asked about.
- **`OBJID_CLIENT`.** `OBJID_WINDOW` is the frame and carries nothing.
- **Visible windows only.** Not an optimisation — walking hidden windows
  left the Start menu stuck open, painted but owned by nothing, and only a
  click on Start itself would clear it. A tool that reports what is on
  screen must not change what is on screen. It also halves the work: 45 of
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
changed, so `PROGRESS.md` settled on "polling is the mechanism, the watcher
is an accelerator". WinEvents say **what** changed, **which window**, and
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

`click` resolves a selector to its rect centre and then performs the same
real `mouse_event` as a coordinate click — one execution path, not two —
then checks `WindowFromPoint` and refuses if something else is on top.

## Screenshots

Capture at `min(screen_bpp, max_bpp)`, wrap in a BMP, deflate, send. The
bridge does depth reduction, palette work, diffing, OCR and PNG.

Measured over four bands of one screen, deflate beat GDI's own RLE at every
depth where both exist, and deflate on raw 16bpp beat RLE8 on quantised
8bpp outright. So `enc=rle8` is not needed and `BACKLOG.md` §8 closes as a
no. `max_bpp` remains as a quality knob — 4bpp for driving, native for
looking — and never as a codec choice.

Region capture is the default; full screen is the exception.

## ImageClick

Give it a bitmap and a tolerance, get a click. It is the bottom of the
ladder, below OCR, for custom-drawn things no tree can see — and the
measurements say it is needed less often than expected, since the Start
button turned out to be a `role=pushbutton` with a `Press` action and a
name, addressable as `[name="Start"]`.

**The scan runs on the bridge.** The guest captures and clicks. A template
search is a transform, the bridge has the memory and the CPU, and the P200
does not. 0.16 measures the guest-side cost anyway, so the decision rests
on a number; it picks the most varied block on screen to time against,
because a flat one matches at the first position and reports a meaningless
zero.

## Injection

It works, and it is not the default path.

A system-wide hook needs its proc in a DLL — with the proc in an EXE, Win98
installs the hook and never calls it. With a DLL, three processes mapped it
and two ran the proc. A named file mapping carries data back, since a DLL's
globals are per-process.

What that buys, now that WinEvents have taken change detection off the list:

- **Text that messages cannot reach.** List box, list view and tree view
  contents, where 9x marshals nothing and `VirtualAllocEx` does not exist.
  Inside the process those are ordinary sends.
- **Readiness**, more precisely than `WaitForInputIdle`.
- **GDI text**, by patching `TextOutA`/`ExtTextOutA` in the target's import
  table — the text as drawn, for owner-drawn controls that answer no
  message at all. Built and working: a draw came back as
  `pid … @7,9 "GASTON-SELFTEST"`.

Two things that cost an Explorer to learn.

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

Selector matching on the guest needs justifying against "the guest
transforms nothing", and the argument is that it is a **filter, not a
transform**: the records it emits are unchanged, it just emits fewer. The
alternative is streaming 83 records per event across a link the project
budgets at 5 KB/s. The compiled selector goes down as a small struct, the
same shape as the sync layer's existing filters.

## What only the P200 can answer

Every millisecond above is from the emulator. The kit in
`tools/gen/mkw98kit.sh` asks these on hardware:

- the walk, with and without MSAA, at a real clock
- WinEvent latency when the CPU is the bottleneck rather than the emulator
- deflate throughput — `PROGRESS.md` marks gate 0.5c provisional for exactly
  this reason
- whether a template scan is tolerable on the guest after all
- whether the foreground lock and injection behave the same on hardware as
  under TCG

## The tool surface

Decided with the user on 2026-09-26, before any of it was built, because the
shape of the MCP tools decides what the guest has to put on the wire.

Four tools go into the existing `machine` server beside `exec`,
`sync_status` and `sync_resolve`:

```
ui     scope=desktop|foreground|pid|<selector>, max_nodes, text, wait_for, timeout
click  target=<selector>, button, double, settle
keys   target=<selector>, text
shot   rect, scale
```

**A control is addressed by a selector the bridge computed, never by a
handle.** Each node record carries a `sel=` field: the minimal selector that
resolves to that one control and nothing else. The model copies it verbatim
into `click`. An HWND is meaningless across a session and 9x reuses them, so
letting one out means a stale reference can click whatever inherited that
handle -- silently, and on somebody's real desktop. With a selector a stale
reference is a clean "not found". Computing it on the bridge keeps the rule
that the guest transforms nothing.

**The waits are parameters, not a fifth tool.** `WaitForControl`, `Text`,
`Gone` and `Idle` exist in the library (`win/automation/wait.h`), but the
agent reaches them through `click(target, settle=true)` and
`ui(wait_for=..., timeout=...)`. The loop an agent actually runs is "do a
thing, then look", and folding the wait into both ends of it turns three
round trips into one. On a 200MHz guest behind a socket, a round trip is the
expensive part.

**`shot` returns the image.** That is what it is for, and the tree is what
keeps it from being needed: a 640x480 frame costs more tokens than a whole
control tree and says less about it. The system prompt steers to `ui` first,
so `shot` is what you reach for when the tree came back empty -- a
custom-drawn control, a game, an installer that is one big window.


## The node record, and why it has three identity fields

Settled 2026-09-26, before the first byte went on the wire, because a record
format is the one thing that cannot be changed quietly later.

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

**Why this is written before section B and not during it.** `src` and `child`
cost nothing today — every record is `src=win` until the in-process collector
lands — and adding them later would mean re-cutting the wire format, the op
table, the bridge's tree assembler, the `sel` computation and every selector
an agent has already learned, all at once. The fields go in now so that the
tier that needs them is a new value in an existing field rather than a flag
day.

What is **not** settled, and is deliberately left open: the selector grammar
has no way to name an `acc` element yet, so those records are readable and
not yet clickable. That is a grammar addition (a step that carries a child
id) and it can happen without touching the record format, which is the whole
point of doing this in this order.
