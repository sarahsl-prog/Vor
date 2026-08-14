# Vör — Testing Plan

## Philosophy
Test the deterministic layer exhaustively — it's cheap, fast, and it's
where the actual safety-critical logic lives (graduation gating, the
DOWNGRADE/RECOMMEND_UPGRADE asymmetry, blast radius honesty rules). Test
the LLM-facing layer's *reconciliation logic* with the model mocked out —
never depend on a real Gemini call in CI. Real-model behavior is a
separate concern (manual/demo validation), not something unit tests
should gate on.

## Coverage map

| Module | File | What's covered |
|---|---|---|
| `identity.py` | `test_identity.py` | Identity key construction/exclusion, two-part graduation gate (the low-diversity-but-meets-count regression case), exhaustive diffing |
| `evidence_diversity.py` | `test_evidence_diversity.py` | Empty/single/diverse/low-diversity cases, graceful degradation on missing fields |
| `blast_radius.py` | `test_blast_radius.py` | Table matching, worst-case-wins on multiple matches, `UNSCORED_DEFAULT`, the MEDIUM/LOW human-review gate, proposals never mutate the table |
| `enrichment.py` | `test_enrichment.py` | `enrich()` (incl. regression test for the diversity_score field-name bug), graduation, seeding, targeted invalidation |
| `review_flag.py` | `test_review_flag.py` | `under_review` lifecycle, `last_reviewed_at` stamping, DOWNGRADE/RECOMMEND_UPGRADE/NO_ACTION each handled correctly |
| `audit_targets.py` | `test_audit_targets.py` | Priority ordering (blast radius weighted heaviest), `max_targets` |
| `orchestrator.py` | `test_orchestrator.py` | **The asymmetric reconciliation** — model mocked, all four cases: correct SUPPRESS, model misses a real deviation (override), model over-cautious (no override), NO_HISTORY skips reconciliation |
| `main.py` | `test_main.py` | `/healthz`, `/classify` firing background audit on SUPPRESS, the duplicate-audit guard, `/sweep` |
| Known gaps | `test_known_gaps.py` | `xfail`-marked regression test for the identity-key delimiter fragility — tracked, not silently ignored |

## What's deliberately NOT covered yet
- `classifier_agent.py` / `auditor_agent.py` — these are prompt strings +
  `Agent()` construction, not logic. Worth a smoke test that they
  construct without error, not much else to unit test.
- Live Gemini calls — no integration suite against the real API yet. Would
  need a separate, explicitly-marked (`@pytest.mark.integration`) suite
  that's excluded from the default CI run, since it costs money and isn't
  deterministic.
- Cloud Run / Cloud Scheduler deployment itself — nothing's actually
  deployed, so there's nothing live to test against yet.
- The 6 synthetic dataset cases as end-to-end fixtures — `conftest.py` has
  fixtures modeling cases #3 (drift) and #6 (field-level deviation)
  specifically, since those are what the reconciliation tests need. The
  full dataset generation is still a separate to-do.

## Running locally
```
pip install -r requirements.txt -r requirements-dev.txt
pytest
ruff check .
```
