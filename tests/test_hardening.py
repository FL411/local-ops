import contextlib
import http.client
import io
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

import launcher_check
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

    def request(self, method, path, body=None, headers=None,
                include_token=True):
        conn = http.client.HTTPConnection(server.HOST, self.port, timeout=4)
        request_headers = dict(headers or {})
        if include_token and method in ("POST", "PUT", "DELETE"):
            request_headers.setdefault(
                "X-Console-Token", self.httpd.control_token)
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

    def _browser_headers(self, token=None, origin=None):
        expected = "http://127.0.0.1:%d" % self.h.port
        headers = {
            "Content-Type": "application/json",
            "Origin": expected if origin is None else origin,
            "Sec-Fetch-Site": "same-origin",
        }
        if token:
            headers["X-Console-Token"] = token
        return headers

    def _control_token(self):
        return self.h.httpd.control_token

    def test_dns_rebinding_host_is_rejected_without_setting_cookie(self):
        status, body, headers = self.h.request(
            "GET", "/api/state",
            headers={"Host": "attacker.example:%d" % self.h.port})
        self.assertEqual(status, 421)
        self.assertFalse(body["ok"])
        self.assertNotIn("Set-Cookie", headers)

    def test_cross_origin_browser_write_is_rejected_even_with_token(self):
        token = self._control_token()
        headers = self._browser_headers(token, "https://attacker.example")
        headers["Sec-Fetch-Site"] = "cross-site"
        status, body, _ = self.h.request(
            "POST", "/api/ui/theme", json.dumps({"theme": "ops"}), headers)
        self.assertEqual(status, 403)
        self.assertFalse(body["ok"])
        self.assertEqual(self.h.cfg.snapshot()["uiTheme"], "ops")

    def test_same_origin_browser_write_requires_valid_control_token(self):
        status, _, _ = self.h.request(
            "POST", "/api/ui/theme", json.dumps({"theme": "ops"}),
            self._browser_headers(), include_token=False)
        self.assertEqual(status, 403)

        token = self._control_token()
        status, body, _ = self.h.request(
            "POST", "/api/ui/theme", json.dumps({"theme": "ops"}),
            self._browser_headers(token))
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

    def test_headerless_local_cli_json_is_rejected(self):
        status, body, _ = self.h.request(
            "POST", "/api/ui/theme", json.dumps({"theme": "ops"}),
            {"Content-Type": "application/json"}, include_token=False)
        self.assertEqual(status, 403)
        self.assertFalse(body["ok"])

    def test_get_does_not_publish_control_token_cookie(self):
        status, _, headers = self.h.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertNotIn("Set-Cookie", headers)

    def test_cors_preflight_is_explicitly_denied(self):
        status, _, headers = self.h.request(
            "OPTIONS", "/api/apps", headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
            })
        self.assertEqual(status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", headers)


class LauncherCapabilityTokenTests(unittest.TestCase):
    def test_open_uses_fragment_token_and_restart_uses_header(self):
        token = "a" * 43
        with mock.patch.object(launcher_check, "_read_control_token",
                               return_value=token), \
                mock.patch("webbrowser.open", return_value=True) as open_browser:
            self.assertEqual(launcher_check.main(["launcher", "open", "9600"]), 0)
        self.assertEqual(
            open_browser.call_args.args[0],
            "http://127.0.0.1:9600/#console_token=" + token)

        response = mock.MagicMock()
        response.__enter__.return_value = response
        with mock.patch.object(launcher_check, "_read_control_token",
                               return_value=token), \
                mock.patch.object(launcher_check.urllib.request, "urlopen",
                                  return_value=response) as urlopen:
            self.assertEqual(launcher_check.main(["launcher", "restart", "9600"]), 0)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("X-console-token"), token)

    def test_open_and_restart_fail_closed_without_token(self):
        with mock.patch.object(launcher_check, "_read_control_token",
                               return_value=None), \
                mock.patch("webbrowser.open") as open_browser, \
                mock.patch.object(launcher_check.urllib.request, "urlopen") as urlopen:
            self.assertEqual(launcher_check.main(["launcher", "open", "9600"]), 1)
            self.assertEqual(launcher_check.main(["launcher", "restart", "9600"]), 1)
        open_browser.assert_not_called()
        urlopen.assert_not_called()

    def test_status_marks_disk_memory_mismatch_stale(self):
        health = {"ok": True, "config": {
            "memoryAppCount": 0, "diskAppCount": 0}}
        with mock.patch.object(launcher_check, "_read_json",
                               return_value=health):
            self.assertEqual(
                launcher_check._console_status(9600, disk_app_count=1),
                "STALE")

    def test_status_leaves_matching_instance_running(self):
        health = {"ok": True, "config": {
            "memoryAppCount": 1, "diskAppCount": 1}}
        with mock.patch.object(launcher_check, "_read_json",
                               return_value=health):
            self.assertEqual(
                launcher_check._console_status(9600, disk_app_count=1),
                "RUNNING")

    def test_old_healthy_instance_is_not_replaced_on_state_timeout(self):
        with mock.patch.object(launcher_check, "_read_json",
                               side_effect=[{"ok": True}, None]):
            self.assertEqual(
                launcher_check._console_status(9600, disk_app_count=1),
                "RUNNING")

    def test_main_status_reports_probe_result(self):
        with mock.patch.object(launcher_check, "find_console_status",
                               return_value=("STALE", 9600)), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(
                launcher_check.main(["launcher_check.py", "status"]), 0)
        self.assertEqual(stdout.getvalue().strip(), "STALE 9600")

    def test_launch_retries_a_stale_candidate_and_requires_ready_state(self):
        candidate = mock.Mock()
        candidate.poll.return_value = 1
        statuses = [
            ("STOPPED", None), ("STALE", 9600),
            ("STOPPED", None), ("RUNNING", 9600),
        ]
        with mock.patch.object(launcher_check, "_configured_app_count",
                               return_value=1), \
                mock.patch.object(launcher_check, "find_console_status",
                                  side_effect=statuses), \
                mock.patch.object(launcher_check, "_start_console_candidate",
                                  return_value=candidate) as start:
            port = launcher_check.launch_console(attempts=2, wait_sec=1)

        self.assertEqual(port, 9600)
        self.assertEqual(start.call_count, 2)
        start.assert_has_calls([mock.call(1, None), mock.call(1, None)])

    def test_unreadable_existing_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not-json")
            with mock.patch.object(launcher_check, "_config_path",
                                   return_value=path):
                self.assertIsNone(launcher_check._configured_app_count())

    def test_missing_config_with_existing_token_is_not_first_run(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            token = os.path.join(td, "control.token")
            with open(token, "w", encoding="ascii") as fh:
                fh.write("x" * 43)
            with mock.patch.object(launcher_check, "_config_path",
                                   return_value=path), \
                    mock.patch.object(launcher_check,
                                      "_control_token_path",
                                      return_value=token):
                self.assertIsNone(launcher_check._configured_app_count())

    def test_missing_config_without_token_is_valid_first_run(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            token = os.path.join(td, "control.token")
            with mock.patch.object(launcher_check, "_config_path",
                                   return_value=path), \
                    mock.patch.object(launcher_check,
                                      "_control_token_path",
                                      return_value=token):
                self.assertEqual(launcher_check._configured_app_count(), 0)

    def test_valid_backup_is_used_when_main_config_is_unreadable(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not-json")
            with open(path + ".bak", "w", encoding="utf-8") as fh:
                json.dump({"apps": [{"id": "saved-card"}]}, fh)
            with mock.patch.object(launcher_check, "_config_path",
                                   return_value=path):
                self.assertEqual(launcher_check._configured_app_count(), 1)

    def test_candidate_receives_expected_count_and_never_opens_browser(self):
        process = mock.Mock()
        with mock.patch.object(launcher_check, "_pythonw_executable",
                               return_value="pythonw.exe"), \
                mock.patch.object(launcher_check.subprocess, "Popen",
                                  return_value=process) as popen:
            self.assertIs(
                launcher_check._start_console_candidate(2, 9601), process)

        args = popen.call_args.args[0]
        self.assertIn("--no-browser", args)
        self.assertIn("--expected-app-count", args)
        self.assertEqual(args[args.index("--expected-app-count") + 1], "2")
        self.assertEqual(args[args.index("--preferred-port") + 1], "9601")

    def test_main_launch_reports_only_a_ready_console(self):
        with mock.patch.object(launcher_check, "find_console_port",
                               return_value=None), \
                mock.patch.object(launcher_check, "launch_console",
                                  return_value=9600), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(
                launcher_check.main(["launcher_check.py", "launch"]), 0)
        self.assertEqual(stdout.getvalue().strip(), "RUNNING 9600")



class EnsureRuntimeTests(unittest.TestCase):
    def test_non_windows_ensure_runtime_fails(self):
        buf = io.StringIO()
        with mock.patch.object(launcher_check.sys, "platform", "darwin"):
            with mock.patch.object(
                    launcher_check, "_psutil_importable_without_user_site") as probe:
                with mock.patch.object(launcher_check, "_install_psutil") as install:
                    with mock.patch("sys.stdout", buf):
                        rc = launcher_check.ensure_runtime()
        self.assertEqual(rc, 1)
        self.assertIn("Windows-only", buf.getvalue())
        buf.getvalue().encode("ascii")
        probe.assert_not_called()
        install.assert_not_called()

    def test_windows_visible_psutil_does_not_install(self):
        buf = io.StringIO()
        with mock.patch.object(launcher_check.sys, "platform", "win32"):
            with mock.patch.object(
                    launcher_check, "_psutil_importable_without_user_site",
                    return_value=True):
                with mock.patch.object(launcher_check, "_install_psutil") as install:
                    with mock.patch("sys.stdout", buf):
                        rc = launcher_check.ensure_runtime()
        self.assertEqual(rc, 0)
        install.assert_not_called()
        self.assertEqual(buf.getvalue().strip(), "OK")
        buf.getvalue().encode("ascii")

    def test_user_site_only_is_treated_as_missing(self):
        buf = io.StringIO()
        with mock.patch.object(launcher_check.sys, "platform", "win32"):
            with mock.patch.object(
                    launcher_check, "_psutil_importable_without_user_site",
                    side_effect=[False, True]):
                with mock.patch.object(
                        launcher_check, "_install_psutil",
                        return_value=True) as install:
                    with mock.patch("sys.stdout", buf):
                        rc = launcher_check.ensure_runtime()
        self.assertEqual(rc, 0)
        install.assert_called_once()
        self.assertIn("OK", buf.getvalue())
        buf.getvalue().encode("ascii")

    def test_install_failure_is_error(self):
        buf = io.StringIO()
        with mock.patch.object(launcher_check.sys, "platform", "win32"):
            with mock.patch.object(
                    launcher_check, "_psutil_importable_without_user_site",
                    return_value=False):
                with mock.patch.object(
                        launcher_check, "_install_psutil", return_value=False):
                    with mock.patch("sys.stdout", buf):
                        rc = launcher_check.ensure_runtime()
        self.assertEqual(rc, 1)
        self.assertIn("ERROR", buf.getvalue())
        buf.getvalue().encode("ascii")

    def test_installed_but_still_invisible_is_error(self):
        buf = io.StringIO()
        with mock.patch.object(launcher_check.sys, "platform", "win32"):
            with mock.patch.object(
                    launcher_check, "_psutil_importable_without_user_site",
                    return_value=False):
                with mock.patch.object(
                        launcher_check, "_install_psutil", return_value=True):
                    with mock.patch("sys.stdout", buf):
                        rc = launcher_check.ensure_runtime()
        self.assertEqual(rc, 1)
        self.assertIn("not visible to pythonw", buf.getvalue())
        buf.getvalue().encode("ascii")

    def test_old_python_is_error(self):
        buf = io.StringIO()
        with mock.patch.object(launcher_check.sys, "version_info", (3, 11, 9)):
            with mock.patch("sys.stdout", buf):
                rc = launcher_check.ensure_runtime()
        self.assertEqual(rc, 1)
        self.assertTrue(buf.getvalue().startswith("ERROR"))
        buf.getvalue().encode("ascii")

    def test_main_dispatches_ensure_runtime(self):
        with mock.patch.object(launcher_check, "ensure_runtime",
                               return_value=0) as fn:
            self.assertEqual(
                launcher_check.main(["launcher_check.py", "ensure-runtime"]), 0)
        fn.assert_called_once_with()

    def test_probe_uses_dash_s_to_ignore_user_site(self):
        with mock.patch.object(launcher_check.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)
            self.assertTrue(launcher_check._psutil_importable_without_user_site())
        args = run.call_args.args[0]
        self.assertEqual(args[0], sys.executable)
        self.assertEqual(args[1], "-s")
        self.assertEqual(args[2], "-c")
        self.assertEqual(args[3], "import psutil")

    def test_install_falls_back_to_realpath_purelib(self):
        pip_calls = []

        def fake_run(args, **kwargs):
            pip_calls.append(list(args))
            result = mock.Mock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with mock.patch.object(launcher_check.subprocess, "run", fake_run):
            with mock.patch.object(
                    launcher_check, "_psutil_importable_without_user_site",
                    side_effect=[False, True]):
                with mock.patch.object(
                        launcher_check.sysconfig, "get_path",
                        return_value="prefix-link/Lib/site-packages"):
                    with mock.patch.object(
                            launcher_check.os.path, "realpath",
                            return_value="prefix-real/Lib/site-packages"):
                        self.assertTrue(launcher_check._install_psutil())
        self.assertEqual(len(pip_calls), 2)
        self.assertNotIn("--target", pip_calls[0])
        self.assertIn("--target", pip_calls[1])
        self.assertIn("prefix-real/Lib/site-packages", pip_calls[1])
        self.assertNotIn("prefix-link/Lib/site-packages", pip_calls[1])

    def test_start_bat_and_launcher_call_ensure_runtime(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "start.bat"), encoding="utf-8") as fh:
            bat = fh.read()
        with open(os.path.join(root, "tools", "launcher.cs"),
                  encoding="utf-8") as fh:
            cs = fh.read()
        self.assertIn("ensure-runtime", bat)
        self.assertNotIn("import sys,psutil", bat)
        self.assertIn("ensure-runtime", cs)
        self.assertIn("launcher_check.py\" status", bat)
        self.assertIn('"status"', cs)
        self.assertIn('StartsWith("RUNNING ")', cs)
        self.assertIn("launcher_check.py\" launch", bat)
        self.assertIn('"launch"', cs)
        self.assertNotIn('"server.py --log-to-file"', cs)


class ControlTokenStorageTests(unittest.TestCase):
    def test_console_reuses_a_private_persistent_control_token(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = server.Config(os.path.join(td, "config.json"))
            first = server.ConsoleServer((server.HOST, 0), server.Handler,
                                         cfg, 0)
            try:
                token = first.control_token
                self.assertRegex(token, r"^[A-Za-z0-9_-]{32,128}$")
                self.assertTrue(os.path.isfile(os.path.join(td, "control.token")))
            finally:
                first.server_close()
            second = server.ConsoleServer((server.HOST, 0), server.Handler,
                                          cfg, 0)
            try:
                self.assertEqual(second.control_token, token)
            finally:
                second.server_close()

    def test_private_directory_protection_sets_current_user_owner(self):
        with tempfile.TemporaryDirectory() as td:
            private = os.path.join(td, "private")
            os.mkdir(private)
            server.sysops.protect_private_directory(private)
            self.assertTrue(
                server.sysops.path_owned_by_current_user(private))

    def test_existing_token_with_untrusted_owner_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "control.token")
            with open(path, "w", encoding="ascii") as f:
                f.write("a" * 43)
            with mock.patch.object(server.sysops, "path_owned_by_current_user",
                                   return_value=False):
                with self.assertRaises(OSError):
                    server.load_control_token(path)


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
                                  return_value={4242: {"uid": server.SELF_UID,
                                                       "ctime": 123456.0}}), \
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
        self.assertEqual(apps[0]["lastCreateTime"], 123456.0)
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
        payload = self.h.cfg.snapshot()
        payload["apps"] = [{"id": "disk-app", "name": "Disk app"}]
        with open(self.h.config_path, "w", encoding="utf-8") as config_file:
            json.dump(payload, config_file)
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
        self.assertEqual(body["config"]["memoryAppCount"], 1)
        self.assertEqual(body["config"]["diskAppCount"], 1)
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
        command = "%s -c \"import time; time.sleep(10)\"" % server._quote_win(sys.executable)
        app = {**server.Config.APP_DEFAULT,
               "id": "deadbeef", "name": "Service", "command": command,
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
            self.assertTrue(entered.wait(3))
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
            command = "%s -c \"import time; time.sleep(20)\"" % server._quote_win(sys.executable)
            base = {**server.Config.APP_DEFAULT, "id": "deadbeef",
                    "name": "Service", "command": command, "cwd": td}
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
                        server.sysops.kill_process(proc.pid, force=True)
                    except OSError:
                        pass

    def test_manual_task_stop_replaces_old_success_with_stopped_result(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(server, "LOGS_DIR", td):
            previous = {"code": 0, "at": 123, "durationSec": 0.1}
            command = "%s -c \"import time; time.sleep(20)\"" % server._quote_win(sys.executable)
            base = {**server.Config.APP_DEFAULT, "id": "deadbeef",
                    "name": "Task", "kind": "task", "command": command,
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
                        server.sysops.kill_process(proc.pid, force=True)
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
            try:
                os.symlink(outside, os.path.join(static, "leak.txt"))
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("当前账户没有创建符号链接的权限")
                raise
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

    def test_cached_state_overlays_disk_apps_without_rebuild(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            payload = {
                "schemaVersion": 1,
                "apps": [{
                    "id": "abcd1234", "name": "demo",
                    "command": "python app.py",
                    "cwd": td, "port": 8000, "kind": "service",
                }],
                "hidden": [], "pinned": [], "promoted": [],
                "watchedKeywords": [], "uiTheme": "ops",
                "openBrowser": True,
            }
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            cfg = server.Config(path)
            cfg._data["apps"] = []
            server._state_cache.update({
                "mono": time.monotonic(),
                "state": {"apps": [], "services": [{"pid": 1}],
                           "port": 9600},
                "listeners": set(),
                "groups": {},
                "building": False,
                "generation": 0,
            })

            def fake_build_apps(cfg_snapshot, listeners, groups=None,
                                attached_repairs=None):
                return [{
                    "id": app["id"],
                    "name": app.get("name"),
                    "running": False,
                } for app in cfg_snapshot.get("apps") or []]

            with mock.patch.object(server, "build_state") as build_state, \
                    mock.patch.object(server, "build_apps",
                                      side_effect=fake_build_apps) as build_apps:
                state = server.get_state_snapshot(cfg, 9600)
            build_state.assert_not_called()
            self.assertTrue(build_apps.called)
            self.assertEqual(len(state["apps"]), 1)
            self.assertEqual(state["apps"][0]["id"], "abcd1234")
            self.assertEqual(state["services"], [{"pid": 1}])

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
                self.assertEqual(stale["built"], 1)
                self.assertEqual(stale["port"], 9600)
                self.assertEqual(stale["uiTheme"], "custom")
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
    @unittest.skipUnless(server.sysops.IS_WINDOWS,
                         "Windows TokenUser SID only")
    def test_current_process_identity_is_a_sid(self):
        self.assertRegex(server.sysops.SELF_UID or "", r"^S-1-")
        self.assertEqual(
            server.sysops.process_uid(os.getpid()), server.sysops.SELF_UID)
        self.assertFalse(server.is_current_user(None))

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
        with mock.patch.object(server.sysops, "_psutil",
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


@unittest.skipUnless(server.sysops.IS_WINDOWS,
                     "Windows 专属:msvcrt 单实例锁")
class ConsoleSelfHealTests(unittest.TestCase):
    def test_expected_disk_cards_are_read_before_startup_continues(self):
        with mock.patch.object(server, "_disk_configured_app_count",
                               side_effect=[0, 1]), \
                mock.patch.object(server.time, "sleep") as sleep:
            self.assertEqual(
                server.require_expected_disk_apps("config.json", 1), 1)
        sleep.assert_called_once_with(0.1)

    def test_expected_disk_cards_fail_closed_instead_of_showing_empty(self):
        with mock.patch.object(server, "_disk_configured_app_count",
                               return_value=0):
            with self.assertRaisesRegex(RuntimeError, "启动前配置校验失败"):
                server.require_expected_disk_apps(
                    "config.json", 1, timeout=0)

    def test_unreadable_established_config_fails_even_when_expected_is_zero(self):
        with mock.patch.object(server, "_disk_configured_app_count",
                               return_value=None):
            with self.assertRaisesRegex(RuntimeError, "均不可读"):
                server.require_expected_disk_apps(
                    "config.json", 0, timeout=0)

    def test_disk_count_uses_backup_when_main_is_unreadable(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not-json")
            with open(path + ".bak", "w", encoding="utf-8") as fh:
                json.dump({"apps": [{"id": "saved-card"}]}, fh)
            self.assertEqual(server._disk_configured_app_count(path), 1)

    def test_established_missing_config_is_read_only_and_not_recreated(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            with open(os.path.join(td, "control.token"),
                      "w", encoding="ascii") as fh:
                fh.write("x" * 43)
            cfg = server.Config(path)
            self.assertFalse(cfg.health_info()["writable"])
            self.assertFalse(os.path.exists(path))

    def test_orphan_without_port_is_stale(self):
        self.assertEqual(
            server.console_instance_status({"pid": 9, "ports": []}, 1),
            "stale")

    def test_healthy_instance_is_left_alone(self):
        health = {"ok": True}
        state = {"apps": [{"id": "abcd1234"}]}
        with mock.patch.object(server, "_http_json_localhost",
                               side_effect=[health, state]):
            self.assertEqual(
                server.console_instance_status(
                    {"pid": 11, "ports": [9600]}, 1),
                "healthy")
        with mock.patch.object(server, "find_console_instances",
                               return_value=[{"pid": 11, "ports": [9600]}]):
            with mock.patch.object(server, "console_instance_status",
                                   return_value="healthy"):
                with mock.patch.object(server, "_reap_console_pids") as reap:
                    self.assertFalse(server.reap_stale_console_processes())
                    reap.assert_not_called()

    def test_empty_state_with_disk_apps_is_stale(self):
        health = {"ok": True}
        state = {"apps": []}
        with mock.patch.object(server, "_http_json_localhost",
                               side_effect=[health, state]):
            self.assertEqual(
                server.console_instance_status(
                    {"pid": 11, "ports": [9600]}, 1),
                "stale")

    def test_empty_state_with_empty_disk_is_healthy(self):
        health = {"ok": True}
        state = {"apps": []}
        with mock.patch.object(server, "_http_json_localhost",
                               side_effect=[health, state]):
            self.assertEqual(
                server.console_instance_status(
                    {"pid": 11, "ports": [9600]}, 0),
                "healthy")

    def test_state_timeout_after_healthy_probe_is_not_reaped(self):
        health = {"ok": True, "config": {
            "memoryAppCount": 1, "diskAppCount": 1}}
        with mock.patch.object(server, "_http_json_localhost",
                               side_effect=[health, None]):
            self.assertEqual(
                server.console_instance_status(
                    {"pid": 11, "ports": [9600]}, 1),
                "healthy")

    def test_health_config_mismatch_is_stale_without_state_scan(self):
        health = {"ok": True, "config": {
            "memoryAppCount": 0, "diskAppCount": 0}}
        with mock.patch.object(server, "_http_json_localhost",
                               return_value=health) as request:
            self.assertEqual(
                server.console_instance_status(
                    {"pid": 11, "ports": [9600]}, 1),
                "stale")
        request.assert_called_once_with(9600, "/api/health", 2.0)

    def test_health_timeout_is_stale(self):
        with mock.patch.object(server, "_http_json_localhost",
                               return_value=None):
            self.assertEqual(
                server.console_instance_status(
                    {"pid": 11, "ports": [9600]}, 0),
                "stale")

    def test_reap_stale_kills_only_unhealthy(self):
        instances = [
            {"pid": 21, "ports": [9600]},
            {"pid": 22, "ports": []},
        ]

        def status(item, disk_app_count=0):
            return "healthy" if item["pid"] == 21 else "stale"

        with mock.patch.object(server, "find_console_instances",
                               return_value=instances):
            with mock.patch.object(server, "_disk_configured_app_count",
                                   return_value=1):
                with mock.patch.object(server, "console_instance_status",
                                       side_effect=status):
                    with mock.patch.object(server, "_reap_console_pids",
                                           return_value=[]) as reap:
                        self.assertTrue(server.reap_stale_console_processes())
                        reap.assert_called_once_with([22], force=True)

    def test_restore_apps_from_disk_when_memory_empty(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            payload = {
                "schemaVersion": 1,
                "apps": [{
                    "id": "abcd1234", "name": "demo",
                    "command": "python app.py",
                    "cwd": td, "port": 8000, "kind": "service",
                }],
                "hidden": [], "pinned": [], "promoted": [],
                "watchedKeywords": [], "uiTheme": "ops",
                "openBrowser": True,
            }
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            cfg = server.Config(path)
            cfg._data["apps"] = []
            self.assertTrue(cfg.restore_apps_from_disk_if_empty())
            self.assertEqual(cfg.snapshot()["apps"][0]["id"], "abcd1234")
            self.assertFalse(cfg.restore_apps_from_disk_if_empty())

    def test_restore_does_not_invent_apps_when_disk_empty(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            cfg = server.Config(path)
            cfg._data["apps"] = []
            self.assertFalse(cfg.restore_apps_from_disk_if_empty())
            self.assertEqual(cfg.snapshot()["apps"], [])

    def test_snapshot_rereads_apps_from_disk_without_restore(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            payload = {
                "schemaVersion": 1,
                "apps": [{
                    "id": "abcd1234", "name": "demo",
                    "command": "python app.py",
                    "cwd": td, "port": 8000, "kind": "service",
                }],
                "hidden": [], "pinned": [], "promoted": [],
                "watchedKeywords": [], "uiTheme": "ops",
                "openBrowser": True,
            }
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            cfg = server.Config(path)
            cfg._data["apps"] = []
            snap = cfg.snapshot()
            self.assertEqual(snap["apps"][0]["id"], "abcd1234")
            self.assertEqual(cfg._data["apps"][0]["id"], "abcd1234")

    def test_snapshot_ignores_backup_when_main_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            empty = {
                "schemaVersion": 1,
                "apps": [],
                "hidden": [], "pinned": [], "promoted": [],
                "watchedKeywords": [], "uiTheme": "ops",
                "openBrowser": True,
            }
            backup = dict(empty)
            backup["apps"] = [{
                "id": "abcd1234", "name": "demo",
                "command": "python app.py",
                "cwd": td, "port": 8000, "kind": "service",
            }]
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(empty, fh)
            with open(path + ".bak", "w", encoding="utf-8") as fh:
                json.dump(backup, fh)
            cfg = server.Config(path)
            self.assertEqual(cfg.snapshot()["apps"], [])

    def test_restore_uses_backup_only_if_main_unreadable(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            payload = {
                "schemaVersion": 1,
                "apps": [{
                    "id": "abcd1234", "name": "demo",
                    "command": "python app.py",
                    "cwd": td, "port": 8000, "kind": "service",
                }],
                "hidden": [], "pinned": [], "promoted": [],
                "watchedKeywords": [], "uiTheme": "ops",
                "openBrowser": True,
            }
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            with open(path + ".bak", "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            cfg = server.Config(path)
            self.assertEqual(cfg.snapshot()["apps"][0]["id"], "abcd1234")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{")
            cfg._data["apps"] = []
            self.assertTrue(cfg.restore_apps_from_disk_if_empty())
            self.assertEqual(cfg.snapshot()["apps"][0]["id"], "abcd1234")

    def test_update_does_not_clobber_disk_apps_when_memory_empty(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            payload = {
                "schemaVersion": 1,
                "apps": [{
                    "id": "abcd1234", "name": "demo",
                    "command": "python app.py",
                    "cwd": td, "port": 8000, "kind": "service",
                }],
                "hidden": [], "pinned": [], "promoted": [],
                "watchedKeywords": [], "uiTheme": "ops",
                "openBrowser": True,
            }
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            cfg = server.Config(path)
            cfg._data["apps"] = []
            cfg.update(lambda d: d.__setitem__("openBrowser", False))
            self.assertEqual(cfg.snapshot()["apps"][0]["id"], "abcd1234")
            self.assertFalse(cfg.snapshot()["openBrowser"])
            with open(path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            self.assertEqual(saved["apps"][0]["id"], "abcd1234")
            self.assertFalse(saved["openBrowser"])

    def test_reap_skips_self_and_foreign_uids(self):
        alive = {101}

        def fake_alive(pid):
            return pid in alive

        def fake_kill(pid, force=False):
            alive.discard(pid)

        with mock.patch.object(server, "SELF_PID", 100):
            with mock.patch.object(server, "pid_alive", side_effect=fake_alive):
                with mock.patch.object(
                        server, "process_uid",
                        side_effect=lambda pid: 1 if pid == 101 else 2):
                    with mock.patch.object(
                            server, "is_current_user",
                            side_effect=lambda uid: uid == 1):
                        with mock.patch.object(
                                server.sysops, "kill_process",
                                side_effect=fake_kill) as kill:
                            leftover = server._reap_console_pids(
                                [100, 101, 102], force=True)
                            killed = [c.args[0] for c in kill.call_args_list]
                            self.assertNotIn(100, killed)
                            self.assertIn(101, killed)
                            self.assertNotIn(102, killed)
                            self.assertEqual(leftover, [])


class WindowsInstanceLockTests(unittest.TestCase):
    """回归:单实例锁必须跨进程互斥,未获锁进程优雅返回而非崩溃。

    历史缺陷:锁位置依赖 pid 字符串长度(位数不同可双实例并存并发写配置);
    并发抢锁时未获锁进程在 write/flush 抛 PermissionError 崩溃。
    """

    PROJECT_ROOT = os.path.dirname(os.path.dirname(server.__file__))
    LOCK_PROBE = (
        "import sys, time\n"
        "sys.path.insert(0, %r)\n"
        "import sysops\n"
        "lock = sysops.acquire_lock(sys.argv[1])\n"
        "print('LOCKED' if lock else 'DENIED', flush=True)\n"
        "if lock:\n"
        "    time.sleep(1.5)\n"
        "    sysops.release_lock(lock)\n"
    ) % PROJECT_ROOT

    def _run_probe(self, lock_path, wait=False):
        proc = subprocess.Popen(
            [sys.executable, "-c", self.LOCK_PROBE, lock_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if not wait:
            return proc
        out, _ = proc.communicate(timeout=20)
        return out.strip(), proc.returncode

    def test_concurrent_acquire_is_mutually_exclusive_and_graceful(self):
        """并发 3 进程:恰 1 个持锁、其余优雅拒绝,无崩溃。"""
        with tempfile.TemporaryDirectory() as td:
            lock_path = os.path.join(td, "instance.lock")
            procs = [self._run_probe(lock_path) for _ in range(3)]
            outs = [p.communicate(timeout=20)[0].strip() for p in procs]
            locked = sum(1 for o in outs if o == "LOCKED")
            denied = sum(1 for o in outs if o == "DENIED")
            self.assertEqual(locked, 1, "应恰好一个实例持锁: %s" % outs)
            self.assertEqual(denied, 2, "其余实例应优雅拒绝: %s" % outs)

    def test_reacquire_after_release_succeeds(self):
        """释放后可重新获取(串行场景不误伤)。"""
        with tempfile.TemporaryDirectory() as td:
            lock_path = os.path.join(td, "instance.lock")
            first, rc1 = self._run_probe(lock_path, wait=True)
            self.assertEqual(first, "LOCKED")
            self.assertEqual(rc1, 0)
            second, rc2 = self._run_probe(lock_path, wait=True)
            self.assertEqual(second, "LOCKED")
            self.assertEqual(rc2, 0)


@unittest.skipUnless(server.sysops.IS_WINDOWS,
                     "Windows 专属:系统托盘(纯 ctypes)")
class WindowsTrayTests(unittest.TestCase):
    """回归:托盘常量/命令回调/宿主窗口(右键菜单依赖真实窗口)。"""

    @unittest.skipUnless(server.sysops.IS_WINDOWS
                         and server._tray_mod is not None,
                         "需要 tray 模块")
    def test_wm_null_constant_is_defined(self):
        """曾缺 WM_NULL 常量导致菜单点击后 NameError(命令不执行)。"""
        self.assertEqual(server._tray_mod.WM_NULL, 0)

    @unittest.skipUnless(server.sysops.IS_WINDOWS
                         and server._tray_mod is not None,
                         "需要 tray 模块")
    def test_menu_command_callbacks_fire(self):
        calls = []
        t = server._tray_mod.TrayIcon(
            "test", lambda: calls.append("open"),
            lambda: calls.append("restart"),
            lambda: calls.append("stop"), None)
        t._on_command(server._tray_mod.CMD_OPEN)
        t._on_command(server._tray_mod.CMD_RESTART)
        t._on_command(server._tray_mod.CMD_STOP)
        self.assertEqual(calls, ["open", "restart", "stop"])

    @unittest.skipUnless(server.sysops.IS_WINDOWS
                         and server._tray_mod is not None,
                         "需要 tray 模块")
    def test_hidden_window_is_created(self):
        """宿主必须是普通窗口(非 message-only),TrackPopupMenu 才能弹菜单。"""
        t = server._tray_mod.TrayIcon("t", None, None, None, None)
        hwnd = t._create_hidden_window()
        self.assertIsNotNone(hwnd, "隐藏窗口创建失败")
        if hwnd:
            server._tray_mod.user32.DestroyWindow(hwnd)


if __name__ == "__main__":
    unittest.main()
