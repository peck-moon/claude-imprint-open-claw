#!/usr/bin/env python3
"""
Chat with the local presence model.

Loads the accumulated Ollama context from heartbeat sessions, so the model
"remembers" what it observed. Your conversation becomes part of the context
— next heartbeat continues from here.

Usage:
  python3 packages/imprint_heartbeat/chat.py
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent.parent

_env_path = PROJECT_DIR / ".env"
if _env_path.exists():
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

OLLAMA_BASE   = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL         = os.environ.get("PRESENCE_MODEL", "gemma3:1b")
CONTEXT_FILE  = PROJECT_DIR / "memory" / "ollama-context.json"


def _generate(prompt: str, context: list[int]) -> tuple[str, list[int]]:
    data = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "stream": True,
        "context": context,
        "options": {"num_predict": 300, "temperature": 0.7},
        "keep_alive": "30m",
    }).encode()

    req = urllib.request.Request(
        f"{OLLAMA_BASE}/api/generate",
        data=data,
        headers={"Content-Type": "application/json"},
    )

    text = ""
    new_context = context

    with urllib.request.urlopen(req, timeout=60) as resp:
        for raw in resp:
            chunk = json.loads(raw.decode())
            token = chunk.get("response", "")
            print(token, end="", flush=True)
            text += token
            if chunk.get("done"):
                new_context = chunk.get("context", context)

    print()
    return text, new_context


def main():
    context: list[int] = []
    if CONTEXT_FILE.exists():
        try:
            context = json.loads(CONTEXT_FILE.read_text())
            print(f"[{MODEL} · {len(context)} tokens loaded from heartbeat sessions]")
        except Exception:
            print(f"[{MODEL} · fresh context]")
    else:
        print(f"[{MODEL} · no saved context yet — run the heartbeat first]")

    print("Ctrl+C or empty line × 2 to exit.\n")

    empty_streak = 0
    while True:
        try:
            user_input = input("你: ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            break

        if not user_input:
            empty_streak += 1
            if empty_streak >= 2:
                break
            continue
        empty_streak = 0

        print("它: ", end="", flush=True)
        try:
            _, context = _generate(user_input, context)
        except Exception as e:
            print(f"[Error: {e}]")
            continue

        # Save updated context so the next heartbeat continues from here
        CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONTEXT_FILE.write_text(json.dumps(context))

    print(f"[Context saved · {len(context)} tokens]")


if __name__ == "__main__":
    main()
