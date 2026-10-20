"""Optional real delivery. Without SMTP_HOST the email only lands in the in-app outbox."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.config import settings


def deliver(to: str, subject: str, html: str, text: str) -> str:
    if not settings.smtp_host:
        return "outbox_only"
    # Demo customers have fake addresses; SMTP_REDIRECT_TO keeps real mail going to the tester.
    rcpt = settings.smtp_redirect_to or to
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, settings.smtp_from, rcpt
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as s:
            s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
        return f"smtp_sent:{rcpt}"
    except Exception as e:  # noqa: BLE001
        return f"smtp_failed:{type(e).__name__}"
