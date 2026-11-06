"""Compliant freelance job intake.

Pulls listings from channels the platforms intend to be machine-readable — official
public APIs and RSS feeds — and scores them against the one deliverable this project
can actually produce: spreadsheet data -> GST invoice PDFs, payment links, reminders.

Deliberately absent: logging in, scraping HTML, auto-bidding, auto-messaging. The
output is a shortlist; a human approves each lead before anything is pitched.
"""
from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Callable, Sequence

# What this codebase can genuinely deliver without a human doing the work by hand.
CAPABILITY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "INVOICE_AUTOMATION": (
        "invoice",
        "billing",
        "gst",
        "tax invoice",
        "pdf invoice",
        "bill generation",
        "quotation",
    ),
    "SPREADSHEET_CLEANUP": (
        "excel",
        "spreadsheet",
        "google sheets",
        "csv",
        "workbook",
        "data cleaning",
        "data cleanup",
        "formatting",
    ),
    "DATA_ENTRY": ("data entry", "copy paste", "retype", "typing work", "manual entry"),
    "PAYMENT_FOLLOWUP": (
        "payment reminder",
        "follow up payment",
        "dunning",
        "overdue",
        "collections",
        "receivables",
    ),
    "REPORTING": (
        "weekly report",
        "cash flow",
        "dashboard",
        "pivot table",
        "financial report",
        "monthly report",
    ),
}

# Titles mentioning these are a different trade; pitching invoice automation wastes time.
OFF_SCOPE_KEYWORDS = (
    "wordpress",
    "shopify theme",
    "photoshop",
    "illustrator",
    "logo",
    "video edit",
    "thumbnail",
    "voice over",
    "translation",
    "seo",
    "android app",
    "ios app",
    "unity",
    "game",
    "3d model",
    "animation",
    "react",
    "flutter",
    "solidity",
    "machine learning",
)

# Below this, the job does not pay for the 15 minutes of onboarding it costs.
MIN_BUDGET_MAJOR: dict[str, int] = {
    "INR": 500,
    "USD": 10,
    "EUR": 10,
    "GBP": 10,
    "AUD": 15,
    "CAD": 15,
    "SGD": 15,
    "AED": 40,
}

RECOMMENDED_MIN_SCORE = 6

# Official public API, no key required. These queries are what this product can deliver.
DEFAULT_FREELANCER_QUERIES = ("invoice", "excel", "data entry")

FREELANCER_SEARCH_URL = (
    "https://www.freelancer.com/api/projects/0.1/projects/active/"
    "?query={query}&limit=20&job_details=true&full_description=true&sort=time_updated"
)

_CURRENCY_SYMBOLS = {"₹": "INR", "$": "USD", "€": "EUR", "£": "GBP"}
_CURRENCY_CODES = ("INR", "USD", "EUR", "GBP", "AUD", "CAD", "SGD", "AED")
_AMOUNT_RE = re.compile(
    r"(?:(?P<sym>[₹$€£])\s?(?P<amt1>\d[\d,]*(?:\.\d+)?))"
    r"|(?:(?P<code>" + "|".join(_CURRENCY_CODES) + r")\s?(?P<amt2>\d[\d,]*(?:\.\d+)?))",
    re.IGNORECASE,
)
_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class RawListing:
    source: str
    external_id: str
    title: str
    description: str
    url: str
    budget_min_minor: int | None = None
    budget_max_minor: int | None = None
    currency: str = ""
    posted_at: str | None = None


@dataclass(frozen=True)
class FeedSource:
    name: str
    url: str
    kind: str = "rss"  # "rss" | "freelancer_json"


@dataclass(frozen=True)
class ScoredListing:
    listing: RawListing
    score: int
    capability: str | None
    reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "source": self.listing.source,
            "title": self.listing.title,
            "url": self.listing.url,
            "score": self.score,
            "capability": self.capability,
            "budget_min_minor": self.listing.budget_min_minor,
            "budget_max_minor": self.listing.budget_max_minor,
            "currency": self.listing.currency,
            "posted_at": self.listing.posted_at,
            "reasons": list(self.reasons),
        }


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _element_text(elem: ET.Element) -> str:
    return _WHITESPACE.sub(" ", "".join(elem.itertext())).strip()


def _clean_text(value: str) -> str:
    return _WHITESPACE.sub(" ", html.unescape(_HTML_TAG.sub(" ", value or ""))).strip()[:2000]


def _to_minor(raw: str) -> int | None:
    try:
        return int((Decimal(raw.replace(",", "")) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        return None


def parse_budget(text: str) -> tuple[int | None, int | None, str]:
    """Extracts currency-prefixed amounts, e.g. 'Budget: ₹1,500-2,000' -> (150000, 200000, 'INR')."""
    amounts: list[int] = []
    currency = ""
    for match in _AMOUNT_RE.finditer(text or ""):
        code = match.group("code")
        symbol = match.group("sym")
        raw = match.group("amt2") if code else match.group("amt1")
        if code and not currency:
            currency = code.upper()
        elif symbol and not currency:
            currency = _CURRENCY_SYMBOLS[symbol]
        minor = _to_minor(raw)
        if minor is not None:
            amounts.append(minor)
        if len(amounts) >= 2:
            break
    if not amounts:
        return None, None, currency
    return min(amounts), max(amounts), currency


def _iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_rss(xml_text: str, source: str) -> list[RawListing]:
    """Parses RSS 2.0 or Atom. Namespace-insensitive, so any feed flavour works."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"{source}: not valid XML ({exc})") from exc

    listings: list[RawListing] = []
    for elem in root.iter():
        if _local(elem.tag) not in ("item", "entry"):
            continue
        fields: dict[str, str] = {}
        for child in elem:
            name = _local(child.tag)
            if name in fields:
                continue
            if name == "link":
                fields[name] = child.get("href") or _element_text(child)
            else:
                fields[name] = _element_text(child)

        title = _clean_text(fields.get("title", ""))
        if not title:
            continue
        description = _clean_text(
            fields.get("description") or fields.get("summary") or fields.get("content") or ""
        )
        link = fields.get("link", "").strip()
        external_id = fields.get("guid") or fields.get("id") or link or title
        budget_min, budget_max, currency = parse_budget(f"{title} {description}")
        listings.append(
            RawListing(
                source=source,
                external_id=_clean_text(external_id)[:200],
                title=title,
                description=description,
                url=link,
                budget_min_minor=budget_min,
                budget_max_minor=budget_max,
                currency=currency,
                posted_at=_iso(fields.get("pubDate") or fields.get("published") or fields.get("updated")),
            )
        )
    return listings


def parse_freelancer_projects(payload: str, source: str) -> list[RawListing]:
    """Parses Freelancer.com's official public projects API (`/api/projects/0.1/projects/active`)."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source}: not valid JSON ({exc})") from exc

    projects = (data.get("result") or {}).get("projects") or []
    listings: list[RawListing] = []
    for project in projects:
        title = _clean_text(project.get("title") or "")
        if not title:
            continue
        budget = project.get("budget") or {}
        currency = (
            ((project.get("currency") or {}).get("code"))
            or ((budget.get("currency") or {}).get("code"))
            or ""
        ).upper()
        budget_min = budget.get("minimum")
        budget_max = budget.get("maximum")
        seo_url = project.get("seo_url") or ""
        posted = project.get("time_submitted")
        listings.append(
            RawListing(
                source=source,
                external_id=str(project.get("id") or seo_url or title)[:200],
                title=title,
                description=_clean_text(
                    project.get("description") or project.get("preview_description") or ""
                ),
                url=(
                    f"https://www.freelancer.com/projects/{seo_url}"
                    if seo_url
                    else f"https://www.freelancer.com/projects/{project.get('id')}"
                ),
                budget_min_minor=_to_minor(str(budget_min)) if budget_min is not None else None,
                budget_max_minor=_to_minor(str(budget_max)) if budget_max is not None else None,
                currency=currency,
                posted_at=(
                    datetime.fromtimestamp(float(posted), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    if posted
                    else None
                ),
            )
        )
    return listings


def fetch_text(url: str, timeout: float = 15.0) -> str:
    """Plain GET with an honest User-Agent. No cookies, no logins, no retries on 4xx."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "SheetFlow-JobIntake/1.0 (human-approved shortlist; no auto-bidding)",
            "Accept": "application/rss+xml, application/atom+xml, application/json, text/xml, */*",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def score_listing(listing: RawListing, now: datetime | None = None) -> ScoredListing:
    """Ranks a listing on deliverability by this codebase, budget fit and freshness."""
    now = now or datetime.now(timezone.utc)
    title = listing.title.lower()
    description = listing.description.lower()
    reasons: list[str] = []

    for bad in OFF_SCOPE_KEYWORDS:
        if bad in title:
            return ScoredListing(listing, 0, None, (f"off-scope title: {bad}",))

    capability_scores: dict[str, int] = {}
    best_title_hits = 0
    for capability, words in CAPABILITY_KEYWORDS.items():
        title_hits = sum(1 for word in words if word in title)
        description_hits = sum(1 for word in words if word in description)
        if title_hits or description_hits:
            capability_scores[capability] = title_hits * 5 + min(description_hits, 2)
            best_title_hits = max(best_title_hits, title_hits)

    capability = (
        max(capability_scores, key=lambda key: capability_scores[key]) if capability_scores else None
    )
    if capability:
        score = min(sum(capability_scores.values()), 15)
        reasons.append(f"deliverable: {capability}")
        if not best_title_hits:
            # The keyword only appears in the body, which usually means the job is about
            # something else that happens to mention invoices.
            score -= 2
            reasons.append("capability only mentioned in description")
    else:
        score = 0
        reasons.append("no capability match")

    for bad in OFF_SCOPE_KEYWORDS:
        if bad in description:
            score -= 4
            reasons.append(f"off-scope mention: {bad}")
            break

    floor = MIN_BUDGET_MAJOR.get(listing.currency.upper())
    budget_cap = listing.budget_max_minor or listing.budget_min_minor
    if budget_cap and floor:
        if budget_cap < floor * 100:
            score -= 6
            reasons.append(f"below budget floor ({listing.currency} {floor})")
        else:
            score += 2
            reasons.append("budget acceptable")
    elif not budget_cap:
        reasons.append("budget not stated")

    if listing.posted_at:
        try:
            posted = datetime.strptime(listing.posted_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            age_hours = (now - posted).total_seconds() / 3600
            if age_hours <= 24:
                score += 4
                reasons.append("posted <24h ago")
            elif age_hours <= 72:
                score += 2
                reasons.append("posted <3d ago")
            elif age_hours <= 168:
                score += 1
                reasons.append("posted this week")
            else:
                reasons.append("stale listing")
        except ValueError:
            pass

    return ScoredListing(listing, max(0, min(score, 25)), capability, tuple(reasons))


def sources_from_env(env: dict[str, str] | None = None) -> list[FeedSource]:
    """SHEETFLOW_JOB_FEEDS (RSS URLs) and SHEETFLOW_FREELANCER_QUERIES (search terms)."""
    env = os.environ if env is None else env
    sources: list[FeedSource] = []
    seen: set[str] = set()

    feeds = [item.strip() for item in (env.get("SHEETFLOW_JOB_FEEDS") or "").split(",") if item.strip()]
    for url in feeds:
        if url in seen:
            continue
        seen.add(url)
        host = urllib.parse.urlsplit(url).netloc or "feed"
        sources.append(FeedSource(name=f"rss:{host}", url=url, kind="rss"))

    queries = [
        item.strip()
        for item in (env.get("SHEETFLOW_FREELANCER_QUERIES") or "").split(",")
        if item.strip()
    ]
    for query in queries:
        url = FREELANCER_SEARCH_URL.format(query=urllib.parse.quote(query))
        if url in seen:
            continue
        seen.add(url)
        sources.append(FeedSource(name=f"freelancer:{query}", url=url, kind="freelancer_json"))

    if not sources:
        sources = [
            FeedSource(
                name=f"freelancer:{query}",
                url=FREELANCER_SEARCH_URL.format(query=urllib.parse.quote(query)),
                kind="freelancer_json",
            )
            for query in DEFAULT_FREELANCER_QUERIES
        ]
    return sources


def scan(
    sources: Sequence[FeedSource],
    fetcher: Callable[[str], str] = fetch_text,
    now: datetime | None = None,
) -> tuple[list[ScoredListing], list[dict]]:
    """Fetches every source; one dead feed never aborts the scan."""
    leads: list[ScoredListing] = []
    errors: list[dict] = []
    for source in sources:
        try:
            payload = fetcher(source.url)
        except Exception as exc:  # noqa: BLE001 - a broken feed must not stop the scan
            errors.append({"source": source.name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        try:
            listings = (
                parse_freelancer_projects(payload, source.name)
                if source.kind == "freelancer_json"
                else parse_rss(payload, source.name)
            )
        except Exception as exc:  # noqa: BLE001
            errors.append({"source": source.name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        leads.extend(score_listing(listing, now=now) for listing in listings)

    leads.sort(key=lambda lead: (-lead.score, lead.listing.title))
    return leads, errors


def run_scan(
    db_path: str,
    sources: Sequence[FeedSource] | None = None,
    fetcher: Callable[[str], str] = fetch_text,
    now: datetime | None = None,
    limit: int = 25,
) -> dict:
    """Scans, scores, stores (deduped) and returns the shortlist. Never bids."""
    from src.database import get_db_connection, init_db
    from src.jobs.store import ensure_schema, upsert_lead

    init_db(db_path)
    selected = list(sources) if sources is not None else sources_from_env()
    leads, errors = scan(selected, fetcher=fetcher, now=now)

    saved: list[dict] = []
    created_count = 0
    with get_db_connection(db_path) as conn:
        ensure_schema(conn)
        for scored in leads:
            lid, created = upsert_lead(conn, scored)
            created_count += int(created)
            entry = scored.as_dict()
            entry["id"] = lid
            entry["created"] = created
            saved.append(entry)

    shortlist = [lead for lead in saved if lead["score"] >= RECOMMENDED_MIN_SCORE][:limit]
    return {
        "sources": [source.name for source in selected],
        "fetched": len(leads),
        "new_leads": created_count,
        "shortlist_count": len(shortlist),
        "threshold": RECOMMENDED_MIN_SCORE,
        "errors": errors,
        "shortlist": shortlist,
    }
