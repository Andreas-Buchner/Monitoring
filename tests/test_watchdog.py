import http.server
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import watchdog  # noqa: E402


class Handler(http.server.BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        code = {"/ok": 200, "/missing": 404, "/boom": 502}.get(self.path, 200)
        self.send_response(code); self.end_headers(); self.wfile.write(b"x")

    def do_POST(self):
        Handler.requests.append((self.path, self.rfile.read(int(self.headers.get("Content-Length", 0)))))
        self.send_response(200); self.end_headers()

    def log_message(self, *args):
        pass


class WatchdogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_http_status_rules(self):
        self.assertIsNone(watchdog.check_http(self.url("/ok"), {200}))
        self.assertIn("404", watchdog.check_http(self.url("/missing"), {200}))
        self.assertIsNone(watchdog.check_http(self.url("/missing"), "<500"))   # "any answer" for apps without /health
        self.assertIn("502", watchdog.check_http(self.url("/boom"), "<500"))   # Cloudflare's origin-down answer
        self.assertIn("nicht erreichbar", watchdog.check_http("http://127.0.0.1:9/", {200}))

    def test_tcp(self):
        self.assertIsNone(watchdog.check_tcp(self.port))
        self.assertIn("nimmt keine Verbindung", watchdog.check_tcp(9))

    def test_thresholds(self):
        self.assertIsNone(watchdog.check_disk("/", 101))
        self.assertIn("voll", watchdog.check_disk("/", 0.0001))
        self.assertIsNone(watchdog.check_mem(0))
        self.assertIn("Arbeitsspeicher", watchdog.check_mem(101))
        self.assertIn("°C", watchdog.check_temp(-1) or "°C")

    def test_media_folder(self):
        self.assertIsNone(watchdog.check_media(str(Path(__file__).resolve().parent)))          # exists, not empty
        self.assertIn("nicht lesbar", watchdog.check_media("/nonexistent/medien"))
        with tempfile.TemporaryDirectory() as empty:
            self.assertIn("leer", watchdog.check_media(empty))

    def test_maintenance_flag_skips_a_group_and_expires(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(watchdog, "MAINT_DIR", Path(d)):
                self.assertFalse(watchdog.in_maintenance("x"))
                (Path(d) / "x").touch()
                self.assertTrue(watchdog.in_maintenance("x"))
                self.assertFalse(watchdog.in_maintenance("x", now=time.time() + 13 * 3600))   # forgotten flag expires

    def test_a_crashing_test_is_a_failure(self):
        with mock.patch.dict(watchdog.CHECKS, {"tcp": lambda port: 1 / 0}):
            self.assertIn("abgestürzt", watchdog.run_test(("tcp", 1)))

    def test_alert_only_after_two_failures_in_a_row(self):
        self.assertEqual(watchdog.decide([], 5), ("ok", 0))
        self.assertEqual(watchdog.decide(["x"], 0), ("wait", 1))
        self.assertEqual(watchdog.decide(["x"], 1), ("fail", 2))
        self.assertEqual(watchdog.decide(["x"], 7), ("fail", 8))

    def test_run_pings_success_or_fail_with_reasons(self):
        sent = []
        groups = {"a": ("A", []), "b": ("B", [("tcp", 9)])}
        urls = {"a": "https://hc.example/a", "b": "https://hc.example/b"}
        state = {}
        with mock.patch.object(watchdog, "GROUPS", groups), \
             mock.patch.object(watchdog, "load", lambda path: urls if path == watchdog.CHECKS_FILE else state), \
             mock.patch.object(watchdog, "ping", lambda url, body=None: sent.append((url, body))), \
             mock.patch.object(Path, "write_text", lambda self, text: state.update(json.loads(text))):
            watchdog.run()
            self.assertEqual(sent, [("https://hc.example/a", None)])        # b failed once: quiet
            sent.clear()
            watchdog.run()
            self.assertEqual(sent[0], ("https://hc.example/a", None))
            self.assertEqual(sent[1][0], "https://hc.example/b/fail")        # failed twice: alert
            self.assertIn("Port 9", sent[1][1])

    def test_ping_without_a_configured_check_is_harmless(self):
        with mock.patch.object(watchdog, "load", lambda path: {}):
            self.assertEqual(watchdog.main(["ping", "backup-nope"]), 0)


if __name__ == "__main__":
    unittest.main()
