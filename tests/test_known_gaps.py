"""
Regression test tracking the identity-key round-trip fragility flagged
in the README's Known Gaps section. Marked xfail, not skipped and not
silently passing — this is a real, tracked bug, and this test exists so
CI keeps reminding us it's unfixed rather than letting it go quiet.

If this test starts passing, the fix has landed — flip it to a normal
passing test and remove the xfail marker, don't just delete this file.
"""

import pytest

from vor_agents.enrichment import _doc_id


@pytest.mark.xfail(
    reason="Known gap, see README 'Known gaps' #1: _doc_id joins on '_', "
    "and _fetch_all_suppressed_patterns() splits on the same character to "
    "reconstruct the identity key. A key component containing an "
    "underscore breaks the round-trip.",
    strict=True,
)
def test_identity_key_roundtrip_survives_underscore_in_component():
    key_with_underscore = (
        "Sigma_Rule_With_Underscores",
        "w3wp.exe",
        "csc.exe",
        "ToolPane_admin",
    )
    doc_id = _doc_id(key_with_underscore)
    reconstructed = tuple(doc_id.split("_"))

    assert reconstructed == key_with_underscore
