"""
Vör — Evidence diversity scoring. Pure computation over confirmed_instances,
no LLM, no external table — this measures how varied the evidence for a
pattern actually is, not just how much of it exists.

20 confirmations from the same host, same user, same hour is weak evidence
dressed up as strong — exactly the failure mode the auditor prompt was
written to watch for (see AUDITOR_SYSTEM_PROMPT in auditor_agent.py). This
gives that intuition a number instead of leaving it to the auditor's
judgment alone.
"""

from datetime import datetime


def evidence_diversity_score(confirmed_instances: list[dict]) -> float:
    """
    Returns a value in [0.0, 1.0]. Computes the distinct-value ratio across
    a few contextual dimensions (host, user, hour-of-day) and averages
    them — 1.0 means every instance came from a meaningfully different
    context, near 0 means they're almost all the same situation logged
    repeatedly.

    Only dimensions actually present on the instances are used, so this
    degrades gracefully if an alert schema doesn't carry host/user/
    timestamp fields rather than crashing or silently scoring 0.
    """
    if not confirmed_instances:
        return 0.0

    n = len(confirmed_instances)
    ratios = []

    for dim in ("host", "user"):
        values = {inst[dim] for inst in confirmed_instances if dim in inst}
        if values:
            ratios.append(min(len(values) / n, 1.0))

    # Parsed via datetime.fromisoformat, not a raw string slice — slicing
    # [11:13] on any string >= 13 chars pulled whatever characters
    # happened to land there regardless of format, so a malformed
    # timestamp like "2026-08-01T99:00:00Z" counted "99" as a real,
    # distinct hour, artificially inflating diversity on bad ingestion
    # data. fromisoformat rejects that outright. No Z-suffix normalization
    # needed — this project requires Python >= 3.13 (pyproject.toml), and
    # fromisoformat has accepted the "Z" (Zulu/UTC) suffix natively since
    # 3.11's relaxed parsing.
    hours = set()
    for inst in confirmed_instances:
        ts = inst.get("timestamp")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue
        hours.add(f"{dt.hour:02d}")
    if hours:
        ratios.append(min(len(hours) / n, 1.0))

    return sum(ratios) / len(ratios) if ratios else 0.0
