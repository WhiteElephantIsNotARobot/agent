import os, json, logging, asyncio
from typing import Dict, List
from fastapi import FastAPI
import httpx

# --- 配置区 ---
GITHUB_API = "https://api.github.com/graphql"
REST_API = "https://api.github.com"

BOT_TOKEN = os.getenv("BOT_TOKEN")
GQL_TOKEN = os.getenv("GQL_TOKEN")
CONTROL_REPO = os.getenv("CONTROL_REPO")
ALLOWED_USERS = [u.strip() for u in os.getenv("ALLOWED_USERS", "").split(",") if u.strip()]
BOT_HANDLE = "@WhiteElephantIsNotARobot"
LOG_FILE = os.getenv("PROCESSED_LOG", "/data/processed_notifications.log")

# --- 处理状态跟踪 ---
# 跟踪正在处理的通知，防止在短时间内重复处理相同的 thread_id
from threading import Lock
processing_in_progress = set()
processing_lock = Lock()

# --- 日志配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("GQLBot")

# --- 持久化逻辑 ---
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

# 检查目录是否可写
dir_path = os.path.dirname(LOG_FILE)
if not os.access(dir_path, os.W_OK):
    logger.error(f"Directory {dir_path} is not writable! Processed IDs will not be persisted!")

if os.path.exists(LOG_FILE):
    try:
        with open(LOG_FILE, "r") as f:
            processed_cache = {line.strip() for line in f if line.strip()}
        logger.info(f"Loaded {len(processed_cache)} processed IDs from {LOG_FILE}")
    except Exception as e:
        logger.error(f"Failed to read log file {LOG_FILE}: {e}")
        processed_cache = set()
else:
    processed_cache = set()
    logger.info(f"No log file found at {LOG_FILE}, starting with empty cache.")
    
    # 尝试创建测试文件以验证写入权限
    try:
        with open(LOG_FILE, "w") as f:
            f.write("# Test write\n")
        os.remove(LOG_FILE)
        logger.info(f"Write permission test passed for {LOG_FILE}")
    except Exception as e:
        logger.error(f"Write permission test FAILED for {LOG_FILE}: {e}")
        logger.error(f"Processed IDs will NOT be saved. The bot will re-process mentions on every restart!")

async def save_to_log(node_id: str):
    if node_id not in processed_cache:
        processed_cache.add(node_id)
        try:
            with open(LOG_FILE, "a") as f:
                f.write(f"{node_id}\n")
                f.flush()  # 立即刷新到磁盘
                os.fsync(f.fileno())  # 确保写入磁盘
            logger.info(f"Successfully logged node_id: {node_id} to {LOG_FILE}")
            logger.info(f"Current processed_cache size: {len(processed_cache)}")
        except Exception as e:
            logger.error(f"CRITICAL: Failed to write to log file {LOG_FILE}: {e}")
            logger.error(f"This node_id {node_id} will be re-processed in the next poll cycle!")
            # 从缓存中移除，这样下次还会尝试处理
            processed_cache.discard(node_id)

app = FastAPI()
# 【修正】删除了此处重复的 processed_cache = set()

# --- GraphQL 查询语句 ---
GQL_UNIVERSAL_QUERY = """
query($url: URI!) {
  resource(url: $url) {
    __typename
    ... on PullRequest {
      title body number
      baseRepository { nameWithOwner }
      comments(last: 50) {
        nodes { id author { login } body createdAt }
      }
      reviews(last: 50) {
        nodes { 
          id author { login } body createdAt 
          comments(last: 50) {
            nodes { id author { login } body path diffHunk createdAt }
          }
        }
      }
    }
    ... on Issue {
      title body number
      repository { nameWithOwner }
      comments(last: 50) {
        nodes { id author { login } body createdAt }
      }
    }
    ... on Commit {
      message oid
      repository { nameWithOwner }
      comments(last: 30) {
        nodes { id author { login } body path createdAt }
      }
    }
  }
}
"""

async def handle_notification(client: httpx.AsyncClient, note: Dict):
    thread_id = note["id"]
    
    # 检查是否正在处理这个 thread_id
    with processing_lock:
        if thread_id in processing_in_progress:
            logger.info(f"Thread {thread_id} is already being processed, skipping.")
            return
        processing_in_progress.add(thread_id)
        logger.debug(f"Added thread {thread_id} to processing_in_progress (size: {len(processing_in_progress)})")
    
    try:
        # 原始 URL 是 REST 格式: https://api.github.com/repos/owner/repo/issues/19
        raw_url = note["subject"].get("url")
        
        logger.info(f"Raw URL from notification: {raw_url}")
        
        # Check if URL is None or empty
        if not raw_url:
            logger.warning(f"Empty URL in notification: {note}")
            return
    
        # 【核心修复】转换为 GraphQL 认可的 HTML 格式
        # 1. 把 api.github.com/repos 换成 github.com
        # 2. 把 /pulls/ 换成 /pull/ (Web 端 PR 的路径是单数)
        subject_url = raw_url.replace("api.github.com/repos/", "github.com/")
        subject_url = subject_url.replace("/pulls/", "/pull/")
        
        # Remove trailing slash if present
        subject_url = subject_url.rstrip('/')

        logger.info(f"Processing: {note['subject']['title']} -> GQL URL: {subject_url}")
            
        # Debug token info (mask token for security)
        token_preview = GQL_TOKEN[:8] + "..." + GQL_TOKEN[-4:] if GQL_TOKEN else "None"
        logger.info(f"Using GQL_TOKEN: {token_preview}")

        gql_headers = {"Authorization": f"Bearer {GQL_TOKEN}"}
        try:
            # 发送转换后的 subject_url
            logger.debug(f"GraphQL query: {GQL_UNIVERSAL_QUERY}")
            logger.debug(f"GraphQL variables: {{'url': {subject_url}}}")
            resp = await client.post(GITHUB_API, json={"query": GQL_UNIVERSAL_QUERY, "variables": {"url": subject_url}}, headers=gql_headers)
            if resp.status_code != 200:
                logger.error(f"GQL HTTP Error {resp.status_code}, body: {resp.text}")
                return

            json_resp = resp.json()
            data = json_resp.get("data", {}).get("resource")
            if not data:
                # 如果还报错，这里会打印出转换后的 URL，方便排查
                errors = json_resp.get("errors", [])
                logger.warning(f"No resource found for URL: {subject_url}")
                logger.warning(f"GraphQL errors: {errors}")
                logger.warning(f"Full response: {json_resp}")
                return
        except Exception as e:
            logger.error(f"Exception during GQL call: {e}")
            return

        # 根据资源类型收集所有评论节点
        nodes = []
        if data["__typename"] == "PullRequest":
            # 收集 PR 评论
            if data.get("comments") and data["comments"].get("nodes"):
                nodes.extend(data["comments"]["nodes"])
            
            # 收集审核（reviews）和审核评论（review comments）
            if data.get("reviews") and data["reviews"].get("nodes"):
                for review in data["reviews"]["nodes"]:
                    # 添加审核对象本身作为节点（如果包含评论内容）
                    if review and review.get("body"):
                        review_copy = review.copy()
                        review_copy["__typename"] = "PullRequestReview"
                        review_copy["author"] = review.get("author", {})
                        nodes.append(review_copy)
                    
                    # 收集审核的评论
                    if review.get("comments") and review["comments"].get("nodes"):
                        nodes.extend(review["comments"]["nodes"])
        
        elif data.get("comments") and data["comments"].get("nodes"):
            # 处理 Issue 和 Commit
            nodes = data["comments"]["nodes"]

        # 过滤掉空节点并匹配
        new_mentions = [
            n for n in nodes
            if n and n.get("body") and BOT_HANDLE.lower() in n["body"].lower()
            and n.get("id") not in processed_cache
        ]

        logger.info(f"Found {len(nodes)} total nodes, {len(new_mentions)} new mentions.")

        if not new_mentions:
            # 如果确实没搜到指令，才标记已读。建议调试阶段先注释掉下面这行，防止吞通知。
            # await client.patch(f"{REST_API}/notifications/threads/{thread_id}", headers={"Authorization": f"token {BOT_TOKEN}"}")
            return

        for node in new_mentions:
            trigger_user = node["author"]["login"]
            if ALLOWED_USERS and trigger_user not in ALLOWED_USERS:
                logger.warning(f"User {trigger_user} not in ALLOWED_USERS. Skipping.")
                continue

            context = {
                "type": data["__typename"],
                "node_id": node["id"],
                "trigger_user": trigger_user,
                "task_body": node["body"],
                "diff_context": node.get("diffHunk") or ""
            }

            if not context["diff_context"] and data["__typename"] in ["PullRequest", "Commit"]:
                diff_url = subject_url.replace("/issues/", "/pulls/")
                try:
                    dr = await client.get(diff_url, headers={"Authorization": f"token {GQL_TOKEN}", "Accept": "application/vnd.github.v3.diff"})
                    if dr.status_code == 200: context["diff_context"] = dr.text[:20000]
                except Exception: pass

            success = await trigger_workflow(client, context, thread_id)
            if success:
                await save_to_log(node["id"])
    
    finally:
        # 确保在处理完成后移除线程ID
        with processing_lock:
            if thread_id in processing_in_progress:
                processing_in_progress.remove(thread_id)
                logger.debug(f"Removed thread {thread_id} from processing_in_progress (size: {len(processing_in_progress)})")

async def trigger_workflow(client: httpx.AsyncClient, ctx: Dict, thread_id: str) -> bool:
    url = f"{REST_API}/repos/{CONTROL_REPO}/actions/workflows/llm-bot-runner.yml/dispatches"
    headers = {"Authorization": f"token {GQL_TOKEN}", "Accept": "application/vnd.github.v3+json"}

    payload = {
        "ref": "main",
        "inputs": {
            "task": ctx["task_body"][:4000],
            "context": json.dumps(ctx, ensure_ascii=False)
        }
    }

    r = await client.post(url, headers=headers, json=payload)
    if r.status_code == 204:
        logger.info(f"Successfully triggered Action for Node {ctx['node_id']}")
        # 触发成功后再标记已读
        await client.patch(f"{REST_API}/notifications/threads/{thread_id}", headers={"Authorization": f"token {BOT_TOKEN}"})
        return True
    else:
        logger.error(f"Workflow dispatch failed ({r.status_code}): {r.text}")
        return False

async def poll_loop():
    async with httpx.AsyncClient() as client:
        logger.info("Poll loop started...")
        while True:
            try:
                r = await client.get(f"{REST_API}/notifications", params={"participating": "true"},
                                    headers={"Authorization": f"token {BOT_TOKEN}"})
                if r.status_code == 200:
                    notes = r.json()
                    if notes:
                        logger.info(f"Fetched {len(notes)} unread notifications.")
                        tasks = [handle_notification(client, n) for n in notes]
                        await asyncio.gather(*tasks)
                elif r.status_code != 304:
                    logger.error(f"Notification API Error {r.status_code}: {r.text}")
            except Exception as e:
                logger.error(f"Poll loop exception: {e}")
            await asyncio.sleep(30)

@app.on_event("startup")
async def startup():
    # 配置验证
    if not GQL_TOKEN:
        logger.error("GQL_TOKEN environment variable is not set!")
        logger.error("Please set GQL_TOKEN with a GitHub Personal Access Token that has 'repo' scope.")
    else:
        token_preview = GQL_TOKEN[:8] + "..." + GQL_TOKEN[-4:]
        logger.info(f"GQL_TOKEN is set: {token_preview}")
    
    if not BOT_TOKEN:
        logger.warning("BOT_TOKEN environment variable is not set!")
    
    if not CONTROL_REPO:
        logger.warning("CONTROL_REPO environment variable is not set!")
    else:
        logger.info(f"CONTROL_REPO: {CONTROL_REPO}")
    
    logger.info(f"ALLOWED_USERS: {ALLOWED_USERS}")
    logger.info(f"LOG_FILE: {LOG_FILE}")
    
    asyncio.create_task(poll_loop())
