#!/usr/bin/env python3
"""
FastAPI server that receives GitHub webhooks and triggers the LLM bot workflow.

Environment variables needed:
- GITHUB_TOKEN: Personal Access Token with repo permissions
- WEBHOOK_SECRET: GitHub webhook secret for verification
- CONTROL_REPO: Repository where workflow is located (e.g., "owner/llm-bot-control")
"""

import os
import hmac
import hashlib
import json
import logging
from typing import Dict, Any, Optional

from fastapi import FastAPI, Request, HTTPException, Header, BackgroundTasks
from pydantic import BaseModel, Field
import httpx

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="LLM Bot Webhook Server")

# GitHub API configuration
GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
CONTROL_REPO = os.getenv("CONTROL_REPO", "owner/llm-bot-control")

# Headers for GitHub API requests
headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "LLM-Bot-Server"
}

class TaskContext(BaseModel):
    """Context data extracted from webhook event."""
    repo: str = Field(..., description="Repository full name (owner/repo)")
    event_type: str = Field(..., description="GitHub event type")
    event_id: str = Field(..., description="GitHub event ID")
    trigger_user: Optional[str] = Field(None, description="User who triggered the event")
    issue_number: Optional[int] = Field(None, description="Issue/PR number if applicable")
    comment_body: Optional[str] = Field(None, description="Comment body if applicable")
    pr_title: Optional[str] = Field(None, description="PR title if applicable")
    pr_body: Optional[str] = Field(None, description="PR body if applicable")
    pr_diff_url: Optional[str] = Field(None, description="PR diff URL if applicable")
    discussion_title: Optional[str] = Field(None, description="Discussion title if applicable")
    discussion_body: Optional[str] = Field(None, description="Discussion body if applicable")

def verify_webhook_signature(payload_body: bytes, signature_header: str) -> bool:
    """Verify GitHub webhook signature."""
    if not WEBHOOK_SECRET:
        logger.warning("WEBHOOK_SECRET not set, skipping signature verification")
        return True
    
    try:
        hash_object = hmac.new(
            WEBHOOK_SECRET.encode(),
            msg=payload_body,
            digestmod=hashlib.sha256
        )
        expected_signature = f"sha256={hash_object.hexdigest()}"
        return hmac.compare_digest(expected_signature, signature_header)
    except Exception as e:
        logger.error(f"Error verifying signature: {e}")
        return False

def extract_context_from_event(event_type: str, payload: Dict[str, Any]) -> TaskContext:
    """Extract relevant context from GitHub webhook payload."""
    repo = payload.get("repository", {}).get("full_name", "")
    event_id = payload.get("headers", {}).get("X-GitHub-Delivery", "") if isinstance(payload.get("headers"), dict) else ""
    
    context = TaskContext(
        repo=repo,
        event_type=event_type,
        event_id=event_id
    )
    
    # Extract based on event type
    if event_type == "issues":
        issue = payload.get("issue", {})
        context.issue_number = issue.get("number")
        context.trigger_user = issue.get("user", {}).get("login")
        context.comment_body = issue.get("body")
    elif event_type == "issue_comment":
        issue = payload.get("issue", {})
        comment = payload.get("comment", {})
        context.issue_number = issue.get("number")
        context.trigger_user = comment.get("user", {}).get("login")
        context.comment_body = comment.get("body")
    elif event_type == "pull_request":
        pr = payload.get("pull_request", {})
        context.issue_number = pr.get("number")
        context.trigger_user = pr.get("user", {}).get("login")
        context.pr_title = pr.get("title")
        context.pr_body = pr.get("body")
        context.pr_diff_url = pr.get("diff_url")
    elif event_type == "pull_request_review_comment":
        pr = payload.get("pull_request", {})
        comment = payload.get("comment", {})
        context.issue_number = pr.get("number")
        context.trigger_user = comment.get("user", {}).get("login")
        context.comment_body = comment.get("body")
    elif event_type == "discussion":
        discussion = payload.get("discussion", {})
        context.trigger_user = discussion.get("user", {}).get("login")
        context.discussion_title = discussion.get("title")
        context.discussion_body = discussion.get("body")
    
    return context

def generate_task_description(event_type: str, context: TaskContext) -> str:
    """Generate natural language task description based on event type."""
    if event_type == "pull_request":
        return f"Review PR #{context.issue_number} in {context.repo} and provide feedback or code improvements."
    elif event_type == "issue_comment" and context.comment_body and "@llm-bot-dev" in context.comment_body:
        return f"Respond to comment on issue #{context.issue_number} in {context.repo}."
    elif event_type == "issues":
        return f"Analyze new issue #{context.issue_number} in {context.repo} and provide initial response."
    elif event_type == "discussion":
        return f"Participate in discussion '{context.discussion_title}' in {context.repo}."
    else:
        return f"Handle {event_type} event in {context.repo}."

async def trigger_workflow_dispatch(task: str, context: TaskContext):
    """Trigger the workflow_dispatch event in the control repository."""
    url = f"{GITHUB_API}/repos/{CONTROL_REPO}/actions/workflows/llm-bot-runner.yml/dispatches"
    
    payload = {
        "ref": "main",
        "inputs": {
            "task": task,
            "context": context.json()
        }
    }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, json=payload, timeout=30.0)
            response.raise_for_status()
            logger.info(f"Workflow dispatch triggered successfully: {response.status_code}")
            return True
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error triggering workflow: {e.response.status_code} - {e.response.text}")
            return False
        except Exception as e:
            logger.error(f"Error triggering workflow: {e}")
            return False

@app.post("/webhook")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: Optional[str] = Header(None),
    x_github_event: Optional[str] = Header(None)
):
    """Endpoint for GitHub webhooks."""
    # Read raw body for signature verification
    body_bytes = await request.body()
    
    # Verify signature if secret is configured
    if x_hub_signature_256 and not verify_webhook_signature(body_bytes, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    
    # Parse JSON payload
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    
    event_type = x_github_event or "unknown"
    logger.info(f"Received {event_type} event for {payload.get('repository', {}).get('full_name', 'unknown')}")
    
    # Extract context from payload
    context = extract_context_from_event(event_type, payload)
    
    # Generate task description
    task = generate_task_description(event_type, context)
    
    # Trigger workflow in background
    background_tasks.add_task(trigger_workflow_dispatch, task, context)
    
    return {
        "status": "processing",
        "event": event_type,
        "repo": context.repo,
        "task": task
    }

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "llm-bot-webhook-server"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)