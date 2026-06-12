#!/usr/bin/env python3
"""Generate the Turkey AI Ecosystem Dashboard (index.html) from Master.db."""

import json
import sqlite3
from collections import Counter
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "Master.db"
OUT_PATH = Path(__file__).resolve().parent / "index.html"

BASE_FILTER = "ai_explanation IS NOT NULL AND ai_explanation != ''"


def query(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def get_sectors(conn):
    rows = query(conn, f"""
        SELECT CASE WHEN Category LIKE '%|%'
                    THEN TRIM(SUBSTR(Category, 1, INSTR(Category, '|') - 1))
                    ELSE Category END AS sector,
               COUNT(*) AS count
        FROM startups WHERE {BASE_FILTER}
        GROUP BY sector ORDER BY count DESC
    """)
    return [{"sector": r[0] or "General", "count": r[1]} for r in rows]


def get_subcategories(conn):
    rows = query(conn, f"""
        SELECT CASE WHEN Category LIKE '%|%'
                    THEN TRIM(SUBSTR(Category, INSTR(Category, '|') + 1))
                    ELSE 'General' END AS subcat,
               COUNT(*) AS count
        FROM startups WHERE {BASE_FILTER}
        GROUP BY subcat ORDER BY count DESC LIMIT 15
    """)
    return [{"subcat": r[0], "count": r[1]} for r in rows]


def get_cities(conn):
    rows = query(conn, f"""
        SELECT city, COUNT(*) AS cnt
        FROM startups
        WHERE {BASE_FILTER} AND city IS NOT NULL AND city != ''
        GROUP BY city ORDER BY cnt DESC LIMIT 20
    """)
    return [{"city": r[0], "cnt": r[1]} for r in rows]


def get_years(conn):
    rows = query(conn, f"""
        SELECT founded_year, COUNT(*) AS cnt
        FROM startups
        WHERE {BASE_FILTER} AND founded_year IS NOT NULL
        GROUP BY founded_year ORDER BY founded_year
    """)
    return [{"year": r[0], "cnt": r[1]} for r in rows]


def get_bmodels(conn):
    rows = query(conn, f"""
        SELECT business_model, COUNT(*) AS cnt
        FROM startups WHERE {BASE_FILTER}
        GROUP BY business_model ORDER BY cnt DESC
    """)
    return [{"business_model": r[0] or "Unknown", "cnt": r[1]} for r in rows]


def get_ai_buckets(conn):
    rows = query(conn, f"""
        SELECT CASE
            WHEN tech_proximity >= 90 THEN 'Core AI (90-100%)'
            WHEN tech_proximity >= 70 THEN 'AI-Heavy (70-89%)'
            WHEN tech_proximity >= 50 THEN 'AI-Enabled (50-69%)'
            WHEN tech_proximity >= 30 THEN 'AI-Adjacent (30-49%)'
            ELSE 'Low AI (<30%)'
        END AS bucket, COUNT(*) AS cnt
        FROM startups WHERE {BASE_FILTER}
        GROUP BY bucket
    """)
    order = ['Core AI (90-100%)', 'AI-Heavy (70-89%)', 'AI-Enabled (50-69%)',
             'AI-Adjacent (30-49%)', 'Low AI (<30%)']
    lookup = {r[0]: r[1] for r in rows}
    return [{"bucket": b, "cnt": lookup.get(b, 0)} for b in order]


def get_ai_proximity_dist(conn):
    rows = query(conn, f"""
        SELECT tech_proximity AS score, COUNT(*) AS count
        FROM startups WHERE {BASE_FILTER}
        GROUP BY score ORDER BY score
    """)
    return [{"score": r[0], "count": r[1]} for r in rows]


def get_cloud_relevance(conn):
    rows = query(conn, f"""
        SELECT COALESCE(cloud_relevance, 'Unclassified') AS label, COUNT(*) AS count
        FROM startups WHERE {BASE_FILTER}
        GROUP BY label ORDER BY count DESC
    """)
    return [{"label": r[0], "count": r[1]} for r in rows]


def get_data_confidence(conn):
    rows = query(conn, f"""
        SELECT CASE
            WHEN data_confidence >= 80 THEN 'High (80-100)'
            WHEN data_confidence >= 60 THEN 'Good (60-79)'
            WHEN data_confidence >= 40 THEN 'Medium (40-59)'
            WHEN data_confidence >= 20 THEN 'Low (20-39)'
            ELSE 'Very Low (0-19)'
        END AS bucket, COUNT(*) AS cnt
        FROM startups WHERE {BASE_FILTER}
        GROUP BY bucket
    """)
    order = ['High (80-100)', 'Good (60-79)', 'Medium (40-59)', 'Low (20-39)', 'Very Low (0-19)']
    lookup = {r[0]: r[1] for r in rows}
    return [{"bucket": b, "cnt": lookup.get(b, 0)} for b in order]


def get_top_investors(conn):
    turkey_location_keywords = [
        "turkey", "türkiye", "istanbul", "ankara", "izmir", "bursa",
        "adana", "antalya", "besiktas", "beşiktaş", "cankaya",
        "çankaya", "eskisehir", "eskişehir", "kocaeli",
    ]
    location_filter = " OR ".join(["LOWER(COALESCE(i.location, '')) LIKE ?"] * len(turkey_location_keywords))
    location_params = tuple(f"%{kw}%" for kw in turkey_location_keywords)

    rows = query(conn, f"""
        SELECT
            i.investor_name,
            i.investor_type,
            i.location,
            SUM(CASE WHEN inv.startup_id IS NOT NULL THEN 1 ELSE 0 END) AS deal_count
        FROM investors i
        LEFT JOIN investments inv ON i.investor_id = inv.investor_id
        WHERE ({location_filter})
        GROUP BY i.investor_id
        ORDER BY deal_count DESC, i.investor_name ASC
    """, location_params)
    return [{"investor_name": r[0], "investor_type": r[1] or "N/A",
             "location": r[2] or "Unknown", "deal_count": r[3]} for r in rows]


def get_round_types(conn, valid_deal_ids):
    if not valid_deal_ids:
        return []
    placeholders = ",".join("?" for _ in valid_deal_ids)
    rows = query(conn, f"""
        SELECT round_type, COUNT(*) AS cnt
        FROM investments
        WHERE id IN ({placeholders})
          AND round_type IS NOT NULL
          AND TRIM(round_type) != ''
          AND UPPER(TRIM(round_type)) != 'NA'
        GROUP BY round_type ORDER BY cnt DESC
    """, tuple(valid_deal_ids))
    return [{"round_type": r[0], "cnt": r[1]} for r in rows]


def get_deal_timeline(conn, valid_deal_ids):
    if not valid_deal_ids:
        return []
    placeholders = ",".join("?" for _ in valid_deal_ids)
    rows = query(conn, f"""
        SELECT SUBSTR(TRIM(investment_date), 1, 4) AS year, COUNT(*) AS cnt
        FROM investments
        WHERE id IN ({placeholders})
          AND investment_date IS NOT NULL
          AND TRIM(investment_date) GLOB '[0-9][0-9][0-9][0-9]*'
        GROUP BY year ORDER BY year
    """, tuple(valid_deal_ids))
    return [{"year": r[0], "cnt": r[1]} for r in rows]


def get_valid_deal_ids(conn):
    rows = query(conn, """
        SELECT id
        FROM investments
        WHERE investment_date IS NOT NULL
          AND TRIM(investment_date) GLOB '[0-9][0-9][0-9][0-9]*'
          AND round_type IS NOT NULL
          AND TRIM(round_type) != ''
          AND UPPER(TRIM(round_type)) != 'NA'
    """)
    return [r[0] for r in rows]


def get_tech_list(conn):
    rows = query(conn, f"""
        SELECT Tech_Mentioned FROM startups
        WHERE {BASE_FILTER} AND Tech_Mentioned IS NOT NULL
              AND Tech_Mentioned != 'None' AND Tech_Mentioned != ''
    """)
    counter = Counter()
    for (techs,) in rows:
        for t in techs.split(","):
            t = t.strip()
            if t and t != "None":
                counter[t] += 1
    top = counter.most_common(15)
    return [{"tech": t, "count": c} for t, c in top]


def get_all_startups(conn):
    cols = [
        "id", "company_name", "website", "crunchbase_url", "description",
        "city", "founders", "founded_year", "legal_business_name",
        "tech_proximity", "Status", "AI_Use_Case",
        "Total_Funding_Formatted", "Investors", "Tech_Mentioned",
        "Tech_Assumed", "data_confidence", "business_model",
        "ai_explanation", "Category", "cloud_relevance",
    ]
    rows = query(conn, f"""
        SELECT {', '.join(cols)} FROM startups WHERE {BASE_FILTER}
        ORDER BY tech_proximity DESC, company_name ASC
    """)
    return [dict(zip(cols, r)) for r in rows]


def get_kpis(conn, sectors, cities, years, valid_deal_ids):
    total = query(conn, f"SELECT COUNT(*) FROM startups WHERE {BASE_FILTER}")[0][0]
    avg_ai = query(conn, f"SELECT AVG(tech_proximity) FROM startups WHERE {BASE_FILTER}")[0][0]
    product_pct = query(conn, f"""
        SELECT ROUND(100.0 * SUM(CASE WHEN business_model='Product' THEN 1 ELSE 0 END) / COUNT(*), 1)
        FROM startups WHERE {BASE_FILTER}
    """)[0][0]
    core_ai = query(conn, f"""
        SELECT COUNT(*) FROM startups WHERE {BASE_FILTER} AND tech_proximity >= 90
    """)[0][0]
    if valid_deal_ids:
        placeholders = ",".join("?" for _ in valid_deal_ids)
        investor_count = query(
            conn,
            f"SELECT COUNT(DISTINCT investor_id) FROM investments WHERE id IN ({placeholders})",
            tuple(valid_deal_ids),
        )[0][0]
        total_deals = query(
            conn,
            f"SELECT COUNT(*) FROM investments WHERE id IN ({placeholders})",
            tuple(valid_deal_ids),
        )[0][0]
    else:
        investor_count = 0
        total_deals = 0
    city_count = query(conn, f"""
        SELECT COUNT(DISTINCT city) FROM startups
        WHERE {BASE_FILTER} AND city IS NOT NULL AND city != ''
    """)[0][0]
    top_city = cities[0] if cities else {"city": "N/A", "cnt": 0}
    top_city_pct = round(100 * top_city["cnt"] / total, 0) if total else 0
    recent = sum(y["cnt"] for y in years if y["year"] and y["year"] >= 2020)
    recent_pct = round(100 * recent / total, 0) if total else 0
    return {
        "total": total,
        "avg_ai": round(avg_ai, 1) if avg_ai else 0,
        "product_pct": product_pct or 0,
        "core_ai": core_ai,
        "investor_count": investor_count,
        "total_deals": total_deals,
        "city_count": city_count,
        "top_city": top_city["city"],
        "top_city_pct": int(top_city_pct),
        "recent": recent,
        "recent_pct": int(recent_pct),
    }


def js_const(name, data):
    return f"const {name} = {json.dumps(data, ensure_ascii=False)};"


def main():
    conn = sqlite3.connect(str(DB_PATH))
    print(f"Connected to {DB_PATH}")
    sectors = get_sectors(conn)
    subcategories = get_subcategories(conn)
    cities = get_cities(conn)
    years = get_years(conn)
    bmodels = get_bmodels(conn)
    ai_buckets = get_ai_buckets(conn)
    ai_proximity_dist = get_ai_proximity_dist(conn)
    cloud_relevance = get_cloud_relevance(conn)
    data_confidence = get_data_confidence(conn)
    valid_deal_ids = get_valid_deal_ids(conn)
    top_investors = get_top_investors(conn)
    round_types = get_round_types(conn, valid_deal_ids)
    deal_timeline = get_deal_timeline(conn, valid_deal_ids)
    tech_list = get_tech_list(conn)
    all_startups = get_all_startups(conn)
    kpis = get_kpis(conn, sectors, cities, years, valid_deal_ids)
    conn.close()
    data = {
        "sectors": sectors, "subcategories": subcategories, "cities": cities,
        "years": years, "bmodels": bmodels, "ai_buckets": ai_buckets,
        "ai_proximity_dist": ai_proximity_dist, "cloud_relevance": cloud_relevance,
        "data_confidence": data_confidence, "top_investors": top_investors,
        "round_types": round_types, "deal_timeline": deal_timeline,
        "tech_list": tech_list, "all_startups": all_startups, "kpis": kpis,
    }
    html = generate_html(data)
    OUT_PATH.write_text(html, encoding="utf-8")
    size_kb = OUT_PATH.stat().st_size / 1024
    print(f"Generated {OUT_PATH} ({size_kb:.0f} KB)")
    print(f"  Companies: {kpis['total']}")
    print(f"  Startups in directory: {len(all_startups)}")
    print(f"  Data constants: 13")

if __name__ == "__main__":
    main()
