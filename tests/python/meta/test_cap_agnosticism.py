"""Tests that the bridge stays guest-agnostic for paths and names.

The path rules and naming helpers used to have Win9x limits (like 8.3 limits, 
DOS illegal chars) baked directly into their logic as hardcoded strings or numbers.
This test ensures that the bridge consults declared capabilities rather than 
assuming a constrained filesystem for everyone.

This is the test that would have caught the original defect described in 
`folder_max`'s docstring -- where a shortnames client asking in writing for 
restrictive rules was answered with a hardcoded assumption that everyone gets.
"""

import inspect
from bridge.files import caps as C
from bridge.files import paths as P
from bridge.server import Bridge

def test_naming_and_path_helpers_consult_caps():
    caps_short = C.CONSTRAINED
    caps_long = C.POSIX

    # 1. folder_max
    assert Bridge.folder_max(caps_short) == 8
    assert Bridge.folder_max(caps_long) > 8, "folder_max must scale with capabilities"

    # 2. folder_name
    long_name = "MyLongFolderName"
    short_res = Bridge.folder_name(long_name, caps_short)
    long_res = Bridge.folder_name(long_name, caps_long)
    
    assert short_res == "MYLONGFO", "Shortnames client must truncate and uppercase"
    assert long_res == "MyLongFolderName", "Longnames client must preserve name and case"
    
    # 3. illegal_chars
    illegal_short = P.illegal_chars(caps_short)
    illegal_long = P.illegal_chars(caps_long)
    
    assert ">" in illegal_short and ":" in illegal_short, "DOS illegal chars should be present for short caps"
    assert ">" not in illegal_long and ":" not in illegal_long, "DOS illegal chars should NOT be present for long caps"

def test_no_hardcoded_assumptions_in_code():
    """Negative check: verify that a hardcoded '8' or '[:8]' would fail.
    
    The guard must be able to go red if someone reverts to the old behaviour
    of hardcoding these limits in the server module.
    """
    source = inspect.getsource(Bridge.folder_name)
    assert "[:8]" not in source, "Do not hardcode slice limits, use Bridge.folder_max(caps)"
    
    # Also verify the test itself didn't just pass by coincidence if the source had hardcoded values
    assert ("[:folder_max(caps)]" in source or
            "[:Bridge.folder_max(caps)]" in source), "The slice should use the caps-aware folder_max"
