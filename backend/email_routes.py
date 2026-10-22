"""Configured, fixed-recipient email delivery for hotel call evidence."""

import asyncio
import logging
import smtplib
from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.email_sender import (
    EmailConfig,
    build_confirmation_email,
    missing_email_configuration,
    send_confirmation,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/email", tags=["Email"])


class ConfirmationEmailRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    hotel_name: str = Field(min_length=1, max_length=200)
    address: str = Field(min_length=1, max_length=500)
    phone: str = Field(min_length=1, max_length=50)
    price: str = Field(min_length=1, max_length=150)
    reservation_date: date
    reservation_number: str | None = Field(default=None, max_length=100)
    confirmed_with: str | None = Field(default=None, max_length=150)
    transcript: str = Field(min_length=1, max_length=30000)


@router.get("/status", summary="Check Gmail SMTP configuration without sending email")
async def email_status(config: EmailConfig) -> dict:
    missing = missing_email_configuration(config)
    return {
        "ok": True,
        "configured": not missing,
        "provider": "gmail_smtp",
        "sender_email": config.sender_email or None,
        "recipient_email": config.recipient_email or None,
        "missing_configuration": missing,
    }


@router.post("/confirmation", summary="Send the hotel enquiry confirmation template")
async def send_confirmation_email(
    request: ConfirmationEmailRequest,
    config: EmailConfig,
) -> dict:
    missing = missing_email_configuration(config)
    if missing:
        raise HTTPException(503, detail={
            "code": "email_not_configured",
            "message": "Set the required email settings in backend/.env.",
            "missing_configuration": missing,
        })

    message = build_confirmation_email(
        sender_email=config.sender_email,
        recipient_email=config.recipient_email,
        hotel_name=request.hotel_name,
        address=request.address,
        phone=request.phone,
        price=request.price,
        reservation_date=request.reservation_date.isoformat(),
        reservation_number=request.reservation_number,
        confirmed_with=request.confirmed_with,
        transcript=request.transcript,
    )
    try:
        await asyncio.to_thread(send_confirmation, config, message)
    except smtplib.SMTPAuthenticationError:
        logger.warning("Gmail SMTP authentication failed")
        raise HTTPException(502, detail={
            "code": "gmail_auth_failed",
            "message": "Gmail rejected the app password. Check EMAIL_SENDER and EMAIL_APP_PASSWORD.",
            "outcome_unknown": False,
        }) from None
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
        logger.warning("Gmail SMTP rejected the confirmation message")
        raise HTTPException(502, detail={
            "code": "email_rejected",
            "message": "Gmail rejected the sender, recipient, or message. Check the email configuration.",
            "outcome_unknown": False,
        }) from None
    except (smtplib.SMTPException, OSError, TimeoutError):
        # A connection failure during delivery may happen after Gmail accepted DATA.
        logger.warning("Gmail SMTP delivery outcome could not be confirmed")
        raise HTTPException(502, detail={
            "code": "email_delivery_unknown",
            "message": "Gmail connection ended before delivery could be confirmed. Check the recipient inbox before retrying.",
            "outcome_unknown": True,
        }) from None

    return {
        "ok": True,
        "status": "accepted",
        "provider": "gmail_smtp",
        "recipient_email": config.recipient_email,
        "subject": str(message["Subject"]),
        "message": "Gmail accepted the email for delivery; this does not confirm it was read.",
    }
