# Vör — "Trust, audited."

Self-tuning confidence layer for Windows Event Log / Hayabusa-style alert
triage. All Things Agentic Hackathon, Collaborative Partner track.

First real build step — everything before this was design (see full
history in Obsidian `Projects/Adaptive-Alert-Agent/`).

## Structure

| File | What it is | Calls an LLM? |
|---|---|---|
| `vor_agents/schemas.py` | Pydantic output schemas for both agents | No |
| `vor_agents/identity.py` | Pattern identity key, structural template, diff logic | No |
| `vor_agents/enrichment.py` | Firestore reads/writes feeding the classifier | No |
| `vor_agents/review_flag.py` | `under_review` race-condition fix | No |
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

## Known gaps — not yet resolved
1. `_fetch_all_suppressed_patterns()` in `orchestrator.py` is an
   unimplemented stub — straightforward Firestore query once
   `confidence_docs` has real data.
2. `GRADUATION_THRESHOLD = 3` (in `identity.py`) still unvalidated against
   expected review load.
3. No Cloud Scheduler / Cloud Run wiring yet for the weekly sweep or the
   event-trigger listener — `run_scheduled_sweep()` and `classify_alert()`
   are ready to be called from either, but the trigger plumbing (Pub/Sub
   topic, Cloud Scheduler job, Cloud Run endpoint) isn't built.
4. `precomputed_deviations` in `orchestrator.classify_alert()` is computed
   but currently unused — flagged as a future correctness check (compare
   Python-computed deviations against what the model reports).
5. `evidence_diversity_score` and `blast_radius_estimate` (referenced by
   `select_audit_targets()`) still aren't defined anywhere — same open gap
   from the design phase, not yet built.

## Not yet built
- Dataset generation for the 6 synthetic cases
- Seeding script using `enrichment.seed_template()`
- Cloud Run deployment config
