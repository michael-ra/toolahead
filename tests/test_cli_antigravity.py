"""Antigravity-Init: workspace-lokale MCP-Config schreiben und mergen."""

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path

from toolahead.cli import init_all, init_antigravity


def _args(project: str, dry_run: bool = False, agent: str = "antigravity",
          replay_tools: bool = False):
    return argparse.Namespace(project=project, url="http://127.0.0.1:4242",
                              dry_run=dry_run, strict=False, agent=agent,
                              replay_tools=replay_tools)


class InitAntigravityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _config(self) -> dict:
        path = self.project / ".agents" / "mcp_config.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_default_is_hooks_only(self):
        self.assertEqual(init_antigravity(_args(str(self.project))), 0)
        hooks = json.loads(
            (self.project / ".agents" / "hooks.json").read_text())
        self.assertIn("toolahead", hooks)
        self.assertFalse(
            (self.project / ".agents" / "mcp_config.json").exists())
        runtime = self.project / ".toolahead" / "runtime"
        for name in ("toolahead_mcp.py", "tool_contracts.py", "services.py",
                     "antigravity_hook.py"):
            self.assertTrue((runtime / name).exists(), name)
        self.assertTrue((self.project / ".prefetch-replay.json").exists())

    def test_replay_tools_registers_mcp_server(self):
        self.assertEqual(
            init_antigravity(_args(str(self.project), replay_tools=True)), 0)
        config = self._config()
        server = config["mcpServers"]["toolahead"]
        self.assertEqual(server["command"], "python3")
        self.assertIn("--workspace", server["args"])
        self.assertEqual(server["env"], {"TOOLAHEAD_MCP_EVENTS": "1"})

    def test_downgrade_removes_mcp_entry(self):
        self.assertEqual(
            init_antigravity(_args(str(self.project), replay_tools=True)), 0)
        self.assertEqual(init_antigravity(_args(str(self.project))), 0)
        config = self._config()
        self.assertNotIn("toolahead", config.get("mcpServers", {}))

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
        self.assertEqual(
            init_antigravity(_args(str(self.project), replay_tools=True)), 0)
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
        self.assertTrue((self.project / ".agents" / "hooks.json").exists())
        self.assertTrue((self.project / ".claude" / "settings.json").exists())
        self.assertTrue((self.project / ".codex" / "hooks.json").exists())
        # hooks-only default: keine MCP-Registrierung ohne --replay-tools
        self.assertFalse(
            (self.project / ".agents" / "mcp_config.json").exists())
        settings = json.loads(
            (self.project / ".claude" / "settings.json").read_text())
        self.assertIn("PostToolUse", settings["hooks"])
        self.assertFalse((self.project / ".mcp.json").exists())

    def test_init_all_replay_tools_registers_everywhere(self):
        self.assertEqual(init_all(_args(str(self.project), agent="all",
                                        replay_tools=True)), 0)
        self.assertTrue(
            (self.project / ".agents" / "mcp_config.json").exists())
        self.assertTrue((self.project / ".mcp.json").exists())
        self.assertIn("[mcp_servers.toolahead]",
                      (self.project / ".codex" / "config.toml").read_text())


if __name__ == "__main__":
    unittest.main()
