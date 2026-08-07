#!/usr/bin/env python3
"""Antigravity-Lifecycle-Hook-Adapter fuer ToolAhead (dependency-frei).

Antigravity ruft Hooks pro Event mit einem JSON-Payload auf stdin auf und
liest eine JSON-Decision von stdout. Dieser Adapter uebersetzt die nativen
Antigravity-Tool-Events in ToolAheads provider-neutrales Event-Format und
meldet sie fail-open an den lokalen Daemon.

KRITISCH: Ein Hook, der ungueltigen Output liefert, kann in Antigravity
Tool-Calls blockieren. Dieser Adapter druckt deshalb IMMER ``{}`` (keine
Decision, keine Einmischung in Permissions) und endet mit Exit-Code 0 —
unabhaengig davon, was intern passiert. Der Event-Name kommt als argv[1],
weil Antigravity ihn nicht im Payload mitschickt.
"""

from __future__ import annotations

import json
import sys
import urllib.request

DEFAULT_URL = "http://127.0.0.1:4242"

_PATH_KEYS = ("TargetFile", "AbsolutePath", "FilePath", "file_path", "path",
              "File", "target_file")


def _path(args: dict) -> str:
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def map_tool(name: str, args: dict) -> tuple[str, dict] | None:
    """Antigravity-native Tool-Calls → Claude-artige (tool_name, tool_input).

    Die Claude-Namen nimmt der Daemon bereits ueberall entgegen; damit
    braucht die Engine fuer Antigravity keinerlei Sonderfaelle.
    """
    if not isinstance(args, dict):
        args = {}
    if name == "run_command":
        command = args.get("CommandLine", args.get("command", ""))
        return "Bash", {"command": command if isinstance(command, str) else ""}
    if name == "view_file":
        return "Read", {"file_path": _path(args)}
    if name == "grep_search":
        pattern = args.get("Query", args.get("query", ""))
        return "Grep", {"pattern": pattern if isinstance(pattern, str) else "",
                        "path": _path(args) or "."}
    if name == "find_by_name":
        pattern = args.get("Pattern", args.get("pattern", "*"))
        return "Glob", {"pattern": pattern if isinstance(pattern, str) else "*"}
    if name == "write_to_file":
        return "Write", {"file_path": _path(args),
                         "content": args.get("CodeContent", "")}
    if name in ("replace_file_content", "multi_replace_file_content"):
        return "Edit", {"file_path": _path(args)}
    return None


def build_event(event_name: str, payload: dict) -> dict | None:
    tool_call = payload.get("toolCall")
    event: dict = {
        "hook_event_name": event_name,
        "session_id": str(payload.get("conversationId") or "antigravity"),
        "source": "antigravity-hook",
    }
    model = payload.get("modelName")
    if isinstance(model, str) and model:
        event["model"] = model
    if event_name in ("PreToolUse", "PostToolUse"):
        if not isinstance(tool_call, dict):
            return None
        mapped = map_tool(str(tool_call.get("name", "")),
                          tool_call.get("args") or {})
        if mapped is None:
            return None
        event["tool_name"], event["tool_input"] = mapped
        event["tool_use_id"] = f"agy-{payload.get('stepIdx', '')}" \
                               f"-{event['session_id'][:8]}"
        if event_name == "PostToolUse":
            error = payload.get("error")
            if error:
                event["error"] = str(error)
            else:
                event["tool_response"] = {"exit_code": 0}
    return event


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    event_name = argv[0] if argv else "PostToolUse"
    url = DEFAULT_URL
    if "--url" in argv:
        try:
            url = argv[argv.index("--url") + 1]
        except IndexError:
            pass
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError
        event = build_event(event_name, payload)
        if event is not None:
            request = urllib.request.Request(
                url.rstrip("/") + "/__prefetch/agent-event", method="POST",
                data=json.dumps(event, separators=(",", ":")).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(request, timeout=0.8).read()
    except Exception:  # noqa: BLE001 — Hooks duerfen NIE blockieren
        pass
    # Immer eine gueltige, neutrale Antwort — sonst kann Antigravity den
    # eigentlichen Tool-Call verweigern.
    sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
