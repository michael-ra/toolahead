"""Tests fuer Pre-Warming (services.py) und die Engine-Integration."""

import os
import socket
import sys
import tempfile
import time
import unittest

os.environ.setdefault("PREFETCH_QUIET", "1")

from toolahead.services import (  # noqa: E402
    CommandRule,
    ReadyCheck,
    ServiceConfigError,
    ServiceManager,
    is_trusted,
    trust_workspace,
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


class _WorkspaceCase(unittest.TestCase):
    """Isoliert Workspace UND Trust-Store pro Test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = self.tmp.name
        self._old_trust = os.environ.get("TOOLAHEAD_TRUST_FILE")
        os.environ["TOOLAHEAD_TRUST_FILE"] = os.path.join(
            self.workspace, ".trust-store.json")

    def tearDown(self):
        if self._old_trust is None:
            os.environ.pop("TOOLAHEAD_TRUST_FILE", None)
        else:
            os.environ["TOOLAHEAD_TRUST_FILE"] = self._old_trust
        self.tmp.cleanup()


class ReadyCheckTest(unittest.TestCase):
    def test_parse_requires_exactly_one_kind(self):
        with self.assertRaises(ServiceConfigError):
            ReadyCheck.parse({"port": 3000, "http": "http://localhost:3000"})
        with self.assertRaises(ServiceConfigError):
            ReadyCheck.parse({})
        with self.assertRaises(ServiceConfigError):
            ReadyCheck.parse({"port": 0})
        with self.assertRaises(ServiceConfigError):
            ReadyCheck.parse({"command": "   "})
        self.assertIsNone(ReadyCheck.parse(None))
        self.assertEqual(ReadyCheck.parse({"port": 8080}).kind, "port")

    def test_port_probe(self):
        port = _free_port()
        check = ReadyCheck("port", port)
        self.assertFalse(check.probe("."))
        with socket.socket() as server:
            server.bind(("127.0.0.1", port))
            server.listen(1)
            self.assertTrue(check.probe("."))

    def test_command_probe(self):
        self.assertTrue(ReadyCheck("command", "true").probe("."))
        self.assertFalse(ReadyCheck("command", "false").probe("."))


class CommandRuleTest(unittest.TestCase):
    def test_prefix_matching_respects_word_boundaries(self):
        rule = CommandRule("e2e", "npm test", ("dev",))
        self.assertTrue(rule.matches("npm test"))
        self.assertTrue(rule.matches("  npm   test --watch=false"))
        self.assertFalse(rule.matches("npm testx"))
        self.assertFalse(rule.matches("yarn test"))
        self.assertFalse(rule.matches(""))


class ConfigLoadTest(_WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.warnings: list[str] = []

    def _load(self, trust: bool = True) -> ServiceManager:
        if trust:
            trust_workspace(self.workspace)
        return ServiceManager.load(
            self.workspace,
            on_event=lambda kind, msg: self.warnings.append(f"{kind}:{msg}"))

    def test_missing_file_is_noop(self):
        manager = self._load(trust=False)
        self.assertFalse(manager.enabled)
        self.assertEqual(manager.requirements_for("npm test"), [])

    def test_valid_config(self):
        _write(os.path.join(self.workspace, "toolahead.toml"), """
[services.dev]
command = "sleep 60"
ready.port = 3000
timeout = 5
prewarm = "manual"

[commands.e2e]
match = "npx playwright test"
requires = ["dev"]
""")
        manager = self._load()
        self.assertTrue(manager.enabled)
        self.assertTrue(manager.trusted)
        self.assertEqual(list(manager.specs), ["dev"])
        self.assertEqual(manager.specs["dev"].prewarm, "manual")
        self.assertEqual(
            manager.requirements_for("npx playwright test --headed"), ["dev"])
        self.assertTrue(manager.external("npx playwright test"))
        self.assertFalse(manager.external("pytest"))

    def test_broken_entries_are_skipped_not_fatal(self):
        _write(os.path.join(self.workspace, "toolahead.toml"), """
[services.ok]
command = "sleep 60"
prewarm = "manual"

[services.broken]
command = ""

[services.escape]
command = "sleep 60"
cwd = "../outside"

[services.forever]
command = "sleep 60"
timeout = nan

[commands.dangling]
match = "npm test"
requires = ["missing"]
""")
        manager = self._load()
        self.assertEqual(list(manager.specs), ["ok"])
        self.assertEqual(manager.rules, [])
        warned = [entry for entry in self.warnings if entry.startswith("warn:")]
        self.assertEqual(len(warned), 4)

    def test_invalid_toml_disables_quietly(self):
        _write(os.path.join(self.workspace, "toolahead.toml"), "not [valid")
        manager = self._load(trust=False)
        self.assertFalse(manager.enabled)
        warned = [entry for entry in self.warnings if entry.startswith("warn:")]
        self.assertEqual(len(warned), 1)


class TrustTest(_WorkspaceCase):
    TOML = """
[services.dev]
command = "sleep 60"
prewarm = "manual"
timeout = 5
"""

    def test_untrusted_config_never_starts_processes(self):
        _write(os.path.join(self.workspace, "toolahead.toml"), self.TOML)
        manager = ServiceManager.load(self.workspace)
        self.assertFalse(manager.trusted)
        self.assertEqual(manager.ensure(["dev"], wait=1.0),
                         {"dev": "untrusted"})
        manager.ensure_async(["dev"])
        time.sleep(0.3)
        self.assertEqual(manager.status()["dev"]["state"], "untrusted")
        self.assertEqual(manager._procs, {})

    def test_trust_enables_and_config_change_revokes(self):
        _write(os.path.join(self.workspace, "toolahead.toml"), self.TOML)
        trust_workspace(self.workspace)
        self.assertTrue(is_trusted(self.workspace))
        manager = ServiceManager.load(self.workspace)
        self.assertTrue(manager.trusted)
        try:
            self.assertEqual(manager.ensure(["dev"], wait=5.0),
                             {"dev": "ready"})
        finally:
            manager.stop_all()
        _write(os.path.join(self.workspace, "toolahead.toml"),
               self.TOML + "\n# geaendert\n")
        self.assertFalse(is_trusted(self.workspace))
        self.assertFalse(ServiceManager.load(self.workspace).trusted)


class LifecycleTest(_WorkspaceCase):
    def _manager(self, toml: str) -> ServiceManager:
        _write(os.path.join(self.workspace, "toolahead.toml"), toml)
        trust_workspace(self.workspace)
        return ServiceManager.load(self.workspace)

    def test_ensure_starts_waits_and_stop_kills(self):
        port = _free_port()
        manager = self._manager(f"""
[services.listener]
command = "{sys.executable} -c 'import socket,time; s=socket.socket(); s.bind((\\"127.0.0.1\\", {port})); s.listen(50); time.sleep(60)'"
ready.port = {port}
timeout = 10
prewarm = "manual"
""")
        try:
            result = manager.ensure(["listener"], report=True)
            self.assertEqual(result["states"], {"listener": "ready"})
            self.assertEqual(result["started_now"], ["listener"])
            self.assertEqual(manager.status()["listener"]["state"], "ready")
            second = manager.ensure(["listener"], report=True)
            self.assertEqual(second["states"], {"listener": "ready"})
            self.assertEqual(second["started_now"], [])
            log_path = os.path.join(self.workspace, ".toolahead", "services",
                                    "listener.log")
            self.assertTrue(os.path.exists(log_path))
        finally:
            manager.stop_all()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if ReadyCheck("port", port).probe(self.workspace) is False:
                break
            time.sleep(0.1)
        self.assertFalse(ReadyCheck("port", port).probe(self.workspace))

    def test_stop_all_closes_manager_for_good(self):
        manager = self._manager("""
[services.dev]
command = "sleep 60"
prewarm = "manual"
timeout = 5
""")
        manager.stop_all()
        self.assertEqual(manager.ensure(["dev"], wait=1.0), {})
        manager.ensure_async(["dev"])
        time.sleep(0.3)
        self.assertEqual(manager._procs, {})

    def test_crashing_service_is_disabled_after_three_failures(self):
        manager = self._manager("""
[services.crash]
command = "false"
prewarm = "manual"
timeout = 2
""")
        try:
            for _ in range(6):
                states = manager.ensure(["crash"], wait=2.0)
                if states.get("crash") == "disabled":
                    break
                time.sleep(0.1)
            self.assertEqual(manager.ensure(["crash"], wait=1.0),
                             {"crash": "disabled"})
            self.assertEqual(manager.status()["crash"]["state"], "disabled")
        finally:
            manager.stop_all()

    def test_unknown_names_are_ignored(self):
        manager = ServiceManager(self.workspace)
        manager.trusted = True
        self.assertEqual(manager.ensure(["nope"]), {})
        manager.ensure_async([])


class EngineIntegrationTest(_WorkspaceCase):
    def setUp(self):
        super().setUp()
        _write(os.path.join(self.workspace, "toolahead.toml"), """
[services.dev]
command = "sleep 60"
prewarm = "manual"
timeout = 5

[commands.integration]
match = "npm test"
requires = ["dev"]
""")
        trust_workspace(self.workspace)
        from toolahead.proxy import PrefetchEngine
        self.engine = PrefetchEngine(
            self.workspace, os.path.join(self.workspace, ".prefetch-table.json"))

    def tearDown(self):
        self.engine.shutdown()
        super().tearDown()

    def test_external_command_is_diverted_not_speculated(self):
        scheduled = self.engine.schedule(
            "bash", {"command": "npm test"}, reason="test", confidence=1.0)
        self.assertFalse(scheduled)
        self.assertEqual(self.engine.stats["external_diverted"], 1)
        self.assertNotIn("bash:npm test", self.engine.inflight)
        deadline = time.monotonic() + 5
        state = None
        while time.monotonic() < deadline:
            state = self.engine.services.status()["dev"]["state"]
            if state == "ready":
                break
            time.sleep(0.1)
        self.assertEqual(state, "ready")

    def test_lookup_never_serves_external_commands(self):
        result = self.engine.lookup("bash", {"command": "npm test"})
        self.assertFalse(result["hit"])
        self.assertEqual(result["reason"], "external-state command")

    def test_snapshot_reports_services(self):
        snapshot = self.engine.snapshot()
        self.assertIn("dev", snapshot["services"])
        self.assertEqual(snapshot["services"]["dev"]["prewarm"], "manual")
        self.assertTrue(snapshot["config"]["services_trusted"])


if __name__ == "__main__":
    unittest.main()
