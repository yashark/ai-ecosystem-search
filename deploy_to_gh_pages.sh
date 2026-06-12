#!/usr/bin/env bash
# Regenerate the dashboard from Master.db and publish to the gh-pages branch.
# Run from the repo root on your Mac (where your GitHub credentials live):
#   ./deploy_to_gh_pages.sh
#
# Live site: https://yashark.github.io/ai-ecosystem-search/

set -euo pipefail
cd "$(dirname "$0")"

REPO_ROOT="$(pwd)"
WT_DIR="$(mktemp -d -t ghpages-XXXXXX)"
TS="$(date +%Y-%m-%d\ %H:%M)"

cleanup() {
  git worktree remove --force "$WT_DIR" 2>/dev/null || true
  rm -rf "$WT_DIR"
}
trap cleanup EXIT

echo "▶ Regenerating deploy/index.html from Master.db …"
python3 deploy/generate_dashboard.py

echo "▶ Fetching origin/gh-pages …"
git fetch origin gh-pages

echo "▶ Setting up worktree at $WT_DIR …"
git worktree add -B gh-pages "$WT_DIR" origin/gh-pages

echo "▶ Copying fresh files into worktree …"
cp deploy/index.html    "$WT_DIR/index.html"
cp deploy/intel.html    "$WT_DIR/intel.html"
cp deploy/IMG_7541.jpeg "$WT_DIR/IMG_7541.jpeg"
touch "$WT_DIR/.nojekyll"

echo "▶ Committing …"
cd "$WT_DIR"
git add -A
if git diff --cached --quiet; then
  echo "  (no changes — nothing to publish)"
  exit 0
fi
git commit -m "Dashboard update $TS"

echo "▶ Pushing to origin/gh-pages …"
git push origin gh-pages

echo "✓ Live in ~1–2 min: https://yashark.github.io/ai-ecosystem-search/"
