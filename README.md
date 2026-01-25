# LLM GitHub Bot Control Repository

This repository implements the "LLM完全控制一个独立GitHub账号" system as described in the project plan.

## Overview

The system allows an LLM (Claude Code) to act as an independent GitHub account (`llm-bot-dev`) through GitHub Actions. The LLM can perform code reviews, modify files, create PRs/issues, and participate in discussions using the bot account's identity.

## Architecture

1. **Independent GitHub Account**: Create a dedicated GitHub account (`llm-bot-dev`) with appropriate permissions
2. **PAT Storage**: Store the bot's Personal Access Token in this repository's GitHub Actions Secrets as `LLMGH_TOKEN`
3. **Control Repository**: This repository contains:
   - GitHub Actions workflows that run Claude Code with MCP tools
   - Claude Code configuration files
   - MCP server configurations
4. **Trigger Mechanism**: External server receives GitHub webhooks and triggers `workflow_dispatch` with task context
5. **Execution**: Claude Code runs in Actions environment with the bot's PAT, performing operations on target repositories

## Setup Instructions

### 1. Create Bot Account and PAT
- Create a new GitHub account (e.g., `llm-bot-dev`)
- Generate a Fine-grained PAT with permissions for:
  - Repository contents (read & write)
  - Issues, pull requests, discussions
  - Workflows (if needed)
- Add the bot account to target repositories with appropriate access levels

### 2. Configure This Control Repository
- Fork or create this repository
- Add the following Secrets in repository Settings > Secrets and variables > Actions:
  - `LLMGH_TOKEN`: The bot account's PAT
  - `ANTHROPIC_API_KEY`: Your Anthropic API key for Claude API

### 3. Customize Configuration
- Edit `.claude/config.json` to adjust Claude Code settings
- Edit `.claude/mcp.config.json` to configure MCP servers
- Modify `.github/workflows/llm-bot-runner.yml` if needed

### 4. Set Up Webhook Server
- Deploy the example FastAPI server (`server.py`) to receive GitHub webhooks
- Configure target repositories to send webhooks to your server
- The server validates webhooks and triggers the LLM bot workflow

### 5. Trigger the Bot
- When events occur (new issue, PR, comment, etc.), the server triggers:
  ```bash
  gh workflow run llm-bot-runner.yml -f task="Review PR #45" -f context='{"repo":"owner/project","pr_number":45,...}'
  ```
- The LLM bot will analyze the context and perform appropriate actions
- For manual testing, you can use the example script:
  ```bash
  ./scripts/trigger-example.sh
  ```

## File Structure

```
├── .github/workflows/
│   ├── llm-bot-runner.yml     # Main workflow for LLM execution
│   └── opencode.yml           # Existing opencode workflow (optional)
├── .claude/
│   ├── config.json            # Claude Code configuration
│   └── mcp.config.json        # MCP servers configuration
├── server.py                  # Example FastAPI webhook server
├── LICENSE
└── README.md
```

## Security Considerations

- The bot account's PAT never leaves GitHub Actions environment
- All sensitive tokens are stored in GitHub Secrets
- Claude Code is configured to never echo or log tokens
- Bot account has limited permissions only to repositories you trust
- Use branch protection rules and required reviews for critical branches

## Example Use Cases

1. **Automated PR Reviews**: LLM reviews PRs, suggests improvements, and can even commit fixes
2. **Issue Triage**: LLM responds to new issues, asks for clarification, or creates follow-up tasks
3. **Code Maintenance**: LLM performs routine code updates, dependency upgrades, or refactoring
4. **Documentation Updates**: LLM improves documentation based on user questions or code changes

## Customization

- Modify the system prompt in `.claude/config.json` to change LLM behavior
- Add more MCP servers for additional tools (Jira, Slack, monitoring systems)
- Extend the webhook server to handle more event types
- Create specialized workflows for different task types

## Troubleshooting

- Check GitHub Actions logs for execution details
- Verify PAT has correct permissions
- Ensure webhook payloads are correctly formatted
- Confirm Claude CLI installation succeeds (check Anthropic installation script)