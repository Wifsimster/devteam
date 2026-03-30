import asyncio
import json
import hmac
import aiohttp
from aiohttp import web
import os
import logging
from datetime import datetime
from pathlib import Path
from itertools import count

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dev-agents")

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
API_SECRET = os.environ.get("API_SECRET", "")
WORKSPACE = os.environ.get("WORKSPACE", "/workspace")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_TURNS = int(os.environ.get("MAX_TURNS", "50"))
DISCORD_API = "https://discord.com/api/v10"
MAX_CONTENT_LENGTH = 10_000

# Subscription plans with limits
SUBSCRIPTION_PLANS = {
    "starter": {
        "requests_limit": 1000,
        "tokens_limit": 1_000_000,
        "input_tokens_limit": 500_000,
        "output_tokens_limit": 500_000,
    },
    "pro": {
        "requests_limit": 10_000,
        "tokens_limit": 10_000_000,
        "input_tokens_limit": 5_000_000,
        "output_tokens_limit": 5_000_000,
    },
    "enterprise": {
        "requests_limit": 100_000,
        "tokens_limit": 100_000_000,
        "input_tokens_limit": 50_000_000,
        "output_tokens_limit": 50_000_000,
    },
}

# Current active subscription plan (can be set via env var)
CURRENT_PLAN = os.environ.get("SUBSCRIPTION_PLAN", "pro")

_task_lock = asyncio.Lock()
_ws_clients = set()
_current_task = None
_usage = {"total_cost_usd": 0, "total_turns": 0, "task_count": 0}
_rate_limits_cache = None
_rate_limits_ts = 0
RATE_LIMITS_TTL = 30
_history_cache = []
HISTORY_FILE = os.path.join(WORKSPACE, ".devteam", "history.jsonl")

# Per-user subscription plans and monthly usage tracking
# {user_id: "pro"} or {user_id: "starter"}
_user_plans = {}
# {user_id: {month: "2026-03": {requests: 5, tokens: 500000}, ...}}
_user_monthly_usage = {}
USAGE_FILE = os.path.join(WORKSPACE, ".devteam", "usage.json")
PLANS_FILE = os.path.join(WORKSPACE, ".devteam", "plans.json")

# Shared aiohttp session (initialized on startup, closed on cleanup)
_http_session = None

# Persistent metrics tracking
# Daily and monthly breakdown: {user_id: {date: "2026-03-30", day: 5, month: "2026-03", requests: 10, tokens: 50000, ...}, ...}
_metrics_cache = []
METRICS_FILE = os.path.join(WORKSPACE, ".devteam", "metrics.jsonl")
QUEUE_FILE = os.path.join(WORKSPACE, ".devteam", "queue.json")

# Upgrade recommendations: {user_id: recommended_plan}
_upgrade_notifications = {}

# Priority queue for tasks
PRIORITY_ORDER = {"high": 0, "normal": 1, "low": 2}
AVG_TASK_SECONDS = int(os.environ.get("AVG_TASK_SECONDS", "120"))
_task_queue = []
_queue_lock = asyncio.Lock()
_queue_seq = count(1)
_queue_worker_task = None


def _normalize_priority(priority):
    p = (priority or "normal").lower()
    return p if p in PRIORITY_ORDER else "normal"


def _make_task_id():
    return f"q-{next(_queue_seq)}"


def _queue_item_to_dict(item):
    return {
        "id": item["id"],
        "author": item["author"],
        "content": item["content"][:140],
        "priority": item["priority"],
        "status": item["status"],
        "enqueued_at": item["enqueued_at"],
        "eta_seconds": item.get("eta_seconds", 0),
    }


async def _broadcast_queue_update():
    async with _queue_lock:
        queue_items = []
        for idx, item in enumerate(_task_queue):
            queue_copy = dict(item)
            queue_copy["eta_seconds"] = (idx + 1) * AVG_TASK_SECONDS
            queue_items.append(_queue_item_to_dict(queue_copy))

    await broadcast({
        "type": "queue_update",
        "queue": queue_items,
        "running": _current_task.to_dict() if _current_task else None,
        "queue_size": len(queue_items),
    })


async def enqueue_task(channel_id, content, message_id, author, priority):
    item = {
        "id": _make_task_id(),
        "channel_id": channel_id,
        "content": content,
        "message_id": message_id,
        "author": author,
        "priority": _normalize_priority(priority),
        "status": "queued",
        "enqueued_at": datetime.now().isoformat(),
    }

    async with _queue_lock:
        _task_queue.append(item)
        _task_queue.sort(key=lambda x: (PRIORITY_ORDER[x["priority"]], x["enqueued_at"]))
        position = next((i for i, q in enumerate(_task_queue) if q["id"] == item["id"]), 0) + 1
        save_queue_state()

    await _broadcast_queue_update()
    return item, position


async def dequeue_next_task():
    async with _queue_lock:
        if not _task_queue:
            return None
        item = _task_queue.pop(0)
        item["status"] = "running"
        save_queue_state()
        return item


async def cancel_queued_task(task_id):
    async with _queue_lock:
        for i, item in enumerate(_task_queue):
            if item["id"] == task_id:
                canceled = _task_queue.pop(i)
                canceled["status"] = "canceled"
                save_queue_state()
                return canceled
    return None


def save_queue_state():
    """Persist task queue to disk"""
    try:
        os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
        with open(QUEUE_FILE, "w") as f:
            json.dump(_task_queue, f)
    except OSError as e:
        log.error(f"Failed to save queue state: {e}")


def load_queue_state():
    """Load persisted task queue from disk"""
    global _queue_seq
    if not os.path.exists(QUEUE_FILE):
        return
    try:
        with open(QUEUE_FILE, "r") as f:
            items = json.load(f)
        if not isinstance(items, list):
            return
        max_seq = 0
        for item in items:
            if item.get("status") == "queued":
                _task_queue.append(item)
            try:
                seq = int(item.get("id", "q-0").split("-")[1])
                max_seq = max(max_seq, seq)
            except (IndexError, ValueError):
                pass
        _task_queue.sort(key=lambda x: (PRIORITY_ORDER.get(x.get("priority", "normal"), 1), x.get("enqueued_at", "")))
        _queue_seq = count(max_seq + 1)
        log.info(f"Loaded {len(_task_queue)} queued tasks from disk")
    except (OSError, json.JSONDecodeError) as e:
        log.error(f"Failed to load queue state: {e}")


async def queue_worker():
    while True:
        try:
            if _task_lock.locked() or _current_task is not None:
                await asyncio.sleep(0.2)
                continue

            next_item = await dequeue_next_task()
            if not next_item:
                await asyncio.sleep(0.3)
                continue

            await _broadcast_queue_update()
            await process_task(
                next_item["channel_id"],
                next_item["content"],
                next_item["message_id"],
                next_item["author"],
            )
            await _broadcast_queue_update()
        except Exception as e:
            log.error(f"Queue worker error: {e}")
            await asyncio.sleep(1)


# --- Task State ---

class TaskState:
    def __init__(self, channel_id, message_id, author, content):
        self.channel_id = channel_id
        self.message_id = message_id
        self.author = author
        self.content = content
        self.thread_id = None
        self.events = []
        self.agents = {}
        self.started_at = datetime.now().isoformat()
        self.status = "running"
        self._active_agent = None
        self.result = ""
        self.cost_usd = 0
        self.num_turns = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.proc = None
        self.aborted = False

    def to_dict(self):
        return {
            "author": self.author,
            "content": self.content[:300],
            "status": self.status,
            "agents": self.agents,
            "events": self.events[-50:],
            "started_at": self.started_at,
            "result": self.result[:500],
            "cost_usd": self.cost_usd,
            "num_turns": self.num_turns,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }

    def to_history(self):
        started = datetime.fromisoformat(self.started_at)
        finished = datetime.now()
        duration_s = int((finished - started).total_seconds())
        return {
            "author": self.author,
            "content": self.content,
            "status": self.status,
            "agents": {k: (v["status"] if isinstance(v, dict) else v) for k, v in self.agents.items()},
            "started_at": self.started_at,
            "finished_at": finished.isoformat(),
            "duration_s": duration_s,
            "result": self.result,
            "cost_usd": self.cost_usd,
            "num_turns": self.num_turns,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


# --- Subscription & Usage Management ---

def get_current_month():
    """Return current month as YYYY-MM"""
    return datetime.now().strftime("%Y-%m")


def get_user_plan(user_id):
    """Get subscription plan for user, fallback to CURRENT_PLAN"""
    return _user_plans.get(user_id, CURRENT_PLAN)


def set_user_plan(user_id, plan):
    """Set subscription plan for user"""
    if plan not in SUBSCRIPTION_PLANS:
        raise ValueError(f"Invalid plan: {plan}")
    _user_plans[user_id] = plan
    _save_plans()
    log.info(f"User {user_id} plan set to {plan}")


def get_user_monthly_usage(user_id, month=None):
    """Get monthly usage for user"""
    if month is None:
        month = get_current_month()
    if user_id not in _user_monthly_usage:
        _user_monthly_usage[user_id] = {}
    if month not in _user_monthly_usage[user_id]:
        _user_monthly_usage[user_id][month] = {"requests": 0, "tokens": 0}
    return _user_monthly_usage[user_id][month]


def increment_user_usage(user_id, tokens_used):
    """Increment monthly usage for user"""
    month = get_current_month()
    usage = get_user_monthly_usage(user_id, month)
    usage["requests"] += 1
    usage["tokens"] += tokens_used
    _save_usage()


def check_user_quota(user_id):
    """Check if user has exceeded their monthly quota. Returns tuple (allowed, remaining_requests, remaining_tokens)"""
    plan = get_user_plan(user_id)
    plan_limits = SUBSCRIPTION_PLANS[plan]
    usage = get_user_monthly_usage(user_id)
    
    requests_remaining = plan_limits["requests_limit"] - usage["requests"]
    tokens_remaining = plan_limits["tokens_limit"] - usage["tokens"]
    
    allowed = requests_remaining > 0 and tokens_remaining > 0
    return allowed, requests_remaining, tokens_remaining


def get_user_usage_percentage(user_id):
    """Get usage percentage for user. Returns (requests_pct, tokens_pct, alert_level)"""
    plan = get_user_plan(user_id)
    plan_limits = SUBSCRIPTION_PLANS[plan]
    usage = get_user_monthly_usage(user_id)
    
    requests_pct = (usage["requests"] / plan_limits["requests_limit"] * 100) if plan_limits["requests_limit"] > 0 else 0
    tokens_pct = (usage["tokens"] / plan_limits["tokens_limit"] * 100) if plan_limits["tokens_limit"] > 0 else 0
    
    # Return highest percentage and alert level
    max_pct = max(requests_pct, tokens_pct)
    if max_pct >= 95:
        alert_level = "critical"
    elif max_pct >= 75:
        alert_level = "warning"
    else:
        alert_level = None
    
    return requests_pct, tokens_pct, alert_level, max_pct


def get_recommended_plan(user_id):
    """Determine if user should upgrade based on usage. Returns (should_upgrade, recommended_plan, reason)"""
    plan = get_user_plan(user_id)
    requests_pct, tokens_pct, alert_level, max_pct = get_user_usage_percentage(user_id)
    
    # No upgrade needed if not critical
    if alert_level != "critical":
        return False, None, None
    
    # Find next tier
    plan_order = ["starter", "pro", "enterprise"]
    current_idx = plan_order.index(plan) if plan in plan_order else 1
    
    if current_idx >= len(plan_order) - 1:
        # Already on highest plan
        return False, None, None
    
    next_plan = plan_order[current_idx + 1]
    reason = f"Usage at {max_pct:.0f}% on {plan} plan, upgrade to {next_plan}"
    return True, next_plan, reason


def record_metric(user_id, requests_used, tokens_used):
    """Record daily/monthly metrics for user"""
    today = datetime.now().strftime("%Y-%m-%d")
    month = get_current_month()
    
    metric = {
        "user": user_id,
        "timestamp": datetime.now().isoformat(),
        "date": today,
        "month": month,
        "plan": get_user_plan(user_id),
        "requests": requests_used,
        "tokens": tokens_used,
    }
    
    _metrics_cache.append(metric)
    
    # Persist to file
    try:
        os.makedirs(os.path.dirname(METRICS_FILE), exist_ok=True)
        with open(METRICS_FILE, "a") as f:
            f.write(json.dumps(metric) + "\n")
        rotate_jsonl_file(METRICS_FILE)
    except OSError as e:
        log.error(f"Failed to write metrics: {e}")
    
    return metric


def load_metrics():
    """Load historical metrics from file"""
    global _metrics_cache
    _metrics_cache = []
    if not os.path.exists(METRICS_FILE):
        return
    try:
        with open(METRICS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    _metrics_cache.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        log.info(f"Loaded {len(_metrics_cache)} metrics from history")
    except OSError as e:
        log.error(f"Failed to load metrics: {e}")


def _save_plans():
    """Persist user plans to disk"""
    try:
        os.makedirs(os.path.dirname(PLANS_FILE), exist_ok=True)
        with open(PLANS_FILE, "w") as f:
            json.dump(_user_plans, f)
    except OSError as e:
        log.error(f"Failed to save plans: {e}")


def _load_plans():
    """Load user plans from disk"""
    global _user_plans
    if not os.path.exists(PLANS_FILE):
        return
    try:
        with open(PLANS_FILE, "r") as f:
            _user_plans = json.load(f)
        log.info(f"Loaded {len(_user_plans)} user plans")
    except (OSError, json.JSONDecodeError) as e:
        log.error(f"Failed to load plans: {e}")


def _save_usage():
    """Persist monthly usage to disk"""
    try:
        os.makedirs(os.path.dirname(USAGE_FILE), exist_ok=True)
        with open(USAGE_FILE, "w") as f:
            json.dump(_user_monthly_usage, f)
    except OSError as e:
        log.error(f"Failed to save usage: {e}")


def _load_usage():
    """Load monthly usage from disk"""
    global _user_monthly_usage
    if not os.path.exists(USAGE_FILE):
        return
    try:
        with open(USAGE_FILE, "r") as f:
            _user_monthly_usage = json.load(f)
        log.info(f"Loaded usage data for {len(_user_monthly_usage)} users")
    except (OSError, json.JSONDecodeError) as e:
        log.error(f"Failed to load usage: {e}")


NOTIFICATIONS_FILE = os.path.join(WORKSPACE, ".devteam", "notifications.json")


def _save_notifications():
    """Persist upgrade notifications to disk"""
    try:
        os.makedirs(os.path.dirname(NOTIFICATIONS_FILE), exist_ok=True)
        with open(NOTIFICATIONS_FILE, "w") as f:
            json.dump(_upgrade_notifications, f)
    except OSError as e:
        log.error(f"Failed to save notifications: {e}")


def _load_notifications():
    """Load upgrade notifications from disk"""
    global _upgrade_notifications
    if not os.path.exists(NOTIFICATIONS_FILE):
        return
    try:
        with open(NOTIFICATIONS_FILE, "r") as f:
            _upgrade_notifications = json.load(f)
        log.info(f"Loaded {len(_upgrade_notifications)} upgrade notifications")
    except (OSError, json.JSONDecodeError) as e:
        log.error(f"Failed to load notifications: {e}")


def get_user_metrics(user_id, month=None):
    """Get aggregated metrics for user in a given month"""
    if month is None:
        month = get_current_month()
    
    total_requests = 0
    total_tokens = 0
    daily_breakdown = {}  # {date: {requests, tokens}}
    
    for metric in _metrics_cache:
        if metric.get("user") == user_id and metric.get("month") == month:
            total_requests += metric.get("requests", 0)
            total_tokens += metric.get("tokens", 0)
            
            date = metric.get("date")
            if date not in daily_breakdown:
                daily_breakdown[date] = {"requests": 0, "tokens": 0}
            daily_breakdown[date]["requests"] += metric.get("requests", 0)
            daily_breakdown[date]["tokens"] += metric.get("tokens", 0)
    
    return {
        "month": month,
        "total_requests": total_requests,
        "total_tokens": total_tokens,
        "daily_breakdown": daily_breakdown,
    }


# --- File Rotation ---

MAX_FILE_ENTRIES = 10_000


def rotate_jsonl_file(filepath, max_entries=MAX_FILE_ENTRIES):
    """Rotate a JSONL file when it exceeds max_entries, keeping the most recent half"""
    try:
        if not os.path.exists(filepath):
            return
        with open(filepath, "r") as f:
            lines = f.readlines()
        if len(lines) <= max_entries:
            return
        keep = lines[len(lines) - max_entries // 2:]
        with open(filepath, "w") as f:
            f.writelines(keep)
        log.info(f"Rotated {filepath}: {len(lines)} -> {len(keep)} entries")
    except OSError as e:
        log.error(f"Failed to rotate {filepath}: {e}")


# --- History ---

def load_history():
    global _history_cache
    _history_cache = []
    if not os.path.exists(HISTORY_FILE):
        return
    try:
        with open(HISTORY_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    _history_cache.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        log.info(f"Loaded {len(_history_cache)} tasks from history")
    except OSError as e:
        log.error(f"Failed to load history: {e}")


def append_history(task):
    entry = task.to_history()
    _history_cache.append(entry)
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        with open(HISTORY_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
        rotate_jsonl_file(HISTORY_FILE)
    except OSError as e:
        log.error(f"Failed to write history: {e}")
    return entry


# --- Discord Helpers ---

async def discord_request(method, path, json_body=None):
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    fn = getattr(_http_session, method)
    async with fn(f"{DISCORD_API}{path}", json=json_body, headers=headers) as resp:
        if resp.status not in (200, 201, 204):
            log.error(f"Discord {method} {path} → {resp.status}")
            return None
        if resp.status == 204:
            return {}
        return await resp.json()


async def send_discord(channel_id, content, reply_to=None):
    chunks = split_message(content)
    for i, chunk in enumerate(chunks):
        body = {"content": chunk}
        if i == 0 and reply_to:
            body["message_reference"] = {"message_id": reply_to}
        await discord_request("post", f"/channels/{channel_id}/messages", body)


async def create_thread(channel_id, message_id, name):
    data = await discord_request(
        "post",
        f"/channels/{channel_id}/messages/{message_id}/threads",
        {"name": name[:100], "auto_archive_duration": 1440},
    )
    return data["id"] if data else None


async def keep_typing(channel_id):
    try:
        while True:
            await discord_request("post", f"/channels/{channel_id}/typing")
            await asyncio.sleep(8)
    except asyncio.CancelledError:
        pass


def split_message(text, limit=1950):
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split = text[:limit].rfind("\n")
        if split < limit // 2:
            split = limit
        chunks.append(text[:split])
        text = text[split:].lstrip("\n")
    return chunks


# --- WebSocket ---

async def broadcast(event):
    global _current_task, _ws_clients
    msg = json.dumps(event)
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_str(msg)
        except Exception:
            dead.add(ws)
    _ws_clients -= dead


# --- Stream Parser ---

TOOL_ICONS = {
    "Read": "📖", "Write": "✏️", "Edit": "📝", "Bash": "💻",
    "Glob": "🔍", "Grep": "🔍", "Agent": "🔀",
}


async def parse_stream_line(line, task):
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return

    event_type = data.get("type", "")
    now = datetime.now().strftime("%H:%M:%S")

    if event_type == "assistant":
        message = data.get("message", {})
        # Accumulate actual token usage from assistant messages
        usage = message.get("usage", {})
        task.input_tokens += usage.get("input_tokens", 0)
        task.output_tokens += usage.get("output_tokens", 0)
        for block in message.get("content", []):
            if block.get("type") == "tool_use":
                await handle_tool_use(block, task, now)
            elif block.get("type") == "text":
                text = block.get("text", "").strip()
                if text and len(text) > 10:
                    ev = {"type": "text", "text": text[:200], "time": now}
                    task.events.append(ev)
                    await broadcast({"type": "event", "event": ev, "task": task.to_dict()})

    elif event_type == "result":
        task.result = data.get("result", "")
        task.status = "done"
        task.cost_usd = data.get("cost_usd", 0)
        task.num_turns = data.get("num_turns", 0)
        # Override with totals from result if available
        if data.get("total_input_tokens"):
            task.input_tokens = data["total_input_tokens"]
        if data.get("total_output_tokens"):
            task.output_tokens = data["total_output_tokens"]
        ev = {"type": "result", "text": "Tache terminee", "time": now}
        task.events.append(ev)
        await broadcast({"type": "task_complete", "task": task.to_dict()})


async def handle_tool_use(block, task, now):
    name = block.get("name", "unknown")
    inp = block.get("input", {})
    icon = TOOL_ICONS.get(name, "🔧")

    if name == "Agent":
        desc = inp.get("description", "agent")
        agent_name = desc.split()[0] if desc else "agent"
        task.agents[agent_name] = {
            "status": "running",
            "description": desc,
            "started_at": now,
            "events_count": 0,
            "last_action": "",
        }
        task._active_agent = agent_name
        discord_msg = f"{icon} **Delegation** → {desc}"
        ev = {"type": "agent_spawn", "name": agent_name, "description": desc, "time": now}
        # Always post agent spawns to thread
        if task.thread_id:
            await send_discord(task.thread_id, discord_msg)

    elif name in ("Write", "Edit"):
        path = inp.get("file_path", "")
        short = os.path.basename(path) if path else "?"
        discord_msg = f"{icon} `{short}`"
        ev = {"type": "tool", "tool": name, "file": short, "time": now}
        if task.thread_id:
            await send_discord(task.thread_id, discord_msg)

    elif name == "Bash":
        cmd = inp.get("command", "")[:80]
        ev = {"type": "tool", "tool": "Bash", "command": cmd, "time": now}
        # Only post significant bash commands to thread
        if task.thread_id and any(kw in cmd for kw in ["git ", "npm ", "docker ", "test", "build"]):
            await send_discord(task.thread_id, f"{icon} `{cmd}`")

    elif name == "Read":
        path = inp.get("file_path", "")
        short = os.path.basename(path) if path else "?"
        ev = {"type": "tool", "tool": "Read", "file": short, "time": now}
    else:
        ev = {"type": "tool", "tool": name, "time": now}

    # Track agent context
    if task._active_agent:
        ev["agent"] = task._active_agent
        agent_data = task.agents.get(task._active_agent)
        if isinstance(agent_data, dict) and name != "Agent":
            agent_data["events_count"] += 1
            if name in ("Write", "Edit"):
                agent_data["last_action"] = f"{icon} {os.path.basename(inp.get('file_path', ''))}"
            elif name == "Bash":
                agent_data["last_action"] = f"{icon} {inp.get('command', '')[:60]}"
            elif name == "Read":
                agent_data["last_action"] = f"📖 {os.path.basename(inp.get('file_path', ''))}"
            else:
                agent_data["last_action"] = f"{icon} {name}"

    task.events.append(ev)
    await broadcast({"type": "event", "event": ev, "task": task.to_dict()})


# --- Claude Runner ---

async def run_claude_stream(prompt, task):
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
        "--model", CLAUDE_MODEL,
        "--max-turns", str(MAX_TURNS),
    ]

    log.info(f"Running claude (stream): {prompt[:80]}...")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=WORKSPACE,
    )
    task.proc = proc

    final_result = ""

    try:
        async with asyncio.timeout(900):
            async for line in proc.stdout:
                decoded = line.decode().strip()
                if not decoded:
                    continue
                await parse_stream_line(decoded, task)
                # Capture result from stream
                try:
                    data = json.loads(decoded)
                    if data.get("type") == "result":
                        final_result = data.get("result", "")
                except json.JSONDecodeError:
                    pass

            await proc.wait()
    except TimeoutError:
        proc.kill()
        task.proc = None
        raise asyncio.TimeoutError("Claude CLI timeout (>15 min)")

    task.proc = None

    if task.aborted:
        raise asyncio.CancelledError("Task aborted by user")

    if proc.returncode != 0:
        stderr = await proc.stderr.read()
        raise RuntimeError(f"Claude exited {proc.returncode}: {stderr.decode()[:500]}")

    return final_result or "Done."


# --- Task Processor ---

async def process_task(channel_id, content, message_id, author):
    global _current_task

    task = TaskState(channel_id, message_id, author, content)
    _current_task = task

    async with _task_lock:
        # Check user quota before processing
        allowed, remaining_requests, remaining_tokens = check_user_quota(author)
        if not allowed:
            msg = f"❌ Quota d'utilisation atteint pour ce mois (plan: {get_user_plan(author)}). Contactez l'admin pour upgrade."
            await send_discord(channel_id, msg, message_id)
            log.warning(f"Task rejected for {author}: quota exceeded")
            return

        # Create thread
        thread_name = content[:80] if len(content) <= 80 else content[:77] + "..."
        task.thread_id = await create_thread(channel_id, message_id, thread_name)

        if task.thread_id:
            await send_discord(task.thread_id, f"🟢 **Jarvis CEO** demarre l'analyse...\n**Repo workspace :** `{WORKSPACE}`\n**Modele :** `{CLAUDE_MODEL}`")

        await broadcast({"type": "task_start", "task": task.to_dict()})

        typing_task = asyncio.create_task(keep_typing(task.thread_id or channel_id))
        try:
            result = await run_claude_stream(content, task)
            typing_task.cancel()

            # Mark all running agents as done
            for name in task.agents:
                agent_data = task.agents[name]
                if isinstance(agent_data, dict) and agent_data["status"] == "running":
                    agent_data["status"] = "done"
                elif agent_data == "running":
                    task.agents[name] = "done"

            # Track usage (actual tokens from stream, fallback to estimate)
            actual_tokens = task.input_tokens + task.output_tokens
            tokens_used = actual_tokens if actual_tokens > 0 else task.num_turns * 1000
            increment_user_usage(author, tokens_used)
            
            # Record metrics for history
            record_metric(author, 1, tokens_used)
            
            # Check if upgrade recommended
            should_upgrade, recommended_plan, reason = get_recommended_plan(author)
            if should_upgrade:
                _upgrade_notifications[author] = {
                    "timestamp": datetime.now().isoformat(),
                    "recommended_plan": recommended_plan,
                    "reason": reason,
                }
                _save_notifications()
            
            # Send WebSocket alert if quota threshold reached
            requests_pct, tokens_pct, alert_level, max_pct = get_user_usage_percentage(author)
            if alert_level:
                alert_event = {
                    "type": "quota_alert",
                    "user": author,
                    "plan": get_user_plan(author),
                    "level": alert_level,
                    "requests_pct": round(requests_pct, 1),
                    "tokens_pct": round(tokens_pct, 1),
                    "max_pct": round(max_pct, 1),
                    "timestamp": datetime.now().isoformat(),
                }
                if alert_level == "critical":
                    alert_event["message"] = f"🚨 CRITIQUE: Vous avez utilisé {max_pct:.0f}% de votre quota mensuel ({get_user_plan(author)})"
                else:
                    alert_event["message"] = f"⚠️ ATTENTION: Vous avez utilisé {max_pct:.0f}% de votre quota mensuel ({get_user_plan(author)})"
                
                # Add upgrade recommendation if applicable
                if should_upgrade:
                    alert_event["upgrade_recommended"] = True
                    alert_event["recommended_plan"] = recommended_plan
                    alert_event["message"] += f"\n📈 Upgrade recommandé vers {recommended_plan}"
                
                await broadcast(alert_event)
                log.warning(f"Quota alert for {author}: {alert_level} ({max_pct:.0f}%)")

            # Post final result
            target = task.thread_id or channel_id
            summary = result
            if task.cost_usd:
                summary += f"\n\n📊 *{task.num_turns} tours, ${task.cost_usd:.4f}*"
            await send_discord(target, summary, message_id if not task.thread_id else None)

            # Also reply in main channel with short summary
            if task.thread_id:
                short = result[:300] + ("..." if len(result) > 300 else "")
                await send_discord(channel_id, f"✅ **Terminee** — voir le thread pour les details.\n{short}", message_id)

            _usage["total_cost_usd"] += task.cost_usd
            _usage["total_turns"] += task.num_turns
            _usage["task_count"] += 1

            log.info(f"Task done for {author} ({len(result)} chars, ${task.cost_usd:.4f})")

        except asyncio.CancelledError:
            typing_task.cancel()
            task.status = "aborted"
            log.info(f"Task aborted for {author}")
            await send_discord(task.thread_id or channel_id, "🛑 Tache annulee par un utilisateur.")
            await broadcast({"type": "task_aborted", "task": task.to_dict()})

        except asyncio.TimeoutError:
            typing_task.cancel()
            task.status = "timeout"
            msg = "⚠️ Timeout (>15 min)"
            await send_discord(task.thread_id or channel_id, msg)
            await broadcast({"type": "task_error", "error": "timeout", "task": task.to_dict()})

        except Exception as e:
            typing_task.cancel()
            task.status = "error"
            log.error(f"Task failed: {e}")
            await send_discord(task.thread_id or channel_id, "❌ Une erreur interne est survenue. Consultez les logs pour plus de details.")
            await broadcast({"type": "task_error", "error": "internal_error", "task": task.to_dict()})

        finally:
            if task.status != "running":
                append_history(task)
            _current_task = None


# --- HTTP Handlers ---

def _check_api_secret(request):
    """Validate API secret from Authorization header. Returns error response or None if OK."""
    if not API_SECRET:
        return None
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return web.json_response({"error": "Missing or invalid Authorization header"}, status=401)
    if not hmac.compare_digest(auth[7:], API_SECRET):
        return web.json_response({"error": "Invalid API secret"}, status=403)
    return None


async def handle_task(request):
    auth_error = _check_api_secret(request)
    if auth_error:
        return auth_error

    try:
        data = await request.json()
    except (json.JSONDecodeError, Exception):
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    channel_id = data.get("channelId")
    content = data.get("content")
    message_id = data.get("messageId")
    author = data.get("author", "unknown")
    priority = data.get("priority", "normal")

    if not channel_id or not content:
        return web.json_response({"error": "channelId and content are required"}, status=400)
    if not isinstance(content, str) or len(content) > MAX_CONTENT_LENGTH:
        return web.json_response({"error": f"content must be a string of max {MAX_CONTENT_LENGTH} chars"}, status=400)
    if not isinstance(channel_id, str):
        return web.json_response({"error": "channelId must be a string"}, status=400)

    log.info(f"Task from {author} ({priority}): {content[:80]}")

    item, position = await enqueue_task(channel_id, content, message_id, author, priority)

    eta_s = position * AVG_TASK_SECONDS
    eta_m = max(1, eta_s // 60)
    await send_discord(
        channel_id,
        f"🧾 Tache en file (priorite: {_normalize_priority(priority)}). Position: {position}, attente estimee: ~{eta_m} min.",
        message_id,
    )

    return web.json_response({
        "status": "queued",
        "task_id": item["id"],
        "priority": item["priority"],
        "position": position,
        "eta_seconds": eta_s,
    }, status=202)


async def handle_health(request):
    status = "busy" if _task_lock.locked() else "idle"
    async with _queue_lock:
        qsize = len(_task_queue)
    return web.json_response({
        "status": status,
        "task": _current_task.to_dict() if _current_task else None,
        "queue_size": qsize,
    })


async def handle_queue(request):
    async with _queue_lock:
        queue_items = []
        for idx, item in enumerate(_task_queue):
            queue_copy = dict(item)
            queue_copy["eta_seconds"] = (idx + 1) * AVG_TASK_SECONDS
            queue_items.append(_queue_item_to_dict(queue_copy))
    return web.json_response({
        "queue": queue_items,
        "queue_size": len(queue_items),
        "running": _current_task.to_dict() if _current_task else None,
    })


async def handle_cancel_task(request):
    auth_error = _check_api_secret(request)
    if auth_error:
        return auth_error

    data = await request.json()
    task_id = data.get("task_id")
    if not task_id:
        return web.json_response({"error": "task_id required"}, status=400)

    canceled = await cancel_queued_task(task_id)
    if not canceled:
        return web.json_response({"error": "task not found or already running"}, status=404)

    await _broadcast_queue_update()
    return web.json_response({"status": "canceled", "task_id": task_id})


async def abort_running_task():
    """Abort the currently running task by killing its Claude CLI subprocess."""
    global _current_task
    if not _current_task:
        return None
    task = _current_task
    task.aborted = True
    if task.proc:
        try:
            task.proc.terminate()
            try:
                await asyncio.wait_for(task.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                task.proc.kill()
        except ProcessLookupError:
            pass
    return task


async def handle_abort(request):
    auth_error = _check_api_secret(request)
    if auth_error:
        return auth_error

    if not _current_task:
        return web.json_response({"error": "no task is currently running"}, status=404)

    task = await abort_running_task()
    if not task:
        return web.json_response({"error": "no task is currently running"}, status=404)

    return web.json_response({
        "status": "aborted",
        "author": task.author,
        "content": task.content[:140],
    })


async def handle_ws(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    _ws_clients.add(ws)
    log.info(f"WS client connected ({len(_ws_clients)} total)")

    # Send current state
    if _current_task:
        await ws.send_str(json.dumps({"type": "task_state", "task": _current_task.to_dict()}))
    else:
        await ws.send_str(json.dumps({"type": "idle"}))

    async with _queue_lock:
        queue_items = []
        for idx, item in enumerate(_task_queue):
            queue_copy = dict(item)
            queue_copy["eta_seconds"] = (idx + 1) * AVG_TASK_SECONDS
            queue_items.append(_queue_item_to_dict(queue_copy))
    await ws.send_str(json.dumps({
        "type": "queue_update",
        "queue": queue_items,
        "running": _current_task.to_dict() if _current_task else None,
        "queue_size": len(queue_items),
    }))

    try:
        async for msg in ws:
            pass  # read-only ws
    finally:
        _ws_clients.discard(ws)
        log.info(f"WS client disconnected ({len(_ws_clients)} total)")

    return ws


def _read_build_info():
    base = Path(__file__).parent
    try:
        version = (base / "VERSION").read_text().strip()
    except FileNotFoundError:
        version = "dev"
    try:
        build_date = (base / ".build_date").read_text().strip()
    except FileNotFoundError:
        build_date = "unknown"
    return version, build_date


async def handle_dashboard(request):
    html = (Path(__file__).parent / "dashboard.html").read_text()
    version, build_date = _read_build_info()
    html = html.replace("{{VERSION}}", version).replace("{{BUILD_DATE}}", build_date)
    return web.Response(text=html, content_type="text/html")


def _safe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


async def fetch_rate_limits():
    """
    Fetches real API rate limits from Anthropic, but returns usage limits
    based on the current subscription plan (max plan used).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    }

    try:
        global _http_session
        if _http_session is None or _http_session.closed:
            _http_session = aiohttp.ClientSession()
        async with _http_session.post(
            "https://api.anthropic.com/v1/messages",
            json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            h = resp.headers
            api_limits = {
                "requests_limit": _safe_int(h.get("anthropic-ratelimit-requests-limit")),
                "requests_remaining": _safe_int(h.get("anthropic-ratelimit-requests-remaining")),
                "requests_reset": h.get("anthropic-ratelimit-requests-reset", ""),
                "tokens_limit": _safe_int(h.get("anthropic-ratelimit-tokens-limit")),
                "tokens_remaining": _safe_int(h.get("anthropic-ratelimit-tokens-remaining")),
                "tokens_reset": h.get("anthropic-ratelimit-tokens-reset", ""),
                "input_tokens_limit": _safe_int(h.get("anthropic-ratelimit-input-tokens-limit")),
                "input_tokens_remaining": _safe_int(h.get("anthropic-ratelimit-input-tokens-remaining")),
                "output_tokens_limit": _safe_int(h.get("anthropic-ratelimit-output-tokens-limit")),
                "output_tokens_remaining": _safe_int(h.get("anthropic-ratelimit-output-tokens-remaining")),
            }
            
            plan_limits = SUBSCRIPTION_PLANS.get(CURRENT_PLAN, SUBSCRIPTION_PLANS["pro"])
            api_limits["requests_limit"] = plan_limits["requests_limit"]
            api_limits["tokens_limit"] = plan_limits["tokens_limit"]
            api_limits["input_tokens_limit"] = plan_limits["input_tokens_limit"]
            api_limits["output_tokens_limit"] = plan_limits["output_tokens_limit"]
            
            return api_limits
    except Exception as e:
        log.error(f"Rate limits fetch failed: {e}")
        return None


async def handle_usage(request):
    global _rate_limits_cache, _rate_limits_ts

    now = asyncio.get_event_loop().time()
    if _rate_limits_cache is None or (now - _rate_limits_ts) > RATE_LIMITS_TTL:
        _rate_limits_cache = await fetch_rate_limits()
        _rate_limits_ts = now

    return web.json_response({
        "rate_limits": _rate_limits_cache,
        "usage": _usage,
        "subscription_plan": CURRENT_PLAN,
    })


async def handle_plans(request):
    """List all available subscription plans"""
    plans_info = {}
    for plan_name, limits in SUBSCRIPTION_PLANS.items():
        plans_info[plan_name] = {
            "limits": limits,
            "active": plan_name == CURRENT_PLAN,
        }
    return web.json_response({"plans": plans_info})


async def handle_user_plan(request):
    """Get or set user's subscription plan"""
    user_id = request.query.get("user_id")
    if not user_id:
        return web.json_response({"error": "user_id required"}, status=400)
    
    # GET: fetch user plan and monthly usage
    if request.method == "GET":
        plan = get_user_plan(user_id)
        usage = get_user_monthly_usage(user_id)
        allowed, remaining_requests, remaining_tokens = check_user_quota(user_id)
        plan_limits = SUBSCRIPTION_PLANS[plan]
        
        return web.json_response({
            "user_id": user_id,
            "plan": plan,
            "month": get_current_month(),
            "usage": usage,
            "limits": plan_limits,
            "quota_allowed": allowed,
            "remaining_requests": remaining_requests,
            "remaining_tokens": remaining_tokens,
        })
    
    # POST: set user plan
    if request.method == "POST":
        auth_error = _check_api_secret(request)
        if auth_error:
            return auth_error
        data = await request.json()
        new_plan = data.get("plan")
        if not new_plan:
            return web.json_response({"error": "plan required"}, status=400)
        try:
            set_user_plan(user_id, new_plan)
            return web.json_response({"user_id": user_id, "plan": new_plan})
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
    
    return web.json_response({"error": "Method not allowed"}, status=405)


async def handle_metrics(request):
    """Get historical metrics for a user"""
    user_id = request.query.get("user_id")
    month = request.query.get("month")
    
    if not user_id:
        return web.json_response({"error": "user_id required"}, status=400)
    
    metrics = get_user_metrics(user_id, month)
    return web.json_response({
        "user_id": user_id,
        "metrics": metrics,
    })


async def handle_upgrade_notification(request):
    """Get upgrade recommendations"""
    user_id = request.query.get("user_id")
    
    if not user_id:
        return web.json_response({"error": "user_id required"}, status=400)
    
    notification = _upgrade_notifications.get(user_id)
    if not notification:
        return web.json_response({
            "user_id": user_id,
            "upgrade_recommended": False,
        })
    
    return web.json_response({
        "user_id": user_id,
        "upgrade_recommended": True,
        "recommended_plan": notification.get("recommended_plan"),
        "reason": notification.get("reason"),
        "timestamp": notification.get("timestamp"),
    })


async def handle_history(request):
    limit = min(int(request.query.get("limit", "50")), 200)
    offset = int(request.query.get("offset", "0"))
    tasks = list(reversed(_history_cache))
    page = tasks[offset:offset + limit]
    return web.json_response({
        "tasks": page,
        "total": len(_history_cache),
    })


# --- App ---

app = web.Application()
app.router.add_post("/task", handle_task)
app.router.add_post("/task/cancel", handle_cancel_task)
app.router.add_post("/task/abort", handle_abort)
app.router.add_get("/health", handle_health)
app.router.add_get("/ws", handle_ws)
app.router.add_get("/queue", handle_queue)
app.router.add_get("/history", handle_history)
app.router.add_get("/usage", handle_usage)
app.router.add_get("/plans", handle_plans)
app.router.add_get("/user-plan", handle_user_plan)
app.router.add_post("/user-plan", handle_user_plan)
app.router.add_get("/", handle_dashboard)

app.router.add_get("/metrics", handle_metrics)
app.router.add_get("/upgrade-notification", handle_upgrade_notification)


async def on_startup(app):
    global _queue_worker_task, _http_session
    _http_session = aiohttp.ClientSession()
    if _queue_worker_task is None or _queue_worker_task.done():
        _queue_worker_task = asyncio.create_task(queue_worker())
        log.info("Queue worker started")


async def on_cleanup(app):
    global _queue_worker_task, _http_session, _current_task
    # Terminate running Claude CLI subprocess
    if _current_task and _current_task.proc:
        log.info("Terminating running Claude CLI subprocess...")
        try:
            _current_task.proc.terminate()
            try:
                await asyncio.wait_for(_current_task.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                _current_task.proc.kill()
            _current_task.status = "interrupted"
            append_history(_current_task)
            log.info("Running task saved as interrupted")
        except ProcessLookupError:
            pass
        _current_task = None

    # Persist state before shutdown
    _save_plans()
    _save_usage()
    _save_notifications()
    save_queue_state()

    if _queue_worker_task and not _queue_worker_task.done():
        _queue_worker_task.cancel()
        try:
            await _queue_worker_task
        except asyncio.CancelledError:
            pass
        log.info("Queue worker stopped")
    if _http_session and not _http_session.closed:
        await _http_session.close()
        log.info("HTTP session closed")


app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    if not API_SECRET:
        log.warning("API_SECRET not set — API endpoints are unprotected!")
    load_history()
    load_metrics()
    _load_plans()
    _load_usage()
    _load_notifications()
    load_queue_state()
    log.info(f"Starting dev-agents (workspace={WORKSPACE}, model={CLAUDE_MODEL}, history={len(_history_cache)} tasks, queue={len(_task_queue)})")
    web.run_app(app, host="0.0.0.0", port=8585)
