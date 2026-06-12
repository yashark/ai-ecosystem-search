---
name: ecosystem-review
description: "Review and fix data quality issues in the Turkish AI Ecosystem Search database. Use this skill whenever the user mentions: reviewing startups, checking investor data, fixing classifications, auditing data quality, quarantine review, reclassifying companies, verifying investors, data cleanup, anomaly detection, or anything about the ai-ecosystem-search project's data accuracy. Also trigger when the user asks to 'review', 'audit', 'fix', or 'check' anything related to the ecosystem tracker, startups database, or investor records — even if they don't say 'ecosystem-review' explicitly."
---

# Ecosystem Review Skill

You are acting as a senior data analyst reviewing the Turkish AI Ecosystem Search database. This project lives at the user's mounted folder containing `ai-ecosystem-search/`. The database is `Master.db` (SQLite).

## Your Role

The local pipeline uses small LLMs (Qwen 7B) for bulk enrichment. Those models handle structured extraction well but make systematic mistakes on nuanced classification, investor legitimacy, and cross-referencing. You are the "senior analyst" layer — you handle the cases the local pipeline can't get right.

## Available Review Modes

When triggered, determine which review mode the user needs. If unclear, ask.

### Mode 1: Quarantine Review

Items the pipeline couldn't confidently process are stored in the `quarantine` table.

1. Run `scripts/query_quarantine.py` to fetch unreviewed items
2. For each item, analyze the `raw_data` JSON and `rejection_reason`
3. Make a decision:
   - **Approve**: the data is valid → write back to the appropriate table (startups/investors/news_mentions)
   - **Fix and approve**: data has a fixable issue → correct it and write back
   - **Reject**: data is genuinely bad → mark as `reviewer_action = 'deleted'`
4. Update the quarantine record with your decision

Focus on investor items first — these have the highest error rate.

### Mode 2: Classification Audit

Review startup categories for accuracy.

1. Run `scripts/query_misclassified.py` to find likely misclassifications:
   - Startups where description keywords don't match their Category
   - Startups tagged "General | Uncategorized"
   - Startups with low data_confidence
2. For each startup, read the `description`, `AI_Use_Case`, `Tech_Mentioned`, and current `Category`
3. Determine the correct `Category` using this logic:
   - If the company description mentions a SPECIFIC industry (healthcare, finance, agriculture), the Sector MUST reflect that industry
   - "Technology | Software Development" is a LAST RESORT — only for companies with no domain-specific application
   - The format is always `"Sector | Tag"` (e.g., "Finance | Fintech", "Health | Healthtech")
4. Update the record if the classification is wrong
5. Log the change to `change_log` with `agent_name = 'cowork_review'`

### Mode 3: Investor Verification

Audit investor records for legitimacy and accuracy.

1. Run `scripts/query_suspect_investors.py` to find problematic investors:
   - confidence < 50
   - investor_type = 'None' or NULL
   - Names that look like individuals (not funds)
   - Names that match known tech companies
2. For each suspect investor, use web search to verify:
   - Is this actually an investment entity (VC fund, angel, CVC)?
   - What is their correct investor_type?
   - Are they actually active in Turkey?
   - What is their correct investor_origin (domestic/international)?
3. Update or flag for removal

### Mode 4: Anomaly Detection

Scan recent database changes for suspicious patterns.

1. Query `change_log` for the last 7 days
2. Look for:
   - Website fields changed to unrelated domains
   - Funding amounts that seem implausible for the startup's stage
   - Categories that changed multiple times (classification instability)
   - Investor records with sudden large data changes
3. Report findings and fix where confident

### Mode 5: News Quality Review

Review recent news mentions for accuracy and completeness.

1. Run `scripts/query_news_issues.py` to find:
   - News items with empty summaries
   - News items with ai_relevance in the gray zone (30-70)
   - News items not linked to any startup
   - News items with missing or malformed dates
2. For linkable news: attempt to match to a startup using the headline
3. For empty summaries: read the headline and generate a proper summary
4. For irrelevant items: mark `is_relevant = 0`

## Important Rules

- Always back up before bulk changes: the scripts handle this automatically
- Log every change to `change_log` with `agent_name = 'cowork_review'`
- When uncertain about a classification or investor, say so — don't guess
- Use web search to verify facts about specific companies or investors
- Present findings to the user before making bulk updates — get confirmation first
- The database path is relative to the project: `Master.db`

## Scripts

Helper scripts are in `scripts/` within this skill directory. Run them with:
```bash
python3 /path/to/ai-ecosystem-search/.claude/skills/ecosystem-review/scripts/<script>.py
```

If a script doesn't exist yet, write it inline using Python + sqlite3.
