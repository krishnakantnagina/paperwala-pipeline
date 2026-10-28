#!/usr/bin/env bash
# Start a workflow run - unless one of that workflow is already queued or
# running (other than this run itself). That is what keeps the chain single:
# two chains that meet merge into one instead of doubling every round.
#
#   scripts/start_run.sh work.yml -f workers=16 ...
#
# Needs GH_TOKEN (the job's github.token, with actions: write). Runs started
# with GITHUB_TOKEN do trigger workflow_dispatch, unlike most events.
set -euo pipefail
wf=$1; shift
busy=$(gh run list --workflow "$wf" --limit 30 --json databaseId,status \
  --jq "[.[] | select(.status != \"completed\" and .databaseId != ${GITHUB_RUN_ID:-0})] | length")
if [ "$busy" -gt 0 ]; then
  echo "$wf: a run is already queued or running; it carries on from here"
  exit 0
fi
gh workflow run "$wf" "$@"
echo "$wf: next run started"
