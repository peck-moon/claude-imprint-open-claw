#!/usr/bin/env python3
"""
imprint-utils — MCP Server
Utility tools: system status, web reading, Spotify control, Discord notifications.

Usage:
  python3 server.py    # stdio mode
"""

import os
import json
import subprocess
import sys
import urllib.request
import urllib.parse
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("imprint-utils")


@mcp.tool()
def send_discord(message: str, title: str = "", color: int = 5793266) -> str:
    """Send a notification to Discord via webhook.
    message: the text to send.
    title: optional embed title.
    color: embed color as integer (default: soft blue 0x587FF2).
    Requires DISCORD_WEBHOOK_URL environment variable."""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        return "Error: DISCORD_WEBHOOK_URL not set"

    payload = {"embeds": [{"description": message, "color": color}]}
    if title:
        payload["embeds"][0]["title"] = title

    data = json.dumps(payload).encode()
    try:
        req = urllib.request.Request(
            webhook_url, data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 204):
                return "Message sent to Discord"
            return f"Error: HTTP {resp.status}"
    except Exception as e:
        return f"Error: {str(e)}"



@mcp.tool()
def system_status() -> str:
    """Check system health: CPU, memory, disk, and service status."""
    try:
        import psutil
    except ImportError:
        return "Error: psutil not installed. Run: pip install psutil"

    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    lines = [
        "System Resources",
        f"  CPU: {cpu}% | RAM: {ram.used / (1024**3):.1f}/{ram.total / (1024**3):.1f} GB ({ram.percent}%) | "
        f"Disk: {disk.used / (1024**3):.0f}/{disk.total / (1024**3):.0f} GB ({disk.percent}%)",
        "", "Services",
    ]

    services = {
        "Memory HTTP (port 8000)": {"port": 8000},
        "Cloudflare Tunnel": {"grep": "cloudflared tunnel"},
        "Telegram Channel": {"grep": "channels plugin:telegram"},
        "Dashboard (port 3000)": {"port": 3000},
    }
    procs_cmdline = []
    try:
        for p in psutil.process_iter(['pid', 'cmdline']):
            try:
                cmd = " ".join(p.info.get('cmdline') or [])
                if cmd:
                    procs_cmdline.append(cmd)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass

    listening_ports = set()
    try:
        for conn in psutil.net_connections(kind='inet'):
            if conn.status == 'LISTEN':
                listening_ports.add(conn.laddr.port)
    except (psutil.AccessDenied, OSError):
        pass

    for name, check in services.items():
        running = False
        if "port" in check:
            running = check["port"] in listening_ports
        if "grep" in check:
            running = any(check["grep"] in cmd for cmd in procs_cmdline)
        status = "running" if running else "stopped"
        lines.append(f"  [{status}] {name}")

    return "\n".join(lines)


@mcp.tool()
def read_webpage(url: str, max_length: int = 5000) -> str:
    """Fetch a webpage and extract text content. Good for reading articles, docs, etc.
    max_length: max characters to return."""
    import html.parser
    import re

    if not url.startswith(("http://", "https://")):
        return "Error: Only http/https URLs supported"

    class TextExtractor(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.texts, self.skip_tags = [], {'script', 'style', 'nav', 'footer', 'header', 'noscript'}
            self.skip_depth, self.title, self.in_title = 0, "", False
        def handle_starttag(self, tag, attrs):
            if tag in self.skip_tags: self.skip_depth += 1
            if tag == 'title': self.in_title = True
        def handle_endtag(self, tag):
            if tag in self.skip_tags and self.skip_depth > 0: self.skip_depth -= 1
            if tag == 'title': self.in_title = False
        def handle_data(self, data):
            if self.in_title: self.title = data.strip()
            elif self.skip_depth == 0 and data.strip(): self.texts.append(data.strip())

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible)"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            ct = resp.headers.get("Content-Type", "")
            if "text" not in ct and "html" not in ct and "json" not in ct:
                return f"Error: Non-text content: {ct}"
            raw = resp.read(1024 * 1024)
            charset = "utf-8"
            if "charset=" in ct:
                charset = ct.split("charset=")[-1].split(";")[0].strip()
            body = raw.decode(charset, errors="replace")
        if "json" in ct:
            return body[:max_length]
        parser = TextExtractor()
        parser.feed(body)
        text = re.sub(r'\n{3,}', '\n\n', "\n".join(parser.texts))
        result = f"{parser.title or '(untitled)'}\n\n{text}"
        return result[:max_length] + ("\n\n... (truncated)" if len(result) > max_length else "")
    except Exception as e:
        return f"Error: Fetch failed: {str(e)}"


@mcp.tool()
def spotify_control(action: str, value: str = "") -> str:
    """Control Spotify playback (macOS only).
    action: play/pause/toggle/next/prev/status/volume_up/volume_down/set_volume/play_track
    value: volume (0-100) for set_volume, Spotify URI for play_track."""
    scripts = {
        "play": 'tell application "Spotify" to play',
        "pause": 'tell application "Spotify" to pause',
        "toggle": 'tell application "Spotify" to playpause',
        "next": 'tell application "Spotify" to next track',
        "prev": 'tell application "Spotify" to previous track',
        "status": '''tell application "Spotify"
            set t to name of current track
            set a to artist of current track
            set al to album of current track
            set pos to player position
            set dur to duration of current track
            set vol to sound volume
            set st to player state as string
            return t & "|" & a & "|" & al & "|" & (pos as integer) & "|" & ((dur / 1000) as integer) & "|" & vol & "|" & st
        end tell''',
        "volume_up": 'tell application "Spotify"\nset v to sound volume\nset sound volume to (v + 10)\nif sound volume > 100 then set sound volume to 100\nreturn "Volume: " & sound volume & "%"\nend tell',
        "volume_down": 'tell application "Spotify"\nset v to sound volume\nset sound volume to (v - 10)\nif sound volume < 0 then set sound volume to 0\nreturn "Volume: " & sound volume & "%"\nend tell',
    }

    if action == "set_volume":
        try:
            vol = max(0, min(100, int(value)))
        except ValueError:
            return "Error: Provide a number 0-100"
        script = f'tell application "Spotify" to set sound volume to {vol}\nreturn "Volume: {vol}%"'
    elif action == "play_track":
        if not value:
            return "Error: Provide a Spotify URI (e.g. spotify:track:xxx)"
        safe_uri = value.replace('"', '').replace('\\', '')
        script = f'tell application "Spotify" to play track "{safe_uri}"'
    elif action in scripts:
        script = scripts[action]
    else:
        return f"Error: Unknown action: {action}\nAvailable: play, pause, toggle, next, prev, status, volume_up, volume_down, set_volume, play_track"

    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            err = result.stderr.strip()
            if "is not running" in err:
                return "Error: Spotify is not running"
            return f"Error: {err}"
        output = result.stdout.strip()
        if action == "status" and "|" in output:
            parts = output.split("|")
            if len(parts) >= 7:
                name, artist, album, pos, dur, vol, state = parts[:7]
                pm, ps = divmod(int(pos), 60)
                dm, ds = divmod(int(dur), 60)
                return f"{name} -- {artist}\n{album}\n{pm}:{ps:02d} / {dm}:{ds:02d}\nVolume: {vol}%  |  {state}"
        return output or f"{action} done"
    except subprocess.TimeoutExpired:
        return "Error: Timeout"
    except FileNotFoundError:
        return "Error: osascript not available (macOS only)"


if __name__ == "__main__":
    mcp.run(transport="stdio")
