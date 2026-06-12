"""
Normalize Investor Locations — Deterministic cleanup, no LLM needed.

Fixes inconsistent location strings and classifies each investor as
Domestic / International / Unknown using region config.

Usage:
    python3 util_normalize_investors.py           # Dry run
    python3 util_normalize_investors.py --commit  # Apply changes
"""
import argparse
import sqlite3
from region_config import get_active_config
from agent_core import get_db_connection

DB_NAME = "Master.db"
_CFG = get_active_config()

# Location data from region config
LOCATION_CANONICAL = _CFG.location_canonical
DOMESTIC_MARKERS = _CFG.domestic_markers


def classify_origin(location):
    """Classify investor as Domestic, International, or Unknown."""
    return _CFG.classify_origin(location)


def normalize_location(location):
    """Apply canonical mapping to a location string."""
    if not location:
        return location

    stripped = location.strip()

    # Direct match
    if stripped in LOCATION_CANONICAL:
        return LOCATION_CANONICAL[stripped]

    # Fix locale-specific characters within longer strings
    result = stripped
    for old, new in _CFG.location_inline_replacements:
        if old in result and new not in result:
            result = result.replace(old, new)

    # Fix country suffixes
    for old, new in _CFG.location_suffix_replacements:
        if result.endswith(old):
            result = result[:-len(old)] + new

    return result


def ensure_investor_origin_column(conn):
    """Add investor_origin column if it doesn't exist."""
    cursor = conn.cursor()
    cols = [row[1] for row in cursor.execute("PRAGMA table_info(investors)").fetchall()]
    if "investor_origin" not in cols:
        cursor.execute("ALTER TABLE investors ADD COLUMN investor_origin TEXT")
        conn.commit()
        print("  [Schema] Added 'investor_origin' column to investors table")
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description="Normalize investor locations")
    parser.add_argument("--commit", action="store_true", help="Apply changes (default: dry run)")
    args = parser.parse_args()

    conn = get_db_connection(DB_NAME)
    conn.execute("PRAGMA busy_timeout = 30000")

    print("=" * 60)
    print(f"  Normalize Investor Locations ({'COMMIT' if args.commit else 'DRY RUN'})")
    print("=" * 60)

    if args.commit:
        ensure_investor_origin_column(conn)

    cursor = conn.cursor()
    rows = cursor.execute("""
        SELECT investor_id, investor_name, location
        FROM investors
    """).fetchall()

    loc_changed = 0
    origin_set = 0
    domestic = 0
    international = 0
    unknown = 0

    for inv_id, name, location in rows:
        new_location = normalize_location(location)
        origin = classify_origin(new_location or location)

        if origin == "Domestic":
            domestic += 1
        elif origin == "International":
            international += 1
        else:
            unknown += 1

        location_changed = new_location != location and new_location is not None

        if location_changed:
            loc_changed += 1
            if loc_changed <= 30:
                print(f"  [{inv_id}] {name}: '{location}' → '{new_location}'")
            if args.commit:
                cursor.execute("UPDATE investors SET location = ? WHERE investor_id = ?",
                               (new_location, inv_id))

        if args.commit:
            cursor.execute("UPDATE investors SET investor_origin = ? WHERE investor_id = ?",
                           (origin, inv_id))
            origin_set += 1

    if args.commit:
        conn.commit()

    # Also fix startup city field using inline replacements
    print(f"\n--- Startup City Normalization ---")
    # Build a query to find cities that need inline replacement
    city_conditions = []
    for old, new in _CFG.location_inline_replacements:
        city_conditions.append(f"(city LIKE '%{old}%' AND city NOT LIKE '%{new}%')")
    # Also handle duplicated city prefix
    city_conditions.append("city LIKE '%İstanbul, İstanbul%'")

    if city_conditions:
        where_clause = " OR ".join(city_conditions)
        city_rows = cursor.execute(f"""
            SELECT id, company_name, city FROM startups
            WHERE {where_clause}
        """).fetchall()
    else:
        city_rows = []

    city_fixed = 0
    for sid, name, city in city_rows:
        new_city = city
        for old, new in _CFG.location_inline_replacements:
            if old in new_city and new not in new_city:
                new_city = new_city.replace(old, new)
        # Fix duplicated city prefix
        new_city = new_city.replace("İstanbul, İstanbul, Turkey", "İstanbul, Turkey")
        new_city = new_city.replace("İstanbul, İstanbul", "İstanbul")
        if new_city != city:
            city_fixed += 1
            if city_fixed <= 10:
                print(f"  [{sid}] {name}: '{city}' → '{new_city}'")
            if args.commit:
                cursor.execute("UPDATE startups SET city = ? WHERE id = ?", (new_city, sid))

    if args.commit:
        conn.commit()

    print(f"\n{'=' * 60}")
    print(f"  SUMMARY")
    print(f"  Investor locations normalized: {loc_changed}")
    print(f"  Investor origins classified: Domestic={domestic}, International={international}, Unknown={unknown}")
    print(f"  Startup cities fixed: {city_fixed}")
    if not args.commit:
        print("  DRY RUN — no changes written. Use --commit to apply.")
    print("=" * 60)

    conn.close()


if __name__ == "__main__":
    main()
