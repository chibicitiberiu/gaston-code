# The scripts

| folder | what lives there |
|---|---|
| `vm/` | drive the guest from outside it — QMP, screenshots, the DOS box, shipping a build |
| `gen/` | generate what ships — the icon, the spinner, the CD images |
| `check/` | the static gates `make check` runs against the built PE |
| `probe/` | bridge stubs that drive the guest's own code directly |

Two shared modules, both about not counting directory levels by hand:

- **`toolkit.py`** — `REPO`, `on_path()`, `repo_path()`, `DOMAIN`, `URI`.
- **`vmlib.sh`** — the same for the shell scripts. Source it, don't run it.

Both **find** the repo root by walking up for markers rather than counting
`..` from `__file__`. Nine Python tools and six shell scripts each counted
one level, in five different spellings, so all fifteen would have broken the
day these files moved into folders — which is the day this was written.

## The ones you actually run

    tools/vm/runvm.sh run            start the VM by hand, QMP on vm/qmp.sock
    tools/vm/vmship.sh               rebuild tools.iso and insert it
    tools/vm/qmp.py click 208 190    drive the guest: shot, key, type, click
    tools/vm/dos.py "DIR C:\\"        type into the MS-DOS box, focus checked first
    tools/vm/vmshot.sh out.png       screenshot as PNG

`qmp.py` is the interesting one. The guest's mouse is PS/2, so it is
relative and every move is arithmetic — see the acceleration table in its
`_walk`, and `tests/python/tools/test_qmp_pointer.py`, which models the
measured guest so the constants cannot rot unnoticed.

## The probes

A probe stands in for `bridge/server.py`: it accepts the one guest and
drives some corner of its code. Start one **instead of** the bridge, then
launch the client on the guest.

    probe/syncprobe.py     the sync state machine, 54 checks. Load-bearing —
                           cited from bridge/files/manifest.py and from
                           tests/python/guest/test_guest_agreement.py
    probe/viewprobe.py     renders one block of every kind, to be looked at
    probe/barprobe.py      the permission/halt bar state stack
    probe/drainprobe.py    queues sync work, then watches shutdown drain

`probelib.py` holds the handshake all four share. Their receive loops differ
and stay where they are.

## Wired into the build

Only `check/cmovcheck.py` and `check/stackframes.py`, from `win/Makefile`'s
`check` target. `gen/mktools-iso.sh` is invoked by `vm/vmship.sh`. Nothing
else runs from a Makefile — the rest are run by hand.
