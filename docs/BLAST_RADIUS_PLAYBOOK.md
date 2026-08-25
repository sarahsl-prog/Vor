# Vör — Blast Radius Playbook

Guidelines for scoring and proposing `blast_radius_estimate` values. This
exists so blast radius stays grounded in stated criteria — for a human
maintaining `blast_radius.py`'s table, and for anyone (human or LLM step)
proposing a new entry.

## The asymmetry that matters
A LOW score means "audit this less." A HIGH score means "audit this more."
**Under-scoring is the dangerous direction** — it quietly reduces scrutiny
on something that turns out to matter. Same shape as the auditor's
DOWNGRADE/RECOMMEND_UPGRADE split: the safe direction (scoring something
HIGH/CRITICAL) can happen freely; the risky direction (scoring something
LOW) needs a human to actually commit it to the trusted table.

## Tiers

**CRITICAL (0.90–1.0)** — direct path to credential material, identity
infrastructure, or a known active-exploit surface.
Examples: process reads/writes LSASS memory or credential stores; endpoint
matches a publicly known unauthenticated-RCE/privesc pattern (e.g. the
CVE-2026-56164 `ToolPane_admin` endpoint family).

**HIGH (0.60–0.89)** — privileged execution context, internet-facing
service, or capable of persistence/code execution.
Examples: SYSTEM-context process spawning unexpected children;
internet-facing service worker (e.g. `w3wp.exe`); anything that can write
executable files or scheduled tasks.

**MEDIUM (0.30–0.59)** — internal-only, privileged-adjacent, or has write
access to sensitive-but-not-credential paths.
Examples: internal service accounts with elevated-but-scoped permissions;
processes that write to shared config, not identity/credential material.

**LOW (0.0–0.29)** — standard user-context, internal-only, no known
escalation path.
Examples: ordinary user-session applications with no privilege boundary
crossing and no network exposure.

**Unscored default is HIGH (0.75), never LOW.** A pattern nobody has
assessed yet must not be quietly treated as safe — see `UNSCORED_DEFAULT`
in `blast_radius.py`. Getting scored down to MEDIUM or LOW is a decision
someone has to actually make, not something that happens by default.

## Proposing a new table entry

A proposal must cite:
1. **Which tier** and why, using the criteria above — not a vibe.
2. **Specific structural indicators** the score is based on (parent
   process, endpoint family, privilege level, network exposure) — not
   general reputation ("this looks suspicious").
3. **What would change the score** — what evidence would move this entry
   up or down a tier, so the entry stays revisable rather than frozen.

Proposals scoring something CRITICAL or HIGH commit directly into the
table — that's the conservative direction, and `propose_blast_radius()`
does this automatically, no separate step. **Any proposal at MEDIUM or
LOW requires a human to review and commit it** — `propose_blast_radius()`
writes it as a pending record instead (`status="pending_human_review"`);
`commit_blast_radius_proposal()`, reached via `POST /blast-radius/commit`,
is the only way it becomes live. No proposal — human or LLM-authored —
goes straight into the table at MEDIUM/LOW without that explicit human
commit step. See `blast_radius.py` for both functions.

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
