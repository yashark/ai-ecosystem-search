"""
Cloud Relevance Classification Utility

Classifies startups by their relationship to cloud computing using LLM inference.
Handles implicit cloud users (SaaS, API-first), explicit cloud tech mentions,
negative signals (on-premise, private cloud emphasis), and Turkish terminology.

Usage:
    python3 util_cloud_classify.py                  # Dry-run
    python3 util_cloud_classify.py --commit         # Apply changes
    python3 util_cloud_classify.py --commit --all   # Process all, no batch limit
    python3 util_cloud_classify.py --batch 200      # Process 200 per run
"""
import sqlite3
import argparse
import json
from datetime import datetime, timezone

from agent_core import _call_llm, get_db_connection, get_utc_now

DB_NAME = "Master.db"

# ─── Keyword Heuristics (pre-filter to reduce LLM calls) ───────────────────

POSITIVE_KEYWORDS = {
    # English cloud terms
    'cloud', 'aws', 'azure', 'gcp', 'google cloud', 'amazon web services',
    'saas', 'paas', 'iaas', 'serverless', 'lambda', 'ec2', 's3',
    'kubernetes', 'k8s', 'docker', 'containerized', 'microservices',
    'cloud-native', 'cloud-based', 'multi-tenant', 'elastic scaling',
    'hosted solution', 'managed service', 'web-based platform',
    'api-first', 'restful api', 'browser-based',
    'firebase', 'heroku', 'vercel', 'netlify', 'cloudflare',
    'openshift', 'terraform', 'ansible', 'devops', 'ci/cd',
    # Turkish cloud terms
    'bulut', 'bulut bilişim', 'bulut tabanlı', 'bulut hizmeti',
    'bulut altyapısı', 'servis olarak yazılım',
}

NEGATIVE_KEYWORDS = {
    # On-premise / private cloud emphasis
    'on-premise', 'on-premises', 'on-prem', 'self-hosted',
    'private cloud', 'private deployment', 'air-gapped',
    'local deployment', 'local data', 'local processing',
    'edge computing', 'edge-only', 'offline-first',
    'data sovereignty', 'data residency', 'no cloud dependency',
    'runs locally', 'embedded system', 'firmware',
    # Turkish equivalents
    'yerel sunucu', 'yerinde kurulum', 'özel bulut',
    'yerel veri', 'gömülü sistem',
}


def keyword_prescan(description, tech_mentioned, business_model):
    """Quick keyword scan to pre-classify obvious cases. Returns:
    - 'positive': strong cloud signal from keywords
    - 'negative': strong anti-cloud signal from keywords
    - 'ambiguous': needs LLM inference
    """
    text = ' '.join([
        (description or '').lower(),
        (tech_mentioned or '').lower(),
        (business_model or '').lower(),
    ])

    neg_hits = [kw for kw in NEGATIVE_KEYWORDS if kw in text]
    pos_hits = [kw for kw in POSITIVE_KEYWORDS if kw in text]

    # Negative signals override positive (user explicitly wants this)
    if neg_hits and not pos_hits:
        return 'negative', neg_hits
    if pos_hits and not neg_hits:
        return 'positive', pos_hits
    # Both or neither — needs LLM
    return 'ambiguous', pos_hits + neg_hits


# ─── LLM Classification ────────────────────────────────────────────────────

def classify_cloud_relevance(company_name, description, tech_mentioned, business_model, category):
    """Use LLM to classify cloud relevance with nuanced reasoning."""
    prompt = f"""You are a Cloud Business Analyst. Classify this company's relationship to cloud computing.

Company: {company_name}
Description: {(description or 'Unknown')[:600]}
Technologies: {tech_mentioned or 'Unknown'}
Business Model: {business_model or 'Unknown'}
Category: {category or 'Unknown'}

Classify into EXACTLY ONE of these labels:

"Cloud-Native" — Company's core product IS a cloud service (SaaS, PaaS, IaaS, cloud infrastructure, API platform). Cloud delivery is central to their business.

"Cloud-Enabled" — Company uses cloud infrastructure to deliver their product/service (web app hosted on cloud, uses AWS/Azure/GCP, cloud-deployed AI models) but cloud isn't their core differentiator.

"Cloud-Adjacent" — Company's technology could leverage cloud but no strong evidence of cloud dependency. Most modern software startups fall here. Includes companies using standard web technologies without explicit cloud mentions.

"Non-Cloud" — Company explicitly operates on-premise, edge-only, embedded systems, hardware-focused, firmware, or emphasizes local/private deployment. Also for companies in physical industries (manufacturing, agriculture hardware, physical logistics) with no software cloud component.

EXCLUSION RULES (very important):
- If the company emphasizes "private cloud", "on-premise deployment", "local data processing", "self-hosted", "data sovereignty", or "edge-only" — classify as "Non-Cloud" even if they use some cloud internally.
- If the company builds on-premise hardware or embedded systems — "Non-Cloud".
- If the company is a consultancy that helps others with cloud but doesn't deliver cloud products — "Cloud-Adjacent".

INCLUSION RULES:
- Any SaaS, web platform, or API service is at minimum "Cloud-Enabled".
- Companies with AWS, Azure, GCP, Kubernetes, Docker, serverless in their tech stack are at minimum "Cloud-Enabled".
- Companies building cloud infrastructure, DevOps tools, or cloud management platforms are "Cloud-Native".

Output ONLY this JSON:
{{"cloud_relevance": "<label>", "confidence": <0.0-1.0>, "reasoning": "<1 sentence>"}}"""

    result = _call_llm(prompt, model_type="fast")
    if result and isinstance(result, dict):
        label = result.get('cloud_relevance', 'Cloud-Adjacent')
        # Validate label
        valid_labels = {'Cloud-Native', 'Cloud-Enabled', 'Cloud-Adjacent', 'Non-Cloud'}
        if label not in valid_labels:
            # Try to match closest
            label_lower = label.lower().replace(' ', '-')
            for v in valid_labels:
                if v.lower().replace(' ', '-') == label_lower:
                    label = v
                    break
            else:
                label = 'Cloud-Adjacent'  # safe default
        return label, result.get('confidence', 0.5), result.get('reasoning', '')
    return None, 0, ''


# ─── Heuristic Overrides ───────────────────────────────────────────────────

def apply_heuristic_overrides(label, description, tech_mentioned, business_model, category):
    """Apply rule-based overrides for cases the LLM commonly gets wrong."""
    text = ' '.join([
        (description or '').lower(),
        (tech_mentioned or '').lower(),
    ])
    cat = (category or '').lower()
    bm = (business_model or '').lower()

    # Override 1: SaaS/Product companies with web tech are at minimum Cloud-Enabled
    if bm == 'product' and label in ('Non-Cloud', 'Cloud-Adjacent'):
        web_signals = ['api', 'web', 'dashboard', 'platform', 'saas', 'online']
        if any(sig in text for sig in web_signals):
            return 'Cloud-Enabled'

    # Override 2: Embedded/hardware categories should be Non-Cloud unless cloud evidence
    hardware_tags = ['robotics', 'unmanned systems', 'automotive', 'adv. manufacturing']
    if any(t in cat for t in hardware_tags) and label == 'Cloud-Adjacent':
        cloud_evidence = ['cloud', 'aws', 'azure', 'gcp', 'saas', 'web platform', 'bulut']
        if not any(ev in text for ev in cloud_evidence):
            return 'Non-Cloud'

    # Override 3: Explicit private/on-prem emphasis — force Non-Cloud
    neg_signals = ['on-premise', 'on-prem', 'self-hosted', 'private cloud',
                   'edge-only', 'air-gapped', 'no cloud', 'yerinde kurulum', 'özel bulut']
    if any(sig in text for sig in neg_signals):
        return 'Non-Cloud'

    return label


# ─── Main Processing Loop ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Cloud Relevance Classification")
    parser.add_argument('--commit', action='store_true', help='Apply changes to DB')
    parser.add_argument('--batch', type=int, default=100, help='Batch size (default 100)')
    parser.add_argument('--all', action='store_true', help='Process all, ignore batch limit')
    parser.add_argument('--force', action='store_true', help='Re-classify already classified')
    args = parser.parse_args()

    conn = get_db_connection(DB_NAME)
    cursor = conn.cursor()

    # Select startups to classify
    if args.force:
        where = "WHERE Status = 'Active'"
    else:
        where = "WHERE Status = 'Active' AND (cloud_relevance IS NULL OR cloud_relevance = '')"

    limit_clause = "" if args.all else f"LIMIT {args.batch}"

    rows = cursor.execute(f"""
        SELECT id, company_name, description, Tech_Mentioned, business_model, Category
        FROM startups {where}
        ORDER BY data_confidence DESC
        {limit_clause}
    """).fetchall()

    total = len(rows)
    print("=" * 60)
    print(f"  Cloud Relevance Classification {'(COMMIT)' if args.commit else '(DRY RUN)'}")
    print(f"  Startups to process: {total}")
    print("=" * 60)

    stats = {'Cloud-Native': 0, 'Cloud-Enabled': 0, 'Cloud-Adjacent': 0, 'Non-Cloud': 0, 'errors': 0}
    keyword_shortcuts = 0

    for i, (sid, name, desc, tech, bm, cat) in enumerate(rows, 1):
        print(f"\n[{i}/{total}] {name}")

        # Step 1: Keyword prescan
        prescan, hits = keyword_prescan(desc, tech, bm)

        if prescan == 'negative' and not args.force:
            label = 'Non-Cloud'
            confidence = 0.8
            reasoning = f"Negative keywords detected: {', '.join(hits[:3])}"
            keyword_shortcuts += 1
            print(f"  [Keyword] → {label} (negative signal: {hits[:3]})")
        elif prescan == 'positive' and not args.force:
            # Still run LLM for positive cases to get accurate sub-classification
            label, confidence, reasoning = classify_cloud_relevance(name, desc, tech, bm, cat)
            if not label:
                stats['errors'] += 1
                print(f"  [ERROR] LLM returned nothing")
                continue
        else:
            # Ambiguous — full LLM classification
            label, confidence, reasoning = classify_cloud_relevance(name, desc, tech, bm, cat)
            if not label:
                stats['errors'] += 1
                print(f"  [ERROR] LLM returned nothing")
                continue

        # Step 2: Apply heuristic overrides
        original_label = label
        label = apply_heuristic_overrides(label, desc, tech, bm, cat)
        if label != original_label:
            print(f"  [Override] {original_label} → {label}")

        stats[label] = stats.get(label, 0) + 1
        print(f"  → {label} (conf: {confidence:.2f}) {reasoning[:80]}")

        # Step 3: Write to DB
        if args.commit:
            cursor.execute(
                "UPDATE startups SET cloud_relevance = ? WHERE id = ?",
                (label, sid)
            )
            if i % 25 == 0:
                conn.commit()
                print(f"  [Checkpoint] Committed batch up to {i}")

    if args.commit:
        conn.commit()

    # Summary
    print(f"\n{'=' * 60}")
    print(f"  SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Processed:       {total}")
    print(f"  Keyword shortcuts: {keyword_shortcuts}")
    for label in ['Cloud-Native', 'Cloud-Enabled', 'Cloud-Adjacent', 'Non-Cloud']:
        pct = (stats[label] / total * 100) if total > 0 else 0
        print(f"  {label:20s}: {stats[label]:5d} ({pct:5.1f}%)")
    print(f"  Errors:          {stats['errors']}")

    if not args.commit:
        print(f"\n  DRY RUN — no changes written. Use --commit to apply.")
    else:
        # Final distribution
        dist = cursor.execute("""
            SELECT cloud_relevance, COUNT(*) FROM startups
            WHERE cloud_relevance IS NOT NULL
            GROUP BY cloud_relevance ORDER BY COUNT(*) DESC
        """).fetchall()
        print(f"\n  DB Distribution:")
        for label, cnt in dist:
            print(f"    {label}: {cnt}")

    conn.close()


def classify_cloud_relevance_heuristic(description, category, tech_mentioned):
    """Fast heuristic-only cloud classification (no LLM call).
    Used by agent_updater.py Tier 2 delta refresh for missing cloud_relevance.
    Returns a label string or None if ambiguous."""
    signal, hits = keyword_prescan(description, tech_mentioned, "")
    if signal == 'positive':
        # Distinguish Cloud-Native vs Cloud-Enabled by category
        if category and any(kw in category.lower() for kw in ('ai infrastructure', 'developer tools')):
            return 'Cloud-Native'
        return 'Cloud-Enabled'
    elif signal == 'negative':
        return 'Non-Cloud'
    # For ambiguous: default most software startups to Cloud-Adjacent
    if category and any(kw in category.lower() for kw in (
        'software development', 'data & analytics', 'fintech', 'saas',
        'e-commerce', 'marketing', 'hr & recruitment', 'edtech',
    )):
        return 'Cloud-Adjacent'
    return None  # Truly ambiguous — leave for LLM pass


if __name__ == "__main__":
    main()
