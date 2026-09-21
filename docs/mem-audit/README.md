# Memory and resource management in the guest

An audit of `win/` — where resources are managed by hand, where a scope
object would do it better, and what that would cost on this toolchain.
Requested 2026-09-21. **No code was changed.**

## The short version

The guest is in better shape than the question implies. There is no leak
epidemic: the frame ownership rules across four threads are implemented
correctly at every site, `exec.cpp`'s six handles are closed on all four
paths, the COM conversion in `htmlview.cpp` left nothing hand-managed, and
no hand-written lock pair anywhere has an early return that skips its
`Leave`.

**Nine defects were found. Seven of them are not lifetime bugs at all.**

The two that are — F1 and F2 — are worth the whole exercise, because F1 fires
on an ordinary user action. When the user saves a file between a scan and a
push, the guest correctly refuses the push, and leaves the uploaded temp file
in their sync tree. Every time. The cause is precisely the shape this audit
was asked to look for: **one resource with two release actions, decoupled**.
The handle is closed early for a good reason, and closing it silently disarms
the cleanup that was keyed on it.

The other seven are ordering and synchronisation bugs. No smart pointer fixes
them, and the report says so rather than stretching.

## The finding behind the findings

Every rule this codebase needs is already written down, correctly, in the
transport layer. `net.cpp` and `rpc.cpp` argue in comments about not holding
a lock across a blocking call, not deleting a critical section under a live
thread, and not closing a handle another thread might hold.

`log.cpp` does all three. `syncrpc.cpp` hardens one global against a race and
leaves its neighbour, seventeen lines later, bare. `rc_net_stop` gates four
teardown steps on its join results and then calls a fifth outside the gate.

**The lessons were learned in one file and not propagated.** That is a
different problem from "we should use smart pointers", and it is the more
expensive one.

## The constraint nobody had measured

`rc_cpp.h` says the dialect allows "no templates beyond the trivial". I
probed the compiler under the shipping flags: policy-parameterised class
templates, partial specialisation, member templates and default template
arguments **all compile clean**. The dialect is not the limit.

The real limit is code size, and it is sharper than expected. **Open Watcom
does not inline these destructors at any optimisation level** — not at
`-otexan`, not with `-oe=100`, not with `-oi`. A function with three guards
and five exit paths measured **322 bytes hand-written against 662 guarded**.
The 832 bytes of guard code already in the image are real, and 496 of them
are eight near-identical `ComPtr<T>` instantiations that the design does not
require.

So RAII here is a genuine trade, not a free win. It has to be chosen per
site, and the ranked list in `04-recommendations.md` starts with the one item
that *frees* 370 bytes, to pay for the next three.

## Read in this order

| file | what is in it |
|---|---|
| `02-findings.md` | the nine defects, ranked, each marked verified or reported |
| `04-recommendations.md` | what to adopt, in what order, and **what to leave alone** |
| `03-threads.md` | the four-thread model, every shared variable, what makes each safe |
| `01-measurements.md` | the compiler probe, the guard-cost measurements, the BSS census |
| `00-manifest.md` | exactly which bytes were audited |

If you read one section, make it R7 in `04-recommendations.md` — the places a
guard would be *wrong*. `rc_net_stop` deliberately leaks a socket rather than
close it under a live thread, and a destructor cannot express that. A later
pass working from an RAII checklist would "fix" it into a use-after-free.

## Two caveats

**This was audited against a moving tree.** `00-manifest.md` records the
exact state: `HEAD` was `81bf6c2` and the working tree was dirty in 31 files
that this audit did not touch. Another session was editing `win/` — including
`net.cpp`, `rpc.cpp`, `exec.cpp`, `sync.cpp` and `main.cpp` — **while this
ran**, and its changes are a memory pass of their own (`RC_MAX_INFLIGHT` cut
from 256 to 64, a shared inflate scratch replacing per-frame allocation).
Line numbers here were re-checked against the working tree at capture time
and may have moved since. Every citation gives enough surrounding context to
re-find.

**This is not the BSS discussion.** 88,412 bytes are committed at load and
**one buffer is 75% of it** (`net.cpp:742`, 66,560 bytes). `docs/BACKLOG.md`
§2 already owns that and names the same buffer. Scope guards do not move
BSS — their cost lands in `AUTO`, not `DGROUP` — so the two are separate
problems and are kept separate here.
