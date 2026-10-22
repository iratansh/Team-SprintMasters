"""Gmail SMTP settings and confirmation email rendering/sending."""

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Annotated

from dotenv import dotenv_values
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field, SecretStr, ValidationError

ENV_PATH = Path(__file__).resolve().parent / ".env"
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class EmailSettings(BaseModel):
    sender_email: str = Field(default="", pattern=EMAIL_PATTERN)
    recipient_email: str = Field(default="", pattern=EMAIL_PATTERN)
    app_password: SecretStr = SecretStr("")
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = Field(default=587, ge=1, le=65535)
    timeout_seconds: float = Field(default=20, gt=0, le=120)


def get_email_settings() -> EmailSettings:
    """Read email settings from backend/.env; exported variables take precedence."""
    local = dotenv_values(ENV_PATH)

    def value(name: str, default: str = "") -> str:
        return str(os.environ.get(name, local.get(name) or default)).strip()

    try:
        return EmailSettings(
            sender_email=value("EMAIL_SENDER"),
            recipient_email=value("EMAIL_RECIPIENT"),
            app_password=value("EMAIL_APP_PASSWORD"),
            smtp_host=value("EMAIL_SMTP_HOST", "smtp.gmail.com"),
            smtp_port=value("EMAIL_SMTP_PORT", "587"),
            timeout_seconds=value("EMAIL_SMTP_TIMEOUT_SECONDS", "20"),
        )
    except ValidationError:
        raise HTTPException(503, detail={
            "code": "invalid_email_configuration",
            "message": "Check EMAIL_SENDER, EMAIL_RECIPIENT, SMTP host, port, and timeout in backend/.env.",
        }) from None


EmailConfig = Annotated[EmailSettings, Depends(get_email_settings)]


def missing_email_configuration(config: EmailSettings) -> list[str]:
    missing = []
    if not config.sender_email:
        missing.append("EMAIL_SENDER")
    if not config.recipient_email:
        missing.append("EMAIL_RECIPIENT")
    if not config.app_password.get_secret_value().strip():
        missing.append("EMAIL_APP_PASSWORD")
    return missing


def build_confirmation_email(
    *,
    sender_email: str,
    recipient_email: str,
    hotel_name: str,
    address: str,
    phone: str,
    price: str,
    reservation_date: str,
    transcript: str,
    reservation_number: str | None = None,
    confirmed_with: str | None = None,
) -> EmailMessage:
    """Build the organizer's email format, clearly marking this as an enquiry."""
    subject = "Team # 20 - Hotel Reservation Confirmed!"
    fields = [
        ("Hotel Name", hotel_name),
        ("Address", address),
        ("Phone", phone),
        ("Price", price),
        ("Reservation Date", reservation_date),
        ("Reservation #", reservation_number or "Not provided"),
        ("Confirmed with", confirmed_with or "Not provided"),
    ]
    body_lines = [
        "Hotel quote enquiry confirmation",
        "",
        "This confirms the availability and price enquiry only. No room was booked or held.",
        "",
        *(f"{label}: {value}" for label, value in fields),
        "",
        "Call Transcript:",
        transcript,
    ]
    text_body = "\n".join(body_lines)
    html_rows = "".join(
        "<tr><th style=\"text-align:left;padding:7px 12px 7px 0;color:#69707d;font-weight:500\">"
        f"{label}</th><td style=\"padding:7px 0;color:#252633\">{_html_escape(value)}</td></tr>"
        for label, value in fields
    )
    html_body = (
        "<div style=\"font:15px/1.6 Arial,sans-serif;color:#252633;max-width:680px;margin:24px auto;padding:0 20px\">"
        "<h2 style=\"font-size:21px;margin-bottom:8px\">Hotel quote enquiry confirmation</h2>"
        "<p style=\"color:#69707d;margin-top:0\">This confirms the availability and price enquiry only. "
        "No room was booked or held.</p>"
        f"<table style=\"border-collapse:collapse;margin:22px 0\">{html_rows}</table>"
        "<h3 style=\"font-size:15px;margin-bottom:8px\">Call Transcript</h3>"
        f"<pre style=\"white-space:pre-wrap;overflow-wrap:anywhere;background:#f6f6f8;padding:14px;border-radius:8px;font:13px/1.6 Arial,sans-serif\">{_html_escape(transcript)}</pre>"
        "<p style=\"color:#92939f;font-size:12px;margin-top:22px\">Team SprintMasters · Team 20</p></div>"
    )

    message = EmailMessage()
    message["From"] = formataddr(("Team SprintMasters", sender_email))
    message["To"] = recipient_email
    message["Subject"] = subject
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    return message


def _html_escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#x27;"))


def send_confirmation(config: EmailSettings, message: EmailMessage) -> None:
    """Deliver one confirmation over Gmail STARTTLS; never log message contents."""
    password = config.app_password.get_secret_value().replace(" ", "")
    context = ssl.create_default_context()
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=config.timeout_seconds) as smtp:
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.ehlo()
        smtp.login(config.sender_email, password)
        refused = smtp.send_message(message, from_addr=config.sender_email,
                                    to_addrs=[config.recipient_email])
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
