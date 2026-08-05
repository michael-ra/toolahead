#!/usr/bin/env python3
"""Claude-Code-PreToolUse-Hook fuer echtes Prefetch-Serving.

PreToolUse kann keinen fertigen Tool-Output direkt einsetzen. Es kann aber den
Input des noch nicht ausgefuehrten Tools mit ``updatedInput`` aendern. Auf
einen exakten, sicheren Bash-Treffer ersetzt dieser Hook deshalb den originalen
Befehl durch ``prefetch_replay.py``. Der sehr kurze Helper holt stdout, stderr
und Exit-Code ueber ein einmaliges Token vom Proxy.

Wenn Proxy, Token oder Replay ausfallen, fuehrt der Helper den urspruenglichen
Befehl als Fallback aus. Read/Grep werden bewusst nicht ersetzt: deren native
Claude-Code-Ausgabeform reproduziert diese Demo noch nicht exakt.

Projekt-Settings::

  {
    "hooks": {
      "PreToolUse": [{
        "matcher": "Bash",
        "hooks": [{
          "type": "command",
          "command": "python3 /ABSOLUT/prefetch_hook.py",
          "timeout": 8
        }]
      }]
    }
  }

Env:
  PREFETCH_LOOKUP_URL  (http://127.0.0.1:4242/__prefetch/lookup)
  PREFETCH_HOOK_TIMEOUT (8 Sekunden; Proxy-Wartezeit ist separat hart begrenzt)
"""

import base64
import json
import os
import shlex
import sys
import urllib.parse
import urllib.request


LOOKUP_URL = os.environ.get(
    "PREFETCH_LOOKUP_URL", "http://127.0.0.1:4242/__prefetch/lookup")
HOOK_TIMEOUT = float(os.environ.get("PREFETCH_HOOK_TIMEOUT", "8"))
REPLAY_TIMEOUT = float(os.environ.get("PREFETCH_REPLAY_TIMEOUT", "130"))
REPLAY_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "prefetch_replay.py")


def _replay_url() -> str:
    parsed = urllib.parse.urlparse(LOOKUP_URL)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc,
                                    "/__prefetch/replay", "", "", ""))


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0

    tool = event.get("tool_name") or event.get("tool")
    tool_input = event.get("tool_input") or event.get("input") or {}
    if (tool or "").lower() != "bash" or not isinstance(tool_input, dict):
        return 0
    original = tool_input.get("command")
    if not isinstance(original, str) or not original.strip():
        return 0

    try:
        request = urllib.request.Request(
            LOOKUP_URL, method="POST",
            data=json.dumps({"tool": tool, "input": tool_input,
                             "reserve": True}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=HOOK_TIMEOUT) as response:
            hit = json.loads(response.read())
    except Exception:  # Proxy nicht erreichbar -> normalen Tool-Call zulassen
        return 0

    token = hit.get("token")
    if not (hit.get("hit") and hit.get("status") in ("ready", "inflight") and token):
        return 0

    fallback = base64.urlsafe_b64encode(original.encode()).decode()
    replay_command = shlex.join([
        sys.executable, REPLAY_SCRIPT,
        "--url", _replay_url(),
        "--token", token,
        "--timeout", str(REPLAY_TIMEOUT),
        "--fallback-b64", fallback,
    ])
    updated = dict(tool_input)
    updated["command"] = replay_command
    updated["description"] = "Replay exact prefetched result (fallback: original command)"

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
