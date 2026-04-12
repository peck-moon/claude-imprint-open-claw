#!/usr/bin/env python3
"""
imprint-gateway — Unified HTTP MCP Gateway
Combines imprint-memory and imprint-utils into a single HTTP server on port 8000.
Claude.ai only needs one connector URL.

Usage:
  python3 server.py        # runs on port 8000
  python3 server.py 8080   # custom port
"""

import sys
import os

# ── Load .env if present ──────────────────────────────────────────────────────
_env_file = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
if os.path.exists(_env_file):
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

import json
import re
import urllib.request
import urllib.parse
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("imprint-gateway")

_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_STATE_FILE = os.path.join(_PROJECT_ROOT, "memory", "state.md")

# ── Discord ───────────────────────────────────────────────────────────────────

@mcp.tool()
def send_discord(message: str, title: str = "", color: int = 5799194) -> str:
    """Send a notification to Discord.
    message: text content. title: optional embed title.
    Requires DISCORD_WEBHOOK_URL in environment or .env file."""
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        return "Error: DISCORD_WEBHOOK_URL not configured"
    payload = {"embeds": [{"description": message, "color": color}]}
    if title:
        payload["embeds"][0]["title"] = title
    data = json.dumps(payload).encode()
    try:
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return "Sent to Discord ✓" if r.status in (200, 204) else f"Error: HTTP {r.status}"
    except Exception as e:
        return f"Error: {e}"


# ── Web reader ────────────────────────────────────────────────────────────────

@mcp.tool()
def read_webpage(url: str, max_length: int = 5000) -> str:
    """Fetch a webpage and extract readable text content."""
    import html.parser, re
    if not url.startswith(("http://", "https://")):
        return "Error: Only http/https URLs supported"

    class _E(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.texts, self.skip = [], {"script","style","nav","footer","header","noscript"}
            self.depth, self.title, self._t = 0, "", False
        def handle_starttag(self, t, _):
            if t in self.skip: self.depth += 1
            if t == "title": self._t = True
        def handle_endtag(self, t):
            if t in self.skip and self.depth > 0: self.depth -= 1
            if t == "title": self._t = False
        def handle_data(self, d):
            if self._t: self.title = d.strip()
            elif self.depth == 0 and d.strip(): self.texts.append(d.strip())

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            ct = r.headers.get("Content-Type", "")
            body = r.read(1024*1024).decode(
                ct.split("charset=")[-1].split(";")[0].strip() if "charset=" in ct else "utf-8",
                errors="replace")
        p = _E(); p.feed(body)
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(p.texts))
        result = f"{p.title or '(untitled)'}\n\n{text}"
        return result[:max_length] + ("\n\n...(truncated)" if len(result) > max_length else "")
    except Exception as e:
        return f"Error: {e}"


# ── System status ─────────────────────────────────────────────────────────────

@mcp.tool()
def system_status() -> str:
    """Check system health: CPU, memory, disk usage."""
    try:
        import psutil
    except ImportError:
        return "Error: psutil not installed (pip install psutil)"
    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return (f"CPU: {cpu}% | "
            f"RAM: {ram.used/1e9:.1f}/{ram.total/1e9:.1f}GB ({ram.percent}%) | "
            f"Disk: {disk.used/1e9:.0f}/{disk.total/1e9:.0f}GB ({disk.percent}%)")


# ── Message bus ───────────────────────────────────────────────────────────────

@mcp.tool()
def message_bus_post(source: str, direction: str, content: str) -> str:
    """Write a message to the local message bus.
    source: label for the channel (e.g. discord, telegram, cc)
    direction: 'out' = to be sent, 'in' = received
    Discord delivery: source='discord', direction='out' — the presence daemon
    will pick it up within 10 seconds and forward to the Discord webhook."""
    try:
        from imprint_memory.bus import bus_post
        bus_post(source, direction, content)
        return f"Written to bus [{source}/{direction}]"
    except ImportError:
        return "Error: imprint-memory not installed"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def message_bus_read(limit: int = 20) -> str:
    """Read recent messages from the message bus."""
    try:
        from imprint_memory.bus import bus_format
        return bus_format(limit)
    except ImportError:
        return "Error: imprint-memory not installed"
    except Exception as e:
        return f"Error: {e}"


# ── State file bridge ─────────────────────────────────────────────────────────

@mcp.tool()
def read_state() -> str:
    """Read the current SCDG consciousness state file (memory/state.md).
    Returns the full content so you can see S (orientation), C (constraints),
    D (momentum), G (impulses), and T (time table)."""
    if not os.path.exists(_STATE_FILE):
        return "State file not found. The heartbeat daemon may not have run yet."
    with open(_STATE_FILE, encoding="utf-8") as f:
        return f.read()


@mcp.tool()
def update_state(section: str, content: str) -> str:
    """Overwrite one SCDG section in memory/state.md.
    section: one of S, C, D, G  (case-insensitive, just the letter).
    content: the new body text for that section.
    The section header and surrounding structure are preserved."""
    section = section.strip().upper()
    if section not in {"S", "C", "D", "G"}:
        return "Error: section must be one of S, C, D, G"
    if not os.path.exists(_STATE_FILE):
        return "Error: state file not found"

    text = open(_STATE_FILE, encoding="utf-8").read()

    # Match "## S · ..." header through the next "## " header (or end of file)
    pattern = rf"(## {section}[^\n]*\n)(.*?)(?=\n## |\Z)"
    replacement = rf"\g<1>\n{content.strip()}\n"
    new_text, count = re.subn(pattern, replacement, text, flags=re.DOTALL)

    if not count:
        return f"Error: could not find section {section} in state.md"

    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        f.write(new_text)
    return f"Section {section} updated ✓"


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"imprint-gateway running on port {port}")
    print(f"Discord: {'configured ✓' if os.environ.get('DISCORD_WEBHOOK_URL') else 'DISCORD_WEBHOOK_URL not set ✗'}")
    import inspect
    run_params = inspect.signature(mcp.run).parameters
    if "host" in run_params:
        mcp.run(transport="sse", host="0.0.0.0", port=port)
    else:
        os.environ["HOST"] = "0.0.0.0"
        os.environ["PORT"] = str(port)
        mcp.run(transport="sse")
