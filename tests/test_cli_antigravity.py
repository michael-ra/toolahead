"""Antigravity-Init: workspace-lokale MCP-Config schreiben und mergen."""

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path

from toolahead.cli import init_all, init_antigravity


def _args(project: str, dry_run: bool = False, agent: str = "antigravity"):
    return argparse.Namespace(project=project, url="http://127.0.0.1:4242",
                              dry_run=dry_run, strict=False, agent=agent)


class InitAntigravityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _config(self) -> dict:
        path = self.project / ".agents" / "mcp_config.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_writes_workspace_config_and_runtime(self):
        self.assertEqual(init_antigravity(_args(str(self.project))), 0)
        config = self._config()
        server = config["mcpServers"]["toolahead"]
        self.assertEqual(server["command"], "python3")
        self.assertIn("--workspace", server["args"])
        self.assertEqual(server["env"], {"TOOLAHEAD_MCP_EVENTS": "1"})
        runtime = self.project / ".toolahead" / "runtime"
        for name in ("toolahead_mcp.py", "tool_contracts.py", "services.py",
                     "antigravity_hook.py"):
            self.assertTrue((runtime / name).exists(), name)
        self.assertTrue((self.project / ".prefetch-replay.json").exists())

    def test_writes_hooks_config_and_merges(self):
        hooks_path = self.project / ".agents" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        hooks_path.write_text(json.dumps(
            {"my-linter": {"PostToolUse": [{"matcher": "run_command",
                                           "hooks": []}]}}),
            encoding="utf-8")
        self.assertEqual(init_antigravity(_args(str(self.project))), 0)
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        self.assertIn("my-linter", hooks)
        ours = hooks["toolahead"]
        for event in ("PreToolUse", "PostToolUse", "Stop"):
            self.assertIn(event, ours)
        pre = ours["PreToolUse"][0]
        self.assertIn("replace_file_content", pre["matcher"])
        self.assertIn("antigravity_hook.py", pre["hooks"][0]["command"])
        self.assertIn("PreToolUse", pre["hooks"][0]["command"])

    def test_merges_existing_servers(self):
        path = self.project / ".agents" / "mcp_config.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(
            {"mcpServers": {"other": {"command": "node", "args": ["x.js"]}}}),
            encoding="utf-8")
        self.assertEqual(init_antigravity(_args(str(self.project))), 0)
        config = self._config()
        self.assertIn("other", config["mcpServers"])
        self.assertIn("toolahead", config["mcpServers"])

    def test_dry_run_writes_nothing(self):
        self.assertEqual(init_antigravity(_args(str(self.project),
                                                dry_run=True)), 0)
        self.assertFalse((self.project / ".agents").exists())
        self.assertFalse((self.project / ".toolahead").exists())

    def test_invalid_existing_config_fails_cleanly(self):
        path = self.project / ".agents" / "mcp_config.json"
        path.parent.mkdir(parents=True)
        path.write_text("[]", encoding="utf-8")
        self.assertEqual(init_antigravity(_args(str(self.project))), 2)

    def test_init_all_agent_all_includes_antigravity(self):
        self.assertEqual(init_all(_args(str(self.project), agent="all")), 0)
        self.assertTrue(
            (self.project / ".agents" / "mcp_config.json").exists())
        self.assertTrue((self.project / ".claude" / "settings.json").exists())
        self.assertTrue((self.project / ".codex" / "config.toml").exists())


if __name__ == "__main__":
    unittest.main()
