import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

import windows_runtime


class WindowsRuntimePureTests(unittest.TestCase):
    def test_job_name_is_sid_scoped_and_validated(self):
        left = windows_runtime.job_name_for("run-01", "S-1-5-21-10")
        right = windows_runtime.job_name_for("run-01", "S-1-5-21-11")
        self.assertTrue(left.startswith("Local\\LocalOps-"))
        self.assertNotEqual(left, right)
        with self.assertRaises(ValueError):
            windows_runtime.job_name_for("bad/name", "S-1-5-21")

    def test_reopen_rejects_anchor_without_creation_time(self):
        manager = object.__new__(windows_runtime.WindowsRuntimeManager)
        manager._api = mock.Mock()
        with self.assertRaisesRegex(ValueError, "creation time is required"):
            manager.reopen("run-01", sid="S-1-5-21-10", anchor_pid=123)
        manager._api.open_job.assert_not_called()

    def test_environment_overlay_is_case_insensitive_and_unicode_safe(self):
        block = windows_runtime._environment_block({
            "PATH": "C:\\custom;雪",
            "console_runtime_test": "a&b%雪",
        })
        self.assertTrue(block.endswith("\x00\x00"))
        self.assertIn("PATH=C:\\custom;雪", block)
        self.assertIn("console_runtime_test=a&b%雪", block)
        path_entries = [entry for entry in block.split("\x00")
                        if entry and entry.split("=", 1)[0].casefold() == "path"]
        self.assertEqual(len(path_entries), 1)

    def test_oversized_merged_environment_is_rejected_before_create_process(self):
        with mock.patch.dict(os.environ, {"PATH": "C:\\Windows"}, clear=True):
            with self.assertRaisesRegex(ValueError, "environment block"):
                windows_runtime._environment_block({"LARGE": "x" * 32760})

    def test_oversized_final_command_line_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "command line"):
            windows_runtime._checked_command_line("x" * 32767)

    def test_exec_command_uses_windows_argv_quoting_without_shell(self):
        executable = os.path.abspath(sys.executable)
        args = ["-c", 'print("a&b %TEMP% ^ | 雪")', r"C:\space dir\x.py"]
        resolved, command_line = windows_runtime._command_for(
            "exec", executable, args, None, None)
        self.assertEqual(resolved, executable)
        self.assertEqual(command_line, subprocess.list2cmdline([executable, *args]))
        self.assertIn("&b", command_line)
        self.assertIn("%TEMP%", command_line)
        self.assertIn("雪", command_line)

    def test_cmd_mode_has_explicit_wrapper_and_rejects_unrepresentable_quotes(self):
        script = r"C:\work dir\start.cmd"
        cmd_exe, command_line = windows_runtime._cmd_line(
            script, ["a&b", "x^y", "雪"])
        self.assertTrue(cmd_exe.lower().endswith(r"\cmd.exe"))
        self.assertIn(" /d /s /c ", command_line)
        self.assertIn('"C:\\work dir\\start.cmd"', command_line)
        self.assertIn('"a&b"', command_line)
        self.assertIn('"x^y"', command_line)
        with self.assertRaises(ValueError):
            windows_runtime._cmd_quote_argument('nested " quote')
        with self.assertRaises(ValueError):
            windows_runtime._cmd_quote_argument('%TEMP%')

    def test_stdio_partial_setup_closes_already_created_handles(self):
        api = object.__new__(windows_runtime._NativeApi)
        closed = []
        api._null_handle = lambda: 100
        api._duplicated_inheritable_handle = mock.Mock(
            side_effect=[101, OSError("duplicate failed")])
        api.close_handle = closed.append

        with self.assertRaisesRegex(OSError, "duplicate failed"):
            api._prepare_stdio(1, 2)

        self.assertEqual(closed, [101, 100])


@unittest.skipUnless(sys.platform == "win32", "requires Windows Job Objects")
class WindowsRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.manager = windows_runtime.WindowsRuntimeManager()
        self.instances = []

    def tearDown(self):
        for instance in reversed(self.instances):
            try:
                instance.terminate(force=True)
                deadline = time.monotonic() + 5
                while instance.members() and time.monotonic() < deadline:
                    time.sleep(0.05)
            except Exception:
                pass
            try:
                instance.close()
            except Exception:
                pass

    def _launch(self, code, cwd):
        instance = self.manager.launch(
            sys.executable, ["-c", code], cwd=cwd,
            run_id="test-" + uuid.uuid4().hex,
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            sid=windows_runtime.current_user_sid())
        self.instances.append(instance)
        return instance

    def test_launch_assigns_before_resume_and_job_controls_descendants(self):
        with tempfile.TemporaryDirectory() as td:
            code = (
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'], "
                "creationflags=subprocess.CREATE_NO_WINDOW); "
                "time.sleep(60)"
            )
            instance = self._launch(code, td)
            deadline = time.monotonic() + 5
            members = []
            while time.monotonic() < deadline:
                members = instance.members()
                if len(members) >= 2:
                    break
                time.sleep(0.05)
            self.assertIn(instance.pid, members)
            self.assertGreaterEqual(len(members), 2)
            self.assertIsNotNone(instance.creation_time)
            ok, error = instance.terminate(force=True)
            self.assertTrue(ok, error)
            instance.wait(timeout=5)
            deadline = time.monotonic() + 5
            while instance.members() and time.monotonic() < deadline:
                time.sleep(0.05)
            instance.wait_for_empty(timeout=5)
            self.assertEqual(instance.members(), [])

    def test_keeper_has_no_children_and_force_stop_releases_it(self):
        with tempfile.TemporaryDirectory() as td:
            instance = self._launch("import time; time.sleep(60)", td)
            import psutil
            keeper = psutil.Process(instance.anchor_pid)
            self.assertIn("pythonw.exe", keeper.exe().casefold())
            self.assertEqual(keeper.children(recursive=True), [])

            ok, error = instance.terminate(force=True)
            self.assertTrue(ok, error)
            self.assertIsNotNone(instance._api.poll_process(instance._anchor_handle))
            self.assertEqual(instance.members(), [])

    def test_managed_child_does_not_inherit_unlisted_parent_handles(self):
        import msvcrt
        with tempfile.TemporaryDirectory() as td:
            read_fd, write_fd = os.pipe()
            try:
                os.set_inheritable(write_fd, True)
                leaked_handle = msvcrt.get_osfhandle(write_fd)
                output = os.path.join(td, "inherited.txt")
                code = (
                    "import ctypes,sys; "
                    "kernel=ctypes.WinDLL('kernel32',use_last_error=True); "
                    "kernel.GetHandleInformation.argtypes=[ctypes.c_void_p,"
                    "ctypes.POINTER(ctypes.c_ulong)]; "
                    "kernel.GetHandleInformation.restype=ctypes.c_int; "
                    "kernel.GetFileType.argtypes=[ctypes.c_void_p]; "
                    "kernel.GetFileType.restype=ctypes.c_ulong; "
                    "flags=ctypes.c_ulong(); "
                    "found=kernel.GetHandleInformation(ctypes.c_void_p(int(sys.argv[1])),"
                    "ctypes.byref(flags)); "
                    "kind=kernel.GetFileType(ctypes.c_void_p(int(sys.argv[1]))); "
                    "open(sys.argv[2],'w').write(str(bool(found and kind==3)))"
                )
                instance = self.manager.launch(
                    sys.executable, ["-c", code, str(leaked_handle), output],
                    cwd=td, run_id="handles-" + uuid.uuid4().hex,
                    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                    sid=windows_runtime.current_user_sid())
                self.instances.append(instance)
                instance.wait(timeout=5)
                self.assertTrue(os.path.isfile(output))
                with open(output, "r", encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "False")
            finally:
                os.close(read_fd)
                os.close(write_fd)

    def test_named_job_reopens_after_handles_are_closed(self):
        with tempfile.TemporaryDirectory() as td:
            instance = self._launch("import time; time.sleep(60)", td)
            run_id = instance.run_id
            job_name = instance.job_name
            pid = instance.pid
            created = instance.creation_time
            self.assertIn(pid, instance.members())

            # Closing is intentionally not a stop operation.  A new manager
            # reopens the named Job Object just as a restarted console would.
            instance.close()
            reconnected = windows_runtime.WindowsRuntimeManager().reopen(
                run_id, job_name, pid, created,
                sid=windows_runtime.current_user_sid())
            self.assertIsNotNone(reconnected)
            self.instances.append(reconnected)
            self.assertIn(pid, reconnected.members())
            ok, error = reconnected.terminate(force=True)
            self.assertTrue(ok, error)
            reconnected.wait(timeout=5)
            self.assertEqual(reconnected.members(), [])

    def test_job_reopens_while_wrapper_child_outlives_root(self):
        with tempfile.TemporaryDirectory() as td:
            code = (
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'], "
                "close_fds=True,creationflags=subprocess.CREATE_NO_WINDOW); "
                "time.sleep(.2)"
            )
            instance = self._launch(code, td)
            run_id = instance.run_id
            job_name = instance.job_name
            root_pid = instance.pid
            created = instance.creation_time
            instance.wait(timeout=5)
            deadline = time.monotonic() + 5
            members = []
            while time.monotonic() < deadline:
                members = instance.members()
                if members and root_pid not in members:
                    break
                time.sleep(0.05)
            self.assertTrue(members, "worker should remain in the Job Object")
            self.assertNotIn(root_pid, members)
            instance.close()

            reconnected = windows_runtime.WindowsRuntimeManager().reopen(
                run_id, job_name, root_pid, created,
                sid=windows_runtime.current_user_sid())
            self.assertIsNotNone(reconnected)
            self.instances.append(reconnected)
            self.assertNotIn(root_pid, reconnected.members())
            self.assertTrue(reconnected.members())
            ok, error = reconnected.terminate(force=True)
            self.assertTrue(ok, error)
            reconnected.wait(timeout=5)
            self.assertEqual(reconnected.members(), [])

    def test_exec_arguments_preserve_shell_metacharacters(self):
        with tempfile.TemporaryDirectory(prefix="ops space 雪 ") as td:
            output = os.path.join(td, "argv.json")
            argument = 'left & right %TEMP% ^ | "nested" 雪'
            code = (
                "import json,sys; "
                "open(sys.argv[1],'w',encoding='utf-8').write(json.dumps(sys.argv[2:]))"
            )
            instance = self.manager.launch(
                sys.executable, ["-c", code, output, argument], cwd=td,
                run_id="args-" + uuid.uuid4().hex,
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                sid=windows_runtime.current_user_sid())
            self.instances.append(instance)
            instance.wait(timeout=5)
            with open(output, "r", encoding="utf-8") as handle:
                import json
                self.assertEqual(json.load(handle), [argument])

    def test_cmd_mode_preserves_quoted_metacharacters_for_batch_arguments(self):
        with tempfile.TemporaryDirectory(prefix="ops cmd 雪 ") as td:
            batch = os.path.join(td, "start service.cmd")
            output = os.path.join(td, "captured.txt")
            python = subprocess.list2cmdline([sys.executable])
            with open(batch, "w", encoding="utf-8", newline="") as handle:
                handle.write(
                    "@echo off\r\n%s -c \"import json,sys;"
                    "open(sys.argv[1],'w',encoding='utf-8').write("
                    "json.dumps(sys.argv[2:],ensure_ascii=False))\" "
                    "\"%%~1\" %%2\r\n" % python)
            argument = "left & right ^ | 雪"
            instance = self.manager.launch(
                batch, [output, argument], cwd=td, mode="cmd",
                run_id="cmd-" + uuid.uuid4().hex,
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                sid=windows_runtime.current_user_sid())
            self.instances.append(instance)
            instance.wait(timeout=5)
            deadline = time.monotonic() + 3
            while not os.path.isfile(output) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(os.path.isfile(output), "batch script should write its captured argument")
            with open(output, "r", encoding="utf-8") as handle:
                captured = handle.read().strip()
            self.assertEqual(captured, '["%s"]' % argument)
            instance.wait_for_empty(timeout=5)
            self.assertEqual(instance.members(), [])


if __name__ == "__main__":
    unittest.main()
