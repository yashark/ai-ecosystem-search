"""
Funding Format Normalization Utility
Standardizes Total_Funding_Formatted to Turkish decimal format: "1.250.000,00 USD"

Handles:
- "$1M", "$1.5M", "$100K"
- "$1 million", "$400 million", "$1.5 billion"
- "1M USD", "$10,000,000.00 USD"
- "70,2 milyon ABD doları" (Turkish text)
- Already-correct Turkish decimal: "1.250.000,00 USD"
- "$0", "Unknown", junk → NULL

Usage:
    python3 util_normalize_funding.py              # Dry-run
    python3 util_normalize_funding.py --commit      # Apply changes
"""
import sqlite3
import re
import argparse

DB_NAME = "Master.db"

# Multiplier lookup
_MULTIPLIERS = {
    "k": 1_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
    "thousand": 1_000,
    "million": 1_000_000,
    "billion": 1_000_000_000,
    "milyon": 1_000_000,
    "milyar": 1_000_000_000,
}

# Currency mapping
_CURRENCY_MAP = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "tl": "TRY",
    "try": "TRY",
    "trl": "TRY",
    "usd": "USD",
    "eur": "EUR",
    "gbp": "GBP",
    "abd doları": "USD",
    "euro": "EUR",
    "euros": "EUR",
    "dollars": "USD",
}


def _format_turkish_decimal(amount, currency="USD"):
    """Format a numeric amount as Turkish decimal: 1.250.000,00 USD"""
    if amount <= 0:
        return None
    # Round to 2 decimals
    amount = round(amount, 2)
    integer_part = int(amount)
    decimal_part = round((amount - integer_part) * 100)

    # Format with period as thousands separator
    int_str = f"{integer_part:,}".replace(",", ".")
    return f"{int_str},{decimal_part:02d} {currency}"


def _parse_funding(raw):
    """Parse a funding string into (amount_float, currency_str) or (None, None)."""
    if not raw:
        return None, None

    original = raw.strip()
    text = original.lower().strip()

    # Skip junk values
    junk_patterns = ("unknown", "n/a", "none", "null", "", "not disclosed",
                     "undisclosed", "not available", "0", "$0", "0,00 usd",
                     "$unknown")
    if text in junk_patterns or text.startswith("over ") or "deals completed" in text or "girişim" in text:
        return None, None
    # Skip "$<5 Million" style ranges, long narrative descriptions, "$213 trillion" outliers
    if text.startswith("$<") or len(text) > 80 or "trillion" in text:
        return None, None

    # Already in Turkish decimal format? e.g. "1.250.000,00 USD" or "€6.000.000,00"
    m = re.match(r'^[€$£]?([\d.]+),(\d{2})\s*([A-Za-z€$£]{1,4})?\s*$', original)
    if m:
        int_part = m.group(1).replace(".", "")
        dec_part = m.group(2)
        raw_cur = (m.group(3) or "").strip().lower()
        currency = _CURRENCY_MAP.get(raw_cur, "USD") if raw_cur else "USD"
        # Detect leading currency symbol
        if original.startswith("€"):
            currency = "EUR"
        elif original.startswith("£"):
            currency = "GBP"
        try:
            amount = float(f"{int_part}.{dec_part}")
            return amount, currency
        except ValueError:
            pass

    # Turkish decimal with non-standard currency suffix: "30.000.000,00 GBP", "35.000.000,00 TL"
    m = re.match(r'^([\d.]+),(\d{2})\s+(\S+)$', original)
    if m:
        int_part = m.group(1).replace(".", "")
        dec_part = m.group(2)
        raw_cur = m.group(3).strip().lower()
        currency = _CURRENCY_MAP.get(raw_cur, raw_cur.upper())
        try:
            amount = float(f"{int_part}.{dec_part}")
            return amount, currency
        except ValueError:
            pass

    # Multi-value with comma separator: "30.000.000,00 GBP, 38.600.000 USD" — take first
    if ", " in original:
        first_part = original.split(", ")[0].strip()
        sub_amount, sub_cur = _parse_funding(first_part)
        if sub_amount:
            return sub_amount, sub_cur

    # Multi-value with "=" total: "4.000.000,00 USD + 11.000.000,00 USD ... = 215.000.000,00 USD"
    eq_match = re.search(r'=\s*([\d.]+),(\d{2})\s*([A-Z]{3})', original)
    if eq_match:
        int_part = eq_match.group(1).replace(".", "")
        dec_part = eq_match.group(2)
        currency = eq_match.group(3)
        try:
            amount = float(f"{int_part}.{dec_part}")
            return amount, currency
        except ValueError:
            pass

    # Also match Turkish decimal without currency: "25.700,00"
    m = re.match(r'^([\d.]+),(\d{2})$', original)
    if m:
        int_part = m.group(1).replace(".", "")
        dec_part = m.group(2)
        try:
            amount = float(f"{int_part}.{dec_part}")
            return amount, "USD"
        except ValueError:
            pass

    # Detect currency
    currency = "USD"
    for token, cur in _CURRENCY_MAP.items():
        if token in text:
            currency = cur
            break

    # Pattern: "5.500.000 USD", "900.000 TL" (Turkish integer, no decimal comma) — before stripping
    m = re.match(r'^([\d.]+)\s+(usd|tl|try|eur|gbp|euro)\s*$', text)
    if m:
        number_str = m.group(1)
        cur_token = m.group(2)
        currency = _CURRENCY_MAP.get(cur_token, currency)
        parts = number_str.split(".")
        if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]):
            amount = float(number_str.replace(".", ""))
            if amount > 0:
                return amount, currency

    # Strip currency symbols and annotations
    cleaned = re.sub(r'[\$€£]', '', text)
    cleaned = re.sub(r'\(.*?\)', '', cleaned)  # Remove parenthetical notes
    cleaned = re.sub(r'over \d+ rounds?', '', cleaned)  # "over 5 rounds"
    cleaned = re.sub(r'pre-acquisition', '', cleaned)
    cleaned = re.sub(r'\+$', '', cleaned)  # trailing +
    cleaned = re.sub(r'\s*(usd|eur|gbp|try|trl|abd doları|euros?|dollars?)\s*', ' ', cleaned)
    cleaned = cleaned.strip()

    # Pattern: "$1.1bn", "$4.9mn"
    m = re.match(r'^([\d,.]+)\s*(bn|mn)\b', cleaned)
    if m:
        number_str = m.group(1).replace(",", "")
        mult = 1_000_000_000 if m.group(2) == "bn" else 1_000_000
        try:
            return float(number_str) * mult, currency
        except ValueError:
            pass

    # Pattern: "NOK 684.4M", "SEK 300,000" (foreign currencies)
    m = re.match(r'^\s*([a-z]{3})\s+([\d,.]+)\s*([kmb])?\s*$', cleaned)
    if m:
        cur_code = m.group(1).upper()
        number_str = m.group(2).replace(",", "")
        mult_key = m.group(3)
        mult = _MULTIPLIERS.get(mult_key, 1) if mult_key else 1
        try:
            return float(number_str) * mult, cur_code
        except ValueError:
            pass

    # Pattern: "1,000,000 TL" (US-format comma thousands with Turkish currency)
    m = re.match(r'^([\d,]+)\s*(tl|try)\s*$', cleaned)
    if m:
        number_str = m.group(1).replace(",", "")
        try:
            amount = float(number_str)
            if amount > 0:
                return amount, "TRY"
        except ValueError:
            pass

    # Pattern: "70,2 milyon" (Turkish — comma as decimal separator)
    m = re.match(r'^([\d]+)[,.](\d+)\s*(milyon|milyar)', cleaned)
    if m:
        number = float(f"{m.group(1)}.{m.group(2)}")
        mult = _MULTIPLIERS.get(m.group(3), 1)
        return number * mult, currency

    # Pattern: "1.5M", "100K", "1.96B"
    m = re.match(r'^([\d,.]+)\s*([kmb])\b', cleaned)
    if m:
        number_str = m.group(1).replace(",", "")
        mult = _MULTIPLIERS.get(m.group(2), 1)
        try:
            return float(number_str) * mult, currency
        except ValueError:
            pass

    # Pattern: "1.5 million", "400 million", "1 billion"
    m = re.match(r'^([\d,.]+)\s*(thousand|million|billion|milyon|milyar)', cleaned)
    if m:
        number_str = m.group(1).replace(",", "")
        mult = _MULTIPLIERS.get(m.group(2), 1)
        try:
            return float(number_str) * mult, currency
        except ValueError:
            pass

    # Pattern: "$10,000,000" or "$10,000,000.00" (US format with commas)
    m = re.match(r'^([\d,]+)(?:\.(\d{1,2}))?\s*$', cleaned)
    if m:
        number_str = m.group(1).replace(",", "")
        dec = m.group(2) or "0"
        try:
            amount = float(f"{number_str}.{dec}")
            if amount > 0:
                return amount, currency
        except ValueError:
            pass

    # Pattern: plain number "14072219"
    m = re.match(r'^(\d+)$', cleaned)
    if m:
        try:
            amount = float(m.group(1))
            if amount > 0:
                return amount, currency
        except ValueError:
            pass

    return None, None


def normalize_funding(conn, commit=False):
    """Normalize all Total_Funding_Formatted values."""
    cursor = conn.cursor()
    rows = cursor.execute("""
        SELECT id, company_name, Total_Funding_Formatted
        FROM startups
        WHERE Total_Funding_Formatted IS NOT NULL AND Total_Funding_Formatted != ''
    """).fetchall()

    stats = {"already_ok": 0, "normalized": 0, "nullified": 0, "failed": 0}

    for sid, name, raw in rows:
        amount, currency = _parse_funding(raw)

        if amount is None:
            # Junk value — set to NULL
            if raw.strip().lower() not in ("unknown", "$0", "0"):
                stats["failed"] += 1
                print(f"  [SKIP] [{sid}] {name}: cannot parse '{raw}'")
            else:
                stats["nullified"] += 1
                if commit:
                    cursor.execute("UPDATE startups SET Total_Funding_Formatted = NULL WHERE id = ?", (sid,))
            continue

        formatted = _format_turkish_decimal(amount, currency)
        if formatted is None:
            stats["nullified"] += 1
            if commit:
                cursor.execute("UPDATE startups SET Total_Funding_Formatted = NULL WHERE id = ?", (sid,))
            continue

        if formatted == raw:
            stats["already_ok"] += 1
            continue

        stats["normalized"] += 1
        if commit:
            cursor.execute("UPDATE startups SET Total_Funding_Formatted = ? WHERE id = ?", (formatted, sid))

    if commit:
        conn.commit()

    print(f"\n{'='*60}")
    print(f"  FUNDING NORMALIZATION SUMMARY")
    print(f"{'='*60}")
    print(f"  Total processed:   {len(rows)}")
    print(f"  Already correct:   {stats['already_ok']}")
    print(f"  Normalized:        {stats['normalized']}")
    print(f"  Nullified (junk):  {stats['nullified']}")
    print(f"  Failed to parse:   {stats['failed']}")


def main():
    parser = argparse.ArgumentParser(description="Normalize funding formats to Turkish decimal")
    parser.add_argument("--commit", action="store_true", help="Apply changes (default: dry-run)")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA journal_mode=WAL")

    print("=" * 60)
    print(f"  Funding Normalization ({'COMMIT' if args.commit else 'DRY RUN'})")
    print("=" * 60)

    normalize_funding(conn, commit=args.commit)

    if not args.commit:
        print(f"\n  DRY RUN — no changes written. Use --commit to apply.")
    print()
    conn.close()


if __name__ == "__main__":
    main()
