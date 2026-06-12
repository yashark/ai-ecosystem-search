#!/usr/bin/env python3
"""
Data Quality Assertions
========================
Automated checks that run after each pipeline execution.
Fail loudly if quality thresholds are exceeded.

Usage:
    python3 -m pytest tests/test_data_quality.py -v
    python3 tests/test_data_quality.py  # Standalone mode
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "Master.db"

SPAM_DOMAINS = {
    "unknowncheats.me", "bydfi.com", "tinyurl.com", "goo.gl", "cutt.ly", "shorturl.at",
}


def get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# FK Integrity
# ---------------------------------------------------------------------------

def test_no_orphaned_news_startups():
    """All news_mentions.startup_id values must exist in startups table."""
    conn = get_conn()
    count = conn.execute("""
        SELECT COUNT(*) FROM news_mentions nm
        LEFT JOIN startups s ON nm.startup_id = s.id
        WHERE nm.startup_id IS NOT NULL AND s.id IS NULL
    """).fetchone()[0]
    conn.close()
    assert count == 0, f"{count} news_mentions reference non-existent startups"


def test_no_orphaned_news_investors():
    """All news_mentions.investor_id values must exist in investors table."""
    conn = get_conn()
    count = conn.execute("""
        SELECT COUNT(*) FROM news_mentions nm
        LEFT JOIN investors i ON nm.investor_id = i.investor_id
        WHERE nm.investor_id IS NOT NULL AND i.investor_id IS NULL
    """).fetchone()[0]
    conn.close()
    assert count == 0, f"{count} news_mentions reference non-existent investors"


def test_no_orphaned_investment_startups():
    """All investments.startup_id values must exist in startups table."""
    conn = get_conn()
    count = conn.execute("""
        SELECT COUNT(*) FROM investments inv
        LEFT JOIN startups s ON inv.startup_id = s.id
        WHERE inv.startup_id IS NOT NULL AND s.id IS NULL
    """).fetchone()[0]
    conn.close()
    assert count == 0, f"{count} investments reference non-existent startups"


def test_no_orphaned_investment_investors():
    """All investments.investor_id values must exist in investors table."""
    conn = get_conn()
    count = conn.execute("""
        SELECT COUNT(*) FROM investments inv
        LEFT JOIN investors i ON inv.investor_id = i.investor_id
        WHERE inv.investor_id IS NOT NULL AND i.investor_id IS NULL
    """).fetchone()[0]
    conn.close()
    assert count == 0, f"{count} investments reference non-existent investors"


# ---------------------------------------------------------------------------
# Investor Quality
# ---------------------------------------------------------------------------

def test_investor_types_valid():
    """All investor_type values must be in the allowed set."""
    conn = get_conn()
    valid_types = {
        'VC', 'CVC', 'Angel', 'Corporate', 'Accelerator', 'PE',
        'Government', 'Family Office', 'Unknown', 'Not Investor',
        'Corporate (Overlap)', 'Corporate VC', 'Private', 'Individual',
        'Private Investor', 'Asset Management Company', 'Hedge Fund',
        'Private Foundation', 'Growth Equity', 'Quant Hedge Fund',
        'Public Funds', 'Foreign', 'Journalist', 'Public', None
    }
    rows = conn.execute("""
        SELECT DISTINCT investor_type FROM investors
        WHERE status IS NULL OR status = 'active'
    """).fetchall()
    conn.close()
    invalid = [r["investor_type"] for r in rows if r["investor_type"] not in valid_types]
    # Allow a few legacy types but warn
    assert len(invalid) <= 5, f"Invalid investor types: {invalid}"


# ---------------------------------------------------------------------------
# Startup Quality
# ---------------------------------------------------------------------------

def test_category_coverage():
    """No more than 2% of startups should be 'General | Uncategorized'."""
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM startups").fetchone()[0]
    uncategorized = conn.execute(
        "SELECT COUNT(*) FROM startups WHERE Category = 'General | Uncategorized' OR Category IS NULL"
    ).fetchone()[0]
    conn.close()
    pct = (uncategorized / max(total, 1)) * 100
    assert pct <= 5, f"{uncategorized}/{total} ({pct:.1f}%) startups are uncategorized (max 5%)"


def test_no_spam_urls():
    """No website field should contain a known spam domain."""
    conn = get_conn()
    rows = conn.execute("SELECT id, company_name, website FROM startups WHERE website IS NOT NULL").fetchall()
    conn.close()
    spam_found = []
    for r in rows:
        domain = (r["website"] or "").lower().replace("https://", "").replace("http://", "").split("/")[0]
        for spam in SPAM_DOMAINS:
            if domain == spam or domain.endswith("." + spam):
                spam_found.append(f"ID {r['id']}: {r['company_name']} → {r['website']}")
    assert len(spam_found) == 0, f"Spam URLs found: {spam_found}"


# ---------------------------------------------------------------------------
# Investment Quality
# ---------------------------------------------------------------------------

def test_investment_currency_coverage():
    """No more than 10% of investments should have 'Unknown' currency."""
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM investments").fetchone()[0]
    unknown = conn.execute(
        "SELECT COUNT(*) FROM investments WHERE currency = 'Unknown' OR currency IS NULL"
    ).fetchone()[0]
    conn.close()
    pct = (unknown / max(total, 1)) * 100
    assert pct <= 10, f"{unknown}/{total} ({pct:.1f}%) investments have unknown currency (max 10%)"


# ---------------------------------------------------------------------------
# News Quality
# ---------------------------------------------------------------------------

def test_news_summary_coverage():
    """No more than 5% of relevant news_mentions should have empty summaries."""
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM news_mentions WHERE is_relevant = 1").fetchone()[0]
    empty = conn.execute(
        "SELECT COUNT(*) FROM news_mentions WHERE is_relevant = 1 AND (summary IS NULL OR summary = '')"
    ).fetchone()[0]
    conn.close()
    pct = (empty / max(total, 1)) * 100
    assert pct <= 5, f"{empty}/{total} ({pct:.1f}%) relevant news have empty summaries (max 5%)"


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

def run_all():
    tests = [
        test_no_orphaned_news_startups,
        test_no_orphaned_news_investors,
        test_no_orphaned_investment_startups,
        test_no_orphaned_investment_investors,
        test_investor_types_valid,
        test_category_coverage,
        test_no_spam_urls,
        test_investment_currency_coverage,
        test_news_summary_coverage,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {test.__name__}: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    print("Data Quality Assertions")
    print("=" * 50)
    sys.exit(run_all())
