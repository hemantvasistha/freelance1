"""Storage for job leads: dedupe by source+id, and a human-owned status field."""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

from src.jobs.intake import ScoredListing

STATUSES = ("NEW", "APPROVED", "SKIPPED")

SCHEMA = """
CREATE TABLE IF NOT EXISTS job_leads (
    id               TEXT PRIMARY KEY,
    source           TEXT NOT NULL,
    external_id      TEXT NOT NULL,
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    url              TEXT NOT NULL DEFAULT '',
    budget_min_minor INTEGER,
    budget_max_minor INTEGER,
    currency         TEXT NOT NULL DEFAULT '',
    posted_at        TEXT,
    score            INTEGER NOT NULL DEFAULT 0,
    capability       TEXT,
    reasons          TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'NEW',
    discovered_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_leads_status ON job_leads (status, score DESC);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _identity(listing) -> str:
    """A project found by two search queries is still one lead: key on its canonical URL."""
    url = (listing.url or "").split("?")[0].rstrip("/")
    return url or f"{listing.source}:{listing.external_id}"


def lead_id(source: str, external_id: str) -> str:
    return hashlib.sha256(f"{source}:{external_id}".encode("utf-8")).hexdigest()[:16]


def upsert_lead(conn: sqlite3.Connection, scored: ScoredListing) -> tuple[str, bool]:
    """Inserts a lead once. Returns (id, created). Re-scans never reset human decisions."""
    listing = scored.listing
    lid = lead_id("lead", _identity(listing))
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO job_leads (
            id, source, external_id, title, description, url,
            budget_min_minor, budget_max_minor, currency, posted_at,
            score, capability, reasons, status, discovered_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NEW', ?)
        """,
        (
            lid,
            listing.source,
            listing.external_id,
            listing.title,
            listing.description,
            listing.url,
            listing.budget_min_minor,
            listing.budget_max_minor,
            listing.currency,
            listing.posted_at,
            scored.score,
            scored.capability,
            "; ".join(scored.reasons),
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )
    created = cur.rowcount == 1
    if not created:
        # Keep the score fresh but never overwrite an APPROVED/SKIPPED decision.
        conn.execute(
            "UPDATE job_leads SET score = ?, capability = ?, reasons = ? WHERE id = ? AND status = 'NEW'",
            (scored.score, scored.capability, "; ".join(scored.reasons), lid),
        )
    conn.commit()
    return lid, created


def list_leads(
    conn: sqlite3.Connection,
    status: str | None = None,
    min_score: int = 0,
    limit: int = 50,
) -> list[dict]:
    query = "SELECT * FROM job_leads WHERE score >= ?"
    params: list[object] = [min_score]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY score DESC, discovered_at DESC LIMIT ?"
    params.append(limit)
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def set_lead_status(conn: sqlite3.Connection, lid: str, status: str) -> bool:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    cur = conn.execute("UPDATE job_leads SET status = ? WHERE id = ?", (status, lid))
    conn.commit()
    return cur.rowcount == 1
