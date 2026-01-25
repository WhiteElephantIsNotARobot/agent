#!/bin/bash
# Example script to trigger the LLM bot workflow manually
# Requires gh CLI and appropriate permissions

set -e

CONTROL_REPO="owner/llm-bot-control"  # Change this to your control repository
TASK="Review PR #45"
CONTEXT='{"repo":"owner/project","pr_number":45,"event_type":"pull_request"}'

echo "Triggering LLM bot workflow..."
echo "Task: $TASK"
echo "Context: $CONTEXT"

# Trigger workflow dispatch
gh workflow run llm-bot-runner.yml \
  --repo "$CONTROL_REPO" \
  -f "task=$TASK" \
  -f "context=$CONTEXT"

echo "Workflow triggered successfully. Check GitHub Actions in the control repository."