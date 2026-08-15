"""
Vör — Deterministic audit target selection. No LLM call, no exceptions —
the auditor's judgment is spent on WHY a suppression might be wrong, not
on WHICH ones to look at. Keeping this arithmetic and inspectable is what
makes it safe for the same kind of reasoning being audited to not also be
the thing deciding what gets audited.
"""


def select_audit_targets(all_suppressed_patterns: list[dict], max_targets: int = 10) -> list[dict]:
    """
    Priority score = weighted sum of:
      - days_since_last_review (older = higher priority)
      - inverse of evidence_diversity_score (thinner evidence = higher priority)
      - blast_radius_estimate (privileged process, internet-facing,
        domain-controller-adjacent = higher priority)

    Each input pattern dict is expected to have these three numeric fields
    plus an "identity_key" tuple/string for downstream lookup.

    days_since_last_review is clamped to >= 0 here too (not just at the
    _fetch_all_suppressed_patterns call site that normally produces it) —
    this function's own contract shouldn't depend on every caller already
    having sanitized its input. A negative value (clock skew: a
    last_reviewed_at timestamp in the future) would otherwise pull a
    pattern's priority below zero and rank it under patterns that are
    genuinely never-audited, the opposite of "needs attention."
    """
    def priority(pattern: dict) -> tuple[float, str]:
        days_since = max(pattern["days_since_last_review"], 0)
        score = (
            days_since * 1.0
            + (1.0 - pattern["evidence_diversity_score"]) * 2.0
            + pattern["blast_radius_estimate"] * 3.0
        )
        # Tie-breaker: sort() is stable, but stability only helps if the
        # input order itself is deterministic — Firestore query result
        # order for equal-priority docs is not guaranteed to be. Sorting
        # on identity_key as a secondary key makes selection reproducible
        # across process restarts / repeated sweeps, not just within one
        # sorted() call in one process.
        return (score, str(pattern["identity_key"]))

    return sorted(all_suppressed_patterns, key=priority, reverse=True)[:max_targets]
