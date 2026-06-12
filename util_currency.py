import re
from datetime import datetime
from currency_converter import CurrencyConverter, RateNotFoundError

# Initialize offline converter (uses European Central Bank historical data)
try:
    c = CurrencyConverter(fallback_on_wrong_date=True, fallback_on_missing_rate=True)
except Exception as e:
    print(f"Warning: CurrencyConverter failed to init: {e}")
    c = None

def parse_amount(amount_str):
    """
    Convert European/Turkish number string (e.g. 1.250.000,00 USD) to float.
    Handles '4.5M USD', '$5M', etc.
    Returns (numeric_value, currency_code)
    """
    if not amount_str or str(amount_str).strip().lower() in ['unknown', 'none', '']:
        return None, None
        
    amount_str = str(amount_str).upper()
    
    # 1. Detect Currency
    currency = "USD"
    if "TRY" in amount_str or "TL" in amount_str:
        currency = "TRY"
        amount_str = amount_str.replace("TRY", "").replace("TL", "")
    elif "EUR" in amount_str or "€" in amount_str:
        currency = "EUR"
        amount_str = amount_str.replace("EUR", "").replace("€", "")
    elif "GBP" in amount_str or "£" in amount_str:
        currency = "GBP"
        amount_str = amount_str.replace("GBP", "").replace("£", "")
    else:
        amount_str = amount_str.replace("USD", "").replace("$", "")
        
    # 2. Extract Numbers & Multipliers (M = million, K = thousand)
    multiplier = 1
    if "M" in amount_str or "MILLION" in amount_str or "MILYON" in amount_str:
        multiplier = 1_000_000
    elif "K" in amount_str or "BIN" in amount_str or "BİN" in amount_str:
        multiplier = 1_000
    elif "B" in amount_str or "BILLION" in amount_str or "MILYAR" in amount_str:
        multiplier = 1_000_000_000
        
    # Remove all letters and spaces
    clean_str = re.sub(r'[A-Za-z\s]', '', amount_str)
    
    # If it has both dots and commas (e.g. 1.250.000,00), convert to standard float format
    if '.' in clean_str and ',' in clean_str:
        # If dot appears before comma, assume European (dot=thousands, comma=decimal)
        if clean_str.find('.') < clean_str.find(','):
            clean_str = clean_str.replace('.', '').replace(',', '.')
        else:
            # Comma before dot (US style 1,250,000.00)
            clean_str = clean_str.replace(',', '')
    elif ',' in clean_str:
        # Just comma. Is it decimal or thousands? Usually decimal in TR.
        # Check if exactly 2 digits after comma
        parts = clean_str.split(',')
        if len(parts) == 2 and len(parts[1]) == 2:
            clean_str = clean_str.replace(',', '.')
        else:
            clean_str = clean_str.replace(',', '')
    
    try:
        val = float(clean_str) * multiplier
        return val, currency
    except ValueError:
        return None, currency

def convert_to_usd(amount_str, date_str):
    """
    Parses an amount string (e.g., "1.500.000 TRY") and converts it to USD 
    based on the historical exchange rate for the given date (YYYY-MM-DD).
    Returns (raw_value, usd_value, currency).
    """
    val, currency = parse_amount(amount_str)
    if val is None:
        return None, None, None
        
    if currency == "USD":
        return val, val, "USD"
        
    if not c:
        return val, None, currency
        
    # Parse date
    try:
        if date_str and date_str.lower() not in ['unknown', '']:
            # Assuming format like "2024-05-12"
            dt = datetime.strptime(date_str.split('T')[0], "%Y-%m-%d")
        else:
            dt = datetime.now()
    except Exception:
        dt = datetime.now()
        
    # Convert
    try:
        usd_val = c.convert(val, currency, 'USD', date=dt)
        return val, round(usd_val, 2), currency
    except RateNotFoundError:
        # Attempt fallback to latest rate if specific date fails
        try:
            usd_val = c.convert(val, currency, 'USD')
            return val, round(usd_val, 2), currency
        except Exception:
            return val, None, currency
    except Exception as e:
        print(f"Currency conversion error: {e}")
        return val, None, currency

# Quick test
if __name__ == "__main__":
    print(convert_to_usd("1.250.000,00 TRY", "2023-01-15"))
    print(convert_to_usd("5M EUR", "2024-05-12"))
    print(convert_to_usd("$2.5M", "2025-01-01"))
