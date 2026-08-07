"""Antigravity-Hook-Adapter: Mapping, Fail-open-Output, Engine-Anbindung."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("PREFETCH_QUIET", "1")

from toolahead.antigravity_hook import build_event, map_tool  # noqa: E402

HOOK = Path(__file__).resolve().parents[1] / "src" / "toolahead" / \
    "antigravity_hook.py"


class MapToolTest(unittest.TestCase):
    def test_run_command(self):
        self.assertEqual(map_tool("run_command", {"CommandLine": "npm test"}),
                         ("Bash", {"command": "npm test"}))

    def test_file_tools(self):
        self.assertEqual(map_tool("view_file", {"AbsolutePath": "/w/a.py"}),
                         ("Read", {"file_path": "/w/a.py"}))
        self.assertEqual(
            map_tool("replace_file_content", {"TargetFile": "app/page.js"}),
            ("Edit", {"file_path": "app/page.js"}))
        name, args = map_tool("write_to_file",
                              {"TargetFile": "x.py", "CodeContent": "pass"})
        self.assertEqual((name, args["file_path"]), ("Write", "x.py"))

    def test_unknown_tool_is_ignored(self):
        self.assertIsNone(map_tool("browser_navigate", {"Url": "http://x"}))


class BuildEventTest(unittest.TestCase):
    def test_post_success_and_error(self):
        payload = {"toolCall": {"name": "replace_file_content",
                                "args": {"TargetFile": "app/page.js"}},
                   "stepIdx": 4, "conversationId": "abc123"}
        event = build_event("PostToolUse", payload)
        self.assertEqual(event["tool_name"], "Edit")
        self.assertEqual(event["tool_response"], {"exit_code": 0})
        payload["error"] = "exit status 1"
        failed = build_event("PostToolUse", payload)
        self.assertEqual(failed["error"], "exit status 1")
        self.assertNotIn("tool_response", failed)

    def test_unknown_tool_returns_none(self):
        self.assertIsNone(build_event("PostToolUse", {
            "toolCall": {"name": "generate_image", "args": {}}}))


class FailOpenOutputTest(unittest.TestCase):
    """Ein kaputter Hook kann in Antigravity Tool-Calls blockieren — der
    Adapter muss deshalb bei JEDEM Input valides ``{}`` liefern."""

    def _run(self, stdin: bytes) -> str:
        result = subprocess.run(
            [sys.executable, str(HOOK), "PreToolUse",
             "--url", "http://127.0.0.1:9"],  # Port 9: garantiert kein Daemon
            input=stdin, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0)
        return result.stdout.decode().strip()

    def test_garbage_input(self):
        self.assertEqual(self._run(b"not json at all"), "{}")

    def test_valid_input_daemon_down(self):
        payload = json.dumps({"toolCall": {"name": "run_command",
                                           "args": {"CommandLine": "ls"}}})
        self.assertEqual(self._run(payload.encode()), "{}")


class EngineWiringTest(unittest.TestCase):
    def test_adapter_event_advances_mutation_generation(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            from toolahead.proxy import PrefetchEngine
            engine = PrefetchEngine(
                tmp.name, os.path.join(tmp.name, ".pt.json"))
            try:
                event = build_event("PostToolUse", {
                    "toolCall": {"name": "replace_file_content",
                                 "args": {"TargetFile": "app/page.js"}},
                    "stepIdx": 1, "conversationId": "agy-test"})
                result = engine.handle_agent_event(event)
                self.assertTrue(result["ok"])
                self.assertEqual(engine.mutation_generation, 1)
                failed = build_event("PostToolUse", {
                    "toolCall": {"name": "replace_file_content",
                                 "args": {"TargetFile": "app/page.js"}},
                    "stepIdx": 2, "conversationId": "agy-test",
                    "error": "exit status 1"})
                engine.handle_agent_event(failed)
                self.assertEqual(engine.mutation_generation, 1)
            finally:
                engine.shutdown()
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
