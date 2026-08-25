# Vör — Dataset & Seeding Runbook

What the 6 synthetic dataset cases actually are, and how to get evidence —
synthetic or real — into a Firestore instance.

Pairs with `vor_agents/datasets.py` (generation), `scripts/seed_firestore.py`
(seeding), and `docs/TESTING_PLAN.md` (what's covered where).

---

## Why these 6 cases

The cases span Vör's **decision surface**, not a taxonomy of alert types.
Vör's only real question is *"is it safe to suppress this autonomously?"*,
and there are six distinct ways that question resolves:

| # | Case | Models | Should never autonomously SUPPRESS? |
|---|---|---|---|
| 1 | `seeded_confirmed` | Bulk-imported history, enters at confirmed tier, provenance `seeded` | No — SUPPRESS permitted |
| 2 | `live_confirmed` | Same pattern earned one alert at a time, provenance `live` | No — SUPPRESS permitted |
| 3 | `identity_drift` | CVE-2026-56164-modeled: `w3wp.exe` spawns `cmd.exe`, a *different identity key* | **Yes** — no history for that key |
| 4 | `low_diversity` | Meets `GRADUATION_THRESHOLD` by count, fails `MIN_DIVERSITY` | **Yes** — stays provisional |
| 5 | `insufficient_history` | Below `GRADUATION_THRESHOLD` | **Yes** — stays provisional |
| 6 | `field_deviation` | Same identity key, all 5 `DIFFABLE_FIELDS` deviate | **Yes** — ESCALATE |

Read as three pairs:

- **1 & 2 — trust, earned two ways.** Identical evidence, different
  provenance. They exist as a pair because provenance is a trust claim
  the auditor is entitled to see: `seeded` means no human signed off on
  any single instance, however good the source dataset was.
- **3 & 6 — deviation, at two levels.** #3 never reaches field-level
  diffing at all (different identity key ⇒ different pattern, so a
  trusted pattern's evidence is not transferable to it). #6 does: a
  *known* pattern behaving wrongly, which is the harder and more
  dangerous case. If the model calls #6 SUPPRESS anyway,
  `classify_alert()`'s deterministic reconciliation overrides it.
- **4 & 5 — the two halves of the graduation gate.** #4 has enough
  evidence of too-uniform a kind; #5 has too little evidence of a
  perfectly good kind. Both stay provisional, for opposite reasons.
  #4 is specifically why the gate is two-part rather than a raw count:
  three repetitions on one host at one hour is one observation, not three.

Cases #1, #3 and #6 were already named in the codebase
(`enrichment.seed_template`, `conftest.drift_alert_cve_model`,
`test_identity.py`) — those numbers are preserved. **Cases #2, #4 and #5
were never enumerated anywhere**; they were chosen here to complete the
decision surface above. If the original intent for those three differs,
this is the place to correct it.

---

## Generating a case

```python
from vor_agents.datasets import DatasetCase, generate_case, generate_all

case = generate_case(DatasetCase.LOW_DIVERSITY, seed=0)
case["instances"]         # confirmed-negative history to seed
case["probe_alert"]       # the alert to then classify against it
case["expected_outcome"]  # what should happen, and why

generate_all(seed=0)      # all six, keyed by case name
```

Generation is **deterministic**: the same `seed` always produces
byte-identical output, including timestamps. Anything built on this
dataset — a seeded Firestore, a demo, a threshold sanity check — is
therefore reproducible. Vary `seed` to get different host/user/time
spread; structural fields never vary with the seed.

`expected_outcome` is documentation, not an assertion. Tests in
`tests/test_datasets.py` assert each case against the *real* graduation,
diversity and diffing code — a synthetic case that doesn't actually
provoke the behavior it claims to would be worse than no dataset at all.

---

## Seeding Firestore

Both paths go through `enrichment.seed_template()`, which stamps every
instance `verified_by: "bulk"` and provenance `"seeded"`. Don't work
around that labelling — it is what tells the auditor no human signed off
on these individually.

**Synthetic** (demos, a fresh dev project):

```bash
.venv/bin/python scripts/seed_firestore.py --case seeded_confirmed --dry-run
.venv/bin/python scripts/seed_firestore.py --case seeded_confirmed
```

**Real history** (the production path) — a JSON list of alert objects,
each carrying the four identity fields and all five `DIFFABLE_FIELDS`:

```bash
.venv/bin/python scripts/seed_firestore.py --file history.json --dry-run
.venv/bin/python scripts/seed_firestore.py --file history.json
```

Instances are grouped by identity key automatically, so one file may
contain many patterns. The whole file is validated **before** anything is
written: a malformed record at line 4,000 refuses the run rather than
leaving a half-seeded collection behind.

Always `--dry-run` first. It reports the tier each batch *would* land at
without writing:

```
[dry-run] would seed 2 instance(s) across 1 pattern(s):
  provisional  ('SharePoint_ToolPane_Rule', 'w3wp.exe', 'csc.exe', 'ToolPane_admin')  <-- below the graduation gate
```

A batch landing at `provisional` is the graduation gate working, not a
failure — but you should see it rather than assume `confirmed`.

---

## Related runbooks

- `docs/DEPLOY.md` — deploying the service; also covers seeding the
  blast-radius table (`scripts/seed_blast_radius_table.py`) and the
  one-time `identity_key` backfill (`scripts/backfill_identity_key.py`).
- `docs/TESTING_PLAN.md` — what's tested where, and what deliberately isn't.
- `docs/BLAST_RADIUS_PLAYBOOK.md` — how blast-radius scores are set and promoted.

---

## Still open

- **No real-history exporter.** `--file` takes a JSON list; producing that
  list from actual Hayabusa/EVTX output is not built here and depends on
  your ingest pipeline.
- **No end-to-end fixture wiring.** `tests/conftest.py` still carries its
  own hand-written fixtures for cases #3 and #6, predating
  `vor_agents/datasets.py`. They agree with the generated cases today but
  are maintained separately; worth collapsing onto the generator next time
  either is touched.
