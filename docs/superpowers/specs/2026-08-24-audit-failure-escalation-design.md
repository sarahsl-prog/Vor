# Consecutive audit-failure escalation — design

**Status:** approved for implementation (spec review pending)
**Date:** 2026-08-24
**Classification:** correctness / operational safety

## Problem

`audit_pattern()`'s `try/except/finally` (added in `docs/TODO-Aug15.md`
Task 1) fixed the *stuck* failure mode — `under_review` no longer gets
permanently stranded `True` on an audit failure. But it introduced a
quieter one: a *repeatedly*-failing audit now clears the flag and records
`AuditorOutput(action=NO_ACTION, reasoning=f"Audit failed with error:
{exc!r}")` every time, forever, with nothing but a log line marking each
occurrence. A pattern whose audit fails every single time — a
consistently malformed stored template, a prompt the model reliably can't
satisfy for this pattern's data shape — silently keeps autonomously
suppressing alerts, never actually re-verified, with no human ever
notified. This is TODO-Aug15's outstanding decision #4, unresolved until
now.

## Goal

Track consecutive audit failures per pattern. At a threshold, force the
pattern out of autonomous trust (deterministically, in code) **and**
leave a visible record a human can find — same "force the safe outcome
in code, don't rely on a log line getting noticed" principle used
throughout this codebase (`under_review`/provisional overrides in
`classify_alert()`, the Cloud Tasks dedup replacing a best-effort guard).

## Non-goals

- Retrying a failed audit sooner than the normal sweep/classify-trigger
  cadence would. This spec only adds tracking and escalation on top, not
  a faster retry loop. Note this is narrower than it first sounds: once a
  pattern crosses the escalation threshold, `classify_alert()` forces
  `UNCERTAIN` for it (see below), which means `/classify`'s
  SUPPRESS-only audit trigger stops firing for that pattern entirely —
  the classify-trigger path is genuinely removed, not just left at its
  normal cadence. The weekly sweep remains as the recovery path (see the
  `select_audit_targets()` interaction called out below); a human
  clearing the pattern manually is the other.
- Alerting infrastructure (PagerDuty, email, Slack). "Visible to a human"
  here means a Firestore record + log line a human dashboard/query can
  surface — wiring an actual notification channel is a separate,
  unscoped follow-up.
- Changing what counts as a "failure" — still exactly the same
  `except Exception` block `audit_pattern()` already has. Not expanding
  or narrowing that catch.

## Architecture

```
audit_pattern() except block
        |
   failure_count += 1 (persisted on the confidence doc)
        |
   failure_count >= 3 ?
        |                    \
       yes                    no
        |                      \
  force ESCALATE          (existing NO_ACTION
  on next classify         degrade, unchanged)
  + write needs_attention
  Firestore doc
```

## Components

### `vor_agents/review_flag.py` changes

- `clear_under_review()` already writes `last_reviewed_at` and
  `under_review: False` on every call. Add `failure_count` handling to
  the same write:
  - On a **successful** audit (the `try` block completed, no exception —
    `clear_under_review()` doesn't currently know this; see below),
    `failure_count` resets to `0`.
  - On a **failed** audit, `failure_count` increments by 1 from whatever
    was previously stored (`0` if absent).
- `clear_under_review()`'s signature needs a new parameter,
  `audit_failed: bool`, since it can't infer success/failure from
  `auditor_decision` alone (a real `NO_ACTION` from a *successful* audit
  and the synthetic `NO_ACTION` `audit_pattern()`'s except block
  constructs on failure are otherwise indistinguishable dicts).
  `audit_pattern()` passes `audit_failed=True` from its `except` branch,
  `audit_failed=False` from the normal path.

### `vor_agents/orchestrator.py` (`audit_pattern`) changes

- `except Exception as exc:` branch: after building the existing
  `decision` object, read the pattern's current `failure_count` from the
  Firestore doc (already fetched earlier in the `try` block for
  `confirmed_instances` — reuse that read, don't issue a second one) and
  compute `new_failure_count = failure_count + 1`.
- If `new_failure_count >= 3` (threshold, see below): write a
  `needs_attention` doc to a new `NEEDS_ATTENTION_COLLECTION` (Firestore),
  containing `identity_key`, `failure_count`, `last_error`
  (`repr(exc)`), `first_failed_at`/`last_failed_at` timestamps. Also log
  at `CRITICAL` via `loguru` (not just the existing `ERROR`-level
  `logger.exception`), so it's distinguishable in log-based alerting from
  a single transient failure.
- Pass `audit_failed=True` and the doc's current data into
  `clear_under_review()` (see above) so the failure count actually
  persists on the confidence doc itself, not just in the
  `needs_attention` side collection.

### `vor_agents/orchestrator.py` (`classify_alert`) changes

- New override, same shape and same position as the existing
  `under_review`/provisional-tier overrides (added right after the
  provisional-tier check, before the ground-truth reconciliation block):
  if `enrichment.get("failure_count", 0) >= 3` and
  `classifier_output.decision == "SUPPRESS"` → force `UNCERTAIN` /
  `uncertain_reason` — needs a new `UncertainReason` enum value (e.g.
  `AUDIT_FAILING`) in `vor_agents/schemas.py`, since none of the existing
  values (`no_history`, `graduation_pending`, `under_review`,
  `missing_data`) describes this case accurately.
- `enrich()` in `vor_agents/enrichment.py` needs to read and pass through
  `failure_count` from the confidence doc into the enrichment dict it
  returns, the same way it already surfaces `under_review` and `tier`.

### Threshold constant

- `AUDIT_FAILURE_ESCALATION_THRESHOLD = 3` — module-level constant in
  `orchestrator.py`, next to where `GRADUATION_THRESHOLD`/`MIN_DIVERSITY`
  live conceptually (`identity.py`). Explicitly flagged as unvalidated
  against real audit-failure-rate data, same posture as those two and as
  the Cloud Tasks retry-backoff defaults.

## Data flow

1. Audit fails (exception in `audit_pattern()`'s `try` block).
2. `except` branch increments `failure_count`, builds the degraded
   `NO_ACTION` decision as today.
3. If the incremented count crosses the threshold: write a
   `needs_attention` doc + log `CRITICAL`.
4. `finally` calls `clear_under_review(..., audit_failed=True)`, which
   persists the new `failure_count` on the confidence doc alongside the
   existing `under_review`/`last_reviewed_at` updates.
5. Next `classify_alert()` call for this identity key: `enrich()` surfaces
   `failure_count` in the enrichment dict; if it's `>= 3` and the model
   says SUPPRESS, the new override forces `UNCERTAIN` /
   `uncertain_reason="audit_failing"` — same deterministic-override
   pattern as every other trust gate in this file.
6. A subsequent **successful** audit (however it eventually gets
   triggered — `under_review` no longer blocks it, and the sweep/classify
   triggers still fire normally) resets `failure_count` to `0` via
   `clear_under_review(..., audit_failed=False)`, lifting the override.

## Error handling & retry safety

- Writing the `needs_attention` doc is itself a Firestore call that can
  fail. It's wrapped in its own `try/except`, logged at `ERROR` on
  failure, and never allowed to prevent `clear_under_review()`'s own
  write from happening in the `finally` block — a failure to *record*
  that escalation happened must not re-introduce the original stuck-flag
  bug this whole design exists to avoid.
- `failure_count` incrementing is read-then-write, not atomic
  (`firestore_client...update({"failure_count":
  firestore.Increment(1)})` would be the atomic alternative). Given
  audits for the same pattern are already serialized by
  `under_review`/Cloud Tasks dedup in the common case, a lost increment
  from a genuine race is a minor undercount, not a correctness bug on the
  scale of the identity-key collision issue fixed earlier — but using
  `firestore.Increment(1)` directly instead of read-then-write is simple
  enough to just do correctly from the start rather than accepting the
  race.

## Logging

`loguru`, `identity_key` bound as context on every call site, consistent
with the rest of the codebase. New: `logger.critical(...)` at the
escalation-threshold crossing — the first `CRITICAL`-level log call in
this codebase; confirm nothing downstream (log aggregation, alerting)
needs configuring to actually surface `CRITICAL` distinctly, or this is
just a differently-labeled `ERROR` in practice.

## Testing

- `tests/test_review_flag.py`: `clear_under_review()` with
  `audit_failed=True` increments `failure_count`; with `audit_failed=False`
  resets it to `0`; absent `failure_count` on a fresh doc treated as `0`
  before incrementing.
- `tests/test_orchestrator.py`: `audit_pattern()` failing 3 times
  consecutively (mock the agent call to always raise) results in a
  `needs_attention` doc being written on the 3rd; failing only 1–2 times
  does not write one; a success after 2 failures resets the count (a 4th
  consecutive failure after that reset does not immediately re-escalate).
  New `TestFailureEscalation` class, mirroring
  `TestProvisionalTierBlocksSuppress`'s structure from Task 21.
- `classify_alert()`: SUPPRESS overridden to UNCERTAIN when
  `failure_count >= 3`; ESCALATE left untouched when
  `failure_count >= 3` (override scoped to SUPPRESS only, same pattern as
  every other override test in this file); `failure_count == 2` does NOT
  trigger the override (threshold boundary test).

## Deploy (`DEPLOY.md` additions)

- No new Cloud Run/IAM changes — `needs_attention` is just a new
  Firestore collection, same project/credentials already in use, no new
  service account or role needed.
- Worth documenting a `gcloud firestore` query or a small script a human
  actually runs to check `needs_attention` — not built here (out of scope,
  see non-goals on alerting infra), but the doc should say explicitly
  "nothing pushes this to a human yet, someone has to go look," so it's
  not mistaken for a solved problem.

## Open items carried forward, not resolved here

- Threshold (3) is a guess, not calibrated against real audit-failure-rate
  data — explicitly flagged, same posture as `GRADUATION_THRESHOLD` etc.
- No actual notification channel — `needs_attention` docs are inert until
  someone builds a way to surface them (dashboard, scheduled digest,
  alerting integration).
- Resolved, not left open: `failure_count >= 3` DOES affect
  `select_audit_targets()` priority, as of this implementation.
  `clear_under_review()` no longer stamps `last_reviewed_at` when
  `audit_failed=True` — a failed audit is not a genuine review, so it no
  longer counts as one for staleness purposes. Since
  `select_audit_targets()` derives `days_since_last_review` from that
  stamp, a repeatedly-failing pattern's `last_reviewed_at` stays frozen
  at whenever it last succeeded (or absent, if it never has), and it
  grows steadily *more* stale — and therefore *higher* sweep priority —
  with every failed audit, instead of being reset to the bottom of the
  list each time. This was necessary, not optional: without it, the
  classify-trigger removal above and this stamping behavior would have
  combined to actively bury a failing pattern behind both of its own
  recovery paths at once.
