import sqlite3
import json
import re

DB_NAME = "Master.db"

def fix_founders():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    rows = cursor.execute("SELECT id, founders FROM startups").fetchall()
    updated = 0
    for row in rows:
        sid, founders = row
        if not founders or str(founders).strip().lower() in ['none', 'unknown', '']:
            continue
            
        new_founders = founders
        
        # 1. Handle JSON array - this naturally fixes unicode escapes like \u00e7
        if founders.strip().startswith('[') and founders.strip().endswith(']'):
            try:
                parsed = json.loads(founders)
                if isinstance(parsed, list):
                    new_founders = ", ".join([str(x).strip() for x in parsed if x])
            except Exception as e:
                print(f"[{sid}] Could not parse JSON for: {founders} - {e}")
                
        # Handle empty lists that became empty strings
        if not new_founders or str(new_founders).strip().lower() in ['none', 'unknown', '']:
             new_founders = 'Unknown'
             
        # Only update if changed
        if new_founders != founders:
             # Ensure correct native characters display instead of ASCII representation or unicode escapes
             cursor.execute("UPDATE startups SET founders = ? WHERE id = ?", (new_founders, sid))
             updated += 1
             print(f"[{sid}] Fixed: {founders} -> {new_founders}")
             
    conn.commit()
    conn.close()
    print(f"\\nFounders updated: {updated}")

if __name__ == '__main__':
    fix_founders()
