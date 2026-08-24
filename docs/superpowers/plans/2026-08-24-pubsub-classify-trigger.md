# Pub/Sub Trigger for /classify Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `POST /classify` accept a Pub/Sub push envelope (base64-encoded alert JSON inside `message.data`) in addition to the raw alert JSON body it already accepts, so a Pub/Sub push subscription can call it directly.

**Architecture:** `main.py`'s `/classify` handler moves from a typed `ClassifierRequest` FastAPI body param back to a raw `Request`, detects whether the body is a Pub/Sub envelope or a plain alert dict, unwraps accordingly, then validates the resulting dict against the same `ClassifierRequest` model as before — so malformed alert data still 422s identically regardless of which shape it arrived in.

**Tech Stack:** Python 3.13, FastAPI, pydantic, existing `google-cloud-tasks`/`loguru` stack (untouched by this plan).

**Spec:** `docs/superpowers/specs/2026-08-24-pubsub-classify-trigger-design.md`

## Global Constraints

- One endpoint (`/classify`) handles both the Pub/Sub envelope shape and the raw-body shape — no separate `/pubsub/classify` route.
- Malformed input (bad JSON, malformed envelope, missing `ClassifierRequest` fields) always returns 422, same as today — this plan must not weaken that.
- Direct/test callers posting raw alert JSON must see byte-for-byte identical behavior to before this plan — every existing `/classify` test in `tests/test_main.py` must keep passing unmodified.
- No changes to `classify_alert()`, `ClassifierRequest`'s fields, or anything downstream of body-parsing — this is a delivery-mechanism change only.

---

## Task 1: `PubSubPushEnvelope`/`PubSubMessage` schemas

**Files:**
- Modify: `vor_agents/schemas.py`
- Test: `tests/test_schemas.py` (new file)

**Interfaces:**
- Produces: `PubSubMessage(BaseModel)` with `data: str`; `PubSubPushEnvelope(BaseModel)` with `message: PubSubMessage`. Consumed by Task 2's `_decode_classify_body()`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_schemas.py`:

```python
"""Tests for vor_agents.schemas -- pure Pydantic model validation, no
Firestore/agent dependencies."""

import base64
import json

import pytest
from pydantic import ValidationError

from vor_agents.schemas import PubSubPushEnvelope


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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_schemas.py -v`
Expected: `ImportError: cannot import name 'PubSubPushEnvelope' from 'vor_agents.schemas'`

- [ ] **Step 3: Add the schemas**

Add to `vor_agents/schemas.py`, after the `AuditRequest` class:

```python
class PubSubMessage(BaseModel):
    """The `message` object inside a Pub/Sub push request body. `data` is
    base64-encoded -- Pub/Sub always encodes the published message body
    this way, regardless of what the publisher originally sent. Other
    fields Pub/Sub includes (messageId, publishTime, attributes) aren't
    read by anything here, so they're not modeled -- extra="allow" isn't
    even needed since pydantic ignores unrecognized fields by default."""

    data: str


class PubSubPushEnvelope(BaseModel):
    """Body shape Pub/Sub actually POSTs to a push endpoint:
    {"message": {"data": "<base64>", ...}, "subscription": "..."}. Used
    only to DETECT this shape in /classify -- see main.py's
    _decode_classify_body(). The alert JSON itself lives base64-encoded
    inside message.data, decoded and re-validated against
    ClassifierRequest separately, not by this model."""

    message: PubSubMessage
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_schemas.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Run the full suite and lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy vor_agents/ main.py`
Expected: all pass, no regressions.

- [ ] **Step 6: Commit**

```bash
git add vor_agents/schemas.py tests/test_schemas.py
git commit -m "Add PubSubPushEnvelope/PubSubMessage schemas"
```

---

## Task 2: `/classify` accepts a Pub/Sub push envelope

**Files:**
- Modify: `main.py`
- Modify: `tests/test_main.py`

**Interfaces:**
- Consumes: `PubSubPushEnvelope`, `PubSubMessage` (Task 1).
- Produces: `_decode_classify_body(raw_body: bytes) -> dict[str, Any]` (module-private helper in `main.py`) -- returns the alert dict regardless of which shape the body was, or raises `ValueError` on anything malformed.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_main.py` (alongside the existing `/classify` tests -- keep every existing test in the file as-is):

```python
import base64
import json as _json  # existing file may already import json under a
                       # different alias -- use whichever the file already
                       # has if `json` is already imported at module level


def _pubsub_envelope(alert: dict) -> dict:
    encoded = base64.b64encode(_json.dumps(alert).encode()).decode()
    return {"message": {"data": encoded, "messageId": "1"}, "subscription": "projects/p/subscriptions/s"}


def test_classify_accepts_pubsub_envelope(fake_firestore, monkeypatch):
    identity_key = ("rule", "w3wp.exe", "csc.exe", "family")
    alert = {
        "detection_rule_id": "rule",
        "parent_image": "w3wp.exe",
        "child_image": "csc.exe",
        "endpoint_family": "family",
    }

    with patch("main.get_firestore_client", return_value=fake_firestore), \
         patch("main.classify_alert", new=AsyncMock(return_value=(_suppress_result(), identity_key))):
        client = TestClient(main.app)
        resp = client.post("/classify", content=_json.dumps(_pubsub_envelope(alert)))

    assert resp.status_code == 200
    assert resp.json()["decision"] == "SUPPRESS"


def test_classify_rejects_malformed_pubsub_envelope(fake_firestore):
    body = {"message": {"data": "not-valid-base64!!!"}, "subscription": "s"}

    with patch("main.get_firestore_client", return_value=fake_firestore):
        client = TestClient(main.app)
        resp = client.post("/classify", content=_json.dumps(body))

    assert resp.status_code == 422


def test_classify_rejects_envelope_whose_decoded_data_is_not_json(fake_firestore):
    encoded = base64.b64encode(b"not json").decode()
    body = {"message": {"data": encoded}, "subscription": "s"}

    with patch("main.get_firestore_client", return_value=fake_firestore):
        client = TestClient(main.app)
        resp = client.post("/classify", content=_json.dumps(body))

    assert resp.status_code == 422


def test_classify_still_accepts_raw_alert_body(fake_firestore):
    """Direct/test callers posting raw alert JSON (no Pub/Sub envelope)
    keep working exactly as before this change."""
    identity_key = ("rule", "w3wp.exe", "csc.exe", "family")
    alert = {
        "detection_rule_id": "rule",
        "parent_image": "w3wp.exe",
        "child_image": "csc.exe",
        "endpoint_family": "family",
    }

    with patch("main.get_firestore_client", return_value=fake_firestore), \
         patch("main.classify_alert", new=AsyncMock(return_value=(_suppress_result(), identity_key))):
        client = TestClient(main.app)
        resp = client.post("/classify", json=alert)

    assert resp.status_code == 200
    assert resp.json()["decision"] == "SUPPRESS"
```

`_suppress_result()`, `AsyncMock`, `patch`, `TestClient`, `main` are already imported/defined earlier in `tests/test_main.py` -- reuse them, don't redefine.

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_main.py -k pubsub -v`
Expected: `test_classify_accepts_pubsub_envelope` and the malformed-envelope tests currently return 422 (today's `ClassifierRequest`-typed body param rejects the envelope shape as missing required fields) or otherwise don't match expectations -- confirms current behavior doesn't yet support the envelope.

- [ ] **Step 3: Rewrite `/classify`'s handler in `main.py`**

Add to the imports at the top of `main.py`:

```python
import base64
import binascii
import json

from pydantic import ValidationError as PydanticValidationError
```

Add `PubSubPushEnvelope` to the existing `from vor_agents.schemas import ...` line:

```python
from vor_agents.schemas import AuditRequest, ClassifierRequest, PubSubPushEnvelope
```

Add this helper function above the `/classify` route:

```python
def _decode_classify_body(raw_body: bytes) -> dict[str, Any]:
    """
    Detects whether /classify's raw request body is a Pub/Sub push
    envelope ({"message": {"data": base64}, ...}) or a raw alert JSON
    body (direct/test callers). Returns the alert dict either way -- NOT
    yet validated against ClassifierRequest, the /classify handler does
    that next with the same model either path took before this change.

    Raises ValueError on invalid JSON, a non-object body, or a malformed
    envelope (invalid base64, or base64 content that isn't a JSON object
    once decoded) -- /classify's handler turns this into a 422, same as
    every other malformed-input case in this codebase.
    """
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Request body is not valid JSON: {exc}") from exc

    if isinstance(body, dict) and isinstance(body.get("message"), dict) and "data" in body["message"]:
        PubSubPushEnvelope.model_validate(body)  # raises PydanticValidationError -> caught by caller
        try:
            decoded = base64.b64decode(body["message"]["data"], validate=True)
            alert = json.loads(decoded)
        except (binascii.Error, json.JSONDecodeError) as exc:
            raise ValueError(f"Malformed Pub/Sub push envelope: {exc}") from exc
        if not isinstance(alert, dict):
            raise ValueError("Decoded Pub/Sub message.data is not a JSON object")
        return alert

    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")
    return body
```

Replace the existing `/classify` route:

```python
@app.post("/classify")
async def classify(request: Request) -> dict[str, Any]:
    """
    Accepts either a raw alert JSON body (direct/test callers) or a
    Pub/Sub push envelope (a push subscription calling this endpoint --
    see docs/superpowers/specs/2026-08-24-pubsub-classify-trigger-design.md).
    Either shape is unwrapped to a plain alert dict by
    _decode_classify_body(), then validated against ClassifierRequest --
    a missing identity field or malformed body returns 422 either way,
    same guarantee the previous typed-body-param version gave, just with
    the validation call made explicitly instead of by FastAPI's own
    body-parsing layer.
    """
    raw_body = await request.body()
    try:
        alert_body = _decode_classify_body(raw_body)
        payload = ClassifierRequest.model_validate(alert_body)
    except (ValueError, PydanticValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    alert = payload.model_dump()
    client = get_firestore_client()
    result, identity_key = await classify_alert(alert, client)

    if result.decision == "SUPPRESS":
        _enqueue(identity_key, {"triggered_by": "classify_suppress"})

    return result.model_dump()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_main.py -v`
Expected: all tests PASS, including every pre-existing `/classify` test (missing-field 422, invalid-JSON 422, extra-fields-allowed, enqueue-on-suppress, dedup, enqueue-failure-doesn't-fail-response) plus the 4 new Pub/Sub tests.

- [ ] **Step 5: Run the full suite and lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy vor_agents/ main.py`
Expected: all pass, no regressions.

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "Accept a Pub/Sub push envelope on /classify"
```

---

## Task 3: `docs/DEPLOY.md` -- Pub/Sub topic and subscription

**Files:**
- Modify: `docs/DEPLOY.md`

**Interfaces:**
- None (documentation only).

- [ ] **Step 1: Insert a new section before step 4 (`/classify`'s trigger source), replacing that section's "still open" framing**

Replace the existing step 4 section (`## 4. The /classify endpoint itself`) with:

```markdown
## 4. Wire /classify to a Pub/Sub push subscription

```bash
gcloud pubsub topics create vor-alerts

gcloud pubsub subscriptions create vor-alerts-sub \
  --topic vor-alerts \
  --push-endpoint "https://YOUR_CLOUD_RUN_URL/classify" \
  --push-auth-service-account "vor-scheduler@YOUR_PROJECT_ID.iam.gserviceaccount.com"
```

Reuses the `vor-scheduler` service account created in step 2 -- it already
has `roles/run.invoker` on this service, same reuse pattern as the Cloud
Tasks `/audit` callback (step 3a).

Whatever the ingest source is (Hayabusa output, a Sigma rule webhook,
etc. -- not built as part of this repo) needs `roles/pubsub.publisher` on
the `vor-alerts` topic:

```bash
gcloud pubsub topics add-iam-policy-binding vor-alerts \
  --member "serviceAccount:YOUR_INGEST_SOURCE_SERVICE_ACCOUNT" \
  --role "roles/pubsub.publisher"
```

`/classify` accepts both a Pub/Sub push envelope and a raw alert JSON
body -- see `main.py`'s `_decode_classify_body()`. No separate endpoint
for direct/manual calls.

**Still open:** no dead-letter topic or `--max-delivery-attempts`
configured on `vor-alerts-sub` yet -- a permanently-malformed message
will retry and 422 until it ages out of the subscription's retention
window. Revisit once real traffic volume exists to calibrate against.
```

- [ ] **Step 2: Verify the doc renders sensibly**

Read the file back and confirm the new section sits in step 4's place, with consistent heading levels and no broken code fences, and that it no longer says "isn't wired to a trigger source yet."

- [ ] **Step 3: Commit**

```bash
git add docs/DEPLOY.md
git commit -m "Document Pub/Sub topic and push subscription for /classify"
```

---

## Final verification

- [ ] Run `.venv/bin/python -m pytest -v` -- full suite passes.
- [ ] Run `.venv/bin/python -m ruff check . && .venv/bin/python -m black --check . && .venv/bin/python -m mypy vor_agents/ main.py && .venv/bin/python -m bandit -r vor_agents/ main.py` -- all clean.
- [ ] Confirm `git log --oneline -3` shows one commit per task.
- [ ] Update `docs/TODO-Aug24.md` Task 1 checkbox to done, referencing the commits.
