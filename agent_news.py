"""
Module 1: News Scanner
Scans news/blog sources for startup and investor mentions.
Populates news_mentions, and creates investments + new startups/investors when found.
"""
import sqlite3
import json
import hashlib
import time
import requests
import re
from datetime import datetime, timedelta
from rapidfuzz import fuzz
from region_config import get_active_config
from data_guard import is_valid_company_name, is_valid_investor_name
from agent_core import (
    search_with_rotation, search_news_with_rotation, agent_update_field, get_utc_now,
    scrape_website_text, execute_8b_workflow, validate_sector_tag, clear_search_cache,
    _call_llm, fuzzy_matcher, OLLAMA_API_URL, MODELS, get_db_connection,
    async_search_news_batch, run_async,
    normalize_round_type, normalize_funding_amount
)
from util_currency import convert_to_usd

DB_NAME = "Master.db"
_CFG = get_active_config()


def _detect_currency(amount_str):
    """Detect currency from amount string. Returns 'Unknown' if ambiguous — never guesses."""
    if not amount_str:
        return "Unknown"
    upper = amount_str.upper()
    if "USD" in upper or "$" in upper:
        return "USD"
    if "EUR" in upper or "\u20ac" in upper:
        return "EUR"
    if "TRY" in upper or " TL" in upper or "\u20ba" in upper:
        return "TRY"
    return "Unknown"


# Pre-LLM quality gate: skip low-signal snippets to reduce incorrect extraction.
# This is intentionally strict for accuracy (you asked for reliability over speed).
_NEWS_AI_KEYWORDS = _CFG.news_tech_keywords

# Expanded action keywords so we don't miss non-funding ecosystem events.
_NEWS_ACTION_KEYWORDS = _CFG.news_action_keywords


def _passes_news_quality_gate(title, snippet, known_startup_names=None):
    text = f"{title or ''} {snippet or ''}".lower()

    # Robust AI detection (avoid missing `AI, AI)` etc).
    ai_hit = (
        any(k in text for k in _NEWS_AI_KEYWORDS)
        or re.search(r"\byapay\s*zeka\b", text) is not None
        or re.search(r"\byapayzeka\b", text) is not None
        or re.search(r"\bai\b", text) is not None
        # Expanded Turkish AI keywords
        or "büyük dil modeli" in text
        or "robotik süreç" in text
        or "otonom" in text
        or "makine öğrenmesi" in text
        or "derin öğrenme" in text
    )

    # Action keywords: funding, partnerships, programs, announcements, launches.
    action_hit = any(k in text for k in _NEWS_ACTION_KEYWORDS)

    # Known startup name match: if a tracked startup appears, always pass
    startup_name_hit = False
    if known_startup_names:
        for name in known_startup_names:
            if name.lower() in text:
                startup_name_hit = True
                break

    # Negative keywords: reduce obvious HR/career/operational announcements.
    negative_hit = any(k in text for k in _CFG.news_negative_keywords)

    # Weighted scoring instead of strict AND gate
    score = 0
    if ai_hit:
        score += 2
    if action_hit:
        score += 1
    if startup_name_hit:
        score += 2
    if negative_hit:
        score -= 3
    return score >= 2


def _extract_year(text):
    """Extract a 4-digit year (19xx/20xx) from arbitrary text."""
    if not text:
        return None
    m = re.search(r'((?:19|20)\d{2})', str(text))
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None

# Consolidated search queries (from region config)
NEWS_QUERIES = _CFG.news_queries

def setup_tables(conn):
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS news_mentions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            startup_id INTEGER,
            investor_id INTEGER,
            headline TEXT,
            url_hash TEXT UNIQUE,
            source_url TEXT,
            source_name TEXT,
            source_language TEXT,
            published_date TEXT,
            summary TEXT,
            event_type TEXT,
            event_category TEXT,
            event_subtype TEXT,
            ai_relevance INTEGER,
            ai_snippet TEXT,
            is_relevant INTEGER DEFAULT 1,
            sentiment TEXT,
            scanned_at TEXT,
            FOREIGN KEY (startup_id) REFERENCES startups(id),
            FOREIGN KEY (investor_id) REFERENCES investors(investor_id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            module_name TEXT,
            scan_start TEXT,
            scan_end TEXT,
            source_scanned TEXT,
            items_found INTEGER,
            items_updated INTEGER,
            status TEXT
        )
    ''')
    # Ensure investors/investments tables exist
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS investors (
            investor_id INTEGER PRIMARY KEY AUTOINCREMENT,
            investor_name TEXT UNIQUE,
            investor_type TEXT,
            website TEXT,
            location TEXT,
            focus_areas TEXT,
            total_investments_count INTEGER DEFAULT 0,
            processed_at TEXT,
            last_updated TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS investments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            startup_id INTEGER,
            investor_id INTEGER,
            news_mention_id INTEGER,
            round_type TEXT,
            amount TEXT,
            currency TEXT,
            amount_usd REAL,
            investment_date TEXT,
            source_url TEXT,
            FOREIGN KEY (startup_id) REFERENCES startups(id),
            FOREIGN KEY (investor_id) REFERENCES investors(investor_id),
            FOREIGN KEY (news_mention_id) REFERENCES news_mentions(id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS news_scan_state (
            query_hash TEXT PRIMARY KEY,
            query_text TEXT,
            last_recent_scan TEXT,
            backfill_pointer TEXT
        )
    ''')

    # --- Ecosystem Entity Enrichment Schema ---

    # Expand ecosystem_entities with enrichment fields (idempotent via try/except)
    _eco_new_cols = [
        ("linkedin_url", "TEXT"),
        ("blog_url", "TEXT"),
        ("social_media_urls", "TEXT"),
        ("ai_focus_areas", "TEXT"),
        ("ai_initiatives_summary", "TEXT"),
        ("last_scanned", "TEXT"),
        ("last_updated", "TEXT"),
        ("data_confidence", "INTEGER DEFAULT 0"),
        ("needs_enrichment", "INTEGER DEFAULT 1"),
    ]
    for col_name, col_def in _eco_new_cols:
        try:
            cursor.execute(f"ALTER TABLE ecosystem_entities ADD COLUMN {col_name} {col_def}")
        except Exception:
            pass  # Column already exists

    # Unified activity log — replaces investments as write destination going forward
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ecosystem_activities (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_type       TEXT NOT NULL,
            headline            TEXT,
            description         TEXT,
            importance          TEXT,
            importance_tags     TEXT,
            source_url          TEXT,
            source_name         TEXT,
            source_type         TEXT,
            activity_date       TEXT,
            entity_id           INTEGER REFERENCES ecosystem_entities(id),
            startup_id          INTEGER REFERENCES startups(id),
            investor_id         INTEGER REFERENCES investors(investor_id),
            amount              TEXT,
            currency            TEXT,
            amount_usd          REAL,
            round_type          TEXT,
            news_mention_id     INTEGER REFERENCES news_mentions(id),
            url_hash            TEXT UNIQUE,
            data_confidence     INTEGER DEFAULT 0,
            needs_enrichment    INTEGER DEFAULT 0,
            scanned_at          TEXT
        )
    ''')

    # Junction table: activity ↔ all related entities (replaces comma-separated ID fields)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ecosystem_activity_entities (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_id INTEGER NOT NULL REFERENCES ecosystem_activities(id) ON DELETE CASCADE,
            entity_type TEXT NOT NULL,
            entity_id   INTEGER NOT NULL,
            role        TEXT
        )
    ''')
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_eae_activity ON ecosystem_activity_entities(activity_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_eae_entity ON ecosystem_activity_entities(entity_type, entity_id)"
    )

    conn.commit()

# --- Fuzzy Matching (delegates to shared FuzzyMatcher) ---

def fuzzy_match_startup(conn, company_name):
    """Find a matching startup using the shared cached matcher."""
    return fuzzy_matcher.find_startup(conn, company_name)

def fuzzy_match_investor(conn, investor_name):
    """Find a matching investor using the shared cached matcher."""
    return fuzzy_matcher.find_investor(conn, investor_name)

def invalidate_indexes():
    """Call after creating new startups/investors to refresh the shared index."""
    fuzzy_matcher.invalidate()

def extract_from_article(snippet, source_name, source_lang):
    """Use LLM to extract structured data from a news snippet."""
    prompt = f"""You are a news extraction agent for the {_CFG.region_name} {_CFG.technology_domain} Startup Ecosystem.
Analyze this news snippet and extract structured data.

Source: {source_name}
Language: {source_lang}
Text: {snippet}

{_CFG.news_extraction_rules}

Output ONLY a valid JSON object with these keys:
"headline" (string, a short headline summarizing the article)
"companies" (list of objects, each with {{"name": str, "role": "subject"|"partner"|"mentioned"}}. Only include {_CFG.region_name} {_CFG.technology_domain} companies. "subject" = the article is about this company. "partner" = mentioned as a partner/customer. "mentioned" = only referenced for context.)
"investors" (list of strings, investor names if any)
"funding_amount" (string, e.g. "2.000.000,00 USD", "5.000.000,00 TRY", or "Unknown")
"round_type" (string, e.g. "Seed", "Series A", "Pre-seed", "Unknown")
"funding_date" (string, CRITICAL: extract the actual date of the funding round from article text. If only year known use "YYYY-01-01". YYYY-MM-DD format. Return "Unknown" ONLY if no date info exists. NEVER use today's date.)
"event_category" (one of: "funding", "product", "partnership", "corporate", "talent", "ecosystem", "expansion", "technology")
"event_subtype" (specific sub-type based on category:
  funding: "seed", "series_a", "series_b", "series_c_plus", "grant", "debt", "undisclosed_round"
  product: "product_launch", "product_update", "pivot", "beta_launch"
  partnership: "strategic_partnership", "distribution_deal", "technology_integration", "joint_venture"
  corporate: "acquisition", "merger", "spin_off", "ipo", "shutdown"
  talent: "key_hire", "team_expansion", "layoff"
  ecosystem: "accelerator_batch", "award", "conference", "government_program", "regulatory"
  expansion: "new_market", "new_office", "international_expansion"
  technology: "ai_breakthrough", "patent", "open_source_release", "research_paper")
"sentiment" (one of: "positive", "neutral", "negative")
"summary" (string, 1-2 sentence summary)
"ai_relevance" (integer 0-100: How relevant is this news to {_CFG.technology_domain}?
  90-100: Core {_CFG.technology_domain} story — {_CFG.technology_domain} product, {_CFG.technology_domain} research, {_CFG.technology_domain}-focused funding
  60-89: Strong {_CFG.technology_domain} angle — {_CFG.technology_domain} adoption, {_CFG.technology_domain} talent, {_CFG.technology_domain} policy
  40-59: Moderate — tech company with some {_CFG.technology_domain} involvement
  20-39: Weak — general tech/startup news, minor {_CFG.technology_domain} mention
  0-19: No {_CFG.technology_domain} connection)
"ai_snippet" (string or null: If ai_relevance >= 40, write 1-2 sentences explaining the {_CFG.technology_domain} angle — what {_CFG.technology_domain} technology is involved, how it's applied, what it means for the ecosystem. If ai_relevance < 40, output null.)

Output ONLY the raw JSON.
"""
    return _call_llm(prompt)

def process_article(conn, article_url, snippet, source_name, source_lang, extracted, pub_date=None):
    """Process an extracted article and update all relevant tables."""
    cursor = conn.cursor()

    # Deduplicate by URL hash
    url_hash = hashlib.md5(article_url.encode()).hexdigest()
    existing = cursor.execute("SELECT id FROM news_mentions WHERE url_hash = ?", (url_hash,)).fetchone()
    if existing:
        return False  # Already processed

    headline = extracted.get("headline", "")
    sentiment = extracted.get("sentiment", "neutral")
    summary = extracted.get("summary", "")

    # New two-level categorization
    event_category = extracted.get("event_category", "general")
    event_subtype = extracted.get("event_subtype", "")
    ai_relevance = extracted.get("ai_relevance")
    ai_snippet = extracted.get("ai_snippet")

    # Parse ai_relevance safely
    if ai_relevance is not None:
        try:
            ai_relevance = int(ai_relevance)
        except (ValueError, TypeError):
            ai_relevance = None

    # Null out ai_snippet if ai_relevance is low
    if ai_relevance is not None and ai_relevance < 40:
        ai_snippet = None

    # Map event_category back to legacy event_type for backward compat
    _CATEGORY_TO_EVENT_TYPE = {
        'funding': 'funding', 'product': 'product_launch', 'partnership': 'partnership',
        'corporate': 'acquisition', 'talent': 'hiring', 'ecosystem': 'award',
        'expansion': 'expansion', 'technology': 'product_launch',
    }
    event_type = _CATEGORY_TO_EVENT_TYPE.get(event_category, extracted.get("event_type", "general"))

    # Blacklist: non-AI news gets stored but excluded from downstream processing
    is_relevant = 0 if (ai_relevance is not None and ai_relevance < 20) else 1

    # Guard: LLM sometimes returns a list instead of a string for scalar fields
    if isinstance(headline, list):
        headline = ", ".join(str(v) for v in headline) if headline else ""
    if isinstance(event_type, list):
        event_type = event_type[0] if event_type else "general"
    if isinstance(sentiment, list):
        sentiment = sentiment[0] if sentiment else "neutral"
    if isinstance(summary, list):
        summary = " ".join(str(v) for v in summary) if summary else ""

    # Early exit for blacklisted (non-AI) articles — skip expensive fuzzy matching
    if not is_relevant:
        cursor.execute('''
            INSERT INTO news_mentions (startup_id, investor_id, headline, url_hash, source_url,
                source_name, source_language, published_date, summary, event_type, sentiment,
                event_category, event_subtype, ai_relevance, ai_snippet, is_relevant, scanned_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            None, None, headline, url_hash, article_url,
            source_name, source_lang, str(pub_date)[:10] if pub_date else None, summary, event_type, sentiment,
            event_category, event_subtype, ai_relevance, None, 0, get_utc_now()
        ))
        conn.commit()
        print(f"    [Blacklist] ai_relevance={ai_relevance}: {headline[:60]}")
        return True  # Stored for dedup but no downstream processing

    companies = extracted.get("companies", [])
    investors = extracted.get("investors", [])
    raw_funding = extracted.get("funding_amount", "Unknown")
    _norm_val, _norm_fmt, _norm_cur = normalize_funding_amount(raw_funding)
    funding = _norm_fmt if _norm_fmt else raw_funding
    round_type = normalize_round_type(extracted.get("round_type", ""))
    # Date fallback chain: LLM-extracted deal date → article publication date → NULL
    funding_date = extracted.get("funding_date") or None
    if funding_date and funding_date.lower() in ("unknown", "n/a", ""):
        funding_date = None
    if not funding_date and pub_date:
        # Article publication date is a reasonable proxy for deal date
        funding_date = str(pub_date)[:10] if pub_date else None

    # Convert currency historically based on the date
    usd_amount = None
    if funding and funding != "Unknown":
        _, usd_amount, _ = convert_to_usd(funding, funding_date)

    # Normalize companies: support both old format (list of strings) and new format (list of objects with name/role)
    normalized_companies = []
    for company in companies:
        if isinstance(company, dict):
            name = company.get("name", "").strip()
            role = company.get("role", "subject")
            if role != "subject":
                print(f"    [Skip] Company '{name}' has role '{role}' — only 'subject' companies are linked")
                continue
            normalized_companies.append(name)
        elif isinstance(company, str):
            normalized_companies.append(company.strip())

    # Collect ALL matched startup IDs (not just the last one)
    startup_ids = []
    for company in normalized_companies:
        match = fuzzy_match_startup(conn, company)
        if match:
            if len(match) > 2 and match[2] == 'Blacklisted':
                print(f"    [Skip] Ignoring explicitly blacklisted company: {company}")
                continue
            startup_ids.append(match[0])
            print(f"    Matched startup: {company} → {match[1]} (ID {match[0]})")
        else:
            # Centralized company name validation guard
            if not is_valid_company_name(company):
                continue
            # Skip placeholder/invalid names the LLM might return
            invalid_names = _CFG.invalid_company_names
            name_lower = company.strip().lower()
            if name_lower in invalid_names or company.strip().startswith('[') or len(company.strip()) < 3:
                continue

            # Validation: confirm this company actually exists AND is Turkish
            confirm = search_with_rotation(_CFG.startup_confirmation_query_template.format(name=company), max_results=2)
            if not confirm or company.lower() not in confirm.lower():
                print(f"    [Skip] Cannot confirm '{company}' exists — skipped skeleton creation")
                continue

            # Guard: reject well-known international companies
            if company.strip().lower() in _CFG.international_blocklist:
                print(f"    [Skip] '{company}' is a known international company — not {_CFG.region_name} ecosystem")
                continue

            # Guard: check for duplicate name (case-insensitive)
            existing_name = cursor.execute(
                "SELECT id FROM startups WHERE LOWER(company_name) = LOWER(?)", (company.strip(),)
            ).fetchone()
            if existing_name:
                startup_ids.append(existing_name[0])
                print(f"    Matched by exact name: {company} (ID {existing_name[0]})")
                continue

            # Create skeleton startup for future verification
            try:
                cursor.execute('''
                    INSERT INTO startups (company_name, Status, data_confidence, processed_at)
                    VALUES (?, 'Active', 0, ?)
                ''', (company, get_utc_now()))
                conn.commit()
                new_id = cursor.lastrowid
                startup_ids.append(new_id)
                print(f"    [NEW] Created skeleton startup: {company} (ID {new_id})")
            except Exception:
                pass  # May fail on unique constraint; handled gracefully

    # Collect ALL matched investor IDs (not just the last one)
    investor_ids = []
    for inv_name in investors:
        if not is_valid_investor_name(inv_name):
            continue
        inv_match = fuzzy_match_investor(conn, inv_name)
        if inv_match:
            investor_ids.append(inv_match[0])
        else:
            try:
                cursor.execute("INSERT INTO investors (investor_name, processed_at) VALUES (?, ?)",
                               (inv_name, get_utc_now()))
                conn.commit()
                investor_ids.append(cursor.lastrowid)
                print(f"    [NEW] Created investor: {inv_name} (ID {cursor.lastrowid})")
            except sqlite3.IntegrityError:
                row = cursor.execute("SELECT investor_id FROM investors WHERE investor_name = ?", (inv_name,)).fetchone()
                if row:
                    investor_ids.append(row[0])

    # Use first IDs for the primary news mention (backward-compatible), or None
    primary_startup_id = startup_ids[0] if startup_ids else None
    primary_investor_id = investor_ids[0] if investor_ids else None

    # Insert primary news mention
    cursor.execute('''
        INSERT INTO news_mentions (startup_id, investor_id, headline, url_hash, source_url,
            source_name, source_language, published_date, summary, event_type, sentiment,
            event_category, event_subtype, ai_relevance, ai_snippet, is_relevant, scanned_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        primary_startup_id, primary_investor_id, headline, url_hash, article_url,
        source_name, source_lang, str(pub_date)[:10] if pub_date else None, summary, event_type, sentiment,
        event_category, event_subtype, ai_relevance, ai_snippet, 1, get_utc_now()
    ))
    news_mention_id = cursor.lastrowid
    conn.commit()

    # Insert additional news mentions for extra startups (2nd, 3rd, etc.)
    for extra_sid in startup_ids[1:]:
        extra_hash = hashlib.md5(f"{article_url}__startup_{extra_sid}".encode()).hexdigest()
        try:
            cursor.execute('''
                INSERT INTO news_mentions (startup_id, investor_id, headline, url_hash, source_url,
                    source_name, source_language, published_date, summary, event_type, sentiment,
                    event_category, event_subtype, ai_relevance, ai_snippet, is_relevant, scanned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                extra_sid, primary_investor_id, headline, extra_hash, article_url,
                source_name, source_lang, str(pub_date)[:10] if pub_date else None, summary, event_type, sentiment,
                event_category, event_subtype, ai_relevance, ai_snippet, 1, get_utc_now()
            ))
        except Exception:
            pass  # Unique constraint on url_hash — already linked
    conn.commit()

    # Create investment records for ALL startup × investor pairs (with dedup guard)
    if event_type == "funding" and startup_ids and investor_ids:
        for sid in startup_ids:
            for iid in investor_ids:
                try:
                    # Dedup key includes year to avoid collapsing distinct rounds
                    existing_inv = cursor.execute(
                        """SELECT id, amount_usd, investment_date FROM investments
                           WHERE startup_id = ? AND investor_id = ? AND round_type = ?
                           AND (strftime('%Y', investment_date) = strftime('%Y', ?) OR investment_date IS NULL)
                           LIMIT 1""",
                        (sid, iid, round_type, funding_date)
                    ).fetchone()

                    # Never auto-dedup Unknown/NA round types
                    if round_type in ("Unknown", "NA", "None", "") and existing_inv:
                        existing_inv = None  # Force new record

                    if existing_inv:
                        # Only update if new data is better (has info the existing record lacks)
                        updates = []
                        params = []
                        if usd_amount and usd_amount > 0 and (not existing_inv[1] or existing_inv[1] == 0):
                            updates.append("amount_usd = ?")
                            params.append(usd_amount)
                        if funding_date and not existing_inv[2]:
                            updates.append("investment_date = ?")
                            params.append(funding_date)
                        if updates:
                            params.append(existing_inv[0])
                            cursor.execute(f"UPDATE investments SET {', '.join(updates)} WHERE id = ?", params)
                    else:
                        cursor.execute('''
                            INSERT INTO investments (startup_id, investor_id, news_mention_id, round_type,
                                                     amount, currency, amount_usd, investment_date, source_url)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (sid, iid, news_mention_id, round_type,
                              funding, _detect_currency(funding) if funding else "Unknown",
                              usd_amount, funding_date, article_url))
                except sqlite3.IntegrityError:
                    pass  # Unique constraint violation — expected
                except Exception as e:
                    print(f"    [Warning] Investment insert error for startup {sid}, investor {iid}: {e}")

                # Dual-write to ecosystem_activities (sole forward-writing destination)
                try:
                    import hashlib as _hl
                    _ea_hash = _hl.md5(f"news::{article_url}::inv::{sid}::{iid}".encode()).hexdigest()
                    cursor.execute('''
                        INSERT OR IGNORE INTO ecosystem_activities (
                            activity_type, headline, source_url, source_name, source_type,
                            activity_date, startup_id, investor_id,
                            amount, currency, amount_usd, round_type,
                            news_mention_id, url_hash,
                            data_confidence, needs_enrichment, scanned_at
                        ) VALUES (
                            'investment', ?, ?, 'agent_news', 'news',
                            ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?,
                            85, 0, ?
                        )
                    ''', (
                        headline or f"Investment in startup {sid} from investor {iid}",
                        article_url, funding_date, sid, iid,
                        funding, _detect_currency(funding) if funding else "Unknown",
                        usd_amount, round_type,
                        news_mention_id, _ea_hash, get_utc_now()
                    ))
                    _ea_id = cursor.lastrowid
                    if _ea_id:
                        cursor.execute(
                            "INSERT OR IGNORE INTO ecosystem_activity_entities (activity_id, entity_type, entity_id, role) VALUES (?, 'startup', ?, 'recipient')",
                            (_ea_id, sid)
                        )
                        cursor.execute(
                            "INSERT OR IGNORE INTO ecosystem_activity_entities (activity_id, entity_type, entity_id, role) VALUES (?, 'investor', ?, 'actor')",
                            (_ea_id, iid)
                        )
                except Exception:
                    pass  # Don't block on ecosystem write failure
            
            # Push funding data back into the startup record if missing
            existing_rec = cursor.execute("SELECT Investors, Total_Funding_Formatted FROM startups WHERE id = ?", (sid,)).fetchone()
            if existing_rec:
                cur_inv, cur_fund = existing_rec
                if investors:
                    inv_names = ", ".join(investors)
                    if not cur_inv or cur_inv == 'Unknown':
                        agent_update_field(cursor, "startup", sid, "agent_news", 85, "Investors", inv_names)
                if funding and funding != 'Unknown':
                    if not cur_fund or cur_fund == 'Unknown':
                        agent_update_field(cursor, "startup", sid, "agent_news", 85, "Total_Funding_Formatted", funding)

        # Update investor counts
        for iid in investor_ids:
            count = cursor.execute("SELECT COUNT(*) FROM investments WHERE investor_id = ?", (iid,)).fetchone()[0]
            cursor.execute("UPDATE investors SET total_investments_count = ? WHERE investor_id = ?", (count, iid))
        
        conn.commit()
        print(f"    [💰] Investment recorded: {round_type} - {funding} ({len(startup_ids)} startups × {len(investor_ids)} investors)")

    return True

def main(max_results=50, mode="recent"):
    """
    Run the news scanner.
    mode="recent": Scans for newest articles (default).
    mode="backfill": Scans for older articles before the current backfill_pointer.
    """
    conn = get_db_connection(DB_NAME)
    setup_tables(conn)
    cursor = conn.cursor()
    clear_search_cache()  # Reset search cache for this run
    invalidate_indexes()  # Reset fuzzy indexes for this run

    scan_start = get_utc_now()
    total_found = 0
    total_updated = 0

    print(f"Module 1: News Scanner ({mode} mode) starting at {scan_start}")

    # --- Async batch: fire all searches concurrently for "recent" mode ---
    all_articles_by_query = {}
    if mode == "recent":
        print(f"\n  [Async] Firing {len(NEWS_QUERIES)} searches concurrently...")
        all_articles_by_query = run_async(
            async_search_news_batch(NEWS_QUERIES, max_results=max_results, time_range="year")
        )
        print(f"  [Async] All searches complete.")

    for q in NEWS_QUERIES:
        query_text = q["query"]
        source = q["source"]
        lang = q["lang"]
        query_hash = hashlib.md5(query_text.encode()).hexdigest()

        # Load state
        row = cursor.execute("SELECT backfill_pointer FROM news_scan_state WHERE query_hash = ?", (query_hash,)).fetchone()
        backfill_pointer = row[0] if row else "2025-01-01"

        current_query = query_text
        if mode == "backfill":
            # For backfill, we use standard text search with before: operator
            # because the News API doesn't allow arbitrary date ranges easily
            current_query = f"{query_text} before:{backfill_pointer}"
            print(f"\n--- Backfilling: {source} ({lang}) before {backfill_pointer} ---")
            articles_raw = search_with_rotation(current_query, max_results=max_results, language=lang)
            # Standard search returns string block, news search returns list of dicts.
            # We convert standard search to match the new dict format for consistency.
            articles = []
            if articles_raw:
                for block in articles_raw.split("\n\n"):
                    lines = block.strip().split("\n")
                    url, snippet = "", ""
                    for line in lines:
                        if line.startswith("Url: "): url = line[5:]
                        elif line.startswith("Snippet: "): snippet = line[9:]
                    if url and snippet:
                        articles.append({"url": url, "body": snippet, "title": "Archive Item", "date": backfill_pointer})
        else:
            print(f"\n--- Scanning Recent: {source} ({lang}) ---")
            # Use pre-fetched async results
            articles = all_articles_by_query.get(query_text, [])

        if not articles:
            print(f"    No results found.")
            if mode == "backfill":
                # If no results, move the pointer back by 1 month to keep trying
                try:
                    dt = datetime.strptime(backfill_pointer, "%Y-%m-%d")
                    new_pointer = (dt - timedelta(days=30)).strftime("%Y-%m-%d")
                    cursor.execute("INSERT OR REPLACE INTO news_scan_state (query_hash, query_text, backfill_pointer) VALUES (?, ?, ?)",
                                   (query_hash, query_text, new_pointer))
                except: pass
            continue

        print(f"    Found {len(articles)} articles.")

        earliest_date = backfill_pointer
        for article in articles:
            url = article.get("url", "")
            snippet = article.get("body", "")
            title = article.get("title", "News Item")
            pub_date = article.get("date", "")
            
            if not url or not snippet:
                continue

            # Cheap pre-LLM gate to reduce low-signal extraction calls.
            if not _passes_news_quality_gate(title, snippet):
                continue

            # 1. Deduplicate by URL hash BEFORE calling LLM
            url_hash = hashlib.md5(url.encode()).hexdigest()
            existing = cursor.execute("SELECT id FROM news_mentions WHERE url_hash = ?", (url_hash,)).fetchone()
            if existing:
                continue

            # 2. Date/Year Filter
            year = _extract_year(pub_date) if pub_date else None
            if year is None:
                # SearXNG often doesn't return structured dates; attempt year extraction from text.
                year = _extract_year((title or "") + " " + (snippet or ""))

            if mode == "recent":
                # Accuracy-first: require an explicit year >= 2025.
                if year is None or year < 2025:
                    continue
            else:
                # Backfill: keep permissive behavior if year isn't parseable.
                if pub_date and year is not None:
                    if str(pub_date) < earliest_date:
                        earliest_date = str(pub_date)[:10]

            print(f"    Analyzing: {title[:50]}...")
            extracted = extract_from_article(snippet, source, lang)
            if extracted:
                was_new = process_article(conn, url, snippet, source, lang, extracted, pub_date=pub_date)
                total_found += 1
                if was_new:
                    total_updated += 1

        # Update state persistence
        if mode == "backfill":
            cursor.execute("INSERT OR REPLACE INTO news_scan_state (query_hash, query_text, backfill_pointer) VALUES (?, ?, ?)",
                           (query_hash, query_text, earliest_date))
        else:
            cursor.execute("INSERT OR REPLACE INTO news_scan_state (query_hash, query_text, last_recent_scan) VALUES (?, ?, ?)",
                           (query_hash, query_text, scan_start))
        conn.commit()

    # Log the overall scan
    scan_end = get_utc_now()
    cursor.execute('''
        INSERT INTO scan_log (module_name, scan_start, scan_end, source_scanned, items_found, items_updated, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (f"M1_news_{mode}", scan_start, scan_end, "all", total_found, total_updated, "completed"))
    conn.commit()

    print(f"\n{'='*60}")
    print(f"Mode: {mode} | Found: {total_found}, New entries: {total_updated}")
    print(f"{'='*60}")
    conn.close()

def run_unified_news_cycle():
    """Performs a recent scan, then a targeted backfill scan for one query."""
    # 1. Recent Scan for all queries (Post-2025 only)
    print("\n--- Phase 1: Scanning for Recent 2025+ News ---")
    main(max_results=20, mode="recent")
    
    # 2. Targeted Backfill for a single query to 'gradually work back'
    print("\n--- Phase 2: Gradually Backfilling Gaps ---")
    main(max_results=10, mode="backfill")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--backfill":
        main(max_results=50, mode="backfill")
    elif len(sys.argv) > 1 and sys.argv[1] == "--recent":
        main(max_results=50, mode="recent")
    else:
        run_unified_news_cycle()
