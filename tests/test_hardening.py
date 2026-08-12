import contextlib
import http.client
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import server


class HttpHarness:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self.tmp.name, "config.json")
        self.config_path = path
        self.cfg = server.Config(path)
        self.httpd = server.ConsoleServer(
            (server.HOST, 0), server.Handler, self.cfg, 0)
        self.port = self.httpd.server_address[1]
        server.invalidate_state_cache()  # 每个用例从空缓存开始，避免跨用例污染
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(server.HOST, self.port, timeout=4)
        request_headers = dict(headers or {})
        if body is not None and not isinstance(body, (bytes, bytearray)):
            body = body.encode("utf-8")
        conn.request(method, path, body=body, headers=request_headers)
        response = conn.getresponse()
        raw = response.read()
        result_headers = dict(response.getheaders())
        status = response.status
        conn.close()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = raw
        return status, payload, result_headers


class HttpSecurityTests(unittest.TestCase):
    def setUp(self):
        self.h = HttpHarness()

    def tearDown(self):
        self.h.close()

    def _browser_headers(self, cookie=None, origin=None):
        expected = "http://127.0.0.1:%d" % self.h.port
        headers = {
            "Content-Type": "application/json",
            "Origin": expected if origin is None else origin,
            "Sec-Fetch-Site": "same-origin",
        }
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _session_cookie(self):
        status, _, headers = self.h.request("GET", "/api/state")
        self.assertEqual(status, 200)
        value = headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", value)
        self.assertIn("SameSite=Strict", value)
        return value.split(";", 1)[0]

    def test_dns_rebinding_host_is_rejected_without_setting_cookie(self):
        status, body, headers = self.h.request(
            "GET", "/api/state",
            headers={"Host": "attacker.example:%d" % self.h.port})
        self.assertEqual(status, 421)
        self.assertFalse(body["ok"])
        self.assertNotIn("Set-Cookie", headers)

    def test_cross_origin_browser_write_is_rejected_even_with_cookie(self):
        cookie = self._session_cookie()
        headers = self._browser_headers(cookie, "https://attacker.example")
        headers["Sec-Fetch-Site"] = "cross-site"
        status, body, _ = self.h.request(
            "POST", "/api/ui/theme", json.dumps({"theme": "ops"}), headers)
        self.assertEqual(status, 403)
        self.assertFalse(body["ok"])
        self.assertEqual(self.h.cfg.snapshot()["uiTheme"], "ops")

    def test_same_origin_browser_write_requires_valid_http_only_session(self):
        status, _, _ = self.h.request(
            "POST", "/api/ui/theme", json.dumps({"theme": "ops"}),
            self._browser_headers())
        self.assertEqual(status, 403)

        cookie = self._session_cookie()
        status, body, _ = self.h.request(
            "POST", "/api/ui/theme", json.dumps({"theme": "ops"}),
            self._browser_headers(cookie))
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.h.cfg.snapshot()["uiTheme"], "ops")

    def test_simple_form_post_cannot_reach_bodyless_control_action(self):
        status, body, _ = self.h.request(
            "POST", "/api/console/stop", "x=1",
            {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 415)
        self.assertFalse(body["ok"])
        # The rejected request must not have scheduled shutdown.
        status, _, _ = self.h.request("GET", "/")
        self.assertEqual(status, 200)

    def test_headerless_local_cli_json_remains_compatible(self):
        status, body, _ = self.h.request(
            "POST", "/api/ui/theme", json.dumps({"theme": "ops"}),
            {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_cors_preflight_is_explicitly_denied(self):
        status, _, headers = self.h.request(
            "OPTIONS", "/api/apps", headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
            })
        self.assertEqual(status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", headers)


class AtomicAttachCreateTests(unittest.TestCase):
    def setUp(self):
        self.h = HttpHarness()

    def tearDown(self):
        self.h.close()

    def _create(self):
        return self.h.request(
            "POST",
            "/api/apps",
            json.dumps({
                "name": "博客",
                "command": "npm run dev",
                "cwd": "/expected",
                "port": 3000,
                "kind": "service",
                "attachPid": 4242,
            }),
            {"Content-Type": "application/json"},
        )

    def test_create_and_attach_are_persisted_as_one_result(self):
        with mock.patch.object(server, "app_alive_sign", return_value=False), \
                mock.patch.object(server, "scan_listeners",
                                  return_value={(4242, 3000)}), \
                mock.patch.object(server, "ps_snapshot",
                                  return_value={4242: {"uid": server.SELF_UID}}), \
                mock.patch.object(server, "listener_app_owners",
                                  return_value={}), \
                mock.patch.object(server, "lsof_cwds",
                                  return_value={4242: "/actual"}):
            status, body, _ = self._create()

        self.assertEqual(status, 200)
        self.assertTrue(body["attached"])
        self.assertTrue(body["running"])
        self.assertEqual(body["pid"], 4242)
        apps = self.h.cfg.snapshot()["apps"]
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["lastPid"], 4242)
        self.assertEqual(apps[0]["cwd"], "/actual")
        self.assertTrue(apps[0]["attached"])

    def test_failed_attach_does_not_leave_a_stopped_card(self):
        with mock.patch.object(server, "app_alive_sign", return_value=False), \
                mock.patch.object(server, "scan_listeners", return_value=set()):
            status, body, _ = self._create()

        self.assertEqual(status, 409)
        self.assertFalse(body["ok"])
        self.assertEqual(self.h.cfg.snapshot()["apps"], [])


class DeliveryMetadataTests(unittest.TestCase):
    def setUp(self):
        self.h = HttpHarness()

    def tearDown(self):
        self.h.close()

    def test_state_exposes_version_schema_and_component_degradation(self):
        with mock.patch.object(server, "build_services",
                               side_effect=RuntimeError("lsof failed")), \
                mock.patch.object(server, "build_watched", return_value=[]), \
                mock.patch.object(server, "build_apps", return_value=[]), \
                mock.patch.object(server, "list_themes", return_value=[]):
            status, body, _ = self.h.request("GET", "/api/state")

        self.assertEqual(status, 200)
        self.assertEqual(body["version"], server.APP_VERSION)
        self.assertEqual(body["schemaVersion"],
                         server.CURRENT_SCHEMA_VERSION)
        self.assertTrue(body["degraded"])
        self.assertEqual(body["degradedReasons"][0]["component"], "services")
        self.assertIn("configHealth", body)
        self.assertTrue(body["configHealth"]["writable"])

    def test_health_is_lightweight_and_reports_runtime_metadata(self):
        icons = os.path.join(self.h.tmp.name, "icons")
        logs = os.path.join(self.h.tmp.name, "logs")
        os.chmod(self.h.tmp.name, 0o700)
        os.mkdir(icons, 0o700)
        os.mkdir(logs, 0o700)
        os.chmod(icons, 0o700)
        os.chmod(logs, 0o700)
        with mock.patch.object(server, "DATA_DIR", self.h.tmp.name), \
                mock.patch.object(server, "ICONS_DIR", icons), \
                mock.patch.object(server, "LOGS_DIR", logs), \
                mock.patch.object(server, "CONFIG_PATH", self.h.config_path), \
                mock.patch.object(server, "build_services") as services:
            status, body, _ = self.h.request("GET", "/api/health")

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["version"], server.APP_VERSION)
        self.assertEqual(body["schemaVersion"],
                         server.CURRENT_SCHEMA_VERSION)
        services.assert_not_called()

    def test_root_favicon_serves_the_unified_brand_asset(self):
        status, body, headers = self.h.request("GET", "/favicon.ico")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/x-icon")
        self.assertIsInstance(body, bytes)
        self.assertGreater(len(body), 100)


class AppConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.h = HttpHarness()

    def tearDown(self):
        self.h.close()

    def test_multiple_launch_profiles_may_share_a_configured_port(self):
        headers = {"Content-Type": "application/json"}
        base = {
            "command": "npm run dev",
            "cwd": None,
            "port": 3000,
            "kind": "service",
        }
        status_a, app_a, _ = self.h.request(
            "POST", "/api/apps",
            json.dumps({**base, "name": "项目 A"}), headers)
        status_b, app_b, _ = self.h.request(
            "POST", "/api/apps",
            json.dumps({**base, "name": "项目 B"}), headers)

        self.assertEqual(status_a, 200)
        self.assertEqual(status_b, 200)
        self.assertNotEqual(app_a["id"], app_b["id"])
        self.assertEqual(
            [app["port"] for app in self.h.cfg.snapshot()["apps"]],
            [3000, 3000],
        )

        healthy = {"status": "ok", "blocking": False, "issues": []}
        with mock.patch.object(server, "app_alive_sign", return_value=False), \
                mock.patch.object(server, "inspect_app_health",
                                  return_value=healthy), \
                mock.patch.object(server, "scan_listeners",
                                  return_value={(999, 3000)}), \
                mock.patch.object(server, "start_app") as start:
            status, body, _ = self.h.request(
                "POST", "/api/apps/%s/start" % app_a["id"], "{}", headers)

        self.assertEqual(status, 409)
        self.assertIn("已被 PID 999 占用", body["error"])
        start.assert_not_called()


class OperationLockTests(unittest.TestCase):
    def setUp(self):
        self.h = HttpHarness()
        app = {**server.Config.APP_DEFAULT,
               "id": "deadbeef", "name": "Service", "command": "sleep 10",
               "kind": "service", "cwd": self.h.tmp.name}
        self.h.cfg.update(lambda data: data["apps"].append(app))

    def tearDown(self):
        self.h.close()

    def test_concurrent_start_is_rejected_before_second_process_is_spawned(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []
        fake_proc = mock.Mock(pid=43123)
        fake_proc.poll.return_value = None

        def slow_start(app):
            calls.append(app["id"])
            entered.set()
            release.wait(2)
            return True, None, fake_proc, fake_proc.pid, "token"

        first_result = []

        def first_request():
            first_result.append(self.h.request(
                "POST", "/api/apps/deadbeef/start", "{}",
                {"Content-Type": "application/json"}))

        with mock.patch.object(server, "app_alive_sign", return_value=False), \
                mock.patch.object(server, "scan_listeners", return_value=set()), \
                mock.patch.object(server, "start_app", side_effect=slow_start), \
                mock.patch.object(server, "persist_started_app", return_value=True):
            thread = threading.Thread(target=first_request)
            thread.start()
            self.assertTrue(entered.wait(1))
            status, body, _ = self.h.request(
                "POST", "/api/apps/deadbeef/start", "{}",
                {"Content-Type": "application/json"})
            self.assertEqual(status, 409)
            self.assertFalse(body["ok"])
            release.set()
            thread.join(timeout=3)

        self.assertEqual(len(calls), 1)
        self.assertEqual(first_result[0][0], 200)

    def test_delete_keeps_config_when_verified_process_does_not_stop(self):
        with mock.patch.object(server, "app_running", return_value=True), \
                mock.patch.object(server, "stop_app_and_clear",
                                  return_value=(False, "应用仍在运行")):
            status, body, _ = self.h.request(
                "DELETE", "/api/apps/deadbeef")
        self.assertEqual(status, 409)
        self.assertFalse(body["ok"])
        self.assertIsNotNone(server.find_app(
            self.h.cfg.snapshot(), "deadbeef"))

    def test_start_preflight_blocks_invalid_config_without_spawning(self):
        health = {
            "status": "error", "blocking": True,
            "issues": [{"title": "脚本不可用", "detail": "找不到脚本"}],
        }
        with mock.patch.object(server, "app_alive_sign", return_value=False), \
                mock.patch.object(server, "inspect_app_health",
                                  return_value=health), \
                mock.patch.object(server, "start_app") as start:
            status, body, _ = self.h.request(
                "POST", "/api/apps/deadbeef/start", "{}",
                {"Content-Type": "application/json"})
        self.assertEqual(status, 422)
        self.assertFalse(body["ok"])
        self.assertEqual(body["health"], health)
        start.assert_not_called()

    def test_restart_preflight_does_not_stop_a_working_service(self):
        health = {
            "status": "error", "blocking": True,
            "issues": [{"title": "工作目录不可用", "detail": "目录已移走"}],
        }
        with mock.patch.object(server, "app_alive_sign", return_value=True), \
                mock.patch.object(server, "inspect_app_health",
                                  return_value=health), \
                mock.patch.object(server, "stop_app_and_clear") as stop:
            status, body, _ = self.h.request(
                "POST", "/api/apps/deadbeef/restart", "{}",
                {"Content-Type": "application/json"})
        self.assertEqual(status, 422)
        self.assertFalse(body["ok"])
        self.assertIn("旧服务仍在运行", body["error"])
        stop.assert_not_called()


class ProcessLifecycleHardeningTests(unittest.TestCase):
    def _config_with_app(self, directory, app):
        path = os.path.join(directory, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({**server.Config.DEFAULT, "apps": [app]}, f)
        return server.Config(path)

    def test_manual_stop_waits_then_clears_without_recording_last_exit(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(server, "LOGS_DIR", td):
            base = {**server.Config.APP_DEFAULT, "id": "deadbeef",
                    "name": "Service", "command": "sleep 20", "cwd": td}
            cfg = self._config_with_app(td, base)
            ok, error, proc, pgid, token = server.start_app(base)
            self.assertTrue(ok, error)
            server.persist_started_app(cfg, base["id"], proc, pgid, token)
            tracked = server.find_app(cfg.snapshot(), base["id"])
            try:
                time.sleep(0.15)
                stopped, error = server.stop_app_and_clear(cfg, tracked, timeout=2)
                self.assertTrue(stopped, error)
                time.sleep(0.05)
                result = server.find_app(cfg.snapshot(), base["id"])
                self.assertIsNone(result["runToken"])
                self.assertIsNone(result["lastPid"])
                self.assertIsNone(result["lastExit"])
            finally:
                if server.stop_target_alive(
                        {"kind": "group", "id": pgid, "members": [proc.pid]}):
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except OSError:
                        pass

    def test_manual_task_stop_replaces_old_success_with_stopped_result(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(server, "LOGS_DIR", td):
            previous = {"code": 0, "at": 123, "durationSec": 0.1}
            base = {**server.Config.APP_DEFAULT, "id": "deadbeef",
                    "name": "Task", "kind": "task", "command": "sleep 20",
                    "cwd": td, "lastExit": previous}
            cfg = self._config_with_app(td, base)
            ok, error, proc, pgid, token = server.start_app(base)
            self.assertTrue(ok, error)
            server.persist_started_app(cfg, base["id"], proc, pgid, token)
            tracked = server.find_app(cfg.snapshot(), base["id"])
            try:
                time.sleep(0.15)
                stopped, error = server.stop_app_and_clear(
                    cfg, tracked, timeout=2)
                self.assertTrue(stopped, error)
                time.sleep(0.05)
                result = server.find_app(cfg.snapshot(), base["id"])
                self.assertIsNone(result["runToken"])
                self.assertIsNone(result["lastPid"])
                self.assertEqual(result["lastExit"]["status"], "stopped")
                self.assertIsNone(result["lastExit"]["code"])
                self.assertGreaterEqual(result["lastExit"]["at"], 1)
                self.assertNotEqual(result["lastExit"], previous)
            finally:
                if server.stop_target_alive(
                        {"kind": "group", "id": pgid, "members": [proc.pid]}):
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except OSError:
                        pass

    @unittest.skipIf(
        sys.platform == "win32",
        "Windows terminate() is not an ignorable POSIX SIGTERM")
    def test_sigterm_timeout_retains_runtime_identity_for_retry(self):
        command = (
            "python3 -c 'import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(20)'")
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(server, "LOGS_DIR", td):
            base = {**server.Config.APP_DEFAULT, "id": "deadbeef",
                    "name": "Stubborn", "command": command, "cwd": td}
            cfg = self._config_with_app(td, base)
            ok, error, proc, pgid, token = server.start_app(base)
            self.assertTrue(ok, error)
            server.persist_started_app(cfg, base["id"], proc, pgid, token)
            tracked = server.find_app(cfg.snapshot(), base["id"])
            try:
                # Let the child install its SIGTERM handler before exercising
                # the timeout path (the real start endpoint probes for 250ms).
                time.sleep(0.6)
                stopped, error = server.stop_app_and_clear(
                    cfg, tracked, timeout=0.35)
                self.assertFalse(stopped)
                self.assertIn("保留管理状态", error)
                result = server.find_app(cfg.snapshot(), base["id"])
                self.assertEqual(result["runToken"], token)
                self.assertEqual(result["lastPgid"], pgid)
                self.assertTrue(server.stop_target_alive(
                    {"kind": "group", "id": pgid, "members": [proc.pid]}))
            finally:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass


class SingleInstanceTests(unittest.TestCase):
    def test_project_lock_rejects_second_instance_until_release(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "console.lock")
            first = server.acquire_instance_lock(path)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(server.acquire_instance_lock(path))
            finally:
                server.release_instance_lock(first)
            third = server.acquire_instance_lock(path)
            self.assertIsNotNone(third)
            server.release_instance_lock(third)


class StaticFileServingTests(unittest.TestCase):
    """静态路由与路径穿越防护的 HTTP 层回归测试。"""

    def setUp(self):
        self.h = HttpHarness()

    def tearDown(self):
        self.h.close()

    def test_static_assets_serve_with_expected_content_type(self):
        status, body, headers = self.h.request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", headers.get("Content-Type", ""))
        self.assertIsInstance(body, bytes)
        self.assertGreater(len(body), 1000)

        status, body, headers = self.h.request("GET", "/js/core.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", headers.get("Content-Type", ""))

        status, body, headers = self.h.request("GET", "/assets/brand-mark.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/png")

        status, body, headers = self.h.request("GET", "/themes/ops.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers.get("Content-Type", ""))

    def test_missing_static_path_returns_404(self):
        status, _, _ = self.h.request("GET", "/no-such-file.js")
        self.assertEqual(status, 404)

    def test_encoded_path_traversal_is_rejected(self):
        for path in (
                "/..%2f..%2f..%2fetc/passwd",
                "/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
                "/..%2f..%2fserver.py",
        ):
            status, _, _ = self.h.request("GET", path)
            self.assertEqual(status, 404)

    def test_dotdot_normalized_inside_static_never_reaches_parent(self):
        status, _, _ = self.h.request("GET", "/js/../server.py")
        self.assertEqual(status, 404)
        status, _, _ = self.h.request("GET", "/js/../../server.py")
        self.assertEqual(status, 404)

    def test_icon_route_cannot_escape_icon_dir(self):
        status, _, _ = self.h.request("GET", "/icons/../../etc/passwd")
        self.assertEqual(status, 404)

    def test_symlink_inside_static_cannot_escape_to_outside(self):
        with tempfile.TemporaryDirectory() as td:
            outside = os.path.join(td, "secret.txt")
            with open(outside, "w", encoding="utf-8") as f:
                f.write("secret")
            static = os.path.join(td, "static")
            os.mkdir(static)
            os.symlink(outside, os.path.join(static, "leak.txt"))
            with mock.patch.object(server, "STATIC_DIR", static):
                status, body, _ = self.h.request("GET", "/leak.txt")
            self.assertEqual(status, 404)
            self.assertNotIn(b"secret", body if isinstance(body, bytes) else b"")


class KillEndpointTests(unittest.TestCase):
    def setUp(self):
        self.h = HttpHarness()
        self.headers = {"Content-Type": "application/json"}

    def tearDown(self):
        self.h.close()

    def test_kill_rejects_missing_or_invalid_pid(self):
        status, body, _ = self.h.request("POST", "/api/kill",
                                         json.dumps({}), self.headers)
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        status, body, _ = self.h.request("POST", "/api/kill",
                                         json.dumps({"pid": "abc"}),
                                         self.headers)
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_kill_refuses_console_itself_and_missing_process(self):
        status, body, _ = self.h.request(
            "POST", "/api/kill", json.dumps({"pid": server.SELF_PID}),
            self.headers)
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertIn("自身", body["error"])

        status, body, _ = self.h.request(
            "POST", "/api/kill", json.dumps({"pid": 99999999}), self.headers)
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertIn("不存在", body["error"])

    def test_kill_sends_sigterm_to_owned_process(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            status, body, _ = self.h.request(
                "POST", "/api/kill", json.dumps({"pid": proc.pid}),
                self.headers)
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            deadline = time.time() + 2
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.05)
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_kill_force_sends_sigkill_to_sigterm_immune_process(self):
        code = ("import signal,time; signal.signal(signal.SIGTERM,"
                " signal.SIG_IGN); time.sleep(30)")
        proc = subprocess.Popen([sys.executable, "-c", code])
        try:
            time.sleep(0.3)  # 等待子进程安装 SIGTERM 处理器
            status, body, _ = self.h.request(
                "POST", "/api/kill",
                json.dumps({"pid": proc.pid, "force": True}), self.headers)
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            deadline = time.time() + 2
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.05)
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()


class WatchAndFlagTests(unittest.TestCase):
    def setUp(self):
        self.h = HttpHarness()
        self.headers = {"Content-Type": "application/json"}

    def tearDown(self):
        self.h.close()

    def test_watch_add_remove_keyword(self):
        status, body, _ = self.h.request(
            "POST", "/api/watch",
            json.dumps({"keyword": "ffmpeg", "action": "add"}), self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["keywords"], ["ffmpeg"])
        self.assertEqual(self.h.cfg.snapshot()["watchedKeywords"],
                         ["ffmpeg"])

        # 重复添加不产生重复项
        status, body, _ = self.h.request(
            "POST", "/api/watch",
            json.dumps({"keyword": "ffmpeg", "action": "add"}), self.headers)
        self.assertEqual(body["keywords"], ["ffmpeg"])

        status, body, _ = self.h.request(
            "POST", "/api/watch",
            json.dumps({"keyword": "ffmpeg", "action": "remove"}),
            self.headers)
        self.assertEqual(body["keywords"], [])
        self.assertEqual(self.h.cfg.snapshot()["watchedKeywords"], [])

    def test_watch_rejects_invalid_action_and_missing_keyword(self):
        status, body, _ = self.h.request(
            "POST", "/api/watch",
            json.dumps({"keyword": "ffmpeg", "action": "toggle"}),
            self.headers)
        self.assertEqual(status, 400)
        status, body, _ = self.h.request(
            "POST", "/api/watch",
            json.dumps({"keyword": "", "action": "add"}), self.headers)
        self.assertEqual(status, 400)

    def test_service_flag_toggles_hidden_pinned_promoted(self):
        key = "mysvc:3000"
        for flag in ("hidden", "pinned", "promoted"):
            status, _, _ = self.h.request(
                "POST", "/api/services/flag",
                json.dumps({"key": key, "flag": flag, "value": True}),
                self.headers)
            self.assertEqual(status, 200)
        cfg = self.h.cfg.snapshot()
        self.assertIn(key, cfg["hidden"])
        self.assertIn(key, cfg["pinned"])
        self.assertIn(key, cfg["promoted"])

        status, _, _ = self.h.request(
            "POST", "/api/services/flag",
            json.dumps({"key": key, "flag": "hidden", "value": False}),
            self.headers)
        self.assertEqual(status, 200)
        self.assertNotIn(key, self.h.cfg.snapshot()["hidden"])

        status, body, _ = self.h.request(
            "POST", "/api/services/flag",
            json.dumps({"key": key, "flag": "bogus", "value": True}),
            self.headers)
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])


class StateMutationEndpointTests(unittest.TestCase):
    def setUp(self):
        self.h = HttpHarness()
        for app in (
                {**server.Config.APP_DEFAULT, "id": "aaaa0001",
                 "name": "服务一", "command": "npm run dev", "kind": "service",
                 "cwd": "/one"},
                {**server.Config.APP_DEFAULT, "id": "bbbb0002",
                 "name": "服务二", "command": "npm run build", "kind": "service",
                 "cwd": "/two"},
                {**server.Config.APP_DEFAULT, "id": "cccc0003",
                 "name": "服务三", "command": "npm run test", "kind": "service",
                 "cwd": "/three"},
        ):
            self.h.cfg.update(lambda data, a=app: data["apps"].append(a))

    def tearDown(self):
        self.h.close()

    def test_reorder_persists_stable_cross_section_order(self):
        headers = {"Content-Type": "application/json"}
        status, body, _ = self.h.request(
            "POST", "/api/apps/reorder",
            json.dumps({"ids": ["bbbb0002", "aaaa0001"]}), headers)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(
            [app["id"] for app in self.h.cfg.snapshot()["apps"]],
            ["bbbb0002", "aaaa0001", "cccc0003"])

        # 只涉及部分 id：被点名的移到前面，未涉及的保持相对顺序（stable sort）
        status, body, _ = self.h.request(
            "POST", "/api/apps/reorder",
            json.dumps({"ids": ["cccc0003"]}), headers)
        self.assertEqual(
            [app["id"] for app in self.h.cfg.snapshot()["apps"]],
            ["cccc0003", "bbbb0002", "aaaa0001"])

    def test_delete_removes_config_icon_and_log_files(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(server, "ICONS_DIR", td), \
                mock.patch.object(server, "LOGS_DIR", td), \
                mock.patch.object(server, "app_running", return_value=False):
            icon = os.path.join(td, "aaaa0001.png")
            fav = os.path.join(td, "fav-aaaa0001.ico")
            log = os.path.join(td, "aaaa0001.log")
            for path in (icon, fav, log):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("x")
            status, body, _ = self.h.request("DELETE", "/api/apps/aaaa0001")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(
            [app["id"] for app in self.h.cfg.snapshot()["apps"]],
            ["bbbb0002", "cccc0003"])
        self.assertFalse(os.path.exists(icon))
        self.assertFalse(os.path.exists(fav))
        self.assertFalse(os.path.exists(log))

    def test_logs_endpoint_returns_tail(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(server, "LOGS_DIR", td):
            with open(os.path.join(td, "aaaa0001.log"),
                      "w", encoding="utf-8") as f:
                f.write("line1\nline2\nline3\n")
            status, body, _ = self.h.request(
                "GET", "/api/apps/aaaa0001/logs?tail=2")
        self.assertEqual(status, 200)
        self.assertEqual(body["text"], "line2\nline3")

    def test_console_log_endpoint_returns_console_log_tail(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(server, "LOGS_DIR", td):
            with open(os.path.join(td, "console.log"),
                      "w", encoding="utf-8") as f:
                f.write("boot ok\nwarn: x\nstarted\n")
            status, body, _ = self.h.request(
                "GET", "/api/console/log?tail=2")
        self.assertEqual(status, 200)
        self.assertEqual(body["text"], "warn: x\nstarted")

    def test_log_tail_is_bounded_and_defaults_to_300(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(server, "LOGS_DIR", td):
            with open(os.path.join(td, "console.log"),
                      "w", encoding="utf-8") as f:
                f.write("\n".join("line%d" % i for i in range(600)))
            status, body, _ = self.h.request("GET", "/api/console/log")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["text"].splitlines()), 300)


class AttachConflictTests(unittest.TestCase):
    """认领检查与写入同锁：并发请求无法把同一 pid 认领给两张卡片。"""

    def setUp(self):
        self.h = HttpHarness()
        claimed = {**server.Config.APP_DEFAULT, "id": "aaaa0001",
                   "name": "已有卡片", "command": "x", "kind": "service",
                   "cwd": "/other", "port": 3000,
                   "lastPid": 4242, "attached": True}
        other = {**server.Config.APP_DEFAULT, "id": "bbbb0002",
                 "name": "新卡片", "command": "y", "kind": "service",
                 "cwd": "/expected", "port": 3000}
        self.h.cfg.update(lambda d: d["apps"].extend([claimed, other]))

    def tearDown(self):
        self.h.close()

    def _mocks(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(
            server, "app_alive_sign", return_value=False))
        stack.enter_context(mock.patch.object(
            server, "scan_listeners", return_value={(4242, 3000)}))
        stack.enter_context(mock.patch.object(
            server, "ps_snapshot",
            return_value={4242: {"uid": server.SELF_UID}}))
        stack.enter_context(mock.patch.object(
            server, "lsof_cwds", return_value={4242: "/actual"}))
        return stack

    def test_attach_to_pid_claimed_by_other_card_is_rejected_in_lock(self):
        with self._mocks():
            status, body, _ = self.h.request(
                "POST", "/api/apps/bbbb0002/attach",
                json.dumps({"pid": 4242}),
                {"Content-Type": "application/json"})
        self.assertEqual(status, 409)
        self.assertIn("其他卡片", body["error"])
        card = server.find_app(self.h.cfg.snapshot(), "bbbb0002")
        self.assertNotEqual(card["lastPid"], 4242)

    def test_create_with_pid_claimed_by_other_card_is_rejected_in_lock(self):
        with self._mocks():
            status, body, _ = self.h.request(
                "POST", "/api/apps",
                json.dumps({"name": "新应用", "command": "npm run dev",
                            "cwd": "/expected", "port": 3000,
                            "kind": "service", "attachPid": 4242}),
                {"Content-Type": "application/json"})
        self.assertEqual(status, 409)
        self.assertIn("其他卡片", body["error"])
        self.assertEqual(len(self.h.cfg.snapshot()["apps"]), 2)


class StateCacheTests(unittest.TestCase):
    """TTL 缓存：单飞刷新、过期复用，并避免与配置锁互锁。"""

    def setUp(self):
        self._orig_cache = server._state_cache
        server._state_cache = {
            "mono": 0.0,
            "state": None,
            "building": False,
            "generation": 0,
        }

    def tearDown(self):
        server._state_cache = self._orig_cache

    def _snapshot_that_counts(self, calls):
        def fake_build(cfg, port, health=None):
            calls.append(port)
            return {"built": len(calls), "port": port}
        return fake_build

    def test_expired_snapshot_returns_immediately_and_refreshes_once(self):
        calls = []
        cfg = mock.Mock()
        cfg.snapshot.return_value = {}
        cfg.health_info.return_value = {}
        refresh_started = threading.Event()
        release_refresh = threading.Event()

        server._state_cache.update({
            "mono": time.monotonic() - server.STATE_CACHE_TTL - 1,
            "state": {"built": 0, "port": 9600},
        })

        def slow_build(cfg_snapshot, port, health=None):
            calls.append(port)
            refresh_started.set()
            release_refresh.wait(2)
            return {"built": len(calls), "port": port}

        with mock.patch.object(
                server, "build_state",
                side_effect=slow_build):
            first = server.get_state_snapshot(cfg, 9600)
            self.assertTrue(refresh_started.wait(1))
            second = server.get_state_snapshot(cfg, 9600)
            self.assertEqual(calls, [9600])
            self.assertEqual(first, {"built": 0, "port": 9600})
            self.assertIs(second, first)
            release_refresh.set()
            deadline = time.monotonic() + 2
            while server._state_cache.get("building") and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(server._state_cache.get("building"))
            third = server.get_state_snapshot(cfg, 9600)
        self.assertEqual(third, {"built": 1, "port": 9600})

    def test_config_update_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = server.Config(os.path.join(td, "config.json"))
            calls = []
            with mock.patch.object(
                    server, "build_state",
                    side_effect=self._snapshot_that_counts(calls)):
                server.get_state_snapshot(cfg, 9600)
                server.get_state_snapshot(cfg, 9600)
                self.assertEqual(len(calls), 1)
                cfg.update(lambda d: d.__setitem__("uiTheme", "custom"))
                stale = server.get_state_snapshot(cfg, 9600)
                self.assertEqual(stale, {"built": 1, "port": 9600})
                deadline = time.monotonic() + 2
                while server._state_cache.get("building") and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(len(calls), 2)

    def test_config_update_and_state_read_do_not_deadlock(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = server.Config(os.path.join(td, "config.json"))
            snapshot_called = threading.Event()
            update_done = threading.Event()
            state_done = threading.Event()
            original_snapshot = cfg.snapshot

            def observed_snapshot():
                snapshot_called.set()
                return original_snapshot()

            def update_op(data):
                self.assertTrue(snapshot_called.wait(1))
                data["uiTheme"] = "custom"

            cfg.snapshot = observed_snapshot
            with mock.patch.object(
                    server, "build_state",
                    return_value={"built": 1, "port": 9600}):
                updater = threading.Thread(
                    target=lambda: (cfg.update(update_op), update_done.set()),
                    daemon=True)
                reader = threading.Thread(
                    target=lambda: (
                        server.get_state_snapshot(cfg, 9600), state_done.set()),
                    daemon=True)
                updater.start()
                reader.start()
                updater.join(2)
                reader.join(2)

            self.assertTrue(update_done.is_set())
            self.assertTrue(state_done.is_set())


class WindowsProcessSnapshotTests(unittest.TestCase):
    def test_targeted_snapshot_skips_pid_that_exits_before_lookup(self):
        fake_psutil = mock.Mock()
        fake_psutil.Process.side_effect = server.sysops.psutil.NoSuchProcess(
            pid=11716)
        fake_psutil.process_iter.side_effect = AssertionError(
            "targeted lookup must not scan every process")

        with mock.patch.object(
                server.sysops, "_psutil", return_value=fake_psutil):
            snapshot = server.sysops._ps_snapshot_windows({11716})

        self.assertEqual(snapshot, {})
        fake_psutil.process_iter.assert_not_called()

    def test_windows_group_stop_uses_frozen_members_leaf_first(self):
        order = []
        fake_psutil = mock.Mock()

        def process_for(pid):
            proc = mock.Mock()
            proc.terminate.side_effect = lambda: order.append(pid)
            return proc

        fake_psutil.Process.side_effect = process_for
        with mock.patch.object(server.sysops, "IS_POSIX", False), \
                mock.patch.object(server.sysops, "_psutil",
                                  return_value=fake_psutil), \
                mock.patch.object(
                    server.sysops, "_group_members_windows",
                    side_effect=AssertionError("must use frozen members")):
            ok, error = server.sysops.signal_group(
                100, signal.SIGTERM, members=[100, 101, 102])

        self.assertTrue(ok, error)
        self.assertEqual(order, [102, 101, 100])

    @unittest.skipUnless(sys.platform == "win32", "Windows cmd quoting only")
    def test_managed_spawn_accepts_quoted_executable_path(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = os.path.join(td, "quoted-command.log")
            script_path = os.path.join(td, "quoted_probe.py")
            result_path = os.path.join(td, "quoted_probe.done")
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(
                    "from pathlib import Path\n"
                    "Path(%r).write_text('ok', encoding='utf-8')\n"
                    % result_path)
            command = subprocess.list2cmdline([sys.executable, script_path])
            env = os.environ.copy()
            with open(log_path, "wb", buffering=0) as logf:
                proc = server.sysops.spawn_managed(
                    command, td, env, "console-run:test", logf)
                code = proc.wait(timeout=10)

            self.assertEqual(code, 0)
            with open(log_path, "rb") as f:
                output = f.read().decode("utf-8", "replace")
            self.assertIn("console-run:test", output)
            with open(result_path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "ok")


if __name__ == "__main__":
    unittest.main()
