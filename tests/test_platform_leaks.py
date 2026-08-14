# -*- coding: utf-8 -*-
"""平台泄漏扫描器回归测试。

双向验证：
1. 基线：当前代码库必须 0 泄漏（防 macOS 残留回归）；
2. 反向：工具本身必须能检出真实泄漏、跳过平台分支/注释/文档
   （防止规则被改坏后工具失效、基线永远"绿"）。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import check_platform_leaks as cpl  # noqa: E402


class PlatformLeakScannerTests(unittest.TestCase):
    def test_current_codebase_is_clean(self):
        """当前代码基线：0 泄漏（工具接入的验收标准）。"""
        self.assertEqual(cpl.main(["--quiet"]), 0)

    def test_detects_backend_leak_outside_branches(self):
        src = 'import subprocess\nr = subprocess.run(["lsof", "-i"], check=True)\n'
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "leak.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write(src)
            hits = cpl.scan_py(path, cpl.BACKEND_PATTERNS)
        labels = [label for _, label, _ in hits]
        self.assertIn("lsof 命令", labels)

    def test_skips_posix_branch_body(self):
        src = ('import sysops\n'
               'if sysops.IS_POSIX:\n'
               '    subprocess.run(["lsof", "-i"], check=True)\n'
               'else:\n'
               '    print("win")\n')
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "branch.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write(src)
            hits = cpl.scan_py(path, cpl.BACKEND_PATTERNS)
        self.assertEqual(hits, [])

    def test_skips_windows_branch_else(self):
        """if IS_WINDOWS: ... else: <POSIX> 的 else 分支也应跳过。"""
        src = ('if sysops.IS_WINDOWS:\n'
               '    print("win")\n'
               'else:\n'
               '    subprocess.run(["osascript", "-e", "x"])\n')
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "else_branch.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write(src)
            hits = cpl.scan_py(path, cpl.BACKEND_PATTERNS)
        self.assertEqual(hits, [])

    def test_skips_implicit_else_tail(self):
        """if IS_WINDOWS 提前 return 后，函数尾部是隐式 POSIX 分支。"""
        src = ('def command_for_script(path):\n'
               '    if sysops.IS_WINDOWS:\n'
               '        return "cmd"\n'
               '    return "/bin/zsh -- %s" % path\n')
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "implicit.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write(src)
            hits = cpl.scan_py(path, cpl.BACKEND_PATTERNS)
        self.assertEqual(hits, [])

    def test_skips_docstring_and_comment(self):
        src = ('"""本模块提供 lsof 封装（平台差异见 sysops）。"""\n'
               'x = 1  # osascript 仅 macOS 使用\n')
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "doc.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write(src)
            hits = cpl.scan_py(path, cpl.BACKEND_PATTERNS)
        self.assertEqual(hits, [])

    def test_detects_frontend_leak(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "leak.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write("const runner = 'python3 -- ' + p;\n")
            hits = cpl.scan_frontend(path, cpl.FRONTEND_PATTERNS)
        labels = [label for _, label, _ in hits]
        self.assertIn("python3 命令名", labels)

    def test_skips_mod_key_platform_definition(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "ok.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write("export const MOD_KEY = IS_MAC ? '⌘' : 'Ctrl';\n")
            hits = cpl.scan_frontend(path, cpl.FRONTEND_PATTERNS)
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
