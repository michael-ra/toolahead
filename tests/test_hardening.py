"""Regressionstests der zweiten Fix-Runde (adversariale Nachpruefung 0.6.1).

Diese Faelle stammen aus einem Angriff auf die erste Fix-Runde. Mehrere davon
waren Regressionen, die erst durch das Beheben anderer Fehler entstanden sind —
deshalb steht hier jeweils die Nutzerwirkung im Test, nicht die Implementierung.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request

os.environ.setdefault("PREFETCH_QUIET", "1")

from toolahead import cli  # noqa: E402
from toolahead.services import derive_routes, trust_workspace  # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src")


def _cli(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "toolahead.cli", *args],
                          capture_output=True, text=True,
                          env=dict(os.environ, PYTHONPATH=SRC))


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


class UserPermissionsTest(unittest.TestCase):
    """Ein Install darf niemals Sicherheitsregeln loeschen, die der Nutzer
    selbst gesetzt hat — auch dann nicht, wenn sie zufaellig genauso heissen
    wie die, die ToolAhead im Strict-Modus setzen wuerde."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _deny(self) -> list:
        with open(os.path.join(self.ws, ".claude", "settings.json")) as handle:
            return json.load(handle).get("permissions", {}).get("deny", [])

    def test_user_authored_denies_survive_first_init(self):
        _write(os.path.join(self.ws, ".claude", "settings.json"),
               json.dumps({"permissions": {"deny": ["Edit", "Write",
                                                    "Bash(rm:*)"],
                                           "defaultMode": "acceptEdits"}}))
        _cli("init", "--agent", "both", "--project", self.ws)
        self.assertEqual(sorted(self._deny()),
                         ["Bash(rm:*)", "Edit", "Write"],
                         "Install hat eigene Deny-Regeln des Nutzers geloescht")
        with open(os.path.join(self.ws, ".claude", "settings.json")) as handle:
            self.assertEqual(
                json.load(handle)["permissions"]["defaultMode"], "acceptEdits")

    def test_permissions_block_is_never_dropped(self):
        _write(os.path.join(self.ws, ".claude", "settings.json"),
               json.dumps({"permissions": {"deny": ["Read", "Write"]}}))
        _cli("init", "--agent", "claude", "--project", self.ws)
        with open(os.path.join(self.ws, ".claude", "settings.json")) as handle:
            self.assertIn("permissions", json.load(handle))

    def test_only_toolahead_added_denies_are_rolled_back(self):
        # Nutzer sperrt Write selbst; --strict ergaenzt die uebrigen vier.
        _write(os.path.join(self.ws, ".claude", "settings.json"),
               json.dumps({"permissions": {"deny": ["Write"]}}))
        _cli("init", "--agent", "claude", "--strict", "--project", self.ws)
        self.assertEqual(sorted(self._deny()),
                         sorted(cli.CLAUDE_NATIVE_ANALOGS))
        _cli("init", "--agent", "claude", "--project", self.ws)
        self.assertEqual(self._deny(), ["Write"],
                         "Rollback muss genau die eigenen Eintraege behalten")


class ManagedTomlTest(unittest.TestCase):
    def test_missing_end_marker_does_not_truncate(self):
        block = f"{cli.MCP_BEGIN}\n[mcp_servers.toolahead]\ncommand = \"x\"\n"
        existing = block + '\n[mcp_servers.company]\ncommand = "y"\n'
        with self.assertRaises(ValueError):
            cli._remove_managed_toml(existing)

    def test_removes_every_managed_block(self):
        block = f"{cli.MCP_BEGIN}\n[mcp_servers.toolahead]\n{cli.MCP_END}\n"
        existing = block + '[history]\npersistence = "none"\n' + block
        result = cli._remove_managed_toml(existing)
        self.assertNotIn("mcp_servers.toolahead", result)
        self.assertIn("[history]", result)

    def test_untouched_config_survives(self):
        existing = '[history]\npersistence = "none"\n'
        self.assertIn("[history]", cli._remove_managed_toml(existing))


class TableLocationTest(unittest.TestCase):
    """Die gelernte Tabelle steuert, was spekulativ ausgefuehrt wird. Sie darf
    deshalb nicht aus dem Repository stammen koennen."""

    def test_default_table_lives_outside_the_workspace(self):
        from toolahead.proxy import default_table_path
        with tempfile.TemporaryDirectory() as ws:
            path = os.path.realpath(default_table_path(ws))
            self.assertFalse(path.startswith(os.path.realpath(ws)),
                             "Lerntabelle liegt im Projekt")
            self.assertTrue(path.startswith(
                os.path.realpath(os.path.expanduser("~"))))

    def test_distinct_workspaces_get_distinct_tables(self):
        from toolahead.proxy import default_table_path
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            self.assertNotEqual(default_table_path(a), default_table_path(b))

    def test_repo_supplied_table_is_not_loaded_by_default(self):
        from toolahead.proxy import build
        with tempfile.TemporaryDirectory() as ws:
            _write(os.path.join(ws, ".prefetch-table.json"),
                   json.dumps({"counts": [["$START", "bash:test:pytest", 99]],
                               "wrong": [],
                               "examples": [["bash:test:pytest",
                                             json.dumps({"tool": "bash",
                                                         "args": {"command":
                                                                  "pytest"}}),
                                             99]]}))
            httpd, _proxy, engine = build(port=0, workspace=ws)
            try:
                self.assertEqual(engine.table.counts, {},
                                 "Repo-Datei wurde als Lernstand uebernommen")
            finally:
                engine.shutdown()
                httpd.server_close()


class EndpointWorkspaceTest(unittest.TestCase):
    """Nicht nur ensure-services: auch Lernen und Ausliefern gehoeren an den
    eigenen Workspace gebunden."""

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
            table=os.path.join(self.ws, ".pt.json"))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.engine.shutdown()
        self.httpd.shutdown()
        os.environ.pop("TOOLAHEAD_TRUST_FILE", None)
        self.tmp.cleanup()

    def _post(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method="POST",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())

    def test_agent_event_from_foreign_workspace_is_refused(self):
        result = self._post("/__prefetch/agent-event", {
            "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": "sh e2e.sh"},
            "workspace": "/definitely/not/this/project"})
        self.assertFalse(result["ok"])
        self.assertEqual(self.engine.services.status()["dev"]["state"],
                         "stopped", "fremdes Event hat Services gestartet")

    def test_lookup_from_foreign_workspace_is_refused(self):
        result = self._post("/__prefetch/lookup", {
            "tool": "Read", "input": {"file_path": "toolahead.toml"},
            "workspace": "/definitely/not/this/project"})
        self.assertFalse(result["hit"])
        self.assertEqual(result.get("reason"), "workspace mismatch")

    def test_subdirectory_counts_as_the_same_workspace(self):
        sub = os.path.join(self.ws, "packages", "app")
        os.makedirs(sub, exist_ok=True)
        result = self._post("/__prefetch/agent-event", {
            "hook_event_name": "UserPromptSubmit", "workspace": sub})
        self.assertTrue(result["ok"],
                        "Unterverzeichnis faelschlich als fremd abgewiesen")


class LearningRobustnessTest(unittest.TestCase):
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
        _write(os.path.join(self.ws, "app", "dash", "page.tsx"), "x")
        from toolahead.proxy import PrefetchEngine
        self.engine = PrefetchEngine(self.ws, os.path.join(self.ws, "t.json"))

    def tearDown(self):
        self.engine.shutdown()
        os.environ.pop("TOOLAHEAD_TRUST_FILE", None)
        self.tmp.cleanup()

    def _edit(self):
        self.engine.handle_agent_event(
            {"hook_event_name": "PostToolUse", "tool_name": "Edit",
             "tool_input": {"file_path": "app/dash/page.tsx"},
             "session_id": "s"})

    def _bash(self, command, exit_code=0):
        self.engine.handle_agent_event(
            {"hook_event_name": "PostToolUse", "tool_name": "Bash",
             "tool_input": {"command": command},
             "tool_response": {"exit_code": exit_code}, "session_id": "s"})

    def _routes(self):
        return self.engine.learned_routes_for("app/dash/page.tsx")

    def test_second_line_mention_is_not_learned(self):
        self._edit()
        self._bash("curl -s http://127.0.0.1:3000/health\n"
                   "grep -r http://127.0.0.1:3000/admin/wipe .")
        self.assertEqual(self._routes(), ["/health"])

    def test_quoted_separator_cannot_forge_a_fetch(self):
        self._edit()
        self._bash("echo 'x; curl http://127.0.0.1:3000/admin/wipe'")
        self.assertEqual(self._routes(), [])

    def test_failed_fetch_is_not_learned(self):
        self._edit()
        self._bash("curl -sf http://127.0.0.1:3000/admin/wipe", exit_code=7)
        self.assertEqual(self._routes(), [])

    def test_second_line_fetch_is_learned(self):
        self._edit()
        self._bash("cd app\ncurl -s http://127.0.0.1:3000/api/items")
        self.assertEqual(self._routes(), ["/api/items"])

    def test_scripts_are_found_in_compound_commands(self):
        _write(os.path.join(self.ws, "e2e.sh"),
               "curl -sf http://127.0.0.1:3000/api/e2e\n")
        for command in ("cd . && ./e2e.sh", "npm run build && ./e2e.sh",
                        "time ./e2e.sh", ". ./e2e.sh", "sh e2e.sh"):
            with self.subTest(command=command):
                self.engine.route_memory.clear()
                self._edit()
                self._bash(command)
                self.assertEqual(self._routes(), ["/api/e2e"])

    def test_commented_line_in_script_is_ignored(self):
        _write(os.path.join(self.ws, "notes.sh"),
               "# curl http://127.0.0.1:3000/admin/wipe\necho ok\n")
        self._edit()
        self._bash("sh notes.sh")
        self.assertEqual(self._routes(), [])


class MonorepoRouteTest(unittest.TestCase):
    def test_service_cwd_defines_the_route_root(self):
        with tempfile.TemporaryDirectory() as ws:
            os.environ["TOOLAHEAD_TRUST_FILE"] = os.path.join(ws, "trust.json")
            try:
                _write(os.path.join(ws, "toolahead.toml"), """
[services.web]
command = "sleep 60"
cwd = "web"
ready.port = 3000
prewarm = "manual"
warm_routes = ["auto"]
""")
                os.makedirs(os.path.join(ws, "web"), exist_ok=True)
                trust_workspace(ws)
                from toolahead.services import ServiceManager
                manager = ServiceManager.load(ws)
                spec = manager.specs["web"]
                root = os.path.join(ws, spec.cwd)
                self.assertEqual(
                    derive_routes("web/app/dashboard/page.tsx", ws), [],
                    "Vorbedingung: repo-relativ ergibt keine Route")
                self.assertEqual(
                    derive_routes(os.path.join(ws, "web/app/dashboard/page.tsx"),
                                  root),
                    ["/dashboard"])
            finally:
                os.environ.pop("TOOLAHEAD_TRUST_FILE", None)


class CommandRuleMatchTest(unittest.TestCase):
    """Ein deklariertes Skript gilt unabhaengig von der Aufrufschreibweise —
    sonst umgeht der Agent die Readiness-Garantie versehentlich."""

    def setUp(self):
        from toolahead.services import CommandRule
        self.rule = CommandRule("e2e", "sh e2e.sh", ("dev",))

    def test_equivalent_invocations_match(self):
        for command in ("sh e2e.sh", "sh ./e2e.sh", "./e2e.sh", "bash e2e.sh",
                        "bash ./e2e.sh", "time ./e2e.sh", "CI=1 sh e2e.sh",
                        "sh e2e.sh --headed"):
            with self.subTest(command=command):
                self.assertTrue(self.rule.matches(command))

    def test_compound_commands_match(self):
        """Agenten schreiben fast nie das nackte Kommando — ein vorangestelltes
        ``cd`` darf die Readiness-Garantie nicht aushebeln."""
        for command in ("cd /tmp/project && sh e2e.sh", "cd . && ./e2e.sh",
                        "npm run build && sh e2e.sh", "cd web; sh e2e.sh",
                        "bash e2e.sh 2>&1", "cd web\nsh e2e.sh"):
            with self.subTest(command=command):
                self.assertTrue(self.rule.matches(command))

    def test_unrelated_commands_do_not_match(self):
        for command in ("sh other.sh", "./other.sh", "cat e2e.sh",
                        "npm test", "", "echo sh e2e.sh",
                        'echo "sh e2e.sh"', "grep -r e2e.sh ."):
            with self.subTest(command=command):
                self.assertFalse(self.rule.matches(command))


class BareInvocationTest(unittest.TestCase):
    """`uvx toolahead` ist der erste Befehl, den jemand tippt — er darf nicht
    mit einer argparse-Fehlermeldung antworten."""

    def test_no_subcommand_prints_guidance_and_succeeds(self):
        result = _cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("error:", result.stderr)
        self.assertIn("toolahead init", result.stdout)
        self.assertIn("github.com/michael-ra/toolahead", result.stdout)

    def test_unknown_subcommand_still_fails(self):
        result = _cli("definitely-not-a-command")
        self.assertNotEqual(result.returncode, 0)


class HookUrlPrecedenceTest(unittest.TestCase):
    """Ein explizites --url muss die Umgebung schlagen: sonst reserviert der
    Hook beim einen Daemon und der umgeschriebene Befehl holt das Ergebnis
    beim anderen ab."""

    def test_explicit_url_wins_over_environment(self):
        probe = (
            "import sys, importlib.util;"
            "sys.argv=['hook','--url','http://127.0.0.1:2222'];"
            f"spec=importlib.util.spec_from_file_location('h', r'{SRC}/toolahead/prefetch_hook.py');"
            "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
            "print(m.LOOKUP_URL, m._replay_url())"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True,
            env=dict(os.environ,
                     PREFETCH_LOOKUP_URL="http://127.0.0.1:1111/__prefetch/lookup"))
        lookup, replay = out.stdout.split()
        self.assertIn("2222", lookup)
        self.assertIn("2222", replay)

    def test_environment_still_works_without_the_flag(self):
        probe = (
            "import sys, importlib.util;"
            "sys.argv=['hook'];"
            f"spec=importlib.util.spec_from_file_location('h', r'{SRC}/toolahead/prefetch_hook.py');"
            "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
            "print(m.LOOKUP_URL)"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True,
            env=dict(os.environ,
                     PREFETCH_LOOKUP_URL="http://127.0.0.1:1111/__prefetch/lookup"))
        self.assertIn("1111", out.stdout)


class HookBudgetTest(unittest.TestCase):
    def test_small_budget_is_not_collapsed_to_one_second(self):
        """Ein knapp gesetztes TOOLAHEAD_ENSURE_WAIT darf nicht in ein
        Ein-Sekunden-Budget und damit in ein falsches Deny umschlagen."""
        source = open(os.path.join(SRC, "toolahead", "prefetch_hook.py"),
                      encoding="utf-8").read()
        self.assertNotIn("max(1.0, wait - 5.0)", source)
        self.assertIn('"wait": wait', source)


if __name__ == "__main__":
    unittest.main()
