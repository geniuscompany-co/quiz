#!/bin/sh
# Push to GitHub, then trigger a Render deploy through your private Deploy Hook.
# One-time setup: put the hook URL (Render > quiz > Settings > Deploy Hook) in a file named .render_hook
# in this folder. That file is git-ignored, so it is never pushed to GitHub.
cd "$(dirname "$0")" || exit 1
git push || exit 1
if [ -f .render_hook ]; then
  curl -fsS "$(cat .render_hook)" && echo "\nDeploy triggered. Watch the Deploys tab in Render."
else
  echo "No .render_hook file found; relying on Render's auto-deploy."
fi
