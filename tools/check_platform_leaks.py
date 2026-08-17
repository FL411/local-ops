#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""macOS 残留扫描器（Windows 移植回归防线）。

本仓库是 macOS 原版的 Windows 移植。代码中允许存在 macOS 专属实现
（POSIX 分支内、sysops 封装层、注释与文档），但**不允许**出现在
Windows 实际执行的路径上（无平台条件的可执行代码、前端运行时文案）。

扫描策略（保守，宁可少报配合人工审计，不可误报产生测试噪音）：
- Python：AST 识别平台分支块（if IS_POSIX / IS_WINDOWS 等）与
  `_posix`/`_windows` 后缀函数并整体跳过；tokenize 只扫描 NAME/STRING
  token（注释天然排除）；`PYTHON_CMD` 定义行跳过。
- JS：行规则 + 平台分支大括号块跟踪（`if (IS_MAC) {` 等整体跳过）。
- HTML：行规则；`data-mod-key` 动态渲染行与 `Ctrl/⌘` 对照文案跳过。
- 白名单：显式登记的良性残留，每条注明原因。

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

# macOS 专属命令/API/路径（后端可执行代码中不应出现）
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

# 前端运行时文案/路径中的 macOS 专属内容
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

# 白名单：{相对路径: [近似内容片段]}。命中且行内含片段 → 跳过。
# 良性判断标准：该内容只在 macOS 分支/路径判断中出现，Windows 永不触发。
ALLOWLIST = {
    # server.py 的 macOS 专属容器路径判断：Windows 上 comm/cwd 不会含
    # /Library/Containers/，该分支永不触发（保留供 macOS 使用）。
    "server.py": [
        "Library/Containers",
        # _ORIGIN_SKIP_NAMES 命令名名单：跨平台通用的名字比较（Windows
        # 同样执行但只是字符串比较，无平台 API 调用），setsid/caffeinate
        # 等仅为名单条目。
        "caffeinate",
        # 进程名排除（ps/lsof 名字比较）：排除监控工具自身进程，
        # 跨平台通用，无平台 API 调用。
        'name in ("ps", "lsof")',
    ],
}


def _platform_block_lines_py(src):
    """返回 Python 源码中应整体跳过的行号集合。

    覆盖：docstring、平台分支块（if IS_POSIX / IS_WINDOWS / IS_MAC 的
    body 或 else 中"非 Windows 侧"）、`_posix`/`_windows` 后缀函数、
    PYTHON_CMD 定义行。
    """
    skip = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return skip

    def _add_range(node):
        for line_number in range(getattr(node, "lineno", 0),
                                 getattr(node, "end_lineno", 0) + 1):
            skip.add(line_number)

    def _docstring_lines(node):
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr):
            val = body[0].value
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                _add_range(body[0])

    def _cond_side(test):
        """平台条件语义：返回 'posix' | 'windows' | None。

        覆盖 Name（IS_POSIX）、Attribute（sysops.IS_WINDOWS）、
        Not 取反与 and/or 组合。
        """
        if isinstance(test, ast.Name):
            return {"IS_POSIX": "posix", "IS_MAC": "posix",
                    "IS_WINDOWS": "windows", "IS_WIN": "windows"}.get(test.id)
        if isinstance(test, ast.Attribute):
            return {"IS_POSIX": "posix", "IS_MAC": "posix",
                    "IS_WINDOWS": "windows", "IS_WIN": "windows"}.get(
                        test.attr)
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            side = _cond_side(test.operand)
            return {"posix": "windows", "windows": "posix"}.get(side)
        names = {n.id for n in ast.walk(test) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(test)
                 if isinstance(n, ast.Attribute)}
        if names & {"IS_POSIX", "IS_MAC"} or attrs & {"IS_POSIX", "IS_MAC"}:
            return "posix"
        if names & {"IS_WINDOWS", "IS_WIN"} or attrs & {"IS_WINDOWS", "IS_WIN"}:
            return "windows"
        return None

    def _all_paths_return(stmts):
        """语句序列的所有路径都以 return/raise 结束（无 fall-through）。"""
        if not stmts:
            return False
        last = stmts[-1]
        if isinstance(last, (ast.Return, ast.Raise)):
            return True
        if isinstance(last, ast.If) and last.orelse:
            return (_all_paths_return(last.body)
                    and _all_paths_return(last.orelse))
        return False

    def _collect_tail(stmts):
        """处理兄弟语句序列：识别「全 return 的平台 if」并把其后的
        兄弟语句（隐式 else / POSIX 尾随代码）加入跳过。"""
        for idx, stmt in enumerate(stmts):
            if isinstance(stmt, ast.If) and not stmt.orelse:
                side = _cond_side(stmt.test)
                if side and _all_paths_return(stmt.body):
                    _add_range(stmt)
                    for tail in stmts[idx + 1:]:
                        _add_range(tail)
                    return  # 其后全部跳过，无需继续
            for name in ("body", "orelse", "finalbody"):
                children = getattr(stmt, name, None)
                if (isinstance(children, list) and children
                        and any(isinstance(c, ast.If) for c in children)):
                    _collect_tail(children)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            _docstring_lines(node)
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                _collect_tail(list(node.body))
        if isinstance(node, ast.If):
            side = _cond_side(node.test)
            if side == "posix":
                for stmt in node.body:
                    _add_range(stmt)
            elif side == "windows":
                for stmt in node.orelse:
                    _add_range(stmt)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.endswith(("_posix", "_windows")):
                _add_range(node)
    for i, line in enumerate(src.splitlines(), 1):
        if "PYTHON_CMD" in line:
            skip.add(i)
    return skip


def scan_py(path, patterns, rel=None):
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    skip = _platform_block_lines_py(src)
    # rel 仅用于白名单；None（如临时文件）时不匹配白名单，
    # 也不计算 relpath（避免跨盘符 ValueError）。
    allow = ALLOWLIST.get(rel, []) if rel else []
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
                    line = lines[tok.start[0] - 1]
                    if any(a in line for a in allow):
                        break
                    hits.append((tok.start[0], label, line.strip()[:100]))
                    break
    except (tokenize.TokenError, IndentationError):
        pass
    return hits


def _js_block_skip_lines(lines):
    """JS 平台分支大括号块（if (IS_MAC) { ... } 等）整体跳过。

    进入条件：行内含 IS_MAC/IS_WINDOWS/MOD_KEY 且行内 `{` 计数大于 `}`。
    此后按大括号平衡维护深度，depth>0 的行跳过。
    """
    skip = set()
    depth = 0
    active = False
    for i, raw in enumerate(lines, 1):
        if not active:
            if re.search(r"IS_MAC|IS_WINDOWS|MOD_KEY", raw) and "{" in raw:
                active = True
                depth = raw.count("{") - raw.count("}")
                if depth <= 0:
                    active = False
                    continue
                skip.add(i)
                continue
            continue
        skip.add(i)
        depth += raw.count("{") - raw.count("}")
        if depth <= 0:
            active = False
    return skip


def _html_skip(raw):
    s = raw.strip()
    if not s:
        return True
    if s.startswith(("<!--", "<!DOCTYPE", "<html", "<head", "<body",
                     "</", "<meta", "<link", "<script", "<style")):
        return True
    if "data-mod-key" in s:
        return True
    if "⌘" in s and "Ctrl" in s:
        return True  # Ctrl/⌘ 平台对照文案
    return False


def scan_frontend(path, patterns, rel=None):
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    rel = rel or os.path.basename(path)
    allow = ALLOWLIST.get(rel, [])
    js_skip = _js_block_skip_lines(lines) if path.endswith(".js") else set()
    hits = []
    for i, raw in enumerate(lines, 1):
        if i in js_skip:
            continue
        if path.endswith(".html") and _html_skip(raw):
            continue
        if path.endswith(".js") and (raw.strip().startswith(("//", "/*", "*"))
                                     or "Ctrl/⌘" in raw
                                     or re.search(r"IS_MAC|IS_WINDOWS|MOD_KEY",
                                                  raw)):
            continue
        if any(a in raw for a in allow):
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
