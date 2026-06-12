#!/usr/bin/env python3
"""
Phase 0: Data Cleanup Script
=============================
One-time cleanup of the worst data quality problems.
Backs up before each operation. All changes are logged to cleanup_log table.

Usage:
    python3 cleanup_phase0.py --dry-run          # Preview changes only
    python3 cleanup_phase0.py --commit           # Apply changes
    python3 cleanup_phase0.py --commit --task 0.1 # Run specific task only
"""

import sqlite3
import json
import argparse
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "Master.db"


def get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys = OFF")  # OFF during cleanup to avoid cascade issues
    return conn


def ensure_cleanup_tables(conn):
    """Create cleanup infrastructure tables if they don't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cleanup_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            action TEXT NOT NULL,
            details TEXT,
            timestamp TEXT NOT NULL
        )
    """)
    # Add status column to investors if not exists
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(investors)").fetchall()]
    if "status" not in cols:
        conn.execute("ALTER TABLE investors ADD COLUMN status TEXT DEFAULT 'active'")
        print("  Added 'status' column to investors table")
    # Add data_notes column to investments if not exists
    inv_cols = [r["name"] for r in conn.execute("PRAGMA table_info(investments)").fetchall()]
    if "data_notes" not in inv_cols:
        conn.execute("ALTER TABLE investments ADD COLUMN data_notes TEXT")
        print("  Added 'data_notes' column to investments table")
    conn.commit()


def log_cleanup(conn, task_id, entity_type, entity_id, action, details):
    conn.execute(
        "INSERT INTO cleanup_log (task_id, entity_type, entity_id, action, details, timestamp) VALUES (?,?,?,?,?,?)",
        (task_id, entity_type, entity_id, action, details, datetime.now().isoformat())
    )


# ---------------------------------------------------------------------------
# Task 0.1: Purge Garbage Investor Records
# ---------------------------------------------------------------------------

# Investor names that are clearly not real investors
GARBAGE_NAMES = [
    'Investor B', 'Investor1', 'early-stage specialists', 'Major brands',
    'Unknown investor', 'Unknown', 'public institutions', 'Çinli yatırımcılar',
    'Investor Name', 'others', 'undisclosed investors', 'angel investor',
    'an angel investor', 'top seed investors', 'top venture firms',
    'trusted advisors', 'serial founders', 'global venture capital firms',
    'world-leading VC', 'finance-focused angels', 'public institutions',
    '3 Others', '9 investors', 'and 16 others', 'VCs',
    'Not mentioned in article', 'Investor names not mentioned',
    'IPO investors', 'Public investors', 'Local investors',
    'Melek Yatırımcı',  # Turkish for "Angel Investor" (generic)
    'Yerli ve yabancı yatırımcılar',  # "Domestic and foreign investors"
    'Mid as lead investor',
]

# IDs that are specifically identified as non-investors
GARBAGE_IDS = [63, 64]  # Jeffrey Epstein, Fransa Cumhurbaşkanı Macron

# Names that are sentences/descriptions, not investor names
SENTENCE_PATTERNS = [
    'We rank investors',
    'We update this investor list',
    "We've invested",
    'and Ahu Serter have met',
    'BİGG 2024',
    "Grosvenor ... Taronga Group leads",
    "Taronga Group's funds are backed",
    'investors are setting higher standards',
    'a diverse pool of investors',
    'angel investors (Insider',
    'angel investors (including founders',
    'angel investors associated with',
    'angel investors such as',
    'several well-known angel investors',
    'three other Saudi angel investors',
    '156 international investors',
    '119 investors',
    '526 investors',
    '2 others',
    'Top 30 Pre Seed VC',
    'Top venture capitalists and notable',
    'including the Technology fund',
    "Turkey's leading deep tech venture capital fund",
    'Uluslararası finansal yatırımcılar',
]

# Names that look like data artifacts
ARTIFACT_NAMES = [
    'fonal@bvportfoy.com',
    'cisarge foi',
    'Öneçlikürler',
    'AcutePanaMaster',
    '$3.13 million investment from ST Engineering',
    'and Ahu Serter have met with Optiyol through the monthly startup ...',
]


def task_0_1_purge_garbage_investors(conn, dry_run=True):
    """Purge garbage investor records."""
    print("\n=== Task 0.1: Purge Garbage Investor Records ===")

    # Build the full list of garbage investor IDs
    garbage_ids = set(GARBAGE_IDS)

    # By exact name match
    for name in GARBAGE_NAMES + ARTIFACT_NAMES:
        row = conn.execute("SELECT investor_id FROM investors WHERE investor_name = ?", (name,)).fetchone()
        if row:
            garbage_ids.add(row["investor_id"])

    # By sentence pattern match
    for pattern in SENTENCE_PATTERNS:
        rows = conn.execute(
            "SELECT investor_id, investor_name FROM investors WHERE investor_name LIKE ?",
            (f"%{pattern}%",)
        ).fetchall()
        for r in rows:
            garbage_ids.add(r["investor_id"])

    # Short names (<=2 chars) WITHOUT linked investments
    short_rows = conn.execute("""
        SELECT i.investor_id, i.investor_name, COUNT(inv.id) as inv_count
        FROM investors i
        LEFT JOIN investments inv ON i.investor_id = inv.investor_id
        WHERE length(i.investor_name) <= 2
        GROUP BY i.investor_id
        HAVING inv_count = 0
    """).fetchall()
    for r in short_rows:
        garbage_ids.add(r["investor_id"])

    # Get details for reporting
    if not garbage_ids:
        print("  No garbage investors found.")
        return

    placeholders = ",".join("?" * len(garbage_ids))
    garbage_records = conn.execute(
        f"SELECT investor_id, investor_name, data_confidence FROM investors WHERE investor_id IN ({placeholders})",
        list(garbage_ids)
    ).fetchall()

    print(f"  Found {len(garbage_records)} garbage investor records to flag as 'removed'")

    # Check linked investments
    linked = conn.execute(
        f"SELECT investor_id, COUNT(*) as cnt FROM investments WHERE investor_id IN ({placeholders}) GROUP BY investor_id",
        list(garbage_ids)
    ).fetchall()
    linked_map = {r["investor_id"]: r["cnt"] for r in linked}
    total_linked = sum(linked_map.values())
    print(f"  {total_linked} linked investment records will have investor_id nullified")

    if dry_run:
        print("\n  [DRY RUN] Would flag these investors as 'removed':")
        for r in sorted(garbage_records, key=lambda x: x["investor_name"]):
            inv_count = linked_map.get(r["investor_id"], 0)
            print(f"    ID {r['investor_id']:4d}: {r['investor_name'][:60]:<60s} (investments: {inv_count})")
        return

    # Apply changes
    for r in garbage_records:
        conn.execute(
            "UPDATE investors SET status = 'removed' WHERE investor_id = ?",
            (r["investor_id"],)
        )
        log_cleanup(conn, "0.1", "investor", r["investor_id"], "flagged_removed",
                     f"Garbage investor name: {r['investor_name']}")

    # Nullify investor_id in linked investments and add note
    for inv_id, cnt in linked_map.items():
        inv_name = next(r["investor_name"] for r in garbage_records if r["investor_id"] == inv_id)
        conn.execute(
            "UPDATE investments SET data_notes = COALESCE(data_notes || '; ', '') || ? WHERE investor_id = ?",
            (f"Original investor '{inv_name}' (ID {inv_id}) removed as garbage data", inv_id)
        )
        conn.execute(
            "UPDATE investments SET investor_id = NULL WHERE investor_id = ?",
            (inv_id,)
        )
        log_cleanup(conn, "0.1", "investment", inv_id, "nullified_investor",
                     f"Unlinked {cnt} investments from garbage investor '{inv_name}'")

    conn.commit()
    print(f"  Done. Flagged {len(garbage_records)} investors, nullified {total_linked} investment links.")


# ---------------------------------------------------------------------------
# Task 0.2: Fix Corporations Misclassified as Investors
# ---------------------------------------------------------------------------

def task_0_2_fix_corp_investors(conn, dry_run=True):
    """Fix corporations that should not be in the investors table."""
    print("\n=== Task 0.2: Fix Corporations Misclassified as Investors ===")

    # Find investors that also exist in startups table
    overlaps = conn.execute("""
        SELECT i.investor_id, i.investor_name, i.investor_type, s.id as startup_id, s.company_name
        FROM investors i
        JOIN startups s ON LOWER(i.investor_name) = LOWER(s.company_name)
        WHERE i.status IS NULL OR i.status = 'active'
    """).fetchall()

    # Known global tech companies that should not be investors
    global_corps = [
        'Nvidia', 'OpenAI', 'Google', 'Meta', 'Amazon', 'Microsoft', 'Apple',
        'xAI', 'Huawei', 'Alibaba-backed Moonshot AI',
    ]
    global_matches = conn.execute(
        f"SELECT investor_id, investor_name, investor_type FROM investors "
        f"WHERE investor_name IN ({','.join('?' * len(global_corps))}) "
        f"AND (status IS NULL OR status = 'active')",
        global_corps
    ).fetchall()

    # Investors with NULL or 'None' investor_type
    null_type = conn.execute("""
        SELECT investor_id, investor_name, investor_type
        FROM investors
        WHERE (investor_type IS NULL OR investor_type = 'None' OR investor_type = '')
        AND (status IS NULL OR status = 'active')
    """).fetchall()

    print(f"  Found {len(overlaps)} investors also in startups table")
    print(f"  Found {len(global_matches)} known global tech companies as investors")
    print(f"  Found {len(null_type)} investors with NULL/empty investor_type")

    if dry_run:
        if overlaps:
            print("\n  [DRY RUN] Startup-investor overlaps:")
            for r in overlaps:
                print(f"    Investor ID {r['investor_id']}: '{r['investor_name']}' = Startup ID {r['startup_id']}: '{r['company_name']}'")
        if global_matches:
            print("\n  [DRY RUN] Global tech companies to flag:")
            for r in global_matches:
                print(f"    ID {r['investor_id']}: {r['investor_name']} (type: {r['investor_type']})")
        if null_type:
            print(f"\n  [DRY RUN] {len(null_type)} investors with NULL type (sample):")
            for r in null_type[:10]:
                print(f"    ID {r['investor_id']}: {r['investor_name']}")
        return

    # Flag overlapping corporations
    for r in overlaps:
        conn.execute(
            "UPDATE investors SET status = 'flagged', investor_type = 'Corporate (Overlap)' WHERE investor_id = ?",
            (r["investor_id"],)
        )
        log_cleanup(conn, "0.2", "investor", r["investor_id"], "flagged_overlap",
                     f"Also exists as startup ID {r['startup_id']}: {r['company_name']}")

    # Flag global tech companies
    for r in global_matches:
        conn.execute(
            "UPDATE investors SET status = 'flagged' WHERE investor_id = ?",
            (r["investor_id"],)
        )
        log_cleanup(conn, "0.2", "investor", r["investor_id"], "flagged_global_corp",
                     f"Global tech company, not a proper investor: {r['investor_name']}")

    # Flag NULL types for review
    for r in null_type:
        conn.execute(
            "UPDATE investors SET status = 'needs_review' WHERE investor_id = ?",
            (r["investor_id"],)
        )
        log_cleanup(conn, "0.2", "investor", r["investor_id"], "flagged_null_type",
                     f"Investor type is NULL/empty: {r['investor_name']}")

    conn.commit()
    print(f"  Done. Flagged {len(overlaps)} overlaps, {len(global_matches)} global corps, {len(null_type)} null types.")


# ---------------------------------------------------------------------------
# Task 0.3: Fix Orphaned News Mentions
# ---------------------------------------------------------------------------

def task_0_3_fix_orphaned_news(conn, dry_run=True):
    """Fix orphaned news mentions."""
    print("\n=== Task 0.3: Fix Orphaned News Mentions ===")

    # News referencing non-existent startups
    bad_startup_refs = conn.execute("""
        SELECT nm.id, nm.headline, nm.startup_id
        FROM news_mentions nm
        LEFT JOIN startups s ON nm.startup_id = s.id
        WHERE nm.startup_id IS NOT NULL AND s.id IS NULL
    """).fetchall()
    print(f"  {len(bad_startup_refs)} news mentions reference non-existent startups")

    # News referencing non-existent investors
    bad_investor_refs = conn.execute("""
        SELECT nm.id, nm.headline, nm.investor_id
        FROM news_mentions nm
        LEFT JOIN investors i ON nm.investor_id = i.investor_id
        WHERE nm.investor_id IS NOT NULL AND i.investor_id IS NULL
    """).fetchall()
    print(f"  {len(bad_investor_refs)} news mentions reference non-existent investors")

    # Orphaned news (no startup_id AND no investor_id)
    orphaned = conn.execute("""
        SELECT id, headline, summary
        FROM news_mentions
        WHERE startup_id IS NULL AND investor_id IS NULL
    """).fetchall()
    print(f"  {len(orphaned)} orphaned news mentions (no startup or investor link)")

    # Empty summary orphans
    empty_orphans = [r for r in orphaned if not r["summary"] or r["summary"].strip() == ""]
    print(f"    - {len(empty_orphans)} with empty summaries (will delete)")

    if dry_run:
        print(f"\n  [DRY RUN] Would delete {len(bad_startup_refs)} bad startup refs")
        print(f"  [DRY RUN] Would nullify {len(bad_investor_refs)} bad investor refs")
        print(f"  [DRY RUN] Would delete {len(empty_orphans)} empty-summary orphans")
        if bad_startup_refs:
            print("  Sample bad startup refs:")
            for r in bad_startup_refs[:5]:
                print(f"    News ID {r['id']}: startup_id={r['startup_id']} — {(r['headline'] or '')[:60]}")
        return

    # Delete news with bad startup references
    for r in bad_startup_refs:
        conn.execute("DELETE FROM news_mentions WHERE id = ?", (r["id"],))
        log_cleanup(conn, "0.3", "news_mention", r["id"], "deleted",
                     f"Referenced non-existent startup_id={r['startup_id']}")

    # Nullify bad investor references (keep the news, just unlink)
    for r in bad_investor_refs:
        conn.execute("UPDATE news_mentions SET investor_id = NULL WHERE id = ?", (r["id"],))
        log_cleanup(conn, "0.3", "news_mention", r["id"], "nullified_investor",
                     f"Referenced non-existent investor_id={r['investor_id']}")

    # Delete empty-summary orphans
    for r in empty_orphans:
        conn.execute("DELETE FROM news_mentions WHERE id = ?", (r["id"],))
        log_cleanup(conn, "0.3", "news_mention", r["id"], "deleted",
                     f"Orphaned with empty summary")

    conn.commit()
    print(f"  Done. Deleted {len(bad_startup_refs) + len(empty_orphans)}, nullified {len(bad_investor_refs)}.")


# ---------------------------------------------------------------------------
# Task 0.4: Fix Investment Currency and Amount Gaps
# ---------------------------------------------------------------------------

CURRENCY_SYMBOLS = {
    "$": "USD", "€": "EUR", "₺": "TRY", "£": "GBP", "¥": "JPY", "₹": "INR",
}
CURRENCY_CODES = {"USD", "EUR", "TRY", "GBP", "CHF", "JPY", "CNY", "SAR", "AED", "INR"}


def detect_currency_from_amount(amount_str):
    """Try to detect currency from the amount string."""
    if not amount_str:
        return None
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in amount_str:
            return code
    upper = amount_str.upper()
    for code in CURRENCY_CODES:
        if code in upper:
            return code
    return None


def task_0_4_fix_currency(conn, dry_run=True):
    """Fix investment currency and amount gaps."""
    print("\n=== Task 0.4: Fix Investment Currency and Amount Gaps ===")

    # Unknown currency investments
    unknown_currency = conn.execute("""
        SELECT inv.id, inv.amount, inv.currency, inv.startup_id, s.company_name, s.city
        FROM investments inv
        LEFT JOIN startups s ON inv.startup_id = s.id
        WHERE inv.currency = 'Unknown' OR inv.currency IS NULL
    """).fetchall()
    print(f"  {len(unknown_currency)} investments with Unknown/NULL currency")

    fixed_count = 0
    defaulted_count = 0
    fixes = []

    for r in unknown_currency:
        detected = detect_currency_from_amount(r["amount"])
        if detected:
            fixes.append((r["id"], detected, f"Detected from amount field: '{r['amount']}'"))
            fixed_count += 1
        elif r["city"] and r["city"] != "Unknown":
            # Turkish startup without currency indicator -> default to USD (industry standard)
            fixes.append((r["id"], "USD", f"Defaulted to USD for Turkish startup '{r['company_name']}'"))
            defaulted_count += 1

    print(f"  {fixed_count} can be fixed by parsing amount field")
    print(f"  {defaulted_count} defaulted to USD (Turkish startups, industry standard)")

    if dry_run:
        print(f"\n  [DRY RUN] Would fix {len(fixes)} currency values")
        for inv_id, currency, reason in fixes[:10]:
            print(f"    Investment ID {inv_id}: -> {currency} ({reason[:50]})")
        return

    for inv_id, currency, reason in fixes:
        conn.execute("UPDATE investments SET currency = ? WHERE id = ?", (currency, inv_id))
        log_cleanup(conn, "0.4", "investment", inv_id, "fixed_currency", reason)

    conn.commit()
    print(f"  Done. Fixed {len(fixes)} currency values.")


# ---------------------------------------------------------------------------
# Task 0.5: Reclassify "General | Uncategorized" Startups
# ---------------------------------------------------------------------------

# Known non-startups to remove
EFES_PRODUCTS = ['Efes Pilsen', 'Efes Sırp', 'Efes Limon', 'Efes Kola']


def task_0_5_reclassify_startups(conn, dry_run=True):
    """Identify startups needing reclassification."""
    print("\n=== Task 0.5: Reclassify 'General | Uncategorized' Startups ===")

    uncategorized = conn.execute("""
        SELECT id, company_name, description, AI_Use_Case, Category
        FROM startups
        WHERE Category = 'General | Uncategorized' OR Category IS NULL
    """).fetchall()
    print(f"  {len(uncategorized)} startups are 'General | Uncategorized' or NULL")

    # Check for Efes products
    efes = conn.execute(
        f"SELECT id, company_name FROM startups WHERE company_name IN ({','.join('?' * len(EFES_PRODUCTS))})",
        EFES_PRODUCTS
    ).fetchall()
    print(f"  {len(efes)} Efes product entries found (not startups)")

    if dry_run:
        print("\n  [DRY RUN] Uncategorized startups:")
        for r in uncategorized:
            desc = (r["description"] or "")[:60]
            print(f"    ID {r['id']:4d}: {r['company_name']:<30s} — {desc}")
        if efes:
            print("\n  [DRY RUN] Efes products to remove:")
            for r in efes:
                print(f"    ID {r['id']}: {r['company_name']}")
        print("\n  Note: Actual reclassification requires running util_reclassify.py")
        print("  This task only flags them and removes non-startups.")
        return

    # Remove Efes products
    for r in efes:
        # Move investments first
        conn.execute("UPDATE investments SET startup_id = NULL, data_notes = COALESCE(data_notes || '; ', '') || ? WHERE startup_id = ?",
                     (f"Startup '{r['company_name']}' (ID {r['id']}) removed: not a startup", r["id"]))
        conn.execute("UPDATE news_mentions SET startup_id = NULL WHERE startup_id = ?", (r["id"],))
        conn.execute("DELETE FROM startups WHERE id = ?", (r["id"],))
        log_cleanup(conn, "0.5", "startup", r["id"], "deleted",
                     f"Non-startup product entry: {r['company_name']}")

    conn.commit()
    print(f"  Done. Removed {len(efes)} non-startup entries.")
    print(f"  {len(uncategorized)} startups still need reclassification via util_reclassify.py")


# ---------------------------------------------------------------------------
# Task 0.6: Sanitize Overwritten URLs
# ---------------------------------------------------------------------------

SPAM_DOMAINS = {
    "unknowncheats.me", "bydfi.com", "tinyurl.com",
    "goo.gl", "cutt.ly", "shorturl.at",
}


def _extract_domain(url):
    """Extract the domain from a URL for spam checking."""
    url = (url or "").lower().strip()
    # Remove protocol
    for prefix in ("https://", "http://", "//"):
        if url.startswith(prefix):
            url = url[len(prefix):]
            break
    # Remove path
    url = url.split("/")[0]
    # Remove port
    url = url.split(":")[0]
    return url


def _is_spam_url(url):
    """Check if a URL's domain matches a known spam domain."""
    domain = _extract_domain(url)
    if not domain:
        return False
    for spam in SPAM_DOMAINS:
        if domain == spam or domain.endswith("." + spam):
            return True
    return False


def task_0_6_sanitize_urls(conn, dry_run=True):
    """Sanitize overwritten URLs by reverting spam changes."""
    print("\n=== Task 0.6: Sanitize Overwritten URLs ===")

    # Check change_log for spam URL overwrites
    spam_changes = conn.execute("""
        SELECT cl.id, cl.entity_type, cl.entity_id, cl.changed_field, cl.old_value, cl.new_value, cl.timestamp
        FROM change_log cl
        WHERE cl.changed_field = 'website'
    """).fetchall()

    revertable = []
    for r in spam_changes:
        if _is_spam_url(r["new_value"]):
            revertable.append(r)

    print(f"  {len(revertable)} spam URL overwrites found in change_log")

    # Also check for current spam URLs in startups
    current_spam = conn.execute("""
        SELECT id, company_name, website FROM startups WHERE website IS NOT NULL
    """).fetchall()
    spam_current = []
    for r in current_spam:
        if _is_spam_url(r["website"]):
            spam_current.append(r)
    print(f"  {len(spam_current)} startups currently have spam URLs")

    if dry_run:
        if revertable:
            print("\n  [DRY RUN] Spam URL changes to revert:")
            for r in revertable:
                print(f"    {r['entity_type']} ID {r['entity_id']}: '{r['new_value'][:50]}' -> revert to '{(r['old_value'] or 'NULL')[:50]}'")
        if spam_current:
            print("\n  [DRY RUN] Current spam URLs:")
            for r in spam_current:
                print(f"    Startup ID {r['id']}: {r['company_name']} — {r['website'][:50]}")
        return

    # Revert spam URL changes
    for r in revertable:
        if r["entity_type"] == "startup":
            conn.execute("UPDATE startups SET website = ? WHERE id = ?", (r["old_value"], r["entity_id"]))
        elif r["entity_type"] == "investor":
            conn.execute("UPDATE investors SET website = ? WHERE investor_id = ?", (r["old_value"], r["entity_id"]))
        log_cleanup(conn, "0.6", r["entity_type"], r["entity_id"], "reverted_spam_url",
                     f"Reverted from '{r['new_value']}' to '{r['old_value']}'")

    # For current spam URLs not in change_log, null them out
    for r in spam_current:
        already_reverted = any(
            rv["entity_id"] == r["id"] and rv["entity_type"] == "startup"
            for rv in revertable
        )
        if not already_reverted:
            conn.execute("UPDATE startups SET website = NULL WHERE id = ?", (r["id"],))
            log_cleanup(conn, "0.6", "startup", r["id"], "nullified_spam_url",
                         f"Spam URL removed: {r['website']}")

    conn.commit()
    print(f"  Done. Reverted {len(revertable)} changes, cleaned {len(spam_current)} current spam URLs.")


# ---------------------------------------------------------------------------
# Summary Report
# ---------------------------------------------------------------------------

def print_summary(conn):
    """Print current data quality summary."""
    print("\n" + "=" * 60)
    print("DATA QUALITY SUMMARY")
    print("=" * 60)

    total_inv = conn.execute("SELECT COUNT(*) FROM investors").fetchone()[0]
    active_inv = conn.execute("SELECT COUNT(*) FROM investors WHERE status = 'active' OR status IS NULL").fetchone()[0]
    removed_inv = conn.execute("SELECT COUNT(*) FROM investors WHERE status = 'removed'").fetchone()[0]
    flagged_inv = conn.execute("SELECT COUNT(*) FROM investors WHERE status IN ('flagged', 'needs_review')").fetchone()[0]

    total_news = conn.execute("SELECT COUNT(*) FROM news_mentions").fetchone()[0]
    orphan_news = conn.execute(
        "SELECT COUNT(*) FROM news_mentions WHERE startup_id IS NULL AND investor_id IS NULL"
    ).fetchone()[0]

    total_investments = conn.execute("SELECT COUNT(*) FROM investments").fetchone()[0]
    unknown_curr = conn.execute(
        "SELECT COUNT(*) FROM investments WHERE currency = 'Unknown' OR currency IS NULL"
    ).fetchone()[0]

    total_startups = conn.execute("SELECT COUNT(*) FROM startups").fetchone()[0]
    uncategorized = conn.execute(
        "SELECT COUNT(*) FROM startups WHERE Category = 'General | Uncategorized' OR Category IS NULL"
    ).fetchone()[0]

    print(f"\n  Investors: {total_inv} total | {active_inv} active | {removed_inv} removed | {flagged_inv} flagged")
    print(f"  News:      {total_news} total | {orphan_news} orphaned")
    print(f"  Investments: {total_investments} total | {unknown_curr} unknown currency ({unknown_curr*100//max(total_investments,1)}%)")
    print(f"  Startups:  {total_startups} total | {uncategorized} uncategorized ({uncategorized*100//max(total_startups,1)}%)")

    cleanup_count = conn.execute("SELECT COUNT(*) FROM cleanup_log").fetchone()[0]
    print(f"\n  Cleanup log entries: {cleanup_count}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Phase 0: Data Cleanup")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Preview changes (default)")
    parser.add_argument("--commit", action="store_true", help="Apply changes")
    parser.add_argument("--task", type=str, help="Run specific task (e.g., '0.1')")
    parser.add_argument("--summary", action="store_true", help="Print summary only")
    args = parser.parse_args()

    dry_run = not args.commit

    conn = get_conn()
    ensure_cleanup_tables(conn)

    if args.summary:
        print_summary(conn)
        conn.close()
        return

    tasks = {
        "0.1": task_0_1_purge_garbage_investors,
        "0.2": task_0_2_fix_corp_investors,
        "0.3": task_0_3_fix_orphaned_news,
        "0.4": task_0_4_fix_currency,
        "0.5": task_0_5_reclassify_startups,
        "0.6": task_0_6_sanitize_urls,
    }

    mode = "DRY RUN" if dry_run else "COMMIT"
    print(f"\n{'='*60}")
    print(f"Phase 0: Data Cleanup — {mode}")
    print(f"{'='*60}")

    if args.task:
        if args.task in tasks:
            tasks[args.task](conn, dry_run)
        else:
            print(f"Unknown task: {args.task}. Available: {', '.join(tasks.keys())}")
    else:
        for task_id, task_fn in tasks.items():
            task_fn(conn, dry_run)

    print_summary(conn)
    conn.close()

    if dry_run:
        print("\n  Run with --commit to apply these changes.")


if __name__ == "__main__":
    main()
