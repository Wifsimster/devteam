import asyncio
import json
import aiohttp
from aiohttp import web
import os
import logging
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dev-agents")

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
WORKSPACE = os.environ.get("WORKSPACE", "/workspace")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_TURNS = int(os.environ.get("MAX_TURNS", "50"))
DISCORD_API = "https://discord.com/api/v10"

_task_lock = asyncio.Lock()
_ws_clients = set()
_current_task = None
_usage = {"total_cost_usd": 0, "total_turns": 0, "task_count": 0}
_rate_limits_cache = None
_rate_limits_ts = 0
RATE_LIMITS_TTL = 60
_history_cache = []
HISTORY_FILE = os.path.join(WORKSPACE, ".devteam", "history.jsonl")


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
        }


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
    except OSError as e:
        log.error(f"Failed to write history: {e}")
    return entry


# --- Discord Helpers ---

async def discord_request(method, path, json_body=None):
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        fn = getattr(session, method)
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
        raise asyncio.TimeoutError("Claude CLI timeout (>15 min)")

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
            await send_discord(task.thread_id or channel_id, f"❌ Erreur: {e}")
            await broadcast({"type": "task_error", "error": str(e), "task": task.to_dict()})

        finally:
            if task.status != "running":
                append_history(task)
            _current_task = None


# --- HTTP Handlers ---

async def handle_task(request):
    data = await request.json()
    channel_id = data["channelId"]
    content = data["content"]
    message_id = data.get("messageId")
    author = data.get("author", "unknown")

    log.info(f"Task from {author}: {content[:80]}")

    if _task_lock.locked():
        await send_discord(channel_id, "⏳ Une tache est deja en cours, patiente.", message_id)
        return web.Response(status=202, text="busy")

    asyncio.create_task(process_task(channel_id, content, message_id, author))
    return web.Response(status=202, text="accepted")


async def handle_health(request):
    status = "busy" if _task_lock.locked() else "idle"
    return web.json_response({"status": status, "task": _current_task.to_dict() if _current_task else None})


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
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                h = resp.headers
                return {
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
app.router.add_get("/health", handle_health)
app.router.add_get("/ws", handle_ws)
app.router.add_get("/history", handle_history)
app.router.add_get("/usage", handle_usage)
app.router.add_get("/", handle_dashboard)

if __name__ == "__main__":
    load_history()
    log.info(f"Starting dev-agents (workspace={WORKSPACE}, model={CLAUDE_MODEL}, history={len(_history_cache)} tasks)")
    web.run_app(app, host="0.0.0.0", port=8585)
