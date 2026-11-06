"""Block 1: Pydantic Schemas & Deterministic Integer Minor-Unit Tax Engine."""
import hashlib
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator

InvoiceStatus = Literal["DRAFT", "READY_TO_BILL", "SENT", "OVERDUE", "PAID", "CANCELLED"]
ReminderStage = Literal["NONE", "T_MINUS_3", "T_ZERO", "T_PLUS_7"]


class TaxBreakdown(BaseModel):
    subtotal_minor: int
    tax_rate_bps: int
    cgst_minor: int
    sgst_minor: int
    igst_minor: int
    total_minor: int
    is_intra_state: bool


def to_minor_units(amount_major: str | Decimal | int | float) -> int:
    """Convert major currency string/Decimal (e.g. '3500.50') to integer minor units (350050)."""
    dec = Decimal(str(amount_major))
    if dec < Decimal("0"):
        raise ValueError("Monetary amount cannot be negative.")
    return int((dec * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_basis_points(rate_pct: str | Decimal | int | float) -> int:
    """Convert percentage (e.g. '18.00') to integer basis points (1800)."""
    dec = Decimal(str(rate_pct))
    if dec < Decimal("0") or dec > Decimal("100"):
        raise ValueError("Tax rate percentage must be between 0 and 100.")
    return int((dec * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def currency_symbol(currency_code: str) -> str:
    """Display prefix for an ISO currency code: '₹' for INR, otherwise 'USD '."""
    code = (currency_code or "INR").upper()
    return "₹" if code == "INR" else f"{code} "


def format_major_currency(minor_units: int, symbol: str = "₹") -> str:
    """Format integer minor units (e.g. 413000) to readable string ('₹4,130.00')."""
    dec = (Decimal(minor_units) / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{symbol}{dec:,.2f}"


def compute_idempotency_key(tenant_id: str, invoice_number: str, issue_date: str) -> str:
    raw = f"{tenant_id.strip()}:{invoice_number.strip()}:{issue_date.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def calculate_gst_breakdown(
    subtotal_major: str | Decimal,
    tax_rate_pct: str | Decimal,
    tenant_home_state: str,
    client_state: str | None,
) -> TaxBreakdown:
    """
    Deterministic GST/Tax split calculator in integer minor units (paise/cents).
    - Intra-state (same state): splits tax equally into CGST and SGST.
    - Inter-state (different state): applies full tax to IGST.
    """
    subtotal_minor = to_minor_units(subtotal_major)
    tax_rate_bps = to_basis_points(tax_rate_pct)

    home_norm = (tenant_home_state or "").strip().lower()
    # A blank place of supply is treated as the supplier's own state (intra-state),
    # matching how the invoice PDF prints "Place of Supply". Defaulting to IGST here
    # would silently over-tax rows where the client simply left the cell empty.
    client_norm = (client_state or "").strip().lower() or home_norm
    is_intra = bool(home_norm and client_norm and home_norm == client_norm)

    if tax_rate_bps == 0:
        return TaxBreakdown(
            subtotal_minor=subtotal_minor,
            tax_rate_bps=0,
            cgst_minor=0,
            sgst_minor=0,
            igst_minor=0,
            total_minor=subtotal_minor,
            is_intra_state=is_intra,
        )

    if is_intra:
        half_bps = Decimal(tax_rate_bps) / Decimal("2")
        cgst_minor = int(
            (Decimal(subtotal_minor) * half_bps / Decimal("10000")).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        sgst_minor = cgst_minor
        igst_minor = 0
    else:
        cgst_minor = 0
        sgst_minor = 0
        igst_minor = int(
            (Decimal(subtotal_minor) * Decimal(tax_rate_bps) / Decimal("10000")).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )

    total_minor = subtotal_minor + cgst_minor + sgst_minor + igst_minor
    return TaxBreakdown(
        subtotal_minor=subtotal_minor,
        tax_rate_bps=tax_rate_bps,
        cgst_minor=cgst_minor,
        sgst_minor=sgst_minor,
        igst_minor=igst_minor,
        total_minor=total_minor,
        is_intra_state=is_intra,
    )


class SheetRowWebhookPayload(BaseModel):
    tenant_id: str = Field(..., min_length=3)
    sheet_row_number: int = Field(..., ge=2)
    invoice_number: str = Field(..., min_length=1)
    client_name: str = Field(..., min_length=1)
    client_email: str = Field(..., min_length=3)
    client_phone: str | None = None
    client_state: str = Field(default="")
    item_description: str = Field(..., min_length=1)
    subtotal_major: str = Field(..., description="Decimal string e.g. '3500.00'")
    tax_rate_pct: str = Field(default="18.00", description="Decimal string e.g. '18.00'")
    issue_date: str = Field(..., description="YYYY-MM-DD")
    due_date: str = Field(..., description="YYYY-MM-DD")
    # Ingestion may only ever open an invoice. Paying, cancelling, or marking an
    # invoice SENT/OVERDUE is driven by this service, never by the caller.
    status: Literal["READY_TO_BILL"] = Field(default="READY_TO_BILL")

    @field_validator("client_email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError(f"Invalid client_email: {v}")
        return v

    @model_validator(mode="after")
    def validate_dates_and_amounts(self) -> "SheetRowWebhookPayload":
        try:
            d_issue = date.fromisoformat(self.issue_date)
            d_due = date.fromisoformat(self.due_date)
        except ValueError as exc:
            raise ValueError(f"issue_date and due_date must be YYYY-MM-DD ({exc})")
        if d_due < d_issue:
            raise ValueError("due_date cannot be earlier than issue_date")
        to_minor_units(self.subtotal_major)
        to_basis_points(self.tax_rate_pct)
        return self


class PaymentWebhookPayload(BaseModel):
    tenant_id: str
    invoice_number: str
    gateway_payment_id: str
    amount_paid_minor: int = Field(..., gt=0)
    paid_at_iso: str
    signature: str
