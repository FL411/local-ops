#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""macOS 残留扫描器（Windows 专用仓库回归防线）。

本仓库不再维护 macOS 运行路径。可执行代码、前端运行时文案中
不允许出现 macOS 专属命令、API 或路径；注释与 docstring 除外。

用法：
    python tools/check_platform_leaks.py            # 扫描并报告，泄漏退出码 1
    python tools/check_platform_leaks.py --quiet    # 仅退出码
"""

import ast
import io
import os
import re
import sys
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BACKEND_FILES = [
    "server.py", "sysops.py", "tray.py", "launcher_check.py",
]
FRONTEND_FILES = [
    "static/app.js",
    "static/js/core.js", "static/js/launchpad.js", "static/js/services.js",
    "static/js/overlays.js", "static/js/ports.js", "static/js/widgets.js",
    "static/index.html",
]

BACKEND_PATTERNS = [
    (r"\blsof\b", "lsof 命令"),
    (r"\bosascript\b", "osascript 命令"),
    (r"\bkillpg\b", "killpg API"),
    (r"\bgetuid\b", "getuid API"),
    (r"\bgetpgid\b", "getpgid API"),
    (r"\bfcntl\b", "fcntl API"),
    (r"\bflock\b", "flock API"),
    (r"\bsetsid\b", "setsid API"),
    (r"/bin/bash\b", "bash 绝对路径"),
    (r"/bin/zsh\b", "zsh 绝对路径"),
    (r"/usr/bin/", "系统命令绝对路径"),
    (r"\bbrew\b", "homebrew"),
    (r"\bsay\b", "say 语音命令"),
    (r"\bpkill\b", "pkill 命令"),
    (r"\bkillall\b", "killall 命令"),
    (r"~/Library", "macOS 用户库路径"),
    (r"/Users/", "macOS 用户目录"),
    (r"/tmp/", "POSIX 临时目录"),
    (r"/opt/homebrew", "homebrew 前缀"),
]

FRONTEND_PATTERNS = [
    (r"\bpython3\b", "python3 命令名"),
    (r"/Users/", "macOS 用户目录"),
    (r"~/Library", "macOS 用户库路径"),
    (r"/tmp/", "POSIX 临时目录"),
    (r"\bFinder\b", "Finder 应用"),
    (r"\bTerminal\b", "Terminal 应用"),
    (r"\bhomebrew\b", "homebrew"),
    (r"⌘", "macOS 命令符号"),
]


def _docstring_lines_py(src):
    """返回 Python docstring 行号，扫描时跳过。"""
    skip = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return skip

    def _add_range(node):
        for line_number in range(getattr(node, "lineno", 0),
                                 getattr(node, "end_lineno", 0) + 1):
            skip.add(line_number)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr):
                val = body[0].value
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    _add_range(body[0])
    return skip


def scan_py(path, patterns, rel=None):
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    skip = _docstring_lines_py(src)
    lines = src.splitlines()
    hits = []
    try:
        toks = tokenize.generate_tokens(io.StringIO(src).readline)
        for tok in toks:
            if tok.type not in (tokenize.NAME, tokenize.STRING):
                continue
            if tok.start[0] in skip:
                continue
            for pat, label in patterns:
                if re.search(pat, tok.string):
                    hits.append((tok.start[0], label, lines[tok.start[0] - 1].strip()[:100]))
                    break
    except (tokenize.TokenError, IndentationError):
        pass
    return hits


def _html_skip(raw):
    s = raw.strip()
    if not s:
        return True
    if s.startswith(("<!--", "<!DOCTYPE", "<html", "<head", "<body",
                     "</", "<meta", "<link", "<script", "<style")):
        return True
    return False


def scan_frontend(path, patterns, rel=None):
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    hits = []
    for i, raw in enumerate(lines, 1):
        if path.endswith(".html") and _html_skip(raw):
            continue
        if path.endswith(".js") and raw.strip().startswith(("//", "/*", "*")):
            continue
        for pat, label in patterns:
            if re.search(pat, raw):
                hits.append((i, label, raw.strip()[:100]))
                break
    return hits


def main(argv):
    quiet = "--quiet" in argv
    all_hits = []
    seen = set()
    for f in BACKEND_FILES:
        path = os.path.join(ROOT, f)
        for lineno, label, content in scan_py(
                path, BACKEND_PATTERNS,
                rel=os.path.relpath(path, ROOT)):
            key = (os.path.relpath(path, ROOT), lineno, label)
            if key in seen:
                continue
            seen.add(key)
            all_hits.append((os.path.relpath(path, ROOT), lineno, label,
                             content))
    for f in FRONTEND_FILES:
        path = os.path.join(ROOT, f)
        for lineno, label, content in scan_frontend(path, FRONTEND_PATTERNS):
            key = (os.path.relpath(path, ROOT), lineno, label)
            if key in seen:
                continue
            seen.add(key)
            all_hits.append((os.path.relpath(path, ROOT), lineno, label,
                             content))
    if not quiet:
        if all_hits:
            print("发现 %d 处疑似 macOS 残留（请人工确认是否 Windows 会执行）："
                  % len(all_hits))
            for rel, lineno, label, content in all_hits:
                print("  %-28s 行 %-5d [%s]  %s"
                      % (rel, lineno, label, content))
        else:
            print("OK: 未发现 macOS 残留（当前代码基线干净）")
    return 1 if all_hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
