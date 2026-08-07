#!/usr/bin/env python3
"""Dependency-free stdio MCP server for replayable ToolAhead tools.

The server owns the actual tool invocation.  That is what makes a prefetched
Read/Search result returnable from memory: unlike lifecycle hooks, an MCP tool
can answer the call itself instead of waiting for a native tool to execute.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    # Normal installed-package path.
    from .tool_contracts import (
        ToolContractError,
        ToolOutcome,
        execute,
        normalize_edit_input,
        normalize_list_input,
        normalize_read_input,
        normalize_run_input,
        normalize_search_input,
        normalize_write_input,
        replayable_command,
        visible_text,
    )
except ImportError:  # copied project-local MCP runtime
    from tool_contracts import (
        ToolContractError,
        ToolOutcome,
        execute,
        normalize_edit_input,
        normalize_list_input,
        normalize_read_input,
        normalize_run_input,
        normalize_search_input,
        normalize_write_input,
        replayable_command,
        visible_text,
    )

try:
    from .services import ServiceManager
except ImportError:  # copied project-local MCP runtime
    from services import ServiceManager


VERSION = "0.3.0"
DEFAULT_URL = "http://127.0.0.1:4242"

READ_DESCRIPTION = (
    "Read a UTF-8 text file from the workspace with 1-based line numbers. "
    "Use this instead of another file-read tool when available: ToolAhead may "
    "return an exact prefetched result. Supports offset and limit for large files."
)
SEARCH_DESCRIPTION = (
    "Search workspace file contents with a regular expression and return "
    "path:line:content matches. Use this instead of another grep/search tool "
    "when available: ToolAhead may return an exact prefetched result."
)
LIST_DESCRIPTION = (
    "Find workspace files whose project-relative paths match a glob pattern. "
    "Use this instead of another glob/file-listing tool when available: "
    "ToolAhead may return an exact prefetched result."
)
RUN_DESCRIPTION = (
    "Run an exact deterministic test, build, or lint command that is listed in "
    ".prefetch-replay.json. ToolAhead may replay an identical prefetched result. "
    "Commands declared under [commands] in toolahead.toml also run here: their "
    "required services are started and health-checked first (never replayed). "
    "Use the agent's native shell for commands that are neither."
)
EDIT_DESCRIPTION = (
    "Replace an exact string in a workspace text file. Use this ToolAhead edit "
    "after ToolAhead read_file; it follows the familiar file_path, old_string, "
    "new_string, and replace_all contract. Edits execute normally and are never "
    "prefetched, cached, or replayed."
)
WRITE_DESCRIPTION = (
    "Create or overwrite a UTF-8 workspace file using the familiar file_path "
    "and content contract. Writes execute normally and are never prefetched, "
    "cached, or replayed."
)


TOOLS = [
    {
        "name": "read_file",
        "title": "Read file (ToolAhead)",
        "description": READ_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path or path relative to the workspace.",
                },
                "offset": {
                    "type": "integer", "minimum": 1,
                    "description": "1-based line number to start reading from.",
                },
                "limit": {
                    "type": "integer", "minimum": 1, "maximum": 10000,
                    "description": "Maximum number of lines to return (default 2000).",
                },
            },
            "required": ["file_path"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "search",
        "title": "Search files (ToolAhead)",
        "description": SEARCH_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string", "description": "Regular expression to search for."
                },
                "query": {
                    "type": "string",
                    "description": "Compatibility alias for pattern.",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search; defaults to the workspace.",
                },
                "glob": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "Include/exclude glob, or a list; prefix exclusions with !.",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": "Result shape; defaults to content.",
                },
                "case_insensitive": {
                    "type": "boolean", "description": "Enable case-insensitive matching."
                },
                "multiline": {
                    "type": "boolean", "description": "Allow matches across line boundaries."
                },
                "head_limit": {
                    "type": "integer", "minimum": 1, "maximum": 2000,
                    "description": "Maximum results to return (default 200).",
                },
                "offset": {
                    "type": "integer", "minimum": 1,
                    "description": "1-based result offset.",
                },
            },
            "anyOf": [{"required": ["pattern"]}, {"required": ["query"]}],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "list_files",
        "title": "List files (ToolAhead)",
        "description": LIST_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern such as **/*.py or src/**/test_*.py.",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search; defaults to the workspace.",
                },
                "limit": {
                    "type": "integer", "minimum": 1, "maximum": 2000,
                    "description": "Maximum paths to return (default 200).",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "edit_file",
        "title": "Edit file (ToolAhead)",
        "description": EDIT_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path or path relative to the workspace.",
                },
                "old_string": {
                    "type": "string",
                    "description": "Exact text to replace; include context when it is not unique.",
                },
                "new_string": {
                    "type": "string", "description": "Replacement text."
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence (default false).",
                },
            },
            "required": ["file_path", "old_string", "new_string"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "write_file",
        "title": "Write file (ToolAhead)",
        "description": WRITE_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path or path relative to the workspace.",
                },
                "content": {
                    "type": "string",
                    "description": "Complete UTF-8 file contents.",
                },
            },
            "required": ["file_path", "content"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "run",
        "title": "Run replayable command (ToolAhead)",
        "description": RUN_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Exact command listed in .prefetch-replay.json.",
                },
                "description": {
                    "type": "string",
                    "description": "Short description of what the command does.",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
]


class ToolAheadMCP:
    def __init__(self, workspace: str, base_url: str = DEFAULT_URL,
                 *, lookup_wait: float = 0.25, replay_wait: float = 125.0,
                 command_timeout: float = 120.0, report_events: bool = True):
        self.workspace = os.path.realpath(os.path.abspath(workspace))
        self.base_url = base_url.rstrip("/")
        self.lookup_wait = max(0.0, lookup_wait)
        self.replay_wait = max(0.1, replay_wait)
        self.command_timeout = max(0.1, command_timeout)
        self.report_events = report_events
        try:
            self.ensure_wait = float(os.environ.get(
                "TOOLAHEAD_ENSURE_WAIT", "45"))
        except ValueError:
            self.ensure_wait = 45.0
        self.session_id = f"mcp-{os.getpid()}-{secrets.token_hex(6)}"
        self.sequence = 0
        self.turn_started = False
        self._services_cache: tuple[float, ServiceManager] | None = None

    def _request(self, path: str, payload: dict[str, Any], *,
                 timeout: float = 1.0) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path, method="POST",
            data=json.dumps(payload, separators=(",", ":")).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read())
        return result if isinstance(result, dict) else {}

    def _event(self, name: str, native_name: str, arguments: dict[str, Any],
               call_id: str, outcome: ToolOutcome | None = None) -> None:
        if not self.report_events:
            return
        payload: dict[str, Any] = {
            "hook_event_name": name,
            "session_id": self.session_id,
            "tool_use_id": call_id,
            "tool_name": native_name,
            "tool_input": arguments,
            "source": "toolahead-mcp",
            "cwd": self.workspace,
        }
        if outcome is not None:
            payload["tool_response"] = outcome.as_json()
        try:
            self._request("/__prefetch/agent-event", payload, timeout=0.5)
        except Exception:
            pass

    def _start_turn(self) -> None:
        """Give the daemon a first-turn signal even when client hooks do not.

        Some non-interactive clients initialize MCP before their project-level
        prompt hook fires (or do not expose that hook at all).  MCP initialize
        is still an exact session boundary, so it can safely trigger the
        learned ``$START`` prediction once. Later transitions are driven by
        the tool events emitted below.
        """

        if self.turn_started or not self.report_events:
            return
        self.turn_started = True
        try:
            self._request("/__prefetch/agent-event", {
                "hook_event_name": "UserPromptSubmit",
                "session_id": self.session_id,
                "source": "toolahead-mcp",
                "cwd": self.workspace,
            }, timeout=0.5)
        except Exception:
            pass

    def _allowlisted(self, command: str) -> bool:
        path = Path(self.workspace, ".prefetch-replay.json")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        commands = value if isinstance(value, list) else value.get("commands", []) \
            if isinstance(value, dict) else []
        return replayable_command(command) and command in commands

    @staticmethod
    def _native(tool_name: str, arguments: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        if tool_name == "read_file":
            args = normalize_read_input(arguments)
            return "read", "Read", args
        if tool_name == "search":
            args = normalize_search_input(arguments)
            return "grep", "Grep", args
        if tool_name == "list_files":
            args = normalize_list_input(arguments)
            return "glob", "Glob", args
        if tool_name == "edit_file":
            args = normalize_edit_input(arguments)
            return "edit", "Edit", args
        if tool_name == "write_file":
            args = normalize_write_input(arguments)
            return "write", "Write", args
        if tool_name == "run":
            args = normalize_run_input(arguments)
            return "bash", "Bash", args
        raise ToolContractError(f"unknown tool: {tool_name}")

    def _lookup(self, native_name: str, arguments: dict[str, Any], *,
                reserve: bool = False, call_id: str) -> dict[str, Any]:
        payload = {
            "tool": native_name,
            "input": arguments,
            "wait_timeout": 0.0 if reserve else self.lookup_wait,
            "reserve": reserve,
            "meta": {
                "session_id": self.session_id,
                "tool_use_id": call_id,
                "source": "toolahead-mcp",
            },
        }
        return self._request("/__prefetch/lookup", payload,
                             timeout=max(1.0, self.lookup_wait + 0.75))

    def _service_manager(self) -> ServiceManager | None:
        """Lokal geparste toolahead.toml (mtime-gecacht), None ohne Config."""
        path = os.path.join(self.workspace, "toolahead.toml")
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            self._services_cache = None
            return None
        if self._services_cache and self._services_cache[0] == mtime:
            return self._services_cache[1]
        manager = ServiceManager.load(self.workspace)
        self._services_cache = (mtime, manager)
        return manager

    def _service_requirements(self, command: str) -> list[str]:
        manager = self._service_manager()
        return manager.requirements_for(command) if manager else []

    def _ensure_services(self, command: str) -> dict[str, Any] | None:
        """Deklarierte Prerequisites (toolahead.toml) vor dem echten Lauf
        hochfahren; liefert die Ensure-Antwort des Daemons.

        Strikt optional: ``TOOLAHEAD_ENSURE_WAIT=0`` schaltet das Warten
        komplett ab, und jeder Fehler — Daemon down, Timeout — ist fail-open
        (Rueckgabe ``None``): das Kommando laeuft dann einfach normal."""
        if self.ensure_wait <= 0:
            return None
        try:
            return self._request("/__prefetch/ensure-services",
                                 {"command": command}, timeout=self.ensure_wait)
        except Exception:  # noqa: BLE001
            return None

    def _replay(self, token: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({
            "token": token, "wait_timeout": self.replay_wait})
        with urllib.request.urlopen(
                f"{self.base_url}/__prefetch/replay?{query}",
                timeout=self.replay_wait + 1.0) as response:
            value = json.loads(response.read())
        return value if isinstance(value, dict) else {}

    def call_tool(self, tool_name: str,
                  arguments: dict[str, Any]) -> dict[str, Any]:
        tool, native_name, args = self._native(tool_name, arguments)
        required: list[str] = []
        if tool == "bash":
            # Zwei getrennte Opt-ins: Replay-Allowlist (Ergebnis wiederverwenden)
            # und toolahead.toml-[commands] (ausfuehren mit Service-Ensure, aber
            # NIE cachen/replayen — das Ergebnis haengt von Server-State ab).
            required = self._service_requirements(args["command"])
            if not required and not self._allowlisted(args["command"]):
                raise ToolContractError(
                    "command is not replayable; add the exact safe test/lint "
                    "command with `toolahead allow`, declare it under [commands] "
                    "in toolahead.toml, or use the agent's native shell")

        self.sequence += 1
        call_id = f"{self.session_id}-{self.sequence}"
        self._event("PreToolUse", native_name, args, call_id)
        cache = "miss"
        outcome: ToolOutcome | None = None
        lookup_meta: dict[str, Any] = {}
        notes: list[str] = []
        try:
            if tool == "bash" and not required:
                hit = self._lookup(native_name, args, reserve=True, call_id=call_id)
                token = hit.get("token")
                if hit.get("hit") and isinstance(token, str) and token:
                    replay = self._replay(token)
                    if replay.get("ok"):
                        outcome = ToolOutcome.from_json(replay)
                        cache = "hit"
                        lookup_meta = {
                            "saved_s": replay.get("saved_s"),
                            "waited_s": replay.get("waited_s"),
                            "native_s": replay.get("dur_s"),
                        }
            elif tool in ("read", "grep", "glob"):
                hit = self._lookup(native_name, args, call_id=call_id)
                if hit.get("hit"):
                    outcome = ToolOutcome.from_json(hit)
                    cache = "hit"
                    lookup_meta = {
                        "saved_s": hit.get("saved_s"),
                        "waited_s": hit.get("waited_s"),
                        "native_s": hit.get("dur_s"),
                    }
        except Exception:
            # Daemon/cache failure is fail-open: this remains a normal MCP tool.
            pass

        ensure: dict[str, Any] | None = None
        if tool == "bash" and required:
            ensure = self._ensure_services(args["command"])
        if ensure is not None and ensure.get("ok"):
            states = ensure.get("services") or {}
            if not ensure.get("trusted", True):
                notes.append(
                    "[ToolAhead] toolahead.toml is not trusted yet, so required "
                    f"service(s) {', '.join(required)} were neither started nor "
                    "health-checked. Run `toolahead trust` once to enable "
                    "managed pre-warming.")
            elif not ensure.get("ready"):
                bad = {name: state for name, state in states.items()
                       if state != "ready"}
                detail = ", ".join(f"{name}={state}"
                                   for name, state in sorted(bad.items()))
                exc = ToolContractError(
                    f"declared service(s) not ready: {detail}. This command "
                    f"requires {', '.join(required)} (toolahead.toml). Check "
                    "the service logs under .toolahead/services/ before "
                    "rerunning.")
                self._event("PostToolUse", native_name, args, call_id,
                            ToolOutcome(stderr=str(exc), exit_code=1))
                raise exc
            else:
                age = ensure.get("last_mutation_age_s")
                started_now = set(ensure.get("started_now") or [])
                warm = [name for name in required if name not in started_now]
                if warm and isinstance(age, (int, float)) and age < 3.0:
                    # Freshness-Hinweis statt Barriere: kostet keine Latenz,
                    # verhindert aber die Fehldiagnose "mein Edit hat nichts
                    # geaendert", wenn ein Hot-Reload-Server noch nachzieht.
                    notes.append(
                        f"[ToolAhead] note: the workspace changed {age:.1f}s "
                        f"ago and service(s) {', '.join(warm)} were already "
                        "running — a hot-reload server may still serve the "
                        "previous build. If this result looks unaffected by "
                        "your edit, re-run once.")

        started = time.monotonic()
        try:
            if outcome is None:
                outcome = execute(tool, self.workspace, args,
                                  command_timeout=self.command_timeout)
        except ToolContractError as exc:
            self._event("PostToolUse", native_name, args, call_id,
                        ToolOutcome(stderr=str(exc), exit_code=1))
            raise
        elapsed = time.monotonic() - started
        self._event("PostToolUse", native_name, args, call_id, outcome)

        metadata = {
            "cache": cache,
            "direct_execution_s": round(elapsed, 6),
            **{key: value for key, value in lookup_meta.items() if value is not None},
        }
        if required:
            metadata["external"] = True
            if ensure is not None and isinstance(ensure.get("services"), dict):
                metadata["services"] = ensure["services"]
        text = visible_text(tool, outcome)
        if notes:
            text = text.rstrip("\n") + "\n\n" + "\n".join(notes) + "\n"
        return {
            "content": [{"type": "text", "text": text}],
            "isError": False,
            "_meta": {"toolahead": {
                **metadata,
                "exit_code": outcome.exit_code,
            }},
        }

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return None
        request_id = request.get("id")
        method = request.get("method")
        if request_id is None:
            return None
        try:
            if method == "initialize":
                self._start_turn()
                params = request.get("params") or {}
                version = params.get("protocolVersion", "2025-06-18") \
                    if isinstance(params, dict) else "2025-06-18"
                result = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "toolahead", "version": VERSION},
                    "instructions": (
                        "Use ToolAhead read_file, search, and list_files for workspace "
                        "inspection; edit_file and write_file for file mutations; and run "
                        "for opted-in test/lint commands and commands declared in "
                        "toolahead.toml. Inputs follow familiar coding-agent conventions. "
                        "Cache hits and cold calls have the same visible output; mutations "
                        "always execute normally and are never replayed."
                    ),
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = request.get("params") or {}
                if not isinstance(params, dict):
                    raise ToolContractError("tools/call params must be an object")
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    raise ToolContractError("tools/call requires name and arguments")
                result = self.call_tool(name, arguments)
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            elif method == "logging/setLevel":
                result = {}
            else:
                return {"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32601,
                                  "message": f"Method not found: {method}"}}
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except ToolContractError as exc:
            if method == "tools/call":
                return {"jsonrpc": "2.0", "id": request_id, "result": {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                    "_meta": {"toolahead": {"cache": "error"}},
                }}
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602, "message": str(exc)}}
        except Exception as exc:  # fail one request, never corrupt stdio framing
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32603, "message": str(exc)}}


def serve_stdio(server: ToolAheadMCP) -> int:
    for raw in sys.stdin.buffer:
        try:
            request = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            continue
        response = server.handle(request)
        if response is not None:
            payload = json.dumps(response, ensure_ascii=False,
                                 separators=(",", ":"))
            sys.stdout.write(payload + "\n")
            sys.stdout.flush()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="toolahead-mcp")
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--url", default=os.environ.get("TOOLAHEAD_URL", DEFAULT_URL))
    parser.add_argument("--lookup-wait", type=float, default=float(os.environ.get(
        "TOOLAHEAD_MCP_LOOKUP_WAIT", "0.25")))
    parser.add_argument("--replay-wait", type=float, default=float(os.environ.get(
        "PREFETCH_REPLAY_WAIT", "125")))
    parser.add_argument("--command-timeout", type=float, default=float(os.environ.get(
        "PREFETCH_COMMAND_TIMEOUT", "120")))
    parser.add_argument("--no-events", action="store_true",
                        default=os.environ.get("TOOLAHEAD_MCP_EVENTS") == "0")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server = ToolAheadMCP(
        args.workspace, args.url, lookup_wait=args.lookup_wait,
        replay_wait=args.replay_wait, command_timeout=args.command_timeout,
        report_events=not args.no_events)
    return serve_stdio(server)


if __name__ == "__main__":
    raise SystemExit(main())
