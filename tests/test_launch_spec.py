import unittest

from launch_spec import (LaunchSpecError, command_from_launch_spec,
                        default_readiness, http_readiness_url,
                        is_launch_configured, normalize_launch_spec)


class LaunchSpecTests(unittest.TestCase):
    def test_structured_spec_preserves_special_argument_text(self):
        special = 'snow & rain %TEMP% ^ pipe | "quoted" 雪'
        spec = normalize_launch_spec({
            "mode": "exec",
            "executable": r"D:\work space\.venv\Scripts\python.exe",
            "args": ["app.py", "--label", special],
            "cwd": r"D:\work space",
            "env": {"APP_NOTE": special},
            "readiness": None,
        }, port=8765)

        self.assertEqual(spec["args"][-1], special)
        self.assertEqual(spec["env"]["APP_NOTE"], special)
        self.assertEqual(spec["readiness"]["type"], "tcp")
        self.assertTrue(command_from_launch_spec(spec))
        self.assertTrue(is_launch_configured(spec))

    def test_legacy_spec_keeps_raw_shell_command_verbatim(self):
        command = 'echo "a&b%TEMP%^|雪" & python app.py'
        spec = normalize_launch_spec(None, command=command,
                                     cwd=r"D:\project", port=80)

        self.assertEqual(spec["mode"], "legacy-shell")
        self.assertEqual(spec["legacyCommand"], command)
        self.assertEqual(command_from_launch_spec(spec), command)

    def test_exec_requires_absolute_executable_by_default(self):
        with self.assertRaises(LaunchSpecError):
            normalize_launch_spec({"mode": "exec", "executable": "python",
                                   "args": []})

    def test_http_probe_requires_url_and_valid_timeout(self):
        base = {"mode": "exec", "executable": r"C:\Python\python.exe",
                "args": []}
        with self.assertRaises(LaunchSpecError):
            normalize_launch_spec({**base, "readiness": {"type": "http",
                                   "port": 8080}})
        with self.assertRaises(LaunchSpecError):
            normalize_launch_spec({**base, "readiness": {"type": "tcp",
                                   "port": 8080, "timeoutSec": 0}})

    def test_tcp_readiness_only_accepts_loopback_hosts(self):
        base = {"mode": "exec", "executable": r"C:\Python\python.exe",
                "args": []}
        for host in ("localhost", "LOCALHOST", "127.0.0.1", "::1", "[::1]"):
            with self.subTest(host=host):
                spec = normalize_launch_spec({**base, "readiness": {
                    "type": "tcp", "host": host, "port": 8080}})
                self.assertEqual(spec["readiness"]["type"], "tcp")
        for host in ("example.com", "192.168.1.10", "0.0.0.0", "::",
                     "2001:db8::1"):
            with self.subTest(host=host), self.assertRaisesRegex(
                    LaunchSpecError, "TCP readiness 仅允许 loopback"):
                normalize_launch_spec({**base, "readiness": {
                    "type": "tcp", "host": host, "port": 8080}})

    def test_http_readiness_is_loopback_and_uses_the_configured_port(self):
        base = {"mode": "exec", "executable": r"C:\Python\python.exe",
                "args": []}
        for url in ("http://example.com:8080/health",
                    "http://127.0.0.1:8081/health",
                    "https://127.0.0.1:8080/health"):
            with self.subTest(url=url), self.assertRaises(LaunchSpecError):
                normalize_launch_spec({**base, "readiness": {
                    "type": "http", "port": 8080, "url": url}})

    def test_ipv6_readiness_uses_bracketed_http_authority(self):
        readiness = {"type": "http", "host": "::1", "port": 8080,
                     "url": "/health", "timeoutSec": 5}
        spec = normalize_launch_spec({
            "mode": "exec", "executable": r"C:\Python\python.exe",
            "args": [], "readiness": readiness})

        self.assertEqual(default_readiness(8080)["host"], "localhost")
        self.assertEqual(http_readiness_url("::1", 8080, "/health")[0],
                         "http://[::1]:8080/health")
        self.assertEqual(spec["readiness"]["host"], "::1")

    def test_command_and_environment_overlays_obey_windows_size_limits(self):
        base = {"mode": "exec", "executable": r"C:\Python\python.exe",
                "args": ["x" * 32760]}
        with self.assertRaisesRegex(LaunchSpecError, "命令行长度"):
            normalize_launch_spec(base)
        with self.assertRaisesRegex(LaunchSpecError, "环境变量总长度"):
            normalize_launch_spec({**base, "args": [],
                                   "env": {"LARGE": "x" * 32760}})

    def test_unpaired_surrogates_raise_launch_spec_error(self):
        invalid_unicode = "\ud800"
        base = {"mode": "exec", "executable": r"C:\Python\python.exe",
                "args": []}
        invalid_specs = (
            {**base, "args": [invalid_unicode]},
            {**base, "env": {"APP_VALUE": invalid_unicode}},
            {"mode": "legacy-shell", "legacyCommand": invalid_unicode},
        )
        for spec in invalid_specs:
            with self.subTest(spec=spec), self.assertRaisesRegex(
                    LaunchSpecError, "无法编码的 Unicode"):
                normalize_launch_spec(spec)

    def test_readiness_host_rejects_unpaired_surrogates_for_all_probe_types(self):
        invalid_host = "\ud800"
        base = {"mode": "exec", "executable": r"C:\Python\python.exe",
                "args": []}
        for probe_type, extra in (("none", {}), ("tcp", {"port": 8080}),
                                  ("http", {"port": 8080, "url": "/health"})):
            with self.subTest(probe_type=probe_type), self.assertRaisesRegex(
                    LaunchSpecError, "无法编码的 Unicode"):
                normalize_launch_spec({
                    **base, "readiness": {"type": probe_type,
                                            "host": invalid_host, **extra}})


if __name__ == "__main__":
    unittest.main()
