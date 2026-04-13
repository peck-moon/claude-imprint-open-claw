"""
Claude Imprint — Presence Daemon
Event-driven: always watching, only wakes Claude when something warrants it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import shutil
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── Config ──────────────────────────────────────────────

TZ_OFFSET = int(os.environ.get("TZ_OFFSET", 0))
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET))

PACKAGE_DIR = Path(__file__).parent
PROJECT_DIR = PACKAGE_DIR.parent.parent  # packages/imprint_heartbeat -> project root
DATA_DIR = Path(os.environ.get("IMPRINT_DATA_DIR", str(Path.home() / ".imprint")))

GLOBAL_CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"
HEARTBEAT_FILE = PROJECT_DIR / "HEARTBEAT.md"
MEMORY_INDEX = DATA_DIR / "MEMORY.md"
STATE_FILE = PROJECT_DIR / "memory" / "state.md"

# ─── Load .env ───────────────────────────────────────────
_env_path = PROJECT_DIR / ".env"
if _env_path.exists():
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

CLAUDE_BIN = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")

# How often the presence daemon checks conditions (lightweight, no LLM)
PRESENCE_INTERVAL = int(os.environ.get("PRESENCE_INTERVAL", 10))

# Minimum gap between full Claude invocations regardless of triggers
MIN_INFERENCE_INTERVAL = int(os.environ.get("MIN_INFERENCE_INTERVAL", 300))

# Time-based fallback: invoke Claude at least every N seconds even if quiet
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", 900))

TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
QUIET_START = int(os.environ.get("QUIET_START", 23))
QUIET_END = int(os.environ.get("QUIET_END", 7))

HEARTBEAT_SESSION_FILE = PROJECT_DIR / "data" / "heartbeat_session.txt"

# ─── Discord dispatcher config ────────────────────────────

DB_PATH = DATA_DIR / "memory.db"
# Tracks the last message_bus id we forwarded to Discord.
# Stored in DATA_DIR so it survives daemon restarts.
DISCORD_BUS_CURSOR = DATA_DIR / "discord-bus-cursor.txt"


# ─── Time helpers ─────────────────────────────────────────

def now_local():
    return datetime.now(LOCAL_TZ)


def is_quiet_hours():
    hour = now_local().hour
    return hour >= QUIET_START or hour < QUIET_END


# ─── Session persistence ──────────────────────────────────

def load_session_id() -> str | None:
    if HEARTBEAT_SESSION_FILE.exists():
        return HEARTBEAT_SESSION_FILE.read_text().strip()
    return None


def save_session_id(sid: str):
    HEARTBEAT_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_SESSION_FILE.write_text(sid)


# ─── State readers ────────────────────────────────────────

def read_g_register() -> str:
    """Extract G section from state.md. Returns empty string if blank/placeholder."""
    if not STATE_FILE.exists():
        return ""
    content = STATE_FILE.read_text(encoding="utf-8")
    if "## G" not in content:
        return ""
    # Split on ## G header, take the section body, stop at next ## header
    g_section = content.split("## G")[1].split("\n##")[0]
    # Skip the first line (subtitle like "· 待激发的冲动") and empty lines
    lines = g_section.split("\n")[1:]
    real_lines = [l.strip() for l in lines if l.strip() and l.strip() != "（等待写入）"]
    result = "\n".join(real_lines)
    return result if len(result) > 5 else ""


def read_last_heartbeat_time() -> datetime | None:
    """Parse last heartbeat timestamp from state.md T table."""
    if not STATE_FILE.exists():
        return None
    content = STATE_FILE.read_text(encoding="utf-8")
    # Match "上次心跳 | 2026-04-11 09:36" style rows
    match = re.search(r"上次心跳[^|]*\|[^|]*(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2})", content)
    if match:
        try:
            return datetime.strptime(match.group(1).strip(), "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)
        except ValueError:
            pass
    return None


def memory_recently_updated(threshold_seconds: int = 60) -> bool:
    """True if memory DB was written to within the last N seconds (new conversation)."""
    for pattern in ("*.db", "*.sqlite", "*.sqlite3"):
        for db_file in DATA_DIR.glob(pattern):
            try:
                if time.time() - db_file.stat().st_mtime < threshold_seconds:
                    return True
            except OSError:
                pass
    return False


# ─── Event checker ────────────────────────────────────────

async def check_and_decide() -> tuple[bool, str]:
    """
    Lightweight check of all trigger conditions.
    Returns (should_activate, reason_string).
    """
    reasons = []
    elapsed: int | None = None

    last_hb = read_last_heartbeat_time()
    if last_hb is None:
        reasons.append("first_run")
    else:
        elapsed = int((now_local() - last_hb).total_seconds())
        if elapsed >= HEARTBEAT_INTERVAL:
            reasons.append("time_threshold")

    g = read_g_register()
    if g:
        reasons.append("g_register_pending")

    if memory_recently_updated(threshold_seconds=45):
        reasons.append("new_memory")

    if not reasons:
        return False, ""

    return True, ", ".join(reasons)


# ─── State stamp (Python-side, always runs) ───────────────

def stamp_heartbeat_time():
    """
    Write the current timestamp into state.md's T table.
    This runs from Python — guaranteed, regardless of what Claude does.
    Claude's job is to update S/G/D with genuine content.
    """
    if not STATE_FILE.exists():
        return
    content = STATE_FILE.read_text(encoding="utf-8")
    ts = now_local().strftime("%Y-%m-%d %H:%M")
    # Replace the "上次心跳" row in the T table
    new_content = re.sub(
        r"(\| 上次心跳\s*\|)[^\n]*",
        f"| 上次心跳 | {ts} |",
        content,
    )
    # Also update the top-level "上次更新" line
    new_content = re.sub(
        r"\*上次更新：[^*]*\*",
        f"*上次更新：{ts}*",
        new_content,
    )
    if new_content != content:
        STATE_FILE.write_text(new_content, encoding="utf-8")

    # Sync current state into memory DB (upsert: one record, always fresh).
    # Claude.ai can find it via memory_search("state") or memory_search("SCDG").
    if DB_PATH.exists():
        try:
            db = sqlite3.connect(str(DB_PATH), timeout=5)
            db.execute(
                "DELETE FROM memories WHERE source='heartbeat' AND category='state'"
            )
            db.execute(
                "INSERT INTO memories (content, category, source, tags, importance, created_at)"
                " VALUES (?, 'state', 'heartbeat', '[\"SCDG\",\"state\"]', 9, ?)",
                (new_content[:3000], now_local().strftime("%Y-%m-%d %H:%M")),
            )
            db.commit()
            db.close()
        except Exception:
            pass


# ─── Heartbeat prompt ─────────────────────────────────────

def build_heartbeat_prompt(trigger_reason: str = "") -> str:
    claude_md = GLOBAL_CLAUDE_MD.read_text(encoding="utf-8") if GLOBAL_CLAUDE_MD.exists() else ""
    heartbeat_md = HEARTBEAT_FILE.read_text(encoding="utf-8") if HEARTBEAT_FILE.exists() else ""
    memory_ctx = MEMORY_INDEX.read_text(encoding="utf-8") if MEMORY_INDEX.exists() else "(No memory index)"
    state_ctx = STATE_FILE.read_text(encoding="utf-8") if STATE_FILE.exists() else "(No state file yet)"
    current_time = now_local().strftime("%Y-%m-%d %H:%M (%A)")
    quiet = is_quiet_hours()
    trigger_line = f"\nWoke up because: {trigger_reason}" if trigger_reason else ""

    return f"""You have a moment to yourself.{trigger_line}
Current time: {current_time}
{"(Quiet hours — don't send messages unless you really want to)" if quiet else ""}

## Identity and Rules
{claude_md}

## Current State (SCDG)
{state_ctx}

## Memory Index
{memory_ctx}

## Heartbeat Protocol
{heartbeat_md}

## Instructions
1. Your state is already loaded above (SCDG). The heartbeat timestamp has already been written.
2. Follow the Heartbeat Protocol step by step.
3. If Discord notification is genuinely warranted, use the send_discord tool (imprint-utils).
4. Use the Edit tool to update `{STATE_FILE}` — fill in S (what you're oriented toward right now), G (any new impulses or remove explored ones), D (momentum). Write what's actually true, even if it's just "nothing new".
5. Reply HEARTBEAT_OK when done.

Don't send Discord just to prove you're alive.
The S/G/D update is your main job this cycle — the timestamp is handled.
"""


# ─── Full inference ───────────────────────────────────────

async def run_heartbeat(trigger_reason: str = ""):
    """Execute one full Claude inference cycle."""
    prompt = build_heartbeat_prompt(trigger_reason)

    HEARTBEAT_MODEL = os.environ.get("HEARTBEAT_MODEL", "claude-haiku-4-5-20251001")

    # No --resume: each heartbeat starts fresh so Claude actually reads the prompt
    # and uses tools. Continuity comes from state.md + memory, not from session cache.
    cmd = [
        CLAUDE_BIN,
        "-p", prompt,
        "--output-format", "json",
        "--model", HEARTBEAT_MODEL,
        "--max-budget-usd", "0.15",
    ]

    mcp_servers = {
        "imprint-memory": {
            "command": "imprint-memory",
            "args": []
        },
        "imprint-utils": {
            "command": "python3",
            "args": [str(PROJECT_DIR / "packages" / "imprint_utils" / "server.py")]
        },
    }

    mcp_config = json.dumps({"mcpServers": mcp_servers})
    cmd.extend(["--mcp-config", mcp_config])
    cmd.extend(["--permission-mode", "bypassPermissions"])

    env = {**os.environ}
    env.pop("CLAUDECODE", None)
    env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + \
                  os.path.expanduser("~/.bun/bin") + ":" + \
                  env.get("PATH", "")

    ts = now_local().strftime('%H:%M:%S')
    print(f"[{ts}] Heartbeat starting...")

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(PROJECT_DIR),
            env=env,
        )

        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

        output = stdout.decode("utf-8", errors="replace").strip()
        err_output = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            debug = (err_output or output)[:400]
            print(f"[{ts}] Heartbeat failed (rc={proc.returncode}): {debug}")
            return

        try:
            result = json.loads(output)
            response_text = result.get("result", "")
            cost = result.get("total_cost_usd", 0)
            turns = result.get("num_turns", 0)
            print(f"[{ts}] turns={turns} cost=${cost:.4f}")
            if "HEARTBEAT_OK" in response_text:
                print(f"[{ts}] Heartbeat OK")
            else:
                print(f"[{ts}] Heartbeat: {response_text[:200]}")
        except json.JSONDecodeError:
            if "HEARTBEAT_OK" in output:
                print(f"[{ts}] Heartbeat OK")
            else:
                print(f"[{ts}] Heartbeat output: {output[:200]}")

    except asyncio.TimeoutError:
        print(f"[{ts}] Heartbeat timeout (5min)")
        if proc:
            proc.kill()
    except Exception as e:
        print(f"[{ts}] Heartbeat error: {e}")


# ─── Discord dispatcher ───────────────────────────────────

def dispatch_discord_outbox():
    """
    Forward any new source='discord' direction='out' rows in message_bus
    to the Discord webhook.  Runs every presence cycle — no Claude invocation
    needed, just a lightweight SQLite read + HTTP POST.

    Cursor file (DATA_DIR/discord-bus-cursor.txt) stores the last dispatched
    row id so we survive restarts without replaying old messages.
    """
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url or not DB_PATH.exists():
        return

    ts = now_local().strftime('%H:%M:%S')

    # --- resolve cursor ---
    if DISCORD_BUS_CURSOR.exists():
        try:
            last_id = int(DISCORD_BUS_CURSOR.read_text().strip())
        except ValueError:
            last_id = 0
    else:
        # First run: initialise cursor to current max id so we only forward
        # messages written *after* the daemon starts.
        try:
            db = sqlite3.connect(str(DB_PATH), timeout=5)
            row = db.execute("SELECT COALESCE(MAX(id), 0) FROM message_bus").fetchone()
            db.close()
            last_id = row[0] if row else 0
        except Exception:
            last_id = 0
        DISCORD_BUS_CURSOR.parent.mkdir(parents=True, exist_ok=True)
        DISCORD_BUS_CURSOR.write_text(str(last_id))
        return  # nothing new on very first call

    # --- fetch pending rows ---
    try:
        db = sqlite3.connect(str(DB_PATH), timeout=5)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT id, content FROM message_bus "
            "WHERE source='discord' AND direction='out' AND id > ? "
            "ORDER BY id ASC LIMIT 10",
            (last_id,),
        ).fetchall()
        db.close()
    except Exception as e:
        print(f"[{ts}] Discord outbox DB error: {e}")
        return

    if not rows:
        return

    new_cursor = last_id
    dispatched = 0

    for row in rows:
        msg_id  = row["id"]
        content = row["content"]

        payload = json.dumps({
            "embeds": [{"description": content, "color": 5793266}]
        }).encode()

        try:
            req = urllib.request.Request(
                webhook_url,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status in (200, 204):
                    new_cursor = msg_id
                    dispatched += 1
                else:
                    print(f"[{ts}] Discord webhook HTTP {resp.status} (msg {msg_id})")
                    break  # stop; retry next cycle
        except Exception as e:
            print(f"[{ts}] Discord dispatch error: {e}")
            break

    if new_cursor > last_id:
        DISCORD_BUS_CURSOR.write_text(str(new_cursor))
        print(f"[{ts}] Discord: dispatched {dispatched} message(s)")


# ─── Presence daemon ──────────────────────────────────────

async def presence_loop():
    """
    Always-running event loop. Checks conditions every PRESENCE_INTERVAL seconds.
    Only invokes Claude when a trigger condition is met — not on a fixed timer.
    """
    discord_status = "configured ✓" if os.environ.get("DISCORD_WEBHOOK_URL") else "DISCORD_WEBHOOK_URL not set ✗"
    print("Presence daemon started")
    print(f"  Check interval : {PRESENCE_INTERVAL}s")
    print(f"  Min gap between inferences: {MIN_INFERENCE_INTERVAL}s")
    print(f"  Time fallback  : {HEARTBEAT_INTERVAL}s ({HEARTBEAT_INTERVAL // 60}min)")
    print(f"  Discord outbox : {discord_status}")
    print(f"  Project        : {PROJECT_DIR}")
    print()

    last_inference_at = 0.0

    while True:
        try:
            # Discord dispatcher runs every cycle — no LLM required
            dispatch_discord_outbox()

            # Hard floor: never invoke more often than MIN_INFERENCE_INTERVAL
            if time.time() - last_inference_at < MIN_INFERENCE_INTERVAL:
                await asyncio.sleep(PRESENCE_INTERVAL)
                continue

            should_activate, reason = await check_and_decide()

            if should_activate:
                ts = now_local().strftime('%H:%M:%S')
                print(f"[{ts}] Triggered ({reason})")
                stamp_heartbeat_time()
                await run_heartbeat(reason)
                last_inference_at = time.time()

        except Exception as e:
            print(f"Presence loop error: {e}")

        await asyncio.sleep(PRESENCE_INTERVAL)


# ─── Entry point ─────────────────────────────────────────

def main():
    loop = asyncio.new_event_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: sys.exit(0))

    try:
        loop.run_until_complete(presence_loop())
    except (KeyboardInterrupt, SystemExit):
        print("\nPresence daemon stopped")


if __name__ == "__main__":
    main()
