"""Offline ElevenLabs route contracts: no credentials, network or real calls."""

from collections.abc import AsyncIterator
from copy import deepcopy

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.elevenlabs_routes as routes
from backend.app import Settings, create_app
from backend.elevenlabs_client import ElevenLabsError
from backend.elevenlabs_routes import ElevenLabsSettings, get_elevenlabs_client


FAKE_SECRET = "offline-elevenlabs-key-never-send"
AGENT_ID = "agent_offline_123"
PHONE_ID = "phnum_offline_123"
CONVERSATION_ID = "conv_offline_123"
BASE = "/api/elevenlabs"


def hotel_request(**overrides: object) -> dict:
    result = {
        "hotel_name": "Offline Test Hotel",
        "address": "123 Example Street, Ottawa, Ontario",
        "phone_number": "+16135550123",
        "check_in": "2026-11-01",
        "check_out": "2026-11-03",
        "guests": 2,
    }
    result.update(overrides)
    return result


def configured_settings(**overrides: object) -> ElevenLabsSettings:
    values = {
        "api_key": FAKE_SECRET,
        "agent_id": AGENT_ID,
        "agent_phone_number_id": PHONE_ID,
        "allow_outbound_calls": True,
    }
    values.update(overrides)
    return ElevenLabsSettings(**values)


def conversation_response(**overrides: object) -> dict:
    result = {
        "agent_id": AGENT_ID,
        "conversation_id": CONVERSATION_ID,
        "status": "done",
        "transcript": [
            {"role": "agent", "message": "  Bonjour !\nTwo nights, please.  ", "time_in_call_secs": 0},
            {"role": "user", "message": "Oui — $95 plus tax.\nNo booking made.", "time_in_call_secs": 3.25},
            {"role": "agent", "message": None, "time_in_call_secs": 4},
            {"role": "agent", "message": "Thank you; goodbye.", "time_in_call_secs": 5.5},
        ],
        "metadata": {"call_duration_secs": 6},
        "analysis": {
            "call_successful": "success",
            "transcript_summary": "A separate summary, not the transcript.",
            "data_collection_results": {"confirmed_price": {"value": "95"}},
        },
    }
    result.update(overrides)
    return result


class FakeElevenLabsClient:
    def __init__(self) -> None:
        self.agent_response: object = {
            "agent_id": AGENT_ID,
            "name": "Offline Hotel Agent",
            "conversation_config": {
                "agent": {
                    "language": "en",
                    "first_message": "Hello {{hotel_name}}.",
                    "prompt": {"prompt": "Confirm {{ check_in }} and {{check_out}} at {{hotel_name}}."},
                }
            },
        }
        self.call_response: object = {
            "success": True, "conversation_id": CONVERSATION_ID, "callSid": "CAoffline123",
        }
        self.conversation_response: object = conversation_response()
        self.error: ElevenLabsError | None = None
        self.calls: list[tuple[str, object]] = []

    def result(self, value: object) -> object:
        if self.error is not None:
            raise self.error
        return deepcopy(value)

    async def get_agent(self, agent_id: str) -> object:
        self.calls.append(("get_agent", agent_id))
        return self.result(self.agent_response)

    async def start_call(self, payload: dict) -> object:
        self.calls.append(("start_call", deepcopy(payload)))
        return self.result(self.call_response)

    async def get_conversation(self, conversation_id: str) -> object:
        self.calls.append(("get_conversation", conversation_id))
        return self.result(self.conversation_response)


def offline_client(
    fake: FakeElevenLabsClient,
    settings: ElevenLabsSettings | None = None,
) -> TestClient:
    application = create_app(
        Settings(), elevenlabs_settings=settings or configured_settings(),
    )

    async def fake_dependency() -> AsyncIterator[FakeElevenLabsClient]:
        yield fake

    application.dependency_overrides[get_elevenlabs_client] = fake_dependency
    return TestClient(application)


@pytest.fixture(autouse=True)
def prohibit_external_async_http(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden_send(*args: object, **kwargs: object) -> None:
        raise AssertionError("Route tests must never make external HTTP requests")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden_send)


@pytest.mark.parametrize("configured", [False, True])
def test_local_status_and_preview_never_construct_client(
    monkeypatch: pytest.MonkeyPatch, configured: bool,
) -> None:
    def forbidden_client(*args: object, **kwargs: object) -> None:
        raise AssertionError("Local status and preview must never construct a client")

    monkeypatch.setattr(routes, "ElevenLabsClient", forbidden_client)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden_client)
    settings = configured_settings() if configured else ElevenLabsSettings()
    with TestClient(create_app(Settings(), elevenlabs_settings=settings)) as client:
        status = client.get(f"{BASE}/status")
        preview = client.post(f"{BASE}/calls/preview", json=hotel_request(confirm_call=True))

    assert status.status_code == preview.status_code == 200
    assert status.json() == {
        "ok": True,
        "api_key_configured": configured,
        "agent_id_configured": configured,
        "phone_number_id_configured": configured,
        "outbound_calls_enabled": configured,
    }
    body = preview.json()
    assert body["dry_run"] is True
    assert body["ready_to_call"] is configured
    assert body["missing_configuration"] == ([] if configured else [
        "ELEVENLABS_API_KEY", "ELEVENLABS_AGENT_ID", "ELEVENLABS_AGENT_PHONE_NUMBER_ID",
    ])
    assert FAKE_SECRET not in status.text + preview.text


@pytest.mark.parametrize("invalid", [
    {"check_out": "2026-11-01"},
    {"check_out": "2026-10-31"},
    {"phone_number": "6135550123"},
    {"phone_number": "+01234567890"},
    {"phone_number": "+16135550123 ext 1"},
    {"guests": "2"},
    {"guests": 0},
    {"online_price": -1},
    {"online_price": "NaN"},
    {"taxes_included": "false"},
    {"source_url": "https://user:private-url-password@example.com/hotel"},
    {"source_checked_at": "2026-10-17T10:30:00"},
    {"currency": "cad"},
])
def test_preview_rejects_invalid_hotel_fields_without_calling_upstream(invalid: dict) -> None:
    fake = FakeElevenLabsClient()
    with offline_client(fake) as client:
        response = client.post(f"{BASE}/calls/preview", json=hotel_request(**invalid))

    assert response.status_code == 422
    assert fake.calls == []
    assert "private-url-password" not in response.text
    assert all("input" not in item and "ctx" not in item for item in response.json()["detail"])


def test_request_credentials_are_forbidden_and_never_echoed() -> None:
    fake = FakeElevenLabsClient()
    with offline_client(fake) as client:
        response = client.post(f"{BASE}/calls/preview", json=hotel_request(api_key=FAKE_SECRET))

    assert response.status_code == 422
    assert FAKE_SECRET not in response.text
    assert fake.calls == []


@pytest.mark.parametrize("price,taxes,expected_price,expected_taxes", [
    (None, None, "unknown", "unknown"),
    (0, False, "0", False),
    ("190.50", True, "190.50", True),
])
def test_preview_preserves_zero_false_unknown_and_uses_flat_scalar_variables(
    price: object, taxes: object, expected_price: str, expected_taxes: object,
) -> None:
    fake = FakeElevenLabsClient()
    with offline_client(fake) as client:
        response = client.post(f"{BASE}/calls/preview", json=hotel_request(
            online_price=price, taxes_included=taxes,
        ))

    assert response.status_code == 200
    variables = response.json()["payload"]["conversation_initiation_client_data"]["dynamic_variables"]
    assert variables["online_price"] == expected_price
    assert variables["taxes_included"] == expected_taxes
    assert variables["source_url"] == variables["source_checked_at"] == "unknown"
    assert variables["nights"] == 2
    assert all(type(value) in (str, int, float, bool) for value in variables.values())
    assert fake.calls == []


def test_outbound_call_requires_server_enable_flag() -> None:
    fake = FakeElevenLabsClient()
    with offline_client(fake, configured_settings(allow_outbound_calls=False)) as client:
        response = client.post(f"{BASE}/calls", json=hotel_request(confirm_call=True))

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "outbound_calls_disabled"
    assert fake.calls == []


@pytest.mark.parametrize("settings,call_request,code,status", [
    (
        configured_settings(api_key="", allow_outbound_calls=False),
        hotel_request(confirm_call=True), "outbound_calls_disabled", 403,
    ),
    (
        configured_settings(api_key=""),
        hotel_request(), "call_confirmation_required", 409,
    ),
    (
        configured_settings(api_key="", agent_id=""),
        hotel_request(confirm_call=True), "missing_elevenlabs_agent_id", 503,
    ),
    (
        configured_settings(api_key="", agent_phone_number_id=""),
        hotel_request(confirm_call=True), "missing_elevenlabs_phone_number_id", 503,
    ),
])
def test_call_permission_and_resource_guards_run_before_client_construction(
    monkeypatch: pytest.MonkeyPatch, settings: ElevenLabsSettings,
    call_request: dict, code: str, status: int,
) -> None:
    def forbidden_client(*args: object, **kwargs: object) -> None:
        raise AssertionError("A blocked call must never construct a client")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden_client)
    monkeypatch.setattr(routes, "ElevenLabsClient", forbidden_client)
    with TestClient(create_app(Settings(), elevenlabs_settings=settings)) as client:
        response = client.post(f"{BASE}/calls", json=call_request)

    assert response.status_code == status
    assert response.json()["detail"]["code"] == code


@pytest.mark.parametrize("confirmation", [{}, {"confirm_call": False}])
def test_outbound_call_requires_explicit_confirmation(confirmation: dict) -> None:
    fake = FakeElevenLabsClient()
    with offline_client(fake) as client:
        response = client.post(f"{BASE}/calls", json=hotel_request(**confirmation))

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "call_confirmation_required"
    assert fake.calls == []


@pytest.mark.parametrize("confirmation", ["true", "false", 1, 0, None])
def test_confirmation_is_strict_boolean(confirmation: object) -> None:
    fake = FakeElevenLabsClient()
    with offline_client(fake) as client:
        response = client.post(f"{BASE}/calls", json=hotel_request(confirm_call=confirmation))

    assert response.status_code == 422
    assert fake.calls == []


def test_missing_key_fails_locally_without_constructing_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_client(*args: object, **kwargs: object) -> None:
        raise AssertionError("Missing credentials must not construct an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden_client)
    settings = configured_settings(api_key=" ")
    with TestClient(create_app(Settings(), elevenlabs_settings=settings)) as client:
        response = client.post(f"{BASE}/calls", json=hotel_request(confirm_call=True))

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "missing_elevenlabs_api_key"


@pytest.mark.parametrize("missing,code", [
    ("agent_id", "missing_elevenlabs_agent_id"),
    ("agent_phone_number_id", "missing_elevenlabs_phone_number_id"),
])
def test_outbound_call_requires_resource_configuration(missing: str, code: str) -> None:
    fake = FakeElevenLabsClient()
    with offline_client(fake, configured_settings(**{missing: ""})) as client:
        response = client.post(f"{BASE}/calls", json=hotel_request(confirm_call=True))

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == code
    assert fake.calls == []


def test_confirmed_call_sends_exact_context_once_without_agent_prompt_override() -> None:
    fake = FakeElevenLabsClient()
    with offline_client(fake) as client:
        response = client.post(f"{BASE}/calls", json=hotel_request(
            confirm_call=True, online_price="190.50", taxes_included=False,
            source_url="https://example.com/hotel",
            source_checked_at="2026-10-17T14:32:00Z",
        ))

    assert response.status_code == 200
    assert response.json()["status"] == "initiated"
    assert response.json()["conversation_id"] == CONVERSATION_ID
    assert response.json()["call_sid"] == "CAoffline123"
    assert fake.calls == [("start_call", {
        "agent_id": AGENT_ID,
        "agent_phone_number_id": PHONE_ID,
        "to_number": "+16135550123",
        "conversation_initiation_client_data": {"dynamic_variables": {
            "hotel_name": "Offline Test Hotel",
            "hotel_address": "123 Example Street, Ottawa, Ontario",
            "hotel_phone": "+16135550123",
            "check_in": "2026-11-01",
            "check_out": "2026-11-03",
            "nights": 2,
            "guests": 2,
            "online_price": "190.50",
            "currency": "CAD",
            "price_basis": "total_stay",
            "taxes_included": False,
            "source_url": "https://example.com/hotel",
            "source_checked_at": "2026-10-17T14:32:00+00:00",
        }},
        "call_recording_enabled": False,
    })]
    assert "conversation_config_override" not in repr(fake.calls)
    assert FAKE_SECRET not in repr(fake.calls) + response.text


@pytest.mark.parametrize("path,method", [
    ("/agent", "get"),
    ("/calls", "post"),
    (f"/conversations/{CONVERSATION_ID}", "get"),
])
@pytest.mark.parametrize("status,code,upstream,uncertain", [
    (502, "elevenlabs_auth_error", 401, False),
    (429, "elevenlabs_rate_limited", 429, False),
    (504, "elevenlabs_timeout", None, True),
])
def test_sanitized_upstream_errors_preserve_status_and_call_uncertainty(
    path: str, method: str, status: int, code: str, upstream: int | None, uncertain: bool,
) -> None:
    fake = FakeElevenLabsClient()
    fake.error = ElevenLabsError(
        status_code=status, code=code,
        message="Sanitized upstream failure.", upstream_status=upstream,
        outcome_unknown=uncertain,
    )
    with offline_client(fake) as client:
        if method == "post":
            response = client.post(BASE + path, json=hotel_request(confirm_call=True))
        else:
            response = client.get(BASE + path)

    assert response.status_code == status
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["outcome_unknown"] is uncertain
    if upstream is not None:
        assert detail["upstream_status"] == upstream
    assert FAKE_SECRET not in response.text
    assert len(fake.calls) == 1


@pytest.mark.parametrize("upstream,code,uncertain", [
    ({"success": False, "message": FAKE_SECRET}, "elevenlabs_call_rejected", False),
    ({}, "invalid_elevenlabs_response", True),
    ({"success": 1, "conversation_id": CONVERSATION_ID}, "invalid_elevenlabs_response", True),
    ({"success": True}, "invalid_elevenlabs_response", True),
    ({"success": True, "conversation_id": "../../escape"}, "invalid_elevenlabs_response", True),
    ({"success": True, "conversation_id": ""}, "invalid_elevenlabs_response", True),
])
def test_rejected_or_malformed_call_response_is_not_reported_as_success(
    upstream: dict, code: str, uncertain: bool,
) -> None:
    fake = FakeElevenLabsClient()
    fake.call_response = upstream
    with offline_client(fake) as client:
        response = client.post(f"{BASE}/calls", json=hotel_request(confirm_call=True))

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == code
    assert response.json()["detail"]["outcome_unknown"] is uncertain
    assert FAKE_SECRET not in response.text
    assert len(fake.calls) == 1


def test_agent_read_exposes_variable_names_not_full_shared_prompt() -> None:
    fake = FakeElevenLabsClient()
    with offline_client(fake) as client:
        response = client.get(f"{BASE}/agent")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "agent_id": AGENT_ID,
        "name": "Offline Hotel Agent",
        "language": "en",
        "first_message": "Hello {{hotel_name}}.",
        "prompt_variables": ["check_in", "check_out", "hotel_name"],
    }
    assert "Confirm" not in response.text
    assert fake.calls == [("get_agent", AGENT_ID)]


@pytest.mark.parametrize("upstream", [
    {},
    {"agent_id": AGENT_ID, "name": 123},
    {"agent_id": AGENT_ID, "name": "Test"},
    {"agent_id": AGENT_ID, "name": "Test", "conversation_config": []},
    {"agent_id": AGENT_ID, "name": "Test", "conversation_config": {"agent": {"prompt": None}}},
    {
        "agent_id": AGENT_ID, "name": "Test",
        "conversation_config": {"agent": {
            "prompt": {"prompt": "Test"}, "first_message": "Hello", "language": {"key": "en"},
        }},
    },
])
def test_malformed_agent_response_is_sanitized_502(upstream: dict) -> None:
    fake = FakeElevenLabsClient()
    fake.agent_response = upstream
    with offline_client(fake) as client:
        response = client.get(f"{BASE}/agent")

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "invalid_elevenlabs_response"


def test_conversation_cannot_read_another_agents_transcript() -> None:
    fake = FakeElevenLabsClient()
    fake.conversation_response = conversation_response(
        agent_id="agent_other", transcript=[{"message": "private-other-agent-text"}],
    )
    with offline_client(fake) as client:
        response = client.get(f"{BASE}/conversations/{CONVERSATION_ID}")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "elevenlabs_conversation_not_found"
    assert "private-other-agent-text" not in response.text


@pytest.mark.parametrize("status,finished", [
    ("initiated", False), ("in-progress", False), ("processing", False),
    ("done", True), ("failed", True),
])
def test_conversation_preserves_all_original_utterances_separate_from_summary(
    status: str, finished: bool,
) -> None:
    fake = FakeElevenLabsClient()
    upstream = conversation_response(status=status)
    fake.conversation_response = upstream
    with offline_client(fake) as client:
        response = client.get(f"{BASE}/conversations/{CONVERSATION_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == status
    assert body["finished"] is finished
    assert body["transcript"] == upstream["transcript"]
    assert body["transcript_text"] == (
        "[0s] agent:   Bonjour !\nTwo nights, please.  \n"
        "[3.25s] user: Oui — $95 plus tax.\nNo booking made.\n"
        "[5.5s] agent: Thank you; goodbye."
    )
    assert body["transcript_summary"] == upstream["analysis"]["transcript_summary"]
    assert body["transcript_text"] != body["transcript_summary"]
    assert body["data_collection_results"] == upstream["analysis"]["data_collection_results"]
    assert body["call_duration_secs"] == 6
    assert fake.calls == [("get_conversation", CONVERSATION_ID)]


@pytest.mark.parametrize("overrides", [
    {"agent_id": None},
    {"agent_id": []},
    {"agent_id": "not a resource ID"},
    {"conversation_id": "conv_wrong"},
    {"status": "invented-status"},
    {"transcript": None},
    {"transcript": {}},
    {"transcript": ""},
    {"transcript": [{"role": "user", "message": "hello", "time_in_call_secs": -1}]},
    {"transcript": [{"role": "user", "message": "hello", "time_in_call_secs": float("inf")}]},
    {"transcript": [{"role": "user", "message": "hello", "time_in_call_secs": float("nan")}]},
    {"transcript": [{"role": "user", "message": [FAKE_SECRET], "time_in_call_secs": 1}]},
    {"metadata": []},
    {"metadata": {"call_duration_secs": float("inf")}},
    {"analysis": []},
    {"analysis": "malformed"},
    {"analysis": {"transcript_summary": {"secret": FAKE_SECRET}}},
])
def test_malformed_conversation_is_sanitized_502(overrides: dict) -> None:
    fake = FakeElevenLabsClient()
    fake.conversation_response = conversation_response(**overrides)
    with offline_client(fake) as client:
        response = client.get(f"{BASE}/conversations/{CONVERSATION_ID}")

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "invalid_elevenlabs_response"
    assert FAKE_SECRET not in response.text


@pytest.mark.parametrize("invalid_id", ["bad.id", "bad@id", "x" * 129])
def test_invalid_conversation_id_is_rejected_before_upstream_call(invalid_id: str) -> None:
    fake = FakeElevenLabsClient()
    with offline_client(fake) as client:
        response = client.get(f"{BASE}/conversations/{invalid_id}")

    assert response.status_code == 422
    assert fake.calls == []
