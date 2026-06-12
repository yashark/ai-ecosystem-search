"""
Bulk Re-classification Utility (8B Optimized)
Uses a 5-step micro-chain to re-classify startups whose tag doesn't match their description.
Also backfills business_model.

Usage:
    python3 util_reclassify.py                  # Dry-run: show what would change
    python3 util_reclassify.py --commit         # Apply changes
    python3 util_reclassify.py --batch 100      # Process 100 startups per run
    python3 util_reclassify.py --commit --all    # Process all, no batch limit
"""
import sqlite3
import argparse
import json
from datetime import datetime, timezone

from agent_core import (
    _call_llm, validate_sector_tag, agent_update_field, get_utc_now,
    seed_tag_keywords, validate_classification_accuracy,
    learn_keywords_from_description, get_top_candidate_tags,
    VALID_MATRIX, SECTION_7_MATRIX_STR, _TAG_SEED,
    get_db_connection,
)

DB_NAME = "Master.db"

# Category is stored as: "<Industrial_Sector> | <Tag_Category>"
def parse_category(category_value):
    if not category_value or str(category_value).strip().lower() in ["", "null", "none", "unknown"]:
        return None, None
    parts = str(category_value).split(" | ", 1)
    if len(parts) == 2:
        return parts[0].strip() or None, parts[1].strip() or None
    return None, None


def build_category(sector, tag):
    if not sector or not tag:
        return None
    s = str(sector).strip()
    t = str(tag).strip()
    if not s or not t:
        return None
    return f"{s} | {t}"

# Known duplicated AI_Use_Case strings that need rewriting
_KNOWN_DUPES = set()

def _load_known_dupes(conn):
    """Populate the set of AI_Use_Case strings shared by ≥3 startups."""
    global _KNOWN_DUPES
    cursor = conn.cursor()
    rows = cursor.execute("""
        SELECT AI_Use_Case FROM startups
        WHERE Status='Active'
        GROUP BY AI_Use_Case HAVING COUNT(*) >= 3
    """).fetchall()
    _KNOWN_DUPES = {row[0] for row in rows}


# ─── 5-Step Micro-Chain (8B Optimized) ─────────────────────────────────────

def step_1_is_logistics(description):
    """Binary gate: Is this company in physical logistics?"""
    prompt = f"""Does this company work in PHYSICAL logistics, shipping, warehousing, fleet management, cargo, freight, or supply chain management?

Company description: "{description[:500]}"

Answer ONLY with JSON: {{"answer": true}} or {{"answer": false}}
Do NOT answer true for software companies, AI platforms, SaaS tools, or analytics — only for physical logistics operations."""
    result = _call_llm(prompt)
    if result and isinstance(result, dict):
        return result.get('answer', False)
    return False


def step_2_extract_domain(description):
    """Extract the primary business domain from description."""
    prompt = f"""What is the main business domain of this company? Answer with just the domain.

Company description: "{description[:500]}"

Examples of domains: "social media management", "cybersecurity", "energy optimization", "medical imaging", "recruitment", "chatbot", "data analytics", "autonomous drones", "document processing", "food delivery", "agricultural monitoring"

Output ONLY JSON: {{"domain": "the domain in 2-4 words"}}"""
    result = _call_llm(prompt)
    if result and isinstance(result, dict):
        return result.get('domain', 'unknown')
    return 'unknown'


def step_3_pick_tag(description, domain, candidate_tags, conn):
    """Pick the best tag from pre-filtered candidates."""
    # Build the short candidate list with descriptions from seed data
    tag_to_sector = {}
    for s, t in VALID_MATRIX:
        tag_to_sector[t] = s

    options = []
    for i, tag in enumerate(candidate_tags, 1):
        sector = tag_to_sector.get(tag, 'General')
        # Get the matrix context for this tag
        seed_keywords = ', '.join(_TAG_SEED.get(tag, [])[:5])
        options.append(f"{i}. {tag} ({sector}) — keywords: {seed_keywords}")
    options_str = '\n'.join(options)

    prompt = f"""This company works in: "{domain}"
Company description: "{description[:400]}"

Pick ONE tag that best fits from these options ONLY:
{options_str}

CRITICAL: Classify by the INDUSTRY the company serves, NOT its delivery model.
SaaS/API/platform are business models, NOT categories. A company doing AI for hotels is "Hospitality & Travel" even if it is SaaS.
"SaaS & Platforms" is NOT a valid category. For industry-agnostic horizontal tools, use "Software Development".

Output ONLY JSON: {{"tag": "exact tag name from list", "sector": "exact sector name"}}"""
    result = _call_llm(prompt)
    if result and isinstance(result, dict):
        tag = result.get('tag', '')
        sector = result.get('sector', '')
        # Validate the pair
        sector, tag = validate_sector_tag(sector, tag)
        return sector, tag
    return None, None


def step_4_write_use_case(description, tag):
    """Write a unique 5-word AI use case summary."""
    prompt = f"""Write a UNIQUE 5-word summary of what this company's AI product does.

Company description: "{description[:400]}"
Category: {tag}

Rules:
- Exactly 5 words
- Must describe THIS specific company's product, not a generic category
- Do NOT use: "AI-powered logistics optimization platform" or any generic template
- Example good outputs: "NLP document parsing automation tool", "Facial recognition access control system", "Predictive crop disease detection app"

Output ONLY JSON: {{"use_case": "five word summary"}}"""
    result = _call_llm(prompt)
    if result and isinstance(result, dict):
        return result.get('use_case', '')
    return ''


def step_5_auditor(description, sector, tag, use_case, current_tag=None):
    """Final sanity check: does this classification make sense?"""
    # If reclassifying away from catch-all buckets, use aggressive prompt that favors the change
    anti_catchall = ""
    if current_tag and ("SaaS" in current_tag or "Software Development" in current_tag):
        anti_catchall = """
IMPORTANT: This company was previously classified as a generic software/platform category.
Classify by the INDUSTRY the company serves, not its delivery model.
SaaS, API, marketplace, platform, and software development are business models — NOT industry categories.
If the proposed sector/tag matches ANY industry signal in the description (health, medical, patient,
education, finance, banking, payment, agriculture, farming, gaming, energy, logistics, supply chain,
HR, recruitment, real estate, marketing, advertising, cybersecurity, robotics, telecom,
insurance, manufacturing, retail, e-commerce, legal, media, travel, hospitality), APPROVE it.
Only reject if the proposed classification is factually wrong (e.g., calling a health company "Finance")."""

    prompt = f"""Does this classification make sense?

Company description: "{description[:400]}"
Assigned sector: {sector}
Assigned tag: {tag}
AI use case: {use_case}
{anti_catchall}
Answer ONLY JSON: {{"correct": true}} or {{"correct": false, "reason": "brief reason"}}"""
    result = _call_llm(prompt)
    if result and isinstance(result, dict):
        return result.get('correct', False)
    return False


def step_bmodel(description):
    """Classify business model with a single binary-gate prompt."""
    prompt = f"""Does this company sell a SOFTWARE PRODUCT (SaaS, API, app, platform, dashboard)?

Description: "{description[:400]}"

Answer ONLY with JSON. Pick exactly one:
{{"model": "Product"}} — they sell software, SaaS, API, a platform, or a downloadable app
{{"model": "Consultancy"}} — they offer custom services, consulting, implementation projects
{{"model": "Hybrid"}} — they do both product and consulting"""
    result = _call_llm(prompt)
    if result and isinstance(result, dict):
        model = result.get('model', 'Unknown')
        if model in ('Product', 'Consultancy', 'Hybrid'):
            return model
    return 'Unknown'


# ─── Main Pipeline ──────────────────────────────────────────────────────────

def process_startup(conn, sid, name, description, current_category, current_use_case, current_bmodel,
                    commit=False, force_retag=False, stage1=False):
    """Run the micro-chain for one startup. Returns a change dict or None."""
    changes = {}
    cursor = conn.cursor()

    current_sector, current_tag = parse_category(current_category)

    # --- Phase A: Tag re-classification ---
    tag_score = validate_classification_accuracy(conn, current_tag, description)
    needs_retag = force_retag or tag_score < 0.30

    if needs_retag:
        # Step 1: Binary logistics gate
        is_logistics = step_1_is_logistics(description)
        if is_logistics:
            # Confirmed logistics — learn and skip re-tag
            learn_keywords_from_description(conn, current_tag, description)
            print(f"    [Step 1] Confirmed as logistics. Keeping tag.")
        else:
            # Step 2: Extract domain
            domain = step_2_extract_domain(description)
            print(f"    [Step 2] Domain: {domain}")

            # Step 3: Pick tag from top candidates
            candidates = get_top_candidate_tags(conn, description, n=5)
            new_sector, new_tag = step_3_pick_tag(description, domain, candidates, conn)

            if new_tag and new_tag != current_tag:
                # Step 4: Write unique use case
                new_use_case = step_4_write_use_case(description, new_tag)

                # Step 5: Auditor sanity check
                if step_5_auditor(description, new_sector, new_tag, new_use_case, current_tag=current_tag):
                    changes['Category'] = build_category(new_sector, new_tag)
                    if new_use_case:
                        changes['AI_Use_Case'] = new_use_case
                    # Learn from the new correct classification
                    learn_keywords_from_description(conn, new_tag, description)
                    print(f"    [Step 5] ✓ Auditor approved: {new_sector}/{new_tag}")
                else:
                    print(f"    [Step 5] ✗ Auditor rejected re-classification")
            elif new_tag:
                print(f"    [Step 3] Same tag picked ({new_tag}). Validating...")
                learn_keywords_from_description(conn, new_tag, description)
    else:
        # Tag validates — learn from this description to grow index
        learn_keywords_from_description(conn, current_tag, description)

    # --- Phase B: AI_Use_Case dedup fix ---
    if current_use_case in _KNOWN_DUPES and 'AI_Use_Case' not in changes:
        effective_tag = current_tag
        if 'Category' in changes:
            _, effective_tag = parse_category(changes['Category'])
        new_use_case = step_4_write_use_case(description, effective_tag)
        if new_use_case and new_use_case != current_use_case:
            changes['AI_Use_Case'] = new_use_case
            print(f"    [Dedup] Use case: '{current_use_case}' → '{new_use_case}'")

    # --- Phase C: business_model backfill (skipped in stage1 mode) ---
    if not stage1 and (not current_bmodel or current_bmodel == 'Unknown'):
        bmodel = step_bmodel(description)
        if bmodel != 'Unknown':
            changes['business_model'] = bmodel
            print(f"    [BModel] → {bmodel}")

    # --- Apply changes ---
    if changes and commit:
        for field, value in changes.items():
            agent_update_field(cursor, "startup", sid, "util_reclassify", 75, field, value)
        cursor.execute("UPDATE startups SET last_updated = ? WHERE id = ?", (get_utc_now(), sid))
        conn.commit()

    return changes if changes else None


def main():
    parser = argparse.ArgumentParser(description="Bulk re-classify startups with 8B micro-chain")
    parser.add_argument("--commit", action="store_true", help="Apply changes to DB (default: dry-run)")
    parser.add_argument("--batch", type=int, default=50, help="Number of startups per run (default: 50)")
    parser.add_argument("--all", action="store_true", help="Process all, no batch limit")
    parser.add_argument("--force-retag", action="store_true", help="Re-classify all startups regardless of validation score")
    parser.add_argument("--stage1", action="store_true", help="Stage 1 mode: Category + AI_Use_Case only, skip business_model")
    parser.add_argument("--category-filter", type=str, default=None, help="Only process startups whose Category contains this text (e.g. 'SaaS', 'Logistics')")
    parser.add_argument("--min-confidence", type=int, default=None, help="Only process startups with confidence >= this value")
    parser.add_argument("--max-confidence", type=int, default=None, help="Only process startups with confidence <= this value")
    args = parser.parse_args()

    conn = get_db_connection(DB_NAME)
    seed_tag_keywords(conn)
    _load_known_dupes(conn)

    cursor = conn.cursor()

    # Select active startups, ordered by most-needed-first
    conditions = ["Status = 'Active'", "description IS NOT NULL", "description != ''"]
    params = []
    if args.category_filter:
        conditions.append("Category LIKE ?")
        params.append(f"%{args.category_filter}%")
    if args.min_confidence is not None:
        conditions.append("data_confidence >= ?")
        params.append(args.min_confidence)
    if args.max_confidence is not None:
        conditions.append("data_confidence <= ?")
        params.append(args.max_confidence)

    query = f"""
        SELECT id, company_name, description, Category, AI_Use_Case, business_model,
               data_confidence
        FROM startups
        WHERE {' AND '.join(conditions)}
        ORDER BY data_confidence ASC, id ASC
    """
    if not args.all:
        query += f" LIMIT {args.batch}"

    startups = cursor.execute(query, params).fetchall()

    print(f"\n{'='*60}")
    print(f"  Re-classification Utility ({'COMMIT' if args.commit else 'DRY RUN'})")
    print(f"  Processing {len(startups)} startups")
    print(f"{'='*60}\n")

    total_changed = 0
    total_tag_changed = 0
    total_usecase_changed = 0
    total_bmodel_filled = 0

    for sid, name, desc, category, use_case, bmodel, conf in startups:
        print(f"\n[{sid}] {name} (category: {category}, conf: {conf})")

        changes = process_startup(conn, sid, name, desc, category, use_case or '', bmodel,
                                  commit=args.commit, force_retag=args.force_retag, stage1=args.stage1)

        if changes:
            total_changed += 1
            if 'Category' in changes:
                total_tag_changed += 1
                print(f"    Category: {category} → {changes['Category']}")
            if 'AI_Use_Case' in changes:
                total_usecase_changed += 1
            if 'business_model' in changes:
                total_bmodel_filled += 1
        else:
            print(f"    No changes needed.")

    # Report keyword growth
    kw_total = cursor.execute("SELECT COUNT(*) FROM tag_keywords").fetchone()[0]
    kw_learned = cursor.execute("SELECT COUNT(*) FROM tag_keywords WHERE source='learned'").fetchone()[0]

    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    print(f"  Startups processed:   {len(startups)}")
    print(f"  Changed:              {total_changed}")
    print(f"    Tags re-classified: {total_tag_changed}")
    print(f"    Use cases fixed:    {total_usecase_changed}")
    print(f"    BModel filled:      {total_bmodel_filled}")
    print(f"  Keyword index:        {kw_total} ({kw_learned} learned)")
    if not args.commit:
        print(f"\n  ⚠ DRY RUN — no changes written. Use --commit to apply.")
    print()

    conn.close()


if __name__ == "__main__":
    main()
