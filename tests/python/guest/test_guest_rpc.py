"""04.5: the RPC worker's lane routing, run as the shipping code under Wine.

The guest's worker had one job slot and no name for the difference between "a
command that can run for two minutes" and "a syscall that must never wait on
one." `win/checks/rpccheck.c` links the real `rpc.c` and `frameproto.c` and
drives `rc_rpc_route`/`rc_rpc_submit` directly -- no live bridge needed, see
the comment at the top of that file -- to prove two things: the lane table
classifies each frame type the way the code that reads it actually behaves,
and a frame in the IMMEDIATE lane (EXECKILL) is never held up by the JOB
lane's one slot being busy, even while that slot is demonstrably busy running
something that cannot finish on its own.

This diffs rpccheck's verdicts rather than reading rpc.c and believing it, the
same discipline test_guest_cpp.py uses for the C++ prelude's guards.
"""


import conftest
import pytest

EXE_NAME = "rpccheck.exe"

# Only the genuinely-unsupported machine skips. A missing binary with the
# toolchain present is a build omission and fails loudly -- see
# conftest.guest_exe.
pytestmark = conftest.needs_guest


@pytest.fixture(scope="module")
def verdicts():
    # The busy-JOB case runs a real EXEC (shell=0, cmd.exe named explicitly --
    # see rpccheck.c for why) for up to 8s of its own timeout budget, plus two
    # 600ms settle sleeps either side of the cancel; 60s is generous slack for
    # a slow CI box without hiding a genuine hang.
    return conftest.guest_verdicts(EXE_NAME, timeout=60)


def test_every_case_passes(verdicts):
    bad = {k: v for k, v in verdicts.items() if v[0] != "pass"}
    assert not bad, bad


def test_lane_table_matches_the_codes_actual_behaviour(verdicts):
    """Each row is derived from what the code that reads the frame actually
    does, not from what its name suggests -- EXEC runs for up to two minutes
    (JOB), EXECKILL is answered on the reader thread and never queued
    (IMMEDIATE), the SYNC* family is state-machine input that must never wait
    behind a JOB (STREAM), and anything else is the UI thread's."""
    assert verdicts["lane(EXEC) == JOB"][0] == "pass"
    assert verdicts["lane(EXECKILL) == IMMEDIATE"][0] == "pass"
    # Process list and kill answer on the reader, like EXECKILL: a hung
    # program is found and ended while an EXEC is stuck in the job slot.
    assert verdicts["lane(PSLIST) == IMMEDIATE"][0] == "pass"
    assert verdicts["lane(PSKILL) == IMMEDIATE"][0] == "pass"
    for t in ("SYNCSCAN", "SYNCPUT", "SYNCPUTEND", "SYNCMKDIR", "SYNCGET"):
        assert verdicts["lane(%s) == STREAM" % t][0] == "pass"
    for t in ("TEXT", "READY", "TURN", "BOGUS"):
        assert verdicts["lane(%s) == UI" % t][0] == "pass"


def test_immediate_is_never_refused_for_a_busy_job(verdicts):
    """The property 04.5 exists to establish: a frame in the IMMEDIATE lane is
    never refused, or delayed, because the JOB lane's one slot is busy.

    rpccheck.c proves the "not delayed" half with a filesystem marker a real,
    deliberately-unfinishable EXEC appends to on every pass of its loop --
    chosen because rc_rpc_submit's own return code cannot tell a busy
    rejection from a successful queueing apart (both return 0), so a route
    call that merely returns fast is not enough. The marker keeps growing
    while the JOB is genuinely busy, and stops growing within a couple of
    scheduler ticks of EXECKILL, well before rc_rpc_stop's own cleanup could
    take credit for it.
    """
    assert verdicts["EXECKILL routed while JOB lane busy"][0] == "pass"
    assert verdicts["EXECKILL call itself did not block"][0] == "pass"
    assert verdicts["slow JOB was genuinely running"][0] == "pass"
    assert verdicts[
        "slow JOB stopped promptly after EXECKILL, before rc_rpc_stop"][0] == "pass"


def test_exec_timeout_clamp_is_logged(verdicts):
    """03.15: a timeout_ms above the guest's 120000ms ceiling is still
    clamped -- that part was never in question -- but it now SAYS SO in the
    log. Before this fix nothing distinguished a clamped request from an
    ordinary one, so a bridge that ever asked for more than the guest allows
    (its own DEFAULT_TIMEOUT_MS/MAX_TIMEOUT_MS in tools.py are meant to
    prevent that, but that agreement was held by comments alone) would have
    told the model it "timed out" against a limit it never actually
    requested, with no diagnostic trail on either side.
    """
    assert verdicts["exec timeout clamp is logged, not silent"][0] == "pass"


# --- a refused conditional push takes its temp file with it -----------------
#
# THE one user-visible bug in docs/mem-audit (F1), and the only finding there
# that fired on an ordinary action rather than on an error path.
#
# do_putend closes the temp file's handle before the compare-and-swap check,
# because the commit needs it closed. put_fail's cleanup was keyed on that
# handle still being live, so from that point on its DeleteFileA could never
# run -- and the compare-and-swap is the NORMAL path: if_crc exists precisely
# to refuse a push when the user edited the file underneath, so every edit
# conflict left a fully-written `.gaston-tmp` behind. rc_sync_is_working_name
# makes the scanner skip that name, so nothing reported it and nothing
# removed it until the next start-up swept the tree.
#
# Verified to FAIL against the bug before it was trusted: restoring the old
# `put_fail` calls leaves `f1.txt.gaston-tmp` in the listing after the
# refusal. That check is what separates this from the F2 cases in
# test_guest_agreement.py, which do not discriminate and say so.

def test_a_refused_conditional_push_leaves_no_temp_file(tmp_path):
    out = conftest.run_guest("rpccheck.exe", "--putend",
                             conftest.wine_path(tmp_path),
                             cwd=tmp_path, timeout=120)
    rows: dict[str, set[str]] = {}
    for line in out.stdout.decode("latin-1").splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            rows.setdefault(parts[0], set()).add(parts[1])
    assert rows, out.stderr.decode("latin-1", "replace")[:800]

    after_put = rows.get("after_put", set())
    assert "f1.txt.gaston-tmp" in after_put, (
        f"the put never created its temp file, so this test is not "
        f"exercising what it claims. Saw: {sorted(after_put)}")

    left = {n for n in rows.get("after_refusal", set())
            if n.endswith(".gaston-tmp")}
    assert not left, (
        f"a refused conditional push left {sorted(left)} in the sync tree. "
        f"The scanner skips that name, so nothing will report it and nothing "
        f"will remove it until the next rc_sync_recover on start-up.")


def test_guest_syncrpc_handlers(tmp_path):
    """Audit 04.1: automated verification of rc_syncrpc_handle in the shipping C.

    Executes SYNCPUT, SYNCPUTEND (happy path and CRC/if_crc mismatch refusals),
    SYNCMKDIR, and SYNCGET. Verifies both the C-level assertions and the actual
    filesystem state (atomic file creation, temp file cleanup, directory hierarchy).
    """
    out = conftest.run_guest("rpccheck.exe", "--sync-rpc",
                             conftest.wine_path(tmp_path),
                             cwd=tmp_path, timeout=120)
    verdicts = {}
    for line in out.stdout.decode("latin-1").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] in ("pass", "FAIL"):
            verdicts[parts[1]] = (parts[0], parts[2] if len(parts) > 2 else "")
    assert verdicts, out.stderr.decode("latin-1", "replace")[:800]
    bad = {k: v for k, v in verdicts.items() if v[0] != "pass"}
    assert not bad, bad

    # Python-side filesystem verification of files written by Wine guest:
    put_ok = tmp_path / "put_ok.txt"
    assert put_ok.exists(), "put_ok.txt should have been committed to disk"
    assert put_ok.read_bytes() == b"hello from syncrpc\n"
    assert not (tmp_path / "put_ok.txt.gaston-tmp").exists(), "temp file must be cleaned up"

    assert not (tmp_path / "put_bad.txt").exists(), "corrupted put should not be committed"
    assert not (tmp_path / "put_bad.txt.gaston-tmp").exists(), "corrupted put temp file must be cleaned up"

    nested_dir = tmp_path / "test_dir" / "nested_sub"
    assert nested_dir.is_dir(), "SYNCMKDIR should have created nested directory hierarchy"

