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
| `datasets.py` | `test_datasets.py` | All 6 synthetic cases, each asserted against the real graduation/diversity/diffing code rather than hardcoded expectations |
| `classifier_agent.py` / `auditor_agent.py` | `test_agents.py` | Construction smoke tests — name, output schema/key, no tools, non-empty prompt |
| `scripts/seed_firestore.py` | `test_seed_firestore.py` | `--file` input validation (validated before any write), identity grouping, tier reporting, dry-run |
| `scripts/backfill_identity_key.py` | `test_backfill_identity_key.py` | identity_key recovery from instances, re-key to the hashed doc ID, idempotency, unrecoverable-doc skip |
| Live Gemini | `test_integration_gemini.py` | Real-API round-trip into valid `ClassifierOutput`/`AuditorOutput`. Marked `integration`, deselected by default (`pytest.ini` `addopts`); run with `pytest -m integration`. Skips cleanly without credentials |
| Known gaps | `test_known_gaps.py` | `xfail`-marked regression test for the identity-key delimiter fragility — tracked, not silently ignored |

## What's deliberately NOT covered yet
- `classifier_agent.py` / `auditor_agent.py` beyond construction — these
  are prompt strings + `Agent()` construction, not logic.
  `test_agents.py` smoke-tests that they construct, carry no tools, and
  have non-empty instructions; there is nothing further to unit test.
- Cloud Run / Cloud Scheduler deployment itself — nothing's actually
  deployed, so there's nothing live to test against yet.
- Wiring the generated dataset cases into `conftest.py`'s fixtures.
  `vor_agents/datasets.py` now defines all 6 cases (see
  `docs/DATASET_RUNBOOK.md`) and `tests/test_datasets.py` asserts each one
  against the real graduation/diversity/diffing code. But `conftest.py`
  still carries its own hand-written fixtures for cases #3 and #6,
  predating that module. They agree today; they are maintained separately.
  Worth collapsing onto the generator next time either is touched.

## Running locally
```
pip install -r requirements.txt -r requirements-dev.txt
pytest                      # default run — integration tests deselected
ruff check .
```

The integration suite is excluded by `pytest.ini`'s
`addopts = -m "not integration"` because it calls the real Gemini API:
it costs money and isn't deterministic, so it must never gate CI. Run it
deliberately:

```
pytest -m integration -v
```

Without Vertex AI credentials configured (see `.env.example`) it reports
skipped rather than failing.
