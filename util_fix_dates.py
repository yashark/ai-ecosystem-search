"""
One-time repair: Fix investment dates that are scan timestamps, not deal dates.

3-tier correction methodology:
1. Recover from linked news_mentions (article publication date)
2. Recover from ecosystem_activities (cross-reference by startup+investor)
3. NULL the rest (honest unknown is better than fake date)
"""
import sqlite3
import sys

DB_NAME = "Master.db"
DRY_RUN = "--commit" not in sys.argv


def main():
    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA journal_mode=WAL")
    cursor = conn.cursor()

    print("=" * 60)
    print(f"  Investment Date Repair {'(DRY RUN)' if DRY_RUN else '(COMMIT)'}")
    print("=" * 60)

    # --- Baseline ---
    total = cursor.execute("SELECT COUNT(*) FROM investments").fetchone()[0]
    scan_dates = cursor.execute("SELECT COUNT(*) FROM investments WHERE investment_date LIKE '2026-%T%'").fetchone()[0]
    unknown_text = cursor.execute("SELECT COUNT(*) FROM investments WHERE investment_date IN ('Unknown', 'December (last year)', '')").fetchone()[0]
    placeholder = cursor.execute("SELECT COUNT(*) FROM investments WHERE investment_date = '2024-01-01'").fetchone()[0]
    null_dates = cursor.execute("SELECT COUNT(*) FROM investments WHERE investment_date IS NULL").fetchone()[0]
    valid_dates = total - scan_dates - unknown_text - placeholder - null_dates

    print(f"\n  Total investments:     {total}")
    print(f"  Scan timestamps:       {scan_dates} (2026-XX ISO timestamps)")
    print(f"  'Unknown' text:        {unknown_text}")
    print(f"  '2024-01-01' placeholder: {placeholder}")
    print(f"  NULL dates:            {null_dates}")
    print(f"  Valid dates:           {valid_dates}")
    print()

    # --- Tier 1: Recover from linked news_mentions ---
    print("--- Tier 1: Recover from news_mentions published_date ---")
    tier1_sql = """
        UPDATE investments SET investment_date = (
            SELECT n.published_date FROM news_mentions n
            WHERE n.id = investments.news_mention_id
            AND n.published_date IS NOT NULL AND n.published_date != ''
            AND n.published_date != 'Unknown'
        )
        WHERE (
            investment_date LIKE '2026-%T%'
            OR investment_date IN ('Unknown', 'December (last year)', '')
            OR investment_date = '2024-01-01'
        )
        AND news_mention_id IS NOT NULL
        AND EXISTS (
            SELECT 1 FROM news_mentions n
            WHERE n.id = investments.news_mention_id
            AND n.published_date IS NOT NULL AND n.published_date != ''
            AND n.published_date != 'Unknown'
        )
    """
    if not DRY_RUN:
        cursor.execute(tier1_sql)
        tier1_fixed = cursor.rowcount
    else:
        # Count how many would be fixed
        tier1_fixed = cursor.execute("""
            SELECT COUNT(*) FROM investments
            WHERE (
                investment_date LIKE '2026-%T%'
                OR investment_date IN ('Unknown', 'December (last year)', '')
                OR investment_date = '2024-01-01'
            )
            AND news_mention_id IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM news_mentions n
                WHERE n.id = investments.news_mention_id
                AND n.published_date IS NOT NULL AND n.published_date != ''
                AND n.published_date != 'Unknown'
            )
        """).fetchone()[0]
    print(f"  Tier 1 recovered: {tier1_fixed}")

    # --- Tier 2: Recover from ecosystem_activities ---
    print("--- Tier 2: Recover from ecosystem_activities ---")
    tier2_sql = """
        UPDATE investments SET investment_date = (
            SELECT ea.activity_date FROM ecosystem_activities ea
            WHERE ea.startup_id = investments.startup_id
            AND ea.investor_id = investments.investor_id
            AND ea.activity_date IS NOT NULL
            AND ea.activity_date NOT LIKE '2026-%T%'
            AND ea.activity_date != 'Unknown'
            AND LENGTH(ea.activity_date) >= 4
            LIMIT 1
        )
        WHERE (
            investment_date LIKE '2026-%T%'
            OR investment_date IN ('Unknown', 'December (last year)', '')
            OR investment_date = '2024-01-01'
        )
        AND EXISTS (
            SELECT 1 FROM ecosystem_activities ea
            WHERE ea.startup_id = investments.startup_id
            AND ea.investor_id = investments.investor_id
            AND ea.activity_date IS NOT NULL
            AND ea.activity_date NOT LIKE '2026-%T%'
            AND ea.activity_date != 'Unknown'
            AND LENGTH(ea.activity_date) >= 4
        )
    """
    if not DRY_RUN:
        cursor.execute(tier2_sql)
        tier2_fixed = cursor.rowcount
    else:
        tier2_fixed = cursor.execute("""
            SELECT COUNT(*) FROM investments
            WHERE (
                investment_date LIKE '2026-%T%'
                OR investment_date IN ('Unknown', 'December (last year)', '')
                OR investment_date = '2024-01-01'
            )
            AND EXISTS (
                SELECT 1 FROM ecosystem_activities ea
                WHERE ea.startup_id = investments.startup_id
                AND ea.investor_id = investments.investor_id
                AND ea.activity_date IS NOT NULL
                AND ea.activity_date NOT LIKE '2026-%T%'
                AND ea.activity_date != 'Unknown'
                AND LENGTH(ea.activity_date) >= 4
            )
        """).fetchone()[0]
    print(f"  Tier 2 recovered: {tier2_fixed}")

    # --- Tier 3: NULL out remaining bad dates ---
    print("--- Tier 3: NULL remaining bad dates ---")
    tier3_targets = cursor.execute("""
        SELECT COUNT(*) FROM investments
        WHERE investment_date LIKE '2026-%T%'
           OR investment_date IN ('Unknown', 'December (last year)', '')
           OR investment_date = '2024-01-01'
    """).fetchone()[0]
    if not DRY_RUN:
        cursor.execute("""
            UPDATE investments SET investment_date = NULL
            WHERE investment_date LIKE '2026-%T%'
               OR investment_date IN ('Unknown', 'December (last year)', '')
               OR investment_date = '2024-01-01'
        """)
    print(f"  Tier 3 NULLed: {tier3_targets}")

    # --- Fix ecosystem_activities scan timestamps ---
    print("--- Fix ecosystem_activities scan timestamps ---")
    ea_scan = cursor.execute("SELECT COUNT(*) FROM ecosystem_activities WHERE activity_date LIKE '2026-%T%'").fetchone()[0]
    if not DRY_RUN:
        cursor.execute("UPDATE ecosystem_activities SET activity_date = NULL WHERE activity_date LIKE '2026-%T%'")
    print(f"  Ecosystem activities NULLed: {ea_scan}")

    # --- Summary ---
    print(f"\n{'=' * 60}")
    print(f"  SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Tier 1 (news_mentions):  {tier1_fixed} recovered")
    print(f"  Tier 2 (activities):     {tier2_fixed} recovered")
    print(f"  Tier 3 (NULLed):         {tier3_targets} cleared")
    print(f"  Ecosystem activities:    {ea_scan} cleared")

    if DRY_RUN:
        print(f"\n  DRY RUN — no changes written. Use --commit to apply.")
    else:
        conn.commit()
        print(f"\n  Changes committed to {DB_NAME}")

    conn.close()


if __name__ == "__main__":
    main()
