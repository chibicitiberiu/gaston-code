"""Verifies the audit ledger invariants: valid verdict tokens, sequence continuity, and no dropped findings.

Findings in notes/audit/*.md and notes/audit-2/*.md must use one of the four
verdict tokens: Fixed, Stale, Deferred, Partial (see notes/audit-2/00-audit-method.md:136-139).
Deferred findings must cite the BACKLOG section they were moved to.
Finding IDs within each audit document must form a continuous sequence with no gaps
(closing finding 00.2, which caught 01.5 and 01.9 missing from the ledger).
"""

import glob
import os
import re

import conftest
import pytest

# The audits are working notes rather than published docs, so they live in
# the gitignored `notes/` tree. A fresh clone has no `notes/`, and these
# tests then skip instead of failing: they police a document that is not
# part of what ships.
NOTES = os.path.join(conftest.REPO, "notes")
AUDIT_DIR = os.path.join(NOTES, "audit")
AUDIT2_DIR = os.path.join(NOTES, "audit-2")

pytestmark = pytest.mark.skipif(
    not os.path.isdir(AUDIT2_DIR),
    reason="notes/audit-2 is not in this checkout; the audits are not published",
)

VALID_TOKENS = {"Fixed", "Stale", "Deferred", "Partial"}

VERDICT_LINE_RE = re.compile(
    r"^([A-Za-z]+):\s+([0-9]{2}\.[0-9]+[a-z]?)\b", re.M
)

AUDIT_FILES_WITH_LEDGERS = {
    "01-guest-c.md": ("01", 0, 63, True),     # prefix, min_id, max_id, allows_01_0b
    "02-bridge-python.md": ("02", 1, 48, False),
    "03-contracts.md": ("03", 1, 56, False),
    "04-planned-features.md": ("04", 1, 35, False),
}

AUDIT2_HEADER_RANGES = {
    "00-audit-method.md": ("00", 1, 4),
    "01-guest.md": ("01", 1, 38),
    "02-bridge.md": ("02", 1, 18),
    "03-requirements.md": ("03", 1, 25),
    "04-verification.md": ("04", 1, 16),
    "05-sync-layer.md": ("05", 1, 6),
    "06-protocol.md": ("06", 1, 7),
}


def test_audit_verdict_tokens():
    """Every finding resolution line across notes/audit and notes/audit-2 must use a valid token."""
    pattern = os.path.join(NOTES, "audit*", "*.md")
    files = sorted(glob.glob(pattern))
    assert len(files) >= 10, f"Expected audit markdown files, found {len(files)}"

    for path in files:
        rel = os.path.relpath(path, conftest.REPO)
        with open(path, encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                m = VERDICT_LINE_RE.match(line)
                if m:
                    token, finding_id = m.groups()
                    assert token in VALID_TOKENS, (
                        f"{rel}:{line_no}: Invalid verdict token '{token}:' for finding {finding_id}. "
                        f"Must be one of: {sorted(VALID_TOKENS)}"
                    )


def test_deferred_findings_cite_backlog():
    """Every Deferred: finding line must cite BACKLOG (per 00-audit-method.md:136-139)."""
    pattern = os.path.join(NOTES, "audit*", "*.md")
    for path in sorted(glob.glob(pattern)):
        rel = os.path.relpath(path, conftest.REPO)
        with open(path, encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                if line.startswith("Deferred:"):
                    assert "BACKLOG" in line, (
                        f"{rel}:{line_no}: Deferred finding does not cite BACKLOG: {line.strip()[:80]}"
                    )


@pytest.mark.parametrize("filename,config", sorted(AUDIT_FILES_WITH_LEDGERS.items()))
def test_audit_ledger_sequence_continuity(filename, config):
    """The 2026-09-18 audit ledgers must have no gaps in their finding sequences."""
    prefix, min_id, max_id, allows_0b = config
    path = os.path.join(AUDIT_DIR, filename)
    assert os.path.exists(path), f"File {filename} missing"

    with open(path, encoding="utf-8") as f:
        text = f.read()

    verdicts = VERDICT_LINE_RE.findall(text)
    ids = [v[1] for v in verdicts]

    # All IDs must be unique
    duplicates = [fid for fid in ids if ids.count(fid) > 1]
    assert not duplicates, f"{filename} contains duplicate finding IDs: {set(duplicates)}"

    # Parse numeric values
    int_ids = set()
    for fid in ids:
        assert fid.startswith(f"{prefix}."), f"{filename}: Unexpected finding ID {fid}"
        suffix = fid.split(".", 1)[1]
        if suffix.endswith("b") and allows_0b:
            continue
        int_ids.add(int(suffix))

    expected = set(range(min_id, max_id + 1))
    missing = expected - int_ids
    assert not missing, (
        f"{filename}: Missing finding IDs in sequence {min_id}..{max_id}: "
        f"{sorted(f'{prefix}.{n}' for n in missing)}"
    )


@pytest.mark.parametrize("filename,config", sorted(AUDIT2_HEADER_RANGES.items()))
def test_audit2_findings_sequence_continuity(filename, config):
    """Findings headers in notes/audit-2/*.md must form a continuous sequence with no gaps."""
    prefix, min_id, max_id = config
    path = os.path.join(AUDIT2_DIR, filename)
    assert os.path.exists(path), f"File {filename} missing"

    with open(path, encoding="utf-8") as f:
        text = f.read()

    header_ids = re.findall(r"^#{2,3}\s+([0-9]{2}\.[0-9]+)\b", text, re.M)
    int_ids = {int(h.split(".", 1)[1]) for h in header_ids if h.startswith(f"{prefix}.")}

    expected = set(range(min_id, max_id + 1))
    missing = expected - int_ids
    assert not missing, (
        f"{filename}: Missing finding header IDs in sequence {min_id}..{max_id}: "
        f"{sorted(f'{prefix}.{n}' for n in missing)}"
    )


def test_audit2_resolved_reports_cover_all_findings():
    """For any audit-2 report with a resolution section, every finding must have a verdict line."""
    for filename in sorted(os.listdir(AUDIT2_DIR)):
        if not filename.endswith(".md") or filename == "README.md":
            continue
        path = os.path.join(AUDIT2_DIR, filename)
        with open(path, encoding="utf-8") as f:
            text = f.read()

        verdicts = VERDICT_LINE_RE.findall(text)
        if not verdicts:
            # Report not resolved yet
            continue

        header_ids = set(re.findall(r"^#{2,3}\s+([0-9]{2}\.[0-9]+)\b", text, re.M))
        verdict_ids = {v[1] for v in verdicts}

        # Every header finding must have a verdict line
        unresolved = header_ids - verdict_ids
        assert not unresolved, (
            f"{filename}: Resolution section is missing verdicts for: {sorted(unresolved)}"
        )
