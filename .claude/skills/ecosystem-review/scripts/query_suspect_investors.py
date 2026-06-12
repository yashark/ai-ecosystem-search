"""Find investors that need verification — low confidence, missing type, suspicious names."""
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[4] / "Master.db"

KNOWN_TECH_COMPANIES = {
    "google", "microsoft", "meta", "amazon", "apple", "nvidia", "openai", "tesla",
    "samsung", "intel", "ibm", "oracle", "salesforce", "adobe", "uber", "airbnb",
    "spotify", "netflix", "twitter", "snap", "pinterest", "zoom", "slack", "stripe",
    "palantir", "databricks", "snowflake", "cloudflare", "twilio"
}

def main():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row

    print("=" * 70)
    print("  SUSPECT INVESTOR AUDIT")
    print("=" * 70)

    # 1. Zero/low confidence
    low_conf = conn.execute(
        "SELECT * FROM investors WHERE (status IS NULL OR status = 'active') "
        "AND data_confidence < 50 ORDER BY data_confidence ASC"
    ).fetchall()
    print(f"\n--- Low Confidence < 50 ({len(low_conf)}) ---")
    for inv in low_conf:
        linked = conn.execute(
            "SELECT COUNT(*) FROM investments WHERE investor_id = ?", (inv["investor_id"],)
        ).fetchone()[0]
        print(f"  [{inv['investor_id']}] {inv['investor_name']} (conf={inv['data_confidence']}, type={inv['investor_type']}, linked={linked})")
        print(f"       Origin: {inv['investor_origin'] or 'Unknown'}  |  Location: {inv['location'] or 'Unknown'}")
        print(f"       Website: {inv['website'] or 'None'}")
        print()

    # 2. Missing investor_type
    no_type = conn.execute(
        "SELECT * FROM investors WHERE (investor_type IS NULL OR investor_type = 'None' OR investor_type = '') "
        "AND (status IS NULL OR status = 'active')"
    ).fetchall()
    print(f"\n--- Missing Investor Type ({len(no_type)}) ---")
    for inv in no_type:
        print(f"  [{inv['investor_id']}] {inv['investor_name']} (conf={inv['data_confidence']})")
        print(f"       Website: {inv['website'] or 'None'}")
        print()

    # 3. Names matching known tech companies
    all_inv = conn.execute(
        "SELECT * FROM investors WHERE status IS NULL OR status = 'active'"
    ).fetchall()
    tech_matches = [inv for inv in all_inv if inv["investor_name"].lower().strip() in KNOWN_TECH_COMPANIES]
    if tech_matches:
        print(f"\n--- Tech Company Names ({len(tech_matches)}) ---")
        for inv in tech_matches:
            print(f"  [{inv['investor_id']}] {inv['investor_name']} (type={inv['investor_type']})")
            print()

    # 4. Very short names (possibly abbreviations/garbage)
    short_names = [inv for inv in all_inv if len(inv["investor_name"].strip()) <= 3]
    if short_names:
        print(f"\n--- Very Short Names <= 3 chars ({len(short_names)}) ---")
        for inv in short_names:
            linked = conn.execute(
                "SELECT COUNT(*) FROM investments WHERE investor_id = ?", (inv["investor_id"],)
            ).fetchone()[0]
            print(f"  [{inv['investor_id']}] '{inv['investor_name']}' (type={inv['investor_type']}, linked={linked})")
            print()

    # 5. Possible individuals (names with spaces, no corporate suffixes)
    corporate_suffixes = ["capital", "ventures", "partners", "fund", "group", "investment",
                          "vc", "holding", "fon", "yatırım", "girişim", "accelerator"]
    possible_individuals = []
    for inv in all_inv:
        name = inv["investor_name"].lower().strip()
        words = name.split()
        if 2 <= len(words) <= 3 and not any(s in name for s in corporate_suffixes):
            possible_individuals.append(inv)

    if possible_individuals:
        print(f"\n--- Possible Individual Names ({len(possible_individuals)}) ---")
        for inv in possible_individuals[:20]:
            print(f"  [{inv['investor_id']}] {inv['investor_name']} (type={inv['investor_type']}, conf={inv['data_confidence']})")
        if len(possible_individuals) > 20:
            print(f"  ... and {len(possible_individuals) - 20} more")
        print()

    conn.close()

if __name__ == "__main__":
    main()
