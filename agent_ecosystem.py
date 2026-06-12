"""
Module 3: Ecosystem Entity Scanner
Actively scans ecosystem entities (Corporates, Teknoparks, Accelerators,
Government bodies, Media) for technology-related activities via news, blog, and LinkedIn.
Logs structured activities to ecosystem_activities with rich metadata and entity links.
"""
import sqlite3
import json
import hashlib
import time
import re
from datetime import datetime, timedelta, timezone
from rapidfuzz import fuzz
from region_config import get_active_config

from data_guard import is_valid_company_name
from agent_core import (
    search_with_rotation, search_news_with_rotation, scrape_website_text,
    _call_llm, agent_update_field, get_utc_now, clear_search_cache,
    EXTERNAL_NEWS_SOURCES, parse_search_results, fuzzy_matcher,
    is_relevant_result, get_db_connection,
    async_search_news_batch, async_search_batch, async_scrape_batch, run_async,
    normalize_round_type,
)
from util_currency import convert_to_usd

DB_NAME = "Master.db"
_CFG = get_active_config()

# Entities with data_confidence < PRIORITY_THRESHOLD are scanned every run
PRIORITY_THRESHOLD = 30
# Already-enriched entities are re-scanned after this many days
REFRESH_DAYS = 14

# Activity types the scanner will classify
VALID_ACTIVITY_TYPES = {
    'investment', 'partnership', 'acquisition', 'program_launch',
    'accelerator_batch', 'research_grant', 'product_launch', 'event'
}

# Importance tag vocabulary (structured + filterable)
IMPORTANCE_TAG_VOCAB = [
    'startup_funding', 'corporate_venture', 'ai_adoption', 'foreign_partnership',
    'domestic_partnership', 'r&d_investment', 'talent_program', 'public_sector_ai',
    'regulatory_action', 'ecosystem_event', 'accelerator_program', 'acquisition',
    'international_expansion', 'deeptech', 'digital_transformation'
]

# Entity-type search query templates (from region config)
SEARCH_TEMPLATES = _CFG.ecosystem_search_templates


# ─────────────────────────────────────────────────────────────────────────────
# Fuzzy entity lookup (delegates to shared FuzzyMatcher)
# ─────────────────────────────────────────────────────────────────────────────

def _load_indexes(conn):
    """Pre-warm the shared fuzzy matcher caches."""
    fuzzy_matcher._ensure_startups(conn)
    fuzzy_matcher._ensure_investors(conn)
    fuzzy_matcher._ensure_entities(conn)


def _fuzzy_find_startup(conn, name):
    return fuzzy_matcher.find_startup(conn, name)


def _fuzzy_find_investor(conn, name):
    return fuzzy_matcher.find_investor(conn, name)


def _fuzzy_find_entity(conn, name):
    return fuzzy_matcher.find_entity(conn, name)


# ─────────────────────────────────────────────────────────────────────────────
# Source gathering (uses shared EXTERNAL_NEWS_SOURCES from agent_core)
# ─────────────────────────────────────────────────────────────────────────────

def _gather_articles(entity_name, entity_type, blog_url, max_per_query=5):
    """Gather articles from four channels concurrently:
    1. News search (entity-type templates)
    2. OWN website blog scrape
    3. Dedicated external media source queries
    4. LinkedIn snippet search

    Channels 1, 3, 4 fire all searches in parallel via async I/O.
    """
    articles = []
    seen_hashes = set()

    def _add(url, text, source_type, source_name):
        if not url or not text:
            return
        h = hashlib.md5(url.encode()).hexdigest()
        if h not in seen_hashes:
            seen_hashes.add(h)
            articles.append({
                'url': url, 'text': text[:2000],
                'source_type': source_type, 'source_name': source_name,
                'url_hash': h
            })

    # Build all search queries for channels 1, 3, 4
    # Channel 1: News search via entity-type templates
    templates = SEARCH_TEMPLATES.get(entity_type, SEARCH_TEMPLATES['Corporate'])
    ch1_queries = [{"query": t.format(name=entity_name), "lang": "tr"} for t in templates]

    # Channel 3: External media sources
    ch3_queries = [
        {"query": f'site:{domain} "{entity_name}"', "max_results": 3, "language": "tr"}
        for domain in EXTERNAL_NEWS_SOURCES
    ]

    # Channel 4: LinkedIn
    li_query = f'"{entity_name}" linkedin yapay zeka OR AI duyuru OR announcement 2024 2025'
    ch4_queries = [{"query": li_query, "max_results": 3, "language": "tr"}]

    # Fire channels 1, 3, 4 concurrently
    ch1_results = run_async(async_search_news_batch(ch1_queries, max_results=max_per_query, time_range="year"))
    ch34_results = run_async(async_search_batch(ch3_queries + ch4_queries))

    # Process Channel 1 results
    for q in ch1_queries:
        for r in (ch1_results.get(q["query"], []) or []):
            text = r.get('body', '') or r.get('title', '')
            if is_relevant_result(entity_name, text):
                _add(r.get('url', ''), text, 'news', r.get('source', 'news_search'))

    # Channel 2: Own website blog/press scrape (quick, single URL)
    if blog_url:
        text = scrape_website_text(blog_url)
        if text:
            _add(blog_url, text, 'blog', 'entity_blog')

    # Process Channel 3 results
    for domain in EXTERNAL_NEWS_SOURCES:
        ext_query = f'site:{domain} "{entity_name}"'
        ext_results = ch34_results.get(ext_query, "")
        if ext_results:
            for r in parse_search_results(ext_results):
                url = r['url']
                content = r['snippet'] or r['title']
                if domain in url.lower():
                    if is_relevant_result(entity_name, content):
                        _add(url, content, 'news', domain)

    # Process Channel 4 results
    li_results = ch34_results.get(li_query, "")
    if li_results:
        for r in parse_search_results(li_results):
            url = r['url']
            snippet = r['snippet']
            if 'linkedin.com' in url and snippet:
                if is_relevant_result(entity_name, snippet):
                    _add(url, snippet, 'social_media', 'linkedin')

    return articles



# ─────────────────────────────────────────────────────────────────────────────
# Three-pass LLM enrichment (per entity, all articles batched)
# ─────────────────────────────────────────────────────────────────────────────
def _pass_1_extract_facts(entity_name, entity_type, articles_text):
    """Pass 1: Extract raw activity facts from all gathered text."""
    prompt = f"""You are a Fact-Extraction Agent for the Turkish AI Ecosystem.
Analyze all the text below about "{entity_name}" ({entity_type}) and extract ONLY AI-related activities.

Text:
{articles_text[:5000]}

CRITICAL RULES:
1. Extract ONLY activities related to: AI, machine learning, technology investment, digital transformation, startup partnerships, R&D grants, accelerator programs.
2. Do NOT extract: routine business operations, HR announcements, branch openings, bill payments, working hours, sustainability reports, general corporate news.
3. If the text is about a company that has NO connection to Turkey or the Turkish AI ecosystem, output {{"activities": []}}
4. Each activity MUST involve AI, technology, or startup/innovation ecosystem actions.

Output a JSON with key "activities" (list), each item having:
"what"        (string: what happened — the core AI/tech action)
"who_else"    (list of strings: other company/startup/investor names involved)
"when"        (string: YYYY-MM-DD if known, or YYYY or "Unknown")
"amount"      (string: financial amount with currency if any, else "Unknown")
"source_hint" (string: which source or URL this came from, or "Unknown")

If no AI-related activities found, output {{"activities": []}}
Output ONLY raw JSON.
"""
    return _call_llm(prompt)


def _pass_2_classify_and_link(entity_name, facts, conn):
    """Pass 2: Classify each activity, assign type, and link related entities."""
    if not facts or not facts.get('activities'):
        return []

    activities_str = json.dumps(facts['activities'], ensure_ascii=False, indent=2)
    prompt = f"""You are a Classification Agent for the Turkish AI Ecosystem.

Entity: "{entity_name}"
Raw Activities:
{activities_str}

For EACH activity, classify it and identify relationships. Output a JSON with key "classified" (list):
"activity_type"   (one of: investment, partnership, acquisition, program_launch, accelerator_batch, research_grant, product_launch, event)
"headline"        (string: short headline, max 15 words)
"when"            (string: YYYY-MM-DD or YYYY — keep from input)
"amount"          (string: from input or "Unknown")
"round_type"      (string: Seed/Series A/etc. for investments, else "Unknown")
"related_startups"  (list of strings: startup names from "who_else")
"related_investors" (list of strings: investor/fund names from "who_else")
"related_entities"  (list of strings: corporate/teknopark/accelerator names from "who_else")
"source_hint"     (string: kept from input)
"skip"            (boolean: MUST be true if ANY of these apply: (a) NOT an AI/tech/startup-related activity, (b) routine business like HR, branch openings, working hours, bill payments, (c) the activity has NO connection to the Turkish tech ecosystem, (d) vague marketing without concrete action)

CRITICAL: Be AGGRESSIVE with skip=true. If in doubt, skip it. Only keep activities that clearly involve AI, technology investment, startup ecosystem actions, or digital transformation.

Output ONLY raw JSON.
"""
    result = _call_llm(prompt)
    if not result:
        return []
    if isinstance(result, list):
        return result
    return result.get('classified', [])


def _pass_3_narrate(entity_name, classified_activity):
    """Pass 3: Generate description, importance, and importance_tags for one activity."""
    act_str = json.dumps(classified_activity, ensure_ascii=False, indent=2)
    tags_vocab = ', '.join(IMPORTANCE_TAG_VOCAB)
    prompt = f"""You are a Data Quality Auditor for the Turkish AI Ecosystem.

Entity: "{entity_name}"
Activity:
{act_str}

Write:
1. "description": 2-3 sentences explaining what this activity includes and who is involved.
2. "importance": 1-2 sentences on why this matters to the Turkish AI ecosystem.
3. "importance_tags": 1-4 comma-separated tags from this list ONLY: {tags_vocab}

Output ONLY raw JSON with keys: "description", "importance", "importance_tags"
"""
    return _call_llm(prompt)

def _pass_3_narrate_batch(entity_name, classified_activities):
    """Pass 3: Batch narration to reduce total LLM calls per entity.

    Returns a list of dicts in the same order as `classified_activities`.
    """
    if not classified_activities:
        return []

    tags_vocab = ', '.join(IMPORTANCE_TAG_VOCAB)
    chunk_size = 3  # Smaller chunks improve per-activity JSON reliability.
    all_results = []

    for i in range(0, len(classified_activities), chunk_size):
        chunk = classified_activities[i:i + chunk_size]
        chunk_str = json.dumps(chunk, ensure_ascii=False, indent=2)

        prompt = f"""You are a Data Quality Auditor for the Turkish AI Ecosystem.
Entity: "{entity_name}"

You will receive a JSON array of classified activities.
For EACH activity in the array (in the same order), generate:
1) "description": 2-3 sentences describing what happened and who is involved
2) "importance": 1-2 sentences on why it matters to the Turkish AI ecosystem
3) "importance_tags": 1-4 comma-separated tags ONLY from this list:
{tags_vocab}

Hard rules:
- Output ONLY raw JSON.
- The output MUST have the same length as the input array.
- If you cannot find enough context for an activity, use "Unknown".

Output schema:
{{"results": [{{"description":"...","importance":"...","importance_tags":"..."}}, ... ]}}

Input activities:
{chunk_str}
"""
        result = _call_llm(prompt)
        if not result:
            return None
        results = result.get("results") if isinstance(result, dict) else None
        if not isinstance(results, list) or len(results) != len(chunk):
            return None

        # Validate importance_tags are drawn from the allowed vocabulary.
        allowed_tags = {t.strip() for t in IMPORTANCE_TAG_VOCAB}
        for local_idx, narrated in enumerate(results):
            try:
                tags_str = narrated.get("importance_tags")
                if not isinstance(tags_str, str):
                    raise ValueError("importance_tags missing or not a string")
                tags = [t.strip() for t in tags_str.split(",") if t.strip()]
                if not tags:
                    raise ValueError("no parsed importance_tags")
                # Strict subset check (case-sensitive after stripping).
                bad = [t for t in tags if t not in allowed_tags]
                if bad:
                    raise ValueError(f"invalid tags: {bad[:3]}")
            except Exception:
                # Fallback to per-activity narration for reliability.
                global_idx = i + local_idx
                fallback = _pass_3_narrate(entity_name, classified_activities[global_idx])
                if isinstance(fallback, dict):
                    results[local_idx] = fallback

        all_results.extend(results)

    return all_results


# ─────────────────────────────────────────────────────────────────────────────
# Three-Pass Entity Profile Enrichment
# ─────────────────────────────────────────────────────────────────────────────

def _gather_entity_context(entity_name, entity_type, website=None):
    """Run targeted searches and scrape relevant URLs for entity enrichment."""
    templates = SEARCH_TEMPLATES.get(entity_type, SEARCH_TEMPLATES.get('Corporate', []))
    queries = [t.format(name=entity_name) for t in templates[:3]]
    queries += [
        f'"{entity_name}" {_CFG.region_name} {entity_type} {_CFG.technology_domain} portfolio programs',
        f'"{entity_name}" site:crunchbase.com OR site:linkedin.com',
        f'"{entity_name}" {_CFG.region_name_local} hakkında kurucular yönetim',
    ]

    search_parts = []
    scrape_urls = []
    for q in queries:
        result = search_with_rotation(q, max_results=5)
        if result:
            search_parts.append(result)
            parsed = parse_search_results(result)
            for p in parsed:
                url = p.get('url', '')
                if any(d in url for d in ['crunchbase.com', 'linkedin.com']) or \
                   entity_name.lower().replace(' ', '') in url.lower():
                    scrape_urls.append(url)

    # Scrape entity's own website if available
    if website and website != 'Unknown':
        scrape_urls.insert(0, website)

    scraped_text = ""
    for url in scrape_urls[:3]:
        text = scrape_website_text(url)
        if text and len(text) > 100:
            scraped_text += f"\n--- Scraped from {url} ---\n{text[:2000]}\n"

    return "\n".join(search_parts), scraped_text


def _entity_enrich_pass_1(entity_name, entity_type, search_context, scraped_text):
    """Pass 1: Entity-type-aware fact extraction."""
    type_specific = {
        'Accelerator': "batch frequency, portfolio companies, success stories, demo day info, application process, fund size if any",
        'Hub': "resident companies, sector focus, facilities, programs offered, resident count",
        'Corporate': "AI initiatives, startup investments, innovation programs, CVC fund size, digital transformation projects",
        'CVC': "fund size, portfolio companies, investment stages, sector focus, managing partners",
        'Investor': "fund size, portfolio companies, investment stages, sector focus, managing partners",
        'Teknopark': "resident companies, sector focus, facilities, resident count, special programs",
        'Government': "programs, grants, budgets, policy initiatives, funded startups",
        'Media': "focus areas, audience reach, notable coverage, events organized",
        'University': "research labs, spin-offs, incubator programs, notable faculty in AI",
    }
    extra = type_specific.get(entity_type, "key programs, partnerships, notable activities")

    prompt = f"""You are a Fact-Extraction Agent for ecosystem entities. Extract detailed facts about this entity.

Entity Name: {entity_name}
Entity Type: {entity_type}

Web Search Results:
{search_context[:4000]}

Scraped Web Pages:
{scraped_text[:3000] if scraped_text else 'No pages scraped.'}

Extract ONLY the facts you can find. For this entity type, pay special attention to: {extra}

Output a JSON with these keys:
"website" (string, URL or "Unknown")
"city" (string, city name or "Unknown")
"description" (string, 2-3 sentence description of what this entity does, or "Unknown")
"founded_year" (integer or null)
"key_people" (list of strings: founder/director/partner names)
"portfolio_or_members" (list of strings: portfolio companies, member startups, or residents)
"programs" (list of strings: program names — accelerator batches, grants, initiatives)
"fund_details" (string: fund size, investment range, or "Unknown")
"stage_focus" (string: Seed, Early, Growth, Late, Multi-stage, or "Unknown")
"ai_focus_areas" (string: comma-separated AI/tech domains they focus on, or "Unknown")
"social_media" (object: {{"linkedin": str, "twitter": str}} or empty object)

CRITICAL: Only include facts you can verify from the search results. Do NOT fabricate data.
If you cannot find evidence, output "Unknown" or an empty list. Output ONLY the raw JSON.
"""
    return _call_llm(prompt, model_type="fast")


def _entity_enrich_pass_2(facts, entity_name, entity_type):
    """Pass 2: Classification, validation, and tech proximity scoring."""
    prompt = f"""You are a Classification Agent for ecosystem entities.

Entity: {entity_name} (Type: {entity_type})
Extracted Facts:
{json.dumps(facts, indent=2, ensure_ascii=False)}

Tasks:

1. Validate and clean the description. It should be 2-3 factual sentences.

2. Classify AI focus areas as a clean comma-separated string (e.g., "NLP, Computer Vision, Fintech AI").

3. Estimate tech_proximity (0-100): How close is this entity to {_CFG.technology_domain}?
   - 90-100: Dedicated {_CFG.technology_domain} entity (AI-focused accelerator, AI research lab)
   - 70-89: Strong {_CFG.technology_domain} focus with some non-{_CFG.technology_domain} activities
   - 40-69: General tech entity with some {_CFG.technology_domain} involvement
   - 10-39: Sector-agnostic with occasional {_CFG.technology_domain} connections
   - 0-9: No {_CFG.technology_domain} focus

4. {_CFG.investor_domestic_instruction.replace('startups', 'companies/startups')}

5. For portfolio_or_members, identify which are {_CFG.region_name}-based companies.

6. Estimate data completeness (0-100): How complete is this profile?

Output JSON:
"description" (string, validated)
"ai_focus_areas" (string, comma-separated)
"tech_proximity" (integer 0-100)
"domestic_portfolio" (list of strings, {_CFG.region_name} companies only)
"total_portfolio_count" (integer, estimated)
"stage_focus" (string)
"data_completeness" (integer 0-100)

Output ONLY the raw JSON.
"""
    return _call_llm(prompt)


def _entity_enrich_pass_3(facts, classification, entity_name, entity_type):
    """Pass 3: Final formatting, normalization, and audit."""
    prompt = f"""You are a Data Quality Auditor for ecosystem entity profiles.

Entity: {entity_name} (Type: {entity_type})
Facts: {json.dumps(facts, indent=2, ensure_ascii=False)}
Classification: {json.dumps(classification, indent=2, ensure_ascii=False)}

Rules:
1. {_CFG.investor_character_rules.replace('investor', 'entity')}
2. City should be a recognized city name with proper characters.
3. Key people: list of proper names with correct characters.
4. Programs: clean list of program/initiative names.
5. Fund details in {_CFG.funding_format_description} or "Unknown".
6. Description: factual, 2-3 sentences, no marketing language.

Output a clean JSON:
"website" (string or "Unknown")
"city" (string or "Unknown")
"description" (string)
"founded_year" (integer or null)
"key_people" (string, comma-separated names)
"portfolio_or_members" (list of strings, all companies)
"domestic_portfolio" (list of strings, {_CFG.region_name} companies only)
"programs" (list of strings)
"fund_details" (string or "Unknown")
"stage_focus" (string or "Unknown")
"ai_focus_areas" (string, comma-separated)
"ai_initiatives_summary" (string, 2-3 sentences about their {_CFG.technology_domain} strategy)
"tech_proximity" (integer 0-100)
"social_media_urls" (string, JSON-encoded object or "Unknown")

Output ONLY the raw JSON.
"""
    return _call_llm(prompt, model_type="fast")


def three_pass_entity_enrichment(conn, entity_id):
    """Run the full three-pass enrichment for one ecosystem entity."""
    cursor = conn.cursor()
    row = cursor.execute(
        "SELECT entity_name, entity_type, website, blog_url FROM ecosystem_entities WHERE id = ?",
        (entity_id,)
    ).fetchone()
    if not row:
        return False

    entity_name, entity_type, website, blog_url = row
    print(f"\n  [{entity_id}] Enriching entity: {entity_name} ({entity_type})")

    # Gather context
    search_context, scraped_text = _gather_entity_context(entity_name, entity_type, website)
    if not search_context.strip() and not scraped_text.strip():
        print(f"    [Skip] No search results for {entity_name}")
        return False

    # Pass 1: Fact Extraction
    facts = _entity_enrich_pass_1(entity_name, entity_type, search_context, scraped_text)
    if not facts:
        print(f"    [Pass 1] Researcher returned nothing.")
        return False
    print(f"    [Pass 1] Facts extracted.")

    # Pass 2: Classification
    classification = _entity_enrich_pass_2(facts, entity_name, entity_type)
    if not classification:
        print(f"    [Pass 2] Classifier returned nothing.")
        return False
    print(f"    [Pass 2] Tech proximity: {classification.get('tech_proximity', '?')} | Portfolio: {classification.get('total_portfolio_count', '?')}")

    # Pass 3: Formatting
    result = _entity_enrich_pass_3(facts, classification, entity_name, entity_type)
    if not result:
        print(f"    [Pass 3] Auditor returned nothing.")
        return False
    print(f"    [Pass 3] Profile complete.")

    # Write to DB
    now = get_utc_now()

    field_map = {
        'website': result.get('website'),
        'city': result.get('city'),
        'description': result.get('description'),
        'ai_focus_areas': result.get('ai_focus_areas'),
        'ai_initiatives_summary': result.get('ai_initiatives_summary'),
        'founded_year': result.get('founded_year'),
        'key_people': result.get('key_people'),
        'fund_details': result.get('fund_details'),
        'stage_focus': result.get('stage_focus'),
        'tech_proximity': result.get('tech_proximity'),
    }

    for field, value in field_map.items():
        if value and str(value).strip().lower() not in ('unknown', 'none', 'null', ''):
            agent_update_field(cursor, 'ecosystem_entity', entity_id, 'M3_entity_enrichment', 70, field, value)

    # Store list fields as JSON
    portfolio = result.get('portfolio_or_members', [])
    if portfolio and isinstance(portfolio, list):
        cursor.execute("UPDATE ecosystem_entities SET portfolio_or_members = ? WHERE id = ?",
                       (json.dumps(portfolio, ensure_ascii=False), entity_id))

    programs = result.get('programs', [])
    if programs and isinstance(programs, list):
        cursor.execute("UPDATE ecosystem_entities SET programs = ? WHERE id = ?",
                       (json.dumps(programs, ensure_ascii=False), entity_id))

    social = result.get('social_media_urls')
    if social and social != 'Unknown':
        social_str = social if isinstance(social, str) else json.dumps(social, ensure_ascii=False)
        cursor.execute("UPDATE ecosystem_entities SET social_media_urls = ? WHERE id = ?",
                       (social_str, entity_id))

    # Store enrichment sources
    sources = []
    if result.get('website') and result['website'] != 'Unknown':
        sources.append(result['website'])
    cursor.execute("UPDATE ecosystem_entities SET enrichment_sources = ?, last_updated = ? WHERE id = ?",
                   (json.dumps(sources, ensure_ascii=False), now, entity_id))

    # Calculate data_confidence
    confidence = 0
    if result.get('website') and result['website'] != 'Unknown':
        confidence += 15
    if result.get('description') and result['description'] != 'Unknown':
        confidence += 15
    if result.get('city') and result['city'] != 'Unknown':
        confidence += 10
    if result.get('ai_focus_areas') and result['ai_focus_areas'] != 'Unknown':
        confidence += 15
    if result.get('key_people') and result['key_people'] != 'Unknown':
        confidence += 10
    if portfolio:
        confidence += 15
    if programs:
        confidence += 10
    if result.get('tech_proximity') is not None:
        confidence += 10
    confidence = min(confidence, 100)

    cursor.execute("""
        UPDATE ecosystem_entities
        SET data_confidence = ?, needs_enrichment = 0, last_scanned = ?
        WHERE id = ?
    """, (confidence, now, entity_id))

    conn.commit()
    print(f"    [DB] Updated. Confidence: {confidence}")

    # Cross-reference portfolio companies with startups table
    # Use investor_id=None to skip investment record creation (entity ≠ investor)
    domestic_portfolio = result.get('domestic_portfolio', [])
    if domestic_portfolio:
        _discover_entity_portfolio(conn, entity_id, entity_name, domestic_portfolio)

    return True


def _discover_entity_portfolio(conn, entity_id, entity_name, portfolio_list):
    """Cross-reference portfolio companies, creating skeleton startups if new.
    Unlike investor portfolio discovery, this does NOT create investment records."""
    if not portfolio_list or not isinstance(portfolio_list, list):
        return
    from rapidfuzz import fuzz
    from agent_core import normalize_turkish
    cursor = conn.cursor()
    discovered = 0
    for company in portfolio_list:
        if not company or not isinstance(company, str):
            continue
        company = company.strip()
        if not is_valid_company_name(company):
            continue
        # Check if already in DB
        all_startups = cursor.execute("SELECT id, company_name FROM startups").fetchall()
        matched = any(
            fuzz.token_sort_ratio(normalize_turkish(company), normalize_turkish(sname)) >= 80
            for _, sname in all_startups
        )
        if not matched:
            try:
                cursor.execute('''
                    INSERT INTO startups (company_name, Status, data_confidence, processed_at)
                    VALUES (?, 'Active', 0, ?)
                ''', (company, get_utc_now()))
                discovered += 1
                print(f"      [New] Created skeleton startup: {company}")
            except Exception:
                pass
    if discovered > 0:
        conn.commit()
        print(f"    [Portfolio] Discovered {discovered} new startups from {entity_name}'s portfolio.")
        fuzzy_matcher.invalidate()


# ─────────────────────────────────────────────────────────────────────────────
# Activity writer
# ─────────────────────────────────────────────────────────────────────────────
def _write_activity(conn, entity_id, entity_name, classified, narrated,
                    articles, activity_date, startup_feedback=True):
    """Write one activity to ecosystem_activities + junction table + startup feedback."""
    cursor = conn.cursor()

    activity_type = classified.get('activity_type', 'partnership')
    if activity_type not in VALID_ACTIVITY_TYPES:
        activity_type = 'partnership'

    headline = classified.get('headline', '')
    amount_str = classified.get('amount', 'Unknown')
    round_type = normalize_round_type(classified.get('round_type', ''))
    source_hint = classified.get('source_hint', '')

    description = narrated.get('description') if narrated else None
    importance = narrated.get('importance') if narrated else None
    importance_tags = narrated.get('importance_tags') if narrated else None

    # Parse financial data
    usd_amount = None
    currency = None
    amount = None
    if amount_str and amount_str != 'Unknown':
        _, usd_amount, currency = convert_to_usd(amount_str, activity_date or '')
        amount = amount_str

    # Match source article for source_url
    source_url = None
    source_type = 'news'
    source_name = 'ecosystem_scanner'
    for art in articles:
        if source_hint and source_hint[:20].lower() in art['url'].lower():
            source_url = art['url']
            source_type = art['source_type']
            source_name = art['source_name']
            break
    if not source_url and articles:
        source_url = articles[0]['url']
        source_type = articles[0]['source_type']
        source_name = articles[0]['source_name']

    # Dedup: include source_url to avoid near-duplicates caused by LLM headline variation.
    url_hash = hashlib.md5(
        f"eco::{entity_id}::{headline}::{activity_date}::{(source_url or '')}".encode()
    ).hexdigest()
    if cursor.execute("SELECT id FROM ecosystem_activities WHERE url_hash = ?", (url_hash,)).fetchone():
        return None

    # Activity-level dedup: (entity_id, activity_type, activity_date, amount)
    if activity_date and activity_date != 'Unknown':
        dup = cursor.execute("""
            SELECT id FROM ecosystem_activities
            WHERE entity_id = ? AND activity_type = ? AND activity_date = ? AND amount IS ?
        """, (entity_id, activity_type, activity_date, amount)).fetchone()
        if dup:
            return None

    # Primary startup/investor IDs
    related_startups = classified.get('related_startups', [])
    related_investors = classified.get('related_investors', [])
    related_entity_names = classified.get('related_entities', [])

    primary_startup_id = None
    primary_investor_id = None
    for s in related_startups:
        match = _fuzzy_find_startup(conn, s)
        if match:
            primary_startup_id = match[0] if isinstance(match, tuple) else match
            break
    for i in related_investors:
        match = _fuzzy_find_investor(conn, i)
        if match:
            primary_investor_id = match[0] if isinstance(match, tuple) else match
            break

    needs_enrichment = 0 if (description and source_url) else 1
    conf = 70 if (description and source_url) else 35

    try:
        cursor.execute("""
            INSERT INTO ecosystem_activities (
                activity_type, headline, description, importance, importance_tags,
                source_url, source_name, source_type, activity_date,
                entity_id, startup_id, investor_id,
                amount, currency, amount_usd, round_type,
                url_hash, data_confidence, needs_enrichment, scanned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            activity_type, headline, description, importance, importance_tags,
            source_url, source_name, source_type,
            activity_date if activity_date != 'Unknown' else None,
            entity_id, primary_startup_id, primary_investor_id,
            amount, currency, usd_amount, round_type if round_type != 'Unknown' else None,
            url_hash, conf, needs_enrichment, get_utc_now()
        ))
        activity_id = cursor.lastrowid

        # Junction table — acting entity
        cursor.execute(
            "INSERT INTO ecosystem_activity_entities (activity_id, entity_type, entity_id, role) VALUES (?, 'ecosystem_entity', ?, 'actor')",
            (activity_id, entity_id)
        )

        # Related startups
        for s in related_startups:
            match = _fuzzy_find_startup(conn, s)
            if match:
                sid = match[0] if isinstance(match, tuple) else match
                cursor.execute(
                    "INSERT INTO ecosystem_activity_entities (activity_id, entity_type, entity_id, role) VALUES (?, 'startup', ?, 'recipient')",
                    (activity_id, sid)
                )
                if startup_feedback and activity_type == 'investment' and amount and amount != 'Unknown':
                    existing = cursor.execute(
                        "SELECT Total_Funding_Formatted, Investors FROM startups WHERE id = ?", (sid,)
                    ).fetchone()
                    if existing:
                        cur_fund, cur_inv = existing
                        if not cur_fund or cur_fund in ('Unknown', 'None', ''):
                            agent_update_field(cursor, "startup", sid, "agent_ecosystem", conf, "Total_Funding_Formatted", amount)
                        if not cur_inv or cur_inv in ('Unknown', 'None', ''):
                            investor_names = ', '.join(n for n in related_investors if n)
                            if investor_names:
                                agent_update_field(cursor, "startup", sid, "agent_ecosystem", conf, "Investors", investor_names)

        # Related investors
        for i in related_investors:
            match = _fuzzy_find_investor(conn, i)
            if match:
                iid = match[0] if isinstance(match, tuple) else match
                role = 'actor' if activity_type == 'investment' else 'partner'
                cursor.execute(
                    "INSERT INTO ecosystem_activity_entities (activity_id, entity_type, entity_id, role) VALUES (?, 'investor', ?, ?)",
                    (activity_id, iid, role)
                )

        # Related ecosystem entities
        for e in related_entity_names:
            eid2 = _fuzzy_find_entity(conn, e)
            if eid2 and eid2 != entity_id:
                cursor.execute(
                    "INSERT INTO ecosystem_activity_entities (activity_id, entity_type, entity_id, role) VALUES (?, 'ecosystem_entity', ?, 'partner')",
                    (activity_id, eid2)
                )

        conn.commit()
        return activity_id

    except Exception as e:
        print(f"    [!] Write failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Entity profile updater
# ─────────────────────────────────────────────────────────────────────────────
def _update_entity_profile(conn, entity_id, entity_name, activities):
    """After processing, update ai_focus_areas and ai_initiatives_summary."""
    if not activities:
        return

    cursor = conn.cursor()
    activity_headlines = '; '.join(
        a.get('headline', '') for a in activities[:10] if a.get('headline')
    )

    prompt = f"""Based on these recent AI activities for "{entity_name}":
{activity_headlines}

Output a JSON with:
"ai_focus_areas"          (string: comma-separated AI domains, max 6, e.g. "NLP, Computer Vision, Fintech AI")
"ai_initiatives_summary"  (string: 2-3 sentence summary of this entity's overall AI strategy/positioning)

Output ONLY raw JSON.
"""
    result = _call_llm(prompt)
    if result:
        focus = result.get('ai_focus_areas')
        summary = result.get('ai_initiatives_summary')
        now = get_utc_now()
        cursor.execute("""
            UPDATE ecosystem_entities
            SET ai_focus_areas = COALESCE(?, ai_focus_areas),
                ai_initiatives_summary = COALESCE(?, ai_initiatives_summary),
                last_scanned = ?, last_updated = ?, data_confidence = 70,
                needs_enrichment = 0
            WHERE id = ?
        """, (focus, summary, now, now, entity_id))
        conn.commit()
        print(f"    [Profile] Focus: {focus}")


# ─────────────────────────────────────────────────────────────────────────────
# Main scan loop
# ─────────────────────────────────────────────────────────────────────────────
def main():
    conn = get_db_connection(DB_NAME)
    clear_search_cache()
    _load_indexes(conn)
    cursor = conn.cursor()

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=REFRESH_DAYS)).isoformat().replace('+00:00', 'Z')

    # Load entities due for scan: priority (needs_enrichment/low conf) + stale
    entities = cursor.execute("""
        SELECT id, entity_name, entity_type, blog_url, data_confidence, needs_enrichment
        FROM ecosystem_entities
        WHERE entity_type NOT IN ('Media', 'Other')
           OR needs_enrichment = 1
        ORDER BY needs_enrichment DESC, data_confidence ASC, last_scanned ASC NULLS FIRST
        LIMIT 30
    """).fetchall()

    # Also include Media entities but only if they need enrichment
    media_entities = cursor.execute("""
        SELECT id, entity_name, entity_type, blog_url, data_confidence, needs_enrichment
        FROM ecosystem_entities
        WHERE entity_type = 'Media' AND needs_enrichment = 1
    """).fetchall()

    all_entities = entities + [e for e in media_entities if e not in entities]

    # Filter by refresh schedule: skip already-enriched entities scanned recently
    to_scan = []
    for row in all_entities:
        eid, ename, etype, blog_url, conf, needs = row
        if needs or (conf or 0) < PRIORITY_THRESHOLD:
            to_scan.append(row)
        else:
            last_scanned = cursor.execute(
                "SELECT last_scanned FROM ecosystem_entities WHERE id = ?", (eid,)
            ).fetchone()
            ls = last_scanned[0] if last_scanned and last_scanned[0] else None
            if not ls or ls < cutoff:
                to_scan.append(row)

    print(f"\nModule 3: Ecosystem Scanner — {len(to_scan)} entities to scan")
    print("=" * 60)

    # ── Phase A: Profile enrichment for sparse entities ──────────────────
    enrich_candidates = cursor.execute("""
        SELECT id, entity_name, entity_type
        FROM ecosystem_entities
        WHERE (needs_enrichment = 1 OR data_confidence IS NULL OR data_confidence < 30)
          AND entity_type NOT IN ('Media')
        ORDER BY COALESCE(data_confidence, 0) ASC
        LIMIT 20
    """).fetchall()

    if enrich_candidates:
        print(f"\n  Enriching {len(enrich_candidates)} sparse entity profiles first...")
        enriched_count = 0
        for eid, ename, etype in enrich_candidates:
            success = three_pass_entity_enrichment(conn, eid)
            if success:
                enriched_count += 1
            time.sleep(1)
        print(f"  Enriched {enriched_count}/{len(enrich_candidates)} entity profiles.\n")

    # ── Phase B: Activity scanning ───────────────────────────────────────
    total_activities = 0

    # Pipeline: prefetch articles for next entity while processing current one
    from concurrent.futures import ThreadPoolExecutor, Future

    prefetch_pool = ThreadPoolExecutor(max_workers=1)
    prefetched_future = None  # type: Future | None

    def _prefetch(ename, etype, blog_url):
        return _gather_articles(ename, etype, blog_url, max_per_query=5)

    # Kick off prefetch for the first entity
    if to_scan:
        eid0, ename0, etype0, blog_url0, _, _ = to_scan[0]
        prefetched_future = prefetch_pool.submit(_prefetch, ename0, etype0, blog_url0)

    for idx, (eid, ename, etype, blog_url, conf, needs) in enumerate(to_scan):
        print(f"\n[{eid}] {ename} ({etype})")

        # 1. Get articles (from prefetch or direct call)
        if prefetched_future is not None:
            articles = prefetched_future.result()
            prefetched_future = None
        else:
            articles = _gather_articles(ename, etype, blog_url, max_per_query=5)

        # Prefetch NEXT entity's articles while we process current one
        if idx + 1 < len(to_scan):
            next_eid, next_ename, next_etype, next_blog, _, _ = to_scan[idx + 1]
            prefetched_future = prefetch_pool.submit(_prefetch, next_ename, next_etype, next_blog)

        if not articles:
            print(f"    No articles found. Marking scanned.")
            cursor.execute(
                "UPDATE ecosystem_entities SET last_scanned = ? WHERE id = ?",
                (get_utc_now(), eid)
            )
            conn.commit()
            continue

        print(f"    Found {len(articles)} articles. Running three-pass analysis...")

        # 2. Concatenate all article texts for batch LLM input
        combined_text = '\n\n---\n\n'.join(
            f"Source: {a['source_name']} | URL: {a['url']}\n{a['text']}"
            for a in articles
        )

        # 3. Pass 1: Extract facts from ALL articles at once
        facts = _pass_1_extract_facts(ename, etype, combined_text)
        if not facts or not isinstance(facts, dict) or not facts.get('activities'):
            print(f"    [Pass 1] No activities extracted.")
            cursor.execute(
                "UPDATE ecosystem_entities SET last_scanned = ?, needs_enrichment = 0 WHERE id = ?",
                (get_utc_now(), eid)
            )
            conn.commit()
            continue
        print(f"    [Pass 1] Extracted {len(facts['activities'])} raw activities.")

        # 4. Pass 2: Classify and link all activities
        classified_list = _pass_2_classify_and_link(ename, facts, conn)
        valid = [c for c in classified_list if not c.get('skip', False)]
        print(f"    [Pass 2] {len(valid)} valid activities after filtering.")

        # 5. Pass 3 + write: batch narration per entity (fewer LLM calls)
        entity_classified = []
        narrated_list = _pass_3_narrate_batch(ename, valid)
        for act_idx, act in enumerate(valid):
            narrated = None
            if isinstance(narrated_list, list) and act_idx < len(narrated_list):
                narrated = narrated_list[act_idx]
            if not narrated:
                # Fallback: preserve prior behavior if batch output is malformed.
                narrated = _pass_3_narrate(ename, act)
            activity_date = act.get('when')

            aid = _write_activity(
                conn, eid, ename, act, narrated, articles, activity_date
            )
            if aid:
                total_activities += 1
                entity_classified.append(act)
                print(f"    [+] Activity: {act.get('headline', '')[:60]}")

        # 6. Update entity AI profile summary
        _update_entity_profile(conn, eid, ename, entity_classified)

    prefetch_pool.shutdown(wait=False)

    # Log scan
    cursor.execute("""
        INSERT INTO scan_log (module_name, scan_start, scan_end, source_scanned, items_found, items_updated, status)
        VALUES ('M3_ecosystem_scanner', ?, ?, 'ecosystem_entities', ?, ?, 'completed')
    """, (get_utc_now(), get_utc_now(), len(to_scan), total_activities))
    conn.commit()

    print(f"\n{'='*60}")
    print(f"Ecosystem scan complete. Entities scanned: {len(to_scan)}, Activities logged: {total_activities}")
    print(f"{'='*60}")
    conn.close()


if __name__ == "__main__":
    main()
