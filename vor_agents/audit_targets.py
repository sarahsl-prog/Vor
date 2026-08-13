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
    """
    def priority(pattern: dict) -> float:
        return (
            pattern["days_since_last_review"] * 1.0
            + (1.0 - pattern["evidence_diversity_score"]) * 2.0
            + pattern["blast_radius_estimate"] * 3.0
        )

    return sorted(all_suppressed_patterns, key=priority, reverse=True)[:max_targets]
