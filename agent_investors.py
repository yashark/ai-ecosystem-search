"""
Module 4: Investor Enrichment
Enriches investor profiles using enhanced three-pass chain with expanded search,
website scraping, and structured investment data extraction.

Usage (standalone):
    python3 agent_investors.py                          # All with confidence < 30
    python3 agent_investors.py --name "212 Capital"     # One specific investor
    python3 agent_investors.py --batch 50               # Top 50 most sparse
    python3 agent_investors.py --type VC                # All VCs
"""
import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone

from region_config import get_active_config
from agent_core import (
    search_with_rotation, scrape_website_text, _call_llm,
    agent_update_field, get_utc_now, clear_search_cache,
    fuzzy_matcher, get_db_connection, parse_search_results,
    normalize_turkish, normalize_funding_amount, discover_portfolio_startups,
)
from util_currency import convert_to_usd

DB_NAME = "Master.db"
_CFG = get_active_config()

# ─────────────────────────────────────────────────────────────────────────────
# Enhanced Three-Pass Investor Enrichment
# ─────────────────────────────────────────────────────────────────────────────

def _gather_investor_context(investor_name):
    """Run 5 targeted searches and scrape top URLs for rich context."""
    queries = _CFG.investor_search_queries(investor_name) + [
        f'"{investor_name}" crunchbase.com OR linkedin.com fund size AUM',
        f'"{investor_name}" {_CFG.region_name} investment startup amount round 2024 2025',
        f'"{investor_name}" site:crunchbase.com/organization',
    ]

    search_parts = []
    scrape_urls = []
    for q in queries:
        result = search_with_rotation(q, max_results=5)
        if result:
            search_parts.append(result)
            # Extract URLs for scraping
            parsed = parse_search_results(result)
            for p in parsed:
                url = p.get('url', '')
                if any(domain in url for domain in ['crunchbase.com', 'linkedin.com']) or investor_name.lower().replace(' ', '') in url.lower():
                    scrape_urls.append(url)

    # Scrape top 3 relevant URLs
    scraped_text = ""
    for url in scrape_urls[:3]:
        text = scrape_website_text(url)
        if text and len(text) > 100:
            scraped_text += f"\n--- Scraped from {url} ---\n{text[:2000]}\n"

    search_context = "\n".join(search_parts)
    return search_context, scraped_text


def _investor_pass_1_enhanced(investor_name, search_context, scraped_text):
    """Pass 1: Enhanced fact extraction with deal-level data."""
    prompt = f"""You are a Fact-Extraction Agent for investors. Extract detailed facts about this investor.

Investor Name: {investor_name}

Web Search Results:
{search_context[:4000]}

Scraped Web Pages:
{scraped_text[:3000] if scraped_text else 'No pages scraped.'}

FIRST: Determine if this entity is actually an investor.
"is_investor" (boolean: true if this entity's PRIMARY purpose involves investing capital. false if it is a technology company, product company, media outlet, person who is not an investor, or a generic/placeholder name)
CRITICAL: A technology company (e.g., Nvidia, Google, OpenAI) is NOT an investor unless it has a dedicated venture/investment arm (e.g., "Google Ventures" is an investor, but "Google" itself is not). If the entity's PRIMARY business is building products/services (not investing), set is_investor to false and set all other fields to "Unknown" or empty.

If is_investor is true, extract ONLY the facts you can find. Output a JSON with these keys:
"investor_type" (string: VC, CVC, Angel, Accelerator, PE, Government, Family Office, or Unknown. Use CVC for corporate venture capital — the investment arm of a corporation)
"website" (string, URL or "Unknown")
"location" (string, city/country or "Unknown")
"founded_year" (integer or null)
"fund_size" (string, any mention of fund size/AUM, or "Unknown")
"fund_vintage" (string, year the current fund was raised, or "Unknown")
"focus_areas" (string, what industries they invest in, or "Unknown")
"stage_focus" (string, one of: Seed, Early, Growth, Late, Multi-stage, or Unknown)
"partners" (list of strings, GP/managing partner names)
"portfolio_companies" (list of strings, company names they have invested in)
"notable_exits" (string, any exits or IPOs, or "Unknown")
"regional_investments" (list of objects, each with: {{"company": str, "amount": str, "currency": str, "round_type": str, "date": str}})

CRITICAL: For regional_investments in {_CFG.region_name}, extract the EXACT amount, round type, and date.
Only include investments you can verify from the search results. Do NOT fabricate data.

If you cannot find evidence, output "Unknown" or an empty list. Output ONLY the raw JSON.
"""
    return _call_llm(prompt, model_type="fast")


def _investor_pass_2_enhanced(facts, investor_name):
    """Pass 2: Classification, validation, and structuring."""
    prompt = f"""You are an Investor Classification Agent. Given facts about an investor, classify and structure them.

Investor Name: {investor_name}
Extracted Facts:
{json.dumps(facts, indent=2, ensure_ascii=False)}

Task 1: Confirm the investor_type. Pick EXACTLY one:
"VC", "Angel", "Corporate", "Accelerator", "PE", "Government", "Family Office", "Unknown"

Task 2: Format the fund_size. If a number is mentioned, format as {_CFG.funding_format_description}. If unknown, "Unknown".

Task 3: List the focus areas as a clean comma-separated string (e.g., "AI, Fintech, SaaS, Deep Tech").

Task 4: {_CFG.investor_domestic_instruction}

Task 5: Classify stage_focus as one of: Seed, Early, Growth, Late, Multi-stage, Unknown.

Task 6: Estimate tech_proximity (0-100): How close is this investor to {_CFG.technology_domain}?
- 90-100: Dedicated {_CFG.technology_domain} fund or {_CFG.technology_domain}-only investor
- 70-89: Strong {_CFG.technology_domain} focus with some non-{_CFG.technology_domain} investments
- 40-69: General tech investor with some {_CFG.technology_domain} deals
- 10-39: Sector-agnostic with occasional {_CFG.technology_domain} investments
- 0-9: No {_CFG.technology_domain} focus

Task 7: Determine investor_origin based on headquarters location:
- "domestic" if headquartered in {_CFG.region_name}
- "international" if headquartered outside {_CFG.region_name} but active in the region

Output JSON:
"investor_type" (string, from the list above)
"fund_size" (string, formatted or "Unknown")
"focus_areas" (string, comma-separated)
"domestic_portfolio" (list of strings, {_CFG.region_name} company names only)
"total_portfolio_count" (integer, estimated total portfolio size)
"stage_focus" (string)
"tech_proximity" (integer 0-100)
"investor_origin" (string: "domestic" or "international")

Output ONLY the raw JSON.
"""
    return _call_llm(prompt)


def _investor_pass_3_enhanced(facts, classification, investor_name):
    """Pass 3: Final formatting, normalization, and audit."""
    prompt = f"""You are a Data Quality Auditor for investor profiles.

Investor: {investor_name}
Facts: {json.dumps(facts, indent=2, ensure_ascii=False)}
Classification: {json.dumps(classification, indent=2, ensure_ascii=False)}

Rules:
1. {_CFG.investor_character_rules}
2. Fund size in {_CFG.funding_format_description} or "Unknown".
3. Location should be "City, Country" format.
4. Focus areas as clean comma-separated string.
5. regional_investments: validate amounts and format consistently.
6. Partners: list of proper names with correct characters.

Output a clean JSON:
"investor_type" (string)
"website" (string or "Unknown")
"location" (string, "City, Country" or "Unknown")
"founded_year" (integer or null)
"fund_size" (string)
"fund_vintage" (string or "Unknown")
"focus_areas" (string)
"stage_focus" (string)
"partners" (string, comma-separated names)
"portfolio_companies" (list of strings, all companies)
"domestic_portfolio" (list of strings, {_CFG.region_name} companies only)
"portfolio_count" (integer)
"notable_investments" (string, 5 most notable comma-separated, or "Unknown")
"regional_investments" (list of objects: {{"company": str, "amount": str, "currency": str, "round_type": str, "date": str}})
"tech_proximity" (integer 0-100)

Output ONLY the raw JSON.
"""
    return _call_llm(prompt, model_type="fast")


def enrich_single_investor(conn, investor_id, investor_name):
    """Run the full enhanced three-pass enrichment for one investor."""
    print(f"\n  [{investor_id}] Enriching: {investor_name}")

    # Gather context
    search_context, scraped_text = _gather_investor_context(investor_name)
    if not search_context.strip() and not scraped_text.strip():
        print(f"    [Skip] No search results for {investor_name}")
        return False

    # Pass 1: Fact Extraction
    facts = _investor_pass_1_enhanced(investor_name, search_context, scraped_text)
    if not facts:
        print(f"    [Pass 1] Researcher returned nothing.")
        return False

    # Gate: check if entity is actually an investor
    if facts.get("is_investor") is False:
        print(f"    [Gate] '{investor_name}' is NOT an investor — skipping enrichment")
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE investors SET investor_type = 'Not Investor', data_confidence = 5 WHERE investor_id = ?",
            (investor_id,)
        )
        conn.commit()
        return False
    print(f"    [Pass 1] Facts extracted.")

    # Pass 2: Classification
    classification = _investor_pass_2_enhanced(facts, investor_name)
    if not classification:
        print(f"    [Pass 2] Classifier returned nothing.")
        return False
    print(f"    [Pass 2] Type: {classification.get('investor_type')} | Portfolio: {classification.get('total_portfolio_count', '?')}")

    # Pass 3: Formatting
    result = _investor_pass_3_enhanced(facts, classification, investor_name)
    if not result:
        print(f"    [Pass 3] Auditor returned nothing.")
        return False
    print(f"    [Pass 3] Profile complete.")

    # Write to DB
    cursor = conn.cursor()
    now = get_utc_now()

    # Update main investor fields
    field_map = {
        'investor_type': result.get('investor_type'),
        'website': result.get('website'),
        'location': result.get('location'),
        'founded_year': result.get('founded_year'),
        'fund_size': result.get('fund_size'),
        'focus_areas': result.get('focus_areas'),
        'portfolio_count': result.get('portfolio_count'),
        'notable_investments': result.get('notable_investments'),
        'partners': result.get('partners'),
        'fund_vintage': result.get('fund_vintage'),
        'stage_focus': result.get('stage_focus'),
        'tech_proximity': result.get('tech_proximity'),
    }

    for field, value in field_map.items():
        if value and str(value).strip().lower() not in ('unknown', 'none', 'null', ''):
            agent_update_field(cursor, 'investor', investor_id, 'M4_investor_enrichment', 70, field, value)

    # Store regional investments as JSON
    regional_inv = result.get('regional_investments', [])
    if regional_inv and isinstance(regional_inv, list) and len(regional_inv) > 0:
        cursor.execute("UPDATE investors SET turkish_investments = ? WHERE investor_id = ?",
                       (json.dumps(regional_inv, ensure_ascii=False), investor_id))

    # Store enrichment sources
    sources = []
    if result.get('website') and result['website'] != 'Unknown':
        sources.append(result['website'])
    cursor.execute("UPDATE investors SET enrichment_sources = ?, last_enriched = ? WHERE investor_id = ?",
                   (json.dumps(sources, ensure_ascii=False), now, investor_id))

    # Calculate and update data_confidence
    confidence = 0
    if result.get('website') and result['website'] != 'Unknown':
        confidence += 20
    if result.get('investor_type') and result['investor_type'] != 'Unknown':
        confidence += 15
    if result.get('location') and result['location'] != 'Unknown':
        confidence += 15
    if result.get('fund_size') and result['fund_size'] != 'Unknown':
        confidence += 15
    if result.get('focus_areas') and result['focus_areas'] != 'Unknown':
        confidence += 10
    if result.get('partners') and result['partners'] != 'Unknown':
        confidence += 10
    if regional_inv:
        confidence += 15
    confidence = min(confidence, 100)

    cursor.execute("UPDATE investors SET data_confidence = ? WHERE investor_id = ?",
                   (confidence, investor_id))

    conn.commit()
    print(f"    [DB] Updated. Confidence: {confidence}")

    # Cross-reference portfolio companies
    portfolio = result.get('domestic_portfolio', [])
    if portfolio:
        discovered = discover_portfolio_startups(conn, investor_id, investor_name, portfolio)
        if discovered > 0:
            print(f"    [Portfolio] Discovered {discovered} new startups from portfolio.")
            fuzzy_matcher.invalidate()

    # Create investment records from regional_investments
    if regional_inv:
        _create_investment_records(conn, investor_id, regional_inv)

    return True


def _create_investment_records(conn, investor_id, regional_investments):
    """Create investment records from extracted regional investment data."""
    cursor = conn.cursor()
    created = 0

    for inv in regional_investments:
        if not isinstance(inv, dict):
            continue
        company = inv.get('company', '').strip()
        if not company or len(company) < 2:
            continue

        # Find matching startup
        match = fuzzy_matcher.find_startup(conn, company)
        if not match:
            continue

        startup_id = match[0]
        amount = inv.get('amount', '')
        currency = inv.get('currency', 'Unknown')
        round_type = inv.get('round_type', 'NA')
        date = inv.get('date', '')

        # Check for existing link
        existing = cursor.execute(
            "SELECT id FROM investments WHERE startup_id = ? AND investor_id = ?",
            (startup_id, investor_id)
        ).fetchone()
        if existing:
            continue

        # Normalize amount
        amount_usd = None
        if amount and amount != 'Unknown':
            _, amount_usd, _ = convert_to_usd(amount, date if date != 'Unknown' else None)

        try:
            cursor.execute('''
                INSERT INTO investments (startup_id, investor_id, round_type, amount, currency,
                    amount_usd, investment_date, source_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (startup_id, investor_id, round_type, amount, currency,
                  amount_usd, date if date and date != 'Unknown' else None,
                  'investor_enrichment'))
            created += 1
        except Exception:
            pass

    if created > 0:
        conn.commit()
        print(f"    [Investments] Created {created} investment records.")


# ─────────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Module 4: Investor Enrichment")
    parser.add_argument("--name", help="Enrich a specific investor by name")
    parser.add_argument("--batch", type=int, default=20, help="Batch size (default: 20)")
    parser.add_argument("--type", dest="investor_type", help="Filter by investor type (VC, Angel, Corporate, etc.)")
    args = parser.parse_args()

    conn = get_db_connection(DB_NAME)
    conn.execute("PRAGMA busy_timeout = 30000")
    clear_search_cache()

    print("=" * 60)
    print("  Module 4: Investor Enrichment")
    print("=" * 60)

    cursor = conn.cursor()

    if args.name:
        # Enrich specific investor
        row = cursor.execute(
            "SELECT investor_id, investor_name FROM investors WHERE investor_name LIKE ?",
            (f"%{args.name}%",)
        ).fetchone()
        if not row:
            print(f"  Investor not found: {args.name}")
            conn.close()
            return
        enrich_single_investor(conn, row[0], row[1])
    else:
        # Batch enrichment
        params = []
        where_clause = "WHERE (data_confidence IS NULL OR data_confidence < 30)"
        if args.investor_type:
            where_clause += " AND investor_type = ?"
            params.append(args.investor_type)
        params.append(args.batch)

        rows = cursor.execute(f"""
            SELECT investor_id, investor_name FROM investors
            {where_clause}
            ORDER BY COALESCE(data_confidence, 0) ASC, last_enriched ASC NULLS FIRST
            LIMIT ?
        """, params).fetchall()

        print(f"  Found {len(rows)} investors to enrich (batch={args.batch})")
        enriched = 0
        for inv_id, inv_name in rows:
            success = enrich_single_investor(conn, inv_id, inv_name)
            if success:
                enriched += 1
            time.sleep(1)  # Rate limit

        print(f"\n{'=' * 60}")
        print(f"  SUMMARY: Enriched {enriched}/{len(rows)} investors")
        print("=" * 60)

    # Log scan
    cursor.execute('''
        INSERT INTO scan_log (module_name, scan_start, scan_end, status)
        VALUES ('M4_investor_enrichment', ?, ?, 'completed')
    ''', (get_utc_now(), get_utc_now()))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
