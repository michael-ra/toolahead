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
import http.client
import json
import os
import shlex
import sys


# Dieser Hook laeuft VOR jedem passenden Tool-Call; sein Prozessstart liegt
# also auf dem kritischen Pfad des Agenten. Deshalb bewusst ``http.client``
# statt ``urllib.request``: gemessen rund 12 ms weniger Importzeit pro Aufruf.
def _explicit_url() -> str | None:
    """``--url`` aus dem installierten Hook-Command, falls vorhanden."""
    argv = sys.argv[1:]
    if "--url" in argv:
        try:
            return argv[argv.index("--url") + 1].rstrip("/")
        except IndexError:
            pass
    return None


def _base_url() -> str:
    explicit = _explicit_url()
    if explicit:
        return explicit
    configured = os.environ.get("PREFETCH_LOOKUP_URL")
    if configured:
        return configured.split("/__prefetch/")[0].rstrip("/")
    return os.environ.get("TOOLAHEAD_URL", "http://127.0.0.1:4242").rstrip("/")


BASE_URL = _base_url()
# Das explizite Flag gewinnt gegen die Umgebung. Sonst koennten Lookup und
# Replay auf verschiedene Daemons zeigen: der Hook reserviert dann bei dem
# einen und der umgeschriebene Befehl holt das Ergebnis beim anderen ab.
LOOKUP_URL = f"{BASE_URL}/__prefetch/lookup" if _explicit_url() \
    else os.environ.get("PREFETCH_LOOKUP_URL",
                        f"{BASE_URL}/__prefetch/lookup")
HOOK_TIMEOUT = float(os.environ.get("PREFETCH_HOOK_TIMEOUT", "8"))
REPLAY_TIMEOUT = float(os.environ.get("PREFETCH_REPLAY_TIMEOUT", "130"))
REPLAY_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "prefetch_replay.py")


def _daemon_url(path: str) -> str:
    return f"{BASE_URL}{path}"


def _replay_url() -> str:
    return _daemon_url("/__prefetch/replay")


def _post(url: str, payload: dict, timeout: float) -> dict | None:
    """Minimaler JSON-POST ohne urllib. Gibt None statt zu werfen."""
    try:
        scheme, _, rest = url.partition("://")
        netloc, _, path = rest.partition("/")
        host, _, port = netloc.partition(":")
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, int(port or 443),
                                               timeout=timeout)
        else:
            conn = http.client.HTTPConnection(host, int(port or 80),
                                              timeout=timeout)
        try:
            conn.request("POST", "/" + path,
                         body=json.dumps(payload, separators=(",", ":")).encode(),
                         headers={"Content-Type": "application/json"})
            body = conn.getresponse().read()
        finally:
            conn.close()
        value = json.loads(body)
        return value if isinstance(value, dict) else None
    except Exception:  # noqa: BLE001 — Hook bleibt fail-open
        return None


def _forward_event(event: dict) -> None:
    """Meldet native Tool-Events an den Daemon (Lernen + Mutationen).

    Damit funktionieren Transition-Learning, Service-Pre-Warming und
    Route-Warming auch ohne die ToolAhead-MCP-Tools. Fail-open."""
    payload = dict(event)
    payload.setdefault("source", "claude-hook")
    payload.setdefault("workspace",
                       os.path.realpath(event.get("cwd") or os.getcwd()))
    _post(_daemon_url("/__prefetch/agent-event"), payload, 0.8)


def _ensure_deny_reason(command: str, cwd: str) -> str:
    """Blockierend auf deklarierte Services warten; Grund nur bei hartem Fail.

    Fail-open in jede andere Richtung: kein toolahead.toml, Daemon down,
    untrusted, falscher Workspace, kein deklariertes Kommando → leerer String
    (Call laeuft normal). ``TOOLAHEAD_ENSURE_WAIT=0`` schaltet das Warten ab."""
    try:
        wait = float(os.environ.get("TOOLAHEAD_ENSURE_WAIT", "110"))
    except ValueError:
        wait = 110.0
    if wait <= 0 or not os.path.exists(os.path.join(cwd, "toolahead.toml")):
        return ""
    # Der Workspace geht mit: sonst entscheidet der Daemon eines FREMDEN
    # Projekts (gleicher Default-Port) ueber dieses Kommando.
    result = _post(_daemon_url("/__prefetch/ensure-services"),
                   {"command": command, "workspace": os.path.realpath(cwd),
                    "wait": wait},
                   wait + 5.0)
    if not isinstance(result, dict) or not result.get("ok"):
        return ""
    services = result.get("services") or {}
    if services and result.get("trusted") and not result.get("ready"):
        bad = ", ".join(f"{name}={state}"
                        for name, state in sorted(services.items())
                        if state != "ready")
        return (f"declared service(s) not ready: {bad}. This command requires "
                "them (toolahead.toml). Check the logs under "
                ".toolahead/services/, fix the service, then retry.")
    return ""


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0

    _forward_event(event)
    if event.get("hook_event_name") not in (None, "PreToolUse"):
        return 0

    tool = event.get("tool_name") or event.get("tool")
    tool_input = event.get("tool_input") or event.get("input") or {}
    if (tool or "").lower() != "bash" or not isinstance(tool_input, dict):
        return 0
    original = tool_input.get("command")
    if not isinstance(original, str) or not original.strip():
        return 0

    deny = _ensure_deny_reason(original, event.get("cwd") or os.getcwd())
    if deny:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": deny,
            }
        }))
        return 0

    hit = _post(LOOKUP_URL, {"tool": tool, "input": tool_input,
                             "reserve": True,
                             "workspace": os.path.realpath(
                                 event.get("cwd") or os.getcwd())},
                HOOK_TIMEOUT)
    if hit is None:  # Proxy nicht erreichbar -> normalen Tool-Call zulassen
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
