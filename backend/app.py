"""Local-only Backboard testing and ElevenLabs hotel-call controls."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from time import perf_counter
from typing import Annotated

import httpx
from backboard import BackboardClient
from dotenv import dotenv_values
from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)

from backend.elevenlabs_routes import (
    ElevenLabsSettings,
    get_elevenlabs_settings,
    router as elevenlabs_router,
)

ENV_PATH = Path(__file__).resolve().parent / ".env"
logger = logging.getLogger(__name__)


class Settings(BaseModel):
    api_key: SecretStr = SecretStr("")
    llm_provider: str | None = None
    model_name: str | None = None
    timeout_seconds: float = Field(default=30, gt=0, le=120)

    @model_validator(mode="after")
    def validate_model_pair(self) -> "Settings":
        if bool(self.llm_provider) != bool(self.model_name):
            raise ValueError("Set both BACKBOARD_LLM_PROVIDER and BACKBOARD_MODEL_NAME.")
        return self


def get_settings() -> Settings:
    """Read only this backend's dotenv file; exported variables take precedence."""
    local = dotenv_values(ENV_PATH)

    def value(name: str, default: str = "") -> str:
        return str(os.environ.get(name, local.get(name) or default)).strip()

    try:
        return Settings(
            api_key=value("BACKBOARD_API_KEY"),
            llm_provider=value("BACKBOARD_LLM_PROVIDER") or None,
            model_name=value("BACKBOARD_MODEL_NAME") or None,
            timeout_seconds=value("BACKBOARD_TIMEOUT_SECONDS", "30"),
        )
    except ValidationError:
        # Pydantic validation errors can contain input values; do not expose them.
        raise HTTPException(
            status_code=503,
            detail={
                "code": "invalid_configuration",
                "message": "Check the provider/model pair and timeout in backend/.env.",
            },
        ) from None


async def get_backboard_client(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncIterator[BackboardClient]:
    key = settings.api_key.get_secret_value().strip()
    if not key:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "missing_api_key",
                "message": "Set BACKBOARD_API_KEY in backend/.env, then retry.",
            },
        )
    async with BackboardClient(api_key=key, timeout=settings.timeout_seconds) as client:
        yield client


class TestRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    prompt: str = Field(
        default="Reply with exactly: Backboard API connection successful.",
        min_length=1,
        max_length=2000,
        description="A short message to verify the API connection.",
    )


class Usage(BaseModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class TestResponse(BaseModel):
    ok: bool = True
    reply: str
    status: str
    elapsed_ms: float
    thread_id: str | None = None
    assistant_id: str | None = None
    model_provider: str | None = None
    model_name: str | None = None
    usage: Usage


def _is_timeout(error: BaseException) -> bool:
    """The SDK wraps HTTPX timeouts as API errors, preserving their context."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (TimeoutError, httpx.TimeoutException)):
            return True
        current = current.__cause__ or current.__context__
    return False


def _upstream_error(error: Exception) -> HTTPException:
    if _is_timeout(error):
        return HTTPException(
            status_code=504,
            detail={"code": "backboard_timeout", "message": "Backboard timed out. Retry later."},
        )

    upstream_status = getattr(error, "status_code", None)
    code = "backboard_error"
    message = "Backboard could not complete the test. Check your account and retry."
    status_code = 502
    if upstream_status in (401, 403):
        code = "backboard_auth_error"
        message = "Backboard rejected the API key. Check the key and selected workspace."
    elif upstream_status == 402:
        code = "backboard_credits_error"
        message = "Check your Backboard balance and hackathon credit redemption."
    elif upstream_status == 429:
        status_code = 429
        code = "backboard_rate_limited"
        message = "Backboard is rate limiting requests. Wait before retrying."

    detail: dict[str, str | int] = {"code": code, "message": message}
    if isinstance(upstream_status, int):
        detail["upstream_status"] = upstream_status
    return HTTPException(status_code=status_code, detail=detail)


def create_app(
    settings: Settings | None = None,
    *, elevenlabs_settings: ElevenLabsSettings | None = None,
) -> FastAPI:
    application = FastAPI(
        title="SprintMasters Agent Backend",
        version="0.2.0",
        description=(
            "Local Backboard connectivity test. GET /health makes no model call. "
            "POST /api/test makes one model request using the server-side key. "
            "ElevenLabs routes control an existing agent; calls/preview never dials. "
            "Real calls require both a server-side enable flag and confirm_call=true."
        ),
    )
    if settings is not None:
        application.dependency_overrides[get_settings] = lambda: settings
    if elevenlabs_settings is not None:
        application.dependency_overrides[get_elevenlabs_settings] = lambda: elevenlabs_settings
    application.include_router(elevenlabs_router)

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(_request, error: RequestValidationError) -> JSONResponse:
        # FastAPI normally echoes invalid input. Keep values out of error responses.
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"loc": item["loc"], "msg": item["msg"], "type": item["type"]}
                    for item in error.errors()
                ]
            },
        )

    @application.get("/health", tags=["Health"])
    async def health(config: Annotated[Settings, Depends(get_settings)]) -> dict:
        return {
            "ok": True,
            "api_key_configured": bool(config.api_key.get_secret_value().strip()),
            "model_provider": config.llm_provider,
            "model_name": config.model_name,
        }

    @application.post("/api/test", response_model=TestResponse, tags=["Backboard"])
    async def test_api(
        request: TestRequest,
        config: Annotated[Settings, Depends(get_settings)],
        client: Annotated[BackboardClient, Depends(get_backboard_client)],
    ) -> TestResponse:
        started = perf_counter()
        options: dict[str, object] = {"stream": False, "memory": "off", "web_search": "off"}
        if config.llm_provider and config.model_name:
            options.update(llm_provider=config.llm_provider, model_name=config.model_name)

        try:
            response = await asyncio.wait_for(
                client.send_message(request.prompt, **options),
                timeout=config.timeout_seconds,
            )
        except Exception as error:
            # Never log exception text/objects: upstream errors can include credentials.
            logger.warning("Backboard test failed (%s)", type(error).__name__)
            raise _upstream_error(error) from None

        try:
            # SDK 1.5.19 stores usage and provider fields in the latest message.
            latest = response.messages[-1]
            status = str(latest.get("status") or "").upper()
            reply = latest.get("content")
            if status != "COMPLETED" or not isinstance(reply, str) or not reply.strip():
                raise ValueError("The test did not return a completed text response.")
            return TestResponse(
                reply=reply,
                status=status,
                elapsed_ms=round((perf_counter() - started) * 1000, 2),
                thread_id=latest.get("thread_id"),
                assistant_id=latest.get("assistant_id"),
                model_provider=latest.get("model_provider") or config.llm_provider,
                model_name=latest.get("model_name") or config.model_name,
                usage=Usage(
                    input_tokens=latest.get("input_tokens"),
                    output_tokens=latest.get("output_tokens"),
                    total_tokens=latest.get("total_tokens"),
                    cost_usd=latest.get("cost_usd"),
                ),
            )
        except (AttributeError, IndexError, TypeError, ValueError):
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "invalid_backboard_response",
                    "message": "Backboard did not return a completed text response.",
                },
            ) from None

    return application


app = create_app()
