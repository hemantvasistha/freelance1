"""Block 1: Application Configuration & Environment Settings."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    app_name: str = "SheetFlow Engine"
    version: str = "1.0.0"
    db_path: str = "sheetflow.db"
    dry_run: bool = True
    output_pdf_dir: str = "generated_invoices"
    default_currency: str = "INR"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = "billing@example.com"
    smtp_password: str = ""
    # Public base URL used to build client-openable invoice PDF links. When empty,
    # the API returns the on-disk path instead (useful only for local demos).
    public_base_url: str = ""
    # Shared secret required on the operator-only endpoints (/dunning/run, /digest).
    # Empty means those endpoints are disabled rather than open to the internet.
    admin_token: str = ""
    # Fallback recipient for the weekly digest when a tenant has no owner_email set.
    owner_email: str = ""
    # Razorpay: link creation needs the API key pair; webhook reconciliation needs only
    # the dashboard webhook secret. Both empty means UPI-only collection.
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    # In-process scheduler: when enabled, the API runs the daily reminder cycle and
    # the Monday digest itself, so no external cron is required.
    scheduler_enabled: bool = False
    scheduler_hour: int = 8
    scheduler_poll_seconds: int = 300


def get_settings(db_path_override: str | None = None) -> Settings:
    return Settings(
        db_path=db_path_override or os.environ.get("SHEETFLOW_DB_PATH", "sheetflow.db"),
        dry_run=os.environ.get("DRY_RUN", "true").lower() in ("1", "true", "yes"),
        output_pdf_dir=os.environ.get("SHEETFLOW_PDF_DIR", "generated_invoices"),
        default_currency=os.environ.get("DEFAULT_CURRENCY", "INR"),
        smtp_host=os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(os.environ.get("SMTP_PORT", "587")),
        smtp_user=os.environ.get("SMTP_USER", "billing@example.com"),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        public_base_url=os.environ.get("SHEETFLOW_PUBLIC_BASE_URL", ""),
        admin_token=os.environ.get("SHEETFLOW_ADMIN_TOKEN", ""),
        owner_email=os.environ.get("SHEETFLOW_OWNER_EMAIL", ""),
        razorpay_key_id=os.environ.get("RAZORPAY_KEY_ID", ""),
        razorpay_key_secret=os.environ.get("RAZORPAY_KEY_SECRET", ""),
        razorpay_webhook_secret=os.environ.get("RAZORPAY_WEBHOOK_SECRET", ""),
        scheduler_enabled=os.environ.get("SHEETFLOW_SCHEDULER_ENABLED", "false").lower()
        in ("1", "true", "yes"),
        scheduler_hour=int(os.environ.get("SHEETFLOW_SCHEDULER_HOUR", "8")),
        scheduler_poll_seconds=int(os.environ.get("SHEETFLOW_SCHEDULER_POLL_SECONDS", "300")),
    )
