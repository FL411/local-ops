"""Validated, JSON friendly launch configuration for managed applications.

This module deliberately contains no process creation code. It gives config
migrations, API handlers, and the Windows runtime one canonical representation
of a command without requiring any of them to parse shell text.
"""

import ipaddress
import ntpath
import subprocess
import urllib.parse


LAUNCH_MODES = frozenset(("exec", "cmd", "powershell", "legacy-shell"))
READINESS_TYPES = frozenset(("tcp", "http", "none"))
MAX_WINDOWS_COMMAND_LINE_CHARS = 32767
MAX_WINDOWS_ENVIRONMENT_CHARS = 32767


class LaunchSpecError(ValueError):
    """A launch specification is malformed or cannot be represented safely."""


def _string(value, field, *, allow_empty=False):
    if not isinstance(value, str):
        raise LaunchSpecError("%s 必须是字符串" % field)
    if "\x00" in value:
        raise LaunchSpecError("%s 不能包含 NUL 字符" % field)
    if not allow_empty and not value.strip():
        raise LaunchSpecError("%s 不能为空" % field)
    return value


def _utf16_units(value):
    return len(value.encode("utf-16-le")) // 2


def _is_loopback_host(host):
    if not isinstance(host, str) or not host:
        return False
    if host.casefold() == "localhost":
        return True
    if "%" in host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _canonical_http_target(url, port, base_host, *, expected_host=None):
    """Validate one local HTTP readiness target and return its canonical host.

    Relative paths use the configured host/port. Absolute URLs are restricted
    to HTTP loopback on that same port so a readiness check cannot probe an
    unrelated service. Redirect callers can also pin the original host.
    """
    if not isinstance(url, str) or not url or any(ord(ch) < 0x20 for ch in url):
        raise LaunchSpecError("HTTP readiness URL 无效")
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise LaunchSpecError("HTTP readiness URL 无效") from exc
    if parsed.scheme or parsed.netloc:
        if parsed.scheme.casefold() != "http" or not parsed.hostname:
            raise LaunchSpecError("HTTP readiness 仅允许 HTTP loopback URL")
        if parsed.username is not None or parsed.password is not None:
            raise LaunchSpecError("HTTP readiness URL 不能包含用户信息")
        try:
            target_port = parsed.port
        except ValueError as exc:
            raise LaunchSpecError("HTTP readiness URL 端口无效") from exc
        if target_port != port:
            raise LaunchSpecError("HTTP readiness URL 端口必须与应用端口一致")
        target_host = parsed.hostname
    else:
        if (not url.startswith("/") or url.startswith("//")
                or "\\" in url or parsed.netloc):
            raise LaunchSpecError("HTTP readiness 必须是本地路径或 loopback URL")
        target_host = base_host.strip("[]")

    if not _is_loopback_host(target_host):
        raise LaunchSpecError("HTTP readiness 仅允许 loopback 地址")
    if expected_host is not None and target_host.casefold() != expected_host.casefold():
        raise LaunchSpecError("HTTP readiness 重定向不能更换主机")
    return target_host


def http_readiness_url(host, port, url, *, expected_host=None):
    """Return the absolute, validated HTTP readiness URL."""
    target_host = _canonical_http_target(
        url, port, host, expected_host=expected_host)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme:
        return url, target_host
    authority_host = ("[%s]" % target_host
                      if ":" in target_host and not target_host.startswith("[")
                      else target_host)
    path = url if url.startswith("/") else "/" + url
    return "http://%s:%d%s" % (authority_host, port, path), target_host


def default_readiness(port=None):
    """Return the default readiness probe for a service port, if any."""
    if port is None:
        return {"type": "none", "host": "localhost", "port": None,
                "url": None, "timeoutSec": 20}
    return {"type": "tcp", "host": "localhost", "port": int(port),
            "url": None, "timeoutSec": 20}


def _normalize_readiness(value, port):
    if value is None:
        return default_readiness(port)
    if not isinstance(value, dict):
        raise LaunchSpecError("readiness 必须是对象")
    probe_type = value.get("type", "tcp" if port is not None else "none")
    if not isinstance(probe_type, str) or probe_type not in READINESS_TYPES:
        raise LaunchSpecError("readiness.type 必须是 tcp、http 或 none")
    host = value.get("host", "localhost")
    if not isinstance(host, str) or "\x00" in host or not host.strip():
        raise LaunchSpecError("readiness.host 必须是有效字符串")
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    probe_port = value.get("port", port)
    if probe_type in ("tcp", "http"):
        if type(probe_port) is not int or not 1 <= probe_port <= 65535:
            raise LaunchSpecError("readiness.port 必须在 1 到 65535 之间")
    else:
        probe_port = None
    url = value.get("url")
    if url is not None:
        url = _string(url, "readiness.url")
    if probe_type == "http" and not url:
        raise LaunchSpecError("HTTP readiness 必须提供 url")
    if probe_type == "http":
        _canonical_http_target(url, probe_port, host)
    timeout = value.get("timeoutSec", 20)
    if type(timeout) not in (int, float) or not 0 < timeout <= 300:
        raise LaunchSpecError("readiness.timeoutSec 必须大于 0 且不超过 300")
    return {"type": probe_type, "host": host, "port": probe_port,
            "url": url, "timeoutSec": timeout}


def legacy_launch_spec(command, cwd=None, port=None):
    """Wrap an existing free-form command without changing its shell meaning."""
    command = _string(command or "", "command", allow_empty=True)
    if cwd is not None:
        cwd = _string(cwd, "cwd", allow_empty=True) or None
    return {
        "mode": "legacy-shell",
        "executable": None,
        "args": [],
        "cwd": cwd,
        "env": {},
        "readiness": default_readiness(port),
        # The old command syntax is intentionally isolated to the compatibility
        # mode. New structured launch specs never need to round-trip shell text.
        "legacyCommand": command,
    }


def normalize_launch_spec(value, *, command="", cwd=None, port=None,
                          require_absolute=True):
    """Validate and return the canonical dict representation.

    ``None`` is accepted only as a compatibility input and wraps ``command``
    in ``legacy-shell`` mode. ``exec``, ``cmd`` and ``powershell`` specs use an
    absolute executable path by default; callers resolving a candidate may
    set ``require_absolute=False`` until it has been resolved.
    """
    if value is None:
        return legacy_launch_spec(command, cwd, port)
    if not isinstance(value, dict):
        raise LaunchSpecError("launchSpec 必须是对象")
    mode = value.get("mode")
    if not isinstance(mode, str) or mode not in LAUNCH_MODES:
        raise LaunchSpecError("launchSpec.mode 无效")
    spec_cwd = value.get("cwd", cwd)
    if spec_cwd is not None:
        spec_cwd = _string(spec_cwd, "launchSpec.cwd", allow_empty=True) or None
    raw_env = value.get("env", {})
    if not isinstance(raw_env, dict):
        raise LaunchSpecError("launchSpec.env 必须是对象")
    env = {}
    for key, item in raw_env.items():
        key = _string(key, "launchSpec.env key")
        item = _string(item, "launchSpec.env value", allow_empty=True)
        if "=" in key:
            raise LaunchSpecError("环境变量名称不能包含 =")
        env[key] = item
    overlay_chars = sum(_utf16_units(key) + _utf16_units(item) + 2
                        for key, item in env.items()) + 1
    if overlay_chars > MAX_WINDOWS_ENVIRONMENT_CHARS:
        raise LaunchSpecError("环境变量总长度超过 Windows 进程限制")

    args = value.get("args", [])
    if not isinstance(args, list):
        raise LaunchSpecError("launchSpec.args 必须是数组")
    args = [_string(arg, "launchSpec.args[]", allow_empty=True)
            for arg in args]

    executable = value.get("executable")
    if mode == "legacy-shell":
        legacy_command = value.get("legacyCommand", value.get("command", command))
        legacy = legacy_launch_spec(legacy_command, spec_cwd, port)
        if _utf16_units(legacy_command) + 1 > MAX_WINDOWS_COMMAND_LINE_CHARS:
            raise LaunchSpecError("启动命令超过 Windows 进程命令行长度限制")
        legacy["env"] = env
        legacy["readiness"] = _normalize_readiness(
            value.get("readiness"), port)
        return legacy

    executable = _string(executable, "launchSpec.executable")
    if require_absolute and not ntpath.isabs(executable):
        raise LaunchSpecError("launchSpec.executable 必须是绝对 Windows 路径")
    readiness = _normalize_readiness(value.get("readiness"), port)
    try:
        command_line = subprocess.list2cmdline([executable] + args)
    except (TypeError, ValueError) as exc:
        raise LaunchSpecError("启动参数无法编码为 Windows 命令行") from exc
    if _utf16_units(command_line) + 1 > MAX_WINDOWS_COMMAND_LINE_CHARS:
        raise LaunchSpecError("启动命令超过 Windows 进程命令行长度限制")
    return {"mode": mode, "executable": executable, "args": args,
            "cwd": spec_cwd, "env": env, "readiness": readiness}


def command_from_launch_spec(value):
    """Produce compatibility display text from a validated LaunchSpec."""
    if not isinstance(value, dict):
        return ""
    mode = value.get("mode")
    if mode == "legacy-shell":
        return value.get("legacyCommand", value.get("command", ""))
    executable = value.get("executable")
    args = value.get("args")
    if not isinstance(executable, str) or not isinstance(args, list):
        return ""
    try:
        return subprocess.list2cmdline([executable] + args)
    except (TypeError, ValueError):
        return ""


def is_launch_configured(spec):
    """Whether a card has enough saved launch information to be managed."""
    if not isinstance(spec, dict):
        return False
    if spec.get("mode") == "legacy-shell":
        return bool((spec.get("legacyCommand") or "").strip())
    return (isinstance(spec.get("mode"), str)
            and spec.get("mode") in ("exec", "cmd", "powershell")
            and bool(spec.get("executable")))


def launch_signature_fields(app):
    """Return the launch-model fields used by the console stale check.

    Configurations written before schema v2 have no ``controlMode`` or
    ``launchSpec``. Treat them as managed legacy cards, matching the explicit
    values written by the v1-to-v2 migration. The old ``attached`` bit is not
    used as a fallback here because it was runtime identity in v1.
    """
    mode = app.get("controlMode", "managed")
    if mode not in ("managed", "monitor"):
        mode = "managed"
    if mode == "monitor":
        spec = None
    else:
        try:
            spec = normalize_launch_spec(
                app.get("launchSpec"), command=app.get("command", ""),
                cwd=app.get("cwd"), port=app.get("port"))
        except LaunchSpecError:
            # Invalid model data is still represented deterministically so the
            # launcher can notice a disk/memory difference and request repair.
            spec = app.get("launchSpec")
    return {"controlMode": mode, "launchSpec": spec}
