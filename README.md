# Vör — "Trust, audited."

Vör is a self-tuning confidence layer for Windows Event Log / Hayabusa-style
alert triage that decides when it's safe to autonomously suppress a known-benign
alert and when to escalate to a human — without letting that trust go stale or
unnoticed. A classifier agent makes the call; a separate auditor agent
periodically re-checks past suppressions against the actual evidence behind
them, with the authority to downgrade trust on its own but never to grant it
without a human signing off.

All Things Agentic Hackathon, **The Taskmaster** track (switched from
Collaborative Partner — see rationale below).

First real build step — everything before this was design (see full
history in Obsidian `Projects/Adaptive-Alert-Agent/`).

## Track — The Taskmaster
Switched from Collaborative Partner after the design pivoted from
personalization (Mimir-era) toward audit/trust. Judging rubric is the same
across all tracks (40% Innovation & Operational Utility, 30% Architectural
Discipline & Tech Stack, 30% Demo & Production Readiness), so track choice
affects framing and prize pool, not scoring mechanics.

- **Collaborative Partner ruled out**: actual track wording is "ask
  clarifying questions, guide the user step-by-step... adapts to the
  user's unique way of thinking" — a live human-facing coaching loop. Vör
  has no user turn-taking left at all.
- **Fortified Enterprise Fleet considered and ruled out despite strong
  thematic fit** ("audit their reasoning, trust their data handling, scale
  them safely" almost literally describes what the auditor agent does):
  the real submission bar is heavier than it first looked — Agent
  Registry, Agent Identity, Agent Gateway, Model Armor, Agent
  Observability, cross-department cataloging, weeks-long async state.
  Vör today is one Firestore collection and two agents; getting to a
  genuine (not just thematic) fit was too much added scope this close to
  the deadline.
- **Taskmaster fits without added scope**: "Make one that takes action...
  handles the details... proves it can do the heavy lifting for you."
  Maps directly onto the classifier autonomously deciding
  SUPPRESS/ESCALATE/UNCERTAIN with no human in the loop for the common
  case — hits the 40%-weighted "autonomous, high-value action... little
  to no hand-holding" criterion using the architecture already built.

Also worth targeting regardless of main track: **Best Architectural
Design** ($5,000, 2 winners) is a track-agnostic bonus category, and
architectural rigor is the strongest asset here.

## Structure

| File | What it is | Calls an LLM? |
|---|---|---|
| `vor_agents/schemas.py` | Pydantic output schemas for both agents | No |
| `vor_agents/identity.py` | Pattern identity key, structural template, diff logic | No |
| `vor_agents/enrichment.py` | Firestore reads/writes feeding the classifier | No |
| `vor_agents/review_flag.py` | `under_review` race-condition fix | No |
| `vor_agents/evidence_diversity.py` | Pure computation of evidence diversity from confirmed instances | No |
| `vor_agents/blast_radius.py` | Hybrid curated table + gated proposal path for risk scoring | No |
| `vor_agents/audit_targets.py` | Deterministic auditor target prioritization | No |
| `vor_agents/classifier_agent.py` | ADK `Agent` definition, classifier prompt | Yes (Gemini) |
| `vor_agents/auditor_agent.py` | ADK `Agent` definition, auditor prompt, separate context | Yes (Gemini) |
| `vor_agents/orchestrator.py` | Wires everything together, only place that calls `Runner` | Orchestrates both |

## Design principle carried through the scaffold
Neither agent has ADK `tools=` attached. Enrichment, template-building, and
all Firestore writes happen in plain Python **before or after** the agent
call, never inside it. This was a deliberate choice, not a limitation
worked around — see `classifier_agent.py` and `auditor_agent.py` docstrings.
It also means this scaffold doesn't depend on ADK's `output_schema` +
`tools` compatibility (which has changed across versions) at all.

## Confidence representation — resolved: targeted evidence invalidation
Every confirmed instance gets a stable `instance_id` (assigned in
`record_confirmed_negative()` / `seed_template()`). On DOWNGRADE, the
auditor cites specific `invalidated_instance_ids` — only those instances
are removed from the pool, and `invalidate_instances()` in `enrichment.py`
rebuilds the template from what remains. Tier is a *consequence* of the
rebuild, not something the caller force-sets: a pattern with 9 good
instances and 1 bad one loses only the bad one and can stay "confirmed"
with a corrected template; a pattern where most evidence gets invalidated
naturally falls back to "provisional." No separate confidence float —
this replaces that idea rather than living alongside it.

The auditor prompt (`auditor_agent.py`) now requires citing real
`instance_id` values from the `confirmed_instances` list it's shown, never
inventing one, and allows citing *every* ID as an explicit full
invalidation when the concern is genuinely pattern-wide.

## Audit prioritization scoring — resolved
Both inputs `select_audit_targets()` needed are now real:

- **`evidence_diversity_score`** (`evidence_diversity.py`): pure
  computation over `confirmed_instances` — distinct-value ratio across
  host/user/hour-of-day, averaged. Catches the failure mode the auditor
  prompt already warns about: 20 confirmations from the same host/user/
  hour is weak evidence dressed up as strong.
- **`blast_radius_estimate`** (`blast_radius.py`): hybrid design per
  `BLAST_RADIUS_PLAYBOOK.md`. A curated table (`BLAST_RADIUS_TABLE`) maps
  structural indicators (parent process, endpoint family) to a risk tier;
  unmatched patterns default to HIGH, never LOW, so an unassessed pattern
  isn't silently trusted. New entries can be proposed via
  `propose_blast_radius()`, but that never writes the table directly —
  CRITICAL/HIGH proposals may be added straight to the table (the safe
  direction), MEDIUM/LOW proposals are gated behind human review (the
  direction that reduces scrutiny, same asymmetry as the auditor's
  DOWNGRADE/RECOMMEND_UPGRADE split).

`_fetch_all_confirmed_patterns()` in `orchestrator.py` is now
implemented using both, plus a `last_reviewed_at` timestamp (newly stamped
by `clear_under_review()` on every audit outcome, not just downgrades) to
compute `days_since_last_review`. Never-audited patterns get a large
sentinel value rather than 0, so they don't look artificially low-priority.

## Graduation threshold — resolved: two-part gate
`GRADUATION_THRESHOLD = 3` alone was statistically weak: if a diffable
field is genuinely variable rather than truly invariant (say a real 80/20
split), the odds of 3 random confirmations all landing on the same value
are roughly 51% — close to a coin flip that a "confirmed" template locks
in a field as trusted when it isn't. Real review-volume data to calibrate
against doesn't exist yet (same open gap as elsewhere in this design), so
rather than guess at a "correct" count, graduation now requires **both**
`instance_count >= GRADUATION_THRESHOLD` **and**
`evidence_diversity_score >= MIN_DIVERSITY` (`identity.py`). Count alone
can pass on repetition (same host/user/hour logged three times); diversity
alone with too few instances is just noise. `MIN_DIVERSITY = 0.5` is a
starting point, explicitly flagged as unvalidated, same as the count.

Also fixed in the same pass: `enrich()` in `enrichment.py` was reading a
field name (`evidence_diversity_score`) that never matched what any write
path actually stored (`diversity_score`) — it was silently always
returning the `0.0` default. Naming is now consistent across
`build_structural_template()`'s return value, every Firestore write path,
and `enrich()`'s read.

## Cloud Run / Cloud Scheduler wiring — resolved
`main.py`, `Dockerfile`, and `DEPLOY.md` added. `POST /classify` is the
event-triggered primary path — a SUPPRESS decision means the pattern's
identity key just matched an incoming alert again, exactly the trigger
condition the hybrid cadence was designed around, so it fires an audit as
a background task. `POST /sweep` is the scheduled safety net, meant to be
hit weekly by Cloud Scheduler with an OIDC-authenticated request (see
DEPLOY.md steps 2–3) — neither endpoint should ever be deployed with
`--allow-unauthenticated`.

`classify_alert()` now returns `(result, identity_key)` instead of just
the `ClassifierOutput` — the identity key comes from `enrich()`'s
deterministic computation, not from parsing the model's own
`matched_pattern_id` text, which would have repeated the same fragile-
split problem as gap #1 below, one layer less reliable since it'd also
depend on the model formatting that string consistently.

Also added: a guard in `/classify` that checks `under_review` before
firing a background audit, so a burst of the same SUPPRESS-eligible
pattern arriving faster than one audit completes doesn't schedule
duplicate concurrent auditor calls for the same identity key.

**Real caveat, not fully resolved**: firing an audit on every single
SUPPRESS decision could get expensive at real alert volume — this wasn't
throttled or sampled, just gated against duplicates. Worth revisiting
with actual traffic data (same "no real volume to calibrate against" gap
as `GRADUATION_THRESHOLD`/`MIN_DIVERSITY`) — a rate limit or sampling
strategy per identity key is the likely fix once that data exists.

**Also not resolved**: `/classify` has no actual trigger source wired up
yet — nothing currently calls it. See DEPLOY.md step 4.

## Model backend — resolved: Vertex AI, not the Gemini API key
Both agents just pass a plain model string (`model="gemini-2.0-flash"`)
to ADK's `Agent` — no explicit client construction in
`classifier_agent.py`/`auditor_agent.py`. Backend selection is entirely
environment-variable-driven, read by `google-genai` (an ADK dependency):
`GOOGLE_GENAI_USE_VERTEXAI=true` + `GOOGLE_CLOUD_PROJECT` +
`GOOGLE_CLOUD_LOCATION` route through Vertex AI using the caller's
Application Default Credentials; their absence (with `GOOGLE_API_KEY`
set instead) falls back to the Gemini Developer API. `.env` and
`DEPLOY.md` are both configured for Vertex AI — matches "meant to be run
in Google Cloud" from `CLAUDE.md`, and means the Cloud Run service
authenticates to the model as itself (its own service account, granted
`roles/aiplatform.user` — see DEPLOY.md step 3a) rather than carrying a
separate API key as a secret to manage and rotate.

## Precomputed deviations — resolved: asymmetric reconciliation
`precomputed_deviations` is no longer computed-and-discarded. After the
classifier agent returns, its reported deviations are compared against
the deterministic diff by field name (not exact string match — the model
isn't guaranteed to phrase `template=X, observed=Y` identically to the
Python-generated version, so comparison is on which fields disagreed, not
the literal text).

Same asymmetry as everywhere else in this design:
- **Ground truth found a deviation the model didn't report, and the model
  still said SUPPRESS** — the dangerous direction. Overridden to ESCALATE
  automatically, in code, not by asking the model to reconsider. The
  override is recorded in the returned `reasoning` text so it's visible,
  never silent.
- **The model reported a deviation ground truth didn't find** — the model
  being more cautious than the deterministic check. Safe direction, no
  override, decision stands as-is.

This closes the loop the original design flagged: the model's diffing was
"authoritative" only in the sense that nothing was checking it. Now a
model failing to notice a real deviation can't silently result in an
autonomous SUPPRESS.

## Known gaps

**Identity-key round-trip fragility — resolved.** `_doc_id()` now hashes the
identity_key tuple instead of joining it with `"_"`, and every write path
stores `identity_key` as its own Firestore field; readers use that field
instead of parsing the doc ID. See `docs/TODO-Aug15.md` Task 3.

**Still open:** Firestore data written *before* that change has no
`identity_key` field. `_fetch_all_confirmed_patterns()` skips and logs a
warning for any doc missing it rather than crashing, so this is not a
crash risk — but a one-time backfill is still needed before first deploy
against pre-existing data. See `scripts/backfill_identity_key.py`.

## Dataset and seeding

The 6 synthetic dataset cases are generated by `vor_agents/datasets.py`,
and `scripts/seed_firestore.py` seeds them — or your own real
confirmed-negative history — into Firestore via
`enrichment.seed_template()`. See `docs/DATASET_RUNBOOK.md` for what each
case models and how to run both.

## Not yet built
- An exporter that turns raw Hayabusa/EVTX output into the JSON history
  `scripts/seed_firestore.py --file` expects — that shape depends on your
  ingest pipeline.
