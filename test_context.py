#!/usr/bin/env python3
"""
本地测试脚本：测试完整的 task 和 context 获取流程
目标：一次性抓取通知，获取 context 和 task，不调用 workflow
"""

import os
import json
import asyncio
import logging
import re
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime

import httpx
from pydantic import BaseModel

# --- 配置区 ---
GITHUB_API = "https://api.github.com/graphql"
REST_API = "https://api.github.com"

# 从环境变量读取 Token
BOT_TOKEN = os.getenv("BOT_TOKEN")     # 机器人Token：仅用于读取通知
GQL_TOKEN = os.getenv("GITHUB_TOKEN")      # 个人PAT：用于GraphQL查询

ALLOWED_USERS = [u.strip() for u in os.getenv("ALLOWED_USERS", "").split(",") if u.strip()]
BOT_HANDLE = "@WhiteElephantIsNotARobot"

# 上下文限制
CONTEXT_MAX_CHARS = int(os.getenv("CONTEXT_MAX_CHARS", "15000"))
DIFF_MAX_CHARS = int(os.getenv("DIFF_MAX_CHARS", "4000"))

# --- 日志配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("TestContext")

# --- 数据模型 (从 server.py 复制) ---
class TimelineItem(BaseModel):
    """时间线项目，统一表示评论、审核、审核评论等"""
    id: str
    body: str
    created_at: str
    user: str
    type: str  # 'comment', 'review', 'review_comment', 'issue', 'pr'
    # 可选字段
    path: Optional[str] = None
    diff_hunk: Optional[str] = None
    state: Optional[str] = None  # 对于review
    review_id: Optional[str] = None  # 对于review_comment

class TaskContext(BaseModel):
    """丰富的上下文数据模型"""
    # 基础信息
    repo: str
    event_type: str
    event_id: str
    trigger_user: Optional[str] = None
    issue_number: Optional[int] = None
    issue_body: Optional[str] = None  # Issue/PR/Discussion正文

    # 标题和描述
    title: Optional[str] = None
    pr_title: Optional[str] = None
    pr_body: Optional[str] = None
    discussion_title: Optional[str] = None
    discussion_body: Optional[str] = None

    # 历史数据
    comments_history: Optional[List[Dict]] = None
    reviews_history: Optional[List[Dict]] = None
    review_comments_batch: Optional[List[Dict]] = None

    # 代码上下文
    diff_content: Optional[str] = None
    diff_url: Optional[str] = None
    clone_url: Optional[str] = None
    head_ref: Optional[str] = None
    base_ref: Optional[str] = None
    head_repo: Optional[str] = None
    base_repo: Optional[str] = None
    commit_sha: Optional[str] = None

    # 元数据
    current_comment_id: Optional[str] = None
    current_review_id: Optional[str] = None
    is_mention_in_body: Optional[bool] = None
    is_mention_in_review: Optional[bool] = None
    is_truncated: Optional[bool] = None
    latest_comment_url: Optional[str] = None

    def to_json_string(self) -> str:
        """Convert context to JSON string for passing to workflow."""
        data = self.model_dump()
        cleaned_data = {}
        for key, value in data.items():
            if value is not None:
                if isinstance(value, (list, dict)) and not value:
                    continue
                if isinstance(value, str):
                    cleaned_data[key] = value
                else:
                    cleaned_data[key] = value
        return json.dumps(cleaned_data, ensure_ascii=False)

# --- 智能节选算法 (恢复早期版本的3:1算法) ---
def truncate_context_by_chars(items: List[TimelineItem], max_chars: int) -> Tuple[List[TimelineItem], bool]:
    """
    3新1老比例抓取 + 超限撤销 + 单边终止算法
    items: 按时间正序排列（索引0是最老，索引-1是最新）
    """
    if not items:
        return [], False

    selected_indices = set()
    total_chars = 0

    left_ptr = 0
    right_ptr = len(items) - 1

    left_active = True   # 老评论端状态
    right_active = True  # 新评论端状态

    while left_ptr <= right_ptr and (left_active or right_active):
        # --- 尝试抓取新评论 (最多3条) ---
        for _ in range(3):
            if right_active and left_ptr <= right_ptr:
                item_text = items[right_ptr].body
                if total_chars + len(item_text) <= max_chars:
                    selected_indices.add(right_ptr)
                    total_chars += len(item_text)
                    right_ptr -= 1
                else:
                    # 关键逻辑：撤销本次添加并锁死右侧
                    right_active = False
                    break

        # --- 尝试抓取老评论 (最多1条) ---
        if left_active and left_ptr <= right_ptr:
            item_text = items[left_ptr].body
            if total_chars + len(item_text) <= max_chars:
                selected_indices.add(left_ptr)
                total_chars += len(item_text)
                left_ptr += 1
            else:
                # 关键逻辑：撤销本次添加并锁死左侧
                left_active = False

    # --- 后序处理：排序并生成结果 ---
    sorted_indices = sorted(list(selected_indices))
    result = []

    # 确保至少包含最新的一条（如果触发点在最新）
    if not selected_indices and len(items) > 0:
        result = [items[-1]]
        selected_indices = {len(items) - 1}
        sorted_indices = [len(items) - 1]

    for i in range(len(sorted_indices)):
        idx = sorted_indices[i]
        result.append(items[idx])

        # 插入截断声明 (Gap Notice)
        if i < len(sorted_indices) - 1:
            next_idx = sorted_indices[i+1]
            if next_idx > idx + 1:
                omitted = next_idx - idx - 1
                gap_item = TimelineItem(
                    id=f"gap_{idx}_{next_idx}",
                    body=f"--- [系统提示: 此处省略了中间 {omitted} 条历史评论] ---",
                    created_at=items[idx].created_at,
                    user="system",
                    type="system_notice"
                )
                result.append(gap_item)

    is_truncated = len(selected_indices) < len(items)
    return result, is_truncated

def extract_pr_timeline_items(resource_data: Dict) -> List[TimelineItem]:
    """从PR资源数据中提取所有时间线项目"""
    timeline = []

    # 1. 普通评论（Issue评论）
    comments = resource_data.get("comments", {}).get("nodes", [])
    for c in comments:
        if c.get("body"):
            created_at = c.get("created_at", "")
            if not created_at:
                created_at = "1970-01-01T00:00:00Z"
            timeline.append(TimelineItem(
                id=str(c.get("id", "")),
                body=c.get("body", ""),
                created_at=created_at,
                user=c.get("author", {}).get("login", "unknown"),
                type="comment"
            ))

    # 2. 审核
    reviews = resource_data.get("reviews", {}).get("nodes", [])
    for r in reviews:
        if r.get("body"):
            # GraphQL返回的是submittedAt（驼峰命名），不是submitted_at（下划线命名）
            created_at = r.get("submittedAt", r.get("createdAt", ""))
            if not created_at:
                created_at = "1970-01-01T00:00:00Z"
            timeline.append(TimelineItem(
                id=str(r.get("id", "")),
                body=r.get("body", ""),
                created_at=created_at,
                user=r.get("author", {}).get("login", "unknown"),
                type="review",
                state=r.get("state")
            ))

    # 3. 审核评论（行内代码评论）
    review_threads = resource_data.get("reviewThreads", {}).get("nodes", [])
    for thread in review_threads:
        thread_comments = thread.get("comments", {}).get("nodes", [])
        for rc in thread_comments:
            if rc.get("body"):
                created_at = rc.get("created_at", "")
                if not created_at:
                    created_at = "1970-01-01T00:00:00Z"
                timeline.append(TimelineItem(
                    id=str(rc.get("id", "")),
                    body=rc.get("body", ""),
                    created_at=created_at,
                    user=rc.get("author", {}).get("login", "unknown"),
                    type="review_comment",
                    path=rc.get("path"),
                    diff_hunk=rc.get("diffHunk"),
                    review_id=str(rc.get("pullRequestReview", {}).get("id", "")) if rc.get("pullRequestReview") else None
                ))

    # 按时间排序
    timeline.sort(key=lambda x: x.created_at)

    # 调试信息
    if timeline:
        logger.info(f"Timeline items extracted: {len(timeline)} total")
        comment_count = sum(1 for item in timeline if item.type == "comment")
        review_count = sum(1 for item in timeline if item.type == "review")
        review_comment_count = sum(1 for item in timeline if item.type == "review_comment")
        logger.info(f"  Comments: {comment_count}, Reviews: {review_count}, Review Comments: {review_comment_count}")

        for i, item in enumerate(timeline[-3:]):
            logger.info(f"  [{len(timeline)-3+i}] {item.created_at[:19]} @{item.user} ({item.type}): {item.body[:50]}...")

    return timeline

def merge_comments_to_timeline(comments: List[Dict]) -> List[TimelineItem]:
    """将评论列表转换为时间线项目"""
    timeline = []
    for c in comments:
        if c.get("body"):
            created_at = c.get("created_at", "")
            if not created_at:
                created_at = "1970-01-01T00:00:00Z"
            timeline.append(TimelineItem(
                id=str(c.get("id", "")),
                body=c.get("body", ""),
                created_at=created_at,
                user=c.get("author", {}).get("login", "unknown"),
                type="comment"
            ))
    timeline.sort(key=lambda x: x.created_at)
    return timeline

# --- GraphQL查询 (增强版) ---
GQL_ENHANCED_QUERY = """
query($url: URI!, $commentsCount: Int = 50, $reviewsCount: Int = 30) {
  resource(url: $url) {
    __typename
    ... on PullRequest {
      title body number
      baseRepository { nameWithOwner }
      url
      headRefName baseRefName
      headRepository { url nameWithOwner }
      comments(last: $commentsCount) {
        nodes {
          id author { login } body createdAt
        }
      }
      reviewThreads(last: $reviewsCount) {
        nodes {
          comments(last: 10) {
            nodes {
              id author { login } body createdAt path diffHunk
              pullRequestReview { id }
            }
          }
        }
      }
      reviews(last: $reviewsCount) {
        nodes {
          id author { login } body createdAt submittedAt state
        }
      }
    }
    ... on Issue {
      title body number
      repository { nameWithOwner }
      url
      comments(last: $commentsCount) {
        nodes { id author { login } body createdAt }
      }
    }
    ... on Commit {
      message oid
      repository { nameWithOwner }
      url
      comments(last: $commentsCount) {
        nodes { id author { login } body createdAt path }
      }
    }
  }
}
"""

async def fetch_resource_details(client: httpx.AsyncClient, raw_url: str) -> Dict:
    """获取资源的详细信息"""
    subject_url = raw_url.replace("api.github.com/repos/", "github.com/")
    subject_url = subject_url.replace("/pulls/", "/pull/")
    subject_url = subject_url.rstrip('/')

    gql_headers = {"Authorization": f"Bearer {GQL_TOKEN.strip()}"}

    try:
        resp = await client.post(
            GITHUB_API,
            json={
                "query": GQL_ENHANCED_QUERY,
                "variables": {"url": subject_url, "commentsCount": 50, "reviewsCount": 30}
            },
            headers=gql_headers
        )

        if resp.status_code != 200:
            logger.error(f"GraphQL HTTP Error {resp.status_code}: {resp.text}")
            return None

        json_resp = resp.json()
        data = json_resp.get("data", {}).get("resource")

        if not data:
            errors = json_resp.get("errors", [])
            logger.warning(f"No resource found for URL: {subject_url}, errors: {errors}")
            return None

        return data

    except Exception as e:
        logger.error(f"Exception during GraphQL call: {e}")
        return None

async def fetch_diff_content(client: httpx.AsyncClient, raw_url: str) -> str:
    """获取PR的diff内容"""
    if "/pulls/" in raw_url or "/pull/" in raw_url:
        diff_url = raw_url.replace("/issues/", "/pulls/").replace("/pull/", "/pulls/")
        try:
            headers = {"Authorization": f"token {GQL_TOKEN.strip()}", "Accept": "application/vnd.github.v3.diff"}
            resp = await client.get(diff_url, headers=headers)
            if resp.status_code == 200:
                return resp.text[:DIFF_MAX_CHARS]
        except Exception as e:
            logger.warning(f"Failed to fetch diff: {e}")

    return ""

def find_trigger_node(nodes: List[TimelineItem], trigger_node_id: str = None) -> Tuple[Optional[TimelineItem], List[TimelineItem]]:
    """寻找触发节点"""
    if trigger_node_id:
        for node in nodes:
            if node.id == trigger_node_id:
                return node, nodes
    else:
        for node in reversed(nodes):
            if node.body and BOT_HANDLE.lower() in node.body.lower():
                logger.info(f"Found trigger node: {node.id} by @{node.user} (type: {node.type})")
                return node, nodes

    return None, nodes

def build_rich_context(
    resource_data: Dict,
    timeline_items: List[TimelineItem],
    trigger_node: Optional[TimelineItem],
    raw_url: str,
    note_id: str
) -> TaskContext:
    """构建丰富的上下文数据"""
    resource_type = resource_data.get("__typename")
    repo_full = ""

    # 获取仓库信息
    if resource_type == "PullRequest":
        repo_full = resource_data.get("baseRepository", {}).get("nameWithOwner", "")
    elif resource_type == "Issue":
        repo_full = resource_data.get("repository", {}).get("nameWithOwner", "")
    elif resource_type == "Commit":
        repo_full = resource_data.get("repository", {}).get("nameWithOwner", "")

    # 如果无法从GraphQL获取repo信息，尝试从URL解析
    if not repo_full and raw_url:
        try:
            match = re.search(r'repos/([^/]+/[^/]+)', raw_url)
            if match:
                repo_full = match.group(1)
        except:
            pass

    # 基础信息
    issue_body_value = None
    title_value = None
    if resource_type == "PullRequest":
        issue_body_value = None
        # 对于PR，title应该为空，使用pr_title
        title_value = None
    else:
        issue_body_value = resource_data.get("body", "")[:3000] if resource_data.get("body") else None
        title_value = resource_data.get("title")

    context = TaskContext(
        repo=repo_full,
        event_type=resource_type.lower(),
        event_id=note_id,
        issue_number=resource_data.get("number"),
        title=title_value,
        issue_body=issue_body_value,
        clone_url=f"git@github.com:{repo_full}.git" if repo_full else None
    )

    # 特定类型的信息
    if resource_type == "PullRequest":
        context.pr_title = resource_data.get("title")
        context.pr_body = resource_data.get("body", "")[:3000] if resource_data.get("body") else None
        context.head_ref = resource_data.get("headRefName")
        context.base_ref = resource_data.get("baseRefName")
        context.diff_url = raw_url.replace("/issues/", "/pulls/") + ".diff"

        # 获取PR分支仓库信息
        head_repo = resource_data.get("headRepository", {})
        repo_path = None

        # 优先使用nameWithOwner字段
        if head_repo and head_repo.get("nameWithOwner"):
            repo_path = head_repo.get("nameWithOwner")
        # 如果没有nameWithOwner，尝试从url解析
        elif head_repo and head_repo.get("url"):
            api_url = head_repo.get("url")
            if "api.github.com/repos/" in api_url:
                repo_match = re.search(r'repos/([^/]+/[^/]+)', api_url)
                if repo_match:
                    repo_path = repo_match.group(1)

        if repo_path:
            clone_url = f"git@github.com:{repo_path}.git"
            context.clone_url = clone_url
            # 设置 head_repo 为 repo:branch 格式
            if context.head_ref:
                context.head_repo = f"{repo_path}:{context.head_ref}"
            logger.info(f"PR branch clone_url (SSH): {clone_url}")
            logger.info(f"PR head_repo (repo:branch): {context.head_repo}")

        # 获取基础仓库信息
        base_repo = resource_data.get("baseRepository", {})
        if base_repo and base_repo.get("nameWithOwner"):
            base_repo_name = base_repo.get("nameWithOwner")
            if context.base_ref:
                context.base_repo = f"{base_repo_name}:{context.base_ref}"
                logger.info(f"PR base_repo (repo:branch): {context.base_repo}")

        if context.pr_body:
            context.is_mention_in_body = BOT_HANDLE.lower() in context.pr_body.lower()

    elif resource_type == "Issue":
        if context.issue_body:
            context.is_mention_in_body = BOT_HANDLE.lower() in context.issue_body.lower()

    elif resource_type == "Commit":
        context.commit_sha = resource_data.get("oid")
        context.title = resource_data.get("message", "")[:200]

    # 触发者信息
    if trigger_node:
        context.trigger_user = trigger_node.user
        context.current_comment_id = trigger_node.id
        context.current_review_id = trigger_node.review_id

        if trigger_node.type == "review":
            context.is_mention_in_review = True

        logger.info(f"Trigger message: '{trigger_node.body[:100]}{'...' if len(trigger_node.body) > 100 else ''}'")
        logger.info(f"Trigger node type: {trigger_node.type}")

    # 分离评论历史
    if timeline_items:
        logger.info(f"Applying smart truncation to {len(timeline_items)} timeline items (max: {CONTEXT_MAX_CHARS} chars)")
        truncated_items, is_truncated = truncate_context_by_chars(timeline_items, CONTEXT_MAX_CHARS)
        context.is_truncated = is_truncated
        logger.info(f"Truncation result: {len(truncated_items)} items selected (truncated: {is_truncated})")

        comments_history = []
        reviews_history = []
        review_comments_batch = []

        for item in truncated_items:
            trigger_type = trigger_node.type if trigger_node else None

            if trigger_type in ["review", "review_comment"]:
                if item.type == "review":
                    # 只保留与当前review相关的review
                    # 如果trigger_node是review本身，其id就是review_id
                    # 如果trigger_node是review_comment，其review_id字段就是所属review
                    trigger_review_id = trigger_node.review_id if trigger_node.review_id else trigger_node.id
                    if item.id == trigger_review_id:
                        reviews_history.append({
                            "id": item.id,
                            "user": item.user,
                            "body": item.body,
                            "state": item.state,
                            "submitted_at": item.created_at
                        })
                        logger.info(f"Including review {item.id} for review {trigger_review_id}")
                elif item.type == "review_comment" and item.review_id:
                    trigger_review_id = trigger_node.review_id if trigger_node.review_id else trigger_node.id
                    if item.review_id == trigger_review_id:
                        review_comments_batch.append({
                            "id": item.id,
                            "user": item.user,
                            "body": item.body,
                            "path": item.path,
                            "diff_hunk": item.diff_hunk
                        })
                        logger.info(f"Including review comment {item.id} for review {trigger_review_id}")
            else:
                # 普通触发（comment）：只处理comment和review，不处理review_comment
                if item.type == "comment":
                    comments_history.append({
                        "id": item.id,
                        "user": item.user,
                        "body": item.body,
                        "created_at": item.created_at,
                        "type": item.type
                    })
                elif item.type == "review":
                    reviews_history.append({
                        "id": item.id,
                        "user": item.user,
                        "body": item.body,
                        "state": item.state,
                        "submitted_at": item.created_at
                    })

        if comments_history:
            context.comments_history = comments_history
        if reviews_history:
            context.reviews_history = reviews_history
        if review_comments_batch:
            context.review_comments_batch = review_comments_batch

    return context

def generate_task_description(event_type: str, context: TaskContext, trigger_node: TimelineItem) -> str:
    """生成LLM任务描述：始终保留原始触发消息（包括@机器人）"""
    trigger_message = trigger_node.body

    # 检查是否是"空提及"
    is_empty_mention = False
    if trigger_message:
        pattern = re.compile(re.escape(BOT_HANDLE), re.IGNORECASE)
        cleaned = pattern.sub("", trigger_message).strip()

        if not cleaned:
            # 无论在哪里，只要包含@就是有效内容
            is_empty_mention = False
        else:
            punctuation_pattern = r'^[\s\.,!?。，！？;:;：\-—~～、]*$'
            if re.match(punctuation_pattern, cleaned):
                is_empty_mention = True
            elif len(cleaned) <= 4:
                short_valid_responses = ["ok", "好的", "行", "yes", "no", "好", "okay", "收到", "roger", "copy"]
                if cleaned.lower() not in short_valid_responses and cleaned not in short_valid_responses:
                    if not re.match(r'^[a-zA-Z0-9]+$', cleaned):
                        is_empty_mention = True

    # 情况1：空提及或没有实际内容 -> 生成智能默认描述
    if is_empty_mention or not trigger_message:
        if event_type == "PullRequest":
            if context.diff_content and len(context.diff_content) > 100:
                return f"Please review the code changes in PR #{context.issue_number}: {context.pr_title or 'No title'}"
            else:
                return f"Please review PR #{context.issue_number}: {context.pr_title or 'No title'}"
        elif event_type == "Issue":
            return f"Please analyze issue #{context.issue_number}: {context.title or 'No title'}"
        elif event_type == "Commit":
            return f"Please review commit {context.commit_sha[:8] if context.commit_sha else 'unknown'}: {context.title or 'No message'}"
        else:
            return f"Please process this {event_type}"

    # 情况2：有实际内容的触发消息 -> 直接返回原始消息
    # 重要：保留原始@指令，让LLM知道这是直接针对它的请求
    return trigger_message

async def fetch_unread_notifications(client: httpx.AsyncClient) -> List[Dict]:
    """获取未读通知"""
    try:
        r = await client.get(
            f"{REST_API}/notifications",
            params={"participating": "true", "all": "false"},
            headers={"Authorization": f"token {BOT_TOKEN.strip()}"}
        )

        if r.status_code == 200:
            notes = r.json()
            logger.info(f"Fetched {len(notes)} unread notifications.")
            return notes
        elif r.status_code == 304:
            logger.info("No changes in notifications (304).")
            return []
        elif r.status_code == 403:
            logger.warning("Rate limit hit or forbidden.")
            return []
        else:
            logger.error(f"Notification API Error {r.status_code}: {r.text}")
            return []
    except Exception as e:
        logger.error(f"Exception fetching notifications: {e}")
        return []

async def process_notification(client: httpx.AsyncClient, note: Dict) -> Tuple[Optional[TaskContext], Optional[str]]:
    """处理单个通知，返回 context 和 task"""
    thread_id = note["id"]
    raw_url = note["subject"].get("url")

    if not raw_url:
        logger.warning(f"Empty URL in notification: {note}")
        return None, None

    logger.info(f"Processing notification: {note['subject']['title']} ({raw_url})")

    # 1. 获取资源详情
    resource_data = await fetch_resource_details(client, raw_url)
    if not resource_data:
        logger.warning(f"Failed to fetch resource details for: {raw_url}")
        return None, None

    # 2. 构建时间线
    timeline_items = []

    if resource_data["__typename"] == "PullRequest":
        timeline_items = extract_pr_timeline_items(resource_data)
        logger.info(f"Extracted {len(timeline_items)} timeline items for PR #{resource_data.get('number')}")

    elif resource_data["__typename"] == "Issue":
        comments = resource_data.get("comments", {}).get("nodes", [])
        timeline_items = merge_comments_to_timeline(comments)

    elif resource_data["__typename"] == "Commit":
        comments = resource_data.get("comments", {}).get("nodes", [])
        for c in comments:
            if c.get("body"):
                created_at = c.get("created_at", "")
                if not created_at:
                    created_at = "1970-01-01T00:00:00Z"
                timeline_items.append(TimelineItem(
                    id=str(c.get("id", "")),
                    body=c.get("body", ""),
                    created_at=created_at,
                    user=c.get("author", {}).get("login", "unknown"),
                    type="comment",
                    path=c.get("path"),
                    diff_hunk=None
                ))

    # 3. 寻找触发节点
    trigger_node = None
    if note["subject"].get("latest_comment_url"):
        try:
            lc_resp = await client.get(
                note["subject"]["latest_comment_url"],
                headers={"Authorization": f"token {BOT_TOKEN.strip()}"}
            )
            if lc_resp.status_code == 200:
                lc_data = lc_resp.json()
                trigger_node_id = str(lc_data.get("id", ""))
                trigger_node, _ = find_trigger_node(timeline_items, trigger_node_id)
        except Exception as e:
            logger.warning(f"Failed to fetch latest comment: {e}")

    if not trigger_node:
        trigger_node, _ = find_trigger_node(timeline_items)

    # 如果还是没有找到触发节点，检查issue body是否包含@机器人
    if not trigger_node and resource_data["__typename"] == "Issue":
        issue_body = resource_data.get("body", "")
        if issue_body and BOT_HANDLE.lower() in issue_body.lower():
            trigger_node = TimelineItem(
                id=f"issue_body_{resource_data.get('id', '')}",
                body=issue_body,
                created_at=resource_data.get("created_at", "1970-01-01T00:00:00Z"),
                user=resource_data.get("author", {}).get("login", "unknown"),
                type="issue_body"
            )
            logger.info(f"Found trigger node in issue body: {trigger_node.id} by @{trigger_node.user} (type: {trigger_node.type})")

    # 4. 权限检查
    if not trigger_node:
        logger.info(f"No trigger node found for notification {thread_id}")
        return None, None

    if ALLOWED_USERS and trigger_node.user not in ALLOWED_USERS:
        logger.warning(f"User {trigger_node.user} not in ALLOWED_USERS. Skipping.")
        return None, None

    # 5. 构建完整上下文
    context = build_rich_context(resource_data, timeline_items, trigger_node, raw_url, thread_id)

    # 6. 获取diff内容（根据触发类型决定）
    if resource_data["__typename"] in ["PullRequest", "Commit"]:
        if trigger_node and trigger_node.type in ["review", "review_comment"]:
            logger.info("Review/review_comment trigger detected, skipping full PR diff")
        else:
            diff_content = await fetch_diff_content(client, raw_url)
            if diff_content:
                context.diff_content = diff_content
                logger.info(f"Full diff fetched: {len(diff_content)} chars")
            else:
                logger.info("No diff content available")

    # 7. 生成任务描述
    task_description = generate_task_description(resource_data["__typename"], context, trigger_node)

    return context, task_description

async def main():
    """主测试函数"""
    # 检查环境变量
    if not GQL_TOKEN:
        logger.error("GQL_TOKEN environment variable is not set!")
        logger.error("Please set GQL_TOKEN with a GitHub Personal Access Token that has 'repo' scope.")
        return

    if not BOT_TOKEN:
        logger.warning("BOT_TOKEN environment variable is not set!")

    logger.info(f"ALLOWED_USERS: {ALLOWED_USERS}")
    logger.info(f"CONTEXT_MAX_CHARS: {CONTEXT_MAX_CHARS}")
    logger.info(f"DIFF_MAX_CHARS: {DIFF_MAX_CHARS}")

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. 获取未读通知
        notifications = await fetch_unread_notifications(client)

        if not notifications:
            logger.info("No notifications to process.")
            return

        # 2. 处理所有通知（一次性，不循环）
        all_results = []
        for i, note in enumerate(notifications):
            logger.info(f"\n{'='*80}")
            logger.info(f"Processing notification {i+1}/{len(notifications)}: {note['subject']['title']}")
            logger.info(f"{'='*80}\n")

            context, task = await process_notification(client, note)

            if context and task:
                logger.info(f"\n{'='*80}")
                logger.info(f"TEST RESULTS - Notification {i+1}/{len(notifications)}")
                logger.info(f"{'='*80}\n")

                # 输出 Task
                logger.info("=== TASK ===")
                logger.info(task)
                logger.info("")

                # 输出 Context (JSON)
                logger.info("=== CONTEXT (JSON) ===")
                context_json = context.to_json_string()
                logger.info(context_json)
                logger.info("")

                # 输出 Context 统计信息
                logger.info("=== CONTEXT STATISTICS ===")
                logger.info(f"Context JSON size: {len(context_json)} chars")
                logger.info(f"Repo: {context.repo}")
                logger.info(f"Event Type: {context.event_type}")
                logger.info(f"Issue Number: {context.issue_number}")
                logger.info(f"Trigger User: {context.trigger_user}")
                logger.info(f"Trigger Node ID: {context.current_comment_id}")
                logger.info(f"Trigger Node Type: {context.current_comment_id}")
                logger.info(f"Is Truncated: {context.is_truncated}")

                if context.comments_history:
                    logger.info(f"Comments History: {len(context.comments_history)} items")
                if context.reviews_history:
                    logger.info(f"Reviews History: {len(context.reviews_history)} items")
                if context.review_comments_batch:
                    logger.info(f"Review Comments Batch: {len(context.review_comments_batch)} items")
                if context.diff_content:
                    logger.info(f"Diff Content: {len(context.diff_content)} chars")

                # 保存结果
                all_results.append({
                    "task": task,
                    "context": json.loads(context_json),
                    "timestamp": datetime.now().isoformat(),
                    "notification": {
                        "id": note["id"],
                        "title": note["subject"]["title"],
                        "url": note["subject"].get("url")
                    }
                })
            else:
                logger.error(f"Failed to generate context and task for notification {i+1}.")

        # 3. 保存所有结果到文件
        if all_results:
            output_file = "test_output.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(all_results, f, ensure_ascii=False, indent=2)
            logger.info(f"\nResults saved to: {output_file}")
            logger.info(f"Total notifications processed: {len(all_results)}")

if __name__ == "__main__":
    asyncio.run(main())
