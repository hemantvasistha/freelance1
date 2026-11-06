"""Block 2: Deterministic payment reference & NPCI UPI deep link generator.

This deployment has exactly one real payment rail: the tenant's own UPI VPA.
There is no hosted checkout page, so none is invented — `payment_url` is the UPI
intent link itself, which opens GPay/PhonePe/Paytm pre-filled on any phone and
costs nothing to collect.

To keep card or international collection later, add a gateway adapter that fills
`payment_url` with a signed gateway URL; every consumer (sheet column M, PDF,
reminder emails) already reads this one field.
"""
import hashlib
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlencode
from dataclasses import dataclass

from src.models import InvoiceRecord, TenantRecord


@dataclass(frozen=True)
class PaymentArtifacts:
    payment_url: str
    upi_uri: str | None
    gateway_reference: str


def generate_payment_artifacts(tenant: TenantRecord, invoice: InvoiceRecord) -> PaymentArtifacts:
    """Builds the deterministic reference and the UPI deep link the customer can tap."""
    digest = hashlib.sha256(
        f"{tenant.id}:{invoice.invoice_number}:{invoice.total_minor}".encode("utf-8")
    ).hexdigest()[:12]
    gateway_ref = f"pay_{digest}"

    amount_major_str = str(
        (Decimal(invoice.total_minor) / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )

    upi_uri = None
    if tenant.upi_vpa and tenant.currency_code.upper() == "INR":
        params = {
            "pa": tenant.upi_vpa,
            "pn": tenant.business_name,
            "tr": gateway_ref,
            "tn": f"Invoice {invoice.invoice_number}",
            "am": amount_major_str,
            "cu": "INR",
        }
        upi_uri = f"upi://pay?{urlencode(params)}"

    return PaymentArtifacts(
        payment_url=upi_uri or "",
        upi_uri=upi_uri,
        gateway_reference=gateway_ref,
    )
