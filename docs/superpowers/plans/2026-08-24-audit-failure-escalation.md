# Consecutive Audit-Failure Escalation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track consecutive audit failures per pattern; once `failure_count` reaches 3, force the pattern's next classify decision to `UNCERTAIN` in code and write a visible `needs_attention` Firestore record a human can find.

**Architecture:** `failure_count` lives on the existing confidence doc, incremented/reset by `clear_under_review()` (which already runs on every audit outcome). `orchestrator.py`'s `audit_pattern()` reads the returned count and, past the threshold, writes a `needs_attention` doc. `classify_alert()` gets one more deterministic override, same shape as its existing `under_review`/provisional-tier overrides.

**Tech Stack:** Python 3.13, `google-cloud-firestore`, `loguru`, existing `FakeFirestoreClient` test double (no changes needed to it).

**Spec:** `docs/superpowers/specs/2026-08-24-audit-failure-escalation-design.md`

## Global Constraints

- `AUDIT_FAILURE_ESCALATION_THRESHOLD = 3` -- unvalidated starting point, same posture as `GRADUATION_THRESHOLD`/`MIN_DIVERSITY`, flagged as such in code.
- `clear_under_review()` uses read-then-write for `failure_count`, not `firestore.Increment` -- the existing `FakeFirestoreClient` test double doesn't model Firestore field-transform sentinels, and audits for the same pattern are already serialized in the common case (`under_review`/Cloud Tasks dedup), so the small race window this leaves is accepted rather than adding transform support to the fake for one field.
- Writing the `needs_attention` doc must never prevent `clear_under_review()`'s own write, and a failure to write it must never raise out of `audit_pattern()` -- wrapped in its own `try/except`, logged, not re-raised.
- No changes to what counts as an audit "failure" -- still exactly `audit_pattern()`'s existing `except Exception` block.

---

## Task 1: `UncertainReason.AUDIT_FAILING`

**Files:**
- Modify: `vor_agents/schemas.py`
- Test: `tests/test_schemas.py` (created by the Pub/Sub plan; if that plan hasn't run yet, create it fresh here)

**Interfaces:**
- Produces: `UncertainReason.AUDIT_FAILING` (value `"audit_failing"`). Consumed by Task 4's `classify_alert()` override.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_schemas.py` (create the file with just this class if it doesn't exist yet):

```python
from vor_agents.schemas import UncertainReason


class TestUncertainReason:
    def test_audit_failing_value_exists(self):
        assert UncertainReason.AUDIT_FAILING == "audit_failing"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_schemas.py -k audit_failing -v`
Expected: `AttributeError: AUDIT_FAILING`

- [ ] **Step 3: Add the enum value**

In `vor_agents/schemas.py`, add to the `UncertainReason` enum:

```python
class UncertainReason(str, Enum):
    NO_HISTORY = "no_history"
    GRADUATION_PENDING = "graduation_pending"
    UNDER_REVIEW = "under_review"
    AUDIT_FAILING = "audit_failing"
    MISSING_DATA = "missing_data"
    NOT_APPLICABLE = "not_applicable"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_schemas.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add vor_agents/schemas.py tests/test_schemas.py
git commit -m "Add UncertainReason.AUDIT_FAILING"
```

---

## Task 2: `failure_count` tracking in `review_flag.py`

**Files:**
- Modify: `vor_agents/review_flag.py`
- Modify: `tests/test_review_flag.py`

**Interfaces:**
- Produces: `clear_under_review(pattern_identity_key, firestore_client, auditor_decision, audit_failed: bool = False) -> int` (return type changes from `None` to `int` -- the resulting `failure_count`). `NEEDS_ATTENTION_COLLECTION = "needs_attention"`. `record_needs_attention(pattern_identity_key: tuple[str, ...], failure_count: int, last_error: str, firestore_client: Client) -> None`. Both consumed by Task 4 (`orchestrator.py`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_review_flag.py`:

```python
class TestFailureCountTracking:
    def test_audit_failed_increments_failure_count(self, fake_firestore):
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")
        mark_under_review(identity_key, fake_firestore)

        new_count = clear_under_review(
            identity_key, fake_firestore, {"action": "NO_ACTION"}, audit_failed=True
        )

        assert new_count == 1
        doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key)).get()
        assert doc.to_dict()["failure_count"] == 1

    def test_audit_succeeded_resets_failure_count(self, fake_firestore):
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")
        mark_under_review(identity_key, fake_firestore)
        clear_under_review(identity_key, fake_firestore, {"action": "NO_ACTION"}, audit_failed=True)
        clear_under_review(identity_key, fake_firestore, {"action": "NO_ACTION"}, audit_failed=True)

        new_count = clear_under_review(
            identity_key, fake_firestore, {"action": "NO_ACTION"}, audit_failed=False
        )

        assert new_count == 0
        doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key)).get()
        assert doc.to_dict()["failure_count"] == 0

    def test_consecutive_failures_accumulate(self, fake_firestore):
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")
        mark_under_review(identity_key, fake_firestore)

        counts = [
            clear_under_review(identity_key, fake_firestore, {"action": "NO_ACTION"}, audit_failed=True)
            for _ in range(3)
        ]

        assert counts == [1, 2, 3]

    def test_missing_failure_count_treated_as_zero_before_incrementing(self, fake_firestore):
        """A doc that predates this feature (or is brand new) has no
        failure_count field at all -- must not KeyError, starts from 0."""
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")
        mark_under_review(identity_key, fake_firestore)

        new_count = clear_under_review(
            identity_key, fake_firestore, {"action": "NO_ACTION"}, audit_failed=True
        )

        assert new_count == 1


class TestRecordNeedsAttention:
    def test_writes_a_needs_attention_doc(self, fake_firestore):
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

        record_needs_attention(identity_key, 3, "RuntimeError('boom')", fake_firestore)

        doc = fake_firestore.collection(NEEDS_ATTENTION_COLLECTION).document(_doc_id(identity_key)).get()
        assert doc.exists
        assert doc.to_dict()["failure_count"] == 3
        assert doc.to_dict()["last_error"] == "RuntimeError('boom')"
        assert doc.to_dict()["identity_key"] == list(identity_key)
```

Add the new imports these tests need to the top of `tests/test_review_flag.py` (alongside whatever's already imported there):

```python
from vor_agents.enrichment import CONFIDENCE_COLLECTION, _doc_id
from vor_agents.review_flag import (
    NEEDS_ATTENTION_COLLECTION,
    clear_under_review,
    mark_under_review,
    record_needs_attention,
)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_review_flag.py -k "FailureCount or NeedsAttention" -v`
Expected: `TypeError: clear_under_review() got an unexpected keyword argument 'audit_failed'` and `ImportError: cannot import name 'record_needs_attention'`.

- [ ] **Step 3: Update `vor_agents/review_flag.py`**

Replace `clear_under_review()`'s body and add the new function:

```python
NEEDS_ATTENTION_COLLECTION = "needs_attention"


def clear_under_review(
    pattern_identity_key: tuple[str, ...],
    firestore_client: Client,
    auditor_decision: dict[str, Any],
    audit_failed: bool = False,
) -> int:
    """
    Called as part of the SAME write that records the auditor's decision
    (DOWNGRADE, RECOMMEND_UPGRADE_FOR_HUMAN_REVIEW, or NO_ACTION) --
    under_review is cleared atomically with the outcome, never in a
    separate write, so there's no window where the flag is false but the
    confidence data hasn't landed yet.

    Also stamps last_reviewed_at on every call, not just DOWNGRADE -- an
    audit that found nothing wrong (NO_ACTION) is still evidence the
    pattern was looked at, which is exactly what select_audit_targets()
    needs to know to stop re-prioritizing it every sweep.

    audit_failed tracks failure_count: incremented on a failed audit,
    reset to 0 on a successful one. Read-then-write, not
    firestore.Increment -- see this plan/spec's rationale; audits for the
    same pattern are already serialized in the common case, so the small
    race window is accepted. Returns the resulting failure_count so
    callers (audit_pattern) can decide whether to escalate without a
    second Firestore read.

    DOWNGRADE resolution -- targeted evidence invalidation, decided over
    the blanket "demote tier to provisional" alternative: the auditor
    cites specific confirmed_instance IDs it no longer trusts
    (auditor_decision["invalidated_instance_ids"]). Only those instances
    are removed; the template is rebuilt from whatever remains, and tier
    falls out of that rebuild rather than being force-set.
    """
    doc_ref = firestore_client.collection(CONFIDENCE_COLLECTION).document(
        _doc_id(pattern_identity_key)
    )

    current = doc_ref.get().to_dict() or {}
    previous_failure_count = current.get("failure_count", 0)
    new_failure_count = previous_failure_count + 1 if audit_failed else 0

    update: dict[str, Any] = {
        "under_review": False,
        "last_reviewed_at": datetime.now(UTC).isoformat(),
        "failure_count": new_failure_count,
    }
    if auditor_decision["action"] == "DOWNGRADE":
        rebuild = invalidate_instances(
            pattern_identity_key,
            auditor_decision.get("invalidated_instance_ids", []),
            firestore_client,
        )
        update.update(rebuild)

    doc_ref.update(update)
    return new_failure_count


def record_needs_attention(
    pattern_identity_key: tuple[str, ...],
    failure_count: int,
    last_error: str,
    firestore_client: Client,
) -> None:
    """
    Writes a visible, queryable record that a pattern has crossed the
    consecutive-audit-failure escalation threshold (see orchestrator.py's
    AUDIT_FAILURE_ESCALATION_THRESHOLD). Deliberately a separate
    collection and a separate call from clear_under_review() -- the
    caller (audit_pattern) wraps this call in its own try/except: a
    failure to record this must never re-introduce the stuck-under_review
    bug clear_under_review()'s own try/finally already fixed, so this
    function's failure must never be able to prevent that one's write.
    """
    doc_ref = firestore_client.collection(NEEDS_ATTENTION_COLLECTION).document(
        _doc_id(pattern_identity_key)
    )
    doc_ref.set(
        {
            "identity_key": list(pattern_identity_key),
            "failure_count": failure_count,
            "last_error": last_error,
            "last_failed_at": datetime.now(UTC).isoformat(),
        },
        merge=True,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_review_flag.py -v`
Expected: all tests in the file PASS, including the 5 new ones.

- [ ] **Step 5: Run the full suite and lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy vor_agents/ main.py`
Expected: all pass except `tests/test_orchestrator.py`'s existing `clear_under_review`-adjacent calls if any assert on its return value being `None` -- there shouldn't be any (the function was previously called for its side effect only), but confirm the only failures (if any) are `audit_pattern()` callers not yet passing `audit_failed` -- there are none, since it defaults to `False` and every other call site's behavior is unchanged. Expect **zero regressions**.

- [ ] **Step 6: Commit**

```bash
git add vor_agents/review_flag.py tests/test_review_flag.py
git commit -m "Track failure_count in clear_under_review, add record_needs_attention"
```

---

## Task 3: `enrich()` surfaces `failure_count`

**Files:**
- Modify: `vor_agents/enrichment.py`
- Modify: `tests/test_enrichment.py`

**Interfaces:**
- Produces: `enrich()`'s returned dict gains a `"failure_count": int` key (present whenever `status == "TEMPLATE"`, defaulting to `0`). Consumed by Task 4's `classify_alert()` override.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_enrichment.py`:

```python
class TestEnrichFailureCount:
    def test_enrich_surfaces_failure_count(self, fake_firestore, baseline_alert):
        record_confirmed_negative(baseline_alert, fake_firestore)
        identity_key = pattern_identity_key(baseline_alert)
        doc_ref = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key))
        doc_ref.set({"failure_count": 2}, merge=True)

        result = enrich(baseline_alert, fake_firestore)

        assert result["failure_count"] == 2

    def test_enrich_defaults_failure_count_to_zero(self, fake_firestore, baseline_alert):
        record_confirmed_negative(baseline_alert, fake_firestore)

        result = enrich(baseline_alert, fake_firestore)

        assert result["failure_count"] == 0
```

Add `pattern_identity_key` to whichever import line in `tests/test_enrichment.py` already imports from `vor_agents.identity`, if it isn't imported yet.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_enrichment.py -k failure_count -v`
Expected: `KeyError: 'failure_count'`

- [ ] **Step 3: Update `enrich()` in `vor_agents/enrichment.py`**

In the `TEMPLATE` branch's returned dict, add one line:

```python
    return {
        "status": "TEMPLATE",
        "pattern_identity_key": identity_key,
        "fields": data.get("fields", {}),
        "tier": data.get("tier", "provisional"),
        "provenance": data.get("provenance", "live"),
        "under_review": data.get("under_review", False),
        "days_since_last_review": data.get("days_since_last_review", 0),
        "diversity_score": data.get("diversity_score", 0.0),
        "failure_count": data.get("failure_count", 0),
    }
```

Also update the module docstring's return-shape comment for `enrich()` to list `"failure_count": int` alongside the existing keys.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_enrichment.py -v`
Expected: all tests PASS, including the 2 new ones.

- [ ] **Step 5: Run the full suite and lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy vor_agents/ main.py`
Expected: all pass, no regressions.

- [ ] **Step 6: Commit**

```bash
git add vor_agents/enrichment.py tests/test_enrichment.py
git commit -m "enrich() surfaces failure_count from the confidence doc"
```

---

## Task 4: `orchestrator.py` -- escalation wiring + `classify_alert()` override

**Files:**
- Modify: `vor_agents/orchestrator.py`
- Modify: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `clear_under_review(..., audit_failed) -> int`, `record_needs_attention()`, `NEEDS_ATTENTION_COLLECTION` (Task 2); `enrich()`'s `failure_count` key (Task 3); `UncertainReason.AUDIT_FAILING` (Task 1).
- Produces: `AUDIT_FAILURE_ESCALATION_THRESHOLD = 3` (module-level constant in `orchestrator.py`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_orchestrator.py`:

```python
class TestFailureEscalation:
    """
    audit_pattern() failing repeatedly for the same pattern must, at the
    threshold, write a needs_attention doc -- not just clear the flag and
    log silently forever. Mirrors TestProvisionalTierBlocksSuppress's
    structure (Task 21 in docs/TODO-Aug15.md).
    """

    async def _fail_audit_once(self, identity_key, fake_firestore, monkeypatch):
        async def _boom(*args, **kwargs):
            raise RuntimeError("model call failed")

        monkeypatch.setattr("vor_agents.orchestrator._run_agent", _boom)
        return await audit_pattern(identity_key, {"triggered_by": "test"}, fake_firestore)

    async def test_third_consecutive_failure_writes_needs_attention(
        self, fake_firestore, monkeypatch
    ):
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

        for _ in range(3):
            await self._fail_audit_once(identity_key, fake_firestore, monkeypatch)

        doc = fake_firestore.collection(NEEDS_ATTENTION_COLLECTION).document(
            _doc_id(identity_key)
        ).get()
        assert doc.exists
        assert doc.to_dict()["failure_count"] == 3

    async def test_two_consecutive_failures_do_not_escalate(self, fake_firestore, monkeypatch):
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

        for _ in range(2):
            await self._fail_audit_once(identity_key, fake_firestore, monkeypatch)

        doc = fake_firestore.collection(NEEDS_ATTENTION_COLLECTION).document(
            _doc_id(identity_key)
        ).get()
        assert not doc.exists

    async def test_success_after_failures_resets_and_prevents_escalation(
        self, fake_firestore, monkeypatch
    ):
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

        for _ in range(2):
            await self._fail_audit_once(identity_key, fake_firestore, monkeypatch)

        async def _ok(*args, **kwargs):
            return {"action": "NO_ACTION", "invalidated_instance_ids": [], "concerns_found": [], "reasoning": "clean"}

        monkeypatch.setattr("vor_agents.orchestrator._run_agent", _ok)
        await audit_pattern(identity_key, {"triggered_by": "test"}, fake_firestore)

        # One more failure after the reset -- count should be 1, not 3.
        await self._fail_audit_once(identity_key, fake_firestore, monkeypatch)

        doc = fake_firestore.collection(NEEDS_ATTENTION_COLLECTION).document(
            _doc_id(identity_key)
        ).get()
        assert not doc.exists


class TestFailureCountBlocksSuppress:
    """Same shape as TestProvisionalTierBlocksSuppress -- SUPPRESS is
    deterministically overridden once failure_count crosses the
    escalation threshold, mirroring the under_review/provisional
    overrides already in classify_alert()."""

    async def test_suppress_overridden_when_failure_count_at_threshold(
        self, fake_firestore, baseline_alert, diverse_confirmed_instances, monkeypatch
    ):
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)
        identity_key = pattern_identity_key(baseline_alert)
        doc_ref = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key))
        doc_ref.set({"failure_count": 3}, merge=True)

        async def _fake_run_agent(*args, **kwargs):
            return {
                "decision": "SUPPRESS",
                "matched_pattern_id": "test",
                "uncertain_reason": "not_applicable",
                "structural_deviations_found": [],
                "reasoning": "matches template",
            }

        monkeypatch.setattr("vor_agents.orchestrator._run_agent", _fake_run_agent)
        result, _ = await classify_alert(baseline_alert, fake_firestore)

        assert result.decision == "UNCERTAIN"
        assert result.uncertain_reason == "audit_failing"

    async def test_failure_count_below_threshold_does_not_override(
        self, fake_firestore, baseline_alert, diverse_confirmed_instances, monkeypatch
    ):
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)
        identity_key = pattern_identity_key(baseline_alert)
        doc_ref = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key))
        doc_ref.set({"failure_count": 2}, merge=True)

        async def _fake_run_agent(*args, **kwargs):
            return {
                "decision": "SUPPRESS",
                "matched_pattern_id": "test",
                "uncertain_reason": "not_applicable",
                "structural_deviations_found": [],
                "reasoning": "matches template",
            }

        monkeypatch.setattr("vor_agents.orchestrator._run_agent", _fake_run_agent)
        result, _ = await classify_alert(baseline_alert, fake_firestore)

        assert result.decision == "SUPPRESS"
```

Add to the imports at the top of `tests/test_orchestrator.py`:

```python
from vor_agents.enrichment import CONFIDENCE_COLLECTION, _doc_id
from vor_agents.identity import pattern_identity_key
from vor_agents.review_flag import NEEDS_ATTENTION_COLLECTION
```

(Keep whatever's already imported from these modules -- add only what's missing.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_orchestrator.py -k "FailureEscalation or FailureCountBlocksSuppress" -v`
Expected: `needs_attention` docs never written (feature doesn't exist yet); SUPPRESS not overridden (no `failure_count` check in `classify_alert()` yet).

- [ ] **Step 3: Update `vor_agents/orchestrator.py`**

Add the import and constant near the top of the file (with the other imports from `.review_flag`):

```python
from .review_flag import NEEDS_ATTENTION_COLLECTION, clear_under_review, mark_under_review, record_needs_attention
```

Add the threshold constant right after the existing module-level `session_service` assignment:

```python
AUDIT_FAILURE_ESCALATION_THRESHOLD = 3
# Unvalidated starting point, same posture as GRADUATION_THRESHOLD /
# MIN_DIVERSITY elsewhere in this design -- no real audit-failure-rate
# data exists yet to calibrate against.
```

Add the new override in `classify_alert()`, right after the existing provisional-tier override block and before the ground-truth reconciliation (`if precomputed_deviations:`):

```python
    if (
        enrichment.get("failure_count", 0) >= AUDIT_FAILURE_ESCALATION_THRESHOLD
        and classifier_output.decision == "SUPPRESS"
    ):
        # Same shape as the under_review/provisional-tier overrides above:
        # a pattern whose audits keep failing has never actually been
        # re-verified, no matter how many times the flag got cleared by
        # audit_pattern()'s try/finally. Force UNCERTAIN rather than let
        # a stale, never-successfully-audited pattern keep autonomously
        # suppressing. See docs/superpowers/specs/
        # 2026-08-24-audit-failure-escalation-design.md.
        classifier_output = classifier_output.model_copy(
            update={
                "decision": "UNCERTAIN",
                "uncertain_reason": "audit_failing",
                "reasoning": (
                    classifier_output.reasoning + " [Vör correctness override: this "
                    "pattern's audits have failed repeatedly and it has not been "
                    "successfully re-verified; SUPPRESS not allowed until a human "
                    "resolves it.]"
                ),
            }
        )
```

Replace `audit_pattern()`'s body:

```python
async def audit_pattern(
    identity_key: tuple[str, ...], pattern_data: dict[str, Any], firestore_client: Client
) -> AuditorOutput:
    """
    Full audit path for one flagged pattern -- see the existing docstring
    for the mark/try/except/finally shape (unchanged). New in this
    revision: tracks consecutive failures via clear_under_review()'s
    audit_failed param, and once AUDIT_FAILURE_ESCALATION_THRESHOLD is
    crossed, writes a needs_attention doc AND classify_alert() forces
    UNCERTAIN for this pattern going forward (see that function's
    failure_count override) -- both the in-band and out-of-band halves
    of the escalation design.
    """
    mark_under_review(identity_key, firestore_client)
    audit_failed = False
    last_error_repr = ""

    try:
        doc = (
            firestore_client.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key)).get()
        )
        confirmed_instances = (doc.to_dict() or {}).get("confirmed_instances", [])

        prompt = (
            f"Pattern under review:\n{json.dumps(pattern_data, indent=2)}\n\n"
            f"Confirmed instances (cite instance_id values from this list only "
            f"if downgrading):\n{json.dumps(confirmed_instances, indent=2)}\n\n"
            "Review this suppression decision per your instructions."
        )
        auditor = build_auditor_agent()
        result = await _run_agent(
            auditor, prompt, session_id=f"audit_{'_'.join(identity_key)}_{uuid.uuid4()}"
        )
        decision = AuditorOutput.model_validate(result)
    except Exception as exc:  # noqa: BLE001 — deliberately catch-all, see
        # existing docstring rationale; unchanged from before this plan.
        audit_failed = True
        last_error_repr = repr(exc)
        logger.bind(identity_key=identity_key).exception("Audit failed")
        decision = AuditorOutput(
            action=AuditorAction.NO_ACTION,
            reasoning=f"Audit failed with error: {exc!r}",
        )
    finally:
        new_failure_count = clear_under_review(
            identity_key, firestore_client, decision.model_dump(), audit_failed=audit_failed
        )

    if audit_failed and new_failure_count >= AUDIT_FAILURE_ESCALATION_THRESHOLD:
        logger.bind(identity_key=identity_key, failure_count=new_failure_count).critical(
            "Audit failed {} consecutive times; pattern needs human attention",
            new_failure_count,
        )
        try:
            record_needs_attention(identity_key, new_failure_count, last_error_repr, firestore_client)
        except Exception as record_exc:  # noqa: BLE001 — deliberate: a
            # failure to record the escalation must never propagate out
            # of audit_pattern() and must never prevent the
            # clear_under_review() write above, which already happened.
            logger.bind(identity_key=identity_key).error(
                "Failed to record needs_attention doc: {}", record_exc
            )

    return decision
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_orchestrator.py -v`
Expected: all tests in the file PASS, including the 5 new ones.

- [ ] **Step 5: Run the full suite and lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check . && .venv/bin/python -m black --check . && .venv/bin/python -m mypy vor_agents/ main.py && .venv/bin/python -m bandit -r vor_agents/ main.py`
Expected: all pass, no regressions.

- [ ] **Step 6: Commit**

```bash
git add vor_agents/orchestrator.py tests/test_orchestrator.py
git commit -m "Escalate to needs_attention and block SUPPRESS after 3 consecutive audit failures"
```

---

## Task 5: `docs/DEPLOY.md` note

**Files:**
- Modify: `docs/DEPLOY.md`

**Interfaces:**
- None (documentation only).

- [ ] **Step 1: Add a short section after step 3a (Cloud Tasks queue)**

```markdown
## 3b. needs_attention collection (no setup required)

A pattern whose audits fail 3 times consecutively gets a doc written to
the `needs_attention` Firestore collection (same project, same
credentials already in use -- Firestore is schemaless, nothing to
provision). **Nothing currently pushes this to a human** -- no
dashboard, no alerting integration. Check it manually:

```bash
gcloud firestore documents list --collection-ids=needs_attention
```

Revisit once there's an actual notification channel to wire this into.
```

- [ ] **Step 2: Commit**

```bash
git add docs/DEPLOY.md
git commit -m "Document the needs_attention collection has no alerting wired up yet"
```

---

## Final verification

- [ ] Run `.venv/bin/python -m pytest -v` -- full suite passes.
- [ ] Run `.venv/bin/python -m ruff check . && .venv/bin/python -m black --check . && .venv/bin/python -m mypy vor_agents/ main.py && .venv/bin/python -m bandit -r vor_agents/ main.py` -- all clean.
- [ ] Confirm `git log --oneline -5` shows one commit per task.
- [ ] Update `docs/TODO-Aug24.md` Task 6 checkbox to done, referencing the commits.
