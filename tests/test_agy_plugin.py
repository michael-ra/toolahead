"""Die Antigravity-CLI liest Hooks nur aus Plugins.

Beobachtet an CLI 1.1.11: ``.agents/hooks.json`` wird von der CLI nie gelesen
(``loaded 0 named hooks from 0 hooks.json file(s)``), erst ein installiertes
Plugin mit ``plugin.json`` und ``hooks.json`` im Wurzelverzeichnis feuert.
Diese Tests halten das Format fest — ein falscher Pfad macht die gesamte
Integration wirkungslos, ohne dass irgendetwas fehlschlaegt.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("PREFETCH_QUIET", "1")

from toolahead import cli  # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src")


def _cli(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "toolahead.cli", *args],
                          capture_output=True, text=True,
                          env=dict(os.environ, PYTHONPATH=SRC,
                                   PATH="/nonexistent"))


class AgyPluginTest(unittest.TestCase):
    """Erzeugt wird in HOME; deshalb bekommt jeder Test ein eigenes HOME."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home, exist_ok=True)
        self.project = os.path.join(self.tmp.name, "project")
        os.makedirs(self.project, exist_ok=True)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self.tmp.cleanup()

    def _install(self):
        return _cli("init-antigravity", "--project", self.project)

    def test_plugin_is_written_with_the_layout_agy_expects(self):
        result = self._install()
        self.assertEqual(result.returncode, 0, result.stderr)
        plugin = Path(self.home) / ".toolahead" / "agy-plugin"
        # plugin.json MUSS im Wurzelverzeichnis liegen, nicht unter
        # .claude-plugin/ — agy meldet sonst "missing plugin.json".
        self.assertTrue((plugin / "plugin.json").is_file())
        # hooks.json ebenfalls im Wurzelverzeichnis; ein hooks/-Unterordner
        # wird von agy als "skipped (not found)" ignoriert.
        self.assertTrue((plugin / "hooks.json").is_file())
        self.assertTrue((plugin / "antigravity_hook.py").is_file(),
                        "Hook muss mitkopiert werden: unter uvx ist der "
                        "Paketpfad fluechtig")

    def test_plugin_hooks_cover_the_native_tools(self):
        self._install()
        plugin = Path(self.home) / ".toolahead" / "agy-plugin"
        hooks = json.loads((plugin / "hooks.json").read_text())["hooks"]
        self.assertEqual(sorted(hooks), ["PostToolUse", "PreToolUse"])
        for event, groups in hooks.items():
            matcher = groups[0]["matcher"]
            for tool in ("run_command", "view_file", "replace_file_content"):
                self.assertIn(tool, matcher, f"{tool} fehlt in {event}")
            command = groups[0]["hooks"][0]["command"]
            self.assertIn("antigravity_hook.py", command)
            self.assertIn(event, command,
                          "Antigravity liefert den Event-Namen nur als argv")
            self.assertIn(str(plugin), command,
                          "Plugin muss auf die eigene Kopie zeigen")

    def test_plugin_carries_no_project_url(self):
        """Plugins sind global; eine Projekt-URL waere fuer jedes andere
        Projekt falsch. Die Zuordnung macht der Workspace-Check des Daemons."""
        self._install()
        plugin = Path(self.home) / ".toolahead" / "agy-plugin"
        hooks = json.loads((plugin / "hooks.json").read_text())["hooks"]
        for groups in hooks.values():
            self.assertNotIn("--url", groups[0]["hooks"][0]["command"])

    def test_pretooluse_timeout_covers_the_readiness_wait(self):
        self._install()
        plugin = Path(self.home) / ".toolahead" / "agy-plugin"
        hooks = json.loads((plugin / "hooks.json").read_text())["hooks"]
        self.assertGreaterEqual(
            hooks["PreToolUse"][0]["hooks"][0]["timeout"], 110)

    def test_ide_files_are_still_written(self):
        self._install()
        self.assertTrue(os.path.isfile(
            os.path.join(self.project, ".agents", "hooks.json")))

    def test_missing_agy_binary_is_not_an_error(self):
        # PATH ist in _cli leer: ohne agy darf init trotzdem gelingen und den
        # manuellen Installationsbefehl nennen.
        result = self._install()
        self.assertEqual(result.returncode, 0)
        self.assertIn("agy plugin install", result.stdout)


if __name__ == "__main__":
    unittest.main()
