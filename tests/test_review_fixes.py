"""Regressionstests fuer die Findings des Adversarial Reviews (0.6.1).

Jeder Test hier reproduziert einen konkret nachgestellten Fehler. Sie sind
absichtlich verhaltensnah geschrieben: die Bugs waren allesamt Zustands-
uebergaenge und Nebenwirkungen, die eine reine Unit-Sicht nicht sieht.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("PREFETCH_QUIET", "1")

from toolahead import cli  # noqa: E402
from toolahead.services import ServiceManager, derive_routes, trust_workspace  # noqa: E402


def _cli(*args) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
    return subprocess.run([sys.executable, "-m", "toolahead.cli", *args],
                          capture_output=True, text=True, env=env)


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


class StrictRollbackTest(unittest.TestCase):
    """Ein spaeterer Default-Init darf keine Deny-Regeln ohne Ersatz hinterlassen."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _claude_deny(self) -> list:
        with open(os.path.join(self.ws, ".claude", "settings.json")) as handle:
            return json.load(handle).get("permissions", {}).get("deny", [])

    def test_default_init_after_strict_restores_native_tools(self):
        _cli("init", "--agent", "both", "--strict", "--project", self.ws)
        self.assertEqual(sorted(self._claude_deny()),
                         sorted(cli.CLAUDE_NATIVE_ANALOGS))
        self.assertTrue(os.path.exists(
            os.path.join(self.ws, ".toolahead", "strict-mcp")))

        _cli("init", "--agent", "both", "--project", self.ws)
        self.assertEqual(self._claude_deny(), [],
                         "native Tools bleiben verboten, obwohl der MCP-Ersatz "
                         "entfernt wurde")
        self.assertFalse(os.path.exists(
            os.path.join(self.ws, ".toolahead", "strict-mcp")),
            "Codex-Strict-Marker ueberlebt den Wechsel auf hooks-only")
        codex_toml = os.path.join(self.ws, ".codex", "config.toml")
        if os.path.exists(codex_toml):
            with open(codex_toml) as handle:
                self.assertNotIn("mcp_servers.toolahead", handle.read())

    def test_unrelated_deny_entries_survive(self):
        settings_dir = os.path.join(self.ws, ".claude")
        os.makedirs(settings_dir, exist_ok=True)
        _write(os.path.join(settings_dir, "settings.json"),
               json.dumps({"permissions": {"deny": ["WebFetch"]}}))
        _cli("init", "--agent", "claude", "--project", self.ws)
        self.assertEqual(self._claude_deny(), ["WebFetch"])

    def test_doctor_passes_on_default_install(self):
        _cli("init", "--agent", "both", "--project", self.ws)
        result = _cli("doctor", "--project", self.ws,
                      "--url", "http://127.0.0.1:9")
        # Der Daemon laeuft im Test nicht; alle uebrigen Checks muessen gruen
        # sein und die MCP-Tools duerfen nicht als Fehler gelten.
        self.assertNotIn("✗ Codex MCP", result.stdout)
        self.assertNotIn("✗ Claude MCP", result.stdout)
        self.assertIn("MCP replay tools", result.stdout)


class HookWiringTest(unittest.TestCase):
    """--url muss im Hook-Command landen, sonst fragt der Hook den falschen Daemon."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_url_reaches_installed_hooks(self):
        _cli("init", "--agent", "all", "--url", "http://127.0.0.1:4999",
             "--project", self.ws)
        with open(os.path.join(self.ws, ".claude", "settings.json")) as handle:
            claude = json.load(handle)["hooks"]
        with open(os.path.join(self.ws, ".codex", "hooks.json")) as handle:
            codex = json.load(handle)["hooks"]
        for hooks in (claude, codex):
            command = hooks["PreToolUse"][0]["hooks"][0]["command"]
            self.assertIn("4999", command)

    def test_pretooluse_timeout_covers_ensure_budget(self):
        _cli("init", "--agent", "all", "--project", self.ws)
        with open(os.path.join(self.ws, ".claude", "settings.json")) as handle:
            claude = json.load(handle)["hooks"]["PreToolUse"][0]
        with open(os.path.join(self.ws, ".codex", "hooks.json")) as handle:
            codex = json.load(handle)["hooks"]["PreToolUse"][0]
        for group in (claude, codex):
            timeout = group["hooks"][0]["timeout"]
            self.assertGreaterEqual(
                timeout, 110,
                "Prozess-Timeout unter dem Ensure-Budget: der Hook wird "
                "gekillt und der Call laeuft ungeprueft los")

    def test_antigravity_stop_uses_flat_handler_list(self):
        _cli("init-antigravity", "--project", self.ws)
        with open(os.path.join(self.ws, ".agents", "hooks.json")) as handle:
            hooks = json.load(handle)["toolahead"]
        self.assertIn("matcher", hooks["PreToolUse"][0])
        self.assertNotIn("matcher", hooks["Stop"][0],
                         "Stop erwartet eine flache Handler-Liste")
        self.assertEqual(hooks["Stop"][0]["type"], "command")


class RouteLearningTest(unittest.TestCase):
    """Gelernt wird nur, was der Agent wirklich abruft — und nie persistent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = self.tmp.name
        os.environ["TOOLAHEAD_TRUST_FILE"] = os.path.join(self.ws, "trust.json")
        _write(os.path.join(self.ws, "toolahead.toml"), """
[services.dev]
command = "sleep 60"
ready.port = 3000
prewarm = "manual"
warm_routes = ["auto"]
""")
        trust_workspace(self.ws)
        os.makedirs(os.path.join(self.ws, "app", "dash"), exist_ok=True)
        _write(os.path.join(self.ws, "app", "dash", "page.tsx"), "x")
        from toolahead.proxy import PrefetchEngine
        self.table = os.path.join(self.ws, ".prefetch-table.json")
        self.engine = PrefetchEngine(self.ws, self.table)

    def tearDown(self):
        self.engine.shutdown()
        os.environ.pop("TOOLAHEAD_TRUST_FILE", None)
        self.tmp.cleanup()

    def _edit(self, path="app/dash/page.tsx"):
        self.engine.handle_agent_event(
            {"hook_event_name": "PostToolUse", "tool_name": "Edit",
             "tool_input": {"file_path": path}, "session_id": "s"})

    def _bash(self, command):
        self.engine.handle_agent_event(
            {"hook_event_name": "PostToolUse", "tool_name": "Bash",
             "tool_input": {"command": command},
             "tool_response": {"exit_code": 0}, "session_id": "s"})

    def test_mentioning_a_url_does_not_learn_it(self):
        self._edit()
        self._bash("echo http://127.0.0.1:3000/logout")
        self.assertEqual(self.engine.learned_routes_for("app/dash/page.tsx"), [],
                         "eine blosse Erwaehnung wird zum spaeteren GET")

    def test_commented_url_in_script_is_not_learned(self):
        _write(os.path.join(self.ws, "notes.sh"),
               "# see http://127.0.0.1:3000/admin/wipe for details\necho ok\n")
        self._edit()
        self._bash("cat notes.sh")
        self.assertEqual(self.engine.learned_routes_for("app/dash/page.tsx"), [])

    def test_actual_fetch_is_learned(self):
        self._edit()
        self._bash("curl -sf http://127.0.0.1:3000/dashboard")
        self.assertEqual(self.engine.learned_routes_for("app/dash/page.tsx"),
                         ["/dashboard"])

    def test_fetch_inside_executed_script_is_learned(self):
        _write(os.path.join(self.ws, "e2e.sh"),
               "curl -sf http://127.0.0.1:3000/checkout\n")
        self._edit()
        self._bash("sh e2e.sh")
        self.assertEqual(self.engine.learned_routes_for("app/dash/page.tsx"),
                         ["/checkout"])

    def test_foreign_origin_is_ignored(self):
        self._edit()
        self._bash("curl -sf https://example.com/tracker")
        self.assertEqual(self.engine.learned_routes_for("app/dash/page.tsx"), [])

    def test_learned_routes_are_never_persisted(self):
        self._edit()
        self._bash("curl -sf http://127.0.0.1:3000/dashboard")
        self.assertTrue(self.engine.learned_routes_for("app/dash/page.tsx"))
        self.engine.save()
        with open(self.table, encoding="utf-8") as handle:
            self.assertNotIn("route:", handle.read())

    def test_repo_supplied_table_cannot_seed_routes(self):
        """Ein geklontes Repo darf keine Auto-GETs mitliefern."""
        self.engine.shutdown()
        json.dump({"counts": [["editfile:README.md",
                               "route:/admin/wipe?confirm=1", 99]],
                   "wrong": [], "examples": []}, open(self.table, "w"))
        from toolahead.proxy import PrefetchEngine
        self.engine = PrefetchEngine(self.ws, self.table)
        self.assertEqual(self.engine.learned_routes_for("README.md"), [])


class DeriveRoutesTest(unittest.TestCase):
    def test_absolute_path_inside_workspace_resolves(self):
        with tempfile.TemporaryDirectory() as ws:
            absolute = os.path.join(ws, "app", "dashboard", "page.tsx")
            self.assertEqual(derive_routes(absolute, ws), ["/dashboard"])

    def test_path_outside_workspace_yields_nothing(self):
        with tempfile.TemporaryDirectory() as ws:
            self.assertEqual(derive_routes("../../app/admin/page.tsx", ws), [])
            self.assertEqual(derive_routes("/elsewhere/app/admin/page.tsx", ws),
                             [])

    def test_traversal_without_workspace_is_rejected(self):
        self.assertEqual(derive_routes("../../app/admin/page.tsx"), [])
        self.assertEqual(derive_routes("/abs/app/admin/page.tsx"), [])

    def test_relative_paths_still_work(self):
        self.assertEqual(derive_routes("app/dashboard/page.tsx"), ["/dashboard"])


class _CountingHandler(BaseHTTPRequestHandler):
    lock = threading.Lock()
    active = 0
    peak = 0
    seen: list = []

    def do_GET(self):  # noqa: N802
        # Nur echte Warm-Requests zaehlen. "/" ist der Readiness-Probe-Pfad
        # und darf naturgemaess parallel aus mehreren Threads kommen.
        counted = self.path != "/"
        if counted:
            with _CountingHandler.lock:
                _CountingHandler.active += 1
                _CountingHandler.peak = max(_CountingHandler.peak,
                                            _CountingHandler.active)
                _CountingHandler.seen.append(self.path)
            time.sleep(0.4)
            with _CountingHandler.lock:
                _CountingHandler.active -= 1
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class WarmSerializationTest(unittest.TestCase):
    """Ein laufender Warm-GET laesst sich nicht abbrechen — also darf nie ein
    zweiter parallel laufen, sonst ueberholen sich Seiteneffekte."""

    def setUp(self):
        _CountingHandler.active = 0
        _CountingHandler.peak = 0
        _CountingHandler.seen = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CountingHandler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = self.tmp.name
        os.environ["TOOLAHEAD_TRUST_FILE"] = os.path.join(self.ws, "trust.json")
        _write(os.path.join(self.ws, "toolahead.toml"), f"""
[services.dev]
command = "sleep 60"
ready.http = "http://127.0.0.1:{self.port}/"
prewarm = "manual"
warm_routes = ["/a", "/b"]
""")
        trust_workspace(self.ws)
        self.manager = ServiceManager.load(self.ws)

    def tearDown(self):
        self.manager.stop_all()
        self.httpd.shutdown()
        os.environ.pop("TOOLAHEAD_TRUST_FILE", None)
        self.tmp.cleanup()

    def test_overlapping_mutations_never_warm_concurrently(self):
        self.manager.ensure(["dev"], wait=10)
        for _ in range(3):
            self.manager.warm_after_mutation("app/page.tsx")
            time.sleep(0.05)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with _CountingHandler.lock:
                idle = _CountingHandler.active == 0 and _CountingHandler.seen
            if idle:
                break
            time.sleep(0.1)
        self.assertLessEqual(_CountingHandler.peak, 1,
                             "zwei Warm-GETs liefen gleichzeitig")


class EnsureWorkspaceTest(unittest.TestCase):
    """Ein Hook darf nie den Daemon eines fremden Workspace befragen."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = self.tmp.name
        os.environ["TOOLAHEAD_TRUST_FILE"] = os.path.join(self.ws, "trust.json")
        _write(os.path.join(self.ws, "toolahead.toml"), """
[services.dev]
command = "sleep 60"
prewarm = "manual"
timeout = 3

[commands.e2e]
match = "sh e2e.sh"
requires = ["dev"]
""")
        trust_workspace(self.ws)
        from toolahead.proxy import build
        self.httpd, _proxy, self.engine = build(
            port=0, workspace=self.ws,
            table=os.path.join(self.ws, ".prefetch-table.json"))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.engine.shutdown()
        self.httpd.shutdown()
        os.environ.pop("TOOLAHEAD_TRUST_FILE", None)
        self.tmp.cleanup()

    def _ensure(self, payload: dict) -> dict:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/__prefetch/ensure-services",
            method="POST", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())

    def test_foreign_workspace_is_refused(self):
        result = self._ensure({"command": "sh e2e.sh",
                               "workspace": "/definitely/not/this/project",
                               "wait": 2})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "workspace mismatch")
        self.assertEqual(self.engine.services.status()["dev"]["state"],
                         "stopped", "fremder Workspace hat Services gestartet")

    def test_matching_workspace_is_served(self):
        result = self._ensure({"command": "sh e2e.sh", "workspace": self.ws,
                               "wait": 2})
        self.assertTrue(result["ok"])
        self.assertIn("dev", result["services"])

    def test_client_budget_bounds_the_wait(self):
        started = time.monotonic()
        self._ensure({"command": "sh e2e.sh", "workspace": self.ws, "wait": 1})
        self.assertLess(time.monotonic() - started, 3.0,
                        "Server wartete laenger als das Client-Budget")


if __name__ == "__main__":
    unittest.main()
