"""Route-Warming: Datei→Route-Heuristik, Config-Validierung, Warm-Flow."""

import os
import socket
import sys
import tempfile
import time
import unittest

os.environ.setdefault("PREFETCH_QUIET", "1")

from toolahead.services import (  # noqa: E402
    ServiceManager,
    derive_routes,
    trust_workspace,
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


RECORDING_SERVER = '''
import http.server
import socketserver
import sys


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with open(sys.argv[2], "a") as fh:
            fh.write(self.path + "\\n")
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("127.0.0.1", int(sys.argv[1])), Handler) as srv:
    srv.serve_forever()
'''


class DeriveRoutesTest(unittest.TestCase):
    def test_next_app_router(self):
        self.assertEqual(derive_routes("app/page.tsx"), ["/"])
        self.assertEqual(derive_routes("app/dashboard/page.tsx"), ["/dashboard"])
        self.assertEqual(derive_routes("src/app/a/b/page.jsx"), ["/a/b"])
        self.assertEqual(derive_routes("app/(marketing)/pricing/page.tsx"),
                         ["/pricing"])
        self.assertEqual(derive_routes("app/blog/[slug]/page.tsx"), [])
        self.assertEqual(derive_routes("app/dashboard/layout.tsx"), [])

    def test_next_pages_router_and_nuxt(self):
        self.assertEqual(derive_routes("pages/index.tsx"), ["/"])
        self.assertEqual(derive_routes("pages/about.tsx"), ["/about"])
        self.assertEqual(derive_routes("src/pages/docs/index.jsx"), ["/docs"])
        self.assertEqual(derive_routes("pages/settings/profile.vue"),
                         ["/settings/profile"])
        self.assertEqual(derive_routes("pages/_app.tsx"), [])
        self.assertEqual(derive_routes("pages/api/users.ts"), [])
        self.assertEqual(derive_routes("pages/blog/[id].tsx"), [])

    def test_sveltekit(self):
        self.assertEqual(derive_routes("src/routes/+page.svelte"), ["/"])
        self.assertEqual(derive_routes("src/routes/about/+page.svelte"),
                         ["/about"])
        self.assertEqual(derive_routes("src/routes/blog/[slug]/+page.svelte"),
                         [])

    def test_non_page_files(self):
        self.assertEqual(derive_routes(None), [])
        self.assertEqual(derive_routes("backend/logic.py"), [])
        self.assertEqual(derive_routes("components/Button.tsx"), [])


class WarmRoutesConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = self.tmp.name
        os.environ["TOOLAHEAD_TRUST_FILE"] = os.path.join(
            self.workspace, ".trust-store.json")
        self.warnings: list[str] = []

    def tearDown(self):
        os.environ.pop("TOOLAHEAD_TRUST_FILE", None)
        self.tmp.cleanup()

    def _load(self) -> ServiceManager:
        return ServiceManager.load(
            self.workspace,
            on_event=lambda kind, msg: self.warnings.append(f"{kind}:{msg}"))

    def test_warm_routes_require_url_basis(self):
        _write(os.path.join(self.workspace, "toolahead.toml"), """
[services.no-basis]
command = "sleep 60"
warm_routes = ["/"]

[services.bad-entry]
command = "sleep 60"
ready.port = 3000
warm_routes = ["dashboard"]

[services.ok]
command = "sleep 60"
ready.port = 3000
warm_routes = ["/", "auto"]
prewarm = "manual"
""")
        manager = self._load()
        self.assertEqual(list(manager.specs), ["ok"])
        self.assertEqual(manager.specs["ok"].warm_routes, ("/", "auto"))
        self.assertEqual(manager.specs["ok"].base_url(),
                         "http://127.0.0.1:3000")


class WarmFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = self.tmp.name
        os.environ["TOOLAHEAD_TRUST_FILE"] = os.path.join(
            self.workspace, ".trust-store.json")
        self.port = _free_port()
        self.hits = os.path.join(self.workspace, "hits.log")
        _write(os.path.join(self.workspace, "server.py"), RECORDING_SERVER)

    def tearDown(self):
        os.environ.pop("TOOLAHEAD_TRUST_FILE", None)
        self.tmp.cleanup()

    def _manager(self, prewarm: str) -> ServiceManager:
        _write(os.path.join(self.workspace, "toolahead.toml"), f"""
[services.web]
command = "{sys.executable} server.py {self.port} hits.log"
ready.port = {self.port}
timeout = 10
prewarm = "{prewarm}"
warm_routes = ["/health", "auto"]
""")
        trust_workspace(self.workspace)
        return ServiceManager.load(self.workspace)

    def _hit_lines(self) -> list[str]:
        if not os.path.exists(self.hits):
            return []
        with open(self.hits, encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]

    def test_mutation_warms_declared_and_derived_routes(self):
        manager = self._manager("mutation")
        try:
            manager.warm_after_mutation("app/dashboard/page.tsx")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                lines = self._hit_lines()
                if "/health" in lines and "/dashboard" in lines:
                    break
                time.sleep(0.2)
            self.assertIn("/health", self._hit_lines())
            self.assertIn("/dashboard", self._hit_lines())
        finally:
            manager.stop_all()

    def test_manual_service_is_never_booted_for_warming(self):
        manager = self._manager("manual")
        try:
            manager.warm_after_mutation("app/dashboard/page.tsx")
            time.sleep(1.0)
            self.assertEqual(manager._procs, {})
            self.assertEqual(self._hit_lines(), [])
        finally:
            manager.stop_all()


if __name__ == "__main__":
    unittest.main()
