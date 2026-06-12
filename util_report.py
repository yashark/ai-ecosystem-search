"""
Company Research Report Tool
Usage:
    python3 util_report.py --name "Koç Holding"                    # Deep research (default)
    python3 util_report.py --name "Aselsan" --report               # Quick DB-only
    python3 util_report.py --name "Startup X" --domain x.co --fresh  # Force fresh
    python3 util_report.py --name "Aselsan" --format json          # JSON output
    python3 util_report.py --name "NewCo" --domain newco.com --update-db  # Enrich + save to DB
"""
import argparse
import sqlite3
import json
import os
import re
import hashlib
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from rapidfuzz import fuzz

from agent_core import (
    search_with_rotation, search_news_with_rotation, scrape_website_text,
    _call_llm, agent_update_field, get_utc_now, clear_search_cache,
    three_pass_enrichment, detect_entity_type_heuristic, find_crunchbase_url,
    three_pass_investor_enrichment, enrich_investor,
    EXTERNAL_NEWS_SOURCES, parse_search_results, fuzzy_matcher,
    is_relevant_result, normalize_turkish
)

DB_NAME = "Master.db"
REPORTS_DIR = Path("reports")
CACHE_MAX_AGE_HOURS = 6

# ═══════════════════════════════════════════════════════════════════════════════
# 1. DB LOOKUP
# ═══════════════════════════════════════════════════════════════════════════════
def db_lookup(conn, name=None, domain=None):
    """Fuzzy search across all tables. Returns dict with all found data."""
    cursor = conn.cursor()
    result = {
        'found': False, 'entity_class': None,
        'startup': None, 'investor': None, 'entity': None,
        'activities': [], 'news': [], 'relationships': [],
        'change_log': [], 'entity_id': None,
    }

    # --- Startups ---
    startups = cursor.execute("SELECT * FROM startups").fetchall()
    col_names = [d[0] for d in cursor.description]
    for row in startups:
        s = dict(zip(col_names, row))
        match = False
        if name and fuzz.token_sort_ratio(normalize_turkish(name), normalize_turkish(s.get('company_name') or '')) >= 80:
            match = True
        if domain and s.get('website') and domain.lower() in s['website'].lower():
            match = True
        if match:
            result['startup'] = s
            result['found'] = True
            result['entity_class'] = 'startup'
            result['entity_id'] = ('startup', s['id'])
            break

    # --- Investors ---
    investors = cursor.execute("SELECT * FROM investors").fetchall()
    inv_cols = [d[0] for d in cursor.description]
    for row in investors:
        inv = dict(zip(inv_cols, row))
        match = False
        if name and fuzz.token_sort_ratio(normalize_turkish(name), normalize_turkish(inv.get('investor_name') or '')) >= 80:
            match = True
        if domain and inv.get('website') and domain.lower() in inv['website'].lower():
            match = True
        if match:
            result['investor'] = inv
            result['found'] = True
            result['entity_class'] = result['entity_class'] or 'investor'
            result['entity_id'] = result['entity_id'] or ('investor', inv['investor_id'])
            break

    # --- Ecosystem entities ---
    entities = cursor.execute("SELECT * FROM ecosystem_entities").fetchall()
    ent_cols = [d[0] for d in cursor.description]
    for row in entities:
        ent = dict(zip(ent_cols, row))
        match = False
        if name and fuzz.token_sort_ratio(normalize_turkish(name), normalize_turkish(ent.get('entity_name') or '')) >= 80:
            match = True
        if domain and ent.get('website') and domain.lower() in ent['website'].lower():
            match = True
        if match:
            result['entity'] = ent
            result['found'] = True
            result['entity_class'] = result['entity_class'] or 'ecosystem_entity'
            result['entity_id'] = result['entity_id'] or ('ecosystem_entity', ent['id'])
            break

    # --- Activities ---
    if result['startup']:
        sid = result['startup']['id']
        result['activities'] = [dict(zip([d[0] for d in cursor.description], r))
            for r in cursor.execute("SELECT * FROM ecosystem_activities WHERE startup_id = ?", (sid,)).fetchall()]
        # Add junction-linked activities
        junct = cursor.execute("""
            SELECT ea.* FROM ecosystem_activities ea
            JOIN ecosystem_activity_entities eae ON eae.activity_id = ea.id
            WHERE eae.entity_type = 'startup' AND eae.entity_id = ?
        """, (sid,)).fetchall()
        if junct:
            jcols = [d[0] for d in cursor.description]
            for r in junct:
                a = dict(zip(jcols, r))
                if a['id'] not in {x['id'] for x in result['activities']}:
                    result['activities'].append(a)

    if result['entity']:
        eid = result['entity']['id']
        ent_acts = cursor.execute("SELECT * FROM ecosystem_activities WHERE entity_id = ?", (eid,)).fetchall()
        if ent_acts:
            acols = [d[0] for d in cursor.description]
            for r in ent_acts:
                a = dict(zip(acols, r))
                if a['id'] not in {x['id'] for x in result['activities']}:
                    result['activities'].append(a)

    # --- News mentions ---
    if result['startup']:
        sid = result['startup']['id']
        result['news'] = [dict(zip([d[0] for d in cursor.description], r))
            for r in cursor.execute("SELECT * FROM news_mentions WHERE startup_id = ? AND COALESCE(is_relevant, 1) = 1", (sid,)).fetchall()]

    # --- Relationships from junction table ---
    for act in result['activities']:
        rels = cursor.execute("""
            SELECT entity_type, entity_id, role FROM ecosystem_activity_entities WHERE activity_id = ?
        """, (act['id'],)).fetchall()
        for et, eid2, role in rels:
            result['relationships'].append({
                'activity_id': act['id'], 'entity_type': et, 'entity_id': eid2, 'role': role
            })

    # --- Change log for freshness ---
    if result['startup']:
        result['change_log'] = cursor.execute(
            "SELECT changed_field, new_value, timestamp FROM change_log WHERE entity_type='startup' AND entity_id=? ORDER BY timestamp DESC",
            (result['startup']['id'],)
        ).fetchall()

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 2. CLASSIFY & ENRICH NEW COMPANIES
# ═══════════════════════════════════════════════════════════════════════════════
def classify_and_enrich(conn, name, domain):
    """Classify unknown company and enrich via existing protocols."""
    cursor = conn.cursor()
    website = f"https://{domain}" if domain and not domain.startswith('http') else domain
    website_text = scrape_website_text(website) if website else ''

    # Classify
    entity_type = detect_entity_type_heuristic(name, website_text or '')

    print(f"  [Classify] Detected type: {entity_type}")

    if entity_type == 'startup':
        # Create skeleton
        cursor.execute("""
            INSERT INTO startups (company_name, website, Status, first_seen)
            VALUES (?, ?, 'Active', ?)
        """, (name, website, get_utc_now()))
        sid = cursor.lastrowid
        conn.commit()

        # Three-pass enrichment
        search_ctx = search_with_rotation(f'"{name}" yapay zeka OR AI startup Turkey')
        startup_data = {'company_name': name, 'website': website, 'id': sid}
        enriched = three_pass_enrichment(startup_data, search_ctx or '', website_text or '')
        if enriched:
            for field, value in enriched.items():
                if value and value != 'Unknown':
                    agent_update_field(cursor, "startup", sid, "util_report", 80, field, str(value))
            conn.commit()

        # Find Crunchbase
        cb_url = find_crunchbase_url(name, website_text[:200] if website_text else '')
        if cb_url:
            agent_update_field(cursor, "startup", sid, "util_report", 85, "crunchbase_url", cb_url)
            conn.commit()

        return 'startup', sid

    elif entity_type == 'investor':
        cursor.execute(
            "INSERT INTO investors (investor_name, website, processed_at) VALUES (?, ?, ?)",
            (name, website, get_utc_now())
        )
        iid = cursor.lastrowid
        conn.commit()
        enriched = three_pass_investor_enrichment(name)
        if enriched:
            for field, value in enriched.items():
                if value:
                    cursor.execute(f"UPDATE investors SET {field} = ? WHERE investor_id = ?", (value, iid))
            conn.commit()
        return 'investor', iid

    else:
        # Ecosystem entity
        cursor.execute("""
            INSERT INTO ecosystem_entities (entity_name, entity_type, website, description,
                data_confidence, needs_enrichment, first_seen, last_updated)
            VALUES (?, ?, ?, ?, 0, 1, ?, ?)
        """, (name, 'Corporate', website, f"{name} — auto-discovered by report tool",
              get_utc_now(), get_utc_now()))
        eid = cursor.lastrowid
        conn.commit()
        return 'ecosystem_entity', eid


# ═══════════════════════════════════════════════════════════════════════════════
# 3. ONLINE RESEARCH
# ═══════════════════════════════════════════════════════════════════════════════
def online_research(name, domain=None, blog_url=None, description=None, website=None):
    """Gather articles from multiple channels, anchored by company identity.
    
    Every query uses the company name + domain to prevent noise from 
    generic/ambiguous company names like 'v-count'.
    """
    sources = {
        'news': [], 'blog': [], 'linkedin': [], 'financial': [], 'technical': []
    }
    seen = set()

    # Extract domain from website if not provided
    if not domain and website:
        m = re.search(r'https?://(?:www\.)?([^/]+)', website)
        if m:
            domain = m.group(1)

    # Build identity anchor for all queries
    anchor = f'"{name}"'
    if domain:
        anchor += f' site:{domain} OR "{domain}"'

    def _add(category, url, text, source_name):
        h = hashlib.md5((url or '').encode()).hexdigest()
        if h not in seen and url and text:
            seen.add(h)
            sources[category].append({'url': url, 'text': text[:2000], 'source': source_name})

    def _guess_language(s):
        s = (s or "").lower()
        # Very small heuristic: detect Turkish characters to prefer `tr`.
        if any(ch in s for ch in ["ğ", "ü", "ş", "ı", "ö", "ç"]):
            return "tr"
        return "en"

    guessed_lang = _guess_language(name)

    # ── News: company-specific queries ──
    news_queries = [
        f'"{name}" yapay zeka OR AI yatırım OR ortaklık 2024 2025',
        f'"{name}" artificial intelligence OR startup OR investment 2024 2025',
    ]
    if domain:
        news_queries.append(f'site:{domain} OR "{domain}" haberler OR news OR press')
    for query in news_queries:
        results = search_news_with_rotation(query, max_results=5, language=guessed_lang, time_range="year")
        for r in (results or []):
            url = r.get('url', '')
            text = r.get('body', '') or r.get('title', '')
            # Relevance check: article must mention the company name or domain
            if is_relevant_result(name, text) or (domain and domain.lower() in (url + text).lower()):
                _add('news', url, text, r.get('source', 'news'))
        time.sleep(0.3)

    # ── External media: company-specific ──
    for src in EXTERNAL_NEWS_SOURCES:
        ext = search_with_rotation(f'site:{src} "{name}"', max_results=3)
        if ext:
            for r in parse_search_results(ext):
                url, snippet = r['url'], r['snippet']
                if src in url.lower() and is_relevant_result(name, snippet):
                    _add('news', url, snippet, src)
        time.sleep(0.2)

    # ── Blog: company's own site ──
    if blog_url:
        text = scrape_website_text(blog_url)
        if text:
            _add('blog', blog_url, text, 'entity_blog')
    if domain:
        for path in ['/haberler', '/basin', '/press', '/news', '/blog']:
            page_url = f'https://www.{domain}{path}'
            text = scrape_website_text(page_url)
            if text and len(text) > 100:
                _add('blog', page_url, text, 'company_website')
                break

    # ── LinkedIn: company-specific ──
    li = search_with_rotation(f'"{name}" linkedin.com/company/ OR linkedin.com/posts/ AI 2024 2025', max_results=3)
    if li:
        for r in parse_search_results(li):
            url, snippet = r['url'], r['snippet']
            if 'linkedin.com' in (url or '').lower() and snippet and is_relevant_result(name, snippet):
                _add('linkedin', url, snippet, 'linkedin')

    # ── Technical: anchored to company, not generic ──
    if description:
        tech_query = f'"{name}" technology OR patent OR product "{description[:40]}"'
    else:
        tech_query = f'"{name}" technology OR product OR solution AI'
    if domain:
        tech_query += f' OR site:{domain}'
    tech = search_with_rotation(tech_query, max_results=3)
    if tech:
        for r in parse_search_results(tech):
            url, snippet = r['url'], r['snippet']
            if url and snippet and is_relevant_result(name, snippet + url):
                _add('technical', url, snippet, 'technical_search')

    return sources


# ═══════════════════════════════════════════════════════════════════════════════
# 4. FOUNDER DISCOVERY (Startups Only)
# ═══════════════════════════════════════════════════════════════════════════════
def discover_founders(name, domain=None, crunchbase_url=None, description=None, website=None):
    """Find founders with strict 4-step protocol. EVERY step is anchored to the company.
    
    Step 1: Scrape the company's own website (most reliable)
    Step 2: Scrape Crunchbase people page
    Step 3: Domain-anchored search (only if steps 1-2 found nothing)
    Step 4: Validate — REJECT anyone not confirmed as associated with THIS company
    """
    candidates = []

    # Derive domain from website if not provided
    if not domain and website:
        m = re.search(r'https?://(?:www\.)?([^/]+)', website)
        if m:
            domain = m.group(1)

    company_context = f"{name}"
    if description:
        company_context += f" ({description[:100]})"
    if domain:
        company_context += f" [{domain}]"

    # ── Step 1: Company's own website ──
    if domain:
        for path in ['/about', '/about-us', '/team', '/hakkimizda', '/ekibimiz',
                     '/kurumsal', '/our-team', '/leadership']:
            url = f'https://www.{domain}{path}'
            text = scrape_website_text(url)
            if text and len(text) > 50:
                result = _call_llm(f"""Extract the names of founders, co-founders, and CEO of ONLY this specific company.
Company: "{name}" (website: {domain})
Company description: {description or 'N/A'}

Page text from {url}:
{text[:3000]}

RULES:
- ONLY extract people who are founders/co-founders/CEO of "{name}"
- Do NOT include people from other companies mentioned on the page
- If unsure, do NOT include them

Output JSON: {{"founders": [{{"name": "...", "role": "..."}}]}}
If no founders found, output {{"founders": []}}
Output ONLY raw JSON.""")
                if result and result.get('founders'):
                    for f in result['founders']:
                        if f.get('name'):
                            candidates.append({
                                'name': f['name'], 'role': f.get('role', ''),
                                'source': 'company_website', 'confidence': 'high'
                            })
                if candidates:
                    break  # Found founders on own site — most reliable
        time.sleep(0.3)

    # ── Step 2: Crunchbase people page ──
    if crunchbase_url and not candidates:
        cb_text = scrape_website_text(f'{crunchbase_url}/people')
        if cb_text and len(cb_text) > 50:
            result = _call_llm(f"""Extract founder/co-founder/CEO names from this Crunchbase page.
Company: "{name}" (website: {domain or 'N/A'})

Text: {cb_text[:3000]}

RULES:
- ONLY people who are founders/co-founders/CEO of "{name}"
- Do NOT include board members, advisors, or employees of other companies

Output JSON: {{"founders": [{{"name": "...", "role": "..."}}]}}
Output ONLY raw JSON.""")
            if result and result.get('founders'):
                for f in result['founders']:
                    if f.get('name') and not any(c['name'].lower() == f['name'].lower() for c in candidates):
                        candidates.append({
                            'name': f['name'], 'role': f.get('role', ''),
                            'source': 'crunchbase', 'confidence': 'high'
                        })
        time.sleep(0.3)

    # ── Step 3: Domain-anchored search (only if steps 1-2 found nothing) ──
    if not candidates:
        # Use domain AND name to prevent wrong-company matches
        if domain:
            search_query = f'"{name}" "{domain}" founder OR kurucu OR CEO'
        else:
            search_query = f'"{name}" founder OR kurucu OR CEO startup Turkey'
        search_result = search_with_rotation(search_query, max_results=5)
        if search_result:
            result = _call_llm(f"""From these search results, extract ONLY the founders/CEO of "{name}".
Company website: {domain or 'Unknown'}
Company description: {description or 'N/A'}

Search results:
{search_result[:3000]}

RULES:
- ONLY extract people who are founders/co-founders/CEO of "{name}"
- If the search results mention people from OTHER companies, do NOT include them
- If you are not confident someone is a founder of "{name}", do NOT include them

Output JSON: {{"founders": [{{"name": "...", "role": "..."}}]}}
Output ONLY raw JSON.""")
            if result and result.get('founders'):
                for f in result['founders']:
                    if f.get('name') and not any(c['name'].lower() == f['name'].lower() for c in candidates):
                        candidates.append({
                            'name': f['name'], 'role': f.get('role', ''),
                            'source': 'search', 'confidence': 'low'
                        })

    # ── Step 4: Validate each candidate — REJECT if not confirmed ──
    validated = []
    for c in candidates[:5]:
        if not c['name']:
            continue

        # Validation: search for name + company together
        confirm_query = f'"{c["name"]}" "{name}"'
        if domain:
            confirm_query += f' OR "{domain}"'
        confirm = search_with_rotation(confirm_query, max_results=3)

        # Check if both name and company appear in results
        if confirm:
            confirm_lower = confirm.lower()
            name_in = c['name'].lower()[:8] in confirm_lower
            company_in = is_relevant_result(name, confirm) or (domain and domain.lower() in confirm_lower)
            if name_in and company_in:
                if c['confidence'] != 'high':
                    c['confidence'] = 'medium'
            else:
                c['confidence'] = 'unverified'
        else:
            if c['confidence'] != 'high':  # Don't downgrade website/crunchbase finds
                c['confidence'] = 'unverified'

        # Only include in report if at least medium confidence
        if c['confidence'] == 'unverified':
            print(f"    [!] Rejected founder '{c['name']}' — not confirmed for {name}")
            continue

        # Enrich: LinkedIn profile (anchored to company)
        li_query = f'"{c["name"]}" "{name}" linkedin.com/in/'
        li_search = search_with_rotation(li_query, max_results=3)
        linkedin_url = None
        if li_search:
            urls = re.findall(r'https?://[^\s\n\)\]]+linkedin\.com/in/[^\s\n\)\]]+', li_search)
            if urls:
                linkedin_url = urls[0].rstrip('.,)')
        c['linkedin'] = linkedin_url

        # Contact: public email
        email = None
        if domain:
            email_search = search_with_rotation(f'"{c["name"]}" email OR contact "{domain}"', max_results=2)
            if email_search:
                emails = re.findall(r'[\w.+-]+@[\w-]+\.[\w.]+', email_search)
                email = emails[0] if emails else None
        c['contact'] = email

        validated.append(c)
        time.sleep(0.3)

    return validated


# ═══════════════════════════════════════════════════════════════════════════════
# 5. COMPETITOR ANALYSIS (by AI Use-Case Proximity)
# ═══════════════════════════════════════════════════════════════════════════════
def find_competitors(conn, company_data):
    """Find local and international competitors using AI use-case proximity."""
    cursor = conn.cursor()

    # Build competitive profile
    ai_use_case = company_data.get('AI_Use_Case', '') or ''
    description = company_data.get('description', '') or ''
    focus_areas = company_data.get('ai_focus_areas', '') or ''
    category = company_data.get('Category', '') or ''
    # Canonical field: Category
    biz_model = company_data.get('business_model', '') or ''

    profile_text = f"{ai_use_case} {description} {focus_areas} {category} {biz_model}".strip()
    if not profile_text or len(profile_text) < 10:
        return {'local': [], 'international': []}

    competitors = {'local': [], 'international': []}

    # Step B: Search DB for local competitors
    company_name = company_data.get('company_name', '') or company_data.get('entity_name', '')
    all_startups = cursor.execute(
        "SELECT id, company_name, AI_Use_Case, description, website FROM startups WHERE Status != 'Blacklisted'"
    ).fetchall()

    for sid, sname, s_usecase, s_desc, s_website in all_startups:
        if sname and normalize_turkish(sname) == normalize_turkish(company_name):
            continue  # Skip self
        s_profile = f"{s_usecase or ''} {s_desc or ''}"
        if len(s_profile.strip()) < 10:
            continue
        proximity = fuzz.token_sort_ratio(profile_text.lower(), s_profile.lower())
        if proximity >= 60:
            competitors['local'].append({
                'name': sname, 'ai_use_case': s_usecase or s_desc or '',
                'proximity': proximity, 'source': 'database'
            })

    # Sort by proximity descending
    competitors['local'].sort(key=lambda x: x['proximity'], reverse=True)
    competitors['local'] = competitors['local'][:10]

    # Step C: Search online for international competitors
    result = _call_llm(f"""Given this company's AI profile:
"{profile_text[:500]}"

List 5-8 international companies (NOT Turkish) that provide similar AI products or services.
Output JSON: {{"competitors": [{{"name": "...", "ai_use_case": "...", "country": "..."}}]}}
Output ONLY raw JSON.""")

    if result and result.get('competitors'):
        for c in result['competitors']:
            # Check if this is actually in our DB (would make it local)
            local_match = cursor.execute(
                "SELECT id FROM startups WHERE company_name = ?", (c.get('name', ''),)
            ).fetchone()
            target = 'local' if local_match else 'international'

            # Compute proximity
            c_profile = c.get('ai_use_case', '')
            proximity = fuzz.token_sort_ratio(profile_text.lower(), c_profile.lower()) if c_profile else 50

            competitors[target].append({
                'name': c.get('name', ''), 'ai_use_case': c_profile,
                'proximity': proximity, 'source': 'llm_analysis',
                'country': c.get('country', 'International')
            })

    competitors['international'].sort(key=lambda x: x['proximity'], reverse=True)
    competitors['international'] = competitors['international'][:10]

    return competitors


# ═══════════════════════════════════════════════════════════════════════════════
# 6. LLM SYNTHESIS (Per-Category)
# ═══════════════════════════════════════════════════════════════════════════════
def synthesize_source_summary(category_name, articles, company_name):
    """One LLM call per source category → plain text summary paragraph."""
    if not articles:
        return "No sources found in this category."
    combined = '\n'.join(f"- {a['source']}: {a['text'][:300]}" for a in articles[:10])
    result = _call_llm(f"""Summarize what these {category_name} sources reveal specifically about the company "{company_name}".
Write 2-3 sentences as a plain text paragraph. Do NOT output JSON. Do NOT use markdown.

Sources:
{combined}

Your summary:""")
    if isinstance(result, dict):
        # LLM returned JSON despite instructions — extract any text value
        return str(list(result.values())[0]) if result else "No summary available."
    return result if isinstance(result, str) else str(result)


def synthesize_profile_gaps(db_data, online_sources):
    """Fill profile table gaps using online data."""
    if not online_sources:
        return {}
    all_text = '\n'.join(
        a['text'][:200] for cat in online_sources.values() for a in cat[:3]
    )
    if not all_text:
        return {}
    result = _call_llm(f"""Based on this information, extract any factual data:
{all_text[:3000]}

Output JSON with any of: "founded_year", "city", "business_model", "sector", "total_funding", "ai_focus_areas"
Only include fields you are confident about. Output ONLY raw JSON.""")
    return result if isinstance(result, dict) else {}


# ═══════════════════════════════════════════════════════════════════════════════
# 7. REPORT GENERATION
# ═══════════════════════════════════════════════════════════════════════════════
def _freshness(change_log, field_name):
    """Get last update date for a field from change_log."""
    for fname, _, ts in (change_log or []):
        if fname == field_name:
            return ts[:10] if ts else '—'
    return '—'

def _freshness_any(change_log, field_names):
    """Get last update date for any field in `field_names` (newest wins)."""
    wanted = set(field_names or [])
    for fname, _, ts in (change_log or []):
        if fname in wanted:
            return ts[:10] if ts else '—'
    return '—'


def generate_markdown_report(name, db_data, online_sources, founders, competitors, source_summaries):
    """Build the full Markdown report."""
    lines = [f"# {name} — Research Report", f"*Generated: {get_utc_now()[:10]}*\n"]

    # === Company Profile ===
    lines.append("## Company Profile\n")
    lines.append("| Field | Value | Last Updated |")
    lines.append("|-------|-------|-------------|")

    s = db_data.get('startup') or {}
    e = db_data.get('entity') or {}
    inv = db_data.get('investor') or {}
    cl = db_data.get('change_log', [])

    profile = {
        'Name': name,
        'Type': db_data.get('entity_class', 'Unknown'),
        'Website': s.get('website') or e.get('website') or inv.get('website') or '—',
        'City': s.get('city') or e.get('city') or inv.get('location') or '—',
        'Description': s.get('description') or e.get('description') or '—',
        'AI Focus': s.get('AI_Use_Case') or e.get('ai_focus_areas') or '—',
        'Business Model': s.get('business_model') or '—',
        'Category': s.get('Category') or '—',
        'Total Funding': s.get('Total_Funding_Formatted') or '—',
        'Investors': s.get('Investors') or '—',
        'Crunchbase': s.get('crunchbase_url') or '—',
        'LinkedIn': e.get('linkedin_url') or '—',
        'AI Proximity': f"{s.get('tech_proximity') or '—'}%"
            if s.get('tech_proximity') else '—',
    }
    for field, value in profile.items():
        if field == 'Category':
            freshness = _freshness_any(cl, ['Category'])
        else:
            freshness = _freshness(cl, field)
        val_str = str(value).replace('|', '\\|')[:100]
        lines.append(f"| {field} | {val_str} | {freshness} |")

    # === Founders ===
    if founders:
        lines.append("\n## Founders\n")
        lines.append("| Name | Role | LinkedIn | Contact | Source | Confidence |")
        lines.append("|------|------|----------|---------|--------|------------|")
        for f in founders:
            li = f"[Profile]({f['linkedin']})" if f.get('linkedin') else '—'
            contact = f['contact'] or '—'
            lines.append(f"| {f['name']} | {f.get('role', '—')} | {li} | {contact} | {f.get('source', '—')} | {f.get('confidence', '—')} |")

    # === Recent Activities ===
    acts = db_data.get('activities', [])
    if acts:
        lines.append("\n## Recent Activities\n")
        lines.append("| Date | Type | Headline | Partner/Investor | Amount |")
        lines.append("|------|------|----------|-----------------|--------|")
        for a in sorted(acts, key=lambda x: x.get('activity_date') or '', reverse=True)[:15]:
            date = a.get('activity_date', '—') or '—'
            atype = a.get('activity_type', '—')
            headline = (a.get('headline') or '—')[:60]
            amount = a.get('amount') or '—'
            lines.append(f"| {date} | {atype} | {headline} | — | {amount} |")

    # === Relationships ===
    rels = db_data.get('relationships', [])
    if rels:
        lines.append("\n## Key Relationships\n")
        lines.append("| Type | Entity ID | Role |")
        lines.append("|------|-----------|------|")
        seen_rels = set()
        for r in rels:
            key = (r['entity_type'], r['entity_id'], r['role'])
            if key not in seen_rels:
                seen_rels.add(key)
                lines.append(f"| {r['entity_type']} | {r['entity_id']} | {r['role']} |")

    # === AI Initiatives Summary ===
    if e.get('ai_initiatives_summary'):
        lines.append("\n## AI Initiatives Summary\n")
        lines.append(e['ai_initiatives_summary'])

    # === Competitors ===
    if competitors:
        lines.append("\n## Competitors & Market Position\n")
        if competitors.get('local'):
            lines.append("### 🇹🇷 Local Competitors\n")
            lines.append("| Company | AI Use-Case | Proximity | Source |")
            lines.append("|---------|-------------|-----------|--------|")
            for c in competitors['local'][:8]:
                uc = (c.get('ai_use_case') or '—')[:50]
                lines.append(f"| {c['name']} | {uc} | {c['proximity']}% | {c.get('source', '—')} |")
        if competitors.get('international'):
            lines.append("\n### 🌍 International Competitors\n")
            lines.append("| Company | AI Use-Case | Proximity | Source |")
            lines.append("|---------|-------------|-----------|--------|")
            for c in competitors['international'][:8]:
                uc = (c.get('ai_use_case') or '—')[:50]
                country = c.get('country', '—')
                lines.append(f"| {c['name']} ({country}) | {uc} | {c['proximity']}% | {c.get('source', '—')} |")

    # === Sources ===
    lines.append("\n---\n\n## Sources\n")
    cat_map = {
        'news': ('📰 News Articles', source_summaries.get('news', '')),
        'blog': ('🌐 Company Website / Blog', source_summaries.get('blog', '')),
        'linkedin': ('💼 LinkedIn', source_summaries.get('linkedin', '')),
        'financial': ('📊 Financial / Investment', source_summaries.get('financial', '')),
        'technical': ('🔬 Research / Technical', source_summaries.get('technical', '')),
    }
    for cat_key, (cat_title, summary) in cat_map.items():
        articles = (online_sources or {}).get(cat_key, [])
        if articles or summary:
            lines.append(f"### {cat_title}\n")
            if summary:
                lines.append(f"> **Finding**: {summary}\n")
            for a in articles[:5]:
                lines.append(f"- [{a.get('source', 'link')}]({a.get('url', '')})")
            lines.append("")

    return '\n'.join(lines)


def generate_json_report(name, db_data, online_sources, founders, competitors, source_summaries):
    """Generate structured JSON version of the report."""
    # Keep Category canonical and avoid exposing deprecated legacy fields in JSON.
    profile_obj = db_data.get('startup') or db_data.get('entity') or db_data.get('investor')
    if isinstance(profile_obj, dict):
        profile_obj.pop('Industrial_Sector', None)
        profile_obj.pop('Tag_Category', None)

    return {
        'company_name': name,
        'generated_at': get_utc_now(),
        'entity_class': db_data.get('entity_class'),
        'profile': profile_obj,
        'founders': founders,
        'activities': db_data.get('activities', []),
        'relationships': db_data.get('relationships', []),
        'news': db_data.get('news', []),
        'competitors': competitors,
        'source_summaries': source_summaries,
        'sources': {k: [{'url': a['url'], 'source': a['source']} for a in v] for k, v in (online_sources or {}).items()},
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CACHE
# ═══════════════════════════════════════════════════════════════════════════════
def _slug(name):
    return re.sub(r'[^a-z0-9]+', '_', normalize_turkish(name)).strip('_')


def _cache_path(name):
    return REPORTS_DIR / _slug(name) / '.cache.json'


def _load_cache(name):
    cp = _cache_path(name)
    if cp.exists():
        try:
            data = json.loads(cp.read_text())
            cached_at = datetime.fromisoformat(data.get('cached_at', '2000-01-01'))
            if datetime.now(timezone.utc) - cached_at < timedelta(hours=CACHE_MAX_AGE_HOURS):
                return data
        except Exception:
            pass
    return None


def _save_cache(name, online_sources):
    cp = _cache_path(name)
    cp.parent.mkdir(parents=True, exist_ok=True)
    data = {'cached_at': datetime.now(timezone.utc).isoformat(), 'sources': online_sources}
    cp.write_text(json.dumps(data, ensure_ascii=False, default=str))


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="Company Research Report Tool")
    parser.add_argument("--name", help="Company name")
    parser.add_argument("--domain", help="Company website domain")
    parser.add_argument("--report", action="store_true", help="Quick DB-only report (no online research)")
    parser.add_argument("--deep", action="store_true", default=True, help="Full deep research (default)")
    parser.add_argument("--fresh", action="store_true", help="Skip cache, force fresh research")
    parser.add_argument("--update-db", action="store_true", help="Write discoveries to DB")
    parser.add_argument("--format", choices=['md', 'json', 'terminal'], default='md', help="Output format")
    args = parser.parse_args()

    if not args.name and not args.domain:
        parser.error("--name and/or --domain required")

    name = args.name or args.domain
    domain = args.domain

    conn = sqlite3.connect(DB_NAME)
    clear_search_cache()

    print(f"\n{'='*60}")
    print(f"  Company Report: {name}")
    print(f"{'='*60}\n")

    # 1. DB Lookup
    print("[1/6] Searching database...")
    db_data = db_lookup(conn, name, domain)
    if db_data['found']:
        print(f"  ✅ Found in DB as: {db_data['entity_class']}")
    else:
        print(f"  ❌ Not found in DB")

    # 2. Classify & Enrich new records
    if not db_data['found'] and (args.update_db or not args.report):
        print("[2/6] Classifying and enriching new company...")
        entity_class, entity_id = classify_and_enrich(conn, name, domain)
        db_data = db_lookup(conn, name, domain)  # Re-query
        print(f"  ✅ Added to DB as: {entity_class}")
    elif not db_data['found']:
        print("[2/6] Skipped (use --update-db to add new companies)")

    # 3. Online research
    online_sources = None
    if not args.report:
        cache = None if args.fresh else _load_cache(name)
        if cache:
            print("[3/6] Using cached research (< 6h old). Use --fresh to override.")
            online_sources = cache.get('sources', {})
        else:
            print("[3/6] Running online research...")
            blog_url = None
            if db_data.get('entity'):
                blog_url = db_data['entity'].get('blog_url')
            startup = db_data.get('startup') or {}
            entity = db_data.get('entity') or {}
            desc = startup.get('description') or entity.get('description') or ''
            ws = startup.get('website') or entity.get('website') or ''
            online_sources = online_research(name, domain, blog_url, description=desc, website=ws)
            total = sum(len(v) for v in online_sources.values())
            print(f"  Found {total} sources across {sum(1 for v in online_sources.values() if v)} categories")
            _save_cache(name, online_sources)
    else:
        print("[3/6] Skipped (--report mode)")

    # 4. Founder discovery (startups only)
    founders = []
    if db_data.get('entity_class') == 'startup' and not args.report:
        print("[4/6] Discovering founders...")
        crunchbase = (db_data.get('startup') or {}).get('crunchbase_url')
        desc = (db_data.get('startup') or {}).get('description', '')
        ws = (db_data.get('startup') or {}).get('website', '')
        founders = discover_founders(name, domain, crunchbase, description=desc, website=ws)
        print(f"  Found {len(founders)} founders")
    else:
        print("[4/6] Skipped (not a startup or --report mode)")

    # 5. Competitor analysis
    competitors = {'local': [], 'international': []}
    if not args.report:
        print("[5/6] Analyzing competitors...")
        company_data = db_data.get('startup') or db_data.get('entity') or {}
        if db_data.get('entity'):
            company_data.update(db_data['entity'])
        competitors = find_competitors(conn, company_data)
        print(f"  Local: {len(competitors['local'])}, International: {len(competitors['international'])}")
    else:
        print("[5/6] Skipped (--report mode)")

    # 6. Synthesis & Report
    print("[6/6] Generating report...")
    source_summaries = {}
    if online_sources:
        for cat in ['news', 'blog', 'linkedin', 'financial', 'technical']:
            if online_sources.get(cat):
                source_summaries[cat] = synthesize_source_summary(cat, online_sources[cat], name)

    # Add investment activities to financial sources
    if db_data.get('activities'):
        inv_acts = [a for a in db_data['activities'] if a.get('activity_type') == 'investment']
        if inv_acts and online_sources is not None:
            for a in inv_acts[:5]:
                online_sources.setdefault('financial', []).append({
                    'url': a.get('source_url') or 'internal DB',
                    'text': a.get('headline', ''), 'source': 'ecosystem_activities'
                })

    # Generate output
    if args.format == 'json':
        report = generate_json_report(name, db_data, online_sources, founders, competitors, source_summaries)
        output = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    elif args.format == 'terminal':
        report_md = generate_markdown_report(name, db_data, online_sources, founders, competitors, source_summaries)
        output = report_md
    else:
        output = generate_markdown_report(name, db_data, online_sources, founders, competitors, source_summaries)

    # Save to file
    slug = _slug(name)
    report_dir = REPORTS_DIR / slug
    report_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    if args.format == 'json':
        report_file = report_dir / f"{date_str}.json"
    else:
        report_file = report_dir / f"{date_str}.md"

    report_file.write_text(output, encoding='utf-8')

    # Terminal output
    if args.format == 'terminal':
        print(f"\n{output}")
    else:
        print(f"\n  Report saved: {report_file}")
        if args.format == 'md':
            print(f"\n{output[:2000]}...")

    print(f"\n{'='*60}")
    print(f"  Report complete: {report_file}")
    print(f"{'='*60}")

    conn.close()


if __name__ == "__main__":
    main()
