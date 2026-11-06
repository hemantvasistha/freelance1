"""Outbound email delivery for dunning reminders.

Two implementations exist so the pipeline is testable and safe by default:

* `RecordingMailer` — never touches the network. Returns `DRY_RUN` and keeps the
  rendered emails in memory. Used when `DRY_RUN=true` (the default) and in tests.
* `SmtpMailer` — real STARTTLS delivery through `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/
  `SMTP_PASSWORD`.

`smtplib` has no retry of its own, so transient failures are the caller's problem:
`send()` reports `(status, error)` instead of raising, and the dunning scheduler
stores that outcome on the reminder it produced.
"""
import logging
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from typing import Protocol

from src.config import Settings

logger = logging.getLogger(__name__)

DELIVERY_SENT = "SENT"
DELIVERY_DRY_RUN = "DRY_RUN"
DELIVERY_FAILED = "FAILED"
DELIVERY_PENDING = "PENDING"


class MailerNotConfigured(RuntimeError):
    """Raised when live delivery is requested without SMTP credentials."""


@dataclass(frozen=True)
class OutboundEmail:
    to: str
    subject: str
    body: str


class Mailer(Protocol):
    def send(self, email: OutboundEmail) -> tuple[str, str | None]:
        """Delivers `email`, returning `(status, error_message_or_None)`."""


@dataclass
class RecordingMailer:
    """Captures emails instead of sending them. Used for DRY_RUN and tests."""

    sent: list[OutboundEmail] = field(default_factory=list)

    def send(self, email: OutboundEmail) -> tuple[str, str | None]:
        self.sent.append(email)
        logger.info("DRY_RUN: would email %s — %s", email.to, email.subject)
        return DELIVERY_DRY_RUN, None


class SmtpMailer:
    """Real SMTP delivery with STARTTLS."""

    def __init__(self, host: str, port: int, user: str, password: str, sender: str | None = None):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.sender = sender or user

    def send(self, email: OutboundEmail) -> tuple[str, str | None]:
        message = MimeMessage()
        message["From"] = self.sender
        message["To"] = email.to
        message["Subject"] = email.subject
        message.set_content(email.body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                if self.user and self.password:
                    smtp.login(self.user, self.password)
                smtp.send_message(message)
            return DELIVERY_SENT, None
        except (smtplib.SMTPException, OSError) as exc:
            logger.warning("Email delivery to %s failed: %s", email.to, exc)
            return DELIVERY_FAILED, str(exc)


def build_mailer(settings: Settings) -> Mailer:
    """Chooses the delivery backend for this process.

    `DRY_RUN=true` (default) always records instead of sending. Turning DRY_RUN off
    requires real SMTP credentials — guessing would silently fail every reminder.
    """
    if settings.dry_run:
        return RecordingMailer()
    if not settings.smtp_password:
        raise MailerNotConfigured(
            "DRY_RUN=false but SMTP_PASSWORD is not set; refusing to drop reminders silently."
        )
    return SmtpMailer(
        host=settings.smtp_host,
        port=settings.smtp_port,
        user=settings.smtp_user,
        password=settings.smtp_password,
    )
