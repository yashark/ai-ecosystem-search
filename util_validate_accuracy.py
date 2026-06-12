"""
Database Accuracy Validator
Run before and after remediation to measure data quality improvements.
Usage: python3 util_validate_accuracy.py
"""
import sqlite3
from agent_core import (
    validate_classification_accuracy, seed_tag_keywords, _TAG_SEED, VALID_MATRIX,
    get_db_connection,
)

DB_NAME = "Master.db"

def parse_category(category_value):
    if not category_value or str(category_value).strip().lower() in ["", "null", "none", "unknown"]:
        return None, None
    parts = str(category_value).split(" | ", 1)
    if len(parts) == 2:
        sector = parts[0].strip() or None
        tag = parts[1].strip() or None
        return sector, tag
    return None, None


def report_tag_accuracy(conn):
    """For each tag, calculate what % of startups have keyword evidence in their description."""
    cursor = conn.cursor()
    print("\n=== Tag Classification Accuracy ===")
    print(f"{'Tag':<30} | {'Count':>5} | {'Keyword Match %':>15} | Status")
    print("-" * 80)

    rows = cursor.execute("""
        SELECT Category, COUNT(*) as cnt
        FROM startups WHERE Status='Active'
        GROUP BY Category ORDER BY cnt DESC
    """).fetchall()

    for category, count in rows:
        _, tag = parse_category(category)
        if not tag:
            continue

        # Score each startup's description against its tag
        startups = cursor.execute(
            "SELECT description FROM startups WHERE Status='Active' AND Category=?", (category,)
        ).fetchall()
        match_count = 0
        for (desc,) in startups:
            score = validate_classification_accuracy(conn, tag, desc or "")
            if score >= 0.15:
                match_count += 1
        pct = (match_count / count * 100) if count > 0 else 0
        status = "✓ OK" if pct >= 50 else ("⚠ LOW" if pct >= 25 else "✗ CRITICAL")
        print(f"{tag:<30} | {count:>5} | {pct:>14.1f}% | {status}")

    print()


def report_use_case_uniqueness(conn):
    """Report AI_Use_Case duplication."""
    cursor = conn.cursor()
    print("=== AI_Use_Case Uniqueness ===")
    dupes = cursor.execute("""
        SELECT AI_Use_Case, COUNT(*) as cnt
        FROM startups WHERE Status='Active'
        GROUP BY AI_Use_Case HAVING cnt >= 3
        ORDER BY cnt DESC
    """).fetchall()

    total_affected = sum(cnt for _, cnt in dupes)
    print(f"Duplicated use cases (≥3 copies): {len(dupes)} groups affecting {total_affected} startups")
    if dupes:
        print(f"\nTop 10 duplicate AI_Use_Case strings:")
        for use_case, cnt in dupes[:10]:
            print(f"  [{cnt:>3}x] {use_case}")
    print()


def report_word_count_violation(conn):
    """Check AI_Use_Case for >5 words."""
    cursor = conn.cursor()
    over = cursor.execute("""
        SELECT COUNT(*) FROM startups
        WHERE Status='Active' AND LENGTH(AI_Use_Case) - LENGTH(REPLACE(AI_Use_Case, ' ', '')) >= 5
    """).fetchone()[0]
    total = cursor.execute("SELECT COUNT(*) FROM startups WHERE Status='Active'").fetchone()[0]
    print(f"=== AI_Use_Case Word Count ===")
    print(f"Over 5 words: {over} / {total} ({over/total*100:.1f}%)")
    print()


def report_missing_fields(conn):
    """Report missing core fields."""
    cursor = conn.cursor()
    print("=== Missing Core Fields (Active Startups) ===")
    total = cursor.execute("SELECT COUNT(*) FROM startups WHERE Status='Active'").fetchone()[0]

    checks = [
        ('website', "website IS NULL OR website='' OR website='Unknown' OR website='nan'"),
        ('description', "description IS NULL OR description='' OR description='Unknown'"),
        ('city', "city IS NULL OR city='' OR city='Unknown'"),
        ('founders', "founders IS NULL OR founders='' OR founders='Unknown'"),
        ('Category', "Category IS NULL OR Category='' OR Category='Unknown'"),
        ('AI_Use_Case', "AI_Use_Case IS NULL OR AI_Use_Case='' OR AI_Use_Case='Unknown' OR AI_Use_Case='None'"),
        ('Total_Funding', "Total_Funding_Formatted IS NULL OR Total_Funding_Formatted='' OR Total_Funding_Formatted='Unknown'"),
        ('Investors', "Investors IS NULL OR Investors='' OR Investors='Unknown'"),
        ('Tech_Mentioned', "Tech_Mentioned IS NULL OR Tech_Mentioned='' OR Tech_Mentioned='Unknown'"),
        ('business_model', "business_model IS NULL OR business_model='Unknown'"),
    ]

    for field, condition in checks:
        cnt = cursor.execute(f"SELECT COUNT(*) FROM startups WHERE Status='Active' AND ({condition})").fetchone()[0]
        pct = cnt / total * 100
        status = "✓" if pct < 5 else ("⚠" if pct < 30 else "✗")
        print(f"  {status} {field:<20} {cnt:>5} / {total}  ({pct:.1f}%)")
    print()


def report_confidence_distribution(conn):
    """Print confidence histogram."""
    cursor = conn.cursor()
    print("=== Confidence Distribution (Active Startups) ===")
    bands = cursor.execute("""
        SELECT
          CASE
            WHEN data_confidence BETWEEN 0 AND 20 THEN '0-20'
            WHEN data_confidence BETWEEN 21 AND 40 THEN '21-40'
            WHEN data_confidence BETWEEN 41 AND 60 THEN '41-60'
            WHEN data_confidence BETWEEN 61 AND 80 THEN '61-80'
            WHEN data_confidence BETWEEN 81 AND 100 THEN '81-100'
            ELSE 'NULL'
          END as band, COUNT(*)
        FROM startups WHERE Status='Active'
        GROUP BY band ORDER BY band
    """).fetchall()
    for band, cnt in bands:
        bar = "█" * (cnt // 10)
        print(f"  {band:>6}  {cnt:>5}  {bar}")
    print()


def report_invalid_pairs(conn):
    """Check for sector/tag pairs not in VALID_MATRIX."""
    cursor = conn.cursor()
    print("=== Invalid Sector/Tag Pairs ===")
    rows = cursor.execute("""
        SELECT Category, COUNT(*) as cnt
        FROM startups WHERE Status='Active'
        GROUP BY Category
    """).fetchall()

    invalid = []
    for category, cnt in rows:
        sector, tag = parse_category(category)
        if not sector or not tag or (sector, tag) not in VALID_MATRIX:
            invalid.append((sector, tag, cnt))
    if invalid:
        for s, t, c in invalid:
            print(f"  ✗ ({s}, {t}) — {c} startups")
    else:
        print("  ✓ All pairs are valid.")
    print()


def report_keyword_index(conn):
    """Report self-improving keyword index status."""
    cursor = conn.cursor()
    print("=== Self-Improving Keyword Index ===")
    total = cursor.execute("SELECT COUNT(*) FROM tag_keywords").fetchone()[0]
    seed = cursor.execute("SELECT COUNT(*) FROM tag_keywords WHERE source='seed'").fetchone()[0]
    learned = cursor.execute("SELECT COUNT(*) FROM tag_keywords WHERE source='learned'").fetchone()[0]
    print(f"  Total keywords: {total} (seed: {seed}, learned: {learned})")

    top_tags = cursor.execute("""
        SELECT tag_category, COUNT(*) as cnt, ROUND(AVG(weight), 1) as avg_w
        FROM tag_keywords GROUP BY tag_category ORDER BY cnt DESC LIMIT 5
    """).fetchall()
    if top_tags:
        print(f"  Top tags by keyword count:")
        for tag, cnt, avg_w in top_tags:
            print(f"    {tag:<30} {cnt:>4} keywords (avg weight: {avg_w})")
    print()


def report_investor_quality(conn):
    """Report investor data quality."""
    cursor = conn.cursor()
    print("=== Investor Data Quality ===")
    total = cursor.execute("SELECT COUNT(*) FROM investors").fetchone()[0]
    conf0 = cursor.execute("SELECT COUNT(*) FROM investors WHERE data_confidence IS NULL OR data_confidence=0").fetchone()[0]
    no_type = cursor.execute("SELECT COUNT(*) FROM investors WHERE investor_type IS NULL OR investor_type='' OR investor_type='Unknown'").fetchone()[0]
    print(f"  Total investors: {total}")
    print(f"  Confidence = 0: {conf0} ({conf0/total*100:.1f}%)")
    print(f"  Missing type:   {no_type} ({no_type/total*100:.1f}%)")
    print()


def main():
    conn = get_db_connection(DB_NAME)
    seed_tag_keywords(conn)  # Ensure seed data exists

    print("=" * 60)
    print("  DATABASE ACCURACY REPORT")
    print("=" * 60)

    report_tag_accuracy(conn)
    report_use_case_uniqueness(conn)
    report_word_count_violation(conn)
    report_missing_fields(conn)
    report_confidence_distribution(conn)
    report_invalid_pairs(conn)
    report_keyword_index(conn)
    report_investor_quality(conn)

    conn.close()


if __name__ == "__main__":
    main()
