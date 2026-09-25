"""Regression contracts for the Windows launch model and observation cards.

These tests exercise only the local HTTP API and pure state helpers.  The
runtime launch/stop boundary is patched in every test that could otherwise
reach it, so the suite never starts a managed application.
"""

import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import server
from launch_spec import command_from_launch_spec


class ApiHarness:
    """Short lived HTTP harness backed by a private temporary config."""

    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = server.Config(os.path.join(self.temp.name, "config.json"))
        self.httpd = server.ConsoleServer(
            (server.HOST, 0), server.Handler, self.cfg, 0)
        self.port = self.httpd.server_address[1]
        server.invalidate_state_cache()
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, path, payload=None):
        conn = http.client.HTTPConnection(server.HOST, self.port, timeout=4)
        headers = {}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if method in ("POST", "PUT", "DELETE"):
            headers["X-Console-Token"] = self.httpd.control_token
        headers["Connection"] = "close"
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        status = response.status
        conn.close()
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            decoded = raw
        return status, decoded


def monitor_app(app_id="a1b2c3d4", **overrides):
    app = {
        **server.Config.APP_DEFAULT,
        "id": app_id,
        "name": "观察卡",
        "command": "python -m http.server 8765",
        "cwd": None,
        "port": 8765,
        "kind": "service",
        "controlMode": "monitor",
        "attached": True,
        "launchSpec": None,
        "launchConfigured": False,
        "observation": {
            "pid": 4242,
            "createTime": 100.0,
            "sid": server.SELF_UID,
            "cwd": os.getcwd(),
            "ports": [8765],
            "observedAt": 1,
        },
        "lastPid": 4242,
        "lastCreateTime": 100.0,
    }
    app.update(overrides)
    return app


def structured_spec(executable=None, *, cwd=None, port=8765, args=None):
    return {
        "mode": "exec",
        "executable": executable or sys.executable,
        "args": list(args or ["-m", "http.server", str(port)]),
        "cwd": cwd or os.getcwd(),
        "env": {},
        "readiness": {
            "type": "tcp",
            "host": "127.0.0.1",
            "port": port,
            "url": None,
            "timeoutSec": 1,
        },
    }


class WindowsLaunchContractTests(unittest.TestCase):
    def setUp(self):
        self.h = ApiHarness()

    def tearDown(self):
        self.h.close()

    def _store(self, *apps):
        self.h.cfg.update(lambda data: data["apps"].extend(apps))

    def test_launch_resolve_route_returns_shell_free_spec_and_keeps_argv(self):
        with tempfile.TemporaryDirectory() as cwd:
            special = "snow & rain %TEMP% ^ pipe | 雪"
            command = subprocess.list2cmdline([
                sys.executable, "-c", "print(1)", "--label", special])
            status, body = self.h.request("POST", "/api/launch/resolve", {
                "command": command,
                "cwd": cwd,
                "port": 8765,
                "kind": "service",
            })

        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        spec = body["launchSpec"]
        self.assertEqual(spec["mode"], "exec")
        self.assertTrue(os.path.isabs(spec["executable"]))
        self.assertIn(special, spec["args"])
        self.assertEqual(spec["cwd"], cwd)

    def test_validate_launch_route_checks_without_spawning(self):
        self._store(monitor_app())
        candidate = structured_spec(port=8765)
        with mock.patch.object(server.windows_runtime, "launch") as launch:
            status, body = self.h.request(
                "POST", "/api/apps/a1b2c3d4/validate-launch", {
                    "launchSpec": candidate,
                    "command": command_from_launch_spec(candidate),
                    "cwd": candidate["cwd"],
                    "port": candidate["readiness"]["port"],
                    "kind": "service",
                })

        self.assertNotEqual(status, 404)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body["launchSpec"]["executable"], candidate["executable"])
        launch.assert_not_called()

    def test_monitor_start_stop_restart_all_require_confirmed_launch_spec(self):
        self._store(monitor_app())
        for action in ("start", "stop", "restart"):
            with self.subTest(action=action), \
                    mock.patch.object(server.windows_runtime, "launch") as launch:
                status, body = self.h.request(
                    "POST", "/api/apps/a1b2c3d4/" + action, {})
                self.assertEqual(status, 409)
                self.assertTrue(body.get("launchSpecRequired"))
                launch.assert_not_called()

    def test_monitor_put_rejects_an_unconfirmed_null_launch_spec(self):
        self._store(monitor_app())
        status, body = self.h.request(
            "PUT", "/api/apps/a1b2c3d4", {
                "name": "观察卡",
                "command": "python -m http.server 8765",
                "cwd": os.getcwd(),
                "port": 8765,
                "kind": "service",
                "launchSpec": None,
            })
        self.assertEqual(status, 422)
        self.assertTrue(body.get("launchSpecRequired"))
        saved = server.find_app(self.h.cfg.snapshot(), "a1b2c3d4")
        self.assertEqual(saved["controlMode"], "monitor")

    def test_health_checks_structured_executable_not_compatibility_text(self):
        with tempfile.TemporaryDirectory() as cwd:
            missing = os.path.join(cwd, "missing", "python.exe")
            spec = structured_spec(missing, cwd=cwd)
            app = {
                "id": "a1b2c3d4",
                "name": "bad launch spec",
                # A valid legacy display command must not hide a missing
                # structured executable from the health checker.
                "command": "python -m http.server 8765",
                "cwd": cwd,
                "port": 8765,
                "kind": "service",
                "controlMode": "managed",
                "launchSpec": spec,
                "launchConfigured": True,
            }
            with mock.patch.object(server, "_resolve_runtime",
                                   return_value=sys.executable):
                health = server.inspect_app_health(app)

        self.assertTrue(health["blocking"])
        self.assertTrue(any(issue["kind"] in ("runtime-missing", "script-missing")
                            for issue in health["issues"]))

    def test_attach_updates_observation_without_discarding_launch_spec(self):
        with tempfile.TemporaryDirectory() as cwd:
            spec = structured_spec(cwd=cwd)
            app = {
                **server.Config.APP_DEFAULT,
                "id": "a1b2c3d4",
                "name": "preconfigured service",
                "command": command_from_launch_spec(spec),
                "cwd": cwd,
                "port": 8765,
                "kind": "service",
                "controlMode": "managed",
                "attached": False,
                "launchSpec": spec,
                "launchConfigured": True,
            }
            self._store(app)
            with mock.patch.object(server, "app_alive_sign", return_value=False), \
                    mock.patch.object(server, "scan_listeners",
                                      return_value={(4242, 8765)}), \
                    mock.patch.object(server, "ps_snapshot", return_value={
                        4242: {"uid": server.SELF_UID, "ctime": 200.0}}), \
                    mock.patch.object(server, "listener_app_owners",
                                      return_value={}), \
                    mock.patch.object(server, "lsof_cwds",
                                      return_value={4242: cwd}):
                status, body = self.h.request(
                    "POST", "/api/apps/a1b2c3d4/attach", {"pid": 4242})

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        saved = server.find_app(self.h.cfg.snapshot(), "a1b2c3d4")
        self.assertEqual(saved["command"], app["command"])
        self.assertEqual(saved["launchSpec"], spec)
        self.assertEqual(saved["controlMode"], "monitor")
        self.assertEqual(saved["observation"]["pid"], 4242)
        self.assertEqual(saved["observation"]["createTime"], 200.0)

    def test_reused_pid_does_not_match_observation_identity(self):
        app = monitor_app()
        observed = server.observed_process_pid(
            app,
            listeners={(4242, 8765)},
            snap={4242: {"uid": server.SELF_UID, "ctime": 200.0}},
            cwds={4242: app["observation"]["cwd"]},
        )

        self.assertIsNone(observed)

    def test_pid_reuse_does_not_leave_stale_card_blocking_new_observation(self):
        with tempfile.TemporaryDirectory() as cwd:
            stale = monitor_app(
                app_id="11111111",
                cwd=cwd,
                observation={"pid": 4242, "createTime": 100.0,
                             "sid": server.SELF_UID, "cwd": cwd,
                             "ports": [8765], "observedAt": 1},
                lastPid=4242,
                lastCreateTime=100.0,
            )
            target = {
                **server.Config.APP_DEFAULT,
                "id": "22222222",
                "name": "new listener",
                "command": "python -m http.server 8765",
                "cwd": cwd,
                "port": 8765,
                "kind": "service",
            }
            self._store(stale, target)
            with mock.patch.object(server, "app_alive_sign", return_value=False), \
                    mock.patch.object(server, "scan_listeners",
                                      return_value={(4242, 8765)}), \
                    mock.patch.object(server, "ps_snapshot", return_value={
                        4242: {"uid": server.SELF_UID, "ctime": 200.0}}), \
                    mock.patch.object(server, "listener_app_owners",
                                      return_value={}), \
                    mock.patch.object(server, "lsof_cwds",
                                      return_value={4242: cwd}):
                status, body = self.h.request(
                    "POST", "/api/apps/22222222/attach", {"pid": 4242})

        self.assertEqual(status, 200, body)
        saved = server.find_app(self.h.cfg.snapshot(), "22222222")
        self.assertEqual(saved["observation"]["createTime"], 200.0)

    def test_state_exposes_saved_starting_process_state(self):
        app = {
            **server.Config.APP_DEFAULT,
            "id": "a1b2c3d4",
            "name": "starting",
            "command": "echo running",
            "cwd": os.getcwd(),
            "controlMode": "managed",
            "runInstance": {
                "runId": "run-1",
                "jobName": None,
                "rootPid": 1234,
                "rootCreateTime": 1.0,
                "processState": "starting",
            },
        }
        result = server.build_apps({"apps": [app]}, listeners=set(), groups={})

        self.assertEqual(result[0]["processState"], "starting")


if __name__ == "__main__":
    unittest.main()
