import unittest

from launch_spec import (LaunchSpecError, command_from_launch_spec,
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


if __name__ == "__main__":
    unittest.main()
