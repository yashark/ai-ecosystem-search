"""
Shared enrichment functions used by all agent modules.
Provides: search rotation, website scraping, domain vitality, LLM workflow, and sector/tag validation.
"""
import json
import time
import os
import re
import hashlib
import sqlite3
import urllib.request
import urllib.error
import requests
import threading
from datetime import datetime, timezone
from pathlib import Path
from rapidfuzz import fuzz
from bs4 import BeautifulSoup
from ddgs import DDGS
from region_config import get_active_config
from data_guard import normalize_city, is_valid_company_name

DB_NAME = "Master.db"

# Load active region configuration (singleton)
_CFG = get_active_config()

def get_db_connection(db_name=None):
    """Return a SQLite connection with WAL mode, FK enforcement, and optimized pragmas."""
    conn = sqlite3.connect(db_name or DB_NAME, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

OLLAMA_API_URL = "http://localhost:11434/api/generate"

# LLM_BACKEND: "ollama" (default on 16 GB M4 — MLX 7B 4-bit hits Metal OOM here)
# or "mlx" (native Apple Silicon, requires ≥24 GB unified memory for the 7B tier).
LLM_BACKEND = os.environ.get("LLM_BACKEND", "ollama")

# Ollama model names — mapped to what's installed locally (gemma4 8B, phi3.5 3.8B).
# phi3.5 handles the many lightweight extraction/classification passes cheaply;
# gemma4 handles reasoning and doubles as the long-context fallback.
MODELS = {
    "fast": "phi3.5:latest",
    "reasoning": "gemma4:latest",
    "context": "gemma4:latest",
}

# MLX model paths (Hugging Face hub IDs) — kept for machines with enough RAM.
MLX_MODELS = {
    "fast": "mlx-community/Qwen2.5-7B-Instruct-4bit",
    "reasoning": "mlx-community/Qwen2.5-7B-Instruct-4bit",
    "context": "mlx-community/Mistral-Nemo-Instruct-2407-4bit",
}

# MLX model cache (loaded once, kept in memory)
_mlx_loaded_models = {}

# MLX hang guards — can be tuned via env. Defaults picked so a stuck call
# gives up before wedging the pipeline (see 2026-04-19 incident).
_MLX_LOAD_TIMEOUT_S = int(os.environ.get("MLX_LOAD_TIMEOUT_S", "180"))
_MLX_GENERATE_TIMEOUT_S = int(os.environ.get("MLX_GENERATE_TIMEOUT_S", "90"))

def _run_with_timeout(fn, timeout_s, label):
    """Run a blocking callable in a daemon thread; return (result, error) or (None, 'timeout')."""
    box = {}
    def _target():
        try:
            box["result"] = fn()
        except BaseException as e:
            box["error"] = e
    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        print(f"  [MLX] {label} timed out after {timeout_s}s — skipping.")
        return None, "timeout"
    if "error" in box:
        return None, box["error"]
    return box.get("result"), None

def _get_mlx_model(model_id):
    """Lazy-load and cache an MLX model + tokenizer."""
    if model_id not in _mlx_loaded_models:
        from mlx_lm import load
        print(f"  [MLX] Loading model: {model_id} ...")
        result, err = _run_with_timeout(lambda: load(model_id), _MLX_LOAD_TIMEOUT_S, f"load({model_id})")
        if err is not None or result is None:
            if err not in (None, "timeout"):
                print(f"  [MLX] Failed to load {model_id}: {err}")
            return None, None
        _mlx_loaded_models[model_id] = result
        print(f"  [MLX] Model loaded: {model_id}")
    return _mlx_loaded_models[model_id]

def _call_mlx(prompt, model_type="reasoning", max_tokens=500, temperature=0.1):
    """MLX-native inference for Apple Silicon. Returns parsed JSON dict or None."""
    model_id = MLX_MODELS.get(model_type, MLX_MODELS["reasoning"])

    # Context-Window Overflow Prevention
    total_tokens = estimate_tokens(prompt)
    if total_tokens > 6000 and model_id != MLX_MODELS["context"]:
        print(f"  [MLX Routing] Prompt size ({total_tokens} tokens) exceeds standard window. Rerouting to context model.")
        model_id = MLX_MODELS["context"]

    model, tokenizer = _get_mlx_model(model_id)
    if model is None:
        return None

    text = ""
    try:
        from mlx_lm import generate
        from mlx_lm.sample_utils import make_sampler

        # Format as chat for instruct models
        messages = [
            {"role": "system", "content": "You are a data extraction assistant. Always respond with valid JSON only. No markdown, no explanation."},
            {"role": "user", "content": prompt}
        ]
        formatted = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)

        sampler = make_sampler(temp=temperature)
        text, err = _run_with_timeout(
            lambda: generate(model, tokenizer, prompt=formatted, max_tokens=max_tokens, sampler=sampler),
            _MLX_GENERATE_TIMEOUT_S,
            "generate()"
        )
        if err is not None or text is None:
            if err not in (None, "timeout"):
                print(f"  [MLX] Generate error: {err}")
            return None
        text = text.strip()

        # Strip markdown fences
        if text.startswith('```'):
            lines = text.split('\n')
            lines = [l for l in lines if not l.strip().startswith('```')]
            text = '\n'.join(lines).strip()

        return json.loads(text)
    except json.JSONDecodeError:
        # Balanced-brace extraction fallback
        start = text.find('{')
        if start != -1:
            depth, end = 0, start
            for i, c in enumerate(text[start:], start):
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                if depth == 0:
                    end = i
                    break
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        print(f"  [MLX] Non-JSON response, could not parse.")
        return None
    except Exception as e:
        print(f"  [MLX] Error: {e}")
        return None

VALID_MATRIX = _CFG.valid_matrix

SECTION_7_MATRIX_STR = _CFG.matrix_description_str

# --- Search Engine Rotation ---

# Search engine rotation: DDG → SearXNG (local Docker). Brave disabled — needs BRAVE_API_KEY.
# To re-enable Brave: add "brave" to the list and set env var BRAVE_API_KEY.
# SearXNG runs at localhost:8888 via: docker run -d --name searxng -p 8888:8080 searxng/searxng
SEARCH_ENGINES = ["searxng"]  # Only backend used (no DDG fallbacks)

# --- Search Result Cache ---
_search_cache = {}

# Persistent cache to avoid repeating expensive search/scrape work across runs.
# Stored inside the workspace so it survives restarts on your M4.
_DISK_CACHE_DIR = Path(".cache/agent_core")
_SEARCH_DISK_TTL_HOURS = 6
_SCRAPE_DISK_TTL_HOURS = 24

# Cache versioning for search_news_with_rotation result post-processing.
_NEWS_DATE_EXTRACT_VERSION = 2

def _disk_cache_key(prefix, payload):
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(f"{prefix}::{raw}".encode("utf-8")).hexdigest()

def _disk_cache_get(key, ttl_hours):
    cp = _DISK_CACHE_DIR / f"{key}.json"
    if not cp.exists():
        return None
    try:
        data = json.loads(cp.read_text(encoding="utf-8"))
        cached_at = data.get("cached_at")
        if not cached_at:
            return None
        dt = datetime.fromisoformat(cached_at)
        age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
        if age_hours > ttl_hours:
            return None
        return data.get("value")
    except Exception:
        return None

def _disk_cache_set(key, value):
    try:
        _DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cp = _DISK_CACHE_DIR / f"{key}.json"
        payload = {
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "value": value,
        }
        cp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:
        # Cache must never break the pipeline.
        pass

def clear_search_cache():
    """Call at the start of each agent run to reset the cache."""
    global _search_cache
    _search_cache = {}

# --- Shared External News Sources (from region config) ---
EXTERNAL_NEWS_SOURCES = _CFG.external_news_sources

def normalize_turkish(text):
    """Safely lowercase text using region-specific character mapping.
    Name kept for backward compatibility — delegates to config."""
    return _CFG.normalize_text(text)

def is_relevant_result(name, text):
    """Robust substring check that handles Turkish chars and abbreviations."""
    if not name or not text:
        return False
    
    n_name = normalize_turkish(name)
    n_text = normalize_turkish(text)
    
    # 1. Exact substring check after normalization
    if n_name in n_text:
        return True
    
    # 2. Check first significant word (useful for "Acme A.Ş." matched against "Acme")
    import re
    words = re.findall(r'\w+', n_name)
    if not words:
        return False
        
    first_word = words[0]
    # If the first word is at least 3 letters, consider it a partial signal
    if len(first_word) >= 3 and first_word in n_text:
        return True
        
    # 3. Match without non-alphanumeric chars
    clean_name = re.sub(r'[^a-z0-9]', '', n_name)
    clean_text = re.sub(r'[^a-z0-9]', '', n_text)
    if clean_name and len(clean_name) >= 3 and clean_name in clean_text:
        return True

    # 4. Fallback constraint: ASCII-fied match (e.g. "cagdas" matches "çağdaş")
    if _CFG.asciify(n_name) in _CFG.asciify(n_text):
        return True

    return False


def parse_search_results(raw_text):
    """Parse search rotation text into structured list of {url, snippet, title}."""
    if not raw_text:
        return []
    results = []
    for block in raw_text.split('\n\n'):
        url, snippet, title = '', '', ''
        for line in block.strip().split('\n'):
            if line.startswith('Url: '):
                url = line[5:]
            elif line.startswith('Snippet: '):
                snippet = line[9:]
            elif line.startswith('Title: '):
                title = line[7:]
        if url:
            results.append({'url': url, 'snippet': snippet, 'title': title})
    return results


# --- Unified Fuzzy Matcher ---

# Corporate suffixes to strip before fuzzy matching (from region config)
_CORPORATE_SUFFIXES = _CFG.corporate_suffixes

def _strip_corporate_suffixes(text):
    """Strip common corporate suffixes for better fuzzy matching."""
    lower = text.lower()
    for suffix in _CORPORATE_SUFFIXES:
        if lower.endswith(suffix):
            text = text[:len(text) - len(suffix)].strip()
            lower = text.lower()
    return text

# Backward-compatible alias
_strip_turkish_suffixes = _strip_corporate_suffixes

class FuzzyMatcher:
    """Shared cached fuzzy matching for startups, investors, and entities.

    Replaces separate implementations in agent_news, agent_ecosystem, and util_report.
    Build once per run, invalidate after DB writes.
    """
    def __init__(self):
        self._startups = None
        self._investors = None
        self._entities = None

    def invalidate(self):
        """Reset all indexes. Call after creating new records."""
        self._startups = None
        self._investors = None
        self._entities = None

    def _ensure_startups(self, conn):
        if self._startups is None:
            cursor = conn.cursor()
            rows = cursor.execute("SELECT id, company_name, Status FROM startups").fetchall()
            self._startups = [
                (sid, sname, status, normalize_turkish(sname),
                 normalize_turkish(_strip_turkish_suffixes(sname)))
                for sid, sname, status in rows
            ]

    def _ensure_investors(self, conn):
        if self._investors is None:
            cursor = conn.cursor()
            rows = cursor.execute("SELECT investor_id, investor_name FROM investors").fetchall()
            self._investors = [
                (iid, iname, normalize_turkish(iname),
                 normalize_turkish(_strip_turkish_suffixes(iname)))
                for iid, iname in rows
            ]

    def _ensure_entities(self, conn):
        if self._entities is None:
            cursor = conn.cursor()
            rows = cursor.execute("SELECT id, entity_name FROM ecosystem_entities").fetchall()
            self._entities = [(eid, ename, normalize_turkish(ename)) for eid, ename in rows]

    # Unified thresholds (configurable)
    EXACT_THRESHOLD = 88
    PARTIAL_THRESHOLD = 92
    MIN_PARTIAL_TOKENS = 2  # Partial match requires at least 2 words

    def _log_match(self, query, matched, score, method, entity_type):
        """Log fuzzy match decisions for audit."""
        # Only print if match found (reduce noise)
        if matched:
            print(f"    [FuzzyMatch] {entity_type}: '{query}' → '{matched}' (score={score}, method={method})")

    def find_startup(self, conn, name):
        """Returns (id, name, status) or None."""
        if not name:
            return None
        self._ensure_startups(conn)
        nl = normalize_turkish(name)
        nl_stripped = normalize_turkish(_strip_turkish_suffixes(name))
        # Exact match first (with and without suffixes)
        for sid, sname, status, sl, sl_stripped in self._startups:
            if sl == nl or sl_stripped == nl_stripped:
                self._log_match(name, sname, 100, "exact", "startup")
                return (sid, sname, status)
        # Fuzzy match on stripped names
        best, best_score = None, 0
        for sid, sname, status, sl, sl_stripped in self._startups:
            score = fuzz.token_sort_ratio(nl_stripped, sl_stripped)
            if score > best_score and score >= self.EXACT_THRESHOLD:
                best_score = score
                best = (sid, sname, status)
        if best:
            self._log_match(name, best[1], best_score, "token_sort", "startup")
            return best
        # Partial ratio fallback: require min 2 tokens to avoid "Peak" matching "Peak Games"
        query_tokens = len(nl_stripped.split())
        if query_tokens >= self.MIN_PARTIAL_TOKENS:
            for sid, sname, status, sl, sl_stripped in self._startups:
                score = fuzz.partial_ratio(nl_stripped, sl_stripped)
                if score >= self.PARTIAL_THRESHOLD and len(nl_stripped) >= 3:
                    self._log_match(name, sname, score, "partial", "startup")
                    return (sid, sname, status)
        return None

    def find_investor(self, conn, name):
        """Returns (id, name) or None."""
        if not name:
            return None
        self._ensure_investors(conn)
        nl = normalize_turkish(name)
        nl_stripped = normalize_turkish(_strip_turkish_suffixes(name))
        # Exact match first
        for iid, iname, il, il_stripped in self._investors:
            if il == nl or il_stripped == nl_stripped:
                self._log_match(name, iname, 100, "exact", "investor")
                return (iid, iname)
        # Fuzzy match
        best, best_score = None, 0
        for iid, iname, il, il_stripped in self._investors:
            score = fuzz.token_sort_ratio(nl_stripped, il_stripped)
            if score > best_score and score >= self.EXACT_THRESHOLD:
                best_score = score
                best = (iid, iname)
        if best:
            self._log_match(name, best[1], best_score, "token_sort", "investor")
            return best
        # Partial ratio fallback with min token requirement
        query_tokens = len(nl_stripped.split())
        if query_tokens >= self.MIN_PARTIAL_TOKENS:
            for iid, iname, il, il_stripped in self._investors:
                score = fuzz.partial_ratio(nl_stripped, il_stripped)
                if score >= self.PARTIAL_THRESHOLD and len(nl_stripped) >= 3:
                    self._log_match(name, iname, score, "partial", "investor")
                    return (iid, iname)
        return None

    def find_entity(self, conn, name):
        """Returns entity id or None."""
        if not name:
            return None
        self._ensure_entities(conn)
        nl = normalize_turkish(name)
        best, best_score = None, 0
        for eid, ename, el in self._entities:
            score = fuzz.token_sort_ratio(nl, el)
            if score > best_score and score >= self.EXACT_THRESHOLD:
                best_score = score
                best = eid
        if best:
            self._log_match(name, str(best), best_score, "token_sort", "entity")
        return best

# Singleton instance
fuzzy_matcher = FuzzyMatcher()

def _normalize_searxng_language(language):
    """Normalize language code for SearXNG API."""
    if language is None:
        return None
    lang = str(language).strip().lower()
    if lang in {"tr", "tur", "turkish"}:
        return "tr"
    if lang in {"en", "eng", "english"}:
        return "en"
    # SearXNG supports many codes; pass through if it looks like a short code.
    if 1 <= len(lang) <= 5 and lang.isalpha():
        return lang
    return None


def search_with_rotation(query, max_results=3, language=None):
    """Search via SearXNG only (cached).

    Kept the name for backward compatibility with existing modules.
    """
    language = _normalize_searxng_language(language)
    mem_key = (query, max_results, language)
    if mem_key in _search_cache:
        return _search_cache[mem_key]

    disk_key = _disk_cache_key(
        "search_with_rotation",
        {"query": query, "max_results": max_results, "language": language},
    )
    cached = _disk_cache_get(disk_key, _SEARCH_DISK_TTL_HOURS)
    if cached is not None:
        _search_cache[mem_key] = cached
        return cached

    # SearXNG is local; if it fails, return empty string.
    try:
        result = _search_searxng(query, max_results, language=language)
        if result:
            _search_cache[mem_key] = result
            _disk_cache_set(disk_key, result)
            return result
    except Exception as e:
        print(f"  [searxng] search_with_rotation failed: {e}")
    return ""

def search_news_with_rotation(query, max_results=10, language=None, time_range=None):
    """News search via SearXNG only (cached).

    time_range: one of: day | month | year (when SearXNG supports it).
    """
    language = _normalize_searxng_language(language)
    if time_range is not None:
        tr = str(time_range).strip().lower()
        if tr not in {"day", "month", "year"}:
            time_range = None
    disk_key = _disk_cache_key(
        "search_news_with_rotation",
        {
            "query": query,
            "max_results": max_results,
            "language": language,
            "time_range": time_range,
            "date_extract_v": _NEWS_DATE_EXTRACT_VERSION,
        },
    )
    cached = _disk_cache_get(disk_key, _SEARCH_DISK_TTL_HOURS)
    if cached is not None:
        return cached
    try:
        resp = requests.get(
            "http://localhost:8888/search",
            params={
                "q": query,
                "format": "json",
                "categories": "news",
                "pageno": 1,
                **({"language": language} if language else {}),
                **({"time_range": time_range} if time_range else {}),
            },
            timeout=10,
        )
        resp.raise_for_status()
        mapped = []
        import re as _re
        for r in resp.json().get("results", [])[:max_results]:
            title = r.get("title", "News Item") or "News Item"
            body = r.get("content", "") or r.get("body", "") or ""
            url = r.get("url", "") or ""

            date_val = (r.get("publishedDate") or r.get("pubdate") or "").strip()
            if not date_val:
                m = _re.search(r'((?:19|20)\d{2})', url)
                if m:
                    date_val = m.group(1) if m.lastindex else m.group(0)
                else:
                    m2 = _re.search(r'((?:19|20)\d{2})', title + " " + body)
                    if m2:
                        date_val = m2.group(1) if m2.lastindex else m2.group(0)

            mapped.append({
                "url": url,
                "body": body,
                "title": title,
                # SearXNG instances often omit structured dates; extract a year from URL first,
                # then from title/body as a fallback (helps Module 4 filtering reliability).
                "date": date_val or "",
            })
        if mapped:
            _disk_cache_set(disk_key, mapped)
            return mapped
    except Exception as e:
        print(f"  [searxng news] failed: {e}")
    return []

def _search_ddg(query, max_results):
    results_text = ""
    results = DDGS().text(query, max_results=max_results)
    if not results:
        raise Exception("DDG returned no results")
    for r in results:
        results_text += f"Url: {r.get('href', '')}\nSnippet: {r.get('body', '')}\n\n"
    time.sleep(1)
    return results_text

def _search_brave(query, max_results):
    """Brave Search API (free tier: 2,000/month). Requires BRAVE_API_KEY env var."""
    import os
    api_key = os.environ.get("BRAVE_API_KEY", "")
    if not api_key:
        raise Exception("No BRAVE_API_KEY set")
    resp = requests.get("https://api.search.brave.com/res/v1/web/search",
        headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
        params={"q": query, "count": max_results}, timeout=10)
    resp.raise_for_status()
    results_text = ""
    for r in resp.json().get("web", {}).get("results", []):
        results_text += f"Url: {r.get('url', '')}\nSnippet: {r.get('description', '')}\n\n"
    time.sleep(1)
    return results_text

def _search_searxng(query, max_results, language=None):
    """SearXNG local instance (Docker). Requires running on localhost:8888."""
    language = _normalize_searxng_language(language)
    resp = requests.get("http://localhost:8888/search",
        params={
            "q": query,
            "format": "json",
            "pageno": 1,
            **({"language": language} if language else {}),
        },
        timeout=10,
    )
    resp.raise_for_status()
    results_text = ""
    for r in resp.json().get("results", [])[:max_results]:
        results_text += f"Url: {r.get('url', '')}\nSnippet: {r.get('content', '')}\n\n"
    return results_text

# --- Domain & Scraping ---

def check_domain_vitality(url):
    """Returns True if the domain is stale/dead (negative signal)."""
    if not url or str(url) == 'nan':
        return True
    if not url.startswith('http'):
        url = 'https://' + url
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        target_url = f"{parsed.scheme}://{parsed.netloc}"
        req = urllib.request.Request(target_url, headers={'User-Agent': 'Mozilla/5.0'})
        response = urllib.request.urlopen(req, timeout=5)
        html = response.read().decode('utf-8', errors='ignore').lower()
        if any(kw in html for kw in ['domain is for sale', 'buy this domain', 'parked by']):
            return True
        if 'copyright' in html or '©' in html:
            if '2023' not in html and '2024' not in html and '2025' not in html and '2026' not in html:
                return True
        return False
    except Exception:
        return True

def scrape_website_text(url):
    """Scrape all visible text from the homepage (up to 3000 chars)."""
    if not url or str(url) == 'nan':
        return ""
    if not url.startswith('http'):
        url = 'https://' + url

    disk_key = _disk_cache_key("scrape_website_text", {"url": url})
    cached = _disk_cache_get(disk_key, _SCRAPE_DISK_TTL_HOURS)
    if cached is not None:
        return cached

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        response = urllib.request.urlopen(req, timeout=8)
        html = response.read().decode('utf-8', errors='ignore')
        soup = BeautifulSoup(html, 'html.parser')
        for script in soup(["script", "style", "noscript"]):
            script.extract()
        text = soup.get_text(separator=' ')
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = ' '.join(chunk for chunk in chunks if chunk)
        text = text[:3000]
        if text:
            _disk_cache_set(disk_key, text)
        return text
    except Exception:
        return ""

# --- Async I/O Helpers ---

import asyncio

async def _async_searxng(session, query, max_results, language=None, categories=None, time_range=None):
    """Async SearXNG search via aiohttp."""
    params = {"q": query, "format": "json", "pageno": 1}
    if language:
        params["language"] = language
    if categories:
        params["categories"] = categories
    if time_range:
        params["time_range"] = time_range
    try:
        async with session.get("http://localhost:8888/search", params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("results", [])[:max_results]
    except Exception as e:
        print(f"  [async searxng] failed: {e}")
        return []

async def _async_scrape(session, url):
    """Async website text scraping via aiohttp."""
    if not url or str(url) == 'nan':
        return ""
    if not url.startswith('http'):
        url = 'https://' + url

    disk_key = _disk_cache_key("scrape_website_text", {"url": url})
    cached = _disk_cache_get(disk_key, _SCRAPE_DISK_TTL_HOURS)
    if cached is not None:
        return cached

    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            html = await resp.text(errors='ignore')
            soup = BeautifulSoup(html, 'html.parser')
            for script in soup(["script", "style", "noscript"]):
                script.extract()
            text = soup.get_text(separator=' ')
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            text = ' '.join(chunk for chunk in chunks if chunk)[:3000]
            if text:
                _disk_cache_set(disk_key, text)
            return text
    except Exception:
        return ""

async def async_search_news_batch(queries, max_results=10, time_range=None):
    """Run multiple news searches concurrently. Returns dict mapping query_text -> results list."""
    import aiohttp as _aiohttp
    results = {}
    async with _aiohttp.ClientSession() as session:
        tasks = {}
        for q in queries:
            query_text = q["query"]
            lang = _normalize_searxng_language(q.get("lang"))

            # Check disk cache first
            disk_key = _disk_cache_key("search_news_with_rotation", {
                "query": query_text, "max_results": max_results,
                "language": lang, "time_range": time_range,
                "date_extract_v": _NEWS_DATE_EXTRACT_VERSION,
            })
            cached = _disk_cache_get(disk_key, _SEARCH_DISK_TTL_HOURS)
            if cached is not None:
                results[query_text] = cached
            else:
                tasks[query_text] = asyncio.create_task(
                    _async_searxng(session, query_text, max_results, language=lang,
                                   categories="news", time_range=time_range)
                )

        # Await all non-cached searches concurrently
        for query_text, task in tasks.items():
            raw = await task
            import re as _re
            mapped = []
            for r in raw:
                title = r.get("title", "News Item") or "News Item"
                body = r.get("content", "") or r.get("body", "") or ""
                url = r.get("url", "") or ""
                date_val = (r.get("publishedDate") or r.get("pubdate") or "").strip()
                if not date_val:
                    m = _re.search(r'((?:19|20)\d{2})', url)
                    if m:
                        date_val = m.group(1) if m.lastindex else m.group(0)
                    else:
                        m2 = _re.search(r'((?:19|20)\d{2})', title + " " + body)
                        if m2:
                            date_val = m2.group(1) if m2.lastindex else m2.group(0)
                mapped.append({"url": url, "body": body, "title": title, "date": date_val or ""})

            if mapped:
                lang = _normalize_searxng_language(
                    next((q["lang"] for q in queries if q["query"] == query_text), None)
                )
                disk_key = _disk_cache_key("search_news_with_rotation", {
                    "query": query_text, "max_results": max_results,
                    "language": lang, "time_range": time_range,
                    "date_extract_v": _NEWS_DATE_EXTRACT_VERSION,
                })
                _disk_cache_set(disk_key, mapped)
            results[query_text] = mapped

    return results

async def async_search_batch(queries_with_params):
    """Run multiple general searches concurrently.
    queries_with_params: list of dicts with keys: query, max_results, language
    Returns dict mapping query -> results string.
    """
    import aiohttp as _aiohttp
    results = {}
    async with _aiohttp.ClientSession() as session:
        tasks = {}
        for qp in queries_with_params:
            query = qp["query"]
            max_r = qp.get("max_results", 3)
            lang = _normalize_searxng_language(qp.get("language"))

            mem_key = (query, max_r, lang)
            if mem_key in _search_cache:
                results[query] = _search_cache[mem_key]
                continue

            disk_key = _disk_cache_key("search_with_rotation", {"query": query, "max_results": max_r, "language": lang})
            cached = _disk_cache_get(disk_key, _SEARCH_DISK_TTL_HOURS)
            if cached is not None:
                _search_cache[mem_key] = cached
                results[query] = cached
                continue

            tasks[query] = asyncio.create_task(
                _async_searxng(session, query, max_r, language=lang)
            )

        for query, task in tasks.items():
            raw = await task
            result_text = ""
            for r in raw:
                result_text += f"Url: {r.get('url', '')}\nSnippet: {r.get('content', '')}\n\n"
            if result_text:
                qp = next((q for q in queries_with_params if q["query"] == query), {})
                max_r = qp.get("max_results", 3)
                lang = _normalize_searxng_language(qp.get("language"))
                mem_key = (query, max_r, lang)
                _search_cache[mem_key] = result_text
                disk_key = _disk_cache_key("search_with_rotation", {"query": query, "max_results": max_r, "language": lang})
                _disk_cache_set(disk_key, result_text)
            results[query] = result_text

    return results

async def async_scrape_batch(urls):
    """Scrape multiple URLs concurrently. Returns dict mapping url -> text."""
    import aiohttp as _aiohttp
    results = {}
    async with _aiohttp.ClientSession() as session:
        tasks = {url: asyncio.create_task(_async_scrape(session, url)) for url in urls if url}
        for url, task in tasks.items():
            results[url] = await task
    return results

def run_async(coro):
    """Helper to run async functions from sync code."""
    try:
        loop = asyncio.get_running_loop()
        # If we're already in an event loop, use nest_asyncio or run in thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    except RuntimeError:
        return asyncio.run(coro)

# Need aiohttp import at module level for type hints
try:
    import aiohttp
except ImportError:
    aiohttp = None

# --- Pre-LLM Entity Heuristic ---

MEDIA_KEYWORDS = _CFG.media_keywords
TEKNOPARK_KEYWORDS = _CFG.techpark_keywords
ACCELERATOR_KEYWORDS = _CFG.accelerator_keywords
GOVERNMENT_KEYWORDS = _CFG.government_keywords

def detect_entity_type_heuristic(name, website_text):
    """Fast Python check before expensive LLM call. Returns entity_type or None if uncertain."""
    combined = (name + " " + (website_text or "")).lower()
    if any(kw in combined for kw in MEDIA_KEYWORDS):
        return 'Media'
    if any(kw in combined for kw in TEKNOPARK_KEYWORDS):
        return 'Teknopark'
    if any(kw in combined for kw in ACCELERATOR_KEYWORDS):
        return 'Accelerator'
    if any(kw in combined for kw in GOVERNMENT_KEYWORDS):
        return 'Government'
    return None  # Uncertain — defer to LLM

# --- Sector/Tag Validation ---

def validate_sector_tag(sector, tag):
    """Auto-correct invalid Sector/Tag pairs against the matrix."""
    # Redirect eliminated categories
    if tag == "SaaS & Platforms":
        tag = "Software Development"
        sector = "Technology"
    pair = (sector, tag)
    if pair in VALID_MATRIX:
        return sector, tag
    for valid_sector, valid_tag in VALID_MATRIX:
        if valid_tag == tag:
            return valid_sector, tag
    for valid_sector, valid_tag in VALID_MATRIX:
        if valid_sector == sector:
            return sector, valid_tag
    return "General", "Uncategorized"

# --- Self-Improving Tag Keywords ---

_TAG_SEED = _CFG.tag_seed_keywords

# Stop words for keyword learning (from region config)
_LEARN_STOP_WORDS = _CFG.stop_words

def seed_tag_keywords(conn, force_refresh=False):
    """Bootstrap or refresh seed keywords. Runs once unless force_refresh=True."""
    cursor = conn.cursor()
    count = cursor.execute("SELECT COUNT(*) FROM tag_keywords").fetchone()[0]
    if count > 0 and not force_refresh:
        return  # Already seeded

    now = get_utc_now()
    seeded = 0
    for tag, keywords in _TAG_SEED.items():
        existing_count = cursor.execute(
            "SELECT COUNT(*) FROM tag_keywords WHERE tag_category = ? AND source = 'seed'", (tag,)
        ).fetchone()[0]
        if existing_count == 0 or force_refresh:
            for kw in keywords:
                cursor.execute(
                    "INSERT OR IGNORE INTO tag_keywords (tag_category, keyword, weight, source, created_at) VALUES (?, ?, 3.0, 'seed', ?)",
                    (tag, kw.lower(), now)
                )
                seeded += 1

    # Apply keyword overrides from config if available
    overrides = getattr(_CFG, 'tag_keyword_overrides', {})
    for tag, keywords in overrides.items():
        for kw in keywords:
            cursor.execute(
                "INSERT OR IGNORE INTO tag_keywords (tag_category, keyword, weight, source, created_at) VALUES (?, ?, 4.0, 'override', ?)",
                (tag, kw.lower(), now)
            )
            seeded += 1

    conn.commit()
    if seeded > 0:
        print(f"  [Keywords] Seeded/refreshed {seeded} keywords across {len(_TAG_SEED)} tags.")

def learn_keywords_from_description(conn, tag_category, description):
    """Learn new keywords from a validated classification. Called after a tag passes validation."""
    if not description or not tag_category or tag_category == 'Uncategorized':
        return
    import re as _re
    words = set(
        w.lower() for w in _re.findall(r'\w+', description.lower())
        if len(w) > 3 and w.lower() not in _LEARN_STOP_WORDS
    )
    # Also extract bigrams (two-word phrases) from the description
    desc_words = _re.findall(r'\w+', description.lower())
    bigrams = set()
    for i in range(len(desc_words) - 1):
        w1, w2 = desc_words[i], desc_words[i+1]
        if len(w1) > 2 and len(w2) > 2 and w1 not in _LEARN_STOP_WORDS and w2 not in _LEARN_STOP_WORDS:
            bigrams.add(f"{w1} {w2}")

    cursor = conn.cursor()
    now = get_utc_now()
    for kw in words | bigrams:
        cursor.execute("""
            INSERT INTO tag_keywords (tag_category, keyword, weight, source, created_at)
            VALUES (?, ?, 0.5, 'learned', ?)
            ON CONFLICT(tag_category, keyword) DO UPDATE SET weight = MIN(weight + 0.3, 10.0)
        """, (tag_category, kw, now))

def validate_classification_accuracy(conn, tag_category, description):
    """Score how well a description matches a tag using the keyword index.
    Returns a float 0.0-1.0. Scores < 0.15 indicate likely misclassification.
    """
    if not description or not tag_category:
        return 0.0
    cursor = conn.cursor()
    keywords = cursor.execute(
        "SELECT keyword, weight FROM tag_keywords WHERE tag_category = ? ORDER BY weight DESC LIMIT 60",
        (tag_category,)
    ).fetchall()
    if not keywords:
        return 0.5  # No keyword data yet — neutral, don't flag

    desc_lower = description.lower()
    hit_weight = sum(w for kw, w in keywords if kw in desc_lower)
    # Normalize against top-10 weight sum (realistic maximum for a match)
    top_weight = sum(w for _, w in keywords[:10])
    return min(hit_weight / max(top_weight, 1.0), 1.0)

def get_top_candidate_tags(conn, description, n=5):
    """Return the top-N tag categories ranked by keyword match score.
    Used to pre-filter candidates for the 8B LLM prompt.
    """
    if not description:
        return list(_TAG_SEED.keys())[:n]
    cursor = conn.cursor()
    all_tags = cursor.execute(
        "SELECT DISTINCT tag_category FROM tag_keywords"
    ).fetchall()
    scores = []
    for (tag,) in all_tags:
        score = validate_classification_accuracy(conn, tag, description)
        scores.append((tag, score))
    scores.sort(key=lambda x: x[1], reverse=True)
    top = [tag for tag, _ in scores[:n]]
    # Always include Software Development as a fallback for horizontal tools
    if 'Software Development' not in top:
        top[-1] = 'Software Development'
    return top

# --- Confidence Scoring ---

def calculate_confidence(website_text, search_context, has_funding,
                         verification_passes=0, data_age_days=0):
    """Multi-factor confidence score with gradual scoring and age decay."""
    score = 0

    # Website quality (0-35): gradual instead of step function
    if website_text:
        wt_len = len(website_text)
        # Detect parking/placeholder pages
        wt_lower = website_text.lower()
        is_parking = any(ind in wt_lower for ind in _PARKING_PAGE_INDICATORS)
        if is_parking:
            score += 0  # Parking page gets no credit
        else:
            score += min(35, wt_len // 10)  # 1 point per 10 chars, max 35

    # Search context quality (0-30): gradual
    if search_context:
        sc_len = len(search_context)
        score += min(30, sc_len // 10)

    # Funding evidence (0-20)
    if has_funding:
        score += 20

    # Verification recency (0-15)
    if verification_passes > 0:
        score += min(verification_passes * 5, 15)

    # Decay for old data (after 90 days, lose 1 point per week, max -20)
    if data_age_days > 90:
        weeks_stale = (data_age_days - 90) // 7
        score -= min(weeks_stale, 20)

    return max(0, min(score, 100))

# --- Post-LLM Validation ---

_SPAM_DOMAINS = {
    "unknowncheats.me", "bydfi.com", "tinyurl.com", "goo.gl", "cutt.ly", "shorturl.at",
}

_PARKING_PAGE_INDICATORS = [
    "coming soon", "under construction", "domain for sale", "parked domain",
    "this domain is for sale", "buy this domain", "website coming soon",
    "site yapım aşamasında", "yakında", "satılık domain",
]

def validate_extracted_data(output: dict, entity_type: str = "startup") -> tuple:
    """
    Validate LLM-extracted data BEFORE database write.
    Returns (cleaned_output, list_of_warnings).
    Fixes fields that can be fixed; flags fields that need manual review.
    """
    warnings = []
    if not output or not isinstance(output, dict):
        return output, ["Output is empty or not a dict"]

    # 1. URL validation
    for url_field in ("website", "crunchbase_url"):
        url = output.get(url_field)
        if url and url != "Unknown":
            if not url.startswith(("http://", "https://")):
                if "." in url:  # Looks like a domain, add protocol
                    output[url_field] = "https://" + url
                else:
                    output[url_field] = "Unknown"
                    warnings.append(f"{url_field} invalid: '{url}'")
            # Check for spam domains
            domain = url.lower().split("//")[-1].split("/")[0].split(":")[0]
            for spam in _SPAM_DOMAINS:
                if domain == spam or domain.endswith("." + spam):
                    output[url_field] = "Unknown"
                    warnings.append(f"{url_field} is spam domain: {domain}")
                    break

    # 2. Founded year validation
    year = output.get("founded_year")
    if year is not None:
        try:
            year = int(year)
            if year < 1900 or year > 2026:
                output["founded_year"] = None
                warnings.append(f"founded_year out of range: {year}")
        except (ValueError, TypeError):
            output["founded_year"] = None

    # 3. Name validation
    for name_field in ("founders", "company_name", "investor_name"):
        name = output.get(name_field)
        if name and isinstance(name, str) and len(name) > 200:
            output[name_field] = name[:200]
            warnings.append(f"{name_field} truncated from {len(name)} chars")

    # 4. Founders count validation (hallucination detector)
    founders = output.get("founders", "")
    if isinstance(founders, str) and founders != "Unknown":
        founder_count = len([f for f in founders.split(",") if f.strip()])
        if founder_count > 5:
            warnings.append(f"founders has {founder_count} names — likely hallucinated, flagged for review")

    # 5. Score validation: clip 0-100 scores
    for score_field in ("ai_relevance", "tech_proximity", "data_confidence"):
        val = output.get(score_field)
        if val is not None:
            try:
                val = int(val)
                output[score_field] = max(0, min(100, val))
            except (ValueError, TypeError):
                pass

    # 6. City validation (Turkish cities) — normalize first, then validate
    city = output.get("city")
    if city and city != "Unknown" and city != "Foreign" and entity_type == "startup":
        city = normalize_city(city)
        output["city"] = city
        known_cities = {c.lower() for c in _CFG.domestic_markers if len(c) > 3}
        if city.lower() not in known_cities and len(city) > 2:
            warnings.append(f"city '{city}' not in known {_CFG.region_name} cities — verify")

    # 7. Investor name: reject known global tech companies
    inv_name = output.get("investor_name", "")
    if inv_name and inv_name.lower() in _CFG.international_blocklist:
        warnings.append(f"investor_name '{inv_name}' is a known global tech company")

    return output, warnings

# --- LLM Workflow ---

def execute_8b_workflow(startup_data, search_context, website_context):
    """Run the 4-step enrichment via Ollama. Returns parsed JSON or None."""
    # Defers to the robust modular chain to prevent monolithic prompt context overflow
    return three_pass_enrichment(startup_data, search_context, website_context)

def execute_delta_workflow(startup_data, search_context, website_context, missing_fields):
    """Run a minimalist LLM prompt that ONLY asks for specific missing fields."""
    if not missing_fields:
        return {}
        
    # Build schema description dynamically based on what's missing
    schema_lines = []
    if 'website' in missing_fields: schema_lines.append('"website" (string)')
    if 'description' in missing_fields: schema_lines.append('"description" (string, 1-2 sentence English)')
    if 'city' in missing_fields: schema_lines.append('"city" (string)')
    if 'founders' in missing_fields: schema_lines.append('"founders" (string, comma-separated, proper native Turkish characters)')
    if 'sector' in missing_fields: schema_lines.append('"sector" (string, strictly from matrix)')
    if 'tag' in missing_fields: schema_lines.append('"tag" (string, strictly from matrix)')
    if 'AI_Use_Case' in missing_fields: schema_lines.append('"AI_Use_Case" (string, strictly custom 5-word summary)')
    if 'Total_Funding' in missing_fields: schema_lines.append('"Total_Funding" (string, Decimal Comma format)')
    if 'Investors' in missing_fields: schema_lines.append('"Investors" (string, comma-separated)')
    if 'Tech_Mentioned' in missing_fields: schema_lines.append('"Tech_Mentioned" (string, frameworks, languages, AI methods)')
    if 'Tech_Assumed' in missing_fields: schema_lines.append('"Tech_Assumed" (string, logical inference based on product type)')
    
    schema_str = ",\n".join(schema_lines)

    prompt = f"""You are a Fact-Checker. Fill ONLY the missing fields for this {_CFG.region_name} startup.

### Missing Fields Requested:
{", ".join(missing_fields)}

### Startup Known Data:
{json.dumps(startup_data, indent=2, ensure_ascii=False)}

### Web Search Context:
{search_context}

### Homepage Text:
{website_context[:3000] if website_context else ""}

### Output Schema:
Produce ONLY a valid JSON object with exactly these keys. Format answers according to the descriptions:
{{
{schema_str}
}}
If you cannot find the answer in the context, output "Unknown" (for strings).
Output ONLY the raw JSON. No markdown, no text.
"""
    return _call_llm(prompt, model_type="fast") or {}

# --- Currency & Amount Normalization ---

def detect_currency(amount_str):
    """Detect currency from amount string. Returns 'Unknown' if ambiguous."""
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


def normalize_funding_amount(raw_str):
    """Normalize funding amounts to consistent format: '5,000,000 USD'.
    Handles Turkish format (83.000.000,00 USD), US format ($5M), narrative text.
    Returns (numeric_value, formatted_str, currency) or (None, None, None) if unparseable.
    """
    if not raw_str or str(raw_str).strip().lower() in ('unknown', 'n/a', 'undisclosed', ''):
        return None, None, None

    raw_str = str(raw_str).strip()
    currency = detect_currency(raw_str)

    # Strip narrative text (e.g., "Over $1.76B in funding across 411 rounds")
    if re.match(r'^(over|approximately|about|undisclosed|toplam|yaklaşık)\s', raw_str, re.IGNORECASE):
        # Try to extract a number from narrative
        num_match = re.search(r'[\$€₺]?\s*(\d[\d.,]*)\s*(billion|million|B|M|K|milyar|milyon)?', raw_str, re.IGNORECASE)
        if not num_match:
            return None, None, None
        raw_str = num_match.group(0)

    # Strip currency symbols and labels
    cleaned = re.sub(r'[\$€£₺]', '', raw_str)
    cleaned = re.sub(r'\b(USD|TRY|TL|EUR|GBP)\b', '', cleaned, flags=re.IGNORECASE).strip()
    cleaned = cleaned.rstrip(',').strip()

    if not cleaned:
        return None, None, None

    # Handle Turkish format: 83.000.000,00 → 83000000.00
    if re.match(r'^\d{1,3}(\.\d{3})+(,\d{1,2})?$', cleaned):
        cleaned = cleaned.replace('.', '').replace(',', '.')

    # Handle shorthand: 5M, 1.5B, 250K, 5 milyon, 1.5 milyar
    multipliers = {
        'K': 1_000, 'M': 1_000_000, 'B': 1_000_000_000,
        'milyon': 1_000_000, 'million': 1_000_000,
        'milyar': 1_000_000_000, 'billion': 1_000_000_000,
    }
    for suffix, mult in multipliers.items():
        pattern = re.compile(rf'^([\d.,]+)\s*{re.escape(suffix)}$', re.IGNORECASE)
        m = pattern.match(cleaned)
        if m:
            try:
                num = float(m.group(1).replace(',', ''))
                value = num * mult
                fmt_currency = currency if currency != "Unknown" else "USD"
                return value, f"{int(value):,} {fmt_currency}", currency
            except ValueError:
                pass

    # Plain number (possibly with commas as thousands separators)
    cleaned = cleaned.replace(',', '')
    try:
        value = float(cleaned)
        if value > 0:
            # Sanity cap: startups raising above the region threshold is almost certainly
            # an LLM hallucination. Cap and return None for suspicious amounts.
            if currency in ("USD", "EUR", "GBP", "Unknown") and value > _CFG.funding_sanity_cap:
                print(f"  [WARN] Suspicious funding amount: {int(value):,} {currency} — NULLed (likely fabricated)")
                return None, None, None
            fmt_currency = currency if currency != "Unknown" else "USD"
            return value, f"{int(value):,} {fmt_currency}", currency
    except ValueError:
        pass

    return None, None, None


# --- Round Type Normalization ---

ROUND_TYPE_CANONICAL = {
    "seri a": "Series A", "seri b": "Series B", "seri c": "Series C",
    "seed": "Seed", "pre-seed": "Pre-Seed", "pre-series a": "Pre-Series A",
    "series a": "Series A", "series b": "Series B", "series c": "Series C",
    "series d": "Series D", "series e": "Series E",
    "angel": "Angel",
    "investment": "NA",
    "seed and series a": "Seed",
    "unknown": "NA",
    "": "NA",
}


def normalize_round_type(raw):
    """Normalize round type to canonical form. Returns 'NA' if not mentioned."""
    if not raw or raw.strip() == "":
        return "NA"
    return ROUND_TYPE_CANONICAL.get(raw.strip().lower(), raw.strip())


# --- Three-Pass Modular Chain ---

def estimate_tokens(text):
    """Estimate token count for context window management."""
    if not text:
        return 0
    return int(len(str(text).split()) * 1.3)

def _call_llm(prompt, model_type="reasoning", max_retries=2):
    """Shared LLM call helper with dynamic routing and retry logic."""
    # Route to MLX backend if configured
    if LLM_BACKEND == "mlx":
        return _call_mlx(prompt, model_type=model_type)

    target_model = MODELS.get(model_type, MODELS["reasoning"])
    
    # Context-Window Overflow Prevention
    total_tokens = estimate_tokens(prompt)
    is_context_model = False
    if total_tokens > 6000 and target_model != MODELS["context"]:
        print(f"  [Routing] Prompt size ({total_tokens} tokens) exceeds standard window. Rerouting to {MODELS['context']}.")
        target_model = MODELS["context"]
        is_context_model = True
    elif target_model == MODELS["context"]:
        is_context_model = True

    # Use larger context window for overflow/context model
    num_ctx = 8192 if is_context_model else 4096

    for attempt in range(max_retries + 1):
        try:
            response = requests.post(OLLAMA_API_URL, json={
                "model": target_model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.1, "num_predict": 500, "num_ctx": num_ctx}
            }, timeout=120)
            response.raise_for_status()
            text = response.json().get("response", "").strip()
            if not text:
                return None
            # --- Format enforcement ---
            # Strip markdown fences if LLM wrapped JSON in ```json ... ```
            if text.startswith('```'):
                lines = text.split('\n')
                # Remove first line (```json) and last line (```)
                lines = [l for l in lines if not l.strip().startswith('```')]
                text = '\n'.join(lines).strip()
            return json.loads(text)
        except json.JSONDecodeError:
            # LLM returned non-JSON text — extract JSON with balanced-brace matching
            start = text.find('{')
            if start != -1:
                depth, end = 0, start
                for i, c in enumerate(text[start:], start):
                    if c == '{':
                        depth += 1
                    elif c == '}':
                        depth -= 1
                    if depth == 0:
                        end = i
                        break
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    pass
            print(f"  LLM returned non-JSON response (attempt {attempt+1}), retrying...")
            if attempt >= max_retries:
                return None
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt < max_retries:
                print(f"  LLM transient error (attempt {attempt+1}/{max_retries+1}): {e}. Retrying in 3s...")
                time.sleep(3)
            else:
                print(f"  LLM Error after {max_retries+1} attempts: {e}")
                return None
        except Exception as e:
            print(f"  LLM Error: {e}")
            return None

# --- JSON Schema Definitions for LLM Output Validation ---
try:
    from jsonschema import validate as _jsonschema_validate, ValidationError as _ValidationError
    _HAS_JSONSCHEMA = True
except ImportError:
    _HAS_JSONSCHEMA = False

_SCHEMA_PASS_1 = {
    "type": "object",
    "required": ["founders", "city", "description"],
    "properties": {
        "founders": {"type": "string"},
        "founded_year": {"type": ["integer", "null", "string"]},
        "city": {"type": "string"},
        "description": {"type": "string"},
        "website": {"type": "string"},
        "funding_info": {"type": "string"},
        "tech_keywords": {"type": "string"},
        "business_indicators": {"type": "string"},
        "regional_presence": {"type": "string"},
    }
}

_SCHEMA_PASS_2 = {
    "type": "object",
    "required": ["sector", "tag", "AI_Use_Case", "business_model", "entity_type"],
    "properties": {
        "sector": {"type": "string"},
        "tag": {"type": "string"},
        "AI_Use_Case": {"type": "string"},
        "business_model": {"type": "string", "enum": ["Product", "Consultancy", "Hybrid", "Unknown"]},
        "entity_type": {"type": "string"},
        "reasoning": {"type": "string"},
    }
}

_SCHEMA_PASS_3 = {
    "type": "object",
    "required": ["sector", "tag", "description"],
    "properties": {
        "website": {"type": "string"},
        "description": {"type": "string"},
        "city": {"type": "string"},
        "founders": {"type": "string"},
        "sector": {"type": "string"},
        "tag": {"type": "string"},
        "AI_Use_Case": {"type": "string"},
        "Total_Funding": {"type": "string"},
        "Investors": {"type": "string"},
        "Tech_Mentioned": {"type": "string"},
        "Tech_Assumed": {"type": "string"},
        "business_model": {"type": "string"},
        "entity_type": {"type": "string"},
    }
}

_SCHEMA_NEWS = {
    "type": "object",
    "required": ["headline", "event_type"],
    "properties": {
        "headline": {"type": ["string", "array"]},
        "companies": {"type": "array", "items": {"type": "string"}},
        "investors": {"type": "array", "items": {"type": "string"}},
        "funding_amount": {"type": "string"},
        "round_type": {"type": "string"},
        "funding_date": {"type": "string"},
        "event_type": {"type": "string"},
        "sentiment": {"type": "string"},
        "summary": {"type": ["string", "array"]},
    }
}

def validate_llm_output(data, schema, label=""):
    """Validate LLM output against a JSON schema. Returns list of issues (empty = valid)."""
    if not _HAS_JSONSCHEMA or data is None:
        return []
    issues = []
    try:
        _jsonschema_validate(instance=data, schema=schema)
    except _ValidationError as e:
        issues.append(f"[{label}] Schema violation: {e.message}")
    return issues

def pass_1_researcher(startup_data, search_context, website_context):
    """Pass 1: Extract raw facts without categorization pressure."""
    prompt = f"""CRITICAL RULES:
1. You are a FACT EXTRACTOR, not a creative writer. Every field you fill must be traceable to a specific phrase in the provided text.
2. If a field is not mentioned or not clearly stated in the text, you MUST output "Unknown". Never guess, infer, or fabricate.
3. For founders: only list names explicitly identified as founders/co-founders. Do NOT list executives, employees, or board members.
4. For funding: only state amounts explicitly mentioned with a number. "Raised funding" without a number = "Unknown".
5. For city: only use cities explicitly mentioned as headquarters or main office location.

You are a Fact-Extraction Agent. Extract raw facts about this {_CFG.region_name} startup.

Startup Name: {startup_data.get('name', 'Unknown')}
Known Data: {json.dumps(startup_data, indent=2, ensure_ascii=False)}

Web Search Results:
{search_context}

Scraped Homepage Text:
{website_context[:3000] if website_context else 'No homepage scraped.'}

{_CFG.pass_1_context_rules}

Extract ONLY the facts you can find. Output a JSON with these keys:
"founders" (string, comma-separated names, MUST use proper native characters)
"founded_year" (integer or null)
"city" (string, city name or "Foreign" if no {_CFG.region_name} presence)
"description" (string, 1-2 sentence English description of what they do)
"website" (string, URL)
"funding_info" (string, any mention of funding amounts and investors)
"tech_keywords" (string, any programming languages, frameworks, tools mentioned)
"business_indicators" (string, evidence of pricing/SaaS/API vs consulting/services)
"regional_presence" (string, evidence of {_CFG.region_name} office: local phone, address, local domain)
"confidence_notes" (string, cite which text snippet supports each key fact you extracted)

If you cannot find evidence, output "Unknown". Output ONLY the raw JSON.
"""
    return _call_llm(prompt, model_type="fast")

def pass_2_architect(facts, startup_data):
    """Pass 2: Map extracted facts to taxonomy and classify business model."""
    prompt = f"""You are a Taxonomy Mapping Agent. Given facts about a startup, classify it.

Startup Name: {startup_data.get('name', 'Unknown')}
Extracted Facts:
{json.dumps(facts, indent=2, ensure_ascii=False)}

Task 1: Select the BEST Sector/Tag pair from this matrix. Pick the row that best fits:
{SECTION_7_MATRIX_STR}

CRITICAL CLASSIFICATION RULE: Classify by the INDUSTRY the company serves, NOT its delivery model.
SaaS, API, marketplace, and platform are business models — they go in business_model, NOT in the category.
A company doing AI-powered medical imaging is "Health | Healthtech" regardless of whether it is SaaS or on-premise.
A company doing AI chatbots for hotels is "Services | Hospitality & Travel" even if it is a cloud platform.
Only use "Technology | Software Development" for genuinely industry-agnostic horizontal tools with no specific industry focus.
"SaaS & Platforms" is NOT a valid category — it has been eliminated from the taxonomy.

Task 2: Classify the business model. Output EXACTLY one of these values:
- "Product" if they have a SaaS platform, API, dashboard, pricing page, downloadable product, or cloud software.
- "Consultancy" if they offer custom solutions, implementation services, or project-based work.
- "Hybrid" if they do both product and consulting.
- "Unknown" if insufficient evidence.
IMPORTANT: Do NOT output "SaaS" — use "Product" instead.

Task 3: Write a custom 5-word AI use case summary (e.g., "AI drone crop spraying platform").

Task 4: Determine entity type: "Startup", "Corporate", "Teknopark", "Media", "Accelerator", "Government", "NGO", or "Other".

Task 5: Classify cloud relevance. Output EXACTLY one of:
- "Cloud-Native" if the core product IS a cloud service (SaaS, PaaS, IaaS, cloud infra, API platform).
- "Cloud-Enabled" if they use cloud infrastructure to deliver their product (web app on cloud, uses AWS/Azure/GCP) but cloud isn't the core differentiator.
- "Cloud-Adjacent" if their tech could leverage cloud but no strong evidence of cloud dependency.
- "Non-Cloud" if they emphasize on-premise, private cloud, edge-only, embedded systems, local data processing, self-hosted, or hardware-focused products. Also for physical industry companies with no cloud software component.
IMPORTANT: If a company emphasizes "private cloud", "on-premise", "self-hosted", "data sovereignty", or "local deployment", classify as "Non-Cloud" even if they use some cloud internally.

{_CFG.pass_2_sector_context}

Explain your reasoning briefly, then output JSON:
"sector" (string, from matrix)
"tag" (string, from matrix, must be valid pair)
"AI_Use_Case" (string, 5-word custom summary)
"business_model" (string: Product, Consultancy, Hybrid, or Unknown)
"entity_type" (string: Startup, Corporate, Teknopark, Media, Accelerator, Government, NGO, Other)
"cloud_relevance" (string: Cloud-Native, Cloud-Enabled, Cloud-Adjacent, or Non-Cloud)
"reasoning" (string, 1-2 sentences explaining your sector choice)

Output ONLY the raw JSON.
"""
    return _call_llm(prompt)

def pass_3_auditor(facts, mapping, startup_data):
    """Pass 3: Final formatting, normalization, and consistency check."""
    prompt = f"""You are a Data Quality Auditor. Format and normalize the final startup profile.

Startup: {startup_data.get('name', 'Unknown')}
Extracted Facts: {json.dumps(facts, indent=2, ensure_ascii=False)}
Classification: {json.dumps(mapping, indent=2, ensure_ascii=False)}

{_CFG.pass_3_formatting_rules}

Output a clean JSON with these keys:
"website" (string)
"description" (string, 1-2 sentences English)
"city" (string)
"founders" (string, Turkish characters, comma-separated)
"sector" (string)
"tag" (string)
"AI_Use_Case" (string, 5 words)
"Total_Funding" (string, Decimal Comma format or "Unknown")
"Investors" (string or "Unknown")
"Tech_Mentioned" (string or "None")
"Tech_Assumed" (string or "None")
"business_model" (string: Product, Consultancy, Hybrid, Unknown)
"entity_type" (string)

Output ONLY the raw JSON.
"""
    return _call_llm(prompt, model_type="fast")

def three_pass_enrichment(startup_data, search_context, website_context):
    """Full modular enrichment using the three-pass chain."""
    # Pre-LLM heuristic check
    heuristic_type = detect_entity_type_heuristic(
        startup_data.get('name', ''), website_context
    )

    # Pass 1: Fact Extraction
    facts = pass_1_researcher(startup_data, search_context, website_context)
    if not facts:
        print("  [Pass 1] Researcher returned nothing.")
        return None
    issues = validate_llm_output(facts, _SCHEMA_PASS_1, "Pass 1")
    if issues:
        print(f"  [Pass 1] Schema issues: {issues}")
    print("  [Pass 1] Facts extracted.")

    # Pass 2: Taxonomy Mapping
    mapping = pass_2_architect(facts, startup_data)
    if not mapping:
        print("  [Pass 2] Architect returned nothing.")
        return None
    issues = validate_llm_output(mapping, _SCHEMA_PASS_2, "Pass 2")
    if issues:
        print(f"  [Pass 2] Schema issues: {issues}")
    # Support both old and new key names from LLM output
    sector = mapping.get('sector') or mapping.get('Industrial_Sector', '')
    tag = mapping.get('tag') or mapping.get('Tag_Category', '')
    print(f"  [Pass 2] Mapped to: {sector}/{tag} | Model: {mapping.get('business_model')}")

    # Override entity_type if heuristic was confident
    if heuristic_type:
        mapping['entity_type'] = heuristic_type
        print(f"  [Heuristic] Entity type overridden to: {heuristic_type}")

    # Cross-validation: enforce Sector/Tag against VALID_MATRIX
    if (sector, tag) not in VALID_MATRIX:
        corrected = validate_sector_tag(sector, tag)
        if corrected and corrected != (sector, tag):
            print(f"  [Cross-Val] Invalid pair ({sector}/{tag}) → corrected to {corrected[0]}/{corrected[1]}")
            sector, tag = corrected[0], corrected[1]
    mapping['sector'] = sector
    mapping['tag'] = tag

    # Cross-validation: funding data consistency
    funding_in_facts = facts.get('funding_info', 'Unknown')
    if funding_in_facts and funding_in_facts != 'Unknown':
        # Flag if Pass 1 found funding but we need to ensure Pass 3 carries it through
        print(f"  [Cross-Val] Funding detected in facts: {funding_in_facts[:80]}")

    # Pass 3: Formatting & Normalization
    result = pass_3_auditor(facts, mapping, startup_data)
    if not result:
        print("  [Pass 3] Auditor returned nothing.")
        return None
    issues = validate_llm_output(result, _SCHEMA_PASS_3, "Pass 3")
    if issues:
        print(f"  [Pass 3] Schema issues: {issues}")
    print("  [Pass 3] Audit complete.")

    # Cross-validation: if facts had funding but result lost it, inject from facts
    if (funding_in_facts and funding_in_facts != 'Unknown'
            and result.get('Total_Funding', 'Unknown') == 'Unknown'):
        result['Total_Funding'] = funding_in_facts
        print("  [Cross-Val] Restored funding data from Pass 1 facts into final result.")

    # Normalize business_model (catch LLM variants)
    bm = result.get('business_model', 'Unknown')
    if bm.lower() in ['saas', 'platform', 'software']:
        result['business_model'] = 'Product'
    elif bm not in ['Product', 'Consultancy', 'Hybrid', 'Unknown']:
        result['business_model'] = 'Unknown'

    # Carry cloud_relevance from Pass 2 mapping into final result
    cloud_rel = mapping.get('cloud_relevance', result.get('cloud_relevance'))
    valid_cloud = {'Cloud-Native', 'Cloud-Enabled', 'Cloud-Adjacent', 'Non-Cloud'}
    if cloud_rel and cloud_rel in valid_cloud:
        result['cloud_relevance'] = cloud_rel
    else:
        result['cloud_relevance'] = 'Cloud-Adjacent'  # safe default

    # Combine sector/tag into single Category field
    final_sector = result.get('sector') or result.get('Industrial_Sector') or sector
    final_tag = result.get('tag') or result.get('Tag_Category') or tag
    if final_sector and final_tag:
        result['Category'] = f"{final_sector.strip()} | {final_tag.strip()}"
    # Remove legacy keys from output
    result.pop('Industrial_Sector', None)
    result.pop('Tag_Category', None)
    result.pop('sector', None)
    result.pop('tag', None)

    # Normalize city before final validation
    if result.get('city'):
        result['city'] = normalize_city(result['city'])

    # Post-LLM data quality validation
    result, validation_warnings = validate_extracted_data(result, "startup")
    if validation_warnings:
        print(f"  [Validation] {len(validation_warnings)} warnings: {'; '.join(validation_warnings[:3])}")

    return result

# --- Search Escalation ---

def search_escalation(company_name, facts, website=None, description=None):
    """Targeted re-search for missing fields. Domain-anchored + description-aware."""
    import re as _re
    extra = ""
    if not facts:
        return extra

    # Extract domain from website for anchor
    domain = None
    if website:
        m = _re.search(r'https?://(?:www\.)?([^/]+)', website)
        if m:
            domain = m.group(1)
    anchor = f' "{domain}"' if domain else ''

    missing_queries = []
    if facts.get('founders', 'Unknown') == 'Unknown':
        # Anchored to company + domain to prevent wrong-person matches
        missing_queries.append(f'"{company_name}"{anchor} {_CFG.search_escalation_founders_suffix}')
    if facts.get('tech_keywords', 'Unknown') == 'Unknown':
        # Use description to search for relevant tech, not generic
        if description and len(description) > 10:
            missing_queries.append(f'"{company_name}" technology "{description[:40]}"')
        else:
            missing_queries.append(f'"{company_name}"{anchor} tech stack github stackshare')
    if facts.get('funding_info', 'Unknown') == 'Unknown':
        missing_queries.append(f'"{company_name}"{anchor} {_CFG.search_escalation_funding_suffix}')
    if facts.get('founded_year') is None:
        missing_queries.append(f'"{company_name}"{anchor} {_CFG.search_escalation_founded_suffix}')

    for q in missing_queries:
        result = search_with_rotation(q)
        if result:
            # Relevance check: result should mention the company
            if is_relevant_result(company_name, result):
                extra += f"\n{result}"
            else:
                pass  # Discard irrelevant results

    return extra

# --- Crunchbase URL Validation (Three-Layer) ---

def _extract_cb_url_from_results(search_results):
    """Parse search results to find crunchbase.com/organization/ URLs."""
    import re
    urls = re.findall(r'https?://(?:www\.)?crunchbase\.com/organization/[a-z0-9_-]+', search_results.lower())
    return urls[0] if urls else None

def _layer_1_slug_match(cb_url, company_name):
    """Layer 1: Compare the URL slug against company name. Returns score 0-100."""
    from rapidfuzz import fuzz
    try:
        slug = cb_url.split("/organization/")[1].split("/")[0].split("?")[0]
        slug_clean = slug.replace("-", " ").replace("_", " ")
        score = fuzz.token_sort_ratio(slug_clean, company_name.lower())
        return score
    except (IndexError, AttributeError):
        return 0

def _layer_2_description_overlap(cb_snippet, db_description):
    """Layer 2: Check keyword overlap between Crunchbase snippet and DB description. Returns overlap ratio 0.0-1.0."""
    if not cb_snippet or not db_description:
        return 0.0
    
    # Remove common stop words for better signal
    stop = {'the', 'a', 'an', 'and', 'or', 'is', 'are', 'was', 'were', 'in', 'on', 'at', 'to', 'for',
            'of', 'with', 'by', 'from', 'as', 'it', 'its', 'that', 'this', 'be', 'has', 'have', 'had',
            'not', 'but', 'if', 'we', 'our', 'they', 'their', 'can', 'will', 'do', 'does'}
    
    db_words = set(w for w in db_description.lower().split() if len(w) > 2 and w not in stop)
    cb_words = set(w for w in cb_snippet.lower().split() if len(w) > 2 and w not in stop)
    
    if not db_words:
        return 0.0
    
    overlap = len(db_words & cb_words)
    return overlap / max(len(db_words), 1)

def _layer_3_llm_verify(company_name, db_description, cb_snippet):
    """Layer 3: Ask the LLM if these describe the same company. Returns True/False."""
    prompt = f"""Do these two descriptions refer to the SAME company? 
Consider the company name, what they do, and their industry.

Company Name: {company_name}

Description A (from our database):
{db_description[:500] if db_description else 'No description available.'}

Description B (from Crunchbase search result):
{cb_snippet[:500] if cb_snippet else 'No snippet available.'}

Answer ONLY with a JSON object: {{"same_company": true}} or {{"same_company": false}}
Output ONLY the raw JSON.
"""
    result = _call_llm(prompt, model_type="fast")
    if result and isinstance(result, dict):
        return result.get('same_company', False)
    return False

def find_crunchbase_url(company_name, db_description=""):
    """
    Three-layer Crunchbase URL validation.
    Layer 1: Slug fuzzy match (free)
    Layer 2: Description keyword overlap (free)  
    Layer 3: LLM yes/no (only for edge cases)
    
    Returns: (url, confidence) or (None, 0)
    """
    search_results = search_with_rotation(f'"{company_name}" site:crunchbase.com/organization')
    if not search_results:
        return None, 0
    
    cb_url = _extract_cb_url_from_results(search_results)
    if not cb_url:
        return None, 0
    
    # Extract the snippet (text after the URL in search results)
    cb_snippet = ""
    if "Snippet:" in search_results:
        parts = search_results.split("Snippet:")
        if len(parts) > 1:
            cb_snippet = parts[1].split("\n\n")[0].strip()
    
    # Layer 1: Slug match
    slug_score = _layer_1_slug_match(cb_url, company_name)
    print(f"    [CB L1] Slug match: {slug_score}%")
    
    if slug_score < 50:
        print(f"    [CB] Rejected — slug too different.")
        return None, 0
    
    if slug_score >= 85:
        # High confidence from slug alone
        print(f"    [CB] Accepted via slug match ({slug_score}%).")
        return cb_url, 90
    
    # Layer 2: Description overlap (slug was 50-84, need more evidence)
    overlap = _layer_2_description_overlap(cb_snippet, db_description)
    print(f"    [CB L2] Description overlap: {overlap:.2f}")
    
    if overlap >= 0.2:
        print(f"    [CB] Accepted via slug ({slug_score}%) + description overlap ({overlap:.2f}).")
        return cb_url, 85
    
    # Layer 3: LLM verification (edge case — slug 50-84, low description overlap)
    print(f"    [CB L3] Asking LLM to verify...")
    is_same = _layer_3_llm_verify(company_name, db_description, cb_snippet)
    
    if is_same:
        print(f"    [CB] Accepted via LLM verification.")
        return cb_url, 75
    else:
        print(f"    [CB] Rejected by LLM.")
        return None, 0

# --- Data Governance & Change Logging ---

def get_utc_now():
    """All agents MUST use this for standardizing timestamps."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def agent_update_field(cursor, entity_type, entity_id, agent_name, agent_confidence, field_name, new_value):
    """
    Attempts to update a specific field on a startup or investor.
    It checks the change_log to see if this field was modified by a higher-confidence agent.
    If so, it skips the update. If not, it updates the record and logs the change.
    
    Returns True if the update occurred, False if it was skipped due to priority rules.
    """
    # Safety: If value is a list, join it to a string
    if isinstance(new_value, list):
        new_value = ", ".join([str(x) for x in new_value if x])

    # Don't bother saving empty values
    if new_value is None or str(new_value).strip().lower() in ['unknown', 'none', 'nan', '']:
        return False

    # Check the history of this exact field
    row = cursor.execute('''
        SELECT agent_name, confidence_score, new_value
        FROM change_log 
        WHERE entity_type = ? AND entity_id = ? AND changed_field = ?
        ORDER BY id DESC LIMIT 1
    ''', (entity_type, entity_id, field_name)).fetchone()

    old_value = None
    if row:
        last_agent, last_confidence, old_value = row
        # Collision rule: Only overwrite if our confidence is >= the previous agent's confidence
        if agent_confidence < (last_confidence or 0):
            # Print skip reason only if values differ (otherwise it's just a duplicate finding)
            if str(old_value) != str(new_value):
                # print(f"  [Skip] {field_name}: '{new_value}' rejected. {last_agent} (conf: {last_confidence}) holds priority.")
                pass
            return False

    # Skip if the value hasn't actually changed
    if str(old_value) == str(new_value):
        return False

    # Insert into the change log
    cursor.execute('''
        INSERT INTO change_log (entity_type, entity_id, agent_name, changed_field, old_value, new_value, confidence_score, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (entity_type, entity_id, agent_name, field_name, str(old_value) if old_value else None, str(new_value), agent_confidence, get_utc_now()))

    # Process the actual update based on entity type
    table_map = {
        "startup": ("startups", "id"),
        "investor": ("investors", "investor_id"),
        "ecosystem_entity": ("ecosystem_entities", "id"),
    }
    table, id_col = table_map.get(entity_type, ("startups", "id"))
    
    cursor.execute(f"UPDATE {table} SET {field_name} = ?, last_updated = ? WHERE {id_col} = ?", 
                   (new_value, get_utc_now(), entity_id))
    
    return True

# --- Investor Workflow ---

def enrich_investor(investor_name):
    """Legacy single-pass investor enrichment. Kept for backward compatibility."""
    queries = _CFG.investor_search_queries(investor_name)
    search_context = search_with_rotation(queries[0])
    if not search_context and len(queries) > 1:
        search_context = search_with_rotation(queries[1])

    prompt = f"""You are a data extraction agent. Given search results about an investor, extract their profile.

Search Results:
{search_context}

Investor Name: {investor_name}

Output ONLY a valid JSON object with these keys:
"investor_type" (one of: "VC", "Angel", "Corporate", "Accelerator", "PE", "Government", "Unknown")
"website" (string, or "Unknown")
"location" (string, city/country, or "Unknown")
"focus_areas" (string, comma-separated industry focuses, or "Unknown")

Output ONLY the raw JSON.
"""
    return _call_llm(prompt)

# --- Three-Pass Investor Enrichment ---

def _investor_pass_1(investor_name, search_context):
    """Pass 1: Extract raw facts about the investor."""
    prompt = f"""You are a Fact-Extraction Agent for investors. Extract raw facts about this investor.

Investor Name: {investor_name}

Web Search Results:
{search_context}

Extract ONLY the facts you can find. Output a JSON with these keys:
"investor_type" (string: VC, Angel, Corporate, Accelerator, PE, Government, Family Office, or Unknown)
"website" (string, URL or "Unknown")
"location" (string, city/country or "Unknown")
"founded_year" (integer or null)
"fund_size" (string, any mention of fund size/AUM, or "Unknown")
"focus_areas" (string, what industries they invest in, or "Unknown")
"portfolio_companies" (list of strings, company names they have invested in)
"notable_exits" (string, any exits or IPOs, or "Unknown")

If you cannot find evidence, output "Unknown" or an empty list. Output ONLY the raw JSON.
"""
    return _call_llm(prompt, model_type="fast")

def _investor_pass_2(facts, investor_name):
    """Pass 2: Classify and structure the investor profile."""
    prompt = f"""You are an Investor Classification Agent. Given facts about an investor, classify and structure them.

Investor Name: {investor_name}
Extracted Facts:
{json.dumps(facts, indent=2, ensure_ascii=False)}

Task 1: Confirm the investor_type. Pick EXACTLY one:
"VC", "Angel", "Corporate", "Accelerator", "PE", "Government", "Family Office", "Unknown"

Task 2: Format the fund_size. If a number is mentioned, format as Decimal Comma (e.g., "50.000.000,00 USD"). If unknown, "Unknown".

Task 3: List the focus areas as a clean comma-separated string (e.g., "AI, Fintech, SaaS, Deep Tech").

Task 4: {_CFG.investor_domestic_instruction}

Output JSON:
"investor_type" (string, from the list above)
"fund_size" (string, Decimal Comma format or "Unknown")
"focus_areas" (string, comma-separated)
"domestic_portfolio" (list of strings, {_CFG.region_name} company names only)
"total_portfolio_count" (integer, estimated total portfolio size)

Output ONLY the raw JSON.
"""
    return _call_llm(prompt)

def _investor_pass_3(facts, classification, investor_name):
    """Pass 3: Final formatting and normalization."""
    prompt = f"""You are a Data Quality Auditor for investor profiles.

Investor: {investor_name}
Facts: {json.dumps(facts, indent=2, ensure_ascii=False)}
Classification: {json.dumps(classification, indent=2, ensure_ascii=False)}

Rules:
1. {_CFG.investor_character_rules}
2. Fund size in {_CFG.funding_format_description} or "Unknown".
3. Location should be "City, Country" format.
4. Focus areas as clean comma-separated string.

Output a clean JSON:
"investor_type" (string)
"website" (string or "Unknown")
"location" (string, "City, Country" or "Unknown")
"founded_year" (integer or null)
"fund_size" (string)
"focus_areas" (string)
"portfolio_companies" (list of strings, all companies)
"domestic_portfolio" (list of strings, {_CFG.region_name} companies only)
"portfolio_count" (integer)
"notable_investments" (string, 5 most notable comma-separated, or "Unknown")

Output ONLY the raw JSON.
"""
    return _call_llm(prompt, model_type="fast")

def three_pass_investor_enrichment(investor_name):
    """Full modular enrichment for investors using the three-pass chain."""
    # Gather context using region-specific queries
    queries = _CFG.investor_search_queries(investor_name)
    search_parts = []
    for q in queries:
        result = search_with_rotation(q)
        if result:
            search_parts.append(result)
    search_context = "\n".join(search_parts)
    
    if not search_context.strip():
        print(f"    [Investor] No search results for {investor_name}")
        return None
    
    # Pass 1: Fact Extraction
    facts = _investor_pass_1(investor_name, search_context)
    if not facts:
        print(f"    [Pass 1] Investor researcher returned nothing.")
        return None
    print(f"    [Pass 1] Investor facts extracted.")
    
    # Pass 2: Classification
    classification = _investor_pass_2(facts, investor_name)
    if not classification:
        print(f"    [Pass 2] Investor classifier returned nothing.")
        return None
    print(f"    [Pass 2] Type: {classification.get('investor_type')} | Portfolio: {classification.get('total_portfolio_count', '?')}")
    
    # Pass 3: Formatting
    result = _investor_pass_3(facts, classification, investor_name)
    if not result:
        print(f"    [Pass 3] Investor auditor returned nothing.")
        return None
    print(f"    [Pass 3] Investor profile complete.")
    
    return result

def discover_portfolio_startups(conn, investor_id, investor_name, portfolio_list):
    """Cross-reference portfolio companies against startups table. Create skeletons for new ones."""
    if not portfolio_list or not isinstance(portfolio_list, list):
        return 0
    
    from rapidfuzz import fuzz
    cursor = conn.cursor()
    discovered = 0
    
    for company in portfolio_list:
        if not company or not isinstance(company, str):
            continue
        company = company.strip()
        if not is_valid_company_name(company):
            continue

        # Skip known invalid names and international companies
        _invalid = {'unknown', 'n/a', 'none', '', 'company', 'startup', 'various'}
        _intl_blocklist = {
            'openai', 'anthropic', 'google', 'meta', 'microsoft', 'amazon', 'apple',
            'nvidia', 'databricks', 'airbnb', 'uber', 'stripe', 'spacex', 'bytedance',
            'tiktok', 'perplexity', 'mistral', 'hugging face', 'stability ai',
            'runway', 'groq', 'verizon', 'starling bank', 'wayve', 'improbable',
        }
        if company.lower() in _invalid or company.lower() in _intl_blocklist:
            continue
        # Skip aggregate entries
        if 'companies backed by' in company.lower() or company.lower().startswith('various'):
            continue
        
        # Check if already in DB (exact or fuzzy)
        all_startups = cursor.execute("SELECT id, company_name FROM startups").fetchall()
        matched = False
        for sid, sname in all_startups:
            if fuzz.token_sort_ratio(normalize_turkish(company), normalize_turkish(sname)) >= 80:
                matched = True
                # Link investment if not already linked
                existing_link = cursor.execute(
                    "SELECT id FROM investments WHERE startup_id = ? AND investor_id = ?",
                    (sid, investor_id)
                ).fetchone()
                if not existing_link:
                    cursor.execute('''
                        INSERT INTO investments (startup_id, investor_id, round_type, source_url, investment_date)
                        VALUES (?, ?, 'NA', 'portfolio_discovery', ?)
                    ''', (sid, investor_id, None))
                    print(f"      [Link] {company} → existing startup {sname} (ID {sid})")
                break
        
        if not matched:
            # Create skeleton startup for this portfolio company
            try:
                cursor.execute('''
                    INSERT INTO startups (company_name, Status, data_confidence, processed_at)
                    VALUES (?, 'Active', 0, ?)
                ''', (company, get_utc_now()))
                new_id = cursor.lastrowid
                # Create investment link
                cursor.execute('''
                    INSERT INTO investments (startup_id, investor_id, round_type, source_url, investment_date)
                    VALUES (?, ?, 'NA', 'portfolio_discovery', ?)
                ''', (new_id, investor_id, None))
                discovered += 1
                print(f"      [NEW] Discovered: {company} (ID {new_id}) from {investor_name}'s portfolio")
            except Exception:
                pass  # May fail on unique constraint
    
    if discovered > 0:
        conn.commit()
    return discovered
