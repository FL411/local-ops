#!/usr/bin/env python3
"""Build and verify a clean, reproducible source/runtime release archive.

The archive uses an explicit allowlist and intentionally excludes user state,
credentials, caches, temporary assets, Git metadata, and prior build output.
ZIP metadata is normalized so identical source bytes produce identical archives.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import zipfile


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIST = ROOT / "dist"
REQUIRED_LICENSES = (
    "licenses/Geist-OFL-1.1.txt",
    "licenses/Lucide-LICENSE.txt",
)
REQUIRED_PROJECT_DOCS = (
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "ASSET_PROVENANCE.md",
    "THIRD_PARTY_NOTICES.md",
    "RELEASE_CHECKLIST.md",
)
INCLUDE = (
    "VERSION",
    *REQUIRED_PROJECT_DOCS,
    "licenses",
    *REQUIRED_LICENSES,
    "server.py",
    "sysops.py",
    "tray.py",
    "start.command",
    "start.bat",
    "launcher_check.py",
    "requirements-runtime-win.txt",
    "LocalOpsConsole.exe",
    "console.ico",
    "总控台.app",
    "static",
    "docs",
    "tests",
    "tools",
    "requirements-dev.txt",
    "Makefile",
)
EXCLUDED_PARTS = {
    ".git",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "data",
    "dist",
    "htmlcov",
    "node_modules",
    "release",
    "tmp",
    "venv",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log", ".swo", ".swp", ".tmp"}
EXCLUDED_NAMES = {".coverage", ".ds_store", "coverage.xml"}
SENSITIVE_NAMES = {
    ".env",
    ".envrc",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".secrets",
    "auth.json",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "service-account.json",
    "service_account.json",
    "secrets.json",
    "token.json",
}
SENSITIVE_SUFFIXES = {
    ".bak",
    ".db",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
    ".sqlite",
    ".sqlite3",
}
SAFE_ENV_SUFFIXES = {".example", ".sample", ".template"}
EXECUTABLE_FILES = {
    "start.command",
    "tools/build_release.py",
    "总控台.app/Contents/MacOS/launcher",
}
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
POSIX_HOME_RE = re.compile(
    rb"/(?:Users|home)/([^/\\\x00\r\n\t \"'<>`]+)"
    rb"(?=/|[\x00\r\n\t \"'<>`]|$)"
)
WINDOWS_HOME_RE = re.compile(
    rb"(?i)[A-Z]:[\\/]+Users[\\/]+"
    rb"([^/\\\x00\r\n\t \"'<>`]+)(?=[\\/]|[\x00\r\n\t \"'<>`]|$)"
)
ESCAPED_POSIX_HOME_RE = re.compile(
    rb"\\/(?:Users|home)\\/([^/\\\x00\r\n\t \"'<>`]+)"
    rb"(?=\\/|[\x00\r\n\t \"'<>`]|$)"
)
PERCENT_HOME_RE = re.compile(
    rb"(?i)%2f(?:Users|home)%2f([^%/\\\x00\r\n\t \"'<>`]+)"
    rb"(?=%2f|[\x00\r\n\t \"'<>`]|$)"
)
PLACEHOLDER_USERS = {
    b"example",
    b"name",
    b"username",
    b"user",
}
PRIVATE_KEY_MARKERS = (
    b"-----BEGIN " + b"PRIVATE KEY-----",
    b"-----BEGIN " + b"ENCRYPTED PRIVATE KEY-----",
    b"-----BEGIN " + b"RSA PRIVATE KEY-----",
    b"-----BEGIN " + b"EC PRIVATE KEY-----",
    b"-----BEGIN " + b"DSA PRIVATE KEY-----",
    b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----",
    b"-----BEGIN PGP " + b"PRIVATE KEY BLOCK-----",
)
SECRET_PATTERNS = (
    (
        re.compile(rb"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
        "AWS access key",
    ),
    (re.compile(rb"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{36,255}"), "GitHub token"),
    (re.compile(rb"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"), "OpenAI API key"),
)
ZIP_MIN_EPOCH = 315_532_800  # 1980-01-01 00:00:00 UTC
ZIP_MAX_EPOCH = 4_354_819_199  # 2107-12-31 23:59:59 UTC


@dataclass(frozen=True)
class ReleaseEntry:
    """One validated, immutable-in-memory release member."""

    source: Path
    relative: Path
    data: bytes


def fail(message: str) -> None:
    raise SystemExit(message)


def version() -> str:
    path = ROOT / "VERSION"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        fail(f"无法读取 VERSION：{exc}")
    match = SEMVER_RE.fullmatch(value)
    if not match:
        fail(f"VERSION 不是完整 SemVer：{value!r}")
    prerelease = match.group(4)
    if prerelease and any(
        identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0")
        for identifier in prerelease.split(".")
    ):
        fail(f"VERSION 不是完整 SemVer：{value!r}")
    return value


def has_excluded_part(relative: Path) -> bool:
    return any(part.casefold() in EXCLUDED_PARTS for part in relative.parts)


def is_sensitive_path(relative: Path) -> bool:
    lowered = relative.name.casefold()
    if lowered in SENSITIVE_NAMES or Path(lowered).suffix in SENSITIVE_SUFFIXES:
        return True
    if lowered.startswith(".env."):
        return not any(lowered.endswith(suffix) for suffix in SAFE_ENV_SUFFIXES)
    return any(part.casefold() in {".aws", ".gnupg", ".ssh"} for part in relative.parts)


def validate_relative_path(relative: Path) -> None:
    text = relative.as_posix()
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        fail(f"发行路径不安全：{text}")
    if "\\" in text or "\x00" in text or any(ord(char) < 32 for char in text):
        fail(f"发行路径包含不安全字符：{text}")


def iter_release_files() -> list[Path]:
    files: list[Path] = []
    missing: list[str] = []
    unsafe_links: list[str] = []
    unsupported: list[str] = []
    sensitive: list[str] = []
    for name in INCLUDE:
        path = ROOT / name
        if not path.exists():
            missing.append(name)
            continue
        if path.is_file() or path.is_symlink():
            candidates = [path]
        elif path.is_dir():
            candidates = path.rglob("*")
        else:
            unsupported.append(name)
            continue
        for candidate in candidates:
            relative = candidate.relative_to(ROOT)
            validate_relative_path(relative)
            if has_excluded_part(relative):
                continue
            if candidate.is_symlink():
                unsafe_links.append(relative.as_posix())
                continue
            if candidate.is_dir():
                continue
            if not candidate.is_file():
                unsupported.append(relative.as_posix())
                continue
            if is_sensitive_path(relative):
                sensitive.append(relative.as_posix())
                continue
            if candidate.suffix.casefold() in EXCLUDED_SUFFIXES:
                continue
            lowered_name = candidate.name.casefold()
            if (
                lowered_name in EXCLUDED_NAMES
                or lowered_name.startswith(("._", ".coverage."))
                or lowered_name.endswith("~")
            ):
                continue
            files.append(candidate)
    if missing:
        fail("发行文件缺失：" + "、".join(missing))
    if unsafe_links:
        fail("发行来源包含符号链接：" + "、".join(sorted(unsafe_links)))
    if unsupported:
        fail("发行来源包含非普通文件：" + "、".join(sorted(unsupported)))
    if sensitive:
        fail("发行来源包含敏感文件：" + "、".join(sorted(sensitive)))
    return sorted(set(files), key=lambda item: item.relative_to(ROOT).as_posix())


def archive_timestamp() -> tuple[int, int, int, int, int, int]:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    try:
        epoch = int(raw, 10) if raw is not None else 1_704_067_200  # 2024-01-01 UTC
    except ValueError:
        fail("SOURCE_DATE_EPOCH 必须是整数秒")
    epoch = min(max(epoch, ZIP_MIN_EPOCH), ZIP_MAX_EPOCH)
    value = list(time.gmtime(epoch)[:6])
    value[5] -= value[5] % 2  # ZIP timestamps have two-second resolution.
    return tuple(value)  # type: ignore[return-value]


def find_path_leak(data: bytes) -> str | None:
    home = os.fsencode(str(Path.home().resolve()))
    root = os.fsencode(str(ROOT.resolve()))
    for marker in (home, root):
        if marker and marker in data:
            return os.fsdecode(marker)

    for pattern, prefix, separator in (
        (POSIX_HOME_RE, "/Users|/home", "/"),
        (WINDOWS_HOME_RE, "drive:/Users", "\\"),
        (ESCAPED_POSIX_HOME_RE, "escaped:/Users|/home", "/"),
        (PERCENT_HOME_RE, "encoded:/Users|/home", "/"),
    ):
        for match in pattern.finditer(data):
            username = match.group(1).lower()
            if username not in PLACEHOLDER_USERS:
                return f"{prefix}/{os.fsdecode(match.group(1))}{separator}"
    return None


def find_secret_marker(data: bytes) -> str | None:
    for marker in PRIVATE_KEY_MARKERS:
        if marker in data:
            return marker.decode("ascii", "replace")
    for pattern, label in SECRET_PATTERNS:
        if pattern.search(data):
            return label
    return None


def validate_payload(files: list[Path]) -> list[ReleaseEntry]:
    path_leaks: list[str] = []
    secret_leaks: list[str] = []
    entries: list[ReleaseEntry] = []
    for path in files:
        try:
            relative = path.relative_to(ROOT)
        except ValueError:
            fail(f"发行文件越出项目根目录：{path}")
        validate_relative_path(relative)
        if has_excluded_part(relative):
            fail("发行包包含禁止路径：" + relative.as_posix())
        if is_sensitive_path(relative):
            fail("发行包包含敏感文件：" + relative.as_posix())
        try:
            data = path.read_bytes()
        except OSError as exc:
            fail(f"无法读取发行文件 {relative.as_posix()}：{exc}")
        leaked_path = find_path_leak(data)
        if leaked_path:
            path_leaks.append(f"{relative.as_posix()} ({leaked_path})")
        secret_marker = find_secret_marker(data)
        if secret_marker:
            secret_leaks.append(f"{relative.as_posix()} ({secret_marker})")
        entries.append(ReleaseEntry(path, relative, data))
    if path_leaks:
        fail("发现用户绝对路径：" + "、".join(path_leaks))
    if secret_leaks:
        fail("发现敏感密钥或令牌内容：" + "、".join(secret_leaks))
    return sorted(entries, key=lambda entry: entry.relative.as_posix())


def archive_prefix(release_version: str) -> str:
    return f"总控台-{release_version}"


def archive_mode(relative: Path) -> int:
    return 0o755 if relative.as_posix() in EXECUTABLE_FILES else 0o644


def member_name(relative: Path, release_version: str) -> str:
    return f"{archive_prefix(release_version)}/{relative.as_posix()}"


def zip_info(relative: Path, release_version: str, timestamp: tuple[int, int, int, int, int, int]) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(member_name(relative, release_version), timestamp)
    info.create_system = 3  # Unix; makes external_attr portable across build hosts.
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = (stat.S_IFREG | archive_mode(relative)) << 16
    info.extra = b""
    info.comment = b""
    return info


def canonical_archive_bytes(
    entries: list[ReleaseEntry], release_version: str
) -> bytes:
    timestamp = archive_timestamp()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for entry in entries:
            archive.writestr(
                zip_info(entry.relative, release_version, timestamp), entry.data
            )
    return buffer.getvalue()


def write_archive(output: Path, entries: list[ReleaseEntry], release_version: str) -> None:
    archive_bytes = canonical_archive_bytes(entries, release_version)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(archive_bytes)
        verify_archive(temporary, entries, release_version)
        temporary.chmod(0o644)
        os.replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def checksum_path(output: Path) -> Path:
    return output.with_suffix(output.suffix + ".sha256")


def write_checksum(output: Path) -> str:
    checksum = sha256(output)
    destination = checksum_path(output)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(f"{checksum}  {output.name}\n")
        os.replace(temporary, destination)
        destination.chmod(0o644)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return checksum


def verify_archive(output: Path, entries: list[ReleaseEntry], release_version: str) -> None:
    if not output.is_file():
        fail(f"发行包不存在：{output}")
    expected = [
        (member_name(entry.relative, release_version), entry)
        for entry in entries
    ]
    expected_names = [name for name, _path in expected]
    timestamp = archive_timestamp()
    try:
        with zipfile.ZipFile(output, "r") as archive:
            if archive.comment:
                fail("发行包包含非确定性 ZIP 注释")
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if names != expected_names:
                fail("发行包成员列表、顺序或数量不符合预期")
            if len(names) != len(set(names)):
                fail("发行包包含重复成员")
            damaged = archive.testzip()
            if damaged:
                fail(f"发行包成员 CRC 校验失败：{damaged}")
            for info, (name, entry) in zip(infos, expected):
                relative = entry.relative
                if info.filename.startswith(("/", "\\")) or ".." in Path(info.filename).parts:
                    fail(f"发行包成员路径不安全：{info.filename}")
                if info.filename != name:
                    fail(f"发行包成员名称不匹配：{info.filename}")
                if info.date_time != timestamp:
                    fail(f"发行包时间戳未规范化：{info.filename}")
                if info.create_system != 3 or info.compress_type != zipfile.ZIP_STORED:
                    fail(f"发行包平台或存储元数据未规范化：{info.filename}")
                mode = (info.external_attr >> 16) & 0xFFFF
                expected_mode = stat.S_IFREG | archive_mode(relative)
                if mode != expected_mode:
                    fail(f"发行包权限不正确：{info.filename}")
                if info.extra or info.comment:
                    fail(f"发行包含非确定性扩展元数据：{info.filename}")
                if archive.read(info) != entry.data:
                    fail(f"发行包内容与源码不一致：{info.filename}")
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        fail(f"无法校验发行包：{exc}")
    if output.read_bytes() != canonical_archive_bytes(entries, release_version):
        fail("发行包字节不是规范的可重复构建结果")


def verify_checksum(output: Path) -> str:
    destination = checksum_path(output)
    try:
        actual = destination.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"无法读取校验文件：{exc}")
    expected_hash = sha256(output)
    expected = f"{expected_hash}  {output.name}\n"
    if actual != expected:
        fail(f"SHA-256 校验文件不匹配：{destination}")
    return expected_hash


def validate_output_dir(output_dir: Path) -> Path:
    resolved = output_dir.expanduser().resolve()
    try:
        relative = resolved.relative_to(ROOT.resolve())
    except ValueError:
        return resolved
    if not relative.parts or not has_excluded_part(relative):
        fail("项目内发行目录必须位于 dist、build、release 或 tmp 等排除目录中")
    return resolved


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建并校验不含用户数据的总控台发行包")
    parser.add_argument("--dist", type=Path, default=DEFAULT_DIST, help="输出目录")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-only", action="store_true", help="只验证发行来源")
    modes.add_argument("--verify-only", action="store_true", help="只验证已有发行包及校验文件")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    release_version = version()
    files = iter_release_files()
    entries = validate_payload(files)
    if args.check_only:
        print(f"发行内容检查通过：{len(files)} 个文件，版本 {release_version}")
        return 0

    output_dir = validate_output_dir(args.dist)
    output = output_dir / f"console-{release_version}.zip"
    if args.verify_only:
        verify_archive(output, entries, release_version)
        checksum = verify_checksum(output)
        print(f"发行包校验通过：{output}")
        print(f"SHA-256 {checksum}")
        return 0

    write_archive(output, entries, release_version)
    verify_archive(output, entries, release_version)
    checksum = write_checksum(output)
    verify_checksum(output)
    print(f"已生成并校验 {output}")
    print(f"SHA-256 {checksum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
