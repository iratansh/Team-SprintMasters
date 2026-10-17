"""Offline HTTP contract tests; no real Backboard credentials or requests."""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from backboard import BackboardAPIError
from backboard.models import ChatMessagesResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError

import backend.app as backend_app
from backend.app import Settings, create_app, get_backboard_client


FAKE_SECRET = "offline-test-key-never-send-this"


def sdk_response(**latest_overrides: object) -> ChatMessagesResponse:
    """Use the installed SDK model, including its raw per-message metrics."""
    latest = {
        "status": "COMPLETED",
        "content": "Connection verified.",
        "thread_id": "thread-offline",
        "assistant_id": "assistant-offline",
        "model_provider": "test-provider",
        "model_name": "test-model",
        "input_tokens": 13,
        "output_tokens": 4,
        "total_tokens": 17,
        "cost_usd": 0.00042,
    }
    latest.update(latest_overrides)
    return ChatMessagesResponse(
        messages=[
            {"status": "COMPLETED", "content": "An older reply.", "total_tokens": 999},
            latest,
        ]
    )


class FakeBackboardClient:
    def __init__(
        self,
        response: ChatMessagesResponse | None = None,
        error: Exception | None = None,
        delay_seconds: float = 0,
    ) -> None:
        self.response = response if response is not None else sdk_response()
        self.error = error
        self.delay_seconds = delay_seconds
        self.calls: list[tuple[str, dict]] = []

    async def send_message(self, content: str, **options: object) -> ChatMessagesResponse:
        self.calls.append((content, options))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.error is not None:
            raise self.error
        return self.response


def offline_client(
    fake: FakeBackboardClient, settings: Settings | None = None
) -> TestClient:
    application = create_app(settings or Settings(api_key=FAKE_SECRET))

    async def fake_dependency() -> AsyncIterator[FakeBackboardClient]:
        yield fake

    application.dependency_overrides[get_backboard_client] = fake_dependency
    return TestClient(application)


@pytest.fixture(autouse=True)
def prohibit_real_async_http(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden_send(*args: object, **kwargs: object) -> None:
        raise AssertionError("Tests must not make external HTTP requests")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden_send)


@pytest.mark.parametrize("api_key, configured", [("", False), (FAKE_SECRET, True)])
def test_health_reports_configuration_without_constructing_sdk_client(
    monkeypatch: pytest.MonkeyPatch, api_key: str, configured: bool
) -> None:
    def forbidden_client(*args: object, **kwargs: object) -> None:
        raise AssertionError("Health must not construct a Backboard client")

    monkeypatch.setattr(backend_app, "BackboardClient", forbidden_client)
    with TestClient(create_app(Settings(api_key=api_key))) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["api_key_configured"] is configured
    assert FAKE_SECRET not in response.text


def test_missing_key_returns_actionable_503_without_constructing_sdk_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_client(*args: object, **kwargs: object) -> None:
        raise AssertionError("An absent key must not initialize the SDK")

    monkeypatch.setattr(backend_app, "BackboardClient", forbidden_client)
    with TestClient(create_app(Settings(api_key=" "))) as client:
        response = client.post("/api/test", json={"prompt": "Hello"})

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "missing_api_key"
    assert "BACKBOARD_API_KEY" in response.json()["detail"]["message"]


@pytest.mark.parametrize("configure_model", [False, True])
def test_success_makes_one_low_cost_sdk_call_and_reads_latest_message(
    configure_model: bool,
) -> None:
    model_settings = (
        {"llm_provider": "configured-provider", "model_name": "configured-model"}
        if configure_model
        else {}
    )
    fake = FakeBackboardClient()
    with offline_client(fake, Settings(api_key=FAKE_SECRET, **model_settings)) as client:
        response = client.post("/api/test", json={"prompt": "  Verify connection.  "})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["reply"] == "Connection verified."
    assert body["status"] == "COMPLETED"
    assert body["thread_id"] == "thread-offline"
    assert body["assistant_id"] == "assistant-offline"
    assert body["model_provider"] == "test-provider"
    assert body["model_name"] == "test-model"
    assert body["usage"] == {
        "input_tokens": 13,
        "output_tokens": 4,
        "total_tokens": 17,
        "cost_usd": 0.00042,
    }
    assert isinstance(body["elapsed_ms"], (int, float))
    assert body["elapsed_ms"] >= 0
    assert FAKE_SECRET not in response.text
    assert fake.calls == [
        (
            "Verify connection.",
            {"stream": False, "memory": "off", "web_search": "off", **model_settings},
        )
    ]


def test_empty_body_uses_a_short_nonblank_default_prompt() -> None:
    fake = FakeBackboardClient()
    with offline_client(fake) as client:
        response = client.post("/api/test", json={})

    assert response.status_code == 200
    assert len(fake.calls) == 1
    assert 0 < len(fake.calls[0][0].strip()) < 200


@pytest.mark.parametrize("prompt", ["", " \t\n ", "x" * 2001, None, ["hello"]])
def test_invalid_prompt_is_rejected_before_any_model_call(prompt: object) -> None:
    fake = FakeBackboardClient()
    with offline_client(fake) as client:
        response = client.post("/api/test", json={"prompt": prompt})

    assert response.status_code == 422
    assert fake.calls == []


def test_maximum_prompt_length_is_accepted_after_trimming() -> None:
    fake = FakeBackboardClient()
    with offline_client(fake) as client:
        response = client.post("/api/test", json={"prompt": " " + "x" * 2000 + " "})

    assert response.status_code == 200
    assert fake.calls[0][0] == "x" * 2000


def test_request_cannot_supply_api_key_and_validation_does_not_echo_it() -> None:
    fake = FakeBackboardClient()
    with offline_client(fake) as client:
        response = client.post(
            "/api/test", json={"prompt": "Hello", "api_key": FAKE_SECRET}
        )

    assert response.status_code == 422
    assert FAKE_SECRET not in response.text
    assert fake.calls == []


@pytest.mark.parametrize(
    "upstream_status, expected_status, expected_code, useful_word",
    [
        (401, 502, "backboard_auth_error", "key"),
        (403, 502, "backboard_auth_error", "workspace"),
        (402, 502, "backboard_credits_error", "balance"),
        (429, 429, "backboard_rate_limited", "retry"),
        (500, 502, "backboard_error", "retry"),
    ],
)
def test_upstream_failures_are_actionable_and_redact_secret_in_response_and_logs(
    upstream_status: int,
    expected_status: int,
    expected_code: str,
    useful_word: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeBackboardClient(
        error=BackboardAPIError(
            f"Upstream URL included api_key={FAKE_SECRET}", status_code=upstream_status
        )
    )
    with offline_client(fake) as client:
        response = client.post("/api/test", json={"prompt": "Hello"})

    assert response.status_code == expected_status
    detail = response.json()["detail"]
    assert detail["code"] == expected_code
    assert detail["upstream_status"] == upstream_status
    assert useful_word in detail["message"].lower()
    assert FAKE_SECRET not in response.text
    assert FAKE_SECRET not in caplog.text


def test_unexpected_error_text_is_not_exposed(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeBackboardClient(error=RuntimeError(f"Unexpected detail {FAKE_SECRET}"))
    with offline_client(fake) as client:
        response = client.post("/api/test", json={"prompt": "Hello"})

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "backboard_error"
    assert FAKE_SECRET not in response.text
    assert FAKE_SECRET not in caplog.text


def test_request_deadline_returns_504() -> None:
    fake = FakeBackboardClient(delay_seconds=1)
    with offline_client(fake, Settings(api_key=FAKE_SECRET, timeout_seconds=0.005)) as client:
        response = client.post("/api/test", json={"prompt": "Hello"})

    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "backboard_timeout"
    assert len(fake.calls) == 1


def test_sdk_wrapped_httpx_timeout_returns_504_and_redacts_secret() -> None:
    wrapped = BackboardAPIError(f"SDK error {FAKE_SECRET}")
    wrapped.__cause__ = httpx.ReadTimeout(f"Transport error {FAKE_SECRET}")
    fake = FakeBackboardClient(error=wrapped)
    with offline_client(fake) as client:
        response = client.post("/api/test", json={"prompt": "Hello"})

    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "backboard_timeout"
    assert FAKE_SECRET not in response.text


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"status": "IN_PROGRESS", "content": "Partial response"}],
        [{"status": "FAILED", "content": "Failed response"}],
        [{"status": "COMPLETED", "content": " \n "}],
        [{"status": "COMPLETED", "content": None}],
    ],
)
def test_incomplete_or_empty_sdk_response_is_not_reported_as_success(
    messages: list[dict],
) -> None:
    fake = FakeBackboardClient(response=ChatMessagesResponse(messages=messages))
    with offline_client(fake) as client:
        response = client.post("/api/test", json={"prompt": "Hello"})

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "invalid_backboard_response"


def test_zero_usage_is_preserved_and_absent_model_metadata_uses_configured_model() -> None:
    fake = FakeBackboardClient(
        response=sdk_response(
            model_provider=None,
            model_name=None,
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            cost_usd=0,
        )
    )
    settings = Settings(api_key=FAKE_SECRET, llm_provider="provider", model_name="model")
    with offline_client(fake, settings) as client:
        response = client.post("/api/test", json={"prompt": "Hello"})

    assert response.status_code == 200
    assert response.json()["model_provider"] == "provider"
    assert response.json()["model_name"] == "model"
    assert response.json()["usage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0,
    }


def test_missing_usage_is_reported_as_unknown_instead_of_zero() -> None:
    fake = FakeBackboardClient(
        response=ChatMessagesResponse(
            messages=[{"status": "COMPLETED", "content": "Hello"}]
        )
    )
    with offline_client(fake) as client:
        response = client.post("/api/test", json={"prompt": "Hello"})

    assert response.status_code == 200
    assert response.json()["usage"] == {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
    }


@pytest.mark.parametrize(
    "model_settings", [{"llm_provider": "provider"}, {"model_name": "model"}]
)
def test_provider_and_model_must_be_configured_together(model_settings: dict) -> None:
    with pytest.raises(ValidationError):
        Settings(api_key=FAKE_SECRET, **model_settings)
