"""Tests for vor_agents.schemas -- pure Pydantic model validation, no
Firestore/agent dependencies."""

import base64
import json

import pytest
from pydantic import ValidationError

from vor_agents.schemas import PubSubPushEnvelope, UncertainReason


class TestPubSubPushEnvelope:
    def test_valid_envelope_parses(self):
        alert = {"detection_rule_id": "rule", "parent_image": "w3wp.exe"}
        encoded = base64.b64encode(json.dumps(alert).encode()).decode()
        envelope = PubSubPushEnvelope.model_validate(
            {"message": {"data": encoded, "messageId": "123"}, "subscription": "sub"}
        )
        assert envelope.message.data == encoded

    def test_missing_message_data_rejected(self):
        with pytest.raises(ValidationError):
            PubSubPushEnvelope.model_validate({"message": {"messageId": "123"}})

    def test_missing_message_rejected(self):
        with pytest.raises(ValidationError):
            PubSubPushEnvelope.model_validate({"subscription": "sub"})


class TestUncertainReason:
    def test_audit_failing_value_exists(self):
        assert UncertainReason.AUDIT_FAILING == "audit_failing"
