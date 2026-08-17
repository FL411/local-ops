#!/usr/bin/env python3
"""总控台统一项目检查。

默认执行语法、结构、生成文件和测试检查。本脚本不修改项目文件；
--release 额外检查 Git 发布边界，但不代替 RELEASE_CHECKLIST.md 的人工验收。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
INFO_PLIST = ROOT / "总控台.app" / "Contents" / "Info.plist"
WINDOWS_RUNTIME_FILES = (
    "sysops.py",
    "tray.py",
    "start.bat",
    "launcher_check.py",
    "requirements-runtime-win.txt",
    "LocalOpsConsole.exe",
    "console.ico",
)
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class CheckError(RuntimeError):
    """预期的项目检查失败。"""


class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def check(self, label: str, fn) -> None:
        try:
            detail = fn()
        except CheckError as exc:
            self.errors.append(f"{label}: {exc}")
            print(f"[FAIL] {label}: {exc}")
        except Exception as exc:  # 检查器自身也必须显式失败
            self.errors.append(f"{label}: {type(exc).__name__}: {exc}")
            print(f"[FAIL] {label}: {type(exc).__name__}: {exc}")
        else:
            self.passed += 1
            suffix = f" — {detail}" if detail else ""
            print(f"[ OK ] {label}{suffix}")

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        print(f"[WARN] {message}")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckError(message)


def command_output(args: list[str], cwd: Path = ROOT) -> str:
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CheckError(f"缺少命令 {args[0]!r}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"命令超时: {' '.join(args)}") from exc
    output = "\n".join(
        part.rstrip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    if completed.returncode != 0:
        tail = "\n".join(output.splitlines()[-30:])
        raise CheckError(
            f"命令退出 {completed.returncode}: {' '.join(args)}"
            + (f"\n{tail}" if tail else "")
        )
    return output


def load_sysops_module():
    """从项目根目录加载平台层，兼容模块导入和脚本直接执行。"""
    root_text = str(ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    import sysops

    return sysops


def check_required_files() -> str:
    required = (
        "VERSION",
        "README.md",
        "CHANGELOG.md",
        "LICENSE",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "ASSET_PROVENANCE.md",
        "THIRD_PARTY_NOTICES.md",
        "RELEASE_CHECKLIST.md",
        ".github/workflows/ci.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/PULL_REQUEST_TEMPLATE.md",
        "requirements-dev.txt",
        "Makefile",
        "server.py",
        "start.command",
        *WINDOWS_RUNTIME_FILES,
        "tests/test_server.py",
        "docs/screenshots/ops-launchpad.jpg",
        "docs/screenshots/ops-services.jpg",
        "static/index.html",
        "static/app.js",
        "总控台.app/Contents/Info.plist",
        "总控台.app/Contents/MacOS/launcher",
    )
    missing = [name for name in required if not (ROOT / name).is_file()]
    require(not missing, "缺少必要文件: " + ", ".join(missing))
    return f"{len(required)} 个必要文件"


def check_asset_provenance() -> str:
    path = ROOT / "ASSET_PROVENANCE.md"
    require(path.is_file(), "ASSET_PROVENANCE.md 不存在")
    text = path.read_text(encoding="utf-8")
    tracked = [
        item
        for folder in (STATIC / "assets", STATIC / "fonts")
        for item in sorted(folder.rglob("*"))
        if item.is_file()
    ]
    tracked.append(ROOT / "总控台.app" / "Contents" / "Resources" / "AppIcon.icns")
    missing = [
        item.relative_to(ROOT).as_posix()
        for item in tracked
        if item.relative_to(ROOT).as_posix() not in text
    ]
    require(not missing, "素材台账缺少文件: " + ", ".join(missing))
    stale = [
        item.relative_to(ROOT).as_posix()
        for item in tracked
        if hashlib.sha256(item.read_bytes()).hexdigest() not in text
    ]
    require(not stale, "素材台账缺少当前 SHA-256: " + ", ".join(stale))
    for status in ("CLEARED", "REVIEW_REQUIRED", "BLOCKED", "TO_REPLACE"):
        require(f"`{status}`" in text, f"素材台账缺少状态定义: {status}")
    return f"{len(tracked)} 个素材文件已登记"


def asset_release_statuses() -> list[tuple[str, str]]:
    text = (ROOT / "ASSET_PROVENANCE.md").read_text(encoding="utf-8")
    section = "未命名素材"
    statuses: list[tuple[str, str]] = []
    for line in text.splitlines():
        if line.startswith("### "):
            section = line[4:].strip()
            continue
        match = re.fullmatch(
            r"- 状态[：:]\s*`(CLEARED|REVIEW_REQUIRED|BLOCKED|TO_REPLACE)`\s*",
            line,
        )
        if match:
            statuses.append((section, match.group(1)))
    return statuses


def check_asset_release_status() -> str:
    statuses = asset_release_statuses()
    require(bool(statuses), "素材台账没有可核验的状态记录")
    blockers = [
        f"{section}={status}"
        for section, status in statuses
        if status in {"BLOCKED", "TO_REPLACE"}
    ]
    require(
        not blockers,
        "公开发行仍包含未清权或待替换素材: " + ", ".join(blockers),
    )
    return f"{len(statuses)} 项素材无 BLOCKED/TO_REPLACE"


def read_version() -> str:
    path = ROOT / "VERSION"
    require(path.is_file(), "VERSION 不存在")
    value = path.read_text(encoding="utf-8").strip()
    require(bool(SEMVER_RE.fullmatch(value)), f"VERSION 不是完整 SemVer: {value!r}")
    return value


def check_version() -> str:
    version = read_version()
    with INFO_PLIST.open("rb") as handle:
        info = plistlib.load(handle)
    short = str(info.get("CFBundleShortVersionString", "")).strip()
    build = str(info.get("CFBundleVersion", "")).strip()
    version_major_minor = tuple(version.split("-", 1)[0].split(".")[:2])
    short_parts = tuple(short.split(".")[:2])
    require(
        len(short_parts) == 2 and short_parts == version_major_minor,
        f"Info.plist 版本 {short!r} 与 VERSION {version!r} 的 major.minor 不一致",
    )
    require(build.isdigit() and int(build) > 0, "CFBundleVersion 必须是正整数")
    require(info.get("CFBundleExecutable") == "launcher", "CFBundleExecutable 不是 launcher")
    return f"VERSION={version}, app={short} ({build})"


def check_python_syntax() -> str:
    paths = [ROOT / "server.py"]
    paths.extend(sorted((ROOT / "tools").glob("*.py")))
    paths.extend(sorted((ROOT / "tests").glob("test_*.py")))
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8")
            ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as exc:
            raise CheckError(f"{path.relative_to(ROOT)}: {exc}") from exc
    return f"{len(paths)} 个 Python 文件"


def check_javascript_syntax() -> str:
    node = shutil.which("node")
    require(bool(node), "未找到 node，无法做 JavaScript 语法检查")
    paths = sorted(STATIC.rglob("*.js"))
    require(bool(paths), "没有发现 JavaScript 文件")
    for path in paths:
        try:
            command_output([node, "--check", str(path)])
        except CheckError as exc:
            raise CheckError(f"{path.relative_to(ROOT)}\n{exc}") from exc
    return f"{len(paths)} 个 JavaScript 文件"


def strip_javascript_literals_and_comments(source: str) -> str:
    """移除字符串/注释内容但保留换行和长度，供轻量静态绑定检查使用。"""
    output: list[str] = []
    index = 0
    state = "code"
    quote = ""
    while index < len(source):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if state == "code":
            if char == "/" and following == "/":
                output.extend((" ", " "))
                index += 2
                state = "line-comment"
                continue
            if char == "/" and following == "*":
                output.extend((" ", " "))
                index += 2
                state = "block-comment"
                continue
            if char == "/":
                previous = next(
                    (item for item in reversed(output) if not item.isspace()), "")
                if previous in ("(", ",", "=", ":", "[", "!", "?", "{", ";"):
                    output.append(" ")
                    index += 1
                    state = "regex"
                    continue
            if char in ("'", '"', "`"):
                quote = char
                output.append(" ")
                index += 1
                state = "string"
                continue
            output.append(char)
            index += 1
            continue
        if state == "line-comment":
            output.append("\n" if char == "\n" else " ")
            index += 1
            if char == "\n":
                state = "code"
            continue
        if state == "block-comment":
            if char == "*" and following == "/":
                output.extend((" ", " "))
                index += 2
                state = "code"
            else:
                output.append("\n" if char == "\n" else " ")
                index += 1
            continue
        if state in ("regex", "regex-class"):
            if char == "\\":
                output.append(" ")
                index += 1
                if index < len(source):
                    output.append("\n" if source[index] == "\n" else " ")
                    index += 1
                continue
            output.append("\n" if char == "\n" else " ")
            index += 1
            if char == "\n":
                state = "code"
            elif state == "regex" and char == "[":
                state = "regex-class"
            elif state == "regex-class" and char == "]":
                state = "regex"
            elif state == "regex" and char == "/":
                state = "code"
            continue
        # string / template literal
        if char == "\\":
            output.append(" ")
            index += 1
            if index < len(source):
                output.append("\n" if source[index] == "\n" else " ")
                index += 1
            continue
        output.append("\n" if char == "\n" else " ")
        index += 1
        if char == quote:
            state = "code"
    return "".join(output)


def javascript_exported_callables(source: str) -> set[str]:
    """提取 `export function` 与导出的箭头函数名称。"""
    names = set(re.findall(
        r"^\s*export\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
        source, flags=re.M,
    ))
    names.update(re.findall(
        r"^\s*export\s+const\s+([A-Za-z_$][\w$]*)\s*=\s*"
        r"(?:async\s*)?(?:\([^)\n]*\)|[A-Za-z_$][\w$]*)\s*=>",
        source, flags=re.M,
    ))
    return names


def javascript_imported_bindings(source: str) -> set[str]:
    """提取命名导入在当前模块中的本地名称（支持 `as`）。"""
    names: set[str] = set()
    for body in re.findall(
            r"\bimport\s*\{(.*?)\}\s*from\s*['\"][^'\"]+['\"]",
            source, flags=re.S):
        for item in body.split(","):
            item = re.sub(r"/\*.*?\*/|//[^\n]*", "", item, flags=re.S).strip()
            if not item:
                continue
            local = re.split(r"\s+as\s+", item)[-1].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", local):
                names.add(local)
    return names


def find_unbound_shared_calls(source: str, shared_callables: set[str]) -> list[str]:
    """找出调用了 core 公共函数、但本模块既未导入也未声明的名称。"""
    scrubbed = strip_javascript_literals_and_comments(source)
    bound = javascript_imported_bindings(source)
    bound.update(re.findall(
        r"\b(?:function|class)\s+([A-Za-z_$][\w$]*)", scrubbed))
    bound.update(re.findall(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)", scrubbed))
    missing = []
    for name in sorted(shared_callables):
        call = re.compile(
            r"(?<![\w$.])" + re.escape(name) + r"\s*\(")
        if name not in bound and call.search(scrubbed):
            missing.append(name)
    return missing


def check_javascript_bindings() -> str:
    """捕获语法检查发现不了的公共函数漏导入（例如点击后才触发的 del）。"""
    core_path = STATIC / "js" / "core.js"
    require(core_path.is_file(), "static/js/core.js 不存在")
    shared = javascript_exported_callables(
        core_path.read_text(encoding="utf-8"))
    require(bool(shared), "没有识别到 core.js 的公共函数")
    failures: list[str] = []
    checked = 0
    for path in sorted(STATIC.rglob("*.js")):
        if path == core_path:
            continue
        source = path.read_text(encoding="utf-8")
        missing = find_unbound_shared_calls(source, shared)
        if missing:
            failures.append(
                f"{path.relative_to(ROOT)} 缺少绑定: {', '.join(missing)}")
        checked += 1
    require(not failures, "\n".join(failures))
    return f"{checked} 个模块，{len(shared)} 个公共可调用导出"


def check_shell_and_plist() -> str:
    if os.name == "nt":
        missing = [name for name in WINDOWS_RUNTIME_FILES
                   if not (ROOT / name).is_file()]
        require(not missing, "缺少 Windows 启动文件: " + ", ".join(missing))
        launcher = (ROOT / "start.bat").read_text(
            encoding="utf-8", errors="replace")
        require("launcher_check.py" in launcher,
                "start.bat 未调用 launcher_check.py")
        return f"Windows {len(WINDOWS_RUNTIME_FILES)} 个启动/运行时文件"
    if sys.platform != "darwin":
        return "当前平台不执行 macOS 启动器语法检查"
    shell_files = (
        ROOT / "start.command",
        ROOT / "总控台.app" / "Contents" / "MacOS" / "launcher",
    )
    for path in shell_files:
        command_output(["/bin/bash", "-n", str(path)])
        require(os.access(path, os.X_OK), f"{path.relative_to(ROOT)} 没有可执行权限")
    plutil = shutil.which("plutil") or "/usr/bin/plutil"
    command_output([plutil, "-lint", str(INFO_PLIST)])
    return "2 个启动脚本 + Info.plist"


def check_windows_acl_smoke() -> str:
    """在临时路径上验证 Windows 私有 DACL 的实际读写能力。"""
    if os.name != "nt":
        return "当前平台跳过 Windows ACL 冒烟测试"
    sysops = load_sysops_module()
    require(sysops.IS_WINDOWS, "sysops 未识别当前 Windows 平台")
    with tempfile.TemporaryDirectory(
        prefix="console-acl-smoke-", ignore_cleanup_errors=True
    ) as raw:
        private_dir = Path(raw) / "private"
        private_dir.mkdir()
        sysops.protect_private_directory(str(private_dir))
        directory_aces = sysops.windows_acl_ace_count(str(private_dir))
        require(directory_aces is not None and directory_aces > 0,
                "私有目录 DACL 为空")
        require(sysops.path_owned_by_current_user(str(private_dir)),
                "私有目录不属于当前用户")

        probe = private_dir / "acl-probe.bin"
        probe.write_bytes(b"before")
        sysops.protect_private_file(str(probe))
        file_aces = sysops.windows_acl_ace_count(str(probe))
        require(file_aces is not None and file_aces > 0,
                "私有文件 DACL 为空")
        require(sysops.path_owned_by_current_user(str(probe)),
                "私有文件不属于当前用户")
        probe.write_bytes(b"after")
        require(probe.read_bytes() == b"after",
                "当前用户无法读写受保护文件")
        probe.unlink()
    return "Windows 私有目录/文件 DACL 非空且当前用户可读写"


def check_dev_requirements() -> str:
    path = ROOT / "requirements-dev.txt"
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    require(bool(lines), "requirements-dev.txt 为空")
    unpinned = [
        line for line in lines
        if not re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+", line)
    ]
    require(not unpinned, "开发依赖必须精确锁定: " + ", ".join(unpinned))
    return f"{len(lines)} 个锁定依赖"


def check_themes() -> str:
    theme_dir = STATIC / "themes"
    manifests = sorted(theme_dir.glob("*.json"))
    require(bool(manifests), "没有发现主题清单")
    ids: set[str] = set()
    for manifest in manifests:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckError(f"{manifest.name}: {exc}") from exc
        require(isinstance(data, dict), f"{manifest.name} 根节点必须是对象")
        theme_id = data.get("id")
        require(theme_id == manifest.stem, f"{manifest.name} 的 id 与文件名不一致")
        require(theme_id not in ids, f"重复主题 id: {theme_id}")
        ids.add(theme_id)
        for key in ("name", "author", "desc"):
            require(isinstance(data.get(key), str) and data[key].strip(), f"{manifest.name} 缺少 {key}")
        colors = data.get("colors")
        require(isinstance(colors, list) and colors, f"{manifest.name} 缺少 colors")
        css = manifest.with_suffix(".css")
        require(css.is_file() and css.stat().st_size > 0, f"缺少主题 CSS: {css.name}")
    require("ops" in ids, "缺少默认主题 ops")
    orphan_css = {
        path.stem for path in theme_dir.glob("*.css")
        if path.stem not in ids
    }
    require(not orphan_css, "存在未注册主题 CSS: " + ", ".join(sorted(orphan_css)))
    return ", ".join(sorted(ids))


def check_static_references() -> str:
    files = [STATIC / "index.html"]
    files.extend(
        manifest.with_suffix(".css")
        for manifest in sorted((STATIC / "themes").glob("*.json"))
    )
    refs: set[str] = set()
    attr_re = re.compile(r"(?:src|href)\s*=\s*['\"](/[^'\"?#]+)", re.I)
    url_re = re.compile(r"url\(\s*['\"]?(/[^'\")?#\s]+)", re.I)
    for path in files:
        text = path.read_text(encoding="utf-8")
        refs.update(attr_re.findall(text))
        refs.update(url_re.findall(text))

    static_prefixes = ("/assets/", "/fonts/", "/js/", "/themes/")
    static_exact = {"/app.js", "/icons.js"}
    checked = 0
    for ref in sorted(refs):
        if ref in static_exact or ref.startswith(static_prefixes):
            relative = ref.lstrip("/")
            require(".." not in Path(relative).parts, f"静态引用包含路径穿越: {ref}")
            require((STATIC / relative).is_file(), f"静态引用不存在: {ref}")
            checked += 1

    import_re = re.compile(
        r"(?:\bfrom\s*|\bimport\s*)['\"]([^'\"]+\.js)['\"]"
    )
    module_count = 0
    for path in sorted(STATIC.rglob("*.js")):
        text = path.read_text(encoding="utf-8")
        for specifier in import_re.findall(text):
            if specifier.startswith("."):
                target = (path.parent / specifier).resolve()
                try:
                    target.relative_to(STATIC.resolve())
                except ValueError as exc:
                    raise CheckError(f"{path.relative_to(ROOT)} 导入越界: {specifier}") from exc
                require(target.is_file(), f"{path.relative_to(ROOT)} 导入不存在: {specifier}")
                module_count += 1
    return f"{checked} 个静态资源 + {module_count} 个本地模块导入"


def expected_icons_js() -> str:
    icons: dict[str, str] = {}
    for path in sorted((STATIC / "icons").glob("*.svg")):
        svg = path.read_text(encoding="utf-8")
        svg = re.sub(r"<!--.*?-->", "", svg, flags=re.S)
        svg = re.sub(r'\s+class="[^"]*"', "", svg)
        svg = re.sub(r'\s*(?:width|height)="24"', "", svg)
        svg = svg.replace('stroke-width="2"', 'stroke-width="1.75"')
        svg = re.sub(r"\s*\n\s*", " ", svg).strip()
        require(svg.startswith("<svg "), f"无效 SVG: {path.name}")
        icons[path.stem] = svg
    require(bool(icons), "没有发现 SVG 图标")
    return (
        "/* Lucide 图标库（vendored, lucide-static, ISC）— 运行时零网络。\n"
        "   由 tools/gen_icons.py 生成，勿手改。 */\n"
        "window.LUCIDE = " + json.dumps(icons, ensure_ascii=False) + ";\n"
    )


def check_generated_icons() -> str:
    actual_path = STATIC / "icons.js"
    require(actual_path.is_file(), "static/icons.js 不存在")
    expected = expected_icons_js()
    actual = actual_path.read_text(encoding="utf-8")
    require(
        actual == expected,
        "static/icons.js 与 SVG 源文件不同步，请运行 make generate-icons",
    )
    count = len(list((STATIC / "icons").glob("*.svg")))
    return f"{count} 个 Lucide 图标"


def check_tests() -> str:
    args = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-p",
        "test_*.py",
        "-v",
    ]
    output = command_output(args)
    match = re.search(r"Ran\s+(\d+)\s+tests?\b", output)
    require(match is not None, "无法确认 unittest 实际运行的测试数")
    count = int(match.group(1))
    require(count > 0, "测试发现结果为 0")
    summary = "\n".join(output.splitlines()[-4:])
    if summary:
        print(summary)
    return f"{count} 个测试"


def check_javascript_tests() -> str:
    """运行前端纯函数行为测试（node --test，零依赖）。"""
    node = shutil.which("node")
    require(bool(node), "未找到 node，无法运行 JavaScript 测试")
    files = sorted(str(path) for path in (ROOT / "tests" / "js").glob("*.test.mjs"))
    require(bool(files), "tests/js/ 下没有 .test.mjs 测试文件")
    output = command_output([node, "--test", *files])
    match = re.search(r"# (pass)\s+(\d+)", output)
    require(match is not None, "无法确认 node --test 结果")
    passed = int(match.group(2))
    require("# fail" not in output or re.search(r"# fail\s+0$", output, re.M),
            "JavaScript 测试存在失败项")
    return f"{passed} 个测试"


def check_release_git() -> str:
    git = shutil.which("git")
    require(bool(git), "未找到 git")
    top = command_output([git, "rev-parse", "--show-toplevel"])
    require(Path(top.strip()).resolve() == ROOT.resolve(), "项目根目录不是 Git 工作区根")
    status = command_output([git, "status", "--porcelain", "--untracked-files=all"])
    if status.strip():
        status_lines = status.splitlines()
        preview = "\n".join(status_lines[:30])
        if len(status_lines) > 30:
            preview += f"\n... 其余 {len(status_lines) - 30} 项已省略"
        raise CheckError("Git 工作区不干净:\n" + preview)
    tracked_raw = subprocess.run(
        [git, "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    tracked = [item.decode("utf-8", "replace") for item in tracked_raw.split(b"\0") if item]
    forbidden: list[str] = []
    for name in tracked:
        parts = Path(name).parts
        if (
            (parts and parts[0] in {"data", "tmp", "build", "dist", "release"})
            or "__pycache__" in parts
            or name.endswith((".pyc", ".pyo"))
            or Path(name).name in {".DS_Store", ".coverage"}
        ):
            forbidden.append(name)
    require(not forbidden, "Git 跟踪了运行/临时文件: " + ", ".join(forbidden))
    return f"工作区干净，{len(tracked)} 个跟踪文件"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检查总控台项目")
    parser.add_argument("--skip-tests", action="store_true", help="只检查语法/结构，不运行测试")
    parser.add_argument("--release", action="store_true", help="额外检查 Git 发布边界")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = Report()
    checks = [
        ("必要文件", check_required_files),
        ("版本一致性", check_version),
        ("Python 语法", check_python_syntax),
        ("JavaScript 语法", check_javascript_syntax),
        ("JavaScript 模块绑定", check_javascript_bindings),
        ("平台启动器", check_shell_and_plist),
        ("开发依赖锁定", check_dev_requirements),
        ("素材来源台账", check_asset_provenance),
        ("主题注册表", check_themes),
        ("静态资源与模块", check_static_references),
        ("生成图标同步", check_generated_icons),
    ]
    for label, fn in checks:
        report.check(label, fn)
    if not args.skip_tests:
        report.check("后端测试", check_tests)
        report.check("JavaScript 行为测试", check_javascript_tests)
    if args.release:
        report.check("Windows ACL 冒烟", check_windows_acl_smoke)
        report.check("素材发布状态", check_asset_release_status)
        report.check("Git 发布边界", check_release_git)
        reviews = [
            section
            for section, status in asset_release_statuses()
            if status == "REVIEW_REQUIRED"
        ]
        if reviews:
            report.warn(
                f"素材台账仍有 {len(reviews)} 项 REVIEW_REQUIRED；"
                "发布负责人必须形成书面结论: " + ", ".join(reviews)
            )

    print()
    if report.errors:
        print(f"检查失败：{len(report.errors)} 项失败，{report.passed} 项通过。")
        return 1
    print(f"检查通过：{report.passed} 项通过，{len(report.warnings)} 项人工提醒。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
