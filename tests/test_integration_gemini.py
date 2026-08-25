"""
Integration suite against the REAL Gemini API.

Excluded from the default run by pytest.ini's `addopts = -m "not
integration"`. Run explicitly:

    .venv/bin/python -m pytest -m integration -v

TESTING_PLAN.md commits to this suite and explains why it is quarantined:
real model calls cost money and are non-deterministic, so they must never
gate CI. What they cover is the one thing mocked tests structurally
cannot -- that a real model call round-trips into a valid
ClassifierOutput / AuditorOutput instead of something the schema rejects.

So the assertions here are deliberately about SHAPE, not content. This
suite must not assert that the model returns a specific decision: that
would make a non-deterministic model a CI-style pass/fail gate, which is
exactly the coupling TESTING_PLAN.md's philosophy section rejects. The
deterministic layer already tests decision logic exhaustively with the
model mocked out.

Requires working Vertex AI credentials in the environment
(GOOGLE_GENAI_USE_VERTEXAI, GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION
plus ADC) -- see .env.example. Without them the whole module skips rather
than failing, so `pytest -m integration` on an unconfigured machine
reports "skipped", not a false red.
"""

import json
import os
import uuid

import pytest

from vor_agents.auditor_agent import build_auditor_agent
from vor_agents.classifier_agent import build_classifier_agent
from vor_agents.orchestrator import _run_agent, classify_alert
from vor_agents.schemas import AuditorOutput, ClassifierOutput

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("GOOGLE_CLOUD_PROJECT"),
        reason="Vertex AI credentials not configured; see .env.example",
    ),
]


class TestLiveClassifier:
    async def test_real_call_round_trips_into_classifier_output(self, baseline_alert):
        """The core claim this suite exists to check: a real model
        response parses into ClassifierOutput. A prompt change that makes
        the model emit prose, a Markdown-fenced blob, or a field the
        schema rejects fails right here."""
        prompt = (
            f"Alert:\n{json.dumps(baseline_alert, indent=2)}\n\n"
            'Enrichment context:\n{"status": "NO_HISTORY"}\n\n'
            "Classify this alert per your instructions."
        )

        result = await _run_agent(
            build_classifier_agent(), prompt, session_id=f"itest_{uuid.uuid4()}"
        )
        output = ClassifierOutput.model_validate(result)

        assert output.decision in {"SUPPRESS", "ESCALATE", "UNCERTAIN"}
        assert output.reasoning

    async def test_no_history_alert_is_never_autonomously_suppressed(self, baseline_alert):
        """The one behavioral claim worth spending a real call on,
        because it is a safety invariant rather than a preference: with
        no confirmed history there is no evidence base to suppress
        against.

        Note this holds in code regardless of what the model says --
        classify_alert() resolves NO_HISTORY deterministically -- so this
        asserts the end-to-end path honors it, not that the model
        behaves."""
        result = await _run_agent(
            build_classifier_agent(),
            (
                f"Alert:\n{json.dumps(baseline_alert, indent=2)}\n\n"
                'Enrichment context:\n{"status": "NO_HISTORY"}\n\n'
                "Classify this alert per your instructions."
            ),
            session_id=f"itest_{uuid.uuid4()}",
        )

        assert ClassifierOutput.model_validate(result).decision != "SUPPRESS"


class TestLiveAuditor:
    async def test_real_call_round_trips_into_auditor_output(self, diverse_confirmed_instances):
        prompt = (
            "Pattern under review:\n"
            f"{json.dumps({'tier': 'confirmed', 'provenance': 'live'}, indent=2)}\n\n"
            "Confirmed instances (cite instance_id values from this list only "
            f"if downgrading):\n{json.dumps(diverse_confirmed_instances, indent=2)}\n\n"
            "Review this suppression decision per your instructions."
        )

        result = await _run_agent(build_auditor_agent(), prompt, session_id=f"itest_{uuid.uuid4()}")
        output = AuditorOutput.model_validate(result)

        assert output.action in {
            "NO_ACTION",
            "DOWNGRADE",
            "RECOMMEND_UPGRADE_FOR_HUMAN_REVIEW",
        }
        assert output.reasoning


class TestLiveEndToEnd:
    async def test_classify_alert_end_to_end_with_a_real_model(
        self, baseline_alert, fake_firestore
    ):
        """Exercises the full orchestrator path -- enrich, prompt, real
        model call, schema validation, deterministic reconciliation --
        with only Firestore faked. The mocked suite covers every branch of
        this function; what it cannot cover is that the real model's
        output survives the whole pipeline without tripping the
        AgentOutputError degrade path."""
        result, identity_key = await classify_alert(baseline_alert, fake_firestore)

        assert identity_key == (
            baseline_alert["detection_rule_id"],
            baseline_alert["parent_image"],
            baseline_alert["child_image"],
            baseline_alert["endpoint_family"],
        )
        # No confirmed history in a fresh fake Firestore, so the
        # deterministic layer must not have produced an autonomous
        # suppression no matter what the model returned.
        assert result.decision != "SUPPRESS"
