"""Find startups likely misclassified — low confidence, uncategorized, or keyword mismatch."""
import sqlite3
import json
from pathlib import Path

DB = Path(__file__).resolve().parents[4] / "Master.db"

# Keywords strongly associated with specific sectors
SECTOR_SIGNALS = {
    "Finance | Fintech": ["banking", "payment", "credit", "lending", "insurance", "accounting", "fintech", "trading", "wallet"],
    "Health | Healthtech": ["clinical", "patient", "diagnosis", "medical", "hospital", "pharma", "health", "biotech", "telemedicine"],
    "Agriculture | Agritech": ["farming", "crop", "soil", "irrigation", "livestock", "agriculture", "agri"],
    "Education | Edtech": ["education", "learning", "student", "school", "course", "tutoring", "edtech"],
    "Services | Logistics & Supply Chain": ["logistics", "shipping", "freight", "warehouse", "delivery", "supply chain", "cargo"],
    "Energy | Cleantech": ["solar", "wind", "energy", "battery", "renewable", "carbon", "sustainability"],
    "Real Estate | PropTech": ["real estate", "property", "proptech", "building", "construction", "housing"],
}

def main():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row

    print("=" * 70)
    print("  MISCLASSIFICATION AUDIT")
    print("=" * 70)

    # 1. Uncategorized startups
    uncategorized = conn.execute(
        "SELECT id, company_name, description, AI_Use_Case, Category, data_confidence "
        "FROM startups WHERE Category = 'General | Uncategorized' OR Category IS NULL OR Category = ''"
    ).fetchall()
    if uncategorized:
        print(f"\n--- Uncategorized ({len(uncategorized)}) ---")
        for s in uncategorized:
            print(f"  [{s['id']}] {s['company_name']} (conf={s['data_confidence']})")
            print(f"       Desc: {(s['description'] or '')[:120]}...")
            print(f"       AI:   {(s['AI_Use_Case'] or '')[:80]}")
            print()

    # 2. Keyword-category mismatches
    all_startups = conn.execute(
        "SELECT id, company_name, description, AI_Use_Case, Category, data_confidence "
        "FROM startups WHERE description IS NOT NULL AND Category IS NOT NULL"
    ).fetchall()

    mismatches = []
    for s in all_startups:
        desc = (s["description"] or "").lower() + " " + (s["AI_Use_Case"] or "").lower()
        current_cat = s["Category"] or ""
        for expected_sector, keywords in SECTOR_SIGNALS.items():
            hits = sum(1 for k in keywords if k in desc)
            if hits >= 2 and expected_sector.split(" | ")[0] not in current_cat:
                mismatches.append((s, expected_sector, hits))
                break

    if mismatches:
        print(f"\n--- Likely Mismatches ({len(mismatches)}) ---")
        for s, expected, hits in mismatches[:30]:
            print(f"  [{s['id']}] {s['company_name']}")
            print(f"       Current:  {s['Category']}")
            print(f"       Expected: {expected} ({hits} keyword hits)")
            print(f"       Desc:     {(s['description'] or '')[:120]}...")
            print()

    # 3. Low confidence with category
    low_conf = conn.execute(
        "SELECT id, company_name, description, Category, data_confidence "
        "FROM startups WHERE data_confidence < 30 AND Category IS NOT NULL AND Category != 'General | Uncategorized' "
        "ORDER BY data_confidence ASC LIMIT 20"
    ).fetchall()
    if low_conf:
        print(f"\n--- Low Confidence with Category (top 20) ---")
        for s in low_conf:
            print(f"  [{s['id']}] {s['company_name']} (conf={s['data_confidence']}) → {s['Category']}")
            print(f"       {(s['description'] or '')[:120]}...")
            print()

    conn.close()

if __name__ == "__main__":
    main()
