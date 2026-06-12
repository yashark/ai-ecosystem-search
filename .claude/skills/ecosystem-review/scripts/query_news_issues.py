"""Find news mentions with quality issues — empty summaries, orphaned, gray-zone relevance."""
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[4] / "Master.db"

def main():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row

    print("=" * 70)
    print("  NEWS QUALITY AUDIT")
    print("=" * 70)

    total = conn.execute("SELECT COUNT(*) FROM news_mentions").fetchone()[0]
    print(f"\nTotal news mentions: {total}")

    # 1. Empty summaries
    empty = conn.execute(
        "SELECT id, headline, source_url, published_date, ai_relevance "
        "FROM news_mentions WHERE (summary IS NULL OR summary = '') AND is_relevant = 1 "
        "ORDER BY published_date DESC LIMIT 20"
    ).fetchall()
    empty_count = conn.execute(
        "SELECT COUNT(*) FROM news_mentions WHERE (summary IS NULL OR summary = '') AND is_relevant = 1"
    ).fetchone()[0]
    print(f"\n--- Empty Summaries (showing 20 of {empty_count}) ---")
    for n in empty:
        print(f"  [{n['id']}] {(n['headline'] or 'No headline')[:80]}")
        print(f"       Date: {n['published_date']}  |  Relevance: {n['ai_relevance']}")
        print()

    # 2. Orphaned (no startup or investor link)
    orphaned_count = conn.execute(
        "SELECT COUNT(*) FROM news_mentions WHERE startup_id IS NULL AND investor_id IS NULL"
    ).fetchone()[0]
    orphaned = conn.execute(
        "SELECT id, headline, summary, published_date, ai_relevance "
        "FROM news_mentions WHERE startup_id IS NULL AND investor_id IS NULL "
        "AND is_relevant = 1 ORDER BY published_date DESC LIMIT 15"
    ).fetchall()
    print(f"\n--- Orphaned News — no entity link (showing 15 of {orphaned_count}) ---")
    for n in orphaned:
        print(f"  [{n['id']}] {(n['headline'] or 'No headline')[:80]}")
        print(f"       Summary: {(n['summary'] or '')[:100]}")
        print(f"       Relevance: {n['ai_relevance']}")
        print()

    # 3. Gray-zone relevance (30-70)
    gray = conn.execute(
        "SELECT id, headline, summary, ai_relevance "
        "FROM news_mentions WHERE ai_relevance BETWEEN 30 AND 70 AND is_relevant = 1 "
        "ORDER BY ai_relevance ASC LIMIT 15"
    ).fetchall()
    gray_count = conn.execute(
        "SELECT COUNT(*) FROM news_mentions WHERE ai_relevance BETWEEN 30 AND 70 AND is_relevant = 1"
    ).fetchone()[0]
    print(f"\n--- Gray-Zone Relevance 30-70 (showing 15 of {gray_count}) ---")
    for n in gray:
        print(f"  [{n['id']}] (relevance={n['ai_relevance']}) {(n['headline'] or '')[:80]}")
        print(f"       {(n['summary'] or '')[:100]}")
        print()

    # 4. Missing dates
    no_date = conn.execute(
        "SELECT COUNT(*) FROM news_mentions WHERE published_date IS NULL OR published_date = ''"
    ).fetchone()[0]
    print(f"\n--- Missing published_date: {no_date} items ---")

    # 5. Recent scan summary
    recent = conn.execute(
        "SELECT source_name, COUNT(*) as cnt, "
        "SUM(CASE WHEN summary IS NULL OR summary = '' THEN 1 ELSE 0 END) as empty_summaries "
        "FROM news_mentions WHERE scanned_at >= date('now', '-14 days') "
        "GROUP BY source_name ORDER BY cnt DESC LIMIT 10"
    ).fetchall()
    print(f"\n--- Source Quality (last 14 days) ---")
    for r in recent:
        print(f"  {r['source_name'] or 'Unknown':<30s}  total={r['cnt']:>4d}  empty_summaries={r['empty_summaries']:>3d}")

    conn.close()

if __name__ == "__main__":
    main()
