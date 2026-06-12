#!/usr/bin/env python3
"""
Schema Migration: Add Foreign Key Enforcement
==============================================
Recreates tables with proper FK constraints (SQLite limitation: can't ALTER to add FK).
Also enables PRAGMA foreign_keys = ON in all DB connections.

Usage:
    python3 migrate_schema.py --dry-run     # Preview changes
    python3 migrate_schema.py --commit      # Apply migration
"""

import sqlite3
import shutil
import argparse
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "Master.db"
BACKUP_DIR = Path(__file__).parent / "backups"


def backup_db():
    """Create a timestamped backup before migration."""
    BACKUP_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"Master_pre_migration_{ts}.db"
    shutil.copy2(str(DB_PATH), str(backup_path))
    print(f"  Backup created: {backup_path}")
    return backup_path


def get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def migrate_investments(conn, dry_run=True):
    """Recreate investments table with enforced FK constraints."""
    print("\n--- Migrating investments table ---")

    # Count FK violations BEFORE migration
    bad_startups = conn.execute("""
        SELECT COUNT(*) FROM investments inv
        LEFT JOIN startups s ON inv.startup_id = s.id
        WHERE inv.startup_id IS NOT NULL AND s.id IS NULL
    """).fetchone()[0]
    bad_investors = conn.execute("""
        SELECT COUNT(*) FROM investments inv
        LEFT JOIN investors i ON inv.investor_id = i.investor_id
        WHERE inv.investor_id IS NOT NULL AND i.investor_id IS NULL
    """).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM investments").fetchone()[0]

    print(f"  Total investments: {total}")
    print(f"  Bad startup refs: {bad_startups}")
    print(f"  Bad investor refs: {bad_investors}")

    if dry_run:
        print("  [DRY RUN] Would recreate table with FK constraints")
        return

    # Nullify bad references first (instead of losing rows)
    conn.execute("""
        UPDATE investments SET startup_id = NULL
        WHERE startup_id IS NOT NULL AND startup_id NOT IN (SELECT id FROM startups)
    """)
    conn.execute("""
        UPDATE investments SET investor_id = NULL
        WHERE investor_id IS NOT NULL AND investor_id NOT IN (SELECT investor_id FROM investors)
    """)

    # Recreate with FK constraints
    conn.execute("""
        CREATE TABLE IF NOT EXISTS investments_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            startup_id INTEGER,
            investor_id INTEGER,
            news_mention_id INTEGER,
            round_type TEXT,
            amount TEXT,
            currency TEXT,
            source_url TEXT,
            investment_date TEXT,
            amount_usd REAL,
            data_notes TEXT,
            FOREIGN KEY (startup_id) REFERENCES startups(id) ON DELETE SET NULL,
            FOREIGN KEY (investor_id) REFERENCES investors(investor_id) ON DELETE SET NULL,
            FOREIGN KEY (news_mention_id) REFERENCES news_mentions(id) ON DELETE SET NULL
        )
    """)

    conn.execute("""
        INSERT INTO investments_new
        SELECT id, startup_id, investor_id, news_mention_id, round_type,
               amount, currency, source_url, investment_date, amount_usd, data_notes
        FROM investments
    """)

    conn.execute("DROP TABLE investments")
    conn.execute("ALTER TABLE investments_new RENAME TO investments")
    conn.commit()
    print(f"  Done. investments table recreated with FK constraints.")


def migrate_news_mentions(conn, dry_run=True):
    """Recreate news_mentions table with enforced FK constraints."""
    print("\n--- Migrating news_mentions table ---")

    bad_startups = conn.execute("""
        SELECT COUNT(*) FROM news_mentions nm
        LEFT JOIN startups s ON nm.startup_id = s.id
        WHERE nm.startup_id IS NOT NULL AND s.id IS NULL
    """).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM news_mentions").fetchone()[0]

    print(f"  Total news_mentions: {total}")
    print(f"  Bad startup refs: {bad_startups}")

    if dry_run:
        print("  [DRY RUN] Would recreate table with FK constraints")
        return

    # Nullify bad references
    conn.execute("""
        UPDATE news_mentions SET startup_id = NULL
        WHERE startup_id IS NOT NULL AND startup_id NOT IN (SELECT id FROM startups)
    """)
    conn.execute("""
        UPDATE news_mentions SET investor_id = NULL
        WHERE investor_id IS NOT NULL AND investor_id NOT IN (SELECT investor_id FROM investors)
    """)

    # Recreate
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_mentions_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            startup_id INTEGER,
            investor_id INTEGER,
            headline TEXT,
            url_hash TEXT UNIQUE,
            source_url TEXT,
            source_name TEXT,
            source_language TEXT,
            published_date TEXT,
            summary TEXT,
            event_type TEXT,
            sentiment TEXT,
            scanned_at TEXT,
            event_category TEXT,
            event_subtype TEXT,
            ai_relevance INTEGER,
            ai_snippet TEXT,
            is_relevant INTEGER DEFAULT 1,
            FOREIGN KEY (startup_id) REFERENCES startups(id) ON DELETE SET NULL,
            FOREIGN KEY (investor_id) REFERENCES investors(investor_id) ON DELETE SET NULL
        )
    """)

    conn.execute("""
        INSERT INTO news_mentions_new
        SELECT id, startup_id, investor_id, headline, url_hash, source_url,
               source_name, source_language, published_date, summary, event_type,
               sentiment, scanned_at, event_category, event_subtype, ai_relevance,
               ai_snippet, is_relevant
        FROM news_mentions
    """)

    conn.execute("DROP TABLE news_mentions")
    conn.execute("ALTER TABLE news_mentions_new RENAME TO news_mentions")
    conn.commit()
    print(f"  Done. news_mentions table recreated with FK constraints.")


def enable_fk_pragma(conn):
    """Enable FK enforcement and verify it's on."""
    conn.execute("PRAGMA foreign_keys = ON")
    result = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    print(f"\n  PRAGMA foreign_keys = {result}")


def create_quarantine_table(conn, dry_run=True):
    """Task 3.3: Create quarantine table for rejected data."""
    print("\n--- Creating quarantine table ---")
    if dry_run:
        print("  [DRY RUN] Would create quarantine table")
        return

    conn.execute("""
        CREATE TABLE IF NOT EXISTS quarantine (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            raw_data TEXT,
            rejection_reason TEXT,
            source_module TEXT,
            created_at TEXT NOT NULL,
            reviewed INTEGER DEFAULT 0,
            reviewer_action TEXT
        )
    """)
    conn.commit()
    print("  quarantine table created.")


def main():
    parser = argparse.ArgumentParser(description="Schema Migration")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    dry_run = not args.commit

    if not dry_run:
        backup_db()

    conn = get_conn()

    mode = "DRY RUN" if dry_run else "COMMIT"
    print(f"\nSchema Migration — {mode}")
    print("=" * 50)

    migrate_investments(conn, dry_run)
    migrate_news_mentions(conn, dry_run)
    create_quarantine_table(conn, dry_run)

    if not dry_run:
        enable_fk_pragma(conn)

    conn.close()

    if dry_run:
        print("\nRun with --commit to apply migration.")


if __name__ == "__main__":
    main()
