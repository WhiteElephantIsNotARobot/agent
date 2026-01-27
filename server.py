import os, json, logging, asyncio
from typing import Dict, List, Optional
from fastapi import FastAPI
import httpx

# --- 配置区 ---
GITHUB_API = "https://api.github.com/graphql"
REST_API = "https://api.github.com"
# 建议将 TOKEN 统一，或确保权限覆盖 repo, notifications, discussion
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") 
CONTROL_REPO = os.getenv("CONTROL_REPO")
ALLOWED_USERS = [u.strip() for u in os.getenv("ALLOWED_USERS", "").split(",") if u.strip()]
BOT_HANDLE = "@WhiteElephantIsNotARobot"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("GQLBot")

app = FastAPI()
# 缓存 (thread_id + updated_at) 确保同一处多次艾特也能触发
processed_cache = set()

# --- GraphQL 万能查询 (覆盖 PR, Issue, Commit, Discussion) ---
GQL_UNIVERSAL_QUERY = """
query($url: URI!) {
  resource(url: $url) {
    __typename
    ... on PullRequest {
      title body number
      baseRepository { nameWithOwner }
      timelineItems(last: 30, itemTypes: [ISSUE_COMMENT, PULL_REQUEST_REVIEW, PULL_REQUEST_REVIEW_COMMENT]) {
        nodes {
          __typename
          ... on IssueComment { id author { login } body createdAt }
          ... on PullRequestReview { id author { login } body createdAt }
          ... on PullRequestReviewComment { 
            id author { login } body createdAt 
            pullRequestReview { id } 
            path diffHunk 
          }
        }
      }
    }
    ... on Issue {
      title body number
      baseRepository { nameWithOwner }
      timelineItems(last: 20, itemTypes: [ISSUE_COMMENT]) {
        nodes { ... on IssueComment { id author { login } body createdAt } }
      }
    }
    ... on Commit {
      message oid
      repository { nameWithOwner }
      comments(last: 20) {
        nodes { id author { login } body path diffHunk createdAt }
      }
    }
    ... on Discussion {
      title body number
      repository { nameWithOwner }
      comments(last: 20) {
        nodes { 
          id author { login } body createdAt
          replies(last: 10) { nodes { id author { login } body createdAt } }
        }
      }
    }
  }
}
"""

# --- 核心处理逻辑 ---

async def handle_notification(client: httpx.AsyncClient, note: Dict):
    thread_id = note["id"]
    subject_url = note["subject"]["url"]
    
    # 1. 使用 resource(url) 反查所有上下文
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}"}
    resp = await client.post(GITHUB_API, json={"query": GQL_UNIVERSAL_QUERY, "variables": {"url": subject_url}}, headers=headers)
    if resp.status_code != 200: return
    
    data = resp.json().get("data", {}).get("resource")
    if not data: return

    # 2. 展平所有评论节点 (将 Timeline 或 Comments 统一化)
    nodes = []
    if "timelineItems" in data:
        nodes = data["timelineItems"]["nodes"]
    elif "comments" in data:
        nodes = data["comments"]["nodes"]
        # 处理 Discussion 的楼中楼
        if data["__typename"] == "Discussion":
            expanded = []
            for c in nodes:
                expanded.append(c)
                if c.get("replies"): expanded.extend(c["replies"]["nodes"])
            nodes = expanded

    # 3. 逆序查找最新一条提及机器人的原子节点 (0误差判定)
    trigger_node = None
    for node in reversed(nodes):
        if BOT_HANDLE.lower() in (node.get("body") or "").lower():
            trigger_node = node
            break

    if not trigger_node:
        logger.info(f"Thread {thread_id}: No explicit mention in nodes.")
        return

    trigger_user = trigger_node["author"]["login"]
    if ALLOWED_USERS and trigger_user not in ALLOWED_USERS:
        logger.warning(f"Unauthorized: {trigger_user}")
        return

    # 4. 构建上下文与精准 Diff 抓取
    context = {
        "type": data["__typename"],
        "title": data.get("title") or data.get("message", "N/A"),
        "trigger_user": trigger_user,
        "task_body": trigger_node["body"],
        "diff_context": ""
    }

    # 优先级 A: 如果是代码行 Review，抓取精准 diffHunk
    if "diffHunk" in trigger_node and trigger_node["diffHunk"]:
        context["diff_context"] = f"### File: {trigger_node.get('path')}\n{trigger_node['diffHunk']}"
    
    # 优先级 B: 如果是 Review 批次，尝试获取该批次所有代码片段
    elif "pullRequestReview" in trigger_node or data["__typename"] == "PullRequestReview":
        # 此处可根据需要进一步递归抓取 batch，为了简洁，这里逻辑回退到文件级
        pass

    # 优先级 C: 回退到 REST 获取全量 Diff (针对 Issue 或全局 PR 指令)
    if not context["diff_context"] and data["__typename"] in ["PullRequest", "Commit"]:
        context["diff_context"] = await fetch_rest_diff(client, subject_url)

    # 5. 触发 Workflow
    await trigger_workflow(client, context, thread_id)

async def fetch_rest_diff(client: httpx.AsyncClient, url: str) -> str:
    # 针对 PR 或 Commit 路径转换，获取 Unified Diff
    diff_url = url.replace("/issues/", "/pulls/")
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3.diff"}
    r = await client.get(diff_url, headers=headers)
    return r.text[:20000] if r.status_code == 200 else "No code context."

async def trigger_workflow(client: httpx.AsyncClient, ctx: Dict, thread_id: str):
    dispatch_url = f"{REST_API}/repos/{CONTROL_REPO}/actions/workflows/llm-bot-runner.yml/dispatches"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    
    payload = {
        "ref": "main",
        "inputs": {
            "task": ctx["task_body"][:4000],
            "context": json.dumps(ctx, ensure_ascii=False)
        }
    }
    r = await client.post(dispatch_url, headers=headers, json=payload)
    if r.status_code == 204:
        logger.info(f"Workflow triggered for {ctx['trigger_user']} on {ctx['type']}")
        # 标记已读
        await client.patch(f"{REST_API}/notifications/threads/{thread_id}", headers=headers)

# --- 轮询器 ---
async def poll_loop():
    async with httpx.AsyncClient() as client:
        while True:
            try:
                # 获取参与的未读通知
                r = await client.get(f"{REST_API}/notifications", params={"participating": "true"}, 
                                    headers={"Authorization": f"token {GITHUB_TOKEN}"})
                if r.status_code == 200:
                    notes = r.json()
                    tasks = []
                    for n in notes:
                        key = f"{n['id']}_{n['updated_at']}"
                        if key not in processed_cache:
                            processed_cache.add(key)
                            tasks.append(handle_notification(client, n))
                    
                    if tasks:
                        # 并行处理不同的 Issue/PR 通知
                        await asyncio.gather(*tasks)
            except Exception as e:
                logger.error(f"Poll loop error: {e}")
            await asyncio.sleep(30)

@app.on_event("startup")
async def startup():
    asyncio.create_task(poll_loop())