"""
data_guard.py — Centralized data quality guards for the startup ecosystem pipeline.

Prevents junk entries (scraped text, email addresses, political figures, news URLs)
from being inserted into the startups, investors, and related tables.

All guards print [Guard] messages when rejecting entries for monitoring.
"""
import re
from region_config import get_active_config

_CFG = get_active_config()

# ─── City Normalization ─────────────────────────────────────────────────────

# Canonical city map: ASCII / common LLM variants → correct Turkish spelling
_CITY_ALIASES = {
    "istanbul": "İstanbul",
    "izmir": "İzmir",
    "eskisehir": "Eskişehir",
    "diyarbakir": "Diyarbakır",
    "tekirdag": "Tekirdağ",
    "canakkale": "Çanakkale",
    "mugla": "Muğla",
    "aydin": "Aydın",
    "duzce": "Düzce",
}

# Build from config's major_cities too (lowercase → proper)
for _city in getattr(_CFG, 'major_cities', []):
    _CITY_ALIASES.setdefault(_city.lower(), _city)
for _city in getattr(_CFG, 'domestic_markers', []):
    if len(_city) > 3 and not _city.lower() in ('turkey', 'türkiye'):
        _CITY_ALIASES.setdefault(_city.lower(), _city.title())
# Ensure canonical forms win over title-cased defaults
_CITY_ALIASES["istanbul"] = "İstanbul"
_CITY_ALIASES["izmir"] = "İzmir"
_CITY_ALIASES["eskişehir"] = "Eskişehir"


def normalize_city(city):
    """Normalize a city name to its canonical Turkish spelling.

    Handles:
    - ASCII variants: Istanbul → İstanbul, Izmir → İzmir
    - LLM verbose outputs: "Unknown, possibly Istanbul" → İstanbul
    - Whitespace / case issues

    Returns the normalized city or the original if no mapping exists.
    """
    if not city or not isinstance(city, str):
        return city

    city = city.strip()
    if not city or city in ("Unknown", "Foreign"):
        return city

    # Handle LLM verbose outputs like "Unknown, possibly Istanbul"
    # or "Istanbul (European side)" — extract the core city name
    for alias, canonical in _CITY_ALIASES.items():
        if alias in city.lower():
            return canonical

    # Direct lookup
    key = city.lower()
    if key in _CITY_ALIASES:
        return _CITY_ALIASES[key]

    return city


# ─── Company Name Validation ────────────────────────────────────────────────

_INVALID_COMPANY_NAMES = {
    'unknown', 'n/a', 'none', '', 'company', 'startup', 'various',
    'the company', 'unnamed', 'test', 'null', 'undefined',
}

# Turkish scrape fragments that commonly appear
_TURKISH_JUNK_PATTERNS = re.compile(
    r'(haberleri|haber|haberi|sayfası|sayfasi|için|değerlendirme|'
    r'kurucusu|yatırım|şirket|ları$|leri$|teknoloji haberleri|'
    r'başkan[ıi]|cumhurbaşkan[ıi])',
    re.IGNORECASE
)

# Patterns that indicate scraped text, not a company name
_JUNK_COMPANY_PATTERNS = [
    re.compile(r'^https?://'),           # URLs
    re.compile(r'@'),                     # Email addresses
    re.compile(r'^\['),                   # Bracket artifacts [Company]
    re.compile(r'^\d'),                   # Starts with digit
    re.compile(r'\.\w{2,4}$'),           # Ends like a domain (.com, .org)
    re.compile(r'\s{3,}'),              # Excessive whitespace (scraped)
    re.compile(r'[<>{}|\\]'),           # HTML/code artifacts
]


def is_valid_company_name(name):
    """Check if a company name is valid (not junk/scraped text).

    Returns True if valid, False if junk.
    Prints [Guard] messages when rejecting.
    """
    if not name or not isinstance(name, str):
        return False

    name = name.strip()

    # Too short
    if len(name) < 3:
        print(f"  [Guard] Rejected company name (too short): '{name}'")
        return False

    # Too long — likely a scraped sentence
    if len(name) > 100:
        print(f"  [Guard] Rejected company name (too long, likely scraped text): '{name[:60]}...'")
        return False

    # Known invalid names
    if name.lower() in _INVALID_COMPANY_NAMES:
        print(f"  [Guard] Rejected company name (known invalid): '{name}'")
        return False

    # Junk patterns (URLs, emails, bracket artifacts, etc.)
    for pattern in _JUNK_COMPANY_PATTERNS:
        if pattern.search(name):
            print(f"  [Guard] Rejected company name (junk pattern): '{name[:60]}'")
            return False

    # Turkish scraped text fragments
    if _TURKISH_JUNK_PATTERNS.search(name):
        print(f"  [Guard] Rejected company name (Turkish scrape fragment): '{name[:60]}'")
        return False

    # Contains too many spaces — likely a sentence, not a name
    if name.count(' ') > 8:
        print(f"  [Guard] Rejected company name (too many words, likely sentence): '{name[:60]}'")
        return False

    return True


# ─── Investor Name Validation ───────────────────────────────────────────────

_INVALID_INVESTOR_NAMES = {
    'unknown', 'n/a', 'none', '', 'investor', 'investors', 'various',
    'the investor', 'unnamed', 'test', 'null', 'undefined',
    'undisclosed', 'undisclosed investors', 'angel investor',
    'angel investors', 'various investors',
}

# Political figure / title patterns (Turkish and English)
_POLITICAL_PATTERNS = re.compile(
    r'(cumhurbaşkan[ıi]|başbakan|bakan[ıi]|milletvekili|'
    r'president\s+of|prime\s+minister|minister\s+of|'
    r'mayor\s+of|governor\s+of|senator|congressman|'
    r'fransa|almanya|ingiltere)',
    re.IGNORECASE
)


def is_valid_investor_name(inv_name):
    """Check if an investor name is valid (not junk/political figure/scraped text).

    Returns True if valid, False if junk.
    Prints [Guard] messages when rejecting.
    """
    if not inv_name or not isinstance(inv_name, str):
        return False

    inv_name = inv_name.strip()

    # Too short
    if len(inv_name) < 3:
        print(f"  [Guard] Rejected investor name (too short): '{inv_name}'")
        return False

    # Too long — likely a scraped sentence
    if len(inv_name) > 80:
        print(f"  [Guard] Rejected investor name (too long): '{inv_name[:60]}...'")
        return False

    # Known invalid names
    if inv_name.lower() in _INVALID_INVESTOR_NAMES:
        print(f"  [Guard] Rejected investor name (known invalid): '{inv_name}'")
        return False

    # Email addresses
    if '@' in inv_name:
        print(f"  [Guard] Rejected investor name (email address): '{inv_name}'")
        return False

    # URLs
    if inv_name.startswith('http') or '.com' in inv_name.lower():
        print(f"  [Guard] Rejected investor name (URL): '{inv_name[:60]}'")
        return False

    # Political figures / titles
    if _POLITICAL_PATTERNS.search(inv_name):
        print(f"  [Guard] Rejected investor name (political figure/title): '{inv_name}'")
        return False

    # Turkish scraped text
    if _TURKISH_JUNK_PATTERNS.search(inv_name):
        print(f"  [Guard] Rejected investor name (Turkish scrape fragment): '{inv_name[:60]}'")
        return False

    # Bracket artifacts
    if inv_name.startswith('[') or inv_name.startswith('{'):
        print(f"  [Guard] Rejected investor name (bracket artifact): '{inv_name[:60]}'")
        return False

    # Too many spaces — likely a sentence
    if inv_name.count(' ') > 8:
        print(f"  [Guard] Rejected investor name (too many words): '{inv_name[:60]}'")
        return False

    # Starts with digit
    if inv_name[0].isdigit():
        print(f"  [Guard] Rejected investor name (starts with digit): '{inv_name}'")
        return False

    return True


# ─── Website URL Validation ─────────────────────────────────────────────────

_NEWS_DOMAINS = {
    'youtube.com', 'linkedin.com', 'twitter.com', 'x.com', 'facebook.com',
    'instagram.com', 'tiktok.com', 'reddit.com',
    'yahoo.com', 'yahoo.co.jp', 'baidu.com', 'google.com',
    'bbc.com', 'bbc.co.uk', 'cnn.com', 'reuters.com',
    'bloomberg.com', 'forbes.com', 'techcrunch.com',
    'webrazzi.com', 'hurriyet.com.tr', 'milliyet.com.tr',
    'sozcu.com.tr', 'sabah.com.tr', 'haberturk.com',
    'ntv.com.tr', 'cnnturk.com', 'dha.com.tr',
    'aa.com.tr', 'trthaber.com',
    'medium.com', 'substack.com', 'wordpress.com',
}


def is_valid_website(url):
    """Check if a URL is a valid company website (not a news/social site).

    Returns True if valid, False if it's a news/social/search URL.
    """
    if not url or not isinstance(url, str):
        return True  # Allow empty — many startups don't have websites yet

    url = url.strip().lower()
    if not url or url == 'unknown':
        return True

    # Check against known news/social domains
    for domain in _NEWS_DOMAINS:
        if domain in url:
            print(f"  [Guard] Rejected website (news/social URL): '{url[:80]}'")
            return False

    return True


# ─── Confidence Computation ─────────────────────────────────────────────────

def compute_initial_confidence(data):
    """Compute a minimum confidence score based on which fields are filled.

    Args:
        data: dict with enrichment fields (description, website, Category, etc.)

    Returns:
        int: confidence score (0-100), at least 35 if core fields present.
    """
    score = 0

    # Core fields: description, website, category
    desc = data.get('description', '') or ''
    website = data.get('website', '') or ''
    category = data.get('Category', '') or data.get('category', '') or ''

    if desc and desc.lower() not in ('unknown', 'none', ''):
        score += 15
    if website and website.lower() not in ('unknown', 'none', ''):
        score += 10
    if category and category.lower() not in ('unknown', 'none', ''):
        score += 10

    # Supplementary fields
    if data.get('founders') and data['founders'] not in ('Unknown', 'None', ''):
        score += 5
    if data.get('city') and data['city'] not in ('Unknown', 'None', ''):
        score += 5
    if data.get('AI_Use_Case') and data['AI_Use_Case'] not in ('Unknown', 'None', ''):
        score += 5
    if data.get('Tech_Mentioned') and data['Tech_Mentioned'] not in ('Unknown', 'None', ''):
        score += 5

    # Ensure minimum of 35 if core trio is present
    if desc and website and category:
        score = max(score, 35)

    return min(score, 100)
