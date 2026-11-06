"""Job intake: parsing, scoring, dedupe and the human approval gate."""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from src.jobs.intake import (
    FeedSource,
    RawListing,
    parse_freelancer_projects,
    parse_rss,
    run_scan,
    score_listing,
    sources_from_env,
)
from src.jobs.store import ensure_schema, list_leads, set_lead_status

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)

RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>Seeking Freelancer</title>
    <item>
      <title>Need invoices generated from my Excel billing sheet</title>
      <link>https://example.com/jobs/1</link>
      <guid>job-1</guid>
      <description>&lt;p&gt;I have a spreadsheet of 40 clients and need GST invoices as PDF with a payment link. Budget: ₹2,500&lt;/p&gt;</description>
      <pubDate>Fri, 06 Nov 2026 09:00:00 GMT</pubDate>
    </item>
    <item>
      <title>WordPress landing page redesign</title>
      <link>https://example.com/jobs/2</link>
      <guid>job-2</guid>
      <description>Need a WordPress site with Elementor.</description>
      <pubDate>Thu, 05 Nov 2026 09:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Excel data entry - quick task</title>
      <link>https://example.com/jobs/3</link>
      <guid>job-3</guid>
      <description>Copy paste 200 rows. Budget: $3</description>
      <pubDate>Thu, 15 Oct 2026 09:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

FREELANCER_FIXTURE = """
{"status":"success","result":{"projects":[
  {"id":9001,"title":"Automate invoice generation from Google Sheets",
   "description":"We bill 60 clients monthly and want PDF invoices plus reminders.",
   "seo_url":"automate-invoice-generation-from-google-sheets",
   "time_submitted":1794000000,
   "budget":{"minimum":40,"maximum":120},"currency":{"code":"USD"}},
  {"id":9002,"title":"Video editing for YouTube channel",
   "description":"Weekly 10 minute videos.","seo_url":"video-editing-youtube",
   "time_submitted":1794000000,"budget":{"minimum":30,"maximum":30},"currency":{"code":"USD"}}
]}}
"""


class TestParsing(unittest.TestCase):
    def test_parse_rss_fields_and_budget(self):
        listings = parse_rss(RSS_FIXTURE, "rss:test")
        self.assertEqual(len(listings), 3)
        first = listings[0]
        self.assertEqual(first.external_id, "job-1")
        self.assertEqual(first.url, "https://example.com/jobs/1")
        self.assertIn("GST invoices", first.description)
        self.assertNotIn("<p>", first.description)
        self.assertEqual(first.budget_max_minor, 250000)
        self.assertEqual(first.currency, "INR")
        self.assertEqual(first.posted_at, "2026-11-06T09:00:00Z")

    def test_parse_freelancer_api(self):
        listings = parse_freelancer_projects(FREELANCER_FIXTURE, "freelancer:invoice")
        self.assertEqual(len(listings), 2)
        job = listings[0]
        self.assertEqual(job.external_id, "9001")
        self.assertEqual(job.currency, "USD")
        self.assertEqual(job.budget_min_minor, 4000)
        self.assertEqual(job.budget_max_minor, 12000)
        self.assertTrue(job.url.startswith("https://www.freelancer.com/projects/"))
        self.assertIsNotNone(job.posted_at)

    def test_sources_from_env_builds_both_kinds(self):
        sources = sources_from_env(
            {
                "SHEETFLOW_JOB_FEEDS": "https://hnrss.org/freelance,https://example.com/feed",
                "SHEETFLOW_FREELANCER_QUERIES": "invoice automation",
            }
        )
        self.assertEqual([s.kind for s in sources], ["rss", "rss", "freelancer_json"])
        self.assertIn("query=invoice%20automation", sources[2].url)


class TestScoring(unittest.TestCase):
    def test_invoice_job_scores_high(self):
        lead = score_listing(parse_rss(RSS_FIXTURE, "rss:test")[0], now=NOW)
        self.assertEqual(lead.capability, "INVOICE_AUTOMATION")
        self.assertGreaterEqual(lead.score, 10)
        self.assertIn("posted <24h ago", lead.reasons)

    def test_off_scope_title_scores_zero(self):
        lead = score_listing(parse_rss(RSS_FIXTURE, "rss:test")[1], now=NOW)
        self.assertEqual(lead.score, 0)
        self.assertIsNone(lead.capability)

    def test_low_budget_and_stale_listing_penalised(self):
        lead = score_listing(parse_rss(RSS_FIXTURE, "rss:test")[2], now=NOW)
        self.assertEqual(lead.capability, "DATA_ENTRY")
        self.assertIn("below budget floor (USD 10)", lead.reasons)
        self.assertIn("stale listing", lead.reasons)
        self.assertLess(lead.score, 6)

    def test_budget_without_currency_is_neutral(self):
        lead = score_listing(
            RawListing(
                source="rss:x",
                external_id="1",
                title="Spreadsheet cleanup",
                description="Please clean up my Excel workbook.",
                url="https://x/1",
            ),
            now=NOW,
        )
        self.assertNotIn("budget acceptable", lead.reasons)
        self.assertIn("budget not stated", lead.reasons)


class TestScanAndStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "jobs.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_survives_dead_feed_and_dedupes(self):
        def fetcher(url):
            if "broken" in url:
                raise OSError("connection refused")
            return RSS_FIXTURE

        sources = [
            FeedSource(name="rss:good", url="https://good.example/feed"),
            FeedSource(name="rss:broken", url="https://broken.example/feed"),
        ]
        first = run_scan(self.db, sources=sources, fetcher=fetcher, now=NOW)
        self.assertEqual(first["fetched"], 3)
        self.assertEqual(first["new_leads"], 3)
        self.assertEqual(len(first["errors"]), 1)
        self.assertIn("connection refused", first["errors"][0]["error"])
        self.assertEqual(first["shortlist_count"], 1)
        self.assertEqual(first["shortlist"][0]["capability"], "INVOICE_AUTOMATION")

        second = run_scan(self.db, sources=sources, fetcher=fetcher, now=NOW)
        self.assertEqual(second["new_leads"], 0)

        conn = sqlite3.connect(self.db)
        try:
            ensure_schema(conn)
            leads = list_leads(conn, status="NEW")
            self.assertEqual(len(leads), 3)
            lid = leads[0]["id"]
            self.assertTrue(set_lead_status(conn, lid, "APPROVED"))
            self.assertEqual(len(list_leads(conn, status="APPROVED")), 1)
            self.assertEqual(len(list_leads(conn, status="NEW")), 2)
            with self.assertRaises(ValueError):
                set_lead_status(conn, lid, "APPLIED")
            self.assertFalse(set_lead_status(conn, "missing", "APPROVED"))
        finally:
            conn.close()

    def test_same_project_from_two_queries_is_one_lead(self):
        def fetcher(_url):
            return FREELANCER_FIXTURE

        sources = [
            FeedSource(name="freelancer:invoice", url="https://api.example/1", kind="freelancer_json"),
            FeedSource(name="freelancer:excel", url="https://api.example/2", kind="freelancer_json"),
        ]
        result = run_scan(self.db, sources=sources, fetcher=fetcher, now=NOW)
        self.assertEqual(result["fetched"], 4)
        self.assertEqual(result["new_leads"], 2)


if __name__ == "__main__":
    unittest.main()
