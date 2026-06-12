#!/usr/bin/env python3
"""
update_dashboard.py
────────────────────────────────────────────────────────────────
Refreshes the Turkey AI Ecosystem dashboard with the latest data
from Master.db, then deploys it to GitHub Pages.

The dashboard is published to the `gh-pages` branch of this repo.
GitHub Pages serves the index.html from that branch.

Setup (one-time):
  1. Enable GitHub Pages in repo settings:
     https://github.com/yashark/ai-ecosystem-search/settings/pages
     → Source: "Deploy from a branch"
     → Branch: gh-pages / root
  2. Ensure you have push access (SSH key or HTTPS credentials)

Run:
  python3 update_dashboard.py           # Generate + deploy to GitHub Pages
  python3 update_dashboard.py --local   # Generate only (no push)
"""

import sqlite3
import json
import re
import os
import sys
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

# ── CONFIG ───────────────────────────────────────────────────────────────────
DB_PATH      = Path(__file__).resolve().parent / "Master.db"
HTML_PATH    = Path(__file__).resolve().parent / "turkey_ai_ecosystem_dashboard.html"
DEPLOY_DIR   = Path(__file__).resolve().parent / "deploy"
REPO_DIR     = Path(__file__).resolve().parent

GH_PAGES_BRANCH = "gh-pages"
# ─────────────────────────────────────────────────────────────────────────────


def extract_data(db_path: Path) -> dict:
    """Query Master.db and return all dashboard data."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    print("  Querying Master.db …")

    # ── Sectors (short name before '|') ─────────────────────────────────────
    c.execute("""
        SELECT SUBSTR(Category, 1, INSTR(Category||'|','|')-1) AS sector,
               COUNT(*) AS count
        FROM startups
        WHERE Status='Active' AND Category IS NOT NULL
        GROUP BY sector ORDER BY count DESC
    """)
    sectors = [{"sector": r["sector"], "count": r["count"]} for r in c.fetchall()]

    # ── Subcategories (part after '|') ───────────────────────────────────────
    c.execute("""
        SELECT TRIM(SUBSTR(Category, INSTR(Category,'|')+1)) AS subcat,
               COUNT(*) AS count
        FROM startups
        WHERE Status='Active' AND Category LIKE '%|%'
        GROUP BY subcat ORDER BY count DESC LIMIT 15
    """)
    subcategories = [{"subcat": r["subcat"], "count": r["count"]} for r in c.fetchall()]

    # ── Cities (Turkish only) ────────────────────────────────────────────────
    c.execute("""
        SELECT city, COUNT(*) AS cnt
        FROM startups
        WHERE Status='Active' AND city NOT IN ('','Unknown','Foreign')
        GROUP BY city ORDER BY cnt DESC LIMIT 20
    """)
    cities = [{"city": r["city"], "cnt": r["cnt"]} for r in c.fetchall()]

    # ── AI Proximity buckets ──────────────────────────────────────────────────
    c.execute("SELECT tech_proximity FROM startups WHERE Status='Active' AND tech_proximity IS NOT NULL")
    prox = [row[0] for row in c.fetchall()]
    buckets = {"0-20": 0, "21-40": 0, "41-60": 0, "61-80": 0, "81-100": 0}
    for p in prox:
        if   p <= 20: buckets["0-20"]   += 1
        elif p <= 40: buckets["21-40"]  += 1
        elif p <= 60: buckets["41-60"]  += 1
        elif p <= 80: buckets["61-80"]  += 1
        else:         buckets["81-100"] += 1
    ai_buckets = [{"range": k, "count": v} for k, v in buckets.items()]

    # ── Per-score distribution (for histogram) ───────────────────────────────
    c.execute("""
        SELECT tech_proximity AS score, COUNT(*) AS count
        FROM startups
        WHERE Status='Active' AND tech_proximity IS NOT NULL
        GROUP BY score ORDER BY score
    """)
    ai_proximity_dist = [{"score": r["score"], "count": r["count"]} for r in c.fetchall()]

    # ── Business models ───────────────────────────────────────────────────────
    c.execute("""
        SELECT business_model, COUNT(*) AS count
        FROM startups WHERE Status='Active'
        GROUP BY business_model ORDER BY count DESC
    """)
    bmodels = [{"model": r["business_model"], "count": r["count"]} for r in c.fetchall()]

    # ── Years founded ─────────────────────────────────────────────────────────
    c.execute("""
        SELECT founded_year, COUNT(*) AS count
        FROM startups
        WHERE Status='Active' AND founded_year >= 2010
        GROUP BY founded_year ORDER BY founded_year
    """)
    years = [{"year": r["founded_year"], "count": r["count"]} for r in c.fetchall()]

    # ── Cloud relevance ───────────────────────────────────────────────────────
    c.execute("""
        SELECT COALESCE(cloud_relevance,'Unclassified') AS label, COUNT(*) AS count
        FROM startups WHERE Status='Active'
        GROUP BY label ORDER BY count DESC
    """)
    cloud_relevance = [{"label": r["label"], "count": r["count"]} for r in c.fetchall()]

    # ── Top investors ─────────────────────────────────────────────────────────
    c.execute("""
        SELECT Investors AS name, COUNT(*) AS deals
        FROM startups
        WHERE Status='Active' AND Investors IS NOT NULL AND Investors != ''
        GROUP BY Investors ORDER BY deals DESC LIMIT 20
    """)
    top_investors = [{"name": r["name"], "deals": r["deals"]} for r in c.fetchall()]

    # ── Tech list (Tech_Mentioned + Tech_Assumed combined) ────────────────────
    from collections import Counter
    c.execute("""
        SELECT Tech_Mentioned, Tech_Assumed FROM startups
        WHERE Status='Active' AND (Tech_Mentioned IS NOT NULL OR Tech_Assumed IS NOT NULL)
    """)
    tech_counter = Counter()
    for row in c.fetchall():
        for field in [row["Tech_Mentioned"], row["Tech_Assumed"]]:
            if field:
                for t in re.split(r'[,;|]+', field):
                    t = t.strip()
                    if t and t.lower() not in ('none', 'n/a', ''):
                        tech_counter[t] += 1
    tech_list = [{"tech": t, "count": n} for t, n in tech_counter.most_common(30)]

    # ── ALL_STARTUPS ──────────────────────────────────────────────────────────
    c.execute("""
        SELECT company_name, city, founded_year, tech_proximity,
               business_model, Category, website, founders,
               LOWER(COALESCE(ai_explanation,'')) AS ai_description
        FROM startups
        WHERE Status='Active'
        ORDER BY company_name
    """)
    all_startups = []
    for r in c.fetchall():
        all_startups.append({
            "company_name":   r["company_name"],
            "city":           r["city"],
            "founded_year":   r["founded_year"],
            "tech_proximity": r["tech_proximity"],
            "business_model": r["business_model"],
            "Category":       r["Category"],
            "website":        r["website"] or "",
            "founders":       r["founders"] or "",
            "ai_description": r["ai_description"] or "",
        })

    # ── KPI values ────────────────────────────────────────────────────────────
    c.execute("SELECT COUNT(*) FROM startups WHERE Status='Active'")
    total = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM startups WHERE Status='Active' AND business_model LIKE '%Product%'")
    product_count = c.fetchone()[0]

    c.execute("SELECT AVG(tech_proximity) FROM startups WHERE Status='Active' AND tech_proximity IS NOT NULL")
    avg_ai = round(c.fetchone()[0] or 0, 1)

    c.execute("SELECT COUNT(*) FROM startups WHERE Status='Active' AND tech_proximity >= 90")
    core_ai = c.fetchone()[0]

    c.execute("""
        SELECT COUNT(*) FROM startups
        WHERE Status='Active' AND Investors IS NOT NULL AND Investors != ''
    """)
    investor_count = c.fetchone()[0]

    c.execute("""
        SELECT COUNT(DISTINCT city) FROM startups
        WHERE Status='Active' AND city NOT IN ('','Unknown','Foreign')
    """)
    city_count = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM startups WHERE Status='Active' AND founded_year >= 2020")
    recent_count = c.fetchone()[0]

    product_pct = round(product_count / total * 100, 1) if total else 0

    conn.close()

    return {
        "sectors":          sectors,
        "subcategories":    subcategories,
        "cities":           cities,
        "ai_buckets":       ai_buckets,
        "ai_proximity_dist": ai_proximity_dist,
        "bmodels":          bmodels,
        "years":            years,
        "cloud_relevance":  cloud_relevance,
        "top_investors":    top_investors,
        "tech_list":        tech_list,
        "all_startups":     all_startups,
        "kpi": {
            "total":         total,
            "product_pct":   product_pct,
            "product_count": product_count,
            "avg_ai":        avg_ai,
            "core_ai":       core_ai,
            "investors":     investor_count,
            "cities":        city_count,
            "recent":        recent_count,
        },
    }


def update_html(html_path: Path, data: dict) -> str:
    """Replace all JS data constants and KPI values in the HTML."""
    print("  Updating HTML …")
    html = html_path.read_text(encoding="utf-8")

    def replace_const(html: str, name: str, value) -> str:
        new_val  = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        pattern  = rf'(const {re.escape(name)}\s*=\s*)(\[.*?\]|\{{.*?\}})(;)'
        replacement = rf'\g<1>{new_val}\3'
        result, n = re.subn(pattern, replacement, html, flags=re.DOTALL)
        if n == 0:
            print(f"    ⚠️  WARNING: const {name} not found in HTML")
        return result

    html = replace_const(html, "SECTORS",          data["sectors"])
    html = replace_const(html, "SUBCATEGORIES",    data["subcategories"])
    html = replace_const(html, "CITIES",           data["cities"])
    html = replace_const(html, "AI_BUCKETS",       data["ai_buckets"])
    html = replace_const(html, "AI_PROXIMITY_DIST",data["ai_proximity_dist"])
    html = replace_const(html, "BMODELS",          data["bmodels"])
    html = replace_const(html, "YEARS",            data["years"])
    html = replace_const(html, "CLOUD_RELEVANCE",  data["cloud_relevance"])
    html = replace_const(html, "TOP_INVESTORS",    data["top_investors"])
    html = replace_const(html, "TECH_LIST",        data["tech_list"])
    html = replace_const(html, "ALL_STARTUPS",     data["all_startups"])

    # ── KPI values ────────────────────────────────────────────────────────────
    kpi = data["kpi"]
    month_year = datetime.now().strftime("%B %Y")

    replacements = [
        (r'(<div class="kpi-value" id="kpi-total">)[^<]*(</div>)',
         rf'\g<1>{kpi["total"]:,}\2'),
        (r'(<div class="kpi-value" id="kpi-ai">)[^<]*(</div>)',
         rf'\g<1>{kpi["avg_ai"]}%\2'),
        (r'(<div class="kpi-value" id="kpi-investors">)[^<]*(</div>)',
         rf'\g<1>{kpi["investors"]:,}\2'),
        (r'(<div class="kpi-value" id="kpi-cities">)[^<]*(</div>)',
         rf'\g<1>{kpi["cities"]}+\2'),
        (r'(<div class="kpi-value" id="kpi-recent">)[^<]*(</div>)',
         rf'\g<1>{kpi["recent"]:,}\2'),
        (r'(<div class="kpi-sublabel"[^>]*>)[^<]*are product companies(</div>)',
         rf'\g<1>{kpi["product_pct"]}% are product companies\2'),
        (r'(<div class="kpi-sublabel"[^>]*>)[^<]*core AI companies[^<]*(</div>)',
         rf'\g<1>{kpi["core_ai"]:,} core AI companies (90%+)\2'),
        (r'(Data as of )[A-Z][a-z]+ \d{4}',
         rf'\g<1>{month_year}'),
    ]

    for pattern, repl in replacements:
        html, n = re.subn(pattern, repl, html, flags=re.DOTALL)
        if n == 0:
            print(f"    ⚠️  Pattern not matched: {pattern[:60]}…")

    return html


def deploy_to_github_pages(html_content: str, deploy_dir: Path) -> str:
    """Deploy updated dashboard to GitHub Pages via the gh-pages branch.

    Strategy: use a temp worktree so we never touch the main branch working tree.
    1. Write index.html + assets into a temp directory
    2. Force-push that directory's contents as the gh-pages branch
    Returns the GitHub Pages URL on success.
    """
    print("  Deploying to GitHub Pages …")

    # Detect repo remote URL to derive the Pages URL
    try:
        remote_url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=str(REPO_DIR), text=True
        ).strip()
    except subprocess.CalledProcessError:
        print("❌  Could not detect git remote. Is this a git repository?")
        sys.exit(1)

    # Parse owner/repo from remote URL
    # Handles: https://github.com/user/repo.git  OR  git@github.com:user/repo.git
    import re as _re
    m = _re.search(r'github\.com[:/](.+?)(?:\.git)?$', remote_url)
    if not m:
        print(f"❌  Could not parse GitHub URL from: {remote_url}")
        sys.exit(1)
    owner_repo = m.group(1)
    owner, repo = owner_repo.split("/", 1)
    pages_url = f"https://{owner}.github.io/{repo}/"

    # Prepare deploy content in a temp directory
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Write the main dashboard
        (tmp / "index.html").write_text(html_content, encoding="utf-8")

        # Copy any assets (images, etc.) from deploy/
        for ext in ("*.jpeg", "*.jpg", "*.png", "*.svg", "*.css", "*.js"):
            for f in deploy_dir.glob(ext):
                if not f.name.startswith("_") and "template" not in f.name:
                    shutil.copy2(f, tmp / f.name)

        # Copy intel.html if it exists
        intel = deploy_dir / "intel.html"
        if intel.exists():
            shutil.copy2(intel, tmp / "intel.html")

        # Add .nojekyll to prevent GitHub from processing with Jekyll
        (tmp / ".nojekyll").write_text("")

        # Initialize a git repo in the temp dir, commit, and force-push
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = "Dashboard Bot"
        env["GIT_AUTHOR_EMAIL"] = "bot@ecosystem.ai"
        env["GIT_COMMITTER_NAME"] = "Dashboard Bot"
        env["GIT_COMMITTER_EMAIL"] = "bot@ecosystem.ai"

        cmds = [
            ["git", "init"],
            ["git", "checkout", "-b", GH_PAGES_BRANCH],
            ["git", "add", "."],
            ["git", "commit", "-m", f"Dashboard update {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
            ["git", "remote", "add", "origin", remote_url],
            ["git", "push", "--force", "origin", GH_PAGES_BRANCH],
        ]

        for cmd in cmds:
            result = subprocess.run(
                cmd, cwd=tmpdir, capture_output=True, text=True, env=env, timeout=60
            )
            if result.returncode != 0 and "push" in " ".join(cmd):
                print(f"❌  git push failed:\n{result.stderr}")
                sys.exit(1)

    print(f"    Pushed to branch: {GH_PAGES_BRANCH} ✓")
    return pages_url


def publish_fresh_dashboard(local_only=False):
    """Generate dashboard from scratch via generate_dashboard.py, then deploy.

    This is the preferred publish path after main.py --all completes.
    Returns the live URL on success, or None on failure.
    """
    print(f"\n{'─'*55}")
    print(f"  Dashboard Publish (generate + deploy)")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'─'*55}\n")

    # Step 1: Run generate_dashboard.py to produce deploy/index.html
    gen_script = Path(__file__).resolve().parent / "deploy" / "generate_dashboard.py"
    if not gen_script.exists():
        print(f"❌  Generator not found: {gen_script}")
        return None

    print("  Step 1: Generating fresh dashboard HTML …")
    result = subprocess.run(
        [sys.executable, str(gen_script)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        print(f"❌  generate_dashboard.py failed:\n{result.stderr[:500]}")
        return None
    print(f"    {result.stdout.strip()}")

    # Step 2: Read the generated file
    index_path = DEPLOY_DIR / "index.html"
    if not index_path.exists():
        print(f"❌  Generated file not found: {index_path}")
        return None

    html_content = index_path.read_text(encoding="utf-8")
    size_kb = len(html_content.encode("utf-8")) / 1024
    print(f"    Generated: {index_path.name} ({size_kb:.0f} KB)")

    if local_only:
        print(f"\n  ✅  Dashboard generated (local only, no deployment)")
        print(f"     File: {index_path}")
        return str(index_path)

    # Step 3: Deploy to GitHub Pages
    live_url = deploy_to_github_pages(html_content, DEPLOY_DIR)

    print(f"\n{'─'*55}")
    print(f"  🚀  Live at: {live_url}")
    print(f"     (may take 1-2 minutes for GitHub Pages to update)")
    print(f"{'─'*55}\n")
    return live_url


def main():
    """Legacy path: update existing HTML in-place then deploy."""
    print(f"\n{'─'*55}")
    print(f"  Turkey AI Dashboard Updater")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'─'*55}\n")

    if not DB_PATH.exists():
        print(f"❌  Database not found: {DB_PATH}")
        sys.exit(1)

    # 1. Extract fresh data
    data = extract_data(DB_PATH)
    kpi  = data["kpi"]
    print(f"\n  📊 Fresh data loaded:")
    print(f"     Companies : {kpi['total']:,}")
    print(f"     Avg AI    : {kpi['avg_ai']}%")
    print(f"     Cities    : {kpi['cities']}")
    print(f"     Startups  : {len(data['all_startups']):,} rows in ALL_STARTUPS\n")

    # 2. Update HTML
    updated_html = update_html(HTML_PATH, data)

    # 3. Save updated HTML back to source file
    HTML_PATH.write_text(updated_html, encoding="utf-8")
    print(f"  ✅  Saved → {HTML_PATH.name}")

    # 4. Deploy to GitHub Pages
    live_url = deploy_to_github_pages(updated_html, DEPLOY_DIR)

    print(f"\n{'─'*55}")
    print(f"  🚀  Live at: {live_url}")
    print(f"{'─'*55}\n")


if __name__ == "__main__":
    if "--local" in sys.argv:
        publish_fresh_dashboard(local_only=True)
    elif "--legacy" in sys.argv:
        main()
    else:
        publish_fresh_dashboard()
