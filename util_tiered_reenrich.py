"""
Tiered Re-enrichment Utility
Stage 2: Confidence-based tiered approach (full three-pass for low, reclassify for mid)
Stage 3: Full three-pass for all (--full mode)

Usage:
    python3 util_tiered_reenrich.py                     # Dry-run tiered
    python3 util_tiered_reenrich.py --commit             # Apply tiered changes
    python3 util_tiered_reenrich.py --full --commit      # Full three-pass for all
    python3 util_tiered_reenrich.py --commit --batch 50  # Process 50 at a time
    python3 util_tiered_reenrich.py --commit --resume    # Skip already-enriched (Stage 2 Tier A)
"""
import argparse
import json
from datetime import datetime, timezone

from agent_core import (
    get_db_connection, _call_llm, validate_sector_tag, agent_update_field,
    get_utc_now, calculate_confidence, three_pass_enrichment,
    search_with_rotation, scrape_website_text, search_escalation,
    seed_tag_keywords, validate_classification_accuracy,
    learn_keywords_from_description, get_top_candidate_tags,
    VALID_MATRIX, SECTION_7_MATRIX_STR, _TAG_SEED,
)
from util_reclassify import (
    parse_category, build_category, process_startup as reclassify_startup,
    _load_known_dupes,
)

DB_NAME = "Master.db"

# Confidence thresholds for tiered processing
TIER_A_THRESHOLD = 40   # data_confidence < 40 → full three-pass
TIER_B_THRESHOLD = 70   # data_confidence 40-70 → reclassify only
                        # data_confidence > 70 → skip (Tier C)


def _gather_search_context(name, website=None, description=None):
    """Gather search context for three-pass enrichment."""
    search_context = ""
    try:
        results = search_with_rotation(f'"{name}" yapay zeka OR AI startup')
        if results:
            search_context = "\n".join(
                f"- {r.get('title', '')}: {r.get('body', '')[:200]}"
                for r in results[:5]
            )
    except Exception as e:
        print(f"    [Search] Error: {e}")

    # Escalation search for missing fields
    extra = search_escalation(name, {}, website=website, description=description)
    if extra:
        search_context += "\n" + extra

    return search_context


def _gather_website_context(website):
    """Scrape website for context."""
    if not website or website in ('Unknown', 'nan', ''):
        return ""
    try:
        return scrape_website_text(website) or ""
    except Exception:
        return ""


def process_tier_a(conn, sid, name, desc, website, city, founders, commit=False):
    """Full three-pass re-enrichment with web search."""
    print(f"    [Tier A] Full three-pass enrichment...")

    base_data = {
        "name": name,
        "description": desc or "",
        "website": website or "",
        "city": city or "",
        "founders": founders or "",
    }

    search_context = _gather_search_context(name, website, desc)
    website_context = _gather_website_context(website)

    result = three_pass_enrichment(base_data, search_context, website_context)
    if not result:
        print(f"    [Tier A] Three-pass chain returned nothing.")
        return None

    if commit:
        cursor = conn.cursor()
        # Update all fields from result
        field_map = {
            'Category': 'Category',
            'AI_Use_Case': 'AI_Use_Case',
            'Total_Funding': 'Total_Funding_Formatted',
            'Investors': 'Investors',
            'Tech_Mentioned': 'Tech_Mentioned',
            'Tech_Assumed': 'Tech_Assumed',
            'business_model': 'business_model',
            'website': 'website',
            'description': 'description',
            'city': 'city',
            'founders': 'founders',
        }
        for result_key, db_col in field_map.items():
            val = result.get(result_key)
            if val and val not in ('Unknown', 'None', 'null', ''):
                agent_update_field(cursor, "startup", sid, "util_tiered_reenrich", 70, db_col, val)

        # Update confidence and timestamp
        confidence = calculate_confidence(cursor, sid)
        cursor.execute(
            "UPDATE startups SET data_confidence = ?, last_updated = ? WHERE id = ?",
            (confidence, get_utc_now(), sid)
        )
        conn.commit()

    return result


def process_tier_b(conn, sid, name, desc, category, use_case, bmodel, commit=False):
    """Reclassify only — reuse the micro-chain from util_reclassify."""
    print(f"    [Tier B] Reclassify only...")
    return reclassify_startup(conn, sid, name, desc, category, use_case or '', bmodel,
                              commit=commit, force_retag=True, stage1=False)


def main():
    parser = argparse.ArgumentParser(description="Tiered re-enrichment of startups")
    parser.add_argument("--commit", action="store_true", help="Apply changes (default: dry-run)")
    parser.add_argument("--batch", type=int, default=0, help="Limit to N startups (0 = all)")
    parser.add_argument("--full", action="store_true", help="Stage 3: full three-pass for ALL startups")
    parser.add_argument("--resume", action="store_true", help="Skip startups already processed by Tier A")
    args = parser.parse_args()

    conn = get_db_connection(DB_NAME)
    seed_tag_keywords(conn)
    _load_known_dupes(conn)
    cursor = conn.cursor()

    # Select all active startups with descriptions
    query = """
        SELECT id, company_name, description, Category, AI_Use_Case, business_model,
               data_confidence, website, city, founders
        FROM startups
        WHERE Status = 'Active' AND description IS NOT NULL AND description != ''
        ORDER BY data_confidence ASC, id ASC
    """
    if args.batch > 0:
        query += f" LIMIT {args.batch}"

    startups = cursor.execute(query).fetchall()

    mode = "FULL THREE-PASS" if args.full else "TIERED"
    print(f"\n{'='*60}")
    print(f"  Re-enrichment ({mode}) — {'COMMIT' if args.commit else 'DRY RUN'}")
    print(f"  Processing {len(startups)} startups")
    print(f"{'='*60}\n")

    stats = {"tier_a": 0, "tier_b": 0, "tier_c": 0, "changed": 0, "failed": 0}

    for row in startups:
        sid, name, desc, category, use_case, bmodel, confidence, website, city, founders = row
        confidence = int(confidence or 0)

        if args.full:
            tier = "A"
        elif confidence < TIER_A_THRESHOLD:
            tier = "A"
        elif confidence <= TIER_B_THRESHOLD:
            tier = "B"
        else:
            tier = "C"

        # Skip Tier C unless in full mode
        if tier == "C" and not args.full:
            stats["tier_c"] += 1
            continue

        # Resume mode: skip if already recently updated by this tool
        if args.resume and tier == "A":
            last_agent = cursor.execute("""
                SELECT agent_name FROM change_log WHERE entity_type='startup' AND entity_id=?
                ORDER BY changed_at DESC LIMIT 1
            """, (sid,)).fetchone()
            if last_agent and last_agent[0] == 'util_tiered_reenrich':
                stats["tier_c"] += 1
                continue

        print(f"\n[{sid}] {name} — Tier {tier} (confidence: {confidence})")

        try:
            if tier == "A":
                result = process_tier_a(conn, sid, name, desc, website, city, founders, commit=args.commit)
                stats["tier_a"] += 1
                if result:
                    stats["changed"] += 1
                else:
                    stats["failed"] += 1
            elif tier == "B":
                changes = process_tier_b(conn, sid, name, desc, category, use_case, bmodel, commit=args.commit)
                stats["tier_b"] += 1
                if changes:
                    stats["changed"] += 1
        except Exception as e:
            print(f"    [Error] {e}")
            stats["failed"] += 1

    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    print(f"  Total processed:  {len(startups)}")
    print(f"  Tier A (full):    {stats['tier_a']}")
    print(f"  Tier B (reclass): {stats['tier_b']}")
    print(f"  Tier C (skipped): {stats['tier_c']}")
    print(f"  Changed:          {stats['changed']}")
    print(f"  Failed:           {stats['failed']}")
    if not args.commit:
        print(f"\n  DRY RUN — no changes written. Use --commit to apply.")
    print()
    conn.close()


if __name__ == "__main__":
    main()
