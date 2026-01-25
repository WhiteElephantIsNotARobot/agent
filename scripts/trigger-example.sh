#!/bin/bash
# 手动触发LLM机器人工作流的示例脚本
# 需要gh CLI和适当的权限

set -e

CONTROL_REPO="owner/llm-bot-control"  # 将此更改为你的控制仓库
TASK="Review PR #45"
CONTEXT='{"repo":"owner/project","pr_number":45,"event_type":"pull_request"}'

echo "正在触发LLM机器人工作流..."
echo "任务: $TASK"
echo "上下文: $CONTEXT"

# 触发工作流调度
gh workflow run llm-bot-runner.yml \
  --repo "$CONTROL_REPO" \
  -f "task=$TASK" \
  -f "context=$CONTEXT"

echo "工作流触发成功。请在控制仓库中检查GitHub Actions。"