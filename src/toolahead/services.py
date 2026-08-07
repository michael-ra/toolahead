"""Pre-Warming deklarierter Services (``toolahead.toml``).

ToolAhead trennt zwei Beschleunigungspfade strikt:

  • Result-Speculation (``proxy.py``): Ergebnisse werden vorab berechnet und
    aus dem Cache serviert — ausschliesslich fuer Calls, deren Output eine
    reine Funktion der Workspace-Dateien ist (Read/Search/List, allowlisted
    Test-Kommandos). Der Content-Hash ist dort der Korrektheitsbeweis.

  • Pre-Warming (dieses Modul): langlebige, teure Prerequisites — Dev-Server,
    Browser-Instanzen — werden vorab gestartet und warm gehalten. Es wird NIE
    ein vorbereitetes Ergebnis serviert; gewonnen wird nur Startlatenz.

Kommandos, deren Ergebnis von externem State abhaengt (z. B. Playwright gegen
einen laufenden Dev-Server), sind per Deklaration von der Result-Speculation
ausgeschlossen: der Workspace-Hash beweist dort keine Ergebnisgleichheit,
weil der Server-State (HMR-Timing) nicht in den Dateien steckt.

Konfiguration: optionale ``toolahead.toml`` im Workspace-Root. Ohne Datei ist
dieses Modul ein No-Op — Zero-Config-Learning bleibt der Default.

    [services.dev-server]
    command = "npm run dev"
    ready.port = 3000       # oder ready.http = "http://…" / ready.command = "…"
    timeout = 30            # Sekunden bis Readiness (Default 30)
    prewarm = "mutation"    # "mutation" (Default) | "start" | "manual"

    [commands.e2e]
    match = "npx playwright test"   # Praefix-Match auf das exakte Kommando
    requires = ["dev-server"]

Services laufen bewusst unsandboxed gegen den echten Workspace — sie SIND die
Umgebung, gegen die der Agent gleich arbeitet. Deshalb ist die Datei allein
KEINE Vertrauensgrenze: ein geklontes Repo darf durch blosses Oeffnen nichts
ausfuehren. Prozesse werden nur gestartet, wenn der Nutzer die exakte Config
einmal per ``toolahead trust`` freigegeben hat (Workspace-Trust-Modell wie bei
direnv/VS Code). Der SHA-256 der Datei liegt ausserhalb des Repos; jede
Aenderung an der toml hebt die Freigabe automatisch auf. Ohne Freigabe bleibt
nur die sichere Richtung aktiv: deklarierte externe Kommandos werden weiterhin
vom Result-Caching ausgeschlossen.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import socket
import subprocess
import threading
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field

CONFIG_NAME = "toolahead.toml"
PREWARM_TRIGGERS = ("start", "mutation", "manual")
MAX_START_FAILURES = 3


# ------------------------------------------------------------ Workspace-Trust

def _trust_path() -> str:
    override = os.environ.get("TOOLAHEAD_TRUST_FILE")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".toolahead", "trust.json")


def config_digest(workspace: str) -> str | None:
    """SHA-256 der toolahead.toml, ``None`` wenn keine existiert."""
    try:
        with open(os.path.join(os.path.abspath(workspace), CONFIG_NAME),
                  "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def _load_trust() -> dict:
    try:
        with open(_trust_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_trusted(workspace: str) -> bool:
    digest = config_digest(workspace)
    if digest is None:
        return False
    return _load_trust().get(os.path.realpath(workspace)) == digest


def trust_workspace(workspace: str) -> str | None:
    """Gibt die aktuelle Config frei; liefert den gespeicherten Digest."""
    digest = config_digest(workspace)
    if digest is None:
        return None
    path = _trust_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    data = _load_trust()
    data[os.path.realpath(workspace)] = digest
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1)
    return digest


class ServiceConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ReadyCheck:
    kind: str  # "port" | "http" | "command"
    value: object

    @classmethod
    def parse(cls, raw: object) -> "ReadyCheck | None":
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ServiceConfigError(
                "ready muss eine Tabelle sein (ready.port / ready.http / ready.command)")
        keys = [key for key in ("port", "http", "command") if key in raw]
        if len(keys) != 1:
            raise ServiceConfigError(
                "ready braucht genau einen Check: port, http oder command")
        kind = keys[0]
        value = raw[kind]
        if kind == "port":
            if not isinstance(value, int) or not 0 < value < 65536:
                raise ServiceConfigError(f"ready.port ungueltig: {value!r}")
        elif not isinstance(value, str) or not value.strip():
            raise ServiceConfigError(f"ready.{kind} ungueltig: {value!r}")
        return cls(kind, value)

    def probe(self, cwd: str) -> bool:
        if self.kind == "port":
            try:
                with socket.create_connection(("127.0.0.1", int(self.value)),
                                              timeout=0.5):
                    return True
            except OSError:
                return False
        if self.kind == "http":
            # 4xx zaehlt als erreichbar: Dev-Server antworten auf "/" oft 404,
            # sind aber trotzdem fertig hochgefahren.
            try:
                with urllib.request.urlopen(str(self.value), timeout=1.0) as resp:
                    return resp.status < 500
            except urllib.error.HTTPError as exc:
                return exc.code < 500
            except Exception:  # noqa: BLE001 — nicht erreichbar → nicht bereit
                return False
        try:
            proc = subprocess.run(str(self.value), shell=True, cwd=cwd,
                                  capture_output=True, timeout=5)
            return proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    command: str
    ready: ReadyCheck | None = None
    timeout: float = 30.0
    prewarm: str = "mutation"
    cwd: str = "."


@dataclass(frozen=True)
class CommandRule:
    name: str
    match: str
    requires: tuple[str, ...] = field(default_factory=tuple)

    def matches(self, command: str) -> bool:
        normalized = " ".join(command.strip().split())
        target = " ".join(self.match.strip().split())
        if not target:
            return False
        return normalized == target or normalized.startswith(target + " ")


def _parse_service(name: str, raw: object, workspace: str) -> ServiceSpec:
    if not isinstance(raw, dict):
        raise ServiceConfigError("Service-Eintrag muss eine Tabelle sein")
    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ServiceConfigError("command fehlt oder ist leer")
    timeout = raw.get("timeout", 30)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) \
            or not math.isfinite(float(timeout)) or timeout <= 0:
        raise ServiceConfigError(f"timeout ungueltig: {timeout!r}")
    prewarm = raw.get("prewarm", "mutation")
    if prewarm not in PREWARM_TRIGGERS:
        raise ServiceConfigError(
            f"prewarm muss eines von {PREWARM_TRIGGERS} sein, nicht {prewarm!r}")
    cwd = raw.get("cwd", ".")
    if not isinstance(cwd, str):
        raise ServiceConfigError(f"cwd ungueltig: {cwd!r}")
    absolute = os.path.realpath(os.path.join(workspace, cwd))
    workspace_real = os.path.realpath(workspace)
    try:
        contained = os.path.commonpath((workspace_real, absolute)) == workspace_real
    except ValueError:
        contained = False
    if not contained:
        raise ServiceConfigError(f"cwd liegt ausserhalb des Workspace: {cwd!r}")
    return ServiceSpec(name=name, command=command.strip(),
                       ready=ReadyCheck.parse(raw.get("ready")),
                       timeout=float(timeout), prewarm=str(prewarm), cwd=cwd)


def _parse_rule(name: str, raw: object) -> CommandRule:
    if not isinstance(raw, dict):
        raise ServiceConfigError("Command-Eintrag muss eine Tabelle sein")
    match = raw.get("match")
    if not isinstance(match, str) or not match.strip():
        raise ServiceConfigError("match fehlt oder ist leer")
    requires = raw.get("requires", [])
    if not isinstance(requires, list) \
            or not all(isinstance(item, str) for item in requires):
        raise ServiceConfigError("requires muss eine Liste von Service-Namen sein")
    return CommandRule(name=name, match=match.strip(), requires=tuple(requires))


class ServiceManager:
    """Startet, ueberwacht und stoppt deklarierte Services als Prozessgruppen."""

    def __init__(self, workspace: str,
                 specs: dict[str, ServiceSpec] | None = None,
                 rules: list[CommandRule] | None = None,
                 on_event=None):
        self.workspace = os.path.abspath(workspace)
        self.specs = dict(specs or {})
        self.rules = list(rules or [])
        self._on_event = on_event
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen] = {}
        self._failures: dict[str, int] = {}
        self._announced_ready: set[str] = set()
        self._ensuring: set[str] = set()
        self._closed = False
        self.trusted = False
        self._digest: str | None = None
        self._log_dir = os.path.join(self.workspace, ".toolahead", "services")

    # ------------------------------------------------------------- Laden

    @classmethod
    def load(cls, workspace: str, on_event=None) -> "ServiceManager":
        manager = cls(workspace, on_event=on_event)
        path = os.path.join(os.path.abspath(workspace), CONFIG_NAME)
        try:
            with open(path, "rb") as handle:
                data = tomllib.load(handle)
        except FileNotFoundError:
            return manager
        except (OSError, tomllib.TOMLDecodeError) as exc:
            manager._event("warn", f"{CONFIG_NAME} ignoriert: {exc}")
            return manager
        raw_services = data.get("services", {})
        if isinstance(raw_services, dict):
            for name, raw in raw_services.items():
                try:
                    manager.specs[str(name)] = _parse_service(
                        str(name), raw, manager.workspace)
                except ServiceConfigError as exc:
                    manager._event("warn",
                                   f"{CONFIG_NAME}: services.{name} ignoriert: {exc}")
        raw_commands = data.get("commands", {})
        if isinstance(raw_commands, dict):
            for name, raw in raw_commands.items():
                try:
                    rule = _parse_rule(str(name), raw)
                except ServiceConfigError as exc:
                    manager._event("warn",
                                   f"{CONFIG_NAME}: commands.{name} ignoriert: {exc}")
                    continue
                unknown = [item for item in rule.requires
                           if item not in manager.specs]
                if unknown:
                    manager._event(
                        "warn", f"{CONFIG_NAME}: commands.{name} verweist auf "
                                f"unbekannte Services {unknown}; Eintrag ignoriert")
                    continue
                manager.rules.append(rule)
        manager._digest = config_digest(workspace)
        manager.trusted = is_trusted(workspace)
        if manager.enabled:
            manager._event(
                "prewarm", f"⚙ {CONFIG_NAME}: {len(manager.specs)} Service(s), "
                           f"{len(manager.rules)} externe(s) Kommando(s)"
                           + ("" if manager.trusted else " — NICHT vertraut"))
            if not manager.trusted and manager.specs:
                manager._event(
                    "warn", f"{CONFIG_NAME} ist nicht freigegeben; es werden "
                            "keine Services gestartet. Freigeben mit: "
                            "toolahead trust")
        return manager

    # ------------------------------------------------------------ Abfragen

    @property
    def enabled(self) -> bool:
        return bool(self.specs or self.rules)

    def refresh_trust(self) -> bool:
        """``toolahead trust`` soll ohne Daemon-Neustart wirken.

        Vertraut wird nur der beim Laden geparste Stand: weicht die Datei
        inzwischen ab, bleibt es konservativ bei "nicht vertraut", bis der
        Daemon sie neu laedt.
        """
        if self._digest is None:
            return self.trusted
        digest = config_digest(self.workspace)
        self.trusted = digest is not None and digest == self._digest \
            and _load_trust().get(os.path.realpath(self.workspace)) == digest
        return self.trusted

    def requirements_for(self, command: str) -> list[str]:
        """Deklarierte Services, die vor diesem Kommando laufen muessen."""
        if not isinstance(command, str) or not command.strip():
            return []
        names: list[str] = []
        for rule in self.rules:
            if rule.matches(command):
                for name in rule.requires:
                    if name not in names:
                        names.append(name)
        return names

    def external(self, command: str) -> bool:
        """True fuer Kommandos, deren Ergebnis von Service-State abhaengt.

        Solche Kommandos duerfen nie result-gecacht werden: der Workspace-Hash
        beweist ihre Ergebnisgleichheit nicht.
        """
        return bool(self.requirements_for(command))

    def status(self) -> dict:
        with self._lock:
            procs = dict(self._procs)
            failures = dict(self._failures)
        result = {}
        for name, spec in self.specs.items():
            proc = procs.get(name)
            if proc is None:
                if failures.get(name, 0) >= MAX_START_FAILURES:
                    state = "disabled"
                elif not self.trusted:
                    state = "untrusted"
                else:
                    state = "stopped"
                pid = None
            elif proc.poll() is not None:
                state = "exited"
                pid = None
            else:
                ready = spec.ready is None or spec.ready.probe(
                    os.path.join(self.workspace, spec.cwd))
                state = "ready" if ready else "starting"
                pid = proc.pid
            result[name] = {"state": state, "pid": pid,
                            "prewarm": spec.prewarm,
                            "ready_check": spec.ready.kind if spec.ready else None,
                            "failures": failures.get(name, 0)}
        return result

    # ------------------------------------------------------- Start & Warten

    def prewarm(self, trigger: str) -> list[str]:
        names = [name for name, spec in self.specs.items()
                 if spec.prewarm == trigger]
        self.ensure_async(names)
        return names

    def ensure_for(self, command: str) -> list[str]:
        names = self.requirements_for(command)
        self.ensure_async(names)
        return names

    def ensure_async(self, names: list[str]) -> None:
        """Fire-and-forget-Ensure mit Dedupe: pro Service maximal ein
        wartender Hintergrund-Thread, egal wie schnell Events feuern."""
        if not names or self._closed or not self.refresh_trust():
            return
        with self._lock:
            fresh = [name for name in dict.fromkeys(names)
                     if name in self.specs and name not in self._ensuring]
            self._ensuring.update(fresh)
        if not fresh:
            return

        def worker():
            try:
                self.ensure(fresh)
            finally:
                with self._lock:
                    self._ensuring.difference_update(fresh)

        threading.Thread(target=worker, daemon=True).start()

    def ensure(self, names: list[str], wait: float | None = None,
               report: bool = False):
        """Startet Services falls noetig und wartet bounded auf Readiness.

        Mit ``report=True`` kommt zusaetzlich zurueck, welche Services in
        diesem Aufruf frisch gestartet wurden — ein frisch gestarteter Server
        serviert sicher den aktuellen Datei-Stand, ein bereits laufender kann
        noch mitten im Hot-Reload stecken.
        """
        requested = [name for name in dict.fromkeys(names) if name in self.specs]
        if requested and not self.refresh_trust():
            states = {name: "untrusted" for name in requested}
            return {"states": states, "started_now": []} if report else states
        if not requested or self._closed:
            return {"states": {}, "started_now": []} if report else {}
        states: dict[str, str] = {}
        pending: list[str] = []
        started_now: list[str] = []
        for name in requested:
            was_running = self._running(name)
            state = self._start_if_needed(name)
            states[name] = state
            if not was_running and state in ("starting", "ready"):
                started_now.append(name)
            if state == "starting":
                pending.append(name)
        if wait is None:
            wait = max(self.specs[name].timeout for name in requested)
        deadline = time.monotonic() + max(0.0, wait)
        while pending and not self._closed:
            for name in list(pending):
                state = self._poll(name)
                states[name] = state
                if state != "starting":
                    pending.remove(name)
            if not pending or time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        for name in pending:
            # Prozess bleibt bewusst am Leben: ein Dev-Server kann nach dem
            # Warte-Budget immer noch fertig hochfahren.
            states[name] = "timeout"
            self._event("prewarm",
                        f"… Service {name} nach {wait:.0f}s noch nicht bereit")
        return {"states": states, "started_now": started_now} if report \
            else states

    def _running(self, name: str) -> bool:
        with self._lock:
            proc = self._procs.get(name)
        return proc is not None and proc.poll() is None

    def _start_if_needed(self, name: str) -> str:
        spec = self.specs[name]
        with self._lock:
            if self._closed or not self.trusted:
                return "stopped"
            proc = self._procs.get(name)
            if proc is not None and proc.poll() is not None:
                self._failures[name] = self._failures.get(name, 0) + 1
                self._announced_ready.discard(name)
                exit_code = proc.returncode
                del self._procs[name]
                proc = None
                self._event("prewarm",
                            f"✗ Service {name} beendet (exit {exit_code})")
            if proc is None:
                if self._failures.get(name, 0) >= MAX_START_FAILURES:
                    return "disabled"
                try:
                    self._procs[name] = self._spawn(spec)
                except OSError as exc:
                    self._failures[name] = self._failures.get(name, 0) + 1
                    self._event("prewarm", f"✗ Service {name} Start fehlgeschlagen: {exc}")
                    return "failed"
                self._event("prewarm", f"▶ Service {name}: {spec.command}")
        return self._poll(name)

    def _spawn(self, spec: ServiceSpec) -> subprocess.Popen:
        os.makedirs(self._log_dir, exist_ok=True)
        log_path = os.path.join(self._log_dir, f"{spec.name}.log")
        log = open(log_path, "ab", buffering=0)  # noqa: SIM115 — lebt mit dem Prozess
        try:
            return subprocess.Popen(
                spec.command, shell=True,
                cwd=os.path.join(self.workspace, spec.cwd),
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True)
        finally:
            log.close()

    def _poll(self, name: str) -> str:
        with self._lock:
            proc = self._procs.get(name)
        if proc is None:
            return "failed" if self._failures.get(name, 0) < MAX_START_FAILURES \
                else "disabled"
        if proc.poll() is not None:
            with self._lock:
                if self._procs.get(name) is proc:
                    self._failures[name] = self._failures.get(name, 0) + 1
                    self._announced_ready.discard(name)
                    del self._procs[name]
            self._event("prewarm",
                        f"✗ Service {name} beendet (exit {proc.returncode})")
            return "failed"
        spec = self.specs[name]
        cwd = os.path.join(self.workspace, spec.cwd)
        if spec.ready is None or spec.ready.probe(cwd):
            announce = False
            with self._lock:
                if name not in self._announced_ready:
                    self._announced_ready.add(name)
                    announce = True
            if announce:
                self._event("prewarm", f"✓ Service {name} bereit")
            return "ready"
        return "starting"

    # ------------------------------------------------------------- Stoppen

    def stop_all(self) -> None:
        with self._lock:
            self._closed = True
            procs = dict(self._procs)
            self._procs.clear()
            self._announced_ready.clear()
        for proc in procs.values():
            if proc.poll() is not None:
                continue
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                proc.terminate()
        deadline = time.monotonic() + 3.0
        for name, proc in procs.items():
            remaining = max(0.1, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
            self._event("prewarm", f"■ Service {name} gestoppt")

    # --------------------------------------------------------------- Intern

    def _event(self, kind: str, msg: str) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(kind, msg)
        except Exception:  # noqa: BLE001 — Diagnose darf nie den Betrieb stoppen
            pass
