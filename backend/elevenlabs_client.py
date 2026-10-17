"""Small, single-attempt ElevenLabs HTTP adapter with sanitized errors."""

import re
from typing import Any

import httpx


_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
_CALL_UNCERTAINTY = (
    " The call may already be placed. Check the ElevenLabs dashboard before retrying."
)


class ElevenLabsError(Exception):
    """Safe error details that may be returned by the local HTTP API."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        upstream_status: int | None = None,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.upstream_status = upstream_status
        self.outcome_unknown = outcome_unknown


def _validate_id(value: str) -> str:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ValueError("ElevenLabs IDs must contain 1-128 letters, digits, underscores or hyphens.")
    return value


def _error(
    *,
    status_code: int,
    code: str,
    message: str,
    upstream_status: int | None = None,
    outcome_unknown: bool = False,
) -> ElevenLabsError:
    return ElevenLabsError(
        status_code=status_code,
        code=code,
        message=message + (_CALL_UNCERTAINTY if outcome_unknown else ""),
        upstream_status=upstream_status,
        outcome_unknown=outcome_unknown,
    )


class ElevenLabsClient:
    """Use a caller-managed AsyncClient; never retry a possible paid call."""

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http = http_client

    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/convai/agents/{_validate_id(agent_id)}")

    async def start_call(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request(
            "POST", "/v1/convai/twilio/outbound-call", payload=payload, starts_call=True
        )

    async def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/v1/convai/conversations/{_validate_id(conversation_id)}"
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        starts_call: bool = False,
    ) -> dict[str, Any]:
        failure: ElevenLabsError | None = None
        try:
            response = await self._http.request(
                method, path, json=payload, follow_redirects=False
            )
        except httpx.TimeoutException:
            failure = _error(
                status_code=504,
                code="elevenlabs_timeout",
                message="ElevenLabs did not respond before the request deadline.",
                outcome_unknown=starts_call,
            )
        except httpx.RequestError:
            failure = _error(
                status_code=502,
                code="elevenlabs_connection_error",
                message="Could not complete the connection to ElevenLabs.",
                outcome_unknown=starts_call,
            )
        if failure is not None:
            # Raise outside the except block, without retaining the upstream exception.
            raise failure

        upstream_status = response.status_code
        if not 200 <= upstream_status < 300:
            status_code = 502
            code = "elevenlabs_error"
            message = "ElevenLabs could not complete the request."
            if upstream_status in (401, 403):
                code = "elevenlabs_auth_error"
                message = "ElevenLabs rejected the API key or its permissions."
            elif upstream_status == 404:
                status_code = 404
                code = "elevenlabs_not_found"
                message = "The requested ElevenLabs resource was not found."
            elif upstream_status == 429:
                status_code = 429
                code = "elevenlabs_rate_limited"
                message = "ElevenLabs is rate limiting requests. Wait before retrying."
            elif upstream_status == 422:
                code = "elevenlabs_invalid_request"
                message = "ElevenLabs rejected the request fields. Check the agent and phone settings."
            raise _error(
                status_code=status_code,
                code=code,
                message=message,
                upstream_status=upstream_status,
                outcome_unknown=starts_call and 500 <= upstream_status < 600,
            )

        try:
            result = response.json()
        except ValueError:
            result = None
        if not isinstance(result, dict):
            raise _error(
                status_code=502,
                code="invalid_elevenlabs_response",
                message="ElevenLabs did not return a valid JSON object.",
                upstream_status=upstream_status,
                outcome_unknown=starts_call,
            )
        return result
