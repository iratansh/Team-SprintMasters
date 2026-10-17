"""Local API bridge from hotel-search data to an existing ElevenLabs agent."""

import os
import re
from collections.abc import AsyncIterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import httpx
from dotenv import dotenv_values
from fastapi import APIRouter, Depends, HTTPException, Path as PathParam
from pydantic import (
    BaseModel, ConfigDict, Field, HttpUrl, SecretStr, StrictBool,
    ValidationError, field_validator, model_validator,
)

from backend.elevenlabs_client import ElevenLabsClient, ElevenLabsError

ENV_PATH = Path(__file__).resolve().parent / ".env"
ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"
router = APIRouter(prefix="/api/elevenlabs", tags=["ElevenLabs"])


class ElevenLabsSettings(BaseModel):
    api_key: SecretStr = SecretStr("")
    agent_id: str = Field(default="", max_length=128)
    agent_phone_number_id: str = Field(default="", max_length=128)
    timeout_seconds: float = Field(default=30, gt=0, le=120)
    allow_outbound_calls: bool = False

    @field_validator("agent_id", "agent_phone_number_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(ID_PATTERN, value):
            raise ValueError("Use the ElevenLabs resource ID, not a URL or phone number.")
        return value


def get_elevenlabs_settings() -> ElevenLabsSettings:
    local = dotenv_values(ENV_PATH)

    def value(name: str, default: str = "") -> str:
        return str(os.environ.get(name, local.get(name) or default)).strip()

    try:
        return ElevenLabsSettings(
            api_key=value("ELEVENLABS_API_KEY"),
            agent_id=value("ELEVENLABS_AGENT_ID"),
            agent_phone_number_id=value("ELEVENLABS_AGENT_PHONE_NUMBER_ID"),
            timeout_seconds=value("ELEVENLABS_TIMEOUT_SECONDS", "30"),
            allow_outbound_calls=value("ELEVENLABS_ALLOW_OUTBOUND_CALLS", "false"),
        )
    except ValidationError:
        raise HTTPException(503, detail={
            "code": "invalid_elevenlabs_configuration",
            "message": "Check ElevenLabs IDs, timeout and outbound-call flag in backend/.env.",
        }) from None


Config = Annotated[ElevenLabsSettings, Depends(get_elevenlabs_settings)]


async def get_elevenlabs_client(config: Config) -> AsyncIterator[ElevenLabsClient]:
    key = config.api_key.get_secret_value().strip()
    if not key:
        raise HTTPException(503, detail={
            "code": "missing_elevenlabs_api_key",
            "message": "Set ELEVENLABS_API_KEY in backend/.env; do not send it in the request.",
        })
    async with httpx.AsyncClient(
        base_url="https://api.elevenlabs.io",
        headers={"xi-api-key": key},
        timeout=config.timeout_seconds,
        follow_redirects=False,
    ) as http:
        yield ElevenLabsClient(http)


Client = Annotated[ElevenLabsClient, Depends(get_elevenlabs_client)]


def upstream_error(error: ElevenLabsError) -> HTTPException:
    detail = {
        "code": error.code,
        "message": error.message,
        "outcome_unknown": error.outcome_unknown,
    }
    if error.upstream_status is not None:
        detail["upstream_status"] = error.upstream_status
    return HTTPException(error.status_code, detail=detail)


def require_agent(config: ElevenLabsSettings) -> str:
    if not config.agent_id:
        raise HTTPException(503, detail={
            "code": "missing_elevenlabs_agent_id",
            "message": "Set ELEVENLABS_AGENT_ID in backend/.env.",
        })
    return config.agent_id


class HotelCallRequest(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="forbid",
        json_schema_extra={"examples": [{
            "hotel_name": "Example Hotel (fictional; preview only)",
            "check_in": "2026-11-01",
            "check_out": "2026-11-03",
            "phone_number": "+14165550123",
        }, {
            "hotel_name": "Example Hotel (fictional; preview only)",
            "address": "123 Example Street, Ottawa, ON",
            "phone_number": "+14165550123",
            "check_in": "2026-10-25",
            "check_out": "2026-10-26",
            "guests": 1,
            "online_price": 99.50,
            "currency": "CAD",
            "price_basis": "total_stay",
            "taxes_included": None,
            "source_url": "https://example.com/hotel",
            "confirm_call": False,
        }]},
    )

    hotel_name: str = Field(min_length=1, max_length=200)
    address: str | None = Field(default=None, min_length=1, max_length=500)
    phone_number: str = Field(pattern=r"^\+[1-9]\d{7,14}$", description="Destination in E.164 format.")
    check_in: date
    check_out: date
    guests: int = Field(default=1, ge=1, le=20, strict=True)
    online_price: Decimal | None = Field(default=None, ge=0, le=1000000, allow_inf_nan=False)
    currency: str = Field(default="CAD", pattern=r"^[A-Z]{3}$")
    price_basis: Literal["total_stay", "per_night"] = "total_stay"
    taxes_included: StrictBool | None = None
    source_url: HttpUrl | None = Field(default=None, max_length=2048)
    source_checked_at: datetime | None = None
    confirm_call: StrictBool = Field(default=False, description="True explicitly requests a real call.")

    @model_validator(mode="after")
    def valid_dates(self) -> "HotelCallRequest":
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in.")
        if self.source_checked_at and self.source_checked_at.utcoffset() is None:
            raise ValueError("source_checked_at must include a timezone.")
        if self.source_url and (self.source_url.username or self.source_url.password):
            raise ValueError("source_url must not contain credentials.")
        return self


def build_call_payload(request: HotelCallRequest, config: ElevenLabsSettings) -> dict:
    """Only flat, scalar context; never override the shared agent's prompt or tools."""
    variables = {
        "hotel_name": request.hotel_name,
        "hotel_address": request.address if request.address is not None else "unknown",
        "hotel_phone": request.phone_number,
        "check_in": request.check_in.isoformat(),
        "check_out": request.check_out.isoformat(),
        "nights": (request.check_out - request.check_in).days,
        "guests": request.guests,
        "online_price": str(request.online_price) if request.online_price is not None else "unknown",
        "currency": request.currency,
        "price_basis": request.price_basis,
        "taxes_included": request.taxes_included if request.taxes_included is not None else "unknown",
        "source_url": str(request.source_url) if request.source_url else "unknown",
        "source_checked_at": request.source_checked_at.isoformat() if request.source_checked_at else "unknown",
    }
    return {
        "agent_id": config.agent_id or None,
        "agent_phone_number_id": config.agent_phone_number_id or None,
        "to_number": request.phone_number,
        "conversation_initiation_client_data": {"dynamic_variables": variables},
        "call_recording_enabled": False,
    }


@router.get("/status", summary="Check local configuration without calling ElevenLabs")
async def status(config: Config) -> dict:
    return {
        "ok": True,
        "api_key_configured": bool(config.api_key.get_secret_value().strip()),
        "agent_id_configured": bool(config.agent_id),
        "phone_number_id_configured": bool(config.agent_phone_number_id),
        "outbound_calls_enabled": config.allow_outbound_calls,
    }


@router.get("/agent", summary="Read the configured agent without modifying it")
async def agent(config: Config, client: Client) -> dict:
    agent_id = require_agent(config)
    try:
        data = await client.get_agent(agent_id)
        if data.get("agent_id") != agent_id or not isinstance(data.get("name"), str):
            raise ValueError
        agent_config = data["conversation_config"]["agent"]
        prompt = agent_config["prompt"]["prompt"]
        first_message = agent_config["first_message"]
        language = agent_config.get("language")
        if not isinstance(prompt, str) or not isinstance(first_message, str):
            raise ValueError
        if language is not None and not isinstance(language, str):
            raise ValueError
        return {
            "ok": True, "agent_id": agent_id, "name": data["name"],
            "language": language,
            "first_message": first_message,
            "prompt_variables": sorted(set(re.findall(
                r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", prompt + "\n" + first_message
            ))),
        }
    except ElevenLabsError as error:
        raise upstream_error(error) from None
    except (AttributeError, KeyError, TypeError, ValueError):
        raise HTTPException(502, detail={
            "code": "invalid_elevenlabs_response", "message": "ElevenLabs returned invalid agent data.",
        }) from None


@router.post("/calls/preview", summary="Preview hotel context; NEVER places a call")
async def preview(request: HotelCallRequest, config: Config) -> dict:
    missing = [name for name, present in {
        "ELEVENLABS_API_KEY": bool(config.api_key.get_secret_value().strip()),
        "ELEVENLABS_AGENT_ID": bool(config.agent_id),
        "ELEVENLABS_AGENT_PHONE_NUMBER_ID": bool(config.agent_phone_number_id),
    }.items() if not present]
    return {
        "ok": True, "dry_run": True,
        "ready_to_call": not missing and config.allow_outbound_calls,
        "outbound_calls_enabled": config.allow_outbound_calls,
        "missing_configuration": missing,
        "payload": build_call_payload(request, config),
        "warning": "Preview only. Online price and availability are not phone-confirmed.",
    }


async def validate_call_permission(request: HotelCallRequest, config: Config) -> None:
    """Run all intentional-dial checks before even constructing the API client."""
    if not config.allow_outbound_calls:
        raise HTTPException(403, detail={
            "code": "outbound_calls_disabled",
            "message": "Preview first. Set ELEVENLABS_ALLOW_OUTBOUND_CALLS=true to enable real calls.",
        })
    if not request.confirm_call:
        raise HTTPException(409, detail={
            "code": "call_confirmation_required",
            "message": "Set confirm_call=true only when you intend to dial this number and incur charges.",
        })
    require_agent(config)
    if not config.agent_phone_number_id:
        raise HTTPException(503, detail={
            "code": "missing_elevenlabs_phone_number_id",
            "message": "Set ELEVENLABS_AGENT_PHONE_NUMBER_ID to the ElevenLabs linked Twilio number ID.",
        })


@router.post("/calls", summary="Place ONE real, potentially billable Twilio call")
async def call(
    request: HotelCallRequest,
    config: Config,
    _permission: Annotated[None, Depends(validate_call_permission)],
    client: Client,
) -> dict:
    try:
        data = await client.start_call(build_call_payload(request, config))
    except ElevenLabsError as error:
        raise upstream_error(error) from None
    if data.get("success") is False:
        raise HTTPException(502, detail={
            "code": "elevenlabs_call_rejected", "message": "ElevenLabs did not accept the call request.",
            "outcome_unknown": False,
        })
    conversation_id, call_sid = data.get("conversation_id"), data.get("callSid")
    if data.get("success") is not True or not (
        isinstance(conversation_id, str) and re.fullmatch(ID_PATTERN, conversation_id)
    ):
        raise HTTPException(502, detail={
            "code": "invalid_elevenlabs_response", "outcome_unknown": True,
            "message": "The call may already be placed. Check the ElevenLabs dashboard before retrying.",
        })
    return {
        "ok": True, "status": "initiated", "conversation_id": conversation_id,
        "call_sid": call_sid if isinstance(call_sid, str) else None,
        "to_number": request.phone_number,
        "message": "Call request accepted; this is NOT proof of availability or a confirmed price.",
    }


class TranscriptTurn(BaseModel):
    role: str
    message: str | None = None
    time_in_call_secs: float = Field(ge=0, allow_inf_nan=False)


class ConversationMetadata(BaseModel):
    call_duration_secs: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ConversationAnalysis(BaseModel):
    call_successful: str | None = None
    transcript_summary: str | None = None
    data_collection_results: dict | None = None


@router.get("/conversations/{conversation_id}", summary="Read call status and every original transcript utterance")
async def conversation(
    conversation_id: Annotated[str, PathParam(pattern=ID_PATTERN)], config: Config, client: Client,
) -> dict:
    agent_id = require_agent(config)
    try:
        data = await client.get_conversation(conversation_id)
        if not isinstance(data.get("agent_id"), str) or not re.fullmatch(ID_PATTERN, data["agent_id"]):
            raise ValueError
        if data.get("agent_id") != agent_id:
            raise HTTPException(404, detail={
                "code": "elevenlabs_conversation_not_found",
                "message": "This conversation does not belong to the configured agent.",
            })
        if data.get("conversation_id") != conversation_id:
            raise ValueError
        call_status = data.get("status")
        if call_status not in {"initiated", "in-progress", "processing", "done", "failed"}:
            raise ValueError
        if not isinstance(data["transcript"], list):
            raise ValueError
        turns = [TranscriptTurn.model_validate(turn) for turn in data["transcript"]]
        metadata = ConversationMetadata.model_validate(
            {} if data.get("metadata") is None else data["metadata"]
        )
        analysis = ConversationAnalysis.model_validate(
            {} if data.get("analysis") is None else data["analysis"]
        )
        return {
            "ok": True, "conversation_id": conversation_id, "agent_id": agent_id,
            "status": call_status, "finished": call_status in {"done", "failed"},
            "transcript": [turn.model_dump() for turn in turns],
            "transcript_text": "\n".join(
                f"[{turn.time_in_call_secs:g}s] {turn.role}: {turn.message}"
                for turn in turns if turn.message is not None
            ),
            "call_duration_secs": metadata.call_duration_secs,
            "call_successful": analysis.call_successful,
            "transcript_summary": analysis.transcript_summary,
            "data_collection_results": analysis.data_collection_results or {},
        }
    except ElevenLabsError as error:
        raise upstream_error(error) from None
    except (AttributeError, KeyError, TypeError, ValueError):
        raise HTTPException(502, detail={
            "code": "invalid_elevenlabs_response",
            "message": "ElevenLabs did not return valid conversation/transcript data.",
        }) from None
