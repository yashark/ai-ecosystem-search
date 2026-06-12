---
name: ecosystem-weekly-report
description: "Generate a weekly intelligence report for the Turkish AI Ecosystem. Use this skill whenever the user mentions: weekly report, ecosystem update, weekly summary, what happened this week, news roundup, funding activity, ecosystem digest, weekly briefing, or anything about summarizing recent Turkish AI startup/investor activity. Also trigger when the user asks for a 'report', 'summary', or 'update' about the ecosystem, startups, investments, or news — even if they don't explicitly say 'weekly'."
---

# Ecosystem Weekly Report Skill

You generate a professional weekly intelligence report from the Turkish AI Ecosystem Search database. The project lives at the user's mounted folder containing `ai-ecosystem-search/`. The database is `Master.db` (SQLite).

## Your Role

The local pipeline collects raw data (news mentions, investments, new startups) but cannot synthesize it into analytical narratives. You provide the intelligence layer — turning data points into insights, spotting trends, and writing a report that a human analyst would find valuable.

## Report Generation Process

### Step 1: Gather Data

Run `scripts/gather_weekly_data.py` to extract data from the last 7 days (or the date range the user specifies). This pulls:

- New news mentions (with summaries, sources, linked startups)
- New investment records (amounts, investors, rounds)
- Newly discovered startups
- Ecosystem activities (partnerships, program launches, government initiatives)
- Data quality metrics (quarantine count, confidence distribution)

If the script doesn't exist, query the database directly:

```sql
-- News from last 7 days
SELECT nm.*, s.company_name, s.Category
FROM news_mentions nm
LEFT JOIN startups s ON nm.startup_id = s.id
WHERE nm.scanned_at >= date('now', '-7 days')
  AND nm.is_relevant = 1
ORDER BY nm.published_date DESC;

-- Recent investments
SELECT i.*, s.company_name, inv.investor_name, inv.investor_type
FROM investments i
JOIN startups s ON i.startup_id = s.id
LEFT JOIN investors inv ON i.investor_id = inv.investor_id
WHERE i.investment_date >= date('now', '-7 days')
   OR i.id IN (SELECT investment_id FROM ... WHERE created_at >= date('now', '-7 days'))
ORDER BY i.amount_usd DESC;

-- New startups
SELECT * FROM startups
WHERE processed_at >= date('now', '-7 days')
ORDER BY data_confidence DESC;

-- Ecosystem activities
SELECT ea.*, ee.entity_name, ee.entity_type
FROM ecosystem_activities ea
LEFT JOIN ecosystem_entities ee ON ea.entity_id = ee.id
WHERE ea.activity_date >= date('now', '-7 days')
ORDER BY ea.activity_date DESC;
```

### Step 2: Analyze and Categorize

Group the data into these sections:

1. **Key Highlights** — The 3-5 most significant events. Prioritize: large funding rounds > acquisitions > major partnerships > new unicorn-track startups > policy changes
2. **Funding Activity** — Each funding round with company, amount, investor(s), round type. Include a total funding summary for the week
3. **New Startups Discovered** — Name, sector, brief description, initial confidence score
4. **Partnerships & Collaborations** — Notable corporate-startup partnerships, international collaborations
5. **Ecosystem Activity** — Government programs, accelerator batches, teknopark news, policy changes
6. **Investor Activity** — New investors entering the Turkish market, notable investor moves
7. **Sector Trends** — Which sectors saw the most activity this week
8. **Data Quality Notes** — Items flagged for review, quarantine queue size, any anomalies spotted

### Step 3: Write the Report

Use this template structure. Write in professional but accessible prose — not just data dumps. Add context and analysis where you can.

```markdown
# Turkey AI Ecosystem — Weekly Report
**Period**: [Start Date] to [End Date]
**Generated**: [Today's Date]

## Key Highlights
[3-5 bullet points of the most significant events, with brief context for each]

## Funding Activity
[Narrative paragraph summarizing the week's funding landscape]
[Table or list of individual rounds: Company | Amount | Investor(s) | Round | Date]
**Week Total**: [Sum of known amounts]

## New Startups Discovered
[Brief intro paragraph]
[For each: Name — Sector — One-line description]

## Partnerships & Collaborations
[Narrative of notable partnerships with why they matter]

## Ecosystem Activity
[Government, accelerator, teknopark news organized by entity type]

## Investor Spotlight
[Any new or notably active investors this week]

## Sector Pulse
[Which sectors are heating up, which are quiet — based on this week's data]

## Data Quality Notes
[Quarantine items pending review, any data anomalies, pipeline health]
```

### Step 4: Save the Report

Save as markdown to: `reports/weekly/[YYYY-MM-DD].md`

Also offer to generate an HTML version if the user wants it for the dashboard.

## Important Rules

- If there's very little activity in a section, say so briefly rather than padding
- Always cite the source when referencing specific news or investment data
- When data seems inconsistent (e.g., funding amount doesn't match round type), note it as uncertain rather than stating it as fact
- If the user asks for a specific date range, adjust all queries accordingly
- Use actual numbers from the database — don't generalize when you have specifics
- If a section has no data for the period, include it with a note like "No new [X] recorded this week"
