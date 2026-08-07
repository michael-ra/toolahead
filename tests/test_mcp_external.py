"""MCP-Pfad fuer deklarierte externe Kommandos (Execution-Opt-in, kein Replay)."""

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

os.environ.setdefault("PREFETCH_QUIET", "1")

from toolahead.mcp import ToolAheadMCP  # noqa: E402
from toolahead.services import trust_workspace  # noqa: E402
from toolahead.tool_contracts import ToolContractError  # noqa: E402


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


class ExternalCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = self.tmp.name
        self._old_trust = os.environ.get("TOOLAHEAD_TRUST_FILE")
        os.environ["TOOLAHEAD_TRUST_FILE"] = os.path.join(
            self.workspace, ".trust-store.json")
        self.httpd = None
        self.engine = None

    def tearDown(self):
        if self.engine is not None:
            self.engine.shutdown()
        if self.httpd is not None:
            self.httpd.shutdown()
        if self._old_trust is None:
            os.environ.pop("TOOLAHEAD_TRUST_FILE", None)
        else:
            os.environ["TOOLAHEAD_TRUST_FILE"] = self._old_trust
        self.tmp.cleanup()

    def _daemon(self) -> str:
        from toolahead.proxy import build
        port = _free_port()
        self.httpd, _proxy, self.engine = build(
            port=port, workspace=self.workspace,
            table=os.path.join(self.workspace, ".prefetch-table.json"))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{port}"

    def test_declared_external_command_runs_after_blocking_ensure(self):
        svc_port = _free_port()
        _write(os.path.join(self.workspace, "toolahead.toml"), f"""
[services.dev]
command = "{sys.executable} -c 'import socket,time; time.sleep(0.8); s=socket.socket(); s.bind((\\"127.0.0.1\\", {svc_port})); s.listen(50); time.sleep(60)'"
ready.port = {svc_port}
timeout = 10
prewarm = "manual"

[commands.e2e]
match = "sh run_e2e.sh"
requires = ["dev"]
""")
        _write(os.path.join(self.workspace, "run_e2e.sh"),
               f"{sys.executable} -c 'import socket; "
               f"socket.create_connection((\"127.0.0.1\", {svc_port}), timeout=2)'\n")
        trust_workspace(self.workspace)
        url = self._daemon()
        server = ToolAheadMCP(self.workspace, url)

        t0 = time.monotonic()
        result = server.call_tool("run", {"command": "sh run_e2e.sh"})
        elapsed = time.monotonic() - t0
        meta = result["_meta"]["toolahead"]
        self.assertFalse(result["isError"])
        self.assertEqual(meta["exit_code"], 0)
        self.assertTrue(meta["external"])
        self.assertEqual(meta["cache"], "miss")
        self.assertEqual(meta["services"], {"dev": "ready"})
        self.assertGreaterEqual(elapsed, 0.7,
                                "ensure hat nicht auf Readiness gewartet")

        # Zweiter Lauf direkt nach einer Mutation: Service lief schon →
        # Freshness-Hinweis statt Barriere.
        self.engine.note_mutation()
        result2 = server.call_tool("run", {"command": "sh run_e2e.sh"})
        self.assertEqual(result2["_meta"]["toolahead"]["exit_code"], 0)
        self.assertIn("[ToolAhead] note:", result2["content"][0]["text"])

    def test_not_ready_service_yields_actionable_error(self):
        _write(os.path.join(self.workspace, "toolahead.toml"), f"""
[services.dev]
command = "sleep 60"
ready.port = {_free_port()}
timeout = 1
prewarm = "manual"

[commands.e2e]
match = "sh run_e2e.sh"
requires = ["dev"]
""")
        _write(os.path.join(self.workspace, "run_e2e.sh"), "exit 0\n")
        trust_workspace(self.workspace)
        url = self._daemon()
        server = ToolAheadMCP(self.workspace, url)
        with self.assertRaises(ToolContractError) as ctx:
            server.call_tool("run", {"command": "sh run_e2e.sh"})
        self.assertIn("not ready", str(ctx.exception))
        self.assertIn("dev=", str(ctx.exception))

    def test_untrusted_config_executes_with_note_and_no_service(self):
        _write(os.path.join(self.workspace, "toolahead.toml"), """
[services.dev]
command = "sleep 60"
prewarm = "manual"
timeout = 2

[commands.e2e]
match = "sh noop.sh"
requires = ["dev"]
""")
        _write(os.path.join(self.workspace, "noop.sh"), "echo ok\n")
        url = self._daemon()
        server = ToolAheadMCP(self.workspace, url)
        result = server.call_tool("run", {"command": "sh noop.sh"})
        self.assertEqual(result["_meta"]["toolahead"]["exit_code"], 0)
        self.assertIn("not trusted", result["content"][0]["text"])
        self.assertEqual(self.engine.services._procs, {})

    def test_undeclared_command_is_still_rejected(self):
        url = self._daemon()
        server = ToolAheadMCP(self.workspace, url)
        with self.assertRaises(ToolContractError):
            server.call_tool("run", {"command": "sh run_e2e.sh"})


if __name__ == "__main__":
    unittest.main()
