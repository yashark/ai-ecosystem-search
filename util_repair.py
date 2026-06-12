"""
One-Time Repair Script — Run ONCE after deploying the agent system improvements.

Repair Pass 1: Re-match orphan news_mentions (startup_id IS NULL)
Repair Pass 2: Re-link multi-company articles (articles with multiple companies but only one startup linked)
Repair Pass 3: Reset single-pass enriched records so three-pass picks them up on next refresh
"""
import sqlite3
import json
import hashlib
import time
from datetime import datetime
from rapidfuzz import fuzz

from agent_core import (
    get_utc_now, agent_update_field, get_db_connection, _call_llm
)
from agent_news import fuzzy_match_startup, fuzzy_match_investor, invalidate_indexes

DB_NAME = "Master.db"


def _extract_companies_from_snippet(snippet, source_name, source_lang):
    """Re-extract company names and investors from a news snippet via LLM."""
    prompt = f"""You are a news extraction agent for the Turkish Startup Ecosystem.
Analyze this news snippet and extract ONLY the company and investor names.

Source: {source_name}
Language: {source_lang}
Text: {snippet}

Output ONLY a valid JSON object with these keys:
"companies" (list of strings, Turkish AI startup names mentioned)
"investors" (list of strings, investor names mentioned)

Output ONLY the raw JSON.
"""
    try:
        result = _call_llm(prompt, model_type="fast")
        # _call_llm / _call_mlx already returns a parsed dict (or None)
        if isinstance(result, dict):
            return result
        return None
    except Exception as e:
        print(f"  LLM Error: {e}")
        return None


def repair_pass_1_orphan_mentions(conn):
    """Re-match news_mentions where startup_id IS NULL — these are orphaned mentions 
    that couldn't be matched when originally processed."""
    cursor = conn.cursor()
    
    orphans = cursor.execute("""
        SELECT id, source_url, summary, source_name, source_language, headline
        FROM news_mentions 
        WHERE startup_id IS NULL AND summary IS NOT NULL AND summary != ''
    """).fetchall()
    
    if not orphans:
        print("[Pass 1] No orphan news mentions found.")
        return
    
    print(f"[Pass 1] Found {len(orphans)} orphan news mentions. Re-matching...")
    invalidate_indexes()  # Fresh index
    fixed = 0
    
    for mention_id, source_url, summary, source_name, source_lang, headline in orphans:
        # Try to match using the headline + summary as context
        text = (headline or "") + " " + (summary or "")
        extracted = _extract_companies_from_snippet(text, source_name or "Unknown", source_lang or "en")
        
        if not extracted:
            continue
        
        companies = extracted.get("companies", [])
        investors = extracted.get("investors", [])
        
        matched_startup = None
        for company in companies:
            invalid_names = {'unknown', '[company name]', 'company', 'startup', 'n/a', 'none', ''}
            if company.strip().lower() in invalid_names:
                continue
            match = fuzzy_match_startup(conn, company)
            if match and (len(match) < 3 or match[2] != 'Blacklisted'):
                matched_startup = match[0]
                print(f"  [{mention_id}] Matched: {company} → {match[1]} (ID {match[0]})")
                break
        
        matched_investor = None
        for inv_name in investors:
            inv_match = fuzzy_match_investor(conn, inv_name)
            if inv_match:
                matched_investor = inv_match[0]
                break
        
        if matched_startup or matched_investor:
            updates = []
            params = []
            if matched_startup:
                updates.append("startup_id = ?")
                params.append(matched_startup)
            if matched_investor:
                updates.append("investor_id = ?")
                params.append(matched_investor)
            params.append(mention_id)
            
            cursor.execute(f"UPDATE news_mentions SET {', '.join(updates)} WHERE id = ?", params)
            fixed += 1
    
    conn.commit()
    print(f"[Pass 1] Fixed {fixed}/{len(orphans)} orphan mentions.\n")


def repair_pass_2_multi_company_articles(conn):
    """Find articles that mention multiple companies but only have one startup linked.
    Re-extract and create additional news_mentions and investment links."""
    cursor = conn.cursor()
    
    # Get all news mentions that have a summary (so we can re-extract)
    mentions = cursor.execute("""
        SELECT id, startup_id, investor_id, source_url, summary, source_name, 
               source_language, headline, event_type, url_hash
        FROM news_mentions 
        WHERE summary IS NOT NULL AND summary != ''
        ORDER BY id
    """).fetchall()
    
    if not mentions:
        print("[Pass 2] No news mentions to check.")
        return
    
    print(f"[Pass 2] Checking {len(mentions)} news mentions for multi-company articles...")
    invalidate_indexes()
    new_links = 0
    
    for row in mentions:
        (mention_id, existing_startup_id, existing_investor_id, source_url, 
         summary, source_name, source_lang, headline, event_type, url_hash) = row
        
        text = (headline or "") + " " + (summary or "")
        extracted = _extract_companies_from_snippet(text, source_name or "Unknown", source_lang or "en")
        
        if not extracted:
            continue
        
        companies = extracted.get("companies", [])
        if len(companies) <= 1:
            continue  # Single-company article, already handled correctly
        
        # We have a multi-company article — check which startups are NOT yet linked
        for company in companies:
            invalid_names = {'unknown', '[company name]', 'company', 'startup', 'n/a', 'none', ''}
            if company.strip().lower() in invalid_names:
                continue
                
            match = fuzzy_match_startup(conn, company)
            if not match or (len(match) > 2 and match[2] == 'Blacklisted'):
                continue
            
            sid = match[0]
            if sid == existing_startup_id:
                continue  # Already linked
            
            # Check if a news mention already exists for this startup + URL combo
            extra_hash = hashlib.md5(f"{source_url}__startup_{sid}".encode()).hexdigest()
            already_exists = cursor.execute(
                "SELECT id FROM news_mentions WHERE url_hash = ?", (extra_hash,)
            ).fetchone()
            
            if already_exists:
                continue
            
            # Create the missing news mention link
            try:
                cursor.execute('''
                    INSERT INTO news_mentions (startup_id, investor_id, headline, url_hash, source_url,
                        source_name, source_language, published_date, summary, event_type, sentiment, scanned_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    sid, existing_investor_id, headline, extra_hash, source_url,
                    source_name, source_lang, "", summary, event_type, "neutral",
                    get_utc_now()
                ))
                new_links += 1
                print(f"  [{mention_id}] Added link: {company} (ID {sid}) to article")
            except Exception:
                pass  # Unique constraint
    
    conn.commit()
    print(f"[Pass 2] Created {new_links} additional article links.\n")


def repair_pass_3_reset_single_pass_records(conn):
    """Reset last_updated on startups that were only enriched by the old single-pass 
    execute_8b_workflow, so the Tier 3 refresher picks them up with three_pass_enrichment."""
    cursor = conn.cursor()
    
    # Find startups where the most recent change_log entry was from a single-pass agent
    # and they haven't been through three-pass yet (no Pass 1/2/3 markers)
    # We identify single-pass records by looking at startups enriched by agent_updater 
    # that have data but were never refreshed by three-pass
    
    # Simple heuristic: startups with tech_proximity > 0 (verified)
    # and business_model IS NULL or 'Unknown' (three-pass populates this, single-pass doesn't)
    single_pass_records = cursor.execute("""
        SELECT id, company_name, last_updated
        FROM startups
        WHERE tech_proximity > 0
          AND Status != 'Blacklisted'
          AND (business_model IS NULL OR business_model = '' OR business_model = 'Unknown')
    """).fetchall()
    
    if not single_pass_records:
        print("[Pass 3] No single-pass records to reset.")
        return
    
    print(f"[Pass 3] Found {len(single_pass_records)} startups enriched by single-pass only.")
    print("         Resetting last_updated so Tier 3 refresh picks them up with three-pass chain...")
    
    for sid, name, last_updated in single_pass_records:
        cursor.execute("UPDATE startups SET last_updated = NULL WHERE id = ?", (sid,))
    
    conn.commit()
    print(f"[Pass 3] Reset {len(single_pass_records)} records for re-enrichment.\n")


def repair_pass_4_orphan_investments(conn):
    """Re-match investments where startup_id references a non-existent startup.
    Attempts fuzzy re-match via related news_mention; flags unresolvable ones."""
    cursor = conn.cursor()

    orphans = cursor.execute("""
        SELECT i.id, i.startup_id, i.investor_id, i.news_mention_id,
               i.round_type, i.amount, i.currency
        FROM investments i
        LEFT JOIN startups s ON i.startup_id = s.id
        WHERE s.id IS NULL AND i.startup_id IS NOT NULL
    """).fetchall()

    if not orphans:
        print("[Pass 4] No orphan investments found.")
        return

    print(f"[Pass 4] Found {len(orphans)} orphan investments. Attempting re-match...")
    invalidate_indexes()
    fixed = 0
    flagged = 0

    for inv_id, old_sid, investor_id, nm_id, round_type, amount, currency in orphans:
        matched_sid = None

        # Strategy 1: Look up linked news_mention for company names
        if nm_id:
            nm_row = cursor.execute(
                "SELECT headline, summary FROM news_mentions WHERE id = ?", (nm_id,)
            ).fetchone()
            if nm_row:
                text = (nm_row[0] or "") + " " + (nm_row[1] or "")
                extracted = _extract_companies_from_snippet(text, "repair", "tr")
                if extracted:
                    for company in extracted.get("companies", []):
                        if company.strip().lower() in {'unknown', '', 'n/a', 'none'}:
                            continue
                        match = fuzzy_match_startup(conn, company)
                        if match and (len(match) < 3 or match[2] != 'Blacklisted'):
                            matched_sid = match[0]
                            print(f"  [{inv_id}] Matched via news: {company} → {match[1]} (ID {match[0]})")
                            break

        if matched_sid:
            cursor.execute("UPDATE investments SET startup_id = ? WHERE id = ?", (matched_sid, inv_id))
            fixed += 1
        else:
            # Flag as unresolvable (-1) rather than deleting
            cursor.execute("UPDATE investments SET startup_id = -1 WHERE id = ?", (inv_id,))
            flagged += 1

    conn.commit()
    print(f"[Pass 4] Fixed {fixed}, flagged {flagged} (startup_id=-1) out of {len(orphans)} orphans.\n")


def main():
    conn = get_db_connection(DB_NAME)
    conn.execute("PRAGMA busy_timeout = 30000")  # Wait up to 30s if locked
    print("=" * 60)
    print("  One-Time Repair Script")
    print("=" * 60)

    print("\n--- Pass 1: Re-match Orphan News Mentions ---")
    repair_pass_1_orphan_mentions(conn)

    print("--- Pass 2: Re-link Multi-Company Articles ---")
    repair_pass_2_multi_company_articles(conn)

    print("--- Pass 3: Reset Single-Pass Records for Three-Pass Re-enrichment ---")
    repair_pass_3_reset_single_pass_records(conn)

    print("--- Pass 4: Re-match Orphan Investments ---")
    repair_pass_4_orphan_investments(conn)

    print("=" * 60)
    print("  Repair complete! Run 'python3 main.py --all' for the")
    print("  refresher to pick up reset records with three-pass chain.")
    print("=" * 60)
    conn.close()


if __name__ == "__main__":
    main()
