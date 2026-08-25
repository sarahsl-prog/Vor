# Blast-Radius Table: Firestore-Backed + Commit Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `BLAST_RADIUS_TABLE` from an in-code Python dict to a Firestore-backed collection, and give `propose_blast_radius()`'s output a real commit path — CRITICAL/HIGH proposals auto-commit, MEDIUM/LOW sit pending until a human calls a new `POST /blast-radius/commit` endpoint.

**Architecture:** `estimate_blast_radius()` reads from a TTL-cached Firestore collection instead of the module dict. `propose_blast_radius()` writes every proposal to a `blast_radius_proposals` collection and auto-commits the conservative-direction tiers immediately. A new `commit_blast_radius_proposal()` function (called from the new endpoint) commits a pending MEDIUM/LOW proposal on explicit human action.

**Tech Stack:** Python 3.13, `google-cloud-firestore`, FastAPI, `loguru`, existing `FakeFirestoreClient` test double (extended with `.stream()` on a bare collection).

**Spec:** `docs/superpowers/specs/2026-08-24-blast-radius-firestore-design.md`

## Global Constraints

- `estimate_blast_radius()` and `propose_blast_radius()` both gain a required `firestore_client` param — every call site across the codebase (`orchestrator.py`, tests) updates in the same task that changes the signature, per CLAUDE.md's "check for callers before changing signatures."
- CRITICAL/HIGH proposals commit immediately (the conservative direction, matches `BLAST_RADIUS_PLAYBOOK.md`'s existing "may be added directly" language) — MEDIUM/LOW never auto-commit, full stop.
- The table cache must degrade safely on a Firestore failure: serve a stale cache if one exists, fall back to `UNSCORED_DEFAULT` for every lookup if it's never been populated at all — never raise out of `estimate_blast_radius()`.
- `UNSCORED_DEFAULT` behavior (an unmatched alert defaults to HIGH, never LOW) is unchanged by this plan.
- No changes to `TIER_RANGES`/tier validation logic in `propose_blast_radius()` — storage and commit workflow only.

---

## Task 1: `FakeFirestoreClient` gains `.stream()` on a bare collection

**Files:**
- Modify: `tests/conftest.py`

**Interfaces:**
- Produces: `_FakeCollection.stream() -> Iterator[_FakeDocSnapshot]` — yields every doc in the collection regardless of field values (unlike the existing `.where(...).stream()`, which filters first). Consumed by Task 2's `estimate_blast_radius()`.

- [x] **Step 1: Write the failing test**

Add to `tests/conftest.py`, right after the existing `FakeFirestoreClient` tests would live — actually there's no dedicated fixture test file, so add this as a small inline sanity check in a new `tests/test_conftest_fakes.py`:

```python
"""Sanity tests for the fake GCP client test doubles in conftest.py
itself -- these aren't testing vor_agents code, just the fakes other
tests depend on."""


def test_fake_collection_stream_yields_every_doc(fake_firestore):
    fake_firestore.collection("things").document("a").set({"x": 1})
    fake_firestore.collection("things").document("b").set({"x": 2})

    docs = list(fake_firestore.collection("things").stream())

    assert {doc.id for doc in docs} == {"a", "b"}
    assert {doc.to_dict()["x"] for doc in docs} == {1, 2}


def test_fake_collection_stream_empty_collection_yields_nothing(fake_firestore):
    assert list(fake_firestore.collection("empty").stream()) == []
```

- [x] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_conftest_fakes.py -v`
Expected: `AttributeError: '_FakeCollection' object has no attribute 'stream'`

- [x] **Step 3: Add `.stream()` to `_FakeCollection`**

In `tests/conftest.py`, add a method to the existing `_FakeCollection` class (alongside `.document()` and `.where()`):

```python
    def stream(self):
        for doc_id, data in self._store.items():
            snap = _FakeDocSnapshot(data)
            snap.id = doc_id
            yield snap
```

- [x] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_conftest_fakes.py -v`
Expected: PASS.

- [x] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: same pass count as before plus 2 new tests — nothing else uses this method yet, so no other behavior changes.

- [x] **Step 6: Commit**

```bash
git add tests/conftest.py tests/test_conftest_fakes.py
git commit -m "FakeFirestoreClient: add stream() on a bare collection"
```

---

## Task 2: `estimate_blast_radius()` reads from a TTL-cached Firestore table

**Files:**
- Modify: `vor_agents/blast_radius.py`
- Modify: `tests/test_blast_radius.py`

**Interfaces:**
- Consumes: `_FakeCollection.stream()` (Task 1).
- Produces: `estimate_blast_radius(alert: dict[str, Any], firestore_client: Client) -> float` (signature change — gains required `firestore_client`). `BLAST_RADIUS_TABLE_COLLECTION = "blast_radius_table"`. `reset_table_cache()` (test-only reset hook, module-public). Consumed by Task 6 (`orchestrator.py`).

- [x] **Step 1: Write the failing tests**

Add to `tests/test_blast_radius.py` (keep every existing test in the file — they'll be updated for the new signature in this same task, not removed):

```python
from vor_agents.blast_radius import (
    BLAST_RADIUS_TABLE_COLLECTION,
    UNSCORED_DEFAULT,
    estimate_blast_radius,
    reset_table_cache,
)


class TestEstimateBlastRadiusFromFirestore:
    def setup_method(self):
        reset_table_cache()

    def _seed_entry(self, fake_firestore, indicator_type, value, score):
        fake_firestore.collection(BLAST_RADIUS_TABLE_COLLECTION).document(
            f"{indicator_type}:{value}"
        ).set({"indicator_type": indicator_type, "value": value, "score": score})

    def test_matches_seeded_entry(self, fake_firestore):
        self._seed_entry(fake_firestore, "parent_image", "lsass.exe", 0.95)

        result = estimate_blast_radius({"parent_image": "lsass.exe"}, fake_firestore)

        assert result == 0.95

    def test_no_match_falls_back_to_unscored_default(self, fake_firestore):
        self._seed_entry(fake_firestore, "parent_image", "lsass.exe", 0.95)

        result = estimate_blast_radius({"parent_image": "notepad.exe"}, fake_firestore)

        assert result == UNSCORED_DEFAULT

    def test_worst_case_wins_on_multiple_matches(self, fake_firestore):
        self._seed_entry(fake_firestore, "parent_image", "w3wp.exe", 0.75)
        self._seed_entry(fake_firestore, "endpoint_family", "ToolPane_admin", 0.95)

        result = estimate_blast_radius(
            {"parent_image": "w3wp.exe", "endpoint_family": "ToolPane_admin"}, fake_firestore
        )

        assert result == 0.95

    def test_cache_serves_repeated_calls_without_rereading(self, fake_firestore):
        self._seed_entry(fake_firestore, "parent_image", "lsass.exe", 0.95)
        estimate_blast_radius({"parent_image": "lsass.exe"}, fake_firestore)

        # Mutate the underlying store directly (bypassing the table's own
        # write path) -- if the cache is honored, this change is invisible
        # until the cache expires/is invalidated.
        fake_firestore.collection(BLAST_RADIUS_TABLE_COLLECTION).document(
            "parent_image:lsass.exe"
        ).set({"indicator_type": "parent_image", "value": "lsass.exe", "score": 0.10})

        result = estimate_blast_radius({"parent_image": "lsass.exe"}, fake_firestore)

        assert result == 0.95  # still the cached value, not the mutated one

    def test_stale_cache_served_on_refresh_failure(self, fake_firestore, monkeypatch):
        self._seed_entry(fake_firestore, "parent_image", "lsass.exe", 0.95)
        estimate_blast_radius({"parent_image": "lsass.exe"}, fake_firestore)  # populates cache

        monkeypatch.setattr("vor_agents.blast_radius._TABLE_CACHE_TTL_SECONDS", 0)

        class _BoomCollection:
            def stream(self):
                raise RuntimeError("Firestore unavailable")

        class _BoomClient:
            def collection(self, name):
                return _BoomCollection()

        result = estimate_blast_radius({"parent_image": "lsass.exe"}, _BoomClient())

        assert result == 0.95  # stale cache, not a raised exception

    def test_cold_cache_failure_falls_back_to_unscored_default(self):
        reset_table_cache()

        class _BoomCollection:
            def stream(self):
                raise RuntimeError("Firestore unavailable")

        class _BoomClient:
            def collection(self, name):
                return _BoomCollection()

        result = estimate_blast_radius({"parent_image": "lsass.exe"}, _BoomClient())

        assert result == UNSCORED_DEFAULT
```

Update every EXISTING test in `tests/test_blast_radius.py` that calls `estimate_blast_radius(alert)` to instead call `estimate_blast_radius(alert, fake_firestore)`, seeding `fake_firestore`'s `blast_radius_table` collection with the table entries each test needs (using the same `_seed_entry` helper pattern above) instead of relying on the old module-level `BLAST_RADIUS_TABLE` dict. Add `reset_table_cache()` in each such test's setup so tests don't leak cache state between each other.

- [x] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_blast_radius.py -v`
Expected: `TypeError: estimate_blast_radius() missing 1 required positional argument: 'firestore_client'` on every test, plus `ImportError` for `reset_table_cache`/`BLAST_RADIUS_TABLE_COLLECTION`.

- [x] **Step 3: Rewrite `estimate_blast_radius()` in `vor_agents/blast_radius.py`**

Add to the imports at the top of the file:

```python
import time

from google.cloud.firestore import Client
from loguru import logger
```

Add module-level cache state and the collection name constant, right after `TIER_RANGES`:

```python
BLAST_RADIUS_TABLE_COLLECTION = "blast_radius_table"

_TABLE_CACHE: dict[tuple[str, str], float] = {}
_TABLE_CACHE_LOADED_AT: float | None = None
_TABLE_CACHE_TTL_SECONDS = 300
# Per-process, TTL'd cache -- _fetch_all_confirmed_patterns() calls
# estimate_blast_radius() once per confirmed instance per sweep; a
# Firestore read per call would turn one sweep into O(instances) reads
# for a table that changes rarely. 5 minutes is an unvalidated starting
# point, same posture as GRADUATION_THRESHOLD elsewhere in this design.


def reset_table_cache() -> None:
    """Test-only reset hook -- module-level cache state persists across
    tests in the same process otherwise. Not called anywhere in
    production code."""
    global _TABLE_CACHE, _TABLE_CACHE_LOADED_AT
    _TABLE_CACHE = {}
    _TABLE_CACHE_LOADED_AT = None


def _invalidate_table_cache() -> None:
    """Called after a commit writes new entries, so the next read sees
    them without waiting out the full TTL."""
    global _TABLE_CACHE_LOADED_AT
    _TABLE_CACHE_LOADED_AT = None


def _load_table(firestore_client: Client) -> dict[tuple[str, str], float]:
    """
    Returns the cached (indicator_type, value) -> score table, refreshing
    from Firestore if the cache is missing or past its TTL. On a refresh
    failure: serves the previous cache if one exists (a stale table is a
    much safer failure mode than an unhandled exception breaking
    estimate_blast_radius() and, transitively, the whole sweep); falls
    back to an empty table (every lookup then returns UNSCORED_DEFAULT,
    same "unassessed defaults to HIGH, never silently trusted" principle
    this whole module already runs on) if the cache has never been
    populated at all.
    """
    global _TABLE_CACHE, _TABLE_CACHE_LOADED_AT
    now = time.monotonic()
    if _TABLE_CACHE_LOADED_AT is not None and (now - _TABLE_CACHE_LOADED_AT) < _TABLE_CACHE_TTL_SECONDS:
        return _TABLE_CACHE

    try:
        fresh: dict[tuple[str, str], float] = {}
        for doc in firestore_client.collection(BLAST_RADIUS_TABLE_COLLECTION).stream():
            data = doc.to_dict() or {}
            indicator_type = data.get("indicator_type")
            value = data.get("value")
            score = data.get("score")
            if indicator_type is None or value is None or score is None:
                logger.bind(doc_id=doc.id).warning(
                    "blast_radius_table doc missing indicator_type/value/score, skipping"
                )
                continue
            fresh[(indicator_type, value)] = score
        _TABLE_CACHE = fresh
        _TABLE_CACHE_LOADED_AT = now
        return _TABLE_CACHE
    except Exception as exc:  # noqa: BLE001 — deliberate catch-all: any
        # Firestore failure here degrades to stale-or-empty, never raises.
        if _TABLE_CACHE_LOADED_AT is not None:
            logger.bind(error=str(exc)).warning(
                "Failed to refresh blast_radius_table cache, serving stale cache"
            )
            return _TABLE_CACHE
        logger.bind(error=str(exc)).warning(
            "Failed to load blast_radius_table cache and no prior cache exists; "
            "every lookup will fall back to UNSCORED_DEFAULT"
        )
        return {}


def estimate_blast_radius(alert: dict[str, Any], firestore_client: Client) -> float:
    """
    Checks every indicator present on the alert against the cached
    blast_radius_table (Firestore-backed, see _load_table), returns the
    MAX matching score -- blast radius is a worst-case estimate, not an
    average. Falls back to UNSCORED_DEFAULT (HIGH, deliberately not LOW
    or zero) when nothing matches or the table is unavailable, so an
    unassessed pattern gets prioritized for audit attention rather than
    silently trusted by omission.
    """
    table = _load_table(firestore_client)
    matches = [score for (indicator_type, value), score in table.items() if alert.get(indicator_type) == value]
    return max(matches) if matches else UNSCORED_DEFAULT
```

Remove the old module-level `BLAST_RADIUS_TABLE` dict entirely — it's replaced by the Firestore collection. Keep `CRITICAL`/`HIGH`/`MEDIUM`/`LOW`/`UNSCORED_DEFAULT`/`TIER_RANGES` as-is (unchanged by this task).

- [x] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_blast_radius.py -v`
Expected: all tests PASS, including the 6 new ones and every updated existing one.

- [x] **Step 5: Run the full suite and lint**

Run: `.venv/bin/python -m pytest -q`
Expected: failures ONLY in `tests/test_orchestrator.py` (still calling the old `estimate_blast_radius(instance)` one-arg form) — confirm that's the only failure category, fixed in Task 6. Run `.venv/bin/python -m ruff check . && .venv/bin/python -m mypy vor_agents/ main.py` — clean.

- [x] **Step 6: Commit**

```bash
git add vor_agents/blast_radius.py tests/test_blast_radius.py
git commit -m "estimate_blast_radius(): read from a TTL-cached Firestore table"
```

---

## Task 3: `propose_blast_radius()` writes proposals, auto-commits CRITICAL/HIGH

**Files:**
- Modify: `vor_agents/blast_radius.py`
- Modify: `tests/test_blast_radius.py`

**Interfaces:**
- Produces: `BLAST_RADIUS_PROPOSALS_COLLECTION = "blast_radius_proposals"`. `propose_blast_radius(identity_key, proposed_tier, proposed_score, cited_indicators, rationale, firestore_client) -> dict[str, Any]` (signature change — gains required `firestore_client`; return dict gains `"proposal_id"`, `"identity_key"` is now a `list`, not a `tuple`). `_parse_cited_indicator(indicator: str) -> tuple[str, str]`, `_commit_indicators(cited_indicators: list[str], score: float, firestore_client: Client) -> None`. Consumed by Task 4 (`commit_blast_radius_proposal`).

- [x] **Step 1: Write the failing tests**

Add to `tests/test_blast_radius.py`:

```python
from vor_agents.blast_radius import BLAST_RADIUS_PROPOSALS_COLLECTION, propose_blast_radius


class TestProposeBlastRadiusStorage:
    def setup_method(self):
        reset_table_cache()

    def test_critical_proposal_auto_commits(self, fake_firestore):
        result = propose_blast_radius(
            ("rule", "p.exe", "c.exe", "family"),
            "CRITICAL",
            0.95,
            ["parent_image=p.exe"],
            "reads credential material",
            fake_firestore,
        )

        assert result["status"] == "committed"
        score = estimate_blast_radius({"parent_image": "p.exe"}, fake_firestore)
        assert score == 0.95

    def test_high_proposal_auto_commits(self, fake_firestore):
        result = propose_blast_radius(
            ("rule", "p.exe", "c.exe", "family"),
            "HIGH",
            0.75,
            ["parent_image=p.exe"],
            "internet-facing service",
            fake_firestore,
        )

        assert result["status"] == "committed"

    def test_medium_proposal_does_not_auto_commit(self, fake_firestore):
        result = propose_blast_radius(
            ("rule", "p.exe", "c.exe", "family"),
            "MEDIUM",
            0.45,
            ["parent_image=p.exe"],
            "internal service account",
            fake_firestore,
        )

        assert result["status"] == "pending_human_review"
        score = estimate_blast_radius({"parent_image": "p.exe"}, fake_firestore)
        assert score == UNSCORED_DEFAULT  # not committed to the table

    def test_low_proposal_does_not_auto_commit(self, fake_firestore):
        result = propose_blast_radius(
            ("rule", "p.exe", "c.exe", "family"),
            "LOW",
            0.15,
            ["parent_image=p.exe"],
            "ordinary user-context app",
            fake_firestore,
        )

        assert result["status"] == "pending_human_review"

    def test_proposal_is_persisted_and_retrievable(self, fake_firestore):
        result = propose_blast_radius(
            ("rule", "p.exe", "c.exe", "family"),
            "MEDIUM",
            0.45,
            ["parent_image=p.exe"],
            "internal service account",
            fake_firestore,
        )

        doc = fake_firestore.collection(BLAST_RADIUS_PROPOSALS_COLLECTION).document(
            result["proposal_id"]
        ).get()
        assert doc.exists
        assert doc.to_dict()["proposed_tier"] == "MEDIUM"

    def test_unknown_tier_still_raises_before_any_write(self, fake_firestore):
        with pytest.raises(ValueError):
            propose_blast_radius(
                ("rule", "p.exe", "c.exe", "family"), "SEVERE", 0.5, [], "x", fake_firestore
            )
        assert list(fake_firestore.collection(BLAST_RADIUS_PROPOSALS_COLLECTION).stream()) == []
```

Add `import pytest` to the top of `tests/test_blast_radius.py` if it isn't already imported.

- [x] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_blast_radius.py -k Propose -v`
Expected: `TypeError: propose_blast_radius() missing 1 required positional argument: 'firestore_client'`.

- [x] **Step 3: Rewrite `propose_blast_radius()` in `vor_agents/blast_radius.py`**

Add to the imports:

```python
import uuid
from datetime import UTC, datetime
```

Add the collection constant near `BLAST_RADIUS_TABLE_COLLECTION`:

```python
BLAST_RADIUS_PROPOSALS_COLLECTION = "blast_radius_proposals"
```

Add these two helpers above `propose_blast_radius()`:

```python
def _parse_cited_indicator(indicator: str) -> tuple[str, str]:
    """
    cited_indicators entries are "indicator_type=value" strings (e.g.
    "parent_image=lsass.exe") -- the format propose_blast_radius()'s
    callers (human or an extended auditor LLM step) are expected to use.
    Raises ValueError on anything else, same "fail loud on a malformed
    proposal rather than silently write a wrong table entry" posture as
    the tier/score validation already in this function.
    """
    if "=" not in indicator:
        raise ValueError(
            f"Malformed cited_indicator {indicator!r}; expected 'indicator_type=value'"
        )
    indicator_type, value = indicator.split("=", 1)
    return indicator_type.strip(), value.strip()


def _commit_indicators(cited_indicators: list[str], score: float, firestore_client: Client) -> None:
    """Writes each cited indicator into blast_radius_table at the given
    score, then invalidates the read cache so the next
    estimate_blast_radius() call sees it without waiting out the TTL."""
    for indicator in cited_indicators:
        indicator_type, value = _parse_cited_indicator(indicator)
        doc_id = _table_doc_id(indicator_type, value)
        firestore_client.collection(BLAST_RADIUS_TABLE_COLLECTION).document(doc_id).set(
            {
                "indicator_type": indicator_type,
                "value": value,
                "score": score,
                "committed_at": datetime.now(UTC).isoformat(),
            },
            merge=True,
        )
    _invalidate_table_cache()


def _table_doc_id(indicator_type: str, value: str) -> str:
    """Content hash, not a raw f-string join -- same collision-avoidance
    reasoning as enrichment._doc_id() and task_queue._task_name()."""
    encoded = json.dumps([indicator_type, value], separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
```

Add `import hashlib` and `import json` to the top of the file if not already present (they aren't, in the current version).

Replace `propose_blast_radius()`'s body:

```python
def propose_blast_radius(
    identity_key: tuple[str, ...],
    proposed_tier: str,
    proposed_score: float,
    cited_indicators: list[str],
    rationale: str,
    firestore_client: Client,
) -> dict[str, Any]:
    """
    Validates tier/score exactly as before (raises ValueError for an
    unknown tier or an out-of-range score -- unchanged). New in this
    revision: persists the proposal to blast_radius_proposals instead of
    just returning an inert dict, and CRITICAL/HIGH proposals commit
    directly into blast_radius_table in the same call (the conservative
    direction -- matches BLAST_RADIUS_PLAYBOOK.md's "may be added
    directly" language). MEDIUM/LOW proposals are written with
    status="pending_human_review" and NOT committed -- see
    commit_blast_radius_proposal() for the human-gated commit path.
    """
    if proposed_tier not in TIER_RANGES:
        raise ValueError(
            f"Unknown blast-radius tier {proposed_tier!r}; must be one of {sorted(TIER_RANGES)}"
        )
    low, high = TIER_RANGES[proposed_tier]
    if not (low <= proposed_score <= high):
        raise ValueError(
            f"proposed_score {proposed_score} is outside {proposed_tier}'s "
            f"documented range [{low}, {high}] (see BLAST_RADIUS_PLAYBOOK.md)"
        )

    requires_review = proposed_tier in ("MEDIUM", "LOW")
    proposal = {
        "proposal_id": str(uuid.uuid4()),
        "identity_key": list(identity_key),
        "proposed_tier": proposed_tier,
        "proposed_score": proposed_score,
        "cited_indicators": cited_indicators,
        "rationale": rationale,
        "proposed_at": datetime.now(UTC).isoformat(),
        "status": "pending_human_review",
        "requires_review": requires_review,
    }

    if not requires_review:
        _commit_indicators(cited_indicators, proposed_score, firestore_client)
        proposal["status"] = "committed"

    firestore_client.collection(BLAST_RADIUS_PROPOSALS_COLLECTION).document(
        proposal["proposal_id"]
    ).set(proposal)
    return proposal
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_blast_radius.py -v`
Expected: all tests PASS.

- [x] **Step 5: Run the full suite and lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy vor_agents/ main.py`
Expected: same pre-existing `test_orchestrator.py` failures as Task 2 left (fixed in Task 6), nothing new.

- [x] **Step 6: Commit**

```bash
git add vor_agents/blast_radius.py tests/test_blast_radius.py
git commit -m "propose_blast_radius(): persist proposals, auto-commit CRITICAL/HIGH"
```

---

## Task 4: `commit_blast_radius_proposal()` — human-gated commit for MEDIUM/LOW

**Files:**
- Modify: `vor_agents/blast_radius.py`
- Modify: `tests/test_blast_radius.py`

**Interfaces:**
- Produces: `ProposalNotFoundError(Exception)`, `ProposalAlreadyResolvedError(Exception)`, `commit_blast_radius_proposal(proposal_id: str, firestore_client: Client) -> dict[str, Any]`. Consumed by Task 5 (`main.py`'s new endpoint).

- [x] **Step 1: Write the failing tests**

Add to `tests/test_blast_radius.py`:

```python
from vor_agents.blast_radius import (
    ProposalAlreadyResolvedError,
    ProposalNotFoundError,
    commit_blast_radius_proposal,
)


class TestCommitBlastRadiusProposal:
    def setup_method(self):
        reset_table_cache()

    def test_commits_a_pending_medium_proposal(self, fake_firestore):
        proposal = propose_blast_radius(
            ("rule", "p.exe", "c.exe", "family"),
            "MEDIUM",
            0.45,
            ["parent_image=p.exe"],
            "internal service account",
            fake_firestore,
        )

        result = commit_blast_radius_proposal(proposal["proposal_id"], fake_firestore)

        assert result["status"] == "committed"
        assert estimate_blast_radius({"parent_image": "p.exe"}, fake_firestore) == 0.45

    def test_unknown_proposal_id_raises_not_found(self, fake_firestore):
        with pytest.raises(ProposalNotFoundError):
            commit_blast_radius_proposal("does-not-exist", fake_firestore)

    def test_already_committed_proposal_raises_already_resolved(self, fake_firestore):
        proposal = propose_blast_radius(
            ("rule", "p.exe", "c.exe", "family"),
            "MEDIUM",
            0.45,
            ["parent_image=p.exe"],
            "internal service account",
            fake_firestore,
        )
        commit_blast_radius_proposal(proposal["proposal_id"], fake_firestore)

        with pytest.raises(ProposalAlreadyResolvedError):
            commit_blast_radius_proposal(proposal["proposal_id"], fake_firestore)

    def test_auto_committed_critical_proposal_cannot_be_recommitted(self, fake_firestore):
        proposal = propose_blast_radius(
            ("rule", "p.exe", "c.exe", "family"),
            "CRITICAL",
            0.95,
            ["parent_image=p.exe"],
            "reads credential material",
            fake_firestore,
        )

        with pytest.raises(ProposalAlreadyResolvedError):
            commit_blast_radius_proposal(proposal["proposal_id"], fake_firestore)
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_blast_radius.py -k Commit -v`
Expected: `ImportError: cannot import name 'commit_blast_radius_proposal'`.

- [x] **Step 3: Add the function and exceptions**

Add to `vor_agents/blast_radius.py`:

```python
class ProposalNotFoundError(Exception):
    """Raised when POST /blast-radius/commit references a proposal_id
    that doesn't exist in blast_radius_proposals."""


class ProposalAlreadyResolvedError(Exception):
    """Raised when a commit is attempted on a proposal whose status isn't
    pending_human_review -- no double-commit, whether it was already
    manually committed or was auto-committed at proposal time (CRITICAL/
    HIGH)."""


def commit_blast_radius_proposal(proposal_id: str, firestore_client: Client) -> dict[str, Any]:
    """
    Human-triggered commit for a pending MEDIUM/LOW proposal -- see
    main.py's POST /blast-radius/commit, the only caller. Writes the
    proposal's cited indicators into blast_radius_table at its
    proposed_score, marks the proposal committed, returns the updated
    proposal dict.
    """
    doc_ref = firestore_client.collection(BLAST_RADIUS_PROPOSALS_COLLECTION).document(proposal_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise ProposalNotFoundError(f"No blast-radius proposal with id {proposal_id!r}")

    data = doc.to_dict() or {}
    if data.get("status") != "pending_human_review":
        raise ProposalAlreadyResolvedError(
            f"Proposal {proposal_id!r} already has status {data.get('status')!r}, "
            "not pending_human_review"
        )

    _commit_indicators(data["cited_indicators"], data["proposed_score"], firestore_client)
    committed_at = datetime.now(UTC).isoformat()
    doc_ref.update({"status": "committed", "committed_at": committed_at})
    data["status"] = "committed"
    data["committed_at"] = committed_at
    return data
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_blast_radius.py -v`
Expected: all tests PASS.

- [x] **Step 5: Run the full suite and lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy vor_agents/ main.py && .venv/bin/python -m bandit -r vor_agents/ main.py`
Expected: same pre-existing `test_orchestrator.py` failures, nothing new; bandit clean.

- [x] **Step 6: Commit**

```bash
git add vor_agents/blast_radius.py tests/test_blast_radius.py
git commit -m "Add commit_blast_radius_proposal() for the human-gated MEDIUM/LOW path"
```

---

## Task 5: `POST /blast-radius/commit` endpoint

**Files:**
- Modify: `vor_agents/schemas.py`
- Modify: `main.py`
- Modify: `tests/test_main.py`

**Interfaces:**
- Consumes: `commit_blast_radius_proposal()`, `ProposalNotFoundError`, `ProposalAlreadyResolvedError` (Task 4).
- Produces: `BlastRadiusCommitRequest(BaseModel)` with `proposal_id: str`.

- [x] **Step 1: Write the failing tests**

Add to `tests/test_main.py`:

```python
from vor_agents.blast_radius import ProposalAlreadyResolvedError, ProposalNotFoundError


def test_blast_radius_commit_commits_a_pending_proposal(fake_firestore):
    with patch("main.get_firestore_client", return_value=fake_firestore), \
         patch("main.commit_blast_radius_proposal", return_value={"status": "committed", "proposal_id": "p1"}):
        client = TestClient(main.app)
        resp = client.post("/blast-radius/commit", json={"proposal_id": "p1"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "committed"


def test_blast_radius_commit_returns_404_for_unknown_proposal(fake_firestore):
    with patch("main.get_firestore_client", return_value=fake_firestore), \
         patch("main.commit_blast_radius_proposal", side_effect=ProposalNotFoundError("no such proposal")):
        client = TestClient(main.app)
        resp = client.post("/blast-radius/commit", json={"proposal_id": "missing"})

    assert resp.status_code == 404


def test_blast_radius_commit_returns_409_for_already_resolved_proposal(fake_firestore):
    with patch("main.get_firestore_client", return_value=fake_firestore), \
         patch("main.commit_blast_radius_proposal", side_effect=ProposalAlreadyResolvedError("already committed")):
        client = TestClient(main.app)
        resp = client.post("/blast-radius/commit", json={"proposal_id": "p1"})

    assert resp.status_code == 409
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_main.py -k blast_radius -v`
Expected: 404 (route doesn't exist yet) on all three.

- [x] **Step 3: Add the schema and endpoint**

Add to `vor_agents/schemas.py`, after `AuditRequest`:

```python
class BlastRadiusCommitRequest(BaseModel):
    """Body shape for POST /blast-radius/commit -- a human committing a
    pending MEDIUM/LOW blast-radius proposal into the live table."""

    proposal_id: str
```

Add to `main.py`'s imports:

```python
from vor_agents.blast_radius import (
    ProposalAlreadyResolvedError,
    ProposalNotFoundError,
    commit_blast_radius_proposal,
)
```

Add `BlastRadiusCommitRequest` to the existing `from vor_agents.schemas import ...` line.

Add the new route, after `/audit`:

```python
@app.post("/blast-radius/commit")
async def blast_radius_commit(payload: BlastRadiusCommitRequest) -> dict[str, Any]:
    """
    Human-triggered commit for a pending MEDIUM/LOW blast-radius
    proposal (see vor_agents/blast_radius.py's
    commit_blast_radius_proposal()). Gated the same way /audit is --
    Cloud Run IAM, OIDC-authenticated caller, never
    --allow-unauthenticated -- but unlike /audit this is meant to be
    called by a human (via `gcloud run services proxy` + curl, or a
    small authenticated script), not a machine dispatcher.
    """
    client = get_firestore_client()
    try:
        proposal = commit_blast_radius_proposal(payload.proposal_id, client)
    except ProposalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProposalAlreadyResolvedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return proposal
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_main.py -v`
Expected: all tests PASS.

- [x] **Step 5: Run the full suite and lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy vor_agents/ main.py && .venv/bin/python -m bandit -r vor_agents/ main.py`
Expected: same pre-existing `test_orchestrator.py` failures, nothing new.

- [x] **Step 6: Commit**

```bash
git add vor_agents/schemas.py main.py tests/test_main.py
git commit -m "Add POST /blast-radius/commit endpoint"
```

---

## Task 6: `orchestrator.py` call site update

**Files:**
- Modify: `vor_agents/orchestrator.py`
- Modify: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `estimate_blast_radius(alert, firestore_client)` (Task 2's new signature).

- [x] **Step 1: Update `_fetch_all_confirmed_patterns()`**

In `vor_agents/orchestrator.py`, change the `blast_radius_estimate` computation inside `_fetch_all_confirmed_patterns()`:

```python
                "blast_radius_estimate": max(
                    estimate_blast_radius(instance, firestore_client) for instance in instances
                ),
```

(was `estimate_blast_radius(instance)`, no `firestore_client` arg — this is the only call site in `vor_agents/` outside `blast_radius.py` itself and its own tests.)

- [x] **Step 2: Update `tests/test_orchestrator.py`**

Any test in this file that seeds Firestore data feeding `_fetch_all_confirmed_patterns()`/`run_scheduled_sweep()` and expects a specific `blast_radius_estimate` value now needs the test's `fake_firestore` to have a matching `blast_radius_table` entry seeded (Task 2's fake collection), or it will see every instance fall back to `UNSCORED_DEFAULT` (0.75) since the table starts empty in a fresh `fake_firestore`. Check each such test's assertions against `UNSCORED_DEFAULT` if it doesn't seed the table — that's the correct new default now that there's no hardcoded dict, not a bug to work around.

- [x] **Step 3: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: **zero failures** — this was the last call site with the old one-arg signature.

- [x] **Step 4: Lint**

Run: `.venv/bin/python -m ruff check . && .venv/bin/python -m black --check . && .venv/bin/python -m mypy vor_agents/ main.py && .venv/bin/python -m bandit -r vor_agents/ main.py`
Expected: all clean.

- [x] **Step 5: Commit**

```bash
git add vor_agents/orchestrator.py tests/test_orchestrator.py
git commit -m "Pass firestore_client through to estimate_blast_radius() in the sweep path"
```

---

## Task 7: One-time seed script for the 5 existing table entries

**Files:**
- Create: `scripts/seed_blast_radius_table.py`

**Interfaces:**
- Consumes: `_commit_indicators()` (Task 3, module-private — imported directly since this script lives in the same repo, not a separate package boundary).

- [x] **Step 1: Write the script**

Create `scripts/seed_blast_radius_table.py`:

```python
"""
One-time migration: seed blast_radius_table with the 5 entries that used
to be hardcoded in vor_agents/blast_radius.py's BLAST_RADIUS_TABLE dict,
before that table moved to Firestore (see
docs/superpowers/specs/2026-08-24-blast-radius-firestore-design.md).

Run once, before first production deploy against a fresh Firestore
project -- see docs/DEPLOY.md. Idempotent: re-running just overwrites the
same 5 entries with the same values (merge=True in _commit_indicators),
so it's safe to run more than once.

Usage:
    .venv/bin/python scripts/seed_blast_radius_table.py
"""

from google.cloud import firestore

from vor_agents.blast_radius import _commit_indicators

# (indicator, score) -- matches the original BLAST_RADIUS_TABLE dict
# exactly, tier constants inlined as their point values since the
# original dict stored scores, not tier labels.
SEED_ENTRIES: list[tuple[str, float]] = [
    ("parent_image=lsass.exe", 0.95),  # CRITICAL
    ("endpoint_family=ToolPane_admin", 0.95),  # CRITICAL (CVE-2026-56164 model)
    ("parent_image=w3wp.exe", 0.75),  # HIGH
    ("parent_image=svchost.exe", 0.45),  # MEDIUM
    ("parent_image=explorer.exe", 0.15),  # LOW
]


def main() -> None:
    client = firestore.Client()
    for indicator, score in SEED_ENTRIES:
        _commit_indicators([indicator], score, client)
    print(f"Seeded {len(SEED_ENTRIES)} blast-radius table entries into Firestore.")


if __name__ == "__main__":
    main()
```

- [x] **Step 2: Verify it imports cleanly**

Run: `.venv/bin/python -c "import ast; ast.parse(open('scripts/seed_blast_radius_table.py').read())"`
Expected: no output (valid syntax) — not run against real Firestore here, since that needs live credentials this environment doesn't have.

- [x] **Step 3: Lint**

Run: `.venv/bin/python -m ruff check scripts/ && .venv/bin/python -m black --check scripts/`
Expected: clean. If `ruff`/`black`'s config excludes `scripts/` by default, add it to `testpaths`-adjacent config the same way `vor_agents/`/`main.py` are covered — check `pyproject.toml`'s `[tool.ruff]`/`[tool.black]` sections (if present) for an `include`/exclude that needs `scripts/` added.

- [x] **Step 4: Commit**

```bash
git add scripts/seed_blast_radius_table.py
git commit -m "Add one-time seed script for blast_radius_table's original 5 entries"
```

---

## Task 8: `docs/DEPLOY.md` and `docs/BLAST_RADIUS_PLAYBOOK.md` updates

**Files:**
- Modify: `docs/DEPLOY.md`
- Modify: `docs/BLAST_RADIUS_PLAYBOOK.md`

**Interfaces:**
- None (documentation only).

- [x] **Step 1: Add a DEPLOY.md section**

Insert after the existing "3b. needs_attention collection" section (or after 3a if that one doesn't exist yet in this repo's current state):

```markdown
## 3c. Seed the blast-radius table and gate the commit endpoint

```bash
.venv/bin/python scripts/seed_blast_radius_table.py
```

Run once, before first production deploy — populates `blast_radius_table`
with the 5 entries that used to be hardcoded. Without this,
`estimate_blast_radius()` falls back to `UNSCORED_DEFAULT` for every
alert until someone re-proposes and commits each entry by hand.

`/blast-radius/commit` must never be deployed with
`--allow-unauthenticated`, same as `/classify`/`/sweep`/`/audit` —
gate it with the same Cloud Run IAM approach (OIDC-authenticated caller).
Unlike the others, this endpoint is meant to be called by a human, not a
machine dispatcher — grant `roles/run.invoker` to whichever human
identities (or a shared review service account) should be allowed to
commit blast-radius proposals.
```

- [x] **Step 2: Add a storage note to `BLAST_RADIUS_PLAYBOOK.md`**

Add after the "Proposing a new table entry" section:

```markdown
## Storage note

`BLAST_RADIUS_TABLE` is Firestore-backed (`blast_radius_table`
collection), not a Python literal — updating an entry no longer requires
a code deploy. The trust model this playbook describes is unchanged: a
CRITICAL/HIGH proposal still commits automatically (the conservative
direction), a MEDIUM/LOW proposal still requires an explicit human
action (`POST /blast-radius/commit`, IAM-gated to authorized reviewers)
before it takes effect. Access control now lives in Cloud Run IAM on
that endpoint, not in "who can open a pull request" — keep the reviewer
list for that IAM binding at least as tight as code-review access was.
```

- [x] **Step 3: Commit**

```bash
git add docs/DEPLOY.md docs/BLAST_RADIUS_PLAYBOOK.md
git commit -m "Document blast-radius table seeding and Firestore storage model"
```

---

## Final verification

- [x] Run `.venv/bin/python -m pytest -v` — full suite passes.
- [x] Run `.venv/bin/python -m ruff check . && .venv/bin/python -m black --check . && .venv/bin/python -m mypy vor_agents/ main.py && .venv/bin/python -m bandit -r vor_agents/ main.py` — all clean.
- [x] Confirm `git log --oneline -8` shows one commit per task.
- [x] Update `docs/TODO-Aug24.md` Task 7 checkbox to done, referencing the commits.
