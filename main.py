"""
Ecosystem Agent — Unified CLI Entry Point
Usage:
    python3 main.py --module 1    # Run Module 1 (news scanner)
    python3 main.py --module 2    # Run Module 2 (updater/gatekeeper)
    python3 main.py --module 3    # Run Module 3 (ecosystem entity scanner)
    python3 main.py --module 4    # Run Module 4 (investor enrichment)
    python3 main.py --all         # Run M1 → M2 → M3 → M4 + publish dashboard
    python3 main.py --update      # Alias for Module 2 (cron use)
    python3 main.py --publish     # Generate dashboard + deploy to GitHub Pages
    python3 main.py --purge       # DB purge (remove non-AI/stale)
    python3 main.py --report --name "Aselsan"  # Company research report
"""
import argparse
import subprocess
import sys

MODULES = {
    1: "agent_news.py",
    2: "agent_updater.py",
    3: "agent_ecosystem.py",
    4: "agent_investors.py",
}

def run_module(module_num):
    script = MODULES.get(module_num)
    if not script:
        print(f"Error: Module {module_num} not found.")
        sys.exit(1)
    print(f"\n{'='*60}")
    print(f"  Running Module {module_num}: {script}")
    print(f"{'='*60}\n")
    result = subprocess.run([sys.executable, script])
    if result.returncode != 0:
        print(f"\nModule {module_num} exited with error (code {result.returncode}).")
        sys.exit(result.returncode)
    print(f"\nModule {module_num} completed successfully.")

def run_publish():
    """Generate fresh dashboard from DB and deploy to GitHub Pages."""
    print(f"\n{'='*60}")
    print(f"  Publishing Dashboard to GitHub Pages")
    print(f"{'='*60}\n")
    from update_dashboard import publish_fresh_dashboard
    url = publish_fresh_dashboard()
    if not url:
        print("\nDashboard publish failed.")
        sys.exit(1)
    print(f"\nDashboard published successfully: {url}")

def run_purge():
    print(f"\n{'='*60}")
    print(f"  Running DB Purge")
    print(f"{'='*60}\n")
    result = subprocess.run([sys.executable, "util_db_purge.py"])
    if result.returncode != 0:
        print(f"\nPurge exited with error (code {result.returncode}).")
        sys.exit(result.returncode)
    print(f"\nDB Purge completed successfully.")

def run_report(args):
    """Run company research report with passthrough flags."""
    cmd = [sys.executable, "util_report.py"]
    if args.report_name:
        cmd += ["--name", args.report_name]
    if args.report_domain:
        cmd += ["--domain", args.report_domain]
    if args.report_fresh:
        cmd.append("--fresh")
    if args.report_update_db:
        cmd.append("--update-db")
    if args.report_format:
        cmd += ["--format", args.report_format]
    if args.report_quick:
        cmd.append("--report")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(result.returncode)

def run_health():
    """Show data quality health dashboard."""
    import sqlite3
    from datetime import datetime
    conn = sqlite3.connect("Master.db")

    print(f"\n{'='*60}")
    print(f"  Data Quality Health Report — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    # Counts
    startups = conn.execute("SELECT COUNT(*) FROM startups").fetchone()[0]
    investors = conn.execute("SELECT COUNT(*) FROM investors WHERE status IS NULL OR status = 'active'").fetchone()[0]
    investments = conn.execute("SELECT COUNT(*) FROM investments").fetchone()[0]
    news = conn.execute("SELECT COUNT(*) FROM news_mentions").fetchone()[0]

    print(f"\n  Entities:  {startups} startups | {investors} investors | {investments} investments | {news} news")

    # Quality metrics
    uncategorized = conn.execute(
        "SELECT COUNT(*) FROM startups WHERE Category = 'General | Uncategorized' OR Category IS NULL"
    ).fetchone()[0]
    unknown_currency = conn.execute(
        "SELECT COUNT(*) FROM investments WHERE currency = 'Unknown' OR currency IS NULL"
    ).fetchone()[0]
    empty_summaries = conn.execute(
        "SELECT COUNT(*) FROM news_mentions WHERE is_relevant = 1 AND (summary IS NULL OR summary = '')"
    ).fetchone()[0]
    orphaned_news = conn.execute(
        "SELECT COUNT(*) FROM news_mentions WHERE startup_id IS NULL AND investor_id IS NULL"
    ).fetchone()[0]
    quarantine_count = conn.execute(
        "SELECT COUNT(*) FROM quarantine WHERE reviewed = 0"
    ).fetchone()[0] if conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='quarantine'"
    ).fetchone() else 0

    print(f"\n  Quality:")
    print(f"    Uncategorized startups:  {uncategorized}/{startups} ({uncategorized*100//max(startups,1)}%)")
    print(f"    Unknown currency:        {unknown_currency}/{investments} ({unknown_currency*100//max(investments,1)}%)")
    print(f"    Empty news summaries:    {empty_summaries}/{news}")
    print(f"    Orphaned news:           {orphaned_news}")
    print(f"    Quarantine queue:        {quarantine_count}")

    # Last runs
    print(f"\n  Last pipeline runs:")
    runs = conn.execute(
        "SELECT module_name, MAX(scan_end) as last_run, SUM(items_found) as found, SUM(items_updated) as updated "
        "FROM scan_log GROUP BY module_name ORDER BY last_run DESC LIMIT 5"
    ).fetchall()
    for module, last_run, found, updated in runs:
        print(f"    {module:<20s} {last_run or 'never':<22s} found={found or 0} updated={updated or 0}")

    conn.close()

    # Run data quality tests
    print(f"\n  Running quality assertions...")
    result = subprocess.run([sys.executable, "tests/test_data_quality.py"], capture_output=True, text=True)
    for line in result.stdout.strip().split("\n"):
        if line.startswith("  "):
            print(f"  {line}")
    print(f"\n{'='*60}")


def run_refresh_keywords():
    """Force-refresh tag seed keywords."""
    from agent_core import get_db_connection, seed_tag_keywords
    conn = get_db_connection()
    seed_tag_keywords(conn, force_refresh=True)
    conn.close()
    print("Tag keywords refreshed.")


def main():
    parser = argparse.ArgumentParser(description="Ecosystem Agent")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--module", type=int, choices=[1, 2, 3, 4], help="Run a specific module")
    group.add_argument("--all", action="store_true", help="Run M1 → M2 → M3 → M4 sequentially")
    group.add_argument("--update", action="store_true", help="Run Module 2 (Unified gatekeeper/updater)")
    group.add_argument("--publish", action="store_true", help="Generate dashboard + deploy to Netlify")
    group.add_argument("--purge", action="store_true", help="Remove non-AI/stale startups from DB")
    group.add_argument("--report", action="store_true", help="Company research report")
    group.add_argument("--health", action="store_true", help="Show data quality health dashboard")
    group.add_argument("--refresh-keywords", action="store_true", help="Force-refresh tag seed keywords")

    # Report sub-flags
    parser.add_argument("--name", dest="report_name", help="Company name (for --report)")
    parser.add_argument("--domain", dest="report_domain", help="Company domain (for --report)")
    parser.add_argument("--fresh", dest="report_fresh", action="store_true", help="Skip cache (for --report)")
    parser.add_argument("--update-db", dest="report_update_db", action="store_true", help="Write to DB (for --report)")
    parser.add_argument("--format", dest="report_format", choices=['md', 'json', 'terminal'], help="Report format")
    parser.add_argument("--quick", dest="report_quick", action="store_true", help="DB-only report, no online research")

    # Module 4 sub-flags
    parser.add_argument("--investor-name", dest="investor_name", help="Specific investor name (for --module 4)")
    parser.add_argument("--batch", type=int, help="Batch size (for --module 4)")
    parser.add_argument("--type", dest="investor_type", help="Investor type filter (for --module 4)")

    args = parser.parse_args()

    if args.module:
        run_module(args.module)
    elif args.all:
        for m in [1, 2, 3, 4]:
            run_module(m)
        # Auto-publish dashboard as final step
        run_publish()
    elif args.update:
        run_module(2)
    elif args.publish:
        run_publish()
    elif args.purge:
        run_purge()
    elif args.report:
        if not args.report_name and not args.report_domain:
            parser.error("--report requires --name and/or --domain")
        run_report(args)
    elif args.health:
        run_health()
    elif args.refresh_keywords:
        run_refresh_keywords()

if __name__ == "__main__":
    main()
