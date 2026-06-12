"""
One-time repair: Fix funding anomalies and normalize amount formats.

1. NULL bogus billion-dollar entries in startups
2. NULL narrative text entries
3. Normalize all Total_Funding_Formatted to consistent format
4. Fix investments with amount_usd > $500M
"""
import sqlite3
import sys
import re

DB_NAME = "Master.db"
DRY_RUN = "--commit" not in sys.argv

# Import normalize_funding_amount from agent_core
sys.path.insert(0, '.')
from agent_core import normalize_funding_amount


# Patterns that indicate bogus data (>$1B for Turkish startups)
BOGUS_AMOUNT_PATTERNS = [
    "50.000.000.000",
    "40.000.000.000",
    "10.000.000.000",
    "5.000.000.000",
    "2.000.000.000",
    "1.000.000.000",
]

# Narrative text patterns that aren't real amounts
NARRATIVE_PATTERNS = [
    r'^Over ',
    r'^Approximately ',
    r'^About ',
    r'^Undisclosed',
    r'^\d+ separate deals',
    r'^\d+ funding rounds',
    r'across \d+ ',
    r'in funding across',
]


def main():
    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA journal_mode=WAL")
    cursor = conn.cursor()

    print("=" * 60)
    print(f"  Funding Repair {'(DRY RUN)' if DRY_RUN else '(COMMIT)'}")
    print("=" * 60)

    # --- Pass 1: NULL bogus billion-dollar entries ---
    print("\n--- Pass 1: NULL bogus billion-dollar entries ---")
    bogus_count = 0
    for pattern in BOGUS_AMOUNT_PATTERNS:
        rows = cursor.execute(
            "SELECT id, company_name, Total_Funding_Formatted FROM startups WHERE Total_Funding_Formatted LIKE ?",
            (f"%{pattern}%",)
        ).fetchall()
        for rid, name, funding in rows:
            print(f"  [NULL] {name}: {funding}")
            bogus_count += 1
            if not DRY_RUN:
                cursor.execute("UPDATE startups SET Total_Funding_Formatted = NULL WHERE id = ?", (rid,))
    print(f"  Bogus entries NULLed: {bogus_count}")

    # --- Pass 2: NULL narrative text funding ---
    print("\n--- Pass 2: NULL narrative text funding ---")
    narrative_count = 0
    all_funding = cursor.execute(
        "SELECT id, company_name, Total_Funding_Formatted FROM startups WHERE Total_Funding_Formatted IS NOT NULL AND Total_Funding_Formatted != ''"
    ).fetchall()
    for rid, name, funding in all_funding:
        is_narrative = False
        for pattern in NARRATIVE_PATTERNS:
            if re.search(pattern, funding, re.IGNORECASE):
                is_narrative = True
                break
        if is_narrative:
            print(f"  [NULL] {name}: {funding[:60]}...")
            narrative_count += 1
            if not DRY_RUN:
                cursor.execute("UPDATE startups SET Total_Funding_Formatted = NULL WHERE id = ?", (rid,))
    print(f"  Narrative entries NULLed: {narrative_count}")

    # --- Pass 3: Normalize remaining amounts ---
    print("\n--- Pass 3: Normalize funding format ---")
    remaining = cursor.execute(
        "SELECT id, company_name, Total_Funding_Formatted FROM startups WHERE Total_Funding_Formatted IS NOT NULL AND Total_Funding_Formatted != ''"
    ).fetchall()
    normalized_count = 0
    failed_count = 0
    for rid, name, funding in remaining:
        value, formatted, currency = normalize_funding_amount(funding)
        if value and formatted:
            if formatted != funding:
                normalized_count += 1
                if not DRY_RUN:
                    cursor.execute("UPDATE startups SET Total_Funding_Formatted = ? WHERE id = ?", (formatted, rid))
        elif value is None and formatted is None:
            failed_count += 1
            if len(funding) > 5:
                print(f"  [WARN] Unparseable: {name}: {funding[:60]}")
    print(f"  Format normalized: {normalized_count}")
    print(f"  Unparseable (kept): {failed_count}")

    # --- Pass 4: Fix investments.amount_usd > $500M ---
    print("\n--- Pass 4: Fix suspicious investment amounts ---")
    big_investments = cursor.execute(
        "SELECT id, startup_id, amount_usd, round_type FROM investments WHERE amount_usd > 500000000"
    ).fetchall()
    big_fixed = 0
    for iid, sid, amount, rtype in big_investments:
        sname = cursor.execute("SELECT company_name FROM startups WHERE id = ?", (sid,)).fetchone()
        sname = sname[0] if sname else f"ID:{sid}"
        print(f"  [NULL] {sname}: ${amount:,.0f} ({rtype})")
        big_fixed += 1
        if not DRY_RUN:
            cursor.execute("UPDATE investments SET amount_usd = NULL WHERE id = ?", (iid,))
    print(f"  Big amounts NULLed: {big_fixed}")

    # --- Summary ---
    print(f"\n{'=' * 60}")
    print(f"  SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Bogus billions NULLed:      {bogus_count}")
    print(f"  Narrative text NULLed:       {narrative_count}")
    print(f"  Formats normalized:          {normalized_count}")
    print(f"  Big investments NULLed:      {big_fixed}")

    if DRY_RUN:
        print(f"\n  DRY RUN — no changes written. Use --commit to apply.")
    else:
        conn.commit()
        print(f"\n  Changes committed to {DB_NAME}")

    conn.close()


if __name__ == "__main__":
    main()
