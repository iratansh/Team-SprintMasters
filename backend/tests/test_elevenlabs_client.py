"""Offline transport contracts; never contact ElevenLabs or place a call."""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from backend.elevenlabs_client import ElevenLabsClient, ElevenLabsError


FAKE_SECRET = "offline-elevenlabs-key-never-send-this"
Method = Callable[[ElevenLabsClient], Any]


def run_request(
    handler: Callable[[httpx.Request], httpx.Response],
    operation: Method,
    *,
    follow_redirects: bool = False,
) -> dict:
    async def run() -> dict:
        async with httpx.AsyncClient(
            base_url="https://api.elevenlabs.io",
            headers={"xi-api-key": FAKE_SECRET},
            transport=httpx.MockTransport(handler),
            follow_redirects=follow_redirects,
        ) as http_client:
            return await operation(ElevenLabsClient(http_client))

    return asyncio.run(run())


@pytest.mark.parametrize(
    "operation, method, path, payload, response_body",
    [
        (
            lambda client: client.get_agent("agent_123-ABC"),
            "GET",
            "/v1/convai/agents/agent_123-ABC",
            None,
            {"agent_id": "agent_123-ABC"},
        ),
        (
            lambda client: client.start_call(
                {
                    "agent_id": "agent_123",
                    "agent_phone_number_id": "phone_123",
                    "to_number": "+14165550100",
                    "conversation_initiation_client_data": {
                        "dynamic_variables": {"hotel_name": "Offline Hotel"}
                    },
                }
            ),
            "POST",
            "/v1/convai/twilio/outbound-call",
            {
                "agent_id": "agent_123",
                "agent_phone_number_id": "phone_123",
                "to_number": "+14165550100",
                "conversation_initiation_client_data": {
                    "dynamic_variables": {"hotel_name": "Offline Hotel"}
                },
            },
            {"success": True, "conversation_id": "conv_123", "callSid": "CA123"},
        ),
        (
            lambda client: client.get_conversation("conv_123-ABC"),
            "GET",
            "/v1/convai/conversations/conv_123-ABC",
            None,
            {"conversation_id": "conv_123-ABC", "status": "done", "transcript": []},
        ),
    ],
)
def test_success_uses_exact_http_contract_once(
    operation: Method, method: str, path: str, payload: dict | None, response_body: dict
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == method
        assert request.url == f"https://api.elevenlabs.io{path}"
        assert request.headers["xi-api-key"] == FAKE_SECRET
        assert request.url.query == b""
        assert (json.loads(request.content) if request.content else None) == payload
        return httpx.Response(200, json=response_body)

    assert run_request(handler, operation) == response_body
    assert len(requests) == 1


@pytest.mark.parametrize(
    "upstream_status, status_code, code",
    [
        (301, 502, "elevenlabs_error"),
        (400, 502, "elevenlabs_error"),
        (401, 502, "elevenlabs_auth_error"),
        (403, 502, "elevenlabs_auth_error"),
        (404, 404, "elevenlabs_not_found"),
        (422, 502, "elevenlabs_invalid_request"),
        (429, 429, "elevenlabs_rate_limited"),
        (500, 502, "elevenlabs_error"),
        (503, 502, "elevenlabs_error"),
    ],
)
@pytest.mark.parametrize("starts_call", [False, True])
def test_http_errors_are_sanitized_and_never_retried(
    upstream_status: int,
    status_code: int,
    code: str,
    starts_call: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            upstream_status,
            json={"detail": FAKE_SECRET},
            headers={"x-sensitive-upstream-detail": FAKE_SECRET},
        )

    with pytest.raises(ElevenLabsError) as caught:
        run_request(
            handler,
            (lambda client: client.start_call({"agent_id": "agent_123"}))
            if starts_call
            else (lambda client: client.get_agent("agent_123")),
        )

    error = caught.value
    assert error.status_code == status_code
    assert error.code == code
    assert error.upstream_status == upstream_status
    assert error.outcome_unknown is (starts_call and 500 <= upstream_status < 600)
    assert ("may already be placed" in error.message) is error.outcome_unknown
    assert ("dashboard before retrying" in error.message) is error.outcome_unknown
    assert len(requests) == 1
    assert FAKE_SECRET not in str(error)
    assert FAKE_SECRET not in repr(error.__dict__)
    assert FAKE_SECRET not in caplog.text
    assert error.__context__ is None


@pytest.mark.parametrize(
    "exception_type, status_code, code",
    [
        (httpx.ReadTimeout, 504, "elevenlabs_timeout"),
        (httpx.ConnectTimeout, 504, "elevenlabs_timeout"),
        (httpx.ConnectError, 502, "elevenlabs_connection_error"),
        (httpx.ReadError, 502, "elevenlabs_connection_error"),
    ],
)
@pytest.mark.parametrize("starts_call", [False, True])
def test_transport_errors_do_not_retain_exception_or_retry(
    exception_type: type[httpx.RequestError],
    status_code: int,
    code: str,
    starts_call: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise exception_type(FAKE_SECRET, request=request)

    with pytest.raises(ElevenLabsError) as caught:
        run_request(
            handler,
            (lambda client: client.start_call({"agent_id": "agent_123"}))
            if starts_call
            else (lambda client: client.get_conversation("conv_123")),
        )

    error = caught.value
    assert error.status_code == status_code
    assert error.code == code
    assert error.upstream_status is None
    assert error.outcome_unknown is starts_call
    assert ("may already be placed" in error.message) is starts_call
    assert len(requests) == 1
    assert FAKE_SECRET not in str(error)
    assert FAKE_SECRET not in repr(error.__dict__)
    assert FAKE_SECRET not in caplog.text
    assert error.__context__ is None
    assert error.__cause__ is None


@pytest.mark.parametrize("body", [b"not JSON", b"[]", b"null", b'"string"', b"1", b""])
@pytest.mark.parametrize("starts_call", [False, True])
def test_malformed_or_non_object_success_is_sanitized_and_not_retried(
    body: bytes, starts_call: bool
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=body)

    with pytest.raises(ElevenLabsError) as caught:
        run_request(
            handler,
            (lambda client: client.start_call({}))
            if starts_call
            else (lambda client: client.get_agent("agent_123")),
        )

    error = caught.value
    assert error.status_code == 502
    assert error.code == "invalid_elevenlabs_response"
    assert error.upstream_status == 200
    assert error.outcome_unknown is starts_call
    assert ("may already be placed" in error.message) is starts_call
    assert len(requests) == 1
    assert error.__context__ is None


def test_client_preserves_success_false_for_route_validation() -> None:
    result = run_request(
        lambda request: httpx.Response(200, json={"success": False}),
        lambda client: client.start_call({}),
    )
    assert result == {"success": False}


@pytest.mark.parametrize("upstream_status", [301, 302, 307, 308])
def test_redirect_is_not_followed_even_if_client_default_allows_it(upstream_status: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            upstream_status, headers={"Location": "https://untrusted.invalid/capture"}
        )

    with pytest.raises(ElevenLabsError) as caught:
        run_request(
            handler,
            lambda client: client.start_call({}),
            follow_redirects=True,
        )
    assert len(requests) == 1
    assert requests[0].url.host == "api.elevenlabs.io"
    assert caught.value.upstream_status == upstream_status


@pytest.mark.parametrize("bad_id", ["", "a" * 129, "../agents", "a/b", "a?key=x", "a#fragment", "a b", "酒店", "agent\n", None])
@pytest.mark.parametrize("method_name", ["get_agent", "get_conversation"])
def test_invalid_ids_are_rejected_before_http(bad_id: object, method_name: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Invalid IDs must not make an HTTP request")

    with pytest.raises(ValueError, match="ElevenLabs IDs"):
        run_request(handler, lambda client: getattr(client, method_name)(bad_id))


def test_longest_allowed_id_is_accepted() -> None:
    value = "a" * 128

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(value)
        return httpx.Response(200, json={})

    assert run_request(handler, lambda client: client.get_agent(value)) == {}
