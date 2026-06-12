"""
Cowork Bridge — Interface between local pipeline and Cowork/Claude intelligence layer.

This module provides functions that the local pipeline (agent_news, agent_updater, etc.)
can call to queue items for Cowork review. Items are written to the `cowork_queue` table
in Master.db and processed when a Cowork session runs the ecosystem-review skill.

Architecture:
    Local Pipeline (runs continuously)     Cowork Session (runs on-demand)
    ├── agent_news.py                      ├── ecosystem-review skill
    ├── agent_updater.py          ──►      │   ├── Process quarantine
    ├── agent_ecosystem.py       queue     │   ├── Verify investors
    ├── agent_investors.py                 │   ├── Reclassify startups
    └── cowork_bridge.py (this file)       │   └── Detect anomalies
                                           └── ecosystem-weekly-report skill
                                               └── Generate weekly digest

Usage in pipeline modules:
    from cowork_bridge import escalate_classification, flag_investor, flag_news

    # When local LLM can't confidently classify a startup:
    escalate_classification(conn, startup_id, local_result, confidence=45)

    # When a new investor looks suspicious:
    flag_investor(conn, investor_id, reason="Name matches known tech company")

    # When a news item is in the gray zone:
    flag_news(conn, news_id, reason="ai_relevance=55, ambiguous content")
"""
import json
import sqlite3
from datetime import datetime


# ---------------------------------------------------------------------------
# Table setup
# ---------------------------------------------------------------------------

def ensure_tables(conn: sqlite3.Connection):
    """Create the cowork_queue table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cowork_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,          -- 'startup', 'investor', 'news', 'investment'
            entity_id INTEGER,                  -- FK to the relevant table
            action TEXT NOT NULL,               -- 'classify', 'verify', 'review', 'anomaly'
            reason TEXT,                        -- Why this was escalated
            local_result TEXT,                  -- JSON: what the local LLM produced
            priority INTEGER DEFAULT 50,        -- 0=low, 100=urgent
            status TEXT DEFAULT 'pending',      -- 'pending', 'in_progress', 'resolved', 'dismissed'
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            resolution TEXT                     -- JSON: what Cowork decided
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_cowork_queue_status
        ON cowork_queue(status, priority DESC)
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Escalation functions (called by local pipeline)
# ---------------------------------------------------------------------------

def escalate_classification(conn: sqlite3.Connection, startup_id: int,
                            local_result: dict, confidence: int = 50,
                            reason: str = None):
    """
    Queue a startup for Cowork classification review.
    Called when the local LLM's classification confidence is below threshold.
    """
    ensure_tables(conn)
    priority = max(0, 100 - confidence)  # Lower confidence = higher priority
    conn.execute(
        "INSERT INTO cowork_queue (entity_type, entity_id, action, reason, local_result, priority, created_at) "
        "VALUES (?, ?, 'classify', ?, ?, ?, ?)",
        (
            "startup", startup_id,
            reason or f"Classification confidence {confidence}% below threshold",
            json.dumps(local_result, ensure_ascii=False),
            priority,
            datetime.now().isoformat()
        )
    )
    conn.commit()


def flag_investor(conn: sqlite3.Connection, investor_id: int,
                  reason: str, priority: int = 60):
    """
    Flag an investor for Cowork verification.
    Called when investor data looks suspicious (name matches tech company,
    missing type, very low confidence, etc.)
    """
    ensure_tables(conn)
    conn.execute(
        "INSERT INTO cowork_queue (entity_type, entity_id, action, reason, priority, created_at) "
        "VALUES (?, ?, 'verify', ?, ?, ?)",
        ("investor", investor_id, reason, priority, datetime.now().isoformat())
    )
    conn.commit()


def flag_news(conn: sqlite3.Connection, news_id: int,
              reason: str, priority: int = 40):
    """
    Flag a news item for Cowork review.
    Called when a news article is in the ai_relevance gray zone (30-70)
    or has an empty summary that the local LLM couldn't generate.
    """
    ensure_tables(conn)
    conn.execute(
        "INSERT INTO cowork_queue (entity_type, entity_id, action, reason, priority, created_at) "
        "VALUES (?, ?, 'review', ?, ?, ?)",
        ("news", news_id, reason, priority, datetime.now().isoformat())
    )
    conn.commit()


def flag_anomaly(conn: sqlite3.Connection, entity_type: str, entity_id: int,
                 anomaly_description: str, priority: int = 70):
    """
    Flag a data anomaly for Cowork investigation.
    Called when automated checks detect suspicious patterns
    (e.g., website changed to spam domain, implausible funding).
    """
    ensure_tables(conn)
    conn.execute(
        "INSERT INTO cowork_queue (entity_type, entity_id, action, reason, priority, created_at) "
        "VALUES (?, ?, 'anomaly', ?, ?, ?)",
        (entity_type, entity_id, anomaly_description, priority, datetime.now().isoformat())
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Queue management (called by Cowork skills)
# ---------------------------------------------------------------------------

def get_pending_items(conn: sqlite3.Connection, entity_type: str = None,
                      action: str = None, limit: int = 20) -> list:
    """Fetch pending queue items, ordered by priority."""
    ensure_tables(conn)
    query = "SELECT * FROM cowork_queue WHERE status = 'pending'"
    params = []
    if entity_type:
        query += " AND entity_type = ?"
        params.append(entity_type)
    if action:
        query += " AND action = ?"
        params.append(action)
    query += " ORDER BY priority DESC, created_at ASC LIMIT ?"
    params.append(limit)

    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def resolve_item(conn: sqlite3.Connection, queue_id: int,
                 resolution: dict, status: str = "resolved"):
    """Mark a queue item as resolved with the Cowork decision."""
    conn.execute(
        "UPDATE cowork_queue SET status = ?, resolved_at = ?, resolution = ? WHERE id = ?",
        (status, datetime.now().isoformat(), json.dumps(resolution, ensure_ascii=False), queue_id)
    )
    conn.commit()


def get_queue_stats(conn: sqlite3.Connection) -> dict:
    """Get summary statistics for the queue."""
    ensure_tables(conn)
    stats = {}
    for status in ("pending", "in_progress", "resolved", "dismissed"):
        count = conn.execute(
            "SELECT COUNT(*) FROM cowork_queue WHERE status = ?", (status,)
        ).fetchone()[0]
        stats[status] = count

    # Breakdown by type
    breakdown = conn.execute(
        "SELECT entity_type, action, COUNT(*) as cnt "
        "FROM cowork_queue WHERE status = 'pending' "
        "GROUP BY entity_type, action ORDER BY cnt DESC"
    ).fetchall()
    stats["pending_breakdown"] = [
        {"type": r[0], "action": r[1], "count": r[2]} for r in breakdown
    ]
    return stats


# ---------------------------------------------------------------------------
# Automated anomaly detection (run periodically)
# ---------------------------------------------------------------------------

def detect_anomalies(conn: sqlite3.Connection, days: int = 7) -> list:
    """
    Scan recent changes for anomalies and auto-flag them.
    Returns list of anomalies found.
    """
    ensure_tables(conn)
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    anomalies = []

    # 1. Website changed to suspicious domain
    SPAM_INDICATORS = ["cheat", "hack", "casino", "bet", "porn", "forex", "crypto-bot"]
    website_changes = conn.execute(
        "SELECT entity_id, old_value, new_value FROM change_log "
        "WHERE changed_field = 'website' AND timestamp >= ? AND entity_type = 'startup'",
        (cutoff,)
    ).fetchall()
    for eid, old, new in website_changes:
        if new and any(s in new.lower() for s in SPAM_INDICATORS):
            flag_anomaly(conn, "startup", eid,
                         f"Website changed to suspicious domain: {new} (was: {old})",
                         priority=90)
            anomalies.append(("startup", eid, f"spam_url: {new}"))

    # 2. Implausible funding for early-stage startups
    recent_funding = conn.execute(
        "SELECT i.startup_id, i.amount_usd, i.round_type, s.company_name, s.founded_year "
        "FROM investments i "
        "JOIN startups s ON i.startup_id = s.id "
        "WHERE i.investment_date >= ? AND i.amount_usd IS NOT NULL",
        (cutoff,)
    ).fetchall()
    for sid, amount, rtype, name, year in recent_funding:
        if rtype in ("Seed", "Pre-Seed", "Angel") and amount and amount > 20_000_000:
            flag_anomaly(conn, "investment", sid,
                         f"{name}: {rtype} round of ${amount:,.0f} seems implausible",
                         priority=75)
            anomalies.append(("investment", sid, f"implausible_{rtype}_{amount}"))

    # 3. Classification instability (changed > 2 times in the period)
    unstable = conn.execute(
        "SELECT entity_id, COUNT(*) as changes FROM change_log "
        "WHERE changed_field = 'Category' AND timestamp >= ? AND entity_type = 'startup' "
        "GROUP BY entity_id HAVING changes > 2",
        (cutoff,)
    ).fetchall()
    for eid, count in unstable:
        flag_anomaly(conn, "startup", eid,
                     f"Category changed {count} times in {days} days — classification unstable",
                     priority=65)
        anomalies.append(("startup", eid, f"unstable_classification_{count}_changes"))

    # 4. Suspicious provenance: recently-added startup with a non-Turkish source URL
    # and either an unknown/empty city or a description that strongly implies a foreign origin.
    # Heuristic tuned after the 2026-04-20 ingest surfaced 4 false positives (Cerebras, Iren, Bing,
    # Nas.com) — all carried İstanbul or Unknown city tags but were clearly US/Australian entities.
    TURKISH_TLDS = (".tr", ".com.tr", ".org.tr", ".gov.tr", ".edu.tr", ".net.tr")
    TURKISH_CITIES = (
        "istanbul", "i̇stanbul", "ankara", "izmir", "i̇zmir", "bursa", "antalya",
        "konya", "adana", "gaziantep", "mersin", "kayseri", "eskişehir", "trabzon",
        "samsun", "diyarbakır", "denizli", "nevşehir",
    )
    FOREIGN_PROVENANCE_TOKENS = (
        "nasdaq:", "asx:", "nyse:", "lse:", "tsx:",
        "seeking alpha", "seekingalpha",
        "australian", "american", "indian company", "us-based", "uk-based",
        "headquartered in san", "headquartered in new york", "headquartered in london",
        "headquartered in sydney", "headquartered in boston",
        "silicon valley",
    )
    # A startup's `website` field should point at that company's own site. When it lands on a
    # known aggregator / review site / generic consumer platform, the ingest almost certainly
    # picked up a *mention* of the company, not the company itself. This is the single strongest
    # false-positive indicator observed in the 2026-04-20 ingest (Cerebras→baidu, Nas.com→zhihu,
    # Iren→seekingalpha, Bing→bing.com).
    NON_STARTUP_WEBSITE_HOSTS = (
        "bing.com", "google.com", "yahoo.com",
        "baidu.com", "zhihu.com",
        "wikipedia.org", "seekingalpha.com", "fool.com",
        "businessinsider.com", "forbes.com", "cnbc.com", "techcrunch.com",
        "bloomberg.com", "reuters.com", "wsj.com",
        "linkedin.com/in/", "twitter.com/", "x.com/", "facebook.com/",
        "youtube.com", "medium.com", "substack.com",
    )
    # Scope: startups touched in the window, either via change_log OR via a recently-scanned
    # news_mention. Fresh ingests often lack change_log entries (only the INSERT happened),
    # so relying on change_log alone misses the exact cases this rule exists to catch.
    recent_startups = conn.execute(
        "SELECT DISTINCT s.id, s.company_name, s.city, s.website, s.description, "
        "       (SELECT source_url FROM news_mentions n2 WHERE n2.startup_id = s.id "
        "        ORDER BY n2.scanned_at DESC LIMIT 1) AS src_url "
        "FROM startups s "
        "WHERE s.id IN ("
        "    SELECT entity_id FROM change_log "
        "     WHERE entity_type = 'startup' AND timestamp >= ?"
        ") OR s.id IN ("
        "    SELECT startup_id FROM news_mentions "
        "     WHERE startup_id IS NOT NULL AND scanned_at >= ?"
        ")",
        (cutoff, cutoff)
    ).fetchall()
    for sid, name, city, website, desc, src_url in recent_startups:
        city_norm = (city or "").strip().lower()
        desc_norm = (desc or "").lower()
        url_norm = (src_url or "").lower()
        site_norm = (website or "").lower()
        reasons = []
        # (a) Website is on a known non-startup / aggregator host — strongest single signal.
        if site_norm and any(host in site_norm for host in NON_STARTUP_WEBSITE_HOSTS):
            reasons.append(f"website points at a known non-company host: {website}")
        # (b) Source URL not on a Turkish TLD and not a Turkish-language aggregator.
        if url_norm and not any(t in url_norm for t in TURKISH_TLDS):
            if not any(k in url_norm for k in ("webrazzi", "donanimhaber", "techinside", "egirisim")):
                reasons.append(f"source URL not on a Turkish TLD: {src_url}")
        # (c) City is missing / Unknown.
        if not city_norm or city_norm in ("unknown", "n/a", "none", ""):
            reasons.append(f"city missing or Unknown ('{city}')")
        # (d) Description contains an unambiguous foreign-provenance marker.
        foreign_tokens_found = [t for t in FOREIGN_PROVENANCE_TOKENS if t in desc_norm]
        if foreign_tokens_found:
            reasons.append(f"foreign-provenance markers in description: {foreign_tokens_found}")
        # (e) City tagged Turkish but description/website implies foreign origin — direct contradiction.
        if any(tc in city_norm for tc in TURKISH_CITIES) and (foreign_tokens_found or
             any(host in site_norm for host in NON_STARTUP_WEBSITE_HOSTS)):
            reasons.append("Turkish city tag contradicts website/description provenance")
        # Flag if the website-host signal fires (strong enough on its own), or if ≥2 weaker signals stack.
        site_is_non_startup = bool(site_norm and any(host in site_norm for host in NON_STARTUP_WEBSITE_HOSTS))
        if site_is_non_startup or len(reasons) >= 2:
            flag_anomaly(conn, "startup", sid,
                         f"{name}: possible non-Turkish false positive — " + "; ".join(reasons),
                         priority=70)
            anomalies.append(("startup", sid, f"suspicious_provenance: {name}"))

    return anomalies


# ---------------------------------------------------------------------------
# CLI for manual use
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Cowork Bridge — Queue Manager")
    parser.add_argument("--stats", action="store_true", help="Show queue statistics")
    parser.add_argument("--pending", action="store_true", help="List pending items")
    parser.add_argument("--detect", action="store_true", help="Run anomaly detection")
    parser.add_argument("--type", help="Filter by entity type")
    parser.add_argument("--days", type=int, default=7, help="Lookback days for anomaly detection")
    args = parser.parse_args()

    conn = sqlite3.connect("Master.db")

    if args.stats:
        stats = get_queue_stats(conn)
        print(f"\nCowork Queue Stats:")
        print(f"  Pending:     {stats['pending']}")
        print(f"  In Progress: {stats['in_progress']}")
        print(f"  Resolved:    {stats['resolved']}")
        print(f"  Dismissed:   {stats['dismissed']}")
        if stats["pending_breakdown"]:
            print(f"\n  Pending breakdown:")
            for item in stats["pending_breakdown"]:
                print(f"    {item['type']}/{item['action']}: {item['count']}")

    elif args.pending:
        items = get_pending_items(conn, entity_type=args.type)
        if not items:
            print("No pending items.")
        else:
            print(f"\n{len(items)} pending items:")
            for item in items:
                print(f"  [{item['id']}] {item['entity_type']}/{item['action']} "
                      f"(priority={item['priority']}) entity_id={item['entity_id']}")
                print(f"       {item['reason'][:100]}")
                print()

    elif args.detect:
        anomalies = detect_anomalies(conn, days=args.days)
        print(f"\nDetected {len(anomalies)} anomalies in last {args.days} days.")
        for atype, aid, desc in anomalies:
            print(f"  {atype} #{aid}: {desc}")

    else:
        parser.print_help()

    conn.close()
