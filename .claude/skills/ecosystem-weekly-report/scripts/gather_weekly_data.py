"""
Gather data from Master.db for the weekly ecosystem report.
Usage: python3 gather_weekly_data.py [--days 7] [--json]
Outputs structured data for the weekly report skill to analyze.
"""
import sqlite3
import json
import argparse
from datetime import datetime, timedelta
from pathlib import Path

DB = Path(__file__).resolve().parents[4] / "Master.db"


def gather(days=7, as_json=False):
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    data = {}

    # --- News Mentions ---
    news = conn.execute("""
        SELECT nm.id, nm.headline, nm.summary, nm.source_name, nm.source_url,
               nm.published_date, nm.ai_relevance, nm.event_type, nm.event_category,
               s.company_name, s.Category as startup_category,
               inv.investor_name
        FROM news_mentions nm
        LEFT JOIN startups s ON nm.startup_id = s.id
        LEFT JOIN investors inv ON nm.investor_id = inv.investor_id
        WHERE nm.is_relevant = 1
          AND (nm.scanned_at >= ? OR nm.published_date >= ?)
        ORDER BY nm.published_date DESC
    """, (cutoff, cutoff)).fetchall()
    data["news"] = [dict(n) for n in news]

    # --- Investments ---
    investments = conn.execute("""
        SELECT i.id, i.round_type, i.amount, i.currency, i.amount_usd,
               i.investment_date, i.source_url,
               s.company_name, s.Category as startup_category, s.city,
               inv.investor_name, inv.investor_type, inv.investor_origin
        FROM investments i
        JOIN startups s ON i.startup_id = s.id
        LEFT JOIN investors inv ON i.investor_id = inv.investor_id
        WHERE i.investment_date >= ?
           OR i.id IN (
               SELECT MAX(id) FROM investments
               GROUP BY startup_id, investor_id
               HAVING MAX(id) IN (SELECT id FROM investments WHERE rowid > (SELECT MAX(rowid) - 200 FROM investments))
           )
        ORDER BY i.amount_usd DESC NULLS LAST
    """, (cutoff,)).fetchall()
    data["investments"] = [dict(i) for i in investments]

    # --- New Startups ---
    new_startups = conn.execute("""
        SELECT id, company_name, website, description, city, Category,
               AI_Use_Case, data_confidence, founded_year, business_model
        FROM startups
        WHERE processed_at >= ?
        ORDER BY data_confidence DESC
    """, (cutoff,)).fetchall()
    data["new_startups"] = [dict(s) for s in new_startups]

    # --- Ecosystem Activities ---
    activities = conn.execute("""
        SELECT ea.id, ea.activity_type, ea.headline, ea.description as activity_desc,
               ea.importance, ea.activity_date, ea.source_url,
               ee.entity_name, ee.entity_type
        FROM ecosystem_activities ea
        LEFT JOIN ecosystem_entities ee ON ea.entity_id = ee.id
        WHERE ea.activity_date >= ? OR ea.scanned_at >= ?
        ORDER BY ea.activity_date DESC
    """, (cutoff, cutoff)).fetchall()
    data["activities"] = [dict(a) for a in activities]

    # --- Data Quality Metrics ---
    metrics = {}
    metrics["total_startups"] = conn.execute("SELECT COUNT(*) FROM startups").fetchone()[0]
    metrics["total_investors"] = conn.execute(
        "SELECT COUNT(*) FROM investors WHERE status IS NULL OR status = 'active'"
    ).fetchone()[0]
    metrics["total_investments"] = conn.execute("SELECT COUNT(*) FROM investments").fetchone()[0]
    metrics["total_news"] = conn.execute("SELECT COUNT(*) FROM news_mentions").fetchone()[0]

    try:
        metrics["quarantine_pending"] = conn.execute(
            "SELECT COUNT(*) FROM quarantine WHERE reviewed = 0"
        ).fetchone()[0]
    except Exception:
        metrics["quarantine_pending"] = 0

    metrics["uncategorized_startups"] = conn.execute(
        "SELECT COUNT(*) FROM startups WHERE Category = 'General | Uncategorized' OR Category IS NULL"
    ).fetchone()[0]
    metrics["low_confidence_startups"] = conn.execute(
        "SELECT COUNT(*) FROM startups WHERE data_confidence < 30"
    ).fetchone()[0]

    # Recent changes
    metrics["changes_this_period"] = conn.execute(
        "SELECT COUNT(*) FROM change_log WHERE timestamp >= ?", (cutoff,)
    ).fetchone()[0]

    data["quality_metrics"] = metrics

    # --- Category Distribution of New Activity ---
    sector_activity = conn.execute("""
        SELECT s.Category, COUNT(DISTINCT nm.id) as news_count,
               COUNT(DISTINCT i.id) as investment_count
        FROM startups s
        LEFT JOIN news_mentions nm ON nm.startup_id = s.id AND nm.scanned_at >= ?
        LEFT JOIN investments i ON i.startup_id = s.id AND i.investment_date >= ?
        WHERE s.Category IS NOT NULL
        GROUP BY s.Category
        HAVING news_count > 0 OR investment_count > 0
        ORDER BY news_count + investment_count DESC
    """, (cutoff, cutoff)).fetchall()
    data["sector_activity"] = [dict(s) for s in sector_activity]

    conn.close()

    # --- Summary ---
    data["period"] = {"start": cutoff, "end": datetime.now().strftime("%Y-%m-%d"), "days": days}

    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"Period: {data['period']['start']} to {data['period']['end']}")
        print(f"News mentions:      {len(data['news'])}")
        print(f"Investments:        {len(data['investments'])}")
        print(f"New startups:       {len(data['new_startups'])}")
        print(f"Ecosystem events:   {len(data['activities'])}")
        print(f"Active sectors:     {len(data['sector_activity'])}")
        print(f"\nQuality: {data['quality_metrics']}")
        print(f"\nTop news:")
        for n in data["news"][:5]:
            print(f"  - {n['headline'][:80]}")
            if n.get("company_name"):
                print(f"    Company: {n['company_name']}")

    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    gather(days=args.days, as_json=args.json)
