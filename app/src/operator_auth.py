"""Tenant-scoped operator authentication.

The customer's Apps Script must never hold the server-wide admin token: that token
unlocks every tenant's dunning and digest. Instead each tenant gets its own operator
token. Only its SHA-256 hash is stored, exactly like a password, so a database leak
does not hand out working credentials.

The plaintext token is shown once, at onboarding, and pasted into that tenant's sheet.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import deque

from src.models import TenantRecord

OPERATOR_TOKEN_HEADER = "X-SheetFlow-Operator-Token"

# Requests per minute allowed per operator token. Cheap in-process guard: this
# deployment is a single process, so a shared limiter would be overhead.
RATE_LIMIT_PER_MINUTE = 60

_lock = threading.Lock()
_recent_calls: dict[str, deque[float]] = {}


def generate_operator_token() -> str:
    """A fresh, unguessable tenant credential (shown to the operator exactly once)."""
    return f"op_{secrets.token_urlsafe(32)}"


def hash_operator_token(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def resolve_tenant_by_token(conn, token: str) -> TenantRecord | None:
    """Looks a tenant up by operator token. Constant-time on the hash comparison."""
    if not token or not token.strip():
        return None
    candidate = hash_operator_token(token)
    rows = conn.execute(
        "SELECT * FROM tenants WHERE operator_token_hash IS NOT NULL AND is_active = 1"
    ).fetchall()
    from src.models import SheetFlowRepository  # local import: avoid a cycle at import time

    repo = SheetFlowRepository(conn)
    for row in rows:
        stored = row["operator_token_hash"]
        if stored and secrets.compare_digest(stored, candidate):
            return repo._row_to_tenant(row)
    return None


def check_rate_limit(token: str) -> bool:
    """True when the token is under RATE_LIMIT_PER_MINUTE; False when it must wait."""
    now = time.monotonic()
    key = hash_operator_token(token)[:16]
    with _lock:
        bucket = _recent_calls.setdefault(key, deque())
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT_PER_MINUTE:
            return False
        bucket.append(now)
        return True


def reset_rate_limits() -> None:
    """Test helper: clears the in-process limiter."""
    with _lock:
        _recent_calls.clear()
