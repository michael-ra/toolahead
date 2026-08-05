#!/usr/bin/env python3
"""ToolAhead lifecycle hook for Codex CLI.

The same executable is registered for SessionStart, UserPromptSubmit,
PreToolUse, PostToolUse, Stop, and SessionEnd. Hook failures are fail-open:
Codex keeps running the original tool call when the local daemon is absent.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import sys
import time
import urllib.parse
import urllib.request


BASE_URL = os.environ.get("TOOLAHEAD_URL", "http://127.0.0.1:4242").rstrip("/")
LOOKUP_URL = os.environ.get(
    "PREFETCH_LOOKUP_URL", f"{BASE_URL}/__prefetch/lookup")
EVENT_URL = os.environ.get(
    "TOOLAHEAD_EVENT_URL", f"{BASE_URL}/__prefetch/agent-event")
HOOK_TIMEOUT = float(os.environ.get("PREFETCH_HOOK_TIMEOUT", "8"))
FUTURE_WAIT = float(os.environ.get(
    "TOOLAHEAD_CODEX_FUTURE_WAIT", str(max(0.1, min(6.5, HOOK_TIMEOUT - 1.0)))))
REPLAY_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "prefetch_replay.py")
TICKET_DIR = os.environ.get("TOOLAHEAD_TICKET_DIR",
                            os.path.dirname(REPLAY_SCRIPT))
TICKET_RE = re.compile(r"\.prefetch-ticket-([A-Za-z0-9_-]+)\.json")
TICKET_MAX_AGE = float(os.environ.get("TOOLAHEAD_TICKET_MAX_AGE", "600"))


def _strict_mcp_enabled() -> bool:
    if os.environ.get("TOOLAHEAD_STRICT_MCP") == "1":
        return True
    # Installed location:
    #   <project>/.codex/hooks/toolahead/codex_hook.py
    project = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    return os.path.isfile(os.path.join(project, ".toolahead", "strict-mcp"))


def _post(url: str, payload: dict, timeout: float = HOOK_TIMEOUT) -> dict:
    request = urllib.request.Request(
        url, method="POST", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read())
    return value if isinstance(value, dict) else {}


def _replay_url() -> str:
    parsed = urllib.parse.urlparse(LOOKUP_URL)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc,
                                    "/__prefetch/replay", "", "", ""))


def _meta(event: dict) -> dict:
    return {key: event.get(key) for key in
            ("session_id", "turn_id", "tool_use_id")
            if event.get(key) is not None} | {"source": "codex-hook"}


def _materialize_replay(token: str) -> str | None:
    """Fetch outside Codex' tool sandbox and persist a single-use ticket.

    Commands executed with Codex ``workspace-write`` may not connect to a
    localhost HTTP socket. The lifecycle hook can, so it resolves the future
    before returning ``updatedInput``. The rewritten Bash tool only reads a
    mode-0600 file and never needs network access.
    """
    query = urllib.parse.urlencode({"token": token, "wait_timeout": FUTURE_WAIT})
    try:
        with urllib.request.urlopen(f"{_replay_url()}?{query}",
                                    timeout=FUTURE_WAIT + 1.0) as response:
            result = json.loads(response.read())
    except Exception:
        return None
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    safe_token = "".join(ch for ch in token if ch.isalnum() or ch in "-_")
    if not safe_token:
        return None
    path = os.path.join(TICKET_DIR,
                        f".prefetch-ticket-{safe_token}.json")
    try:
        os.makedirs(TICKET_DIR, mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(result, handle)
    except OSError:
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    return path


def _cleanup_tickets(command: str | None = None, *, remove_all: bool = False) -> None:
    """Remove consumed tickets outside Codex' restricted tool sandbox.

    A replay helper normally unlinks its ticket itself. Codex may allow the
    sandboxed command to read project hook files while denying deletion, so
    PostToolUse performs a second, authoritative cleanup. Old tickets from a
    cancelled tool are garbage-collected opportunistically.
    """
    if command:
        for match in TICKET_RE.finditer(command):
            path = os.path.join(TICKET_DIR, match.group(0))
            try:
                os.unlink(path)
            except OSError:
                pass
    try:
        now = time.time()
        for entry in os.scandir(TICKET_DIR):
            if not TICKET_RE.fullmatch(entry.name):
                continue
            try:
                if (remove_all or
                        now - entry.stat(follow_symlinks=False).st_mtime
                        > TICKET_MAX_AGE):
                    os.unlink(entry.path)
            except OSError:
                pass
    except OSError:
        pass


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(event, dict):
        return 0

    event = dict(event)
    event["source"] = "codex-hook"
    event_name = event.get("hook_event_name")
    tool_name = str(event.get("tool_name") or "").lower()
    # Optional coherent-MCP mode: Codex otherwise strongly prefers its native
    # patch tool even after using ToolAhead reads. Denying only that mutation
    # call makes it retry the advertised ToolAhead edit_file tool; reads,
    # searches, arbitrary shell work, and all non-ToolAhead workflows remain
    # untouched. This is opt-in because native apply_patch is more expressive.
    if (_strict_mcp_enabled() and
            event_name == "PreToolUse" and tool_name == "apply_patch"):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Use mcp__toolahead__edit_file after ToolAhead reads so "
                    "the edit-to-test transition can be prefetched."),
            }
        }))
        return 0
    # A cancelled command may never reach PostToolUse. SessionEnd is the final
    # safe point to remove every abandoned output-bearing ticket immediately.
    _cleanup_tickets(remove_all=event_name == "SessionEnd")
    try:
        _post(EVENT_URL, event)
    except Exception:
        # Telemetry/prediction is optional and must never break Codex.
        pass

    if event_name == "PostToolUse":
        tool_input = event.get("tool_input") or {}
        if isinstance(tool_input, dict):
            command = tool_input.get("command")
            _cleanup_tickets(command if isinstance(command, str) else None)
        return 0
    if event_name != "PreToolUse":
        return 0
    tool = event.get("tool_name") or ""
    tool_input = event.get("tool_input") or {}
    if str(tool).lower() != "bash" or not isinstance(tool_input, dict):
        return 0
    original = tool_input.get("command")
    if not isinstance(original, str) or not original.strip():
        return 0

    try:
        hit = _post(LOOKUP_URL, {
            "tool": "Bash",
            "input": tool_input,
            "reserve": True,
            "meta": _meta(event),
        })
    except Exception:
        return 0
    token = hit.get("token")
    if not (hit.get("hit") and hit.get("status") in ("ready", "inflight") and token):
        return 0

    ticket = _materialize_replay(str(token))
    if ticket is None:
        return 0

    fallback = base64.urlsafe_b64encode(original.encode()).decode()
    replay_command = shlex.join([
        sys.executable, REPLAY_SCRIPT,
        "--file", ticket,
        "--fallback-b64", fallback,
    ])
    updated = dict(tool_input)
    updated["command"] = replay_command
    updated["description"] = "Replay exact ToolAhead result (fallback: original command)"
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "exact prefetched Bash result",
            "updatedInput": updated,
        }
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
