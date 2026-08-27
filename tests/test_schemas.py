"""Tests for vor_agents.schemas -- pure Pydantic model validation, no
Firestore/agent dependencies."""

import base64
import json

import pytest
from pydantic import ValidationError

from vor_agents.schemas import (
    ClassifierOutput,
    PubSubPushEnvelope,
    StructuralDeviation,
    UncertainReason,
)


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


class TestStructuralDeviationSchema:
    """Regression for final-review C-1: list[dict[str, Any]] rendered as
    a propertyless OBJECT in the generated JSON Schema -- valid JSON
    Schema, but not a valid *structured-output* schema, and
    indistinguishable from Any from the model's side. A declared
    StructuralDeviation submodel must produce named properties."""

    def test_generated_schema_declares_named_properties_for_each_deviation(self):
        schema = ClassifierOutput.model_json_schema()
        item_schema = schema["properties"]["structural_deviations_found"]["items"]

        # Pydantic represents a nested model as a $ref into $defs rather
        # than inlining it, so resolve the ref to reach the real property
        # declarations. The OLD (broken) shape had no $ref at all -- it
        # was a bare {"type": "object"} with no "properties" anywhere --
        # so this lookup itself fails on the pre-fix schema.
        assert "$ref" in item_schema
        def_name = item_schema["$ref"].removeprefix("#/$defs/")
        deviation_properties = schema["$defs"][def_name]["properties"]

        assert {"field", "template", "observed"}.issubset(deviation_properties.keys())
        assert schema["$defs"][def_name]["required"] == ["field"]

    def test_a_well_formed_deviation_dict_validates(self):
        output = ClassifierOutput(
            decision="ESCALATE",
            structural_deviations_found=[
                {"field": "integrity_level", "template": "Medium", "observed": "High"}
            ],
            reasoning="test",
        )
        assert isinstance(output.structural_deviations_found[0], StructuralDeviation)
        assert output.structural_deviations_found[0].field == "integrity_level"

    def test_a_deviation_missing_its_field_name_is_rejected(self):
        """The check that used to live in orchestrator._deviation_field_names
        (skip-and-log a deviation with no "field" key) is now enforced one
        layer earlier by the type system -- a model response like this
        fails ClassifierOutput validation outright, which classify_alert()
        degrades to UNCERTAIN rather than silently under-reporting."""
        with pytest.raises(ValidationError):
            ClassifierOutput(
                decision="ESCALATE",
                structural_deviations_found=[{"template": "Medium", "observed": "High"}],
                reasoning="test",
            )
