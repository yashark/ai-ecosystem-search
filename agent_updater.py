"""
Module 5: Recurring Updater
Process A: News delta scan (scan_end → now).
Process B: Refresh stale startup/investor records on schedule.
"""
import sqlite3
import json
import time
from datetime import datetime, timedelta

from data_guard import is_valid_investor_name, compute_initial_confidence, normalize_city
from agent_core import (
    search_with_rotation, scrape_website_text, OLLAMA_API_URL, MODELS,
    agent_update_field, get_utc_now, calculate_confidence, check_domain_vitality,
    execute_8b_workflow, execute_delta_workflow, validate_sector_tag,
    enrich_investor, clear_search_cache, three_pass_enrichment,
    detect_entity_type_heuristic, search_escalation, find_crunchbase_url,
    is_relevant_result, normalize_turkish, normalize_funding_amount,
    seed_tag_keywords, validate_classification_accuracy,
    learn_keywords_from_description, get_top_candidate_tags, _call_llm,
    get_db_connection
)

DB_NAME = "Master.db"

# Tiered Refresh SLA
TIER_1_DAYS = 7      # Vitality check only
TIER_2_DAYS = 14     # Delta LLM for missing fields
TIER_3_DAYS = 30     # Full three-pass re-enrichment (temporarily lowered from 90)
INVESTOR_REFRESH_DAYS = 30

# Tag validation thresholds (keyword-index driven)
# - If the LLM-chosen tag scores below LOW, we try a deterministic keyword-index auto-pick first.
# - Only if that auto-pick is still weak do we call the LLM to repick.
TAG_SCORE_LOW_THRESHOLD = 0.15
TAG_SCORE_AUTO_ACCEPT_THRESHOLD = 0.45

def parse_category(category_value):
    """Parse "<Sector> | <Tag>" into (sector, tag)."""
    if not category_value or str(category_value).strip().lower() in ["", "null", "none", "unknown"]:
        return None, None
    parts = str(category_value).split(" | ", 1)
    if len(parts) == 2:
        sector = parts[0].strip() or None
        tag = parts[1].strip() or None
        return sector, tag
    return None, None


def build_category(sector, tag):
    """Build "<Sector> | <Tag>" or None if incomplete."""
    if not sector or not tag:
        return None
    s = str(sector).strip()
    t = str(tag).strip()
    if not s or not t:
        return None
    return f"{s} | {t}"

# --- Shared Helpers ---

def is_missing(val):
    """Check if a DB value is effectively missing/empty."""
    return not val or str(val).strip().lower() in ['nan', 'unknown', 'none', '']

def update_if_new(cursor, entity_id, field, existing_val, new_val):
    """Write new_val via agent_update_field only if existing is missing and new is real."""
    if is_missing(existing_val) and new_val and new_val != 'Unknown':
        agent_update_field(cursor, "startup", entity_id, "agent_updater", 60, field, new_val)

def process_a_news_delta(conn):
    """Scan news articles published since the last successful scan."""
    cursor = conn.cursor()

    # Find the last successful scan end time
    row = cursor.execute(
        "SELECT scan_end FROM scan_log WHERE module_name LIKE 'module4_news_%' AND status = 'completed' ORDER BY scan_end DESC LIMIT 1"
    ).fetchone()

    if row:
        last_scan = row[0]
        print(f"Last successful news scan: {last_scan}")
    else:
        from datetime import timezone
        last_scan = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat().replace("+00:00", "Z")
        print(f"No previous scan found. Defaulting to 12 months back: {last_scan}")

    # Import and run Module 4's main logic with 10 results (recurring scan)
    print("\nRunning news delta scan (10 results per query)...")
    import agent_news
    agent_news.main(max_results=10)

def sync_startup_investors(conn, startup_id, investors_str, funding_str):
    """Parses a comma-separated investor string, creates missing investors, and links them via the investments table."""
    if not investors_str or str(investors_str).strip().lower() in ['nan', 'unknown', 'none', '']:
        return

    cursor = conn.cursor()
    investor_names = [n.strip() for n in investors_str.split(",") if n.strip()]
    
    for inv_name in investor_names:
        if not is_valid_investor_name(inv_name):
            continue
        # Get or create investor
        row = cursor.execute("SELECT investor_id FROM investors WHERE investor_name = ?", (inv_name,)).fetchone()
        if row:
            inv_id = row[0]
        else:
            cursor.execute("INSERT INTO investors (investor_name, processed_at) VALUES (?, ?)", (inv_name, get_utc_now()))
            inv_id = cursor.lastrowid
            
        # Parse funding amount and currency using shared normalizer
        raw_funding = funding_str if funding_str else "Unknown"
        norm_val, norm_fmt, norm_cur = normalize_funding_amount(raw_funding)
        amount = norm_fmt if norm_fmt else raw_funding
        currency = norm_cur if norm_cur and norm_cur != "Unknown" else "Unknown"
        if currency == "Unknown" and raw_funding and raw_funding != "Unknown":
            # Use proper currency detection instead of naive split
            from agent_core import detect_currency as _detect_cur
            detected = _detect_cur(raw_funding)
            if detected and detected != "Unknown":
                currency = detected

        # Get or Create Investment Link
        existing_link = cursor.execute("SELECT id FROM investments WHERE startup_id = ? AND investor_id = ?", (startup_id, inv_id)).fetchone()
        if not existing_link:
            cursor.execute("INSERT INTO investments (startup_id, investor_id, amount, currency, round_type, investment_date) VALUES (?, ?, ?, ?, ?, ?)", (startup_id, inv_id, amount, currency, 'NA', None))
        else:
            cursor.execute("UPDATE investments SET amount = ?, currency = ? WHERE id = ?", (amount, currency, existing_link[0]))

        # Dual-write to ecosystem_activities
        try:
            import hashlib as _hl
            _ea_hash = _hl.md5(f"updater::inv::{startup_id}::{inv_id}::{amount}".encode()).hexdigest()
            cursor.execute('''
                INSERT OR IGNORE INTO ecosystem_activities (
                    activity_type, headline, source_name, source_type,
                    startup_id, investor_id, amount, currency,
                    url_hash, data_confidence, needs_enrichment, scanned_at
                ) VALUES (
                    'investment', ?, 'agent_updater', 'enrichment',
                    ?, ?, ?, ?,
                    ?, 50, 1, ?
                )
            ''', (
                f"Investment link: startup {startup_id} ← {inv_name}",
                startup_id, inv_id, amount, currency,
                _ea_hash, get_utc_now()
            ))
            _ea_id = cursor.lastrowid
            if _ea_id:
                cursor.execute(
                    "INSERT OR IGNORE INTO ecosystem_activity_entities (activity_id, entity_type, entity_id, role) VALUES (?, 'startup', ?, 'recipient')",
                    (_ea_id, startup_id)
                )
                cursor.execute(
                    "INSERT OR IGNORE INTO ecosystem_activity_entities (activity_id, entity_type, entity_id, role) VALUES (?, 'investor', ?, 'actor')",
                    (_ea_id, inv_id)
                )
        except Exception:
            pass  # Don't block on ecosystem write failure

def process_b_refresh_unverified_startups(conn):
    """The Master Gatekeeper: Hunts for any unverified skeleton startups and executes full AI validation."""
    cursor = conn.cursor()
    # Find startups with NULL ai_proximity (meaning they bypassed Agent 1)
    unverified = cursor.execute("SELECT id, company_name, website, description FROM startups WHERE (tech_proximity IS NULL OR tech_proximity = 0) AND (data_confidence IS NULL OR data_confidence >= 20)").fetchall()
    
    if not unverified:
        print("No unverified skeleton startups found.")
        return
        
    print(f"\n[Gatekeeper] Found {len(unverified)} unverified startups. Commencing rigorous AI evaluation...")

    # Pipeline: prefetch search+scrape for next startup while LLM processes current one
    from concurrent.futures import ThreadPoolExecutor

    def _prefetch_context(name, website):
        sc = search_with_rotation(f'"{name}" startup turkey AI')
        ws = website
        if not ws and sc and "Url: " in sc:
            ws = sc.split("Url: ")[1].split("\n")[0].strip()
        wc = scrape_website_text(ws) if ws else ""
        return sc, ws, wc

    prefetch_pool = ThreadPoolExecutor(max_workers=1)
    prefetched_future = None

    # Kick off prefetch for the first startup
    if unverified:
        _, name0, website0, _ = unverified[0]
        prefetched_future = prefetch_pool.submit(_prefetch_context, name0, website0)

    for idx, (sid, name, website, desc) in enumerate(unverified):
        print(f"  Evaluating {name}...")

        # 1. Gather Context (from prefetch or direct)
        if prefetched_future is not None:
            search_context, website, website_context = prefetched_future.result()
            prefetched_future = None
        else:
            search_context, website, website_context = _prefetch_context(name, website)

        # Prefetch NEXT startup while we LLM-process current one
        if idx + 1 < len(unverified):
            _, next_name, next_website, _ = unverified[idx + 1]
            prefetched_future = prefetch_pool.submit(_prefetch_context, next_name, next_website)

        base_data = {"name": name, "description": desc or ""}
        
        # 2. Run the Full Three-Pass AI Verification (higher accuracy than single-pass)
        enriched = three_pass_enrichment(base_data, search_context, website_context)
        if enriched:
            # Normalize city in enrichment output
            if enriched.get('city'):
                enriched['city'] = normalize_city(enriched['city'])

            # Compute minimum confidence from filled fields (Issue 4: zero-confidence fix)
            _min_confidence = compute_initial_confidence(enriched)

            desc_new = enriched.get('description', '').lower()
            ai_use = enriched.get('AI_Use_Case', '').lower()
            tech = enriched.get('Tech_Mentioned', '').lower() + " " + enriched.get('Tech_Assumed', '').lower()
            
            ai_prox = 0
            ai_keywords = [' ai ', 'artificial intelligence', 'machine learning', ' ml ', 'deep learning', 'nlp', 'computer vision', 'neural network', 'llm', 'generative ai']
            combined_text = desc_new + " " + ai_use + " " + tech
            
            if any(kw in combined_text for kw in ai_keywords):
                ai_prox = 80
                if 'core' in combined_text or 'driven' in combined_text or 'platform' in ai_use:
                    ai_prox = 100
            elif tech != 'none' and len(tech) > 5:
                ai_prox = 30
                
            # Layer 2 Geographic Strict Filtering (MUST come before entity cataloguing)
            extracted_city = enriched.get('city', '').lower()
            if 'foreign' in extracted_city:
                print(f"    [!] No Turkish Physical Address Detected by LLM. Forcing PURGE.")
                ai_prox = 0
                # Don't catalogue foreign entities — fall through to blacklist
                cursor.execute("UPDATE startups SET Status = 'Blacklisted', tech_proximity = 0, last_updated = ? WHERE id = ?", (get_utc_now(), sid))
                cursor.execute("DELETE FROM change_log WHERE entity_id = ? AND entity_type = 'startup'", (sid,))
                cursor.execute("DELETE FROM news_mentions WHERE startup_id = ?", (sid,))
                cursor.execute("DELETE FROM investments WHERE startup_id = ?", (sid,))
                conn.commit()
                continue

            # Layer 3 Entity Type Filtering (catalogue non-startups in ecosystem_entities)
            entity_type = enriched.get('entity_type', 'Startup').strip()
            _INVALID_NAMES = {'unknown', 'none', 'n/a', '', 'startup', 'company', 'the company'}
            
            # Type-indicator keywords: if entity name/description contains these, 
            # use them to auto-correct similar entities later
            _TYPE_INDICATORS = {
                'University': ['university', 'üniversite', 'üniversitesi'],
                'Research Lab': ['research lab', 'araştırma merkezi', 'research center', 'research institute'],
                'Bank': ['bank', 'bankası', 'banka'],
                'Foundation': ['foundation', 'vakfı', 'vakıf'],
                'NGO': ['ngo', 'non-profit', 'derneği', 'dernek'],
                'Chamber': ['chamber', 'odası', 'oda'],
                'Municipality': ['municipality', 'belediye', 'belediyesi'],
                'Ministry': ['ministry', 'bakanlık', 'bakanlığı'],
                'Association': ['association', 'birliği', 'birlik'],
            }
            
            if entity_type.lower() not in ['startup', '']:
                # Normalize entity type to Title Case
                normalized_type = entity_type.strip().title()
                
                # Validate entity name is real (not junk from LLM)
                name_lower = name.strip().lower()
                is_junk = (
                    name_lower in _INVALID_NAMES
                    or len(name.strip()) < 3
                    or len(name.strip()) > 80
                    or name.strip()[0].isdigit()
                    or any(w in name_lower for w in ['companies', 'investments', 'portfolio', 'stores', 'restaurants', 
                                                      'channels', 'stations', 'dealerships', 'acquired', 'months'])
                )
                if is_junk:
                    print(f"    [Skip] Entity name '{name}' looks like junk. Blacklisting.")
                    cursor.execute("UPDATE startups SET Status = 'Blacklisted', tech_proximity = 0, last_updated = ? WHERE id = ?", (get_utc_now(), sid))
                    conn.commit()
                    continue

                print(f"    [Catalogued] Entity classified as '{normalized_type}'. Moving to ecosystem_entities table.")
                try:
                    cursor.execute('''
                        INSERT OR IGNORE INTO ecosystem_entities (entity_name, entity_type, website, city, description, first_seen, source)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (name, normalized_type, enriched.get('website', ''), enriched.get('city', ''), enriched.get('description', ''), get_utc_now(), 'agent_4_gatekeeper'))
                    cursor.execute("DELETE FROM startups WHERE id = ?", (sid,))
                    conn.commit()
                    
                    # Auto-correct: find similar existing entities that may be miscategorized
                    # Priority rules: more specific indicators take precedence
                    _PRIORITY_ORDER = ['Bank', 'University', 'Ministry', 'Municipality', 'Research Lab',
                                       'Chamber', 'Association', 'Foundation', 'NGO']
                    
                    for correct_type in _PRIORITY_ORDER:
                        indicators = _TYPE_INDICATORS.get(correct_type, [])
                        if any(ind in name_lower for ind in indicators):
                            for ind in indicators:
                                mismatched = cursor.execute("""
                                    SELECT id, entity_name, entity_type FROM ecosystem_entities
                                    WHERE LOWER(entity_name) LIKE ? AND entity_type != ?
                                """, (f'%{ind}%', correct_type)).fetchall()
                                for mid, mname, mtype in mismatched:
                                    # Skip if a higher-priority indicator also matches
                                    mname_lower = mname.lower()
                                    skip = False
                                    for higher_type in _PRIORITY_ORDER:
                                        if higher_type == correct_type:
                                            break
                                        higher_inds = _TYPE_INDICATORS.get(higher_type, [])
                                        if any(hi in mname_lower for hi in higher_inds):
                                            skip = True
                                            break
                                    if not skip:
                                        print(f"    [Auto-fix] '{mname}' was '{mtype}' → '{correct_type}'")
                                        cursor.execute("UPDATE ecosystem_entities SET entity_type = ? WHERE id = ?", (correct_type, mid))
                            conn.commit()
                            break
                            
                except Exception as e:
                    print(f"    [Error] Failed to catalogue entity: {e}")
                continue

            # Non-Tech Business Strict Filtering
            if 'none none' in tech and ai_prox < 80:
                print(f"    [!] No Technology Stack Detected by LLM. Forcing PURGE.")
                ai_prox = 0
                
            if ai_prox > 0:
                print(f"    [PASS] Confirmed AI relevance ({ai_prox}%). Committing profile...")
                # Resolve sector/tag from enrichment result (supports both old and new keys)
                if 'Category' in enriched:
                    _s, _t = parse_category(enriched['Category'])
                    sector, tag = validate_sector_tag(_s or 'Services', _t or 'E-commerce & Retail')
                else:
                    sector, tag = validate_sector_tag(
                        enriched.get('sector') or enriched.get('Industrial_Sector', 'Services'),
                        enriched.get('tag') or enriched.get('Tag_Category', 'E-commerce & Retail')
                    )
                
                # Post-LLM classification validation against keyword index
                tag_desc = enriched.get('description', '') or desc or ''
                tag_score = validate_classification_accuracy(conn, tag, tag_desc)
                if tag_desc and tag_score < TAG_SCORE_LOW_THRESHOLD:
                    print(f"    [Validator] Tag '{tag}' scored {tag_score:.2f} — keyword auto-pick first...")
                    candidates = get_top_candidate_tags(conn, tag_desc, n=5)
                    top_tag = candidates[0] if candidates else tag
                    new_sector, new_tag = validate_sector_tag('', top_tag)
                    top_score = validate_classification_accuracy(conn, new_tag, tag_desc)

                    if top_score >= TAG_SCORE_AUTO_ACCEPT_THRESHOLD:
                        if new_tag != tag:
                            print(f"    [Validator] Auto-picked tag: {tag} → {new_tag} (kw-score {top_score:.2f})")
                        sector, tag = new_sector, new_tag
                    else:
                        print(f"    [Validator] Auto-pick too weak (kw-score {top_score:.2f}) — LLM repick...")
                        repick_prompt = f"""This company's description: "{tag_desc[:400]}"

Pick the BEST tag from these options ONLY:
{chr(10).join(f'{i+1}. {t}' for i, t in enumerate(candidates))}

Output ONLY JSON: {{"tag": "exact tag name"}}"""
                        repick = _call_llm(repick_prompt)
                        if repick and repick.get('tag'):
                            new_sector2, new_tag2 = validate_sector_tag('', repick['tag'])
                            if new_tag2 != tag:
                                print(f"    [Validator] Re-picked: {tag} → {new_tag2}")
                                sector, tag = new_sector2, new_tag2
                
                # Learn from validated classification
                learn_keywords_from_description(conn, tag, tag_desc)
                
                # We use direct SQL instead of agent_update_field because we are formally initializing 
                # this record's verified status for the first time. The baseline loop will pick it up later.
                try:
                    category_val = f"{sector} | {tag}" if sector and tag else None
                    cursor.execute('''
                        UPDATE startups SET 
                            website = ?, description = ?, city = ?, founders = ?,
                            tech_proximity = ?, Status = 'Active',
                            Category = ?,
                            AI_Use_Case = ?,
                            Total_Funding_Formatted = ?, Investors = ?, Tech_Mentioned = ?, Tech_Assumed = ?,
                            data_confidence = ?, last_updated = ?
                        WHERE id = ?
                    ''', (
                        website, enriched.get('description', ''), enriched.get('city', ''), enriched.get('founders', ''),
                        ai_prox, category_val, ai_use,
                        enriched.get('Total_Funding', 'Unknown'), enriched.get('Investors', 'Unknown'),
                        enriched.get('Tech_Mentioned', 'None'), enriched.get('Tech_Assumed', 'None'),
                        max(85, _min_confidence), get_utc_now(), sid
                    ))
                    conn.commit()
                    
                    # Native Agent 4 Investor Sync
                    sync_startup_investors(conn, sid, enriched.get('Investors', 'Unknown'), enriched.get('Total_Funding', 'Unknown'))
                    
                    # Update business_model if available (from three-pass chain)
                    if enriched.get('business_model') and enriched['business_model'] != 'Unknown':
                        cursor.execute("UPDATE startups SET business_model = ? WHERE id = ?", (enriched['business_model'], sid))
                        conn.commit()

                    # Update cloud_relevance if available (from three-pass chain)
                    cloud_rel = enriched.get('cloud_relevance')
                    if cloud_rel and cloud_rel in ('Cloud-Native', 'Cloud-Enabled', 'Cloud-Adjacent', 'Non-Cloud'):
                        cursor.execute("UPDATE startups SET cloud_relevance = ? WHERE id = ?", (cloud_rel, sid))
                        conn.commit()
                    
                except Exception as e:
                    print(f"    [Error] Failed to initialize verified profile: {e}")
            else:
                print(f"    [BLACKLIST] 0% AI Proximity or Foreign Address. Blacklisting {name}.")
                cursor.execute("UPDATE startups SET Status = 'Blacklisted', tech_proximity = 0, last_updated = ? WHERE id = ?", (get_utc_now(), sid))
                cursor.execute("DELETE FROM change_log WHERE entity_id = ? AND entity_type = 'startup'", (sid,))
                cursor.execute("DELETE FROM news_mentions WHERE startup_id = ?", (sid,))
                cursor.execute("DELETE FROM investments WHERE startup_id = ?", (sid,))
                conn.commit()
        else:
            print(f"    [Skip] LLM failed to evaluate {name}.")

    prefetch_pool.shutdown(wait=False)

def process_b_refresh_startups(conn):
    """Tiered refresh: Tier 1 (7d) vitality only, Tier 2 (30d) delta LLM, Tier 3 (90d) full three-pass."""
    from datetime import timezone
    cursor = conn.cursor()
    clear_search_cache()
    now = datetime.now(timezone.utc)
    
    tier_1_cutoff = (now - timedelta(days=TIER_1_DAYS)).isoformat().replace("+00:00", "Z")
    
    stale_startups = cursor.execute('''
        SELECT id, company_name, website, crunchbase_url, description,
               founders, tech_proximity, data_confidence, city,
               Investors, Total_Funding_Formatted, Tech_Mentioned, Tech_Assumed,
               Category, AI_Use_Case, vitality_failures,
               last_updated, cloud_relevance
        FROM startups
        WHERE (last_updated IS NULL OR last_updated < ?)
          AND Status != 'Blacklisted'
          AND (data_confidence IS NULL OR data_confidence >= 20)
        ORDER BY last_updated ASC
    ''', (tier_1_cutoff,)).fetchall()

    if not stale_startups:
        print("No startups need refreshing.")
        return

    print(f"\n{len(stale_startups)} startups due for tiered refresh.")

    # Priority: skeleton records (conf=0) first, then missing founders/year, then by confidence
    def _refresh_priority(r):
        conf = r[7] if r[7] is not None else -1
        founders = r[5]
        has_founders = 1 if founders and founders not in ('', 'Unknown') else 0
        # Lower score = higher priority: skeletons first, then missing data, then low confidence
        return (conf > 0, has_founders, conf)
    stale_startups.sort(key=_refresh_priority)

    for row in stale_startups:
        (sid, name, website, cb_url, desc, founders, ai_pct, conf, city,
         current_inv, current_fund_fmt, current_tech_m, current_tech_a,
         current_category, current_ai_use, current_failures,
         last_updated_str, current_cloud_rel) = row
         
        current_failures = int(current_failures or 0)

        # Category is the only stored classification field.
        # It encodes: "<Industrial_Sector> | <Tag_Category>"
        current_sector, current_tag = parse_category(current_category)
        
        # Determine tier based on age
        if last_updated_str:
            try:
                last_dt = datetime.fromisoformat(last_updated_str.replace("Z", "+00:00"))
                age_days = (now - last_dt).days
            except:
                age_days = 999
        else:
            age_days = 999
        
        if age_days >= TIER_3_DAYS:
            tier = 3
        elif age_days >= TIER_2_DAYS:
            tier = 2
        else:
            tier = 1

        print(f"\n[{sid}] {name} — Tier {tier} refresh (age: {age_days}d)...")

        # --- TIER 1: Vitality check only ---
        negative_signals = 0
        domain_stale = check_domain_vitality(website)
        if domain_stale:
            negative_signals += 1

        li_search = search_with_rotation(f'"{name}" turkey linkedin site:linkedin.com')
        li_stale = True
        if li_search:
            recent_markers = ['2026', '2025', 'month ago', 'weeks ago', 'days ago', 'yesterday', 'today']
            if any(marker in li_search.lower() for marker in recent_markers):
                li_stale = False
        if li_stale:
            negative_signals += 1

        is_stale_signal = (negative_signals == 2)
        new_failures = (current_failures + 1) if is_stale_signal else 0
        status = 'Stale' if new_failures >= 3 else 'Active'

        if tier == 1:
            # Just update vitality and timestamp
            agent_update_field(cursor, "startup", sid, "agent_updater", 60, "Status", status)
            cursor.execute("UPDATE startups SET last_updated = ?, vitality_failures = ? WHERE id = ?",
                           (get_utc_now(), new_failures, sid))
            conn.commit()
            print(f"  [Tier 1] Vitality: {'Stale' if is_stale_signal else 'Active'}. Done.")
            continue

        # --- TIER 2 & 3: Search + LLM ---
        # Extract domain for anchored queries
        domain = None
        if website:
            import re as _re
            m = _re.search(r'https?://(?:www\.)?([^/]+)', website)
            if m:
                domain = m.group(1)
        domain_anchor = f' "{domain}"' if domain else ''

        search_general = search_with_rotation(f'"{name}"{domain_anchor} startup turkey AI')
        search_funding = search_with_rotation(f'"{name}"{domain_anchor} raised OR yatırım OR funding investors milyon OR million')

        # Relevance filter: discard results that don't mention the company
        if search_general and not is_relevant_result(name, search_general):
            search_general = ""
        if search_funding and not is_relevant_result(name, search_funding):
            search_funding = ""

        search_context = (search_general or "") + "\n" + (search_funding or "")
        website_context = scrape_website_text(website) if website else ""

        has_funding = current_fund_fmt and current_fund_fmt != 'Unknown'
        confidence = calculate_confidence(website_context, search_context, has_funding,
                                          data_age_days=age_days)

        if tier == 2:
            # Delta LLM — only fill missing fields
            missing = []
            if is_missing(website): missing.append('website')
            if is_missing(desc): missing.append('description')
            if is_missing(city): missing.append('city')
            if is_missing(founders): missing.append('founders')
            if is_missing(current_sector): missing.append('sector')
            if is_missing(current_tag): missing.append('tag')
            if is_missing(current_ai_use): missing.append('AI_Use_Case')
            if is_missing(current_fund_fmt): missing.append('Total_Funding')
            if is_missing(current_inv): missing.append('Investors')
            if is_missing(current_tech_m): missing.append('Tech_Mentioned')
            if is_missing(current_tech_a): missing.append('Tech_Assumed')

            # Cloud classification — if missing, infer from description + category
            if is_missing(current_cloud_rel) and not is_missing(desc):
                from util_cloud_classify import classify_cloud_relevance_heuristic
                cloud_label = classify_cloud_relevance_heuristic(desc, current_category or "", current_tech_m or "")
                if cloud_label:
                    cursor.execute("UPDATE startups SET cloud_relevance = ? WHERE id = ?", (cloud_label, sid))
                    print(f"  [Tier 2] Cloud: {cloud_label}")

            if not missing:
                print("  [Tier 2] All fields populated. Vitality only.")
                agent_update_field(cursor, "startup", sid, "agent_updater", 60, "Status", status)
                cursor.execute("UPDATE startups SET data_confidence = ?, last_updated = ?, vitality_failures = ? WHERE id = ?",
                               (confidence, get_utc_now(), new_failures, sid))
                conn.commit()
                continue
            
            base_data = {"name": name, "description": desc or "", "website": website or "",
                         "city": city or "", "founders": founders or ""}
            enriched = execute_delta_workflow(base_data, search_context, website_context, missing)
            print(f"  [Tier 2] Delta fill for: {', '.join(missing)}")

        elif tier == 3:
            # Full three-pass re-enrichment
            base_data = {"name": name, "description": desc or "", "website": website or "",
                         "city": city or "", "founders": founders or ""}
            enriched = three_pass_enrichment(base_data, search_context, website_context)
            if enriched:
                print(f"  [Tier 3] Full three-pass enrichment complete.")
            else:
                print(f"  [Tier 3] Three-pass chain failed. Falling back to delta.")
                enriched = {}

        if enriched:
            # Resolve Category — three_pass_enrichment now returns 'Category' directly,
            # but delta workflow may return 'sector'/'tag' separately.
            if 'Category' in enriched:
                # Already combined by three_pass_enrichment
                cat_sector, cat_tag = parse_category(enriched['Category'])
            elif 'sector' in enriched and 'tag' in enriched:
                cat_sector = enriched.get('sector', '')
                cat_tag = enriched.get('tag', '')
            elif 'Industrial_Sector' in enriched and 'Tag_Category' in enriched:
                # Legacy fallback
                cat_sector = enriched.get('Industrial_Sector', '')
                cat_tag = enriched.get('Tag_Category', '')
            else:
                cat_sector, cat_tag = None, None

            if cat_sector and cat_tag:
                sector, tag = validate_sector_tag(cat_sector, cat_tag)

                # Post-LLM classification validation against keyword index
                tag_desc = enriched.get('description', '') or desc or ''
                tag_score = validate_classification_accuracy(conn, tag, tag_desc)
                if tag_desc and tag_score < TAG_SCORE_LOW_THRESHOLD:
                    print(f"  [Validator] Tag '{tag}' scored {tag_score:.2f} — keyword auto-pick first...")
                    candidates = get_top_candidate_tags(conn, tag_desc, n=5)
                    top_tag = candidates[0] if candidates else tag
                    new_sector, new_tag = validate_sector_tag('', top_tag)
                    top_score = validate_classification_accuracy(conn, new_tag, tag_desc)

                    if top_score >= TAG_SCORE_AUTO_ACCEPT_THRESHOLD:
                        if new_tag != tag:
                            print(f"  [Validator] Auto-picked tag: {tag} → {new_tag} (kw-score {top_score:.2f})")
                        sector, tag = new_sector, new_tag
                    else:
                        print(f"  [Validator] Auto-pick too weak (kw-score {top_score:.2f}) — LLM repick...")
                        repick_prompt = f"""This company's description: "{tag_desc[:400]}"

Pick the BEST tag from these options ONLY:
{chr(10).join(f'{i+1}. {t}' for i, t in enumerate(candidates))}

Output ONLY JSON: {{"tag": "exact tag name"}}"""
                        repick = _call_llm(repick_prompt)
                        if repick and repick.get('tag'):
                            new_sector2, new_tag2 = validate_sector_tag('', repick['tag'])
                            if new_tag2 != tag:
                                print(f"  [Validator] Re-picked: {tag} → {new_tag2}")
                                sector, tag = new_sector2, new_tag2

                category_new = f"{sector} | {tag}"
                # Learn from validated classification
                learn_keywords_from_description(conn, tag, tag_desc)
            else:
                category_new = None

            # Smart fill logic via module-level update_if_new
            agent_update_field(cursor, "startup", sid, "agent_updater", 60, "Status", status)

            # Category is the only stored category field.
            update_if_new(cursor, sid, 'Category', current_category, category_new)

            update_if_new(cursor, sid, 'AI_Use_Case', current_ai_use, enriched.get('AI_Use_Case'))
            update_if_new(cursor, sid, 'Total_Funding_Formatted', current_fund_fmt, enriched.get('Total_Funding'))
            update_if_new(cursor, sid, 'Investors', current_inv, enriched.get('Investors'))
            update_if_new(cursor, sid, 'Tech_Mentioned', current_tech_m, enriched.get('Tech_Mentioned'))
            update_if_new(cursor, sid, 'Tech_Assumed', current_tech_a, enriched.get('Tech_Assumed'))
            update_if_new(cursor, sid, 'website', website, enriched.get('website'))
            update_if_new(cursor, sid, 'description', desc, enriched.get('description'))
            update_if_new(cursor, sid, 'city', city, enriched.get('city'))
            update_if_new(cursor, sid, 'founders', founders, enriched.get('founders'))
            
            # Update business_model if available (from three-pass chain)
            if enriched.get('business_model') and enriched['business_model'] != 'Unknown':
                agent_update_field(cursor, "startup", sid, "agent_updater", 60, "business_model", enriched['business_model'])

            # Update cloud_relevance if available (from three-pass chain)
            cloud_rel = enriched.get('cloud_relevance')
            if cloud_rel and cloud_rel in ('Cloud-Native', 'Cloud-Enabled', 'Cloud-Adjacent', 'Non-Cloud'):
                cursor.execute("UPDATE startups SET cloud_relevance = ? WHERE id = ?", (cloud_rel, sid))

            # Crunchbase URL lookup (only if missing)
            if not cb_url or str(cb_url).strip().lower() in ['nan', '', 'none']:
                cb_found, cb_conf = find_crunchbase_url(name, desc or "")
                if cb_found:
                    agent_update_field(cursor, "startup", sid, "agent_updater", cb_conf, "crunchbase_url", cb_found)
                    print(f"  [CB] Crunchbase URL set: {cb_found} (conf: {cb_conf})")

            cursor.execute("UPDATE startups SET data_confidence = ?, last_updated = ?, vitality_failures = ? WHERE id = ?",
                           (confidence, get_utc_now(), new_failures, sid))
            conn.commit()
            
            sync_startup_investors(conn, sid, enriched.get('Investors', current_inv), enriched.get('Total_Funding', current_fund_fmt))
            
            print(f"  [+] Refreshed (confidence: {confidence}%)")
        else:
            # Even if LLM failed, update vitality
            agent_update_field(cursor, "startup", sid, "agent_updater", 60, "Status", status)
            cursor.execute("UPDATE startups SET last_updated = ?, vitality_failures = ? WHERE id = ?",
                           (get_utc_now(), new_failures, sid))
            conn.commit()
            print(f"  [!] LLM failed for {name}. Vitality updated.")

def process_b_refresh_investors(conn):
    """Tiered investor refresh with portfolio-based startup discovery."""
    from datetime import timezone
    cursor = conn.cursor()
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=INVESTOR_REFRESH_DAYS)).isoformat().replace("+00:00", "Z")

    stale_investors = cursor.execute('''
        SELECT investor_id, investor_name, investor_type, website, location, 
               focus_areas, data_confidence, last_updated, fund_size,
               founded_year, portfolio_count, notable_investments, crunchbase_url
        FROM investors
        WHERE (last_updated IS NULL OR last_updated < ?)
          AND (data_confidence IS NULL OR data_confidence >= 20)
        ORDER BY last_updated ASC
    ''', (cutoff,)).fetchall()

    if not stale_investors:
        print("No investors need refreshing.")
        return

    print(f"\n{len(stale_investors)} investors due for refresh (>{INVESTOR_REFRESH_DAYS} days).")
    total_discovered = 0

    for row in stale_investors:
        (inv_id, inv_name, i_type, i_web, i_loc, i_focus, i_conf,
         last_updated_str, i_fund, i_founded, i_portfolio_cnt, 
         i_notable, i_cb_url) = row

        # Determine tier based on data quality
        has_basic = all([
            i_type and i_type != 'Unknown',
            i_web and i_web != 'Unknown',
            i_loc and i_loc != 'Unknown'
        ])
        has_full = has_basic and all([
            i_fund and i_fund != 'Unknown',
            i_focus and i_focus != 'Unknown',
            i_portfolio_cnt and i_portfolio_cnt > 0
        ])

        if has_full:
            tier = 1  # Already enriched, just update timestamp
        elif has_basic:
            tier = 2  # Has basics, fill missing with delta
        else:
            tier = 3  # Needs full three-pass enrichment

        print(f"\n  [{inv_id}] {inv_name} — Tier {tier} refresh...")

        if tier == 1:
            cursor.execute("UPDATE investors SET last_updated = ? WHERE investor_id = ?",
                           (get_utc_now(), inv_id))
            conn.commit()
            print(f"    [Tier 1] Already enriched. Timestamp updated.")
            continue

        if tier == 2:
            # Delta: use legacy enrich_investor for missing basics
            profile = enrich_investor(inv_name)
            if profile:
                def update_inv(field, existing, new_val):
                    if not existing or str(existing).strip().lower() in ['nan', 'unknown', 'none', '']:
                        if new_val and new_val != 'Unknown':
                            agent_update_field(cursor, "investor", inv_id, "agent_updater", 60, field, new_val)

                update_inv('investor_type', i_type, profile.get('investor_type'))
                update_inv('website', i_web, profile.get('website'))
                update_inv('location', i_loc, profile.get('location'))
                update_inv('focus_areas', i_focus, profile.get('focus_areas'))
            
            cursor.execute("UPDATE investors SET last_updated = ?, data_confidence = ? WHERE investor_id = ?",
                           (get_utc_now(), 50, inv_id))
            conn.commit()
            print(f"    [Tier 2] Delta fill complete.")
            continue

        # --- TIER 3: Full three-pass enrichment + portfolio discovery ---
        from agent_core import three_pass_investor_enrichment, discover_portfolio_startups, find_crunchbase_url
        
        profile = three_pass_investor_enrichment(inv_name)
        if profile:
            def update_inv(field, existing, new_val):
                if not existing or str(existing).strip().lower() in ['nan', 'unknown', 'none', '']:
                    if new_val and new_val != 'Unknown':
                        agent_update_field(cursor, "investor", inv_id, "agent_updater", 70, field, new_val)
            
            update_inv('investor_type', i_type, profile.get('investor_type'))
            update_inv('website', i_web, profile.get('website'))
            update_inv('location', i_loc, profile.get('location'))
            update_inv('focus_areas', i_focus, profile.get('focus_areas'))
            update_inv('fund_size', i_fund, profile.get('fund_size'))
            update_inv('notable_investments', i_notable, profile.get('notable_investments'))
            
            # Update numeric fields directly
            if profile.get('founded_year') and not i_founded:
                agent_update_field(cursor, "investor", inv_id, "agent_updater", 70, "founded_year", profile['founded_year'])
            if profile.get('portfolio_count') and (not i_portfolio_cnt or i_portfolio_cnt == 0):
                agent_update_field(cursor, "investor", inv_id, "agent_updater", 70, "portfolio_count", profile['portfolio_count'])
            
            # Crunchbase URL (reuse three-layer validation)
            if not i_cb_url or str(i_cb_url).strip().lower() in ['nan', '', 'none']:
                cb_url, cb_conf = find_crunchbase_url(inv_name)
                if cb_url:
                    agent_update_field(cursor, "investor", inv_id, "agent_updater", cb_conf, "crunchbase_url", cb_url)
                    print(f"    [CB] Crunchbase: {cb_url}")
            
            cursor.execute("UPDATE investors SET data_confidence = ?, last_updated = ? WHERE investor_id = ?",
                           (70, get_utc_now(), inv_id))
            conn.commit()
            
            # Portfolio Discovery: cross-reference Turkish portfolio companies
            turkish_portfolio = profile.get('turkish_portfolio', [])
            all_portfolio = profile.get('portfolio_companies', [])
            # Prioritize Turkish ones, but also check all for matches
            discovery_list = turkish_portfolio if turkish_portfolio else all_portfolio
            if discovery_list:
                discovered = discover_portfolio_startups(conn, inv_id, inv_name, discovery_list)
                total_discovered += discovered
                if discovered:
                    print(f"    [Discovery] {discovered} new startups from {inv_name}'s portfolio!")
            
            print(f"    [Tier 3] Full enrichment complete.")
        else:
            # LLM failed, still update timestamp
            cursor.execute("UPDATE investors SET last_updated = ? WHERE investor_id = ?",
                           (get_utc_now(), inv_id))
            conn.commit()
            print(f"    [!] Three-pass failed for {inv_name}.")
    
    if total_discovered > 0:
        print(f"\n  [🔍] Total new startups discovered from investor portfolios: {total_discovered}")

def main():
    conn = get_db_connection(DB_NAME)
    seed_tag_keywords(conn)  # Ensure keyword index is bootstrapped
    print("="*60)
    print("  Module 5: Recurring Updater")
    print("="*60)

    # Process A: News Delta Scan
    print("\n--- Process A: News Delta Scan ---")
    process_a_news_delta(conn)

    # Process B: Unverified Skeleton Gatekeeper
    print("\n--- Process B: Secure Unverified Startups ---")
    process_b_refresh_unverified_startups(conn)

    # Process C: Refresh Stale Records
    print("\n--- Process C: Refresh Stale Startups ---")
    process_b_refresh_startups(conn)

    print("\n--- Process C: Refresh Stale Investors ---")
    process_b_refresh_investors(conn)

    print("\nRecurring update complete!")
    conn.close()

if __name__ == "__main__":
    main()
