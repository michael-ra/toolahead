"""Spekulativer Tool-Prefetcher — echter HTTP-Proxy (stdlib, keine Abhängigkeiten).

Sitzt als transparenter Pass-Through vor der Anthropic-Messages-API
(`ANTHROPIC_BASE_URL=http://localhost:4242`). Er

  1. reicht /v1/messages an den Upstream durch und bewahrt die dekodierten
     SSE-Payload-Bytes (HTTP-Framing wird korrekt neu aufgebaut),
  2. liest den Stream MIT: Thinking-Deltas → Intent-Prefetch (Stufe 0),
     tool_use-Blöcke → Transition-Table (Stufe 1),
  3. führt vorhergesagte idempotente Tools (Read/Grep) und whitelisted
     Test-Kommandos spekulativ gegen den Workspace aus — hinter einem
     Erwartungswert-Gate (teure Kommandos nur bei hoher Konfidenz),
  4. führt jeden spekulativen Bash-/Testlauf ausschliesslich in einer
     Wegwerfkopie aus; erfolgreiche Edits/Writes erhöhen eine Mutation-
     Generation, brechen überholte Läufe ab und starten nur die neueste
     Vorhersage nach einem kurzen Debounce neu,
  5. bietet /__prefetch/lookup (Serving via PreToolUse-Hook) und
     /__prefetch/stats (Status-Command).

Upstream ist frei konfigurierbar (UPSTREAM_URL) — dadurch lässt sich der
Proxy VOR einen bereits genutzten Proxy/Gateway hängen (Chaining):
  Claude Code → dieser Proxy → dein Proxy → Provider.

Env:
  PREFETCH_PORT (4242) · UPSTREAM_URL (https://api.anthropic.com)
  PREFETCH_WORKSPACE (cwd) · PREFETCH_TABLE (<ws>/.prefetch-table.json)
  PREFETCH_BASH_CONF (0.6) · PREFETCH_MAX_EXPENSIVE (1)
  PREFETCH_LOOKUP_WAIT (0; Hook-Lookup blockiert nicht)
  PREFETCH_MUTATION_DEBOUNCE_MS (50; schnelle Writes zusammenfassen)
"""

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .services import ServiceManager
from .speculator import Context, TransitionTable, parse_intents
from .telemetry import LatencyTracker
from .tool_contracts import (
    ToolContractError,
    ToolOutcome,
    execute as execute_contract,
    normalize_edit_input,
    normalize_list_input,
    normalize_read_input,
    normalize_run_input,
    normalize_search_input,
    normalize_write_input,
    replayable_command,
    resolve_path as resolve_contract_path,
)
from .watcher import WorkspaceWatcher


# ------------------------------------------------------------- Tool-Abstraktion

EXPENSIVE = {"bash"}


def cc_toolcall(name: str, inp: dict) -> tuple[str, dict]:
    """Claude-Code-Toolname + Input → (tool, args)."""
    n = (name or "").lower()
    # MCP clients expose namespaced names such as
    # ``mcp__toolahead__read_file``.  Normalize only our own namespace; a tool
    # from another MCP server must not accidentally enter ToolAhead's cache.
    if n.startswith("mcp__toolahead__"):
        n = n.rsplit("__", 1)[-1]
    if n in ("read", "read_file"):
        try:
            normalized = normalize_read_input(inp)
        except ToolContractError:
            normalized = dict(inp)
            normalized.setdefault("file_path", inp.get("path", ""))
        return "read", {"path": normalized.get("file_path", ""),
                        "input": normalized}
    if n in ("grep", "search"):
        try:
            normalized = normalize_search_input(inp)
        except ToolContractError:
            normalized = dict(inp)
        return "grep", {"pattern": normalized.get("pattern", ""),
                        "path": normalized.get("path", "."),
                        "input": normalized}
    if n in ("glob", "list_files"):
        try:
            normalized = normalize_list_input(inp)
        except ToolContractError:
            normalized = dict(inp)
        return "glob", {"pattern": normalized.get("pattern", ""),
                        "path": normalized.get("path", "."),
                        "input": normalized}
    if n in ("bash", "run"):
        try:
            normalized = normalize_run_input(inp)
        except ToolContractError:
            normalized = dict(inp)
        return "bash", {"command": (normalized.get("command") or "").strip(),
                        "input": normalized}
    if n in ("edit", "edit_file"):
        try:
            normalized = normalize_edit_input(inp)
        except ToolContractError:
            normalized = dict(inp)
        return "edit", {"path": normalized.get("file_path", ""),
                        "old": normalized.get("old_string", ""),
                        "new": normalized.get("new_string", ""),
                        "input": normalized}
    if n in ("write", "write_file"):
        try:
            normalized = normalize_write_input(inp)
        except ToolContractError:
            normalized = dict(inp)
        # A write is a mutation signal, not a cacheable/predictable operation.
        # Canonicalizing it as edit lets the learned edit -> test transition
        # fire after the real write completes.
        return "edit", {"path": normalized.get("file_path", ""),
                        "input": normalized}
    # Write/MultiEdit/NotebookEdit lassen sich ohne ihr vollstaendiges
    # Patch-Modell nicht sicher in der Schattenkopie vorwegnehmen.
    if n in ("write", "multiedit", "notebookedit"):
        return n, dict(inp)
    return n, dict(inp)


def agent_toolcall(name: str, inp: dict) -> tuple[str, dict]:
    """Normalisiert lokale Agent-Hook-Tools auf den internen Toolraum.

    Codex meldet Shell/unified-exec kanonisch als ``Bash`` und Datei-Patches
    als ``apply_patch``. Claude-Code-Namen bleiben ebenfalls kompatibel.
    """
    n = (name or "").lower()
    if n in ("bash", "exec_command"):
        return cc_toolcall("Bash", inp)
    if n in ("edit", "edit_file", "write", "write_file"):
        return cc_toolcall(n, inp)
    if n in ("apply_patch", "multiedit"):
        command = inp.get("command", "") if isinstance(inp, dict) else ""
        match = re.search(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$",
                          command, re.MULTILINE)
        return "edit", {"path": match.group(1).strip() if match else ""}
    return cc_toolcall(name, inp)


def _path_key(path: str, workspace: str | None) -> str:
    if not path:
        return ""
    if not workspace:
        return os.path.normpath(path)
    ws = os.path.abspath(workspace)
    absolute = os.path.abspath(path if os.path.isabs(path) else os.path.join(ws, path))
    try:
        if os.path.commonpath((ws, absolute)) == ws:
            return os.path.relpath(absolute, ws).replace(os.sep, "/")
    except ValueError:
        pass
    return absolute


def _command_key(command: str, workspace: str | None) -> str:
    """Konservativer Ausfuehrungs-Key: nur Rand-Whitespace und Workspace-Pfad.

    Insbesondere werden Flags, Quotes und inneres Whitespace NICHT umsortiert:
    der Cache darf zwei nur vermeintlich aequivalente Shell-Befehle nie
    verwechseln.
    """
    value = command.strip()
    if workspace:
        value = value.replace(os.path.abspath(workspace), "$WORKSPACE")
    return value


def _shell_words(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return command.split()


def _bash_family(command: str) -> str:
    words = [os.path.basename(word).lower() for word in _shell_words(command)]
    joined = " ".join(words)
    if "-m unittest" in joined or "unittest" in words:
        return "unittest"
    if any(word in ("pytest", "py.test") for word in words):
        return "pytest"
    if "npm test" in joined or "npm run test" in joined:
        return "npm-test"
    if "yarn test" in joined:
        return "yarn-test"
    if "go test" in joined:
        return "go-test"
    if "cargo test" in joined:
        return "cargo-test"
    if "make test" in joined:
        return "make-test"
    for checker in ("jest", "vitest", "ruff", "eslint", "tsc", "mypy"):
        if checker in words:
            return checker
    return "other"


def _safe_bash_command(command: str) -> bool:
    return replayable_command(command)


def exec_key(tool: str, args: dict, workspace: str | None = None) -> str:
    if tool == "bash":
        return f"bash:{_command_key(args.get('command', ''), workspace)}"
    if tool == "grep":
        raw = dict(args.get("input") or {"pattern": args.get("pattern", "")})
        if raw.get("path"):
            raw["path"] = _path_key(raw["path"], workspace)
        return "grep:" + json.dumps(raw, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"))
    if tool == "glob":
        raw = dict(args.get("input") or {"pattern": args.get("pattern", "")})
        if raw.get("path"):
            raw["path"] = _path_key(raw["path"], workspace)
        return "glob:" + json.dumps(raw, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"))
    if tool == "read":
        raw = dict(args.get("input") or {"file_path": args.get("path", "")})
        raw["file_path"] = _path_key(args.get("path", raw.get("file_path", "")), workspace)
        return "read:" + json.dumps(raw, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"))
    if tool == "edit":
        return f"edit:{_path_key(args.get('path', ''), workspace)}"
    return f"{tool}:?"


def canon_key(tool: str, args: dict, ctx: Context, workspace: str | None = None) -> str:
    path = _path_key(args.get("path", ""), workspace)
    grep1 = _path_key(ctx.last_grep_hits[0], workspace) if ctx.last_grep_hits else None
    if tool == "read" and grep1 and path == grep1:
        return "read:$GREP1"
    if tool == "read":
        return f"read:{path}"
    if tool == "grep":
        # Der konkrete Suchausdruck variiert stark. Fuer die Transition ist
        # nur die Rolle "Suche" stabil; aufloesen/spekulieren kann man diesen
        # groben Key ohne aktuelle Argumente bewusst nicht.
        return "grep"
    if tool == "glob":
        return "glob"
    if tool == "edit":
        return "edit"
    if tool == "bash":
        return f"bash:test:{_bash_family(args.get('command', ''))}"
    return exec_key(tool, args, workspace)


def resolve_canon(key: str, ctx: Context, table: TransitionTable | None = None,
                  workspace: str | None = None) -> tuple[str, dict] | None:
    if table is not None:
        example = table.example(key)
        if (example and example.get("tool") in ("read", "grep", "glob", "bash") and
                isinstance(example.get("args"), dict)):
            resolved_tool = example["tool"]
            resolved_args = dict(example["args"])
            if key == "read:$GREP1" and ctx.last_grep_hits:
                path = ctx.last_grep_hits[0]
                raw = dict(resolved_args.get("input") or {})
                raw["file_path"] = path
                resolved_args.update({"path": path, "input": raw})
            return resolved_tool, resolved_args
    if key == "read:$GREP1":
        if not ctx.last_grep_hits:
            return None
        path = ctx.last_grep_hits[0]
        return "read", {"path": path, "input": {"file_path": path}}
    if key == "edit":
        return None
    if key == "grep":
        return None
    if key == "glob":
        return None
    tool, _, rest = key.partition(":")
    if tool == "bash":
        return "bash", {"command": rest, "input": {"command": rest}}
    if tool == "grep":
        return "grep", {"pattern": rest, "input": {"pattern": rest}}
    if tool == "glob":
        return "glob", {"pattern": rest, "input": {"pattern": rest}}
    if tool == "read":
        path = rest
        return "read", {"path": path, "input": {"file_path": path}}
    return None


# ---------------------------------------------------------------- Prefetch-Core


class AgentSessionState:
    def __init__(self):
        self.ctx = Context()
        self.prev_key = "$START"
        self.fired: set[str] = set()
        self.reasoning_text = ""
        self.pending: dict[str, tuple[str, dict]] = {}

class PrefetchEngine:
    def __init__(self, workspace: str, table_path: str):
        self.workspace = os.path.abspath(workspace)
        self.table_path = table_path
        self.table = TransitionTable.load(table_path)
        self.pool = ThreadPoolExecutor(max_workers=6)
        self.lock = threading.RLock()
        self.table_lock = threading.RLock()
        self.cache: dict[tuple[str, str], dict] = {}
        # Inflight wird absichtlich nach exaktem Tool-Key verfolgt, nicht nach
        # einem ungeprueften Watcher-Token. Der Worker legt das Ergebnis erst
        # nach einem echten Content-Hash unter (exec_key, fingerprint) ab.
        self.inflight: set[str] = set()
        self.inflight_info: dict[str, dict] = {}
        # Latest-mutation-wins: every successful Edit/Write advances a
        # monotonic generation. Expensive work from older generations is
        # cancelled and its newest replacement is queued by exact command key.
        self.mutation_generation = 0
        self.last_mutation_wall: float | None = None
        self.pending_restarts: dict[str, dict] = {}
        self.mutation_timers: dict[str, dict] = {}
        self.mutation_debounce_s = max(
            0.0, float(os.environ.get("PREFETCH_MUTATION_DEBOUNCE_MS", "50"))
            / 1000.0)
        self._shutting_down = False
        self.reservations: dict[str, dict] = {}
        self.expensive_inflight = 0
        # -- Konfiguration (Erwartungswert-Gate) --
        self.sandbox = True  # fuer spekulatives Bash nicht abschaltbar
        self.bash_conf = float(os.environ.get("PREFETCH_BASH_CONF", "0.6"))
        self.max_expensive = int(os.environ.get("PREFETCH_MAX_EXPENSIVE", "1"))
        self.lookup_wait = float(os.environ.get("PREFETCH_LOOKUP_WAIT", "0.0"))
        self.command_timeout = float(os.environ.get("PREFETCH_COMMAND_TIMEOUT", "120"))
        self.replay_wait = float(os.environ.get(
            "PREFETCH_REPLAY_WAIT", str(self.command_timeout + 5)))
        self.cache_limit = int(os.environ.get("PREFETCH_CACHE_LIMIT", "128"))
        self.reservation_ttl = float(os.environ.get("PREFETCH_RESERVATION_TTL", "30"))
        self.replay_commands = self._load_replay_commands()
        # Der Watcher ist nur ein billiges Dedupe-/Diagnose-Signal. Er ist NIE
        # der Korrektheitsbeweis: lookup() hasht am Serve-Zeitpunkt immer frisch.
        self.watcher = WorkspaceWatcher(self.workspace)
        self.telemetry = LatencyTracker()
        self.agent_sessions: dict[str, AgentSessionState] = {}
        self.observed_models: set[str] = set()
        # -- Statistik --
        self.stats = {"scheduled": 0, "hits": 0, "misses": 0, "gated": 0,
                      "sandbox_runs": 0, "saved_s": 0.0, "wasted_s": 0.0,
                      "invalidated": 0, "hash_computed": 0,
                      "serve_hashes": 0, "background_hashes": 0,
                      "workspace_races": 0, "lookup_timeouts": 0,
                      "reservations": 0, "replays": 0,
                      "reservations_abandoned": 0, "client_aborts": 0,
                      "mutations": 0, "superseded_runs": 0,
                      "superseded_s": 0.0, "mutation_restarts": 0,
                      "mutation_coalesced": 0,
                      "replay_invalidated": 0,
                      "external_diverted": 0}
        self.per_tool: dict[str, dict] = {}
        self.log: list[dict] = []
        self.t0 = time.monotonic()
        # Pre-Warming ist strikt von der Result-Speculation getrennt: Services
        # liefern nie gecachte Ergebnisse, nur eliminierte Startlatenz.
        self.services = ServiceManager.load(self.workspace, on_event=self._event)
        self.services.prewarm("start")

    def _load_replay_commands(self) -> set[str]:
        """Explizites Opt-in fuer Befehle, deren Seiteneffekte entbehrlich sind."""
        configured = os.environ.get("PREFETCH_REPLAY_COMMANDS")
        path = os.path.join(self.workspace, ".prefetch-replay.json")
        try:
            if configured:
                data = json.loads(configured)
            else:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
        except (OSError, ValueError, TypeError):
            return set()
        commands = data if isinstance(data, list) else data.get("commands", []) \
            if isinstance(data, dict) else []
        return {_command_key(command, self.workspace) for command in commands
                if isinstance(command, str) and _safe_bash_command(command)}

    def _pt(self, tool: str) -> dict:
        return self.per_tool.setdefault(
            tool, {"scheduled": 0, "served": 0, "wasted_runs": 0,
                   "saved_s": 0.0, "wasted_s": 0.0, "run_s": 0.0})

    # -- Content-adressierter Hash (relative Pfade -> Sandbox-Kopie matcht) --
    def _hash_workspace(self, root: str, kind: str = "background") -> str:
        h = hashlib.sha256()
        # Sort in-place while walking. Wrapping ``os.walk`` in ``sorted`` would
        # eagerly consume the generator before ``dirs[:]`` can prune .git and
        # runtime trees, accidentally hashing every ignored file.
        for dp, dirs, files in os.walk(root):
            # Dependencies und ignorierte Dateien werden bewusst NICHT pauschal
            # ausgeschlossen: Tests koennen davon abhaengen. Nur VCS- und reine
            # Interpreter-/Prefetch-Artefakte sind nicht Teil des Inputs.
            dirs[:] = sorted(d for d in dirs if d not in (
                "__pycache__", ".git", ".toolahead"))
            # The installed Codex hook runtime is ToolAhead machinery rather
            # than a test input.  In particular, replay tickets live here and
            # must not invalidate the exact workspace snapshot they serve.
            relative_dir = os.path.relpath(dp, root).replace(os.sep, "/")
            if relative_dir == ".codex/hooks":
                dirs[:] = [d for d in dirs if d != "toolahead"]
            for f in sorted(files):
                if f.endswith(".pyc") or f.startswith(".prefetch"):
                    continue
                p = os.path.join(dp, f)
                try:
                    with open(p, "rb") as fh:
                        rel = os.path.relpath(p, root).encode()
                        content = fh.read()
                        h.update(len(rel).to_bytes(8, "big")); h.update(rel)
                        h.update(len(content).to_bytes(8, "big")); h.update(content)
                except OSError:
                    pass
        with self.lock:
            self.stats["hash_computed"] += 1
            counter = "serve_hashes" if kind == "serve" else "background_hashes"
            self.stats[counter] += 1
        return h.hexdigest()

    def _hash_scope(self, root: str, target: str, *, kind: str) -> str:
        """Hash one exact tool input scope instead of unrelated workspace data."""

        h = hashlib.sha256()
        root_real = os.path.realpath(root)
        targets: list[str] = []
        if os.path.isfile(target):
            targets = [target]
        elif os.path.isdir(target):
            for dp, dirs, files in os.walk(target):
                dirs[:] = sorted(d for d in dirs if d not in (
                    "__pycache__", ".git", "node_modules", ".toolahead"))
                relative_dir = os.path.relpath(dp, root).replace(os.sep, "/")
                if relative_dir == ".codex/hooks":
                    dirs[:] = [d for d in dirs if d != "toolahead"]
                targets.extend(os.path.join(dp, name) for name in sorted(files))
        for path in targets:
            real = os.path.realpath(path)
            try:
                if os.path.commonpath((root_real, real)) != root_real:
                    continue
            except ValueError:
                continue
            name = os.path.basename(path)
            if name.endswith(".pyc") or name.startswith(".prefetch"):
                continue
            try:
                with open(real, "rb") as handle:
                    relative = os.path.relpath(real, root_real).encode()
                    content = handle.read()
            except OSError:
                continue
            h.update(len(relative).to_bytes(8, "big")); h.update(relative)
            h.update(len(content).to_bytes(8, "big")); h.update(content)
        with self.lock:
            self.stats["hash_computed"] += 1
            counter = "serve_hashes" if kind == "serve" else "background_hashes"
            self.stats[counter] += 1
        return h.hexdigest()

    def _fingerprint_for(self, tool: str, args: dict, root: str, *,
                         kind: str = "background") -> str:
        if tool == "read":
            raw = dict(args.get("input") or {"file_path": args.get("path", "")})
            normalized = normalize_read_input(raw)
            target = resolve_contract_path(root, normalized["file_path"])
            return self._hash_scope(root, target, kind=kind)
        if tool in ("grep", "glob"):
            raw = dict(args.get("input") or {"pattern": args.get("pattern", "")})
            normalized = (normalize_search_input(raw) if tool == "grep"
                          else normalize_list_input(raw))
            target = resolve_contract_path(root, normalized["path"])
            return self._hash_scope(root, target, kind=kind)
        return self._hash_workspace(root, kind=kind)

    def fingerprint(self, root: str | None = None) -> str:
        return self._hash_workspace(root or self.workspace)

    def _resolve_path(self, path: str, root: str | None = None) -> str:
        root = root or self.workspace
        return path if os.path.isabs(path) else os.path.join(root, path)

    def _event(self, kind: str, msg: str):
        self.log.append({"t": round(time.monotonic() - self.t0, 2), "kind": kind, "msg": msg})
        if len(self.log) > 2000:
            self.log = self.log[-1000:]
        if os.environ.get("PREFETCH_QUIET") != "1":
            print(f"  [proxy {time.monotonic() - self.t0:6.2f}s] {msg}", flush=True)

    # -- Sicherheits-Gate --
    def allowed(self, tool: str, args: dict) -> tuple[bool, str]:
        if tool in ("read", "grep", "glob"):
            return True, "idempotent"
        if tool == "bash":
            cmd = args.get("command", "")
            if _safe_bash_command(cmd):
                return True, "whitelisted (sandbox)"
            return False, "bash nicht whitelisted"
        return False, "Schreib-/unbekanntes Tool"

    def predict(self, prev: str) -> tuple[str | None, float]:
        with self.table_lock:
            return self.table.predict(prev)

    def predict_executable(
            self, prev: str, ctx: Context,
    ) -> tuple[str | None, float, tuple[str, dict] | None]:
        """Choose among transitions ToolAhead can actually execute.

        Mutation chains deliberately contain unexecutable ``edit -> edit``
        edges. Conditioning confidence on executable successors lets the
        eventual safe test start after the first edit; latest-mutation-wins
        then cancels/restarts it as further edits arrive.
        """

        candidates: list[tuple[str, float, tuple[str, dict]]] = []
        with self.table_lock:
            for (source, key), count in self.table.counts.items():
                if source != prev or self.table.wrong.get((prev, key), 0) >= 3:
                    continue
                resolved = resolve_canon(key, ctx, self.table, self.workspace)
                if resolved is None:
                    continue
                allowed, _reason = self.allowed(*resolved)
                if allowed:
                    candidates.append((key, float(count), resolved))
        if not candidates:
            return None, 0.0, None
        total = sum(count for _key, count, _resolved in candidates)
        key, count, resolved = max(candidates, key=lambda item: item[1])
        return key, count / total if total else 0.0, resolved

    def resolve_prediction(self, key: str, ctx: Context) -> tuple[str, dict] | None:
        with self.table_lock:
            return resolve_canon(key, ctx, self.table, self.workspace)

    def prefetch_safe_chain(self, prev: str, ctx: Context, *, reason: str,
                            meta: dict | None = None) -> list[str]:
        """Prefetch a learned deterministic chain up to the next mutation.

        Agents frequently emit several independent MCP calls in one batch. A
        one-step predictor then has no lead time after the first result. Walk
        the learned canonical path immediately and schedule every resolvable,
        allowed operation; an edit/write edge is an intentional barrier.
        Serving remains exact-key + content-hash validated.
        """

        scheduled: list[str] = []
        visited = {prev}
        current = prev
        while True:
            key, confidence = self.predict(current)
            if key is None or key in visited:
                break
            visited.add(key)
            resolved = self.resolve_prediction(key, ctx)
            if resolved is None:
                break
            if self._divert_external(resolved[0], resolved[1], reason):
                break
            if not self.allowed(*resolved)[0]:
                break
            self.schedule(
                *resolved,
                reason=f"{reason} chain[{len(scheduled) + 1}] p={confidence:.2f}",
                confidence=confidence,
                meta=meta,
            )
            scheduled.append(key)
            current = key
        return scheduled

    def record_transition(self, prev: str, nxt: str, tool: str, args: dict):
        # Canonical labels may intentionally be coarse (notably ``grep`` and
        # test families).  Store the most common exact call for every cacheable
        # tool so the next prediction is executable, while serving still
        # requires the exact exec key and content fingerprint.
        example = {"tool": tool, "args": args} \
            if tool in ("read", "grep", "glob", "bash") else None
        with self.table_lock:
            self.table.record(prev, nxt, example=example)

    def _agent_state(self, event: dict) -> tuple[str, AgentSessionState]:
        sid = str(event.get("session_id") or event.get("thread_id") or "unknown")
        with self.lock:
            return sid, self.agent_sessions.setdefault(sid, AgentSessionState())

    @staticmethod
    def _mutation_succeeded(event: dict) -> bool:
        """Treat only a completed successful mutation as a new generation."""

        if event.get("error") or event.get("is_error"):
            return False
        response = event.get("tool_response")
        if isinstance(response, dict):
            code = response.get("exit_code")
            if code is not None:
                try:
                    return int(code) == 0
                except (TypeError, ValueError):
                    return False
            if response.get("isError") is True or response.get("error"):
                return False
        return True

    def note_mutation(self, meta: dict | None = None) -> int:
        """Advance the workspace generation and supersede older expensive work.

        The content hash remains the serving proof. The generation exists to
        stop wasting time on intermediate states and to ensure a prediction
        skipped by inflight deduplication is restarted for the newest state.
        """

        timers: list[threading.Timer] = []
        carried: dict[str, dict] = {}
        cancelled = 0
        coalesced = 0
        with self.lock:
            self.last_mutation_wall = time.monotonic()
            self.mutation_generation += 1
            generation = self.mutation_generation
            self.stats["mutations"] += 1

            for xkey, item in list(self.mutation_timers.items()):
                if int(item.get("generation", -1)) < generation:
                    timers.append(item["timer"])
                    request = dict(item.get("request") or {})
                    if request:
                        carried[xkey] = request
                    del self.mutation_timers[xkey]
                    coalesced += 1

            for xkey, request in list(self.pending_restarts.items()):
                if int(request.get("generation", -1)) < generation:
                    carried[xkey] = dict(request)
                    del self.pending_restarts[xkey]
                    coalesced += 1

            for info in self.inflight_info.values():
                if info.get("tool") not in EXPENSIVE:
                    continue
                if int(info.get("generation", 0)) >= generation:
                    continue
                cancel_event = info.get("cancel_event")
                if isinstance(cancel_event, threading.Event) \
                        and not cancel_event.is_set():
                    info["superseded_by"] = generation
                    cancel_event.set()
                    cancelled += 1
                    carried[exec_key(info["tool"], info["args"],
                                     self.workspace)] = {
                        "tool": info["tool"], "args": dict(info["args"]),
                        "reason": info.get("reason", "Latest mutation"),
                        "confidence": info.get("confidence", 1.0),
                        "meta": dict(info.get("meta") or {}),
                    }

            self.stats["mutation_coalesced"] += coalesced

        for timer in timers:
            timer.cancel()
        # Carry the exact previously predicted command forward even if the
        # newly observed edit->edit transition temporarily lowers predictor
        # confidence. This is the core latest-state guarantee.
        for request in carried.values():
            carried_meta = dict(request.get("meta") or {})
            carried_meta.update(dict(meta or {}))
            carried_meta["mutation_generation"] = generation
            carried_meta["_mutation_restart"] = True
            self.schedule(
                request["tool"], request["args"],
                reason=f"Latest mutation generation {generation}",
                confidence=max(float(request.get("confidence", 1.0)),
                               self.bash_conf),
                meta=carried_meta)
        detail = f"Generation {generation}"
        if cancelled:
            detail += f", {cancelled} alter Lauf abgebrochen"
        if coalesced:
            detail += f", {coalesced} Vorhersage(n) zusammengefasst"
        self._event("mutation", f"↻ Latest mutation wins: {detail}")
        # Edits kuendigen typischerweise Test-/E2E-Laeufe an: deklarierte
        # Services jetzt hochfahren, damit sie beim echten Call warm sind.
        self.services.prewarm("mutation")
        return generation

    def handle_agent_event(self, event: dict) -> dict:
        """Verarbeitet provider-neutrale Hook- und Reasoning-Ereignisse.

        Hooks liefern den stabilen Lern-/Replay-Pfad. Reasoning-Deltas kommen
        separat aus ``codex exec --json`` oder einem App-Server-Adapter und
        dienen nur als zusaetzliches, fehlertolerantes Prefetch-Signal.
        """
        name = str(event.get("hook_event_name") or event.get("event") or "")
        model = event.get("model")
        if isinstance(model, str) and model:
            with self.lock:
                self.observed_models.add(model)
        sid, state = self._agent_state(event)
        meta = {"session_id": sid, "turn_id": event.get("turn_id"),
                "source": event.get("source", "agent")}

        if name == "Reasoning":
            self.telemetry.record_reasoning(event)
            text = event.get("text", "")
            if not isinstance(text, str) or not text:
                return {"ok": True, "recorded": "Reasoning", "scheduled": 0}
            # Nur ein kurzes In-Memory-Fenster halten; weder Snapshot noch Log
            # enthalten Reasoning-Inhalt.
            state.reasoning_text = (state.reasoning_text + text)[-4096:]
            scheduled = 0
            for key in parse_intents(state.reasoning_text):
                if key in state.fired:
                    continue
                state.fired.add(key)
                resolved = self.resolve_prediction(key, state.ctx)
                if resolved:
                    self.schedule(*resolved, reason="Codex reasoning intent",
                                  confidence=1.0, meta=meta)
                    scheduled += 1
            return {"ok": True, "recorded": "Reasoning", "scheduled": scheduled}

        tool_name = event.get("tool_name") or event.get("tool") or ""
        tool_input = event.get("tool_input") or event.get("input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        tool, args = agent_toolcall(str(tool_name), tool_input)
        annotated = dict(event)
        annotated.setdefault("source", "codex-hook")
        annotated["label"] = canon_key(tool, args, state.ctx, self.workspace) \
            if tool else "none"
        recorded = self.telemetry.record_hook(annotated)

        if name == "SessionStart":
            return {"ok": True, **recorded}

        if name == "UserPromptSubmit":
            state.prev_key = "$START"
            state.fired.clear()
            state.reasoning_text = ""
            chain = self.prefetch_safe_chain(
                state.prev_key, state.ctx, reason="Codex turn start", meta=meta)
            nxt, conf = self.predict(state.prev_key)
            return {"ok": True, **recorded, "prediction": nxt,
                    "confidence": round(conf, 3) if nxt else None,
                    "prefetch_chain": chain}

        call_id = str(event.get("tool_use_id") or event.get("item_id") or "")
        if name == "PreToolUse":
            if call_id:
                with self.lock:
                    state.pending[call_id] = (tool, args)
            if tool == "bash":
                # Letzte Chance vor einem nativen Lauf: deklarierte
                # Prerequisites (falls noch kalt) asynchron hochfahren.
                self.services.ensure_for(args.get("command", ""))
            return {"ok": True, **recorded, "tool": tool,
                    "label": annotated["label"]}

        if name == "PostToolUse":
            if call_id:
                with self.lock:
                    original = state.pending.pop(call_id, None)
                if original:
                    tool, args = original
            cur = canon_key(tool, args, state.ctx, self.workspace) if tool else "unknown"
            previous = state.prev_key
            self.record_transition(previous, cur, tool, args)
            mutation_generation = None
            mutation_failed = tool == "edit" and not self._mutation_succeeded(event)
            if tool == "edit" and not mutation_failed:
                mutation_generation = self.note_mutation(meta)
            if tool == "grep":
                response = event.get("tool_response")
                if isinstance(response, dict):
                    result_text = response.get("stdout") or response.get("output") or ""
                else:
                    result_text = response or ""
                if isinstance(result_text, str):
                    hits = []
                    for line in result_text.splitlines():
                        path = line.split(":", 1)[0].strip()
                        if path and path not in hits:
                            hits.append(path)
                    if hits:
                        state.ctx.last_grep_hits = hits
            state.prev_key = cur
            state.fired.clear()
            state.reasoning_text = ""
            prediction_meta = dict(meta)
            if mutation_generation is not None:
                prediction_meta["mutation_generation"] = mutation_generation
            chain: list[str] = []
            if not mutation_failed and tool == "edit":
                # Mutation traces legitimately contain edit->edit->test. The
                # first executable successor is the test; latest-mutation-wins
                # restarts it for each newer edit.
                nxt, conf, resolved = self.predict_executable(cur, state.ctx)
                if resolved:
                    self.schedule(*resolved,
                                  reason=f"Codex PostToolUse {cur} p={conf:.2f}",
                                  confidence=conf, meta=prediction_meta)
                    chain = [nxt] if nxt else []
            elif not mutation_failed:
                chain = self.prefetch_safe_chain(
                    cur, state.ctx, reason=f"Codex PostToolUse {cur}",
                    meta=prediction_meta)
                nxt, conf = self.predict(cur)
            else:
                nxt, conf = self.predict(cur)
            self.save()
            return {"ok": True, **recorded, "transition": [previous, cur],
                    "prediction": nxt, "confidence": round(conf, 3) if nxt else None,
                    "mutation_generation": mutation_generation,
                    "prefetch_chain": chain}

        if name in ("Stop", "SessionEnd"):
            self.save()
        return {"ok": True, **recorded}

    def _divert_external(self, tool: str, args: dict, reason: str) -> bool:
        """Externe Kommandos: Pre-Warming statt Result-Speculation.

        Ein per ``toolahead.toml`` deklariertes Kommando haengt von laufenden
        Services ab. Sein Ergebnis ist keine reine Funktion der Dateien, also
        wird es nie vorab ausgefuehrt oder gecacht — stattdessen werden die
        deklarierten Prerequisites asynchron hochgefahren.
        """
        if tool != "bash" or not self.services.enabled:
            return False
        names = self.services.requirements_for(args.get("command", ""))
        if not names:
            return False
        with self.lock:
            self.stats["external_diverted"] += 1
        self._event("prewarm",
                    f"≋ extern: {exec_key(tool, args, self.workspace)} → "
                    f"Pre-Warming {', '.join(names)} ({reason})")
        self.services.ensure_async(names)
        return True

    # -- Spekulative Ausführung (mit Erwartungswert-Gate) --
    def schedule(self, tool: str, args: dict, reason: str, confidence: float = 1.0,
                 meta: dict | None = None):
        if self._divert_external(tool, args, reason):
            return False
        ok, _why = self.allowed(tool, args)
        if not ok:
            return False
        # Do not rerun a prediction that is already cached for the exact
        # current bytes. This matters when a turn-start chain finishes before
        # the agent reports its first tool result and predicts the same suffix.
        xkey = exec_key(tool, args, self.workspace)
        with self.lock:
            has_cached_key = any(key[0] == xkey for key in self.cache)
        if has_cached_key:
            fp = self._fingerprint_for(tool, args, self.workspace)
            with self.lock:
                if (xkey, fp) in self.cache:
                    return False
        # Erwartungswert-Gate: teure Kommandos nur bei hoher Konfidenz + Budget.
        if tool in EXPENSIVE:
            if confidence < self.bash_conf:
                self.stats["gated"] += 1
                self._event("gate", f"⊘ EV-Gate: {xkey} übersprungen "
                                    f"(Konfidenz {confidence:.2f} < {self.bash_conf:.2f})")
                return False

        metadata = dict(meta or {})
        explicit_generation = metadata.get("mutation_generation")
        with self.lock:
            generation = self.mutation_generation
        if explicit_generation is not None:
            try:
                generation = int(explicit_generation)
            except (TypeError, ValueError):
                pass
        request = {
            "tool": tool,
            "args": dict(args),
            "reason": reason,
            "confidence": confidence,
            "meta": metadata,
            "generation": generation,
            "created": time.monotonic(),
            "restart": bool(metadata.pop("_mutation_restart", False)),
        }
        if tool in EXPENSIVE and explicit_generation is not None \
                and self.mutation_debounce_s > 0:
            return self._debounce_mutation_schedule(request)
        return self._schedule_now(request)

    def _debounce_mutation_schedule(self, request: dict) -> bool:
        """Briefly coalesce adjacent writes before starting expensive work."""

        xkey = exec_key(request["tool"], request["args"], self.workspace)
        previous = None
        kept_newer = False
        timer: threading.Timer
        with self.lock:
            if self._shutting_down:
                return False
            # HTTP hook events can finish out of order.  Never let a late
            # generation-N callback replace work already queued for N+1.  If
            # there is no newer timer, promote the still-useful exact command
            # hypothesis to the current workspace generation.
            request = dict(request)
            request["meta"] = dict(request.get("meta") or {})
            if int(request.get("generation", -1)) < self.mutation_generation:
                request["generation"] = self.mutation_generation
                request["meta"]["mutation_generation"] = self.mutation_generation
                request["restart"] = True
                self.stats["mutation_coalesced"] += 1
            previous = self.mutation_timers.get(xkey)
            if previous and int(previous.get("generation", -1)) \
                    >= int(request["generation"]):
                self.stats["mutation_coalesced"] += 1
                kept_newer = True
            else:
                previous = self.mutation_timers.pop(xkey, None)
            if previous and not kept_newer:
                self.stats["mutation_coalesced"] += 1
            if not kept_newer:
                timer = threading.Timer(
                    self.mutation_debounce_s, self._fire_debounced,
                    args=(xkey, int(request["generation"])))
                timer.daemon = True
                self.mutation_timers[xkey] = {
                    "timer": timer,
                    "generation": int(request["generation"]),
                    "request": request,
                }
        if kept_newer:
            self._event("debounce", f"… Mutation {request['generation']}: "
                                    f"veraltete {xkey}-Planung ignoriert")
            return True
        if previous:
            previous["timer"].cancel()
        timer.start()
        self._event("debounce", f"… Mutation {request['generation']}: "
                                f"{xkey} für {self.mutation_debounce_s * 1000:.0f}ms gebündelt")
        return True

    def _fire_debounced(self, xkey: str, generation: int):
        request = None
        with self.lock:
            item = self.mutation_timers.get(xkey)
            if not item or int(item.get("generation", -1)) != generation:
                return
            del self.mutation_timers[xkey]
            if self._shutting_down:
                self.stats["mutation_coalesced"] += 1
                return
            request = item["request"]
            if generation != self.mutation_generation:
                # A mutation advanced after this timer was armed.  The exact
                # test/build command remains a valid hypothesis; run it only
                # against the newest snapshot instead of silently dropping it.
                request = dict(request)
                request["generation"] = self.mutation_generation
                request["restart"] = True
                request["meta"] = dict(request.get("meta") or {})
                request["meta"]["mutation_generation"] = \
                    self.mutation_generation
                self.stats["mutation_coalesced"] += 1
        self._schedule_now(request)

    def _queue_latest_locked(self, xkey: str, request: dict) -> None:
        previous = self.pending_restarts.get(xkey)
        queued = dict(request)
        queued["restart"] = True
        self.pending_restarts[xkey] = queued
        if previous:
            self.stats["mutation_coalesced"] += 1

    def _flush_matching_debounce(self, xkey: str) -> None:
        """Start pending latest-state work when the real call arrives now."""

        item = None
        with self.lock:
            candidate = self.mutation_timers.get(xkey)
            if candidate and int(candidate.get("generation", -1)) \
                    == self.mutation_generation:
                item = self.mutation_timers.pop(xkey)
        if item:
            item["timer"].cancel()
            self._event("debounce", f"↯ {xkey}: durch echten Tool-Aufruf "
                                    "sofort gestartet")
            self._schedule_now(item["request"])

    def _schedule_now(self, request: dict) -> bool:
        tool = request["tool"]
        args = request["args"]
        reason = request["reason"]
        confidence = float(request["confidence"])
        generation = int(request.get("generation", 0))
        xkey = exec_key(tool, args, self.workspace)
        cancel_old = None
        queued = False
        with self.lock:
            if self._shutting_down:
                return False
            if xkey in self.inflight:
                info = self.inflight_info.get(xkey, {})
                if tool in EXPENSIVE and generation > int(info.get("generation", 0)):
                    self._queue_latest_locked(xkey, request)
                    cancel_old = info.get("cancel_event")
                    info["superseded_by"] = generation
                    queued = True
                else:
                    return False
            if not queued and tool in EXPENSIVE \
                    and self.expensive_inflight >= self.max_expensive:
                if request.get("meta", {}).get("mutation_generation") is not None:
                    self._queue_latest_locked(xkey, request)
                    queued = True
                else:
                    self.stats["gated"] += 1
                    self._event("gate", f"⊘ EV-Gate: {xkey} übersprungen "
                                        f"(Budget: {self.expensive_inflight} teure laufen)")
                    return False
            if queued:
                # The old process is terminated outside the lock. Its finally
                # path drains pending_restarts and starts only the newest one.
                pass
            else:
                self.inflight.add(xkey)
                now = time.monotonic()
                run_id = secrets.token_hex(12)
                cancel_event = threading.Event()
                self.inflight_info[xkey] = {
                    "fingerprint": None, "phase": "snapshot",
                    "created": now, "scheduled_at": now,
                    "reason": reason, "confidence": confidence,
                    "meta": dict(request.get("meta") or {}),
                    "tool": tool, "args": dict(args),
                    "generation": generation, "run_id": run_id,
                    "cancel_event": cancel_event,
                }
                if tool in EXPENSIVE:
                    self.expensive_inflight += 1
                self.stats["scheduled"] += 1
                if request.get("restart"):
                    self.stats["mutation_restarts"] += 1
                self._pt(tool)["scheduled"] += 1

        if queued:
            if isinstance(cancel_old, threading.Event):
                cancel_old.set()
            self._event("restart", f"↻ {xkey}: neueste Mutation {generation} wartet auf Neustart")
            return True

        self._event("spec", f"⚡ spekuliere {xkey}  [{reason}; gen={generation}]")
        runner = self._run_bash_isolated if tool == "bash" else self._run_consistent
        self.pool.submit(runner, tool, args, xkey, run_id, cancel_event, generation)
        return True

    def _finish_inflight(self, tool: str, xkey: str, run_id: str):
        restart = None
        with self.lock:
            info = self.inflight_info.get(xkey)
            if not info or info.get("run_id") != run_id:
                return
            self.inflight.discard(xkey)
            self.inflight_info.pop(xkey, None)
            if tool in EXPENSIVE:
                self.expensive_inflight = max(0, self.expensive_inflight - 1)
            if not self._shutting_down and self.pending_restarts:
                # Drop stale queued generations and start only the newest
                # request for the current workspace generation.
                for key, item in list(self.pending_restarts.items()):
                    if int(item.get("generation", -1)) < self.mutation_generation:
                        del self.pending_restarts[key]
                        self.stats["mutation_coalesced"] += 1
                if self.pending_restarts \
                        and self.expensive_inflight < self.max_expensive:
                    newest_key, restart = max(
                        self.pending_restarts.items(),
                        key=lambda pair: (int(pair[1].get("generation", 0)),
                                          float(pair[1].get("created", 0))))
                    del self.pending_restarts[newest_key]
        if restart:
            self._schedule_now(restart)

    def _put_cache(self, xkey: str, fp: str, tool: str, outcome: ToolOutcome,
                   duration: float, sandboxed: bool = False):
        key = (xkey, fp)
        with self.lock:
            info = dict(self.inflight_info.get(xkey, {}))
            self.cache[key] = {"outcome": outcome, "ts": time.monotonic(),
                               "dur": duration, "status": "ready", "tool": tool,
                               "sandboxed": sandboxed,
                               "scheduled_at": info.get("scheduled_at"),
                               "command_started": info.get("command_started"),
                               "reason": info.get("reason"),
                               "confidence": info.get("confidence"),
                               "meta": info.get("meta", {})}
            if len(self.cache) > self.cache_limit:
                oldest = min(self.cache, key=lambda item: self.cache[item]["ts"])
                evicted = self.cache.pop(oldest)
                self.stats["invalidated"] += 1
                self.stats["wasted_s"] += evicted["dur"]

    def _run_consistent(self, tool: str, args: dict, xkey: str, run_id: str,
                        cancel_event: threading.Event, generation: int):
        try:
            fp_before = self._fingerprint_for(tool, args, self.workspace)
            with self.lock:
                if xkey in self.inflight_info:
                    self.inflight_info[xkey].update(
                        {"fingerprint": fp_before, "phase": "running",
                         "command_started": time.monotonic()})
            t0 = time.monotonic()
            outcome = self._do(tool, args, self.workspace)
            dur = time.monotonic() - t0
            fp_after = self._fingerprint_for(tool, args, self.workspace)
            if fp_before != fp_after:
                with self.lock:
                    self.stats["workspace_races"] += 1
                    self.stats["invalidated"] += 1
                    self.stats["wasted_s"] += dur
                self._event("inval", f"✗ {xkey} verworfen: Workspace waehrend Lauf geaendert")
                return
            self._put_cache(xkey, fp_before, tool, outcome, dur)
            self._pt(tool)["run_s"] += dur
            self._event("ready", f"✓ bereit {xkey}  ({dur:.2f}s vorab)")
        except Exception as e:  # noqa: BLE001
            self._event("err", f"Prefetch-Fehler {xkey}: {e}")
        finally:
            self._finish_inflight(tool, xkey, run_id)

    def _copy_workspace(self) -> tuple[str, str]:
        tmp = tempfile.mkdtemp(prefix="prefetch_sandbox_")
        ws = os.path.join(tmp, "ws")
        try:
            shutil.copytree(self.workspace, ws, symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__", ".git",
                                                          ".prefetch*",
                                                          ".toolahead"))
            # Symlinks aus der Kopie duerfen nicht zurueck in den echten Workspace
            # oder an andere beschreibbare Orte zeigen. Sonst koennte ein Test trotz
            # kopiertem cwd reale Dateien mutieren.
            ws_real = os.path.realpath(ws)
            for dp, dirs, files in os.walk(ws):
                for name in dirs + files:
                    path = os.path.join(dp, name)
                    if os.path.islink(path):
                        target = os.path.realpath(path)
                        try:
                            contained = os.path.commonpath((ws_real, target)) == ws_real
                        except ValueError:
                            contained = False
                        if not contained:
                            raise ValueError(f"externer Symlink in Sandbox: {path}")
            return tmp, ws
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def _sandbox_command(self, command: str, ws: str) -> str:
        # Replace the workspace only as a complete shell path component.  A
        # sibling such as /tmp/workspace-backup must remain untouched.
        boundary = r"(?=$|[/\s'\";|&()<>{}\[\],])"
        return re.sub(re.escape(self.workspace) + boundary,
                      lambda _match: ws, command)

    def _record_superseded(self, tool: str, xkey: str, run_id: str,
                           generation: int, duration: float) -> None:
        with self.lock:
            info = self.inflight_info.get(xkey)
            if not info or info.get("run_id") != run_id \
                    or info.get("superseded_accounted"):
                return
            info["superseded_accounted"] = True
            self.stats["superseded_runs"] += 1
            self.stats["superseded_s"] += duration
            self.stats["wasted_s"] += duration
            self.stats["invalidated"] += 1
            pt = self._pt(tool)
            pt["wasted_runs"] += 1
            pt["wasted_s"] += duration
        self._event("superseded", f"↻ {xkey}: Generation {generation} nach "
                                  f"{duration:.2f}s durch neueren Edit ersetzt")

    def _run_bash_isolated(self, tool: str, args: dict, xkey: str, run_id: str,
                           cancel_event: threading.Event, generation: int):
        tmp = None
        started = None
        try:
            if cancel_event.is_set():
                self._record_superseded(tool, xkey, run_id, generation, 0.0)
                return
            tmp, ws = self._copy_workspace()
            if cancel_event.is_set():
                self._record_superseded(tool, xkey, run_id, generation, 0.0)
                return
            fp = self._hash_workspace(ws)
            sandbox_args = dict(args)
            sandbox_args["command"] = self._sandbox_command(args["command"], ws)
            with self.lock:
                info = self.inflight_info.get(xkey)
                if not info or info.get("run_id") != run_id:
                    return
                info.update({"fingerprint": fp, "phase": "running",
                             "command_started": time.monotonic()})
            started = time.monotonic()
            outcome = self._do("bash", sandbox_args, ws,
                               cancel_event=cancel_event)
            dur = time.monotonic() - started
            with self.lock:
                current_generation = self.mutation_generation
                info = self.inflight_info.get(xkey)
                superseded = (cancel_event.is_set()
                              or generation < current_generation
                              or not info or info.get("run_id") != run_id)
            if superseded:
                self._record_superseded(tool, xkey, run_id, generation, dur)
                return
            self._put_cache(xkey, fp, "bash", outcome, dur, sandboxed=True)
            with self.lock:
                self.stats["sandbox_runs"] += 1
            self._pt("bash")["run_s"] += dur
            self._event("ready", f"✓ bereit (Sandbox) {xkey}  ({dur:.2f}s vorab)")
        except Exception as e:  # noqa: BLE001
            self._event("err", f"Sandbox-Fehler {xkey}: {e}")
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
            self._finish_inflight(tool, xkey, run_id)

    def _do(self, tool: str, args: dict, cwd: str,
            cancel_event: threading.Event | None = None) -> ToolOutcome:
        raw = dict(args.get("input") or {})
        if tool == "read":
            raw.setdefault("file_path", args.get("path", ""))
        elif tool == "grep":
            raw.setdefault("pattern", args.get("pattern", ""))
            raw.setdefault("path", args.get("path", "."))
        elif tool == "glob":
            raw.setdefault("pattern", args.get("pattern", ""))
            raw.setdefault("path", args.get("path", "."))
        elif tool == "bash":
            # A sandboxed absolute command has already been rewritten in args;
            # do not re-use the original command retained in ``input``.
            raw["command"] = args.get("command", "")
        return execute_contract(tool, cwd, raw,
                                command_timeout=self.command_timeout,
                                cancel_event=cancel_event)

    def _prune_reservations(self):
        now = time.monotonic()
        with self.lock:
            expired = [token for token, item in self.reservations.items()
                       if now - item["created"] > self.reservation_ttl]
            for token in expired:
                del self.reservations[token]
                self.stats["reservations_abandoned"] += 1

    def _account_hit(self, entry: dict, tool: str, xkey: str,
                     started: float) -> tuple[float, float]:
        wait_s = time.monotonic() - started
        saved = max(0.0, entry["dur"] - wait_s)
        with self.lock:
            self.stats["hits"] += 1
            self.stats["saved_s"] += saved
            pt = self._pt(entry.get("tool", tool))
            pt["served"] += 1
            pt["saved_s"] += saved
        self._event("HIT", f"★ HIT {xkey} — {wait_s:.2f}s Hook+Replay statt "
                           f"{entry['dur']:.2f}s → {saved:.2f}s gespart")
        return wait_s, saved

    def _reserve(self, entry: dict | None, key: tuple[str, str], tool: str,
                 args: dict, xkey: str, started: float, meta: dict | None = None,
                 run_id: str | None = None) -> dict:
        self._prune_reservations()
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.reservations[token] = {"entry": entry, "key": key,
                                        "tool": tool, "args": dict(args),
                                        "xkey": xkey,
                                        "started": started,
                                        "meta": dict(meta or {}),
                                        "run_id": run_id,
                                        "generation": self.mutation_generation,
                                        "created": time.monotonic()}
            self.stats["reservations"] += 1
        return {"hit": True, "status": "ready" if entry else "inflight",
                "token": token,
                "dur_s": round(entry["dur"], 3) if entry else None,
                "waited_s": round(time.monotonic() - started, 3)}

    # -- Serving: Lookup blockiert nicht; ein exakter Inflight wird als Future
    #    reserviert und erst vom eigentlichen Replay-Tool fertiggewartet. --
    def lookup(self, tool: str, args: dict, wait_timeout: float | None = None,
               reserve: bool = False, meta: dict | None = None) -> dict:
        t_call = time.monotonic()
        meta = dict(meta or {})

        def finish(result: dict, entry: dict | None = None,
                   info: dict | None = None) -> dict:
            elapsed = time.monotonic() - t_call
            result.setdefault("lookup_s", round(elapsed, 4))
            self.telemetry.record_lookup(meta, elapsed_s=elapsed,
                                         hit=bool(result.get("hit")),
                                         entry=entry, info=info)
            return result

        wait_timeout = self.lookup_wait if wait_timeout is None else wait_timeout
        # Der echte Claude-Code-Hook replayt vorerst nur Bash. Read/Grep haben
        # eigene strukturierte Ausgabeformen, die diese Demo nicht exakt ersetzt.
        if reserve and tool != "bash":
            return finish({"hit": False, "reason": "replay unsupported for this tool"})
        if tool == "bash" and self.services.external(args.get("command", "")):
            # Defense in depth: extern deklarierte Kommandos werden nie
            # spekuliert, also kann hier auch nie ein Ergebnis liegen.
            return finish({"hit": False, "reason": "external-state command"})
        ok, _why = self.allowed(tool, args)
        if not ok:
            return finish({"hit": False, "reason": "tool not safe for replay"})
        if reserve:
            # ``toolahead allow`` should take effect without restarting a
            # long-running agent session. The file is tiny and read only on an
            # actual replay reservation, not on every prediction.
            refreshed = self._load_replay_commands()
            with self.lock:
                self.replay_commands = refreshed
            if _command_key(args.get("command", ""), self.workspace) \
                    not in refreshed:
                return finish({"hit": False,
                               "reason": "command not opted in for replay"})
        xkey = exec_key(tool, args, self.workspace)
        if reserve and tool == "bash":
            # Debouncing must not make a batched final Edit->Test miss. The
            # request itself is the strongest possible signal to flush now.
            self._flush_matching_debounce(xkey)
        fp = self._fingerprint_for(tool, args, self.workspace, kind="serve")
        key = (xkey, fp)
        with self.lock:
            entry = self.cache.get(key)
            info = self.inflight_info.get(xkey)
        if reserve and entry:
            return finish(self._reserve(entry, key, tool, args, xkey,
                                        t_call, meta),
                          entry=entry)
        if reserve and info and (
                (info.get("phase") == "running" and info.get("fingerprint") == fp)
                or info.get("phase") == "snapshot"):
            # For a running command the snapshot already matches exactly. A
            # just-flushed debounced command may still be copying; reserve its
            # run_id plus the current exact hash. If the eventual snapshot is
            # different, no cache entry can match and replay fails open.
            return finish(self._reserve(None, key, tool, args, xkey, t_call, meta,
                                        run_id=info.get("run_id")),
                          info=info)
        waited = False
        while True:
            with self.lock:
                entry = self.cache.get(key)
                inflight = xkey in self.inflight
            if entry or not inflight:
                break
            waited = True
            if wait_timeout <= 0 or time.monotonic() - t_call >= wait_timeout:
                with self.lock:
                    if wait_timeout > 0:
                        self.stats["lookup_timeouts"] += 1
                break
            # File/search tools often finish in a few milliseconds.  A 50 ms
            # polling quantum would erase the very latency MCP is meant to
            # remove, so keep the future wait responsive without busy-spinning.
            time.sleep(0.01)
        if waited:
            # Der Workspace kann sich waehrend des begrenzten Wartens geaendert
            # haben. Deshalb am eigentlichen Serve-Punkt erneut exakt hashen.
            fp = self._fingerprint_for(tool, args, self.workspace, kind="serve")
            key = (xkey, fp)
            with self.lock:
                entry = self.cache.get(key)
        if entry:
            if reserve:
                return finish(self._reserve(entry, key, tool, args, xkey,
                                            t_call, meta),
                              entry=entry)
            wait_s, saved = self._account_hit(entry, tool, xkey, t_call)
            outcome: ToolOutcome = entry["outcome"]
            return finish({"hit": True, "status": "ready", "result": outcome.combined,
                           "stdout": outcome.stdout, "stderr": outcome.stderr,
                           "exit_code": outcome.exit_code, "saved_s": round(saved, 3),
                           "waited_s": round(wait_s, 3),
                           "dur_s": round(entry["dur"], 3)}, entry=entry)
        with self.lock:
            self.stats["misses"] += 1
        return finish({"hit": False}, info=info)

    def replay(self, token: str, wait_timeout: float | None = None) -> dict:
        self._prune_reservations()
        with self.lock:
            item = self.reservations.pop(token, None)
        if item is None:
            return {"ok": False, "error": "unknown or expired replay token"}
        entry = item["entry"]
        timeout = self.replay_wait if wait_timeout is None else max(0.0, wait_timeout)
        deadline = time.monotonic() + timeout
        while entry is None:
            with self.lock:
                entry = self.cache.get(item["key"])
                current = self.inflight_info.get(item["xkey"])
                running = bool(current) and (
                    not item.get("run_id")
                    or current.get("run_id") == item.get("run_id"))
            if entry is not None:
                break
            if not running:
                return {"ok": False, "error": "speculation failed; run fallback"}
            if time.monotonic() >= deadline:
                with self.lock:
                    self.stats["lookup_timeouts"] += 1
                return {"ok": False, "error": "replay wait expired; run fallback"}
            time.sleep(0.01)
        # A reservation is not a correctness lease. Another agent/editor may
        # mutate the workspace between lookup and replay, so revalidate the
        # exact content hash at the last possible point before returning cached
        # output. Generation is scheduling metadata, not identity: delayed
        # hook delivery can advance it even when bytes are unchanged.
        current_fp = self._fingerprint_for(
            item["tool"], item["args"], self.workspace, kind="serve")
        if current_fp != item["key"][1]:
            with self.lock:
                self.stats["replay_invalidated"] += 1
                self.stats["misses"] += 1
            self._event("inval", f"✗ Replay {item['xkey']} verworfen: "
                                   "Workspace nach Reservierung geändert")
            return {"ok": False,
                    "error": "workspace changed after reservation; run fallback"}
        wait_s, saved = self._account_hit(entry, item["tool"], item["xkey"],
                                          item["started"])
        self.telemetry.record_replay(item.get("meta", {}), entry=entry,
                                     wait_s=wait_s)
        with self.lock:
            self.stats["replays"] += 1
        outcome: ToolOutcome = entry["outcome"]
        return {"ok": True, **outcome.as_json(), "saved_s": round(saved, 3),
                "waited_s": round(wait_s, 3), "dur_s": round(entry["dur"], 3)}

    def invalidate(self):
        # Alte Content-Hashes bleiben als sichere Memoization erhalten. Ein
        # anderer Workspace-Zustand kann sie wegen des Serve-Hashes nicht
        # treffen; die LRU-Grenze raeumt sie spaeter auf.
        self._prune_reservations()

    def client_aborted(self):
        with self.lock:
            self.stats["client_aborts"] += 1
        self._event("abort", "Client hat den Stream abgebrochen; Upstream geschlossen")

    def snapshot(self) -> dict:
        s = dict(self.stats)
        total = s["hits"] + s["misses"]
        s["acceptance_rate"] = round(s["hits"] / total, 3) if total else None
        denom = s["saved_s"] + s["wasted_s"]
        s["efficiency"] = round(s["saved_s"] / denom, 3) if denom else None
        s["net_saved_s"] = round(s["saved_s"], 2)
        s["wasted_s"] = round(s["wasted_s"], 2)
        s["saved_s"] = round(s["saved_s"], 2)
        s["superseded_s"] = round(s["superseded_s"], 2)
        s["delivery_rate"] = round(s["replays"] / s["reservations"], 3) \
            if s["reservations"] else None
        with self.table_lock:
            table_rows = sorted([[p, n, round(c, 1)]
                                 for (p, n), c in self.table.counts.items()],
                                key=lambda x: -x[2])
        return {"stats": s,
                "per_tool": {t: {k: (round(v, 2) if isinstance(v, float) else v)
                                 for k, v in d.items()} for t, d in self.per_tool.items()},
                "config": {"sandbox": self.sandbox, "bash_conf": self.bash_conf,
                           "max_expensive": self.max_expensive,
                           "lookup_wait_s": self.lookup_wait,
                           "replay_wait_s": self.replay_wait,
                           "command_timeout_s": self.command_timeout,
                           "mutation_generation": self.mutation_generation,
                           "mutation_debounce_ms": round(
                               self.mutation_debounce_s * 1000),
                           "replay_commands": len(self.replay_commands),
                           "observed_models": sorted(self.observed_models),
                           "services_trusted": self.services.trusted,
                           "workspace": self.workspace},
                "watcher": {"backend": self.watcher.backend,
                            "generation": self.watcher.generation,
                            "role": "optimization-only"},
                "services": self.services.status(),
                "cache": [k[0] for k in self.cache],
                "inflight": sorted(self.inflight),
                "table": table_rows,
                "latency": self.telemetry.snapshot()}

    def save(self):
        try:
            with self.table_lock:
                self.table.save(self.table_path)
        except OSError:
            pass

    def shutdown(self):
        self.save()
        with self.lock:
            self._shutting_down = True
            timers = [item["timer"] for item in self.mutation_timers.values()]
            self.mutation_timers.clear()
            self.pending_restarts.clear()
            cancel_events = [info.get("cancel_event")
                             for info in self.inflight_info.values()]
        for timer in timers:
            timer.cancel()
        for event in cancel_events:
            if isinstance(event, threading.Event):
                event.set()
        self.watcher.stop()
        self.services.stop_all()
        self.pool.shutdown(wait=True, cancel_futures=True)


# ---------------------------------------------------------- Konversationsstatus

class ConvoState:
    def __init__(self):
        self.ctx = Context()
        self.prev_key = "$START"
        self.fired: set[str] = set()


class Proxy:
    def __init__(self, upstream_url: str, engine: PrefetchEngine):
        u = urlparse(upstream_url)
        self.scheme = u.scheme or "https"
        self.host = u.hostname
        self.port = u.port or (443 if self.scheme == "https" else 80)
        self.engine = engine

    def connect(self):
        if self.scheme == "https":
            return HTTPSConnection(self.host, self.port, timeout=600)
        return HTTPConnection(self.host, self.port, timeout=600)

    def parse_history(self, body: dict) -> ConvoState:
        """Rekonstruiert den Zustand pro Request aus der echten Historie.

        Das ist absichtlich stateless: ein Hash der ersten User-Nachricht brach
        bei Context-Compaction und parallelen Requests. Fehlt nach einer
        Compaction die Tool-Historie, fallen wir sicher auf $START zurueck und
        verlieren nur eine Vorhersage.
        """
        st = ConvoState()
        msgs = body.get("messages", [])
        tool_by_id: dict[str, tuple[str, dict]] = {}
        last_tool: tuple[str, dict] | None = None
        last_completed: tuple[str, dict] | None = None
        for m in msgs:
            content = m.get("content")
            if not isinstance(content, list):
                continue
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "tool_use":
                    last_tool = (blk.get("name"), blk.get("input") or {})
                    if blk.get("id"):
                        tool_by_id[blk["id"]] = last_tool
                elif blk.get("type") == "tool_result":
                    linked = tool_by_id.get(blk.get("tool_use_id"))
                    if linked:
                        last_completed = linked
                    c = blk.get("content")
                    if isinstance(c, list):
                        result_text = " ".join(
                            b.get("text", "") for b in c if isinstance(b, dict))
                    elif isinstance(c, str):
                        result_text = c
                    else:
                        result_text = ""
                    linked_tool = cc_toolcall(*linked)[0] if linked else ""
                    if linked_tool == "grep" and result_text:
                        hits = []
                        for line in result_text.splitlines():
                            path = line.split(":", 1)[0].strip()
                            if path and path not in hits:
                                hits.append(path)
                        if hits:
                            st.ctx.last_grep_hits = hits
        previous = last_completed or last_tool
        if previous:
            tool, args = cc_toolcall(*previous)
            st.prev_key = canon_key(tool, args, st.ctx, self.engine.workspace)
        else:
            st.prev_key = "$START"
        return st

    @staticmethod
    def uses_toolahead_mcp(body: dict) -> bool:
        """Whether MCP lifecycle events are the authoritative signal path."""

        for item in body.get("tools", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").lower().startswith(
                    "mcp__toolahead__"):
                return True
        return False

    def predict_and_prefetch(self, st: ConvoState, reason: str):
        _nxt, conf, resolved = self.engine.predict_executable(
            st.prev_key, st.ctx)
        if resolved:
            self.engine.schedule(*resolved, reason=f"{reason} p={conf:.2f}",
                                 confidence=conf)

    def handle_messages(self, req_headers, body_bytes, wfile, send_status_headers,
                        upstream_path: str = "/v1/messages"):
        try:
            body = json.loads(body_bytes)
        except (ValueError, TypeError):
            body = {}
        st = self.parse_history(body)
        # ToolAhead MCP reports actual Pre/PostToolUse events. Treating the
        # Anthropic history as a second predictor would duplicate work and
        # overweight the same transitions.
        if not self.uses_toolahead_mcp(body):
            self.predict_and_prefetch(st, "Transition-Table")

        conn = self.connect()
        # Transparent: ALLE Header durchreichen außer Hop-by-Hop / was http.client
        # selbst setzt. So gehen Auth (x-api-key/authorization), anthropic-beta,
        # Caching-Header etc. von echtem Claude Code unverändert durch.
        hop = ("host", "content-length", "connection", "transfer-encoding",
               "accept-encoding", "proxy-connection")
        fwd = {k: v for k, v in req_headers.items() if k.lower() not in hop}
        fwd["host"] = f"{self.host}:{self.port}"
        fwd["accept-encoding"] = "identity"
        parser = None
        aborted = False
        try:
            conn.request("POST", upstream_path, body=body_bytes, headers=fwd)
            resp = conn.getresponse()
            ct = resp.getheader("Content-Type", "")
            response_hop = {"connection", "keep-alive", "proxy-authenticate",
                            "proxy-authorization", "te", "trailer",
                            "transfer-encoding", "upgrade", "content-length"}
            passthrough = [(k, v) for k, v in resp.getheaders()
                           if k.lower() not in response_hop]
            send_status_headers(resp.status, passthrough)

            parser = _SSEParser(self, st) if "event-stream" in ct else None
            while True:
                chunk = resp.read(1024)
                if not chunk:
                    break
                _write_chunk(wfile, chunk)
                if parser:
                    parser.feed(chunk)
            _write_chunk(wfile, b"")
        except (BrokenPipeError, ConnectionResetError):
            aborted = True
            self.engine.client_aborted()
        finally:
            conn.close()
        if parser and not aborted:
            parser.finish()


# ---------------------------------------------------------------- SSE-Parser

class _SSEParser:
    def __init__(self, proxy: Proxy, st: ConvoState):
        self.proxy = proxy
        self.st = st
        self.buf = b""
        self.blocks: dict[int, dict] = {}
        self.origin_prev = st.prev_key
        self.seen_tool_keys: list[str] = []

    def feed(self, chunk: bytes):
        self.buf += chunk
        while b"\n\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n\n", 1)
            self._event(raw.decode("utf-8", "replace"))

    def _event(self, block: str):
        data = None
        for line in block.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
        if not data:
            return
        try:
            ev = json.loads(data)
        except ValueError:
            return
        t = ev.get("type")
        if t == "content_block_start":
            cb = ev.get("content_block", {})
            self.blocks[ev.get("index", 0)] = {"type": cb.get("type"),
                                               "name": cb.get("name"), "json": "",
                                               "input": cb.get("input") or {},
                                               "thinking": ""}
        elif t == "content_block_delta":
            idx = ev.get("index", 0)
            d = ev.get("delta", {})
            blk = self.blocks.setdefault(idx, {"type": None, "json": "", "thinking": ""})
            if d.get("type") == "input_json_delta":
                blk["json"] += d.get("partial_json", "")
            elif d.get("type") in ("thinking_delta", "text_delta"):
                blk["thinking"] += d.get("thinking", d.get("text", ""))
                self._on_thinking(blk["thinking"])
        elif t == "content_block_stop":
            self._on_block_done(self.blocks.get(ev.get("index", 0)))

    def _on_thinking(self, text: str):
        for key in parse_intents(text):
            if key not in self.st.fired:
                self.st.fired.add(key)
                resolved = self.proxy.engine.resolve_prediction(key, self.st.ctx)
                if resolved:
                    self.proxy.engine.schedule(*resolved, reason="Intent im Thinking")

    def _on_block_done(self, blk):
        if not blk or blk.get("type") != "tool_use":
            return
        try:
            inp = json.loads(blk["json"]) if blk["json"].strip() else blk.get("input", {})
        except ValueError:
            inp = {}
        raw_name = str(blk.get("name") or "").lower()
        if raw_name.startswith("mcp__toolahead__"):
            # ToolAhead's MCP server reports authoritative Pre/PostToolUse
            # events around the actual execution. The Anthropic stream only
            # says that the model requested the call. Ignoring our own MCP
            # blocks here prevents duplicate learning/prefetch while retaining
            # SSE support for native Claude tools and other MCP servers.
            return
        tool, args = cc_toolcall(blk.get("name"), inp)
        cur = canon_key(tool, args, self.st.ctx, self.proxy.engine.workspace)
        # Mehrere tool_use-Bloecke eines Responses sind Geschwister (parallel),
        # keine kuenstliche Kette A->B. Jeder wird vom Historien-Vorgaenger aus
        # gelernt; die exakten Calls werden trotzdem alle ueberlappt.
        self.proxy.engine.record_transition(self.origin_prev, cur, tool, args)
        self.seen_tool_keys.append(cur)
        self.st.prev_key = cur
        if tool == "edit":
            # Nicht gegen den Client um denselben Edit rennen. Der unmittelbar
            # folgende /v1/messages-Request enthaelt das Edit-Ergebnis; dort
            # startet predict_and_prefetch den Test gegen eine Kopie des nun
            # garantiert echten Post-Edit-Zustands, noch vor dem Modell-Denken.
            return
        # aktuellen Call sofort überlappen …
        self.proxy.engine.schedule(tool, args, reason="Overlap (aktueller Call)")
        # … und die übernächste Aktion vorhersagen.
        self.proxy.predict_and_prefetch(self.st, "Transition-Table (Stream)")

    def finish(self):
        if self.buf.strip():
            self._event(self.buf.decode("utf-8", "replace"))
            self.buf = b""
        self.proxy.engine.save()


# ---------------------------------------------------------------- HTTP-Framing

def _write_chunk(wfile, data: bytes):
    wfile.write(f"{len(data):X}\r\n".encode()); wfile.write(data); wfile.write(b"\r\n")
    wfile.flush()


def make_handler(proxy: Proxy, engine: PrefetchEngine):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            payload = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                engine.client_aborted()

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/__prefetch/stats":
                self._json(200, engine.snapshot())
            elif parsed.path == "/__prefetch/replay":
                token = parse_qs(parsed.query).get("token", [""])[0]
                raw_wait = parse_qs(parsed.query).get("wait_timeout", [None])[0]
                try:
                    wait_timeout = float(raw_wait) if raw_wait is not None else None
                except (TypeError, ValueError):
                    wait_timeout = None
                result = engine.replay(token, wait_timeout=wait_timeout)
                self._json(200 if result.get("ok") else 404, result)
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            path = self.path.split("?", 1)[0]  # Query-String abtrennen (?beta=…)
            engine._event("http", f"POST {self.path}")
            if path == "/__prefetch/lookup":
                try:
                    q = json.loads(body)
                    tool, args = cc_toolcall(q.get("tool"), q.get("input", {}))
                    meta = q.get("meta") if isinstance(q.get("meta"), dict) else {
                        key: q.get(key) for key in
                        ("session_id", "turn_id", "tool_use_id", "source")
                        if q.get(key) is not None
                    }
                    self._json(200, engine.lookup(tool, args,
                                                  wait_timeout=q.get("wait_timeout"),
                                                  reserve=bool(q.get("reserve")),
                                                  meta=meta))
                except Exception as e:  # noqa: BLE001
                    self._json(200, {"hit": False, "error": str(e)})
                return
            if path == "/__prefetch/ensure-services":
                # Blockierend bis Readiness (bounded durch die deklarierten
                # Service-Timeouts). Ohne Deklaration antwortet das sofort.
                try:
                    q = json.loads(body or b"{}")
                    names = q.get("services")
                    if isinstance(q.get("command"), str):
                        names = engine.services.requirements_for(q["command"])
                    names = [n for n in (names or []) if isinstance(n, str)]
                    result = engine.services.ensure(names, report=True) \
                        if names else {"states": {}, "started_now": []}
                    states = result["states"]
                    with engine.lock:
                        last = engine.last_mutation_wall
                    age = round(time.monotonic() - last, 3) \
                        if last is not None else None
                    self._json(200, {
                        "ok": True,
                        "ready": all(v == "ready" for v in states.values()),
                        "trusted": engine.services.trusted,
                        "services": states,
                        "started_now": result["started_now"],
                        "last_mutation_age_s": age,
                    })
                except Exception as e:  # noqa: BLE001
                    self._json(200, {"ok": False, "error": str(e)})
                return
            if path == "/__prefetch/agent-event":
                try:
                    event = json.loads(body)
                    if not isinstance(event, dict):
                        raise ValueError("event must be an object")
                    self._json(200, engine.handle_agent_event(event))
                except Exception as e:  # noqa: BLE001
                    self._json(200, {"ok": False, "error": str(e)})
                return
            if path == "/v1/messages":
                headers_sent = False

                def send(status, headers):
                    nonlocal headers_sent
                    self.send_response(status)
                    for k, v in headers:
                        self.send_header(k, v)
                    self.send_header("x-prefetch-proxy", "1")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    headers_sent = True
                try:
                    proxy.handle_messages(self.headers, body, self.wfile, send,
                                          upstream_path=self.path)
                except Exception as e:  # noqa: BLE001
                    engine._event("err", f"Proxy-Fehler: {e}")
                    if not headers_sent:
                        self._json(502, {"error": "upstream request failed"})
                return
            self._passthrough(body)

        def _passthrough(self, body):
            conn = proxy.connect()
            hop = {"host", "content-length", "connection", "transfer-encoding",
                   "accept-encoding", "proxy-connection", "keep-alive", "te",
                   "trailer", "upgrade"}
            fwd = {k: v for k, v in self.headers.items() if k.lower() not in hop}
            fwd["host"] = f"{proxy.host}:{proxy.port}"
            fwd["accept-encoding"] = "identity"
            try:
                conn.request("POST", self.path, body=body, headers=fwd)
                r = conn.getresponse()
                data = r.read()
                self.send_response(r.status)
                response_hop = {"connection", "keep-alive", "proxy-authenticate",
                                "proxy-authorization", "te", "trailer",
                                "transfer-encoding", "upgrade", "content-length"}
                for key, value in r.getheaders():
                    if key.lower() not in response_hop:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
            finally:
                conn.close()

    return Handler


def build(port=None, upstream=None, workspace=None, table=None):
    port = int(os.environ.get("PREFETCH_PORT", "4242")) if port is None else port
    upstream = upstream or os.environ.get("UPSTREAM_URL", "https://api.anthropic.com")
    workspace = workspace or os.environ.get("PREFETCH_WORKSPACE", os.getcwd())
    table = table or os.environ.get("PREFETCH_TABLE",
                                    os.path.join(workspace, ".prefetch-table.json"))
    engine = PrefetchEngine(workspace, table)
    proxy = Proxy(upstream, engine)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(proxy, engine))
    return httpd, proxy, engine


if __name__ == "__main__":
    httpd, proxy, engine = build()
    print(f"Prefetch-Proxy auf http://127.0.0.1:{httpd.server_address[1]}  "
          f"→ Upstream {proxy.scheme}://{proxy.host}:{proxy.port}  "
          f"| Workspace {engine.workspace}  | Bash-Sandbox=an", flush=True)
    print(f"  ANTHROPIC_BASE_URL=http://127.0.0.1:{httpd.server_address[1]} claude", flush=True)
    print(f"  Status: python3 prefetch_stats.py   (oder GET /__prefetch/stats)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.shutdown()
        httpd.server_close()
