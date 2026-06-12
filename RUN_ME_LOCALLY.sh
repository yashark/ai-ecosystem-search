#!/bin/bash
# Generated 2026-04-20 by the weekly-report follow-up.
#
# These two modules need your local LLM (MLX / Ollama on localhost:11434) and
# outbound search access, so the Cowork sandbox couldn't run them. Run this
# from the repo root on your Mac to close the M3/M4 gap flagged in the
# 2026-04-20 weekly report.
#
# Log files go under logs/.

set -euo pipefail
cd "$(dirname "$0")"

mkdir -p logs

TS=$(date +%Y%m%d_%H%M%S)

echo "▶ Running M3 ecosystem scanner …"
python3 agent_ecosystem.py 2>&1 | tee "logs/m3_ecosystem_${TS}.log"

echo "▶ Running M4 investor enrichment (batch=20) …"
python3 agent_investors.py --batch 20 2>&1 | tee "logs/m4_investors_${TS}.log"

echo "▶ Re-running anomaly detection after new data landed …"
python3 cowork_bridge.py --detect --days 7 2>&1 | tee "logs/anomalies_${TS}.log"

echo "✓ Done. Log files in logs/. Review cowork_queue items via:"
echo "    python3 cowork_bridge.py --pending"
