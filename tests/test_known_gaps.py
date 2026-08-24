"""
Regression test tracking the identity-key round-trip fragility flagged
in the README's Known Gaps section — FIXED, see docs/TODO-Aug15.md Task 3.

Was xfail(strict=True) pending the fix, per this file's original intent:
"If this test starts passing, the fix has landed — flip it to a normal
passing test and remove the xfail marker, don't just delete this file."
Doing exactly that here rather than deleting the file, so the regression
coverage for the underscore-in-component case stays in place.
"""

from vor_agents.enrichment import _doc_id


def test_identity_key_roundtrip_survives_underscore_in_component():
    """
    _doc_id() is now a content hash of the identity_key tuple, not a
    "_"-joined string — there's no split-based reconstruction to break,
    and no ambiguity between differently-shaped keys that used to
    collide. Exercises the exact collision case flagged in the code
    review: ("a", "b_c"), ("a_b", "c"), and ("a", "b", "c") all used to
    join to the same "a_b_c" doc ID.
    """
    assert _doc_id(("a", "b_c")) != _doc_id(("a_b", "c"))
    assert _doc_id(("a", "b_c")) != _doc_id(("a", "b", "c"))
    assert _doc_id(("a_b", "c")) != _doc_id(("a", "b", "c"))

    # Same key, called twice -> same doc ID (deterministic, not random).
    assert _doc_id(("a", "b_c")) == _doc_id(("a", "b_c"))
