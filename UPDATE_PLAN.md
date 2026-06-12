# AI Ecosystem Search — Update Plan

> **Generated**: 2026-04-16
> **Purpose**: Comprehensive improvement plan for data quality, pipeline reliability, and architecture.
> **Usage**: Run each phase as a task in Claude Code. Phases are ordered by dependency — complete Phase 1 before Phase 2, etc.

---

## Executive Summary

### Current State

The system tracks ~1,700 startups, ~1,261 investors, ~1,857 investments, and ~2,122 news mentions for the Turkish AI ecosystem. A deep audit revealed systemic data quality problems caused by weak LLM prompts, missing validation layers, and incomplete pipeline logic.

### Critical Findings

| Problem Area | Evidence | Root Cause |
|---|---|---|
| **Startup mis-categorization** | 30 startups "General \| Uncategorized" despite clear AI use cases; category mismatches (e.g., messaging app tagged Healthtech) | Weak tag candidate selection; LLM auditor validates its own decisions (confirmation bias); incomplete keyword seeding |
| **Investor data corruption** | 13 investors at 0 confidence; placeholder names ("Investor B", "Unknown"); corporations (Nvidia, OpenAI) tagged as investors; politicians listed as investors; website fields overwritten with spam URLs | No validation on LLM-extracted investor names; no entity-type gate; no URL sanitization on updates |
| **News scanning failures** | 265 items (12.5%) with empty summaries; 1,064 orphaned news mentions; 56 news items referencing non-existent startups; spam/irrelevant articles in DB | Quality gate requires BOTH AI + action keywords (too strict for real news, too loose for spam); no post-extraction validation; no foreign-key integrity checks |
| **Investment data gaps** | 678 investments (36.5%) with "Unknown" currency; 192 missing amounts; naive dedup collapses distinct rounds | Weak currency detection regex; no sanity-check against startup stage; dedup key too broad |

---

## Phase 0: Data Cleanup (Run First)

> **Goal**: Fix the worst data problems immediately so later pipeline improvements don't re-contaminate clean data.

### Task 0.1 — Purge Garbage Investor Records

**File**: `agent_core.py` (new utility function) + one-time script

Delete or flag investors that are clearly not real investors:

```
Records to remove or flag as invalid:
- investor_name IN ('Investor B', 'Investor1', 'early-stage specialists', 'Major brands',
  'Unknown investor', 'Unknown', 'public institutions', 'Çinli yatırımcılar')
- investor_name = 'Jeffrey Epstein' (ID 63)
- investor_name = 'Fransa Cumhurbaşkanı Macron' (ID 64)
- Investors with confidence = 0 AND no linked investments
- investor_name length <= 2 (HV, D3, HP, UK, F6) unless they have valid linked investments
```

**Steps**:
1. Add a `status` column to the `investors` table if it doesn't exist (`active`, `flagged`, `removed`)
2. Write a SQL script that flags these records as `removed` (don't hard-delete — keep for audit)
3. Update all `investments` rows referencing removed investor IDs: set `investor_id = NULL` and add a note in a new `data_notes` field
4. Log every change to a cleanup_log table with timestamp and reason

### Task 0.2 — Fix Corporations Misclassified as Investors

**File**: new utility `util_fix_investors.py`

Corporations like Nvidia (ID 65), OpenAI (ID 86) should be reclassified:

1. Query: `SELECT * FROM investors WHERE investor_type = 'Corporate' AND investor_name IN (SELECT company_name FROM startups)`
2. For each match: if the entity exists in `startups` table, remove from `investors` and relink investments to the startup's corporate investment arm (or flag for manual review)
3. Query: `SELECT * FROM investors WHERE investor_type = 'None' OR investor_type IS NULL` — attempt reclassification using web search, or flag for review
4. Validate all `investor_type = 'Corporate'` entries: a corporate investor should be a company making strategic investments, not a tech company that happens to exist

### Task 0.3 — Fix Orphaned News Mentions

**File**: `util_repair.py` (extend existing)

1. Delete news_mentions where `startup_id` references a non-existent startup (56 records)
2. Delete news_mentions where `investor_id` references a non-existent investor (2 records)
3. For the 1,064 orphaned news mentions (no startup_id AND no investor_id): delete those with empty summaries; for the rest, attempt re-linking by running fuzzy match on headline against startup names
4. Remove clearly irrelevant news (non-Turkish, non-AI: Belgian marketplace, German beauty articles, WhatsApp tutorials) — identify by language detection on summary text

### Task 0.4 — Fix Investment Currency and Amount Gaps

**File**: `util_normalize_funding.py` (extend)

1. For 678 "Unknown" currency investments: parse the `amount` field for currency symbols ($, €, ₺) or suffixes (USD, TRY, EUR). If the startup is Turkish and no currency indicator exists, default to USD (industry standard for reporting)
2. For 192 missing amounts: if the news_mention source_url is available, attempt re-extraction from the article
3. Validate: no startup with `entity_type = 'Startup'` and `founded_year > 2020` should have `Total_Funding > $100M` without a Series B+ round in the investments table

### Task 0.5 — Reclassify "General | Uncategorized" Startups

**File**: `util_reclassify.py` (run existing, then verify)

1. Query all 30 startups with `Category = 'General | Uncategorized'`
2. For each: read description and AI_Use_Case, run the reclassification pipeline
3. Manual spot-check: verify InsurAI → Finance | Insurtech, AR Pandora → Real Estate | PropTech, etc.
4. Remove the 4 Efes product entries (Efes Pilsen, Efes Sırp, Efes Limon, Efes Kola) — these are beverage products, not startups

### Task 0.6 — Sanitize Overwritten URLs

**File**: new utility or SQL script

1. Query `startup_change_log` for `field = 'website'` where `new_value` contains known spam domains (unknowncheats.me, bydfi.com, etc.)
2. Revert these to `old_value`
3. Add a URL validation function that rejects obviously non-corporate domains before any website update

---

## Phase 1: Strengthen LLM Prompts & Validation

> **Goal**: Fix the prompts and validation logic that cause bad data to enter the system in the first place.

### Task 1.1 — Harden Pass 1 (Fact Extraction) Prompt Against Hallucination

**File**: `agent_core.py`, around the three-pass enrichment section (~lines 1250-1278)

Current prompt says "Extract ONLY the facts you can find" — this is too passive.

**Changes**:
1. Add explicit anti-hallucination instruction at the TOP of the prompt:
   ```
   CRITICAL RULES:
   1. You are a FACT EXTRACTOR, not a creative writer. Every field you fill must be traceable to a specific phrase in the provided text.
   2. If a field is not mentioned or not clearly stated in the text, you MUST output "Unknown". Never guess, infer, or fabricate.
   3. For founders: only list names explicitly identified as founders/co-founders. Do NOT list executives, employees, or board members.
   4. For funding: only state amounts explicitly mentioned with a number. "Raised funding" without a number = "Unknown".
   5. For city: only use cities explicitly mentioned as headquarters or main office location.
   ```
2. Add a `"confidence_notes"` field to the JSON schema asking the LLM to cite which text snippet supports each extracted fact
3. Post-extraction validation: if `founders` contains more than 5 names, flag for review (likely hallucinated list)

### Task 1.2 — Harden Pass 2 (Classification) With Examples and Constraints

**File**: `agent_core.py` + `region_config/search_parameter.py`

The classification prompt needs few-shot examples and negative examples.

**Changes**:
1. In `search_parameter.py`, add a `classification_examples` list to the config:
   ```python
   classification_examples = [
       {"description": "AI-powered accounting software for SMEs", "correct": "Finance | Fintech", "wrong": "Technology | Software Development", "why": "Primary value is financial, not generic software"},
       {"description": "Cloud infrastructure for ML model deployment", "correct": "Technology | AI Infrastructure", "wrong": "Technology | Cloud & SaaS", "why": "Core purpose is AI model serving"},
       {"description": "Communication platform with chat features", "correct": "Technology | Software Development", "wrong": "Health | Healthtech", "why": "Messaging is not healthcare"},
   ]
   ```
2. Include 3-5 examples in the Pass 2 prompt showing correct AND incorrect classifications with reasoning
3. Add rule: "If the company description mentions a SPECIFIC industry (healthcare, finance, agriculture, logistics), the Sector MUST reflect that industry, not 'Technology'"
4. Add rule: "Software Development is a LAST RESORT category — only use when the product has no domain-specific application"

### Task 1.3 — Fix News Extraction Prompt to Prevent International Company Leakage

**File**: `agent_news.py` (~lines 245-284)

**Changes**:
1. Add negative examples to the news extraction prompt:
   ```
   IMPORTANT: Only extract companies that are BASED IN or OPERATING FROM Turkey.
   Do NOT extract:
   - Global tech companies (Google, Microsoft, OpenAI, Meta, Amazon, etc.)
   - Companies mentioned only as partners, customers, or technology providers
   - Companies mentioned only in comparisons ("like Uber but for...")
   
   If the article says "Turkish startup X partnered with Google", extract ONLY "X", not "Google".
   ```
2. Add a `company_role` field to the extraction schema: `"company_role": "subject" | "partner" | "mentioned"` — only create/link startups for `"subject"` role companies
3. Post-extraction filter: reject any company name that appears in a hardcoded list of known global tech companies (top 200)

### Task 1.4 — Strengthen Investor Classification Prompt

**File**: `agent_investors.py` (~lines 80-130)

**Changes**:
1. Add investor_type sub-classification:
   ```
   "investor_type": one of:
     - "VC" (venture capital fund that raises LP capital)
     - "CVC" (corporate venture capital — investment arm of a corporation)  
     - "Angel" (individual investor using personal funds)
     - "Accelerator" (program that provides funding + mentorship)
     - "PE" (private equity — buys majority stakes in mature companies)
     - "Government" (state-backed fund or grant program)
     - "Family Office" (wealth management for a single family)
     - "Unknown" (cannot determine from available information)
   
   CRITICAL: A technology company (e.g., Nvidia, Google, OpenAI) is NOT an investor
   unless it has a dedicated venture/investment arm. If the entity's PRIMARY business
   is building products/services (not investing), classify as "Not an investor" and
   set all other fields to null.
   ```
2. Add a `is_investor` boolean gate: if the LLM determines the entity is not actually an investor, return `{"is_investor": false}` and skip all further enrichment
3. Add `investor_origin` field: `"domestic"` (headquartered in Turkey) vs `"international"` (foreign investor active in Turkey) — this must be based on HQ location, not portfolio

### Task 1.5 — Add Post-LLM Validation Layer

**File**: `agent_core.py` (new function `validate_llm_output()`)

Create a validation function that runs AFTER every LLM extraction, BEFORE database write:

```python
def validate_llm_output(output: dict, entity_type: str) -> tuple[dict, list[str]]:
    """
    Returns (cleaned_output, list_of_warnings).
    Rejects or fixes fields that fail validation.
    """
    warnings = []
    
    # 1. URL validation: must start with http(s)://, no spam domains
    # 2. Funding validation: amount must be numeric; currency must be valid ISO code
    # 3. Date validation: must be valid YYYY-MM-DD or YYYY
    # 4. Name validation: no names > 100 chars; no names that are full sentences
    # 5. Score validation: all 0-100 scores clipped to range
    # 6. ai_relevance: clip to 0-100
    # 7. Founded year: must be between 1900 and current_year
    # 8. City: must exist in a known Turkish cities list (or be flagged)
    # 9. Investor name: reject if matches known global tech company list
    # 10. Website: reject if domain is in a spam/blacklist
```

This function should be called in `agent_core.py` before any `INSERT` or `UPDATE` that uses LLM-extracted data.

---

## Phase 2: Fix Pipeline Logic Bugs

> **Goal**: Fix the structural code issues that cause data loss, duplication, and corruption.

### Task 2.1 — Harmonize Fuzzy Matching Thresholds

**File**: `agent_core.py` (~lines 317-376)

**Problem**: Startups use threshold 80, investors use 82, entities use 85 — inconsistent.

**Changes**:
1. Unify to a single configurable threshold per match type in `region_config`:
   ```python
   fuzzy_thresholds = {
       "startup_exact": 88,      # High confidence exact match
       "startup_partial": 92,    # Partial ratio (stricter than current 90)
       "investor_exact": 88,
       "investor_partial": 92,
       "entity_exact": 88,
   }
   ```
2. Add minimum token count: partial ratio matching should require the shorter name to have at least 2 tokens (words), preventing "Peak" from matching "Peak Games"
3. Return match confidence score alongside the match result so callers can make informed decisions
4. Add logging: every fuzzy match decision should log `(query, matched_name, score, method)` for later audit

### Task 2.2 — Fix News Quality Gate (Too Strict for Real News)

**File**: `agent_news.py` (~lines 50-68)

**Problem**: Requires BOTH `ai_hit` AND `action_hit` — drops legitimate AI news that uses uncommon phrasing.

**Changes**:
1. Change from AND to weighted scoring:
   ```python
   def _passes_news_quality_gate(title, snippet):
       score = 0
       if ai_hit: score += 2
       if action_hit: score += 1
       if startup_name_hit: score += 2  # NEW: if a known startup name appears
       if negative_hit: score -= 3
       return score >= 2
   ```
2. Add `startup_name_hit`: check if any known startup name from the DB appears in the title/snippet — if yes, always pass the gate regardless of keyword matches
3. Expand Turkish AI keywords: add "yapay zeka başkanı", "makine öğrenmesi", "derin öğrenme", "büyük dil modeli", "otonom", "robotik süreç" to the keyword list
4. Log rejected articles to a `rejected_news` table for periodic review (catch false negatives)

### Task 2.3 — Fix Investment Deduplication

**File**: `agent_news.py` (~lines 483-509)

**Problem**: Dedup key is `(startup_id, investor_id, round_type)` — collapses distinct rounds.

**Changes**:
1. Add `investment_date` (year) to the dedup key:
   ```python
   existing = cursor.execute(
       """SELECT id, amount_usd FROM investments 
          WHERE startup_id = ? AND investor_id = ? AND round_type = ?
          AND (strftime('%Y', investment_date) = strftime('%Y', ?) OR investment_date IS NULL)
          LIMIT 1""",
       (sid, iid, round_type, date)
   ).fetchone()
   ```
2. If `round_type = "NA"` or `"Unknown"`, never auto-dedup — always insert as new record with a `needs_review` flag
3. When updating an existing investment, only overwrite if the new data has HIGHER confidence (e.g., has amount when existing doesn't)

### Task 2.4 — Fix Currency Detection in Updater

**File**: `agent_updater.py` (~lines 114-117)

**Problem**: Regex extracts "M" from "5M" as currency.

**Changes**:
1. Replace the naive split with a proper currency detection function:
   ```python
   VALID_CURRENCIES = {"USD", "EUR", "TRY", "GBP", "CHF", "JPY", "CNY", "SAR", "AED"}
   
   def detect_currency(raw_funding: str) -> str:
       # Check for currency symbols first
       if "$" in raw_funding: return "USD"
       if "€" in raw_funding: return "EUR"  
       if "₺" in raw_funding: return "TRY"
       if "£" in raw_funding: return "GBP"
       # Check for ISO codes
       for code in VALID_CURRENCIES:
           if code in raw_funding.upper():
               return code
       return "Unknown"
   ```
2. Import this function in `agent_updater.py` and `agent_news.py` to replace all ad-hoc currency parsing

### Task 2.5 — Fix Confidence Score Calculation

**File**: `agent_core.py` (~lines 908-927)

**Problem**: Arbitrary thresholds (100 chars = 40 points vs 99 chars = 15 points); no quality check.

**Changes**:
1. Replace step-function with gradual scoring:
   ```python
   def calculate_confidence(website_text, search_context, has_funding, verification_pass, last_enriched):
       score = 0
       # Website quality (0-35)
       if website_text:
           wt_len = len(website_text)
           score += min(35, int(wt_len / 10))  # Gradual: 1 point per 10 chars, max 35
       # Search context quality (0-30)
       if search_context:
           sc_len = len(search_context)
           score += min(30, int(sc_len / 10))
       # Funding evidence (0-20)
       if has_funding: score += 20
       # Verification recency (0-15)
       if verification_pass:
           days_since = (now - last_enriched).days
           recency_score = max(0, 15 - (days_since // 7))  # Lose 1 point per week
           score += recency_score
       return min(100, score)
   ```
2. Add a "parking page detector": if website_text is mostly boilerplate (check for "coming soon", "under construction", "domain for sale"), set website_text score to 0

### Task 2.6 — Complete Tiered Refresh Logic in Updater

**File**: `agent_updater.py`

**Problem**: Tier refresh constants are defined but the actual refresh logic is incomplete.

**Changes**:
1. Implement `process_b_refresh_stale()`:
   ```python
   def process_b_refresh_stale(conn):
       now = datetime.now()
       
       # Tier 1 (7 days): vitality check only
       tier1 = query_stale_startups(conn, days=7, min_confidence=60)
       for s in tier1:
           alive = check_website_alive(s['website'])
           if not alive:
               update_field(conn, s['id'], 'status', 'Possibly Inactive')
       
       # Tier 2 (14 days): delta enrichment for missing fields
       tier2 = query_stale_startups(conn, days=14, has_missing_fields=True)
       for s in tier2:
           fill_missing_fields(conn, s)  # Only fills NULL/Unknown fields
       
       # Tier 3 (30 days): full re-enrichment
       tier3 = query_stale_startups(conn, days=30, max_confidence=50)
       for s in tier3:
           full_reenrich(conn, s)  # Runs all 3 passes
   ```
2. Add `query_stale_startups()` helper that filters by `last_enriched` date and optionally by confidence score or missing fields
3. Wire this into `main.py --update` so it runs after the news delta scan

---

## Phase 3: Architecture Improvements

> **Goal**: Add structural capabilities that prevent future data quality issues and enable smarter analysis.

### Task 3.1 — Add a Cowork Integration Layer

**File**: new `cowork_bridge.py` + updates to `main.py`

The system currently uses small local LLMs (Qwen 7B, Mistral-Nemo) for all enrichment. These models are adequate for structured extraction but struggle with nuanced classification, cross-referencing, and analysis. A Cowork/Claude layer can serve as a "senior analyst" for tasks that require higher intelligence.

**Architecture**:
```
Local LLM (fast, cheap)          Cowork/Claude (smart, expensive)
├── Pass 1: Fact extraction      ├── Ambiguous classification disputes
├── Pass 2: Basic classification ├── Investor legitimacy verification
├── Pass 3: Formatting           ├── Weekly ecosystem report generation
└── News keyword extraction      ├── Cross-entity relationship analysis
                                 ├── Anomaly detection in data quality
                                 └── Complex research reports
```

**Implementation**:
1. Create `cowork_bridge.py` with functions:
   - `escalate_classification(startup_data, local_llm_result, candidates)` — when local LLM confidence is low or auditor disagrees, escalate to Claude for final classification
   - `verify_investor(investor_data)` — for new investors, ask Claude to verify legitimacy using web search
   - `generate_weekly_report(news_mentions, investments, new_startups)` — use Claude's analytical capability for narrative report generation
   - `detect_anomalies(recent_changes)` — periodic data quality check using Claude to spot patterns a local LLM would miss
2. Add `--cowork` flag to `main.py` that enables the Cowork layer
3. Define escalation thresholds in `region_config`:
   ```python
   cowork_escalation = {
       "classification_confidence_below": 60,  # Escalate if local LLM confidence < 60
       "investor_confidence_below": 50,
       "news_ai_relevance_ambiguous": (30, 70),  # Escalate if score is in the gray zone
   }
   ```
4. Implement rate limiting: batch escalations and send to Cowork in groups to minimize API calls

### Task 3.2 — Add Foreign Key Integrity Enforcement

**File**: `agent_core.py` (DB initialization section)

**Problem**: 56 news mentions reference non-existent startups; 2 investments reference non-existent startups.

**Changes**:
1. Enable SQLite foreign key enforcement:
   ```python
   conn.execute("PRAGMA foreign_keys = ON")
   ```
2. Add foreign key constraints to the schema (requires migration):
   ```sql
   -- investments
   ALTER TABLE investments ADD CONSTRAINT fk_inv_startup 
       FOREIGN KEY (startup_id) REFERENCES startups(id) ON DELETE SET NULL;
   ALTER TABLE investments ADD CONSTRAINT fk_inv_investor
       FOREIGN KEY (investor_id) REFERENCES investors(investor_id) ON DELETE SET NULL;
   -- news_mentions
   ALTER TABLE news_mentions ADD CONSTRAINT fk_news_startup
       FOREIGN KEY (startup_id) REFERENCES startups(id) ON DELETE SET NULL;
   ```
   Note: SQLite doesn't support ALTER TABLE ADD CONSTRAINT — implement via table recreation in a migration script.
3. Create `migrate_schema.py` that:
   - Backs up Master.db
   - Creates new tables with constraints
   - Copies data, skipping rows that violate FK constraints (log them)
   - Renames tables

### Task 3.3 — Add a Rejected/Quarantine System

**File**: new tables + updates to `agent_news.py`, `agent_investors.py`

Instead of silently dropping bad data, quarantine it for review:

1. Create `quarantine` table:
   ```sql
   CREATE TABLE quarantine (
       id INTEGER PRIMARY KEY,
       entity_type TEXT,       -- 'startup', 'investor', 'news', 'investment'
       raw_data TEXT,          -- JSON blob of the rejected data
       rejection_reason TEXT,  -- Why it was rejected
       source_module TEXT,     -- Which module created it
       created_at TEXT,
       reviewed BOOLEAN DEFAULT 0,
       reviewer_action TEXT    -- 'approved', 'deleted', 'modified'
   );
   ```
2. Update all pipeline modules: when data fails validation, INSERT into quarantine instead of silently skipping
3. Add `main.py --review-quarantine` command that lists quarantined items for manual review
4. The Cowork integration (Task 3.1) can periodically review quarantine items and auto-resolve obvious cases

### Task 3.4 — Improve the Reclassification Pipeline

**File**: `util_reclassify.py`

**Problem**: Same LLM validates its own decisions (confirmation bias); logistics gate permanently locks categories; no diversity in tag candidates.

**Changes**:
1. **Remove the logistics binary gate** (Step 1): Replace with a multi-label preliminary classification:
   ```python
   def step_1_preliminary_classify(description):
       """Returns top-3 broad sectors with confidence scores, not a binary gate."""
       prompt = f"""Given this company description, rank the TOP 3 most likely broad sectors.
       Output JSON: {{"sectors": [{{"name": "...", "confidence": 0-100}}, ...]}}
       Sectors: Technology, Finance, Health, Manufacturing, Services, Agriculture, Energy, Real Estate, Media, Education, General
       """
   ```
2. **Add candidate diversity**: In Step 3, ensure the 5 tag candidates come from at least 2 different sectors (not all from the same one)
3. **Independent auditor**: In Step 5, use a DIFFERENT prompt structure and explicitly tell the LLM "You are an INDEPENDENT REVIEWER. A classification system chose [sector|tag]. Given ONLY the description below, would YOU classify it the same way? If not, what would you choose?"
4. **Add cross-reference validation**: After classification, check if the chosen category is consistent with:
   - The startup's `Tech_Mentioned` field
   - The startup's `AI_Use_Case` field
   - Similar startups in the same city/founded_year range

### Task 3.5 — Add a Weekly Report Generator

**File**: new `report_weekly.py` + integration with Cowork

**Problem**: The current system can't correctly identify details for weekly reports from news data.

**Implementation**:
1. Create `report_weekly.py` that:
   - Queries news_mentions from the last 7 days
   - Groups by category: funding rounds, partnerships, product launches, ecosystem events
   - For each category, generates a narrative summary (use Cowork/Claude for quality)
   - Highlights: top 3 funding rounds, notable new startups, investor activity
   - Flags data quality issues found during the week
2. Output formats: Markdown (for email/Notion), HTML (for dashboard), JSON (for API)
3. Add `main.py --weekly-report` command
4. Structure:
   ```
   # Turkey AI Ecosystem — Weekly Report (2026-04-07 to 2026-04-14)
   
   ## Key Highlights
   - [Top 3 most significant events]
   
   ## Funding Activity
   - [Each funding round with company, amount, investor, round type]
   
   ## New Startups Discovered
   - [Name, sector, brief description]
   
   ## Partnerships & Collaborations
   - [Notable partnerships]
   
   ## Ecosystem Activity
   - [Government programs, accelerator batches, teknopark news]
   
   ## Data Quality Notes
   - [Items flagged for review]
   ```

### Task 3.6 — Add URL Sanitization and Spam Protection

**File**: `agent_core.py` (new utility functions)

**Problem**: Website fields have been overwritten with spam URLs (unknowncheats.me, bydfi.com).

**Implementation**:
1. Create a URL validation function:
   ```python
   SPAM_DOMAINS = {"unknowncheats.me", "bydfi.com", "bit.ly", ...}  # Maintain blocklist
   
   def validate_url(url: str, entity_name: str) -> tuple[bool, str]:
       """Returns (is_valid, reason)."""
       # Check: starts with http/https
       # Check: domain not in spam list
       # Check: domain resolves (optional, with cache)
       # Check: domain is plausibly related to entity name
       # Check: not a URL shortener
   ```
2. Apply this validation before ANY website field update in the database
3. Add to `startup_change_log`: include a `validated` boolean field
4. Periodic sweep: run URL validation on all existing website fields, flag suspicious ones

---

## Phase 4: Keyword & Taxonomy Improvements

> **Goal**: Improve the classification foundation that all modules depend on.

### Task 4.1 — Audit and Expand Tag Seed Keywords

**File**: `agent_core.py` (~line 818+) + `region_config/search_parameter.py`

**Problem**: Seed keywords are one-time only; new tags get zero keywords; some stop words remove industry signals.

**Changes**:
1. Change keyword seeding from one-time to periodic refresh:
   ```python
   def seed_tag_keywords(conn, force_refresh=False):
       for tag in ALL_TAGS:
           count = get_keyword_count(conn, tag)
           if count == 0 or force_refresh:
               learn_keywords_from_descriptions(conn, tag)
   ```
2. Fix stop words list: remove industry-significant terms from stop words:
   ```python
   # REMOVE from stop words: "agricultural", "financial", "medical", "logistics"
   # KEEP in stop words: "technology", "solutions", "startup", "company", "platform"
   ```
3. Add manual keyword overrides in config for critical tags:
   ```python
   tag_keyword_overrides = {
       "Finance | Fintech": ["banking", "payment", "credit", "lending", "insurance", "accounting"],
       "Health | Healthtech": ["clinical", "patient", "diagnosis", "medical", "hospital", "pharma"],
       "Agriculture | Agritech": ["farming", "crop", "soil", "irrigation", "livestock", "agriculture"],
   }
   ```
4. Add `main.py --refresh-keywords` command

### Task 4.2 — Add "Not an AI Company" Detection

**File**: `agent_core.py` or new `util_ai_relevance.py`

**Problem**: Some non-AI companies (Efes beer products, traditional logistics) exist in the database.

**Implementation**:
1. After enrichment, calculate `tech_proximity` more rigorously:
   - Check description for AI-specific terms (not just "technology")
   - Check `AI_Use_Case` — if it's generic ("Uses technology for operations"), score low
   - Check `Tech_Mentioned` — if no AI/ML frameworks or techniques, score low
2. If `tech_proximity < 15` after enrichment, move to quarantine (Task 3.3) instead of storing in startups table
3. Run a one-time sweep on all existing startups with `tech_proximity < 20`, flagging them for review

---

## Phase 5: Testing & Monitoring

> **Goal**: Prevent regressions and catch data quality issues early.

### Task 5.1 — Add Data Quality Assertions

**File**: new `tests/test_data_quality.py`

Create automated checks that can run after each pipeline execution:

```python
def test_no_orphaned_news():
    """All news_mentions.startup_id values must exist in startups table."""
    
def test_no_zero_confidence_without_reason():
    """Startups with confidence=0 must have a data_notes explaining why."""
    
def test_investor_types_valid():
    """All investor_type values must be in the allowed set."""
    
def test_no_spam_urls():
    """No website field should contain a known spam domain."""
    
def test_category_coverage():
    """No more than 2% of startups should be 'General | Uncategorized'."""
    
def test_investment_currency_coverage():
    """No more than 10% of investments should have 'Unknown' currency."""
    
def test_news_summary_coverage():
    """No more than 5% of news_mentions should have empty summaries."""
```

Run these after every `main.py --all` execution. Fail loudly if thresholds are exceeded.

### Task 5.2 — Add Pipeline Execution Logging

**File**: `agent_core.py` + all agent modules

**Changes**:
1. Enhance `scan_log` table with more detail:
   ```sql
   ALTER TABLE scan_log ADD COLUMN records_created INTEGER;
   ALTER TABLE scan_log ADD COLUMN records_updated INTEGER;
   ALTER TABLE scan_log ADD COLUMN records_quarantined INTEGER;
   ALTER TABLE scan_log ADD COLUMN records_rejected INTEGER;
   ALTER TABLE scan_log ADD COLUMN warnings TEXT;  -- JSON array of warning messages
   ```
2. Each module should populate these fields at the end of its run
3. Add `main.py --health` command that shows:
   - Last run time for each module
   - Records created/updated/rejected in last run
   - Current data quality metrics (% uncategorized, % unknown currency, etc.)
   - Quarantine queue size

---

## Implementation Order

```
Phase 0 (Data Cleanup)          ← Run FIRST, one-time
  ├── Task 0.1-0.6              ← ~2-3 hours of work
  │
Phase 1 (LLM Prompts)           ← Foundation fixes
  ├── Task 1.1-1.5              ← ~3-4 hours
  │
Phase 2 (Pipeline Logic)        ← Bug fixes
  ├── Task 2.1-2.6              ← ~4-5 hours
  │
Phase 3 (Architecture)          ← New capabilities  
  ├── Task 3.1-3.6              ← ~6-8 hours
  │
Phase 4 (Taxonomy)              ← Classification quality
  ├── Task 4.1-4.2              ← ~2-3 hours
  │
Phase 5 (Testing)               ← Regression prevention
  ├── Task 5.1-5.2              ← ~2-3 hours
```

**Total estimated effort**: 19-26 hours of implementation

**Quick wins** (highest impact, lowest effort):
1. Task 0.1 — Purge garbage investor records (30 min)
2. Task 1.3 — Fix news extraction to reject global companies (1 hour)
3. Task 2.2 — Fix news quality gate scoring (1 hour)
4. Task 1.5 — Add post-LLM validation layer (2 hours)
5. Task 0.5 — Reclassify uncategorized startups (30 min)

---

## Files Changed Per Phase

| Phase | Files Modified | Files Created |
|---|---|---|
| 0 | `util_repair.py`, `util_normalize_funding.py` | `util_fix_investors.py`, cleanup SQL script |
| 1 | `agent_core.py`, `agent_news.py`, `agent_investors.py`, `region_config/search_parameter.py` | — |
| 2 | `agent_core.py`, `agent_news.py`, `agent_updater.py` | — |
| 3 | `main.py`, `agent_core.py` | `cowork_bridge.py`, `migrate_schema.py`, `report_weekly.py` |
| 4 | `agent_core.py`, `region_config/search_parameter.py` | `util_ai_relevance.py` |
| 5 | all agent modules | `tests/test_data_quality.py` |
