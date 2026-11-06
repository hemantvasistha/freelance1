"""Pitch drafting for shortlisted job leads.

One shortlisted lead becomes one plain-text email draft addressed to you (the operator),
ready to forward to the client with minimal editing. Drafts are stored in
`job_pitches` so re-runs never lose a pitch you already composed.

Deliberately not auto-sending to the client: the operator reviews each one first.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from src.jobs.intake import ScoredListing, RECOMMENDED_MIN_SCORE
from src.notifications.mailer import OutboundEmail


PITCH_TABLE = """
CREATE TABLE IF NOT EXISTS job_pitches (
    id               TEXT PRIMARY KEY,
    lead_id          TEXT NOT NULL,
    subject          TEXT NOT NULL,
    body             TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_pitches_lead ON job_pitches (lead_id);
"""


def ensure_pitch_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(PITCH_TABLE)
    conn.commit()


def pitch_id(lead_id: str) -> str:
    import hashlib

    return hashlib.sha256(f"pitch:{lead_id}:{datetime.now(timezone.utc).date()}".encode()).hexdigest()[:16]


def render_pitch(listing: ScoredListing, you_email: str) -> OutboundEmail:
    """One draft email per shortlisted lead, addressed to the operator to forward."""
    reasons = "; ".join(listing.reasons) if listing.reasons else "no notes"
    budget = _budget_line(listing.listing)

    body = (
        f"Found via: {listing.listing.source}\n"
        f"Posted: {listing.listing.posted_at or 'unknown'}\n"
        f"Score: {listing.score}/25 ({reasons})\n\n"
        f"Title: {listing.listing.title}\n\n"
        f"{budget}\n\n"
        f"Link: {listing.listing.url}\n\n"
        f"Description:\n{listing.listing.description[:1200]}\n\n"
        f"---\n"
        f"I can take this on. Happy to send a fixed quote and a quick turnaround estimate "
        f"if the scope looks right. Let me know if you want me to proceed.\n"
    )

    return OutboundEmail(
        to=you_email,
        subject=f"[SheetFlow pitch] {listing.listing.title[:60]}",
        body=body,
    )


def _budget_line(listing) -> str:
    lo = listing.budget_min_minor
    hi = listing.budget_max_minor
    cur = (listing.currency or "INR").upper()
    if not lo and not hi:
        return f"Budget: not stated ({cur})"
    if hi and hi != lo:
        return f"Budget: {cur} {lo/100:g} - {hi/100:g}"
    return f"Budget: {cur} {lo/100:g}"


def upsert_pitch(conn: sqlite3.Connection, lead_id: str, subject: str, body: str) -> str:
    pid = pitch_id(lead_id)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT OR REPLACE INTO job_pitches (id, lead_id, subject, body, created_at) VALUES (?, ?, ?, ?, ?)",
        (pid, lead_id, subject, body, now),
    )
    conn.commit()
    return pid
