"""Compliant freelance job intake (official APIs + RSS, human-approved)."""
from src.jobs.intake import (
    FeedSource,
    RawListing,
    ScoredListing,
    parse_freelancer_projects,
    parse_rss,
    run_scan,
    score_listing,
    sources_from_env,
)
from src.jobs.store import ensure_schema, list_leads, set_lead_status

__all__ = [
    "FeedSource",
    "RawListing",
    "ScoredListing",
    "ensure_schema",
    "list_leads",
    "parse_freelancer_projects",
    "parse_rss",
    "run_scan",
    "score_listing",
    "set_lead_status",
    "sources_from_env",
]
