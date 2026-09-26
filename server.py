#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""总控台后端（单文件，仅 Python 3 标准库）。

本地服务监控 + 快速启动台：
    python server.py  →  绑定 127.0.0.1，端口 9600 起（被占 +1，最多 10 个）
API 契约与实现要点见 AGENTS.md。
"""

import glob
import functools
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sysops
import windows_runtime
from launch_spec import (LaunchSpecError, command_from_launch_spec,
                         default_readiness, is_launch_configured,
                         http_readiness_url,
                         launch_signature_fields,
                         normalize_launch_spec)

# Windows 系统托盘（纯 ctypes，零依赖）。
try:
    import tray as _tray_mod
except ImportError:
    _tray_mod = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION_PATH = os.path.join(BASE_DIR, "VERSION")
LEGACY_DATA_DIR = os.path.join(BASE_DIR, "data")
DEFAULT_DATA_DIR = sysops.default_data_dir()
DEFAULT_LOGS_DIR = sysops.default_logs_dir()


def resolve_runtime_dir(name, default):
    """解析专用运行目录，拒绝空值、相对路径和过宽目标。"""
    if name not in os.environ:
        return os.path.abspath(default), False
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        raise RuntimeError("%s 不能为空" % name)
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        raise RuntimeError("%s 必须是绝对路径" % name)
    path = os.path.abspath(expanded)
    forbidden = {os.path.abspath(os.sep), os.path.abspath(os.path.expanduser("~")),
                 os.path.abspath(BASE_DIR)}
    if path in forbidden:
        raise RuntimeError("%s 必须指向专用子目录" % name)
    return path, True


_TRAY_PNG = None
if _tray_mod is not None:
    try:
        with open(os.path.join(BASE_DIR, "static", "assets",
                               "favicon-32.png"), "rb") as _f:
            _TRAY_PNG = _f.read()
    except (OSError, IOError):
        _TRAY_PNG = None


DATA_DIR, DATA_DIR_OVERRIDDEN = resolve_runtime_dir(
    "CONSOLE_DATA_DIR", DEFAULT_DATA_DIR)
ICONS_DIR = os.path.join(DATA_DIR, "icons")
LOGS_DIR, LOGS_DIR_OVERRIDDEN = resolve_runtime_dir(
    "CONSOLE_LOG_DIR", DEFAULT_LOGS_DIR)
STATIC_DIR = os.path.join(BASE_DIR, "static")
THEMES_DIR = os.path.join(STATIC_DIR, "themes")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
INSTANCE_LOCK_PATH = os.path.join(DATA_DIR, "console.lock")
CONTROL_TOKEN_PATH = os.path.join(DATA_DIR, "control.token")

CURRENT_SCHEMA_VERSION = 2

# 默认 UI 主题：新安装与无偏好回退均使用它，主题清单中固定排首位。
DEFAULT_UI_THEME = "ops"


def read_project_version(path=VERSION_PATH):
    """读取根目录 VERSION。失败时保持服务可诊断，但标记为降级。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = f.read(128).strip()
        if not re.fullmatch(
                r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
                r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", value):
            raise ValueError("VERSION 不是合法的 SemVer")
        return value, None
    except (OSError, UnicodeError, ValueError) as e:
        return "0.0.0+unknown", str(e)


APP_VERSION, VERSION_LOAD_ERROR = read_project_version()

HOST = "127.0.0.1"
PORT_START = 9600
PORT_TRIES = 10
SUBPROCESS_TIMEOUT = 5          # 外部命令超时（秒）
MAX_ICON_BYTES = 5 * 1024 * 1024
MAX_JSON_BYTES = 1 * 1024 * 1024
MAX_DETECT_FILE_BYTES = 2 * 1024 * 1024
MAX_LOG_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 3
LOG_MAINTENANCE_SEC = 30
STARTUP_PROBE_SEC = 0.25
APP_STOP_TIMEOUT_SEC = 5.0
# 开机自启：总控台启动后延迟 AUTOSTART_DELAY_SEC 再逐个拉起标记 autostart
# 的 service，服务之间间隔 AUTOSTART_INTERVAL_SEC 避免同时启动冲突。
AUTOSTART_DELAY_SEC = 5
AUTOSTART_INTERVAL_SEC = 2
RUN_TOKEN_ENV = "CONSOLE_RUN_TOKEN"
RUN_TOKEN_ARG_PREFIX = "console-run:"
TASK_CANCELED_EXIT_CODE = 130
CONTROL_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")

SELF_PID = os.getpid()
SELF_UID = sysops.SELF_UID
ICON_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".ico")
LOG = logging.getLogger("console")
LOG_LOCK = threading.RLock()
MANUAL_STOP_LOCK = threading.RLock()
MANUAL_STOP_TOKENS = set()
WATCHER_LOCK = threading.RLock()
ACTIVE_EXIT_WATCHERS = set()
ACTIVE_EXIT_PROCS = {}
ACTIVE_READINESS_WATCHERS = set()
RUN_JOB_REOPEN_FAILED = object()
RETAINED_RUN_JOBS_LOCK = threading.RLock()
RETAINED_RUN_JOBS = {}
UNPERSISTED_RUNS = {}
RUN_JOB_ACCESS_LOCK = threading.RLock()


def configure_console_encoding():
    """让 Windows 非 UTF-8 控制台也能安全输出诊断信息。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            # 某些嵌入式/测试输出流不支持修改编码；print 本身仍可继续。
            pass


configure_console_encoding()


def is_current_user(identity):
    """严格判断进程身份是否属于当前用户。

    Windows 使用 SID。身份未知时必须拒绝，不能把 ``None == None``
    误判为同一用户。
    """
    return identity is not None and SELF_UID is not None and identity == SELF_UID


def classify_task_exit(code):
    """把一次性任务的退出码归一为稳定的产品语义。"""
    if code == 0:
        return "succeeded"
    if code == TASK_CANCELED_EXIT_CODE:
        return "canceled"
    return "failed"


def public_last_exit(app):
    """兼容旧配置：只在 API 输出时补齐任务状态，不改写磁盘。"""
    value = app.get("lastExit")
    if not isinstance(value, dict):
        return value
    result = dict(value)
    if (app.get("kind") or "service") == "task":
        # 旧版把“总控台按钮停止”记作 canceled + null；新协议中它是 stopped。
        if result.get("status") == "canceled" and result.get("code") is None:
            result["status"] = "stopped"
        elif (result.get("status") not in
              {"succeeded", "canceled", "failed", "stopped"}
              and isinstance(result.get("code"), int)):
            result["status"] = classify_task_exit(result["code"])
    return result


STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".otf": "font/otf",
    ".woff2": "font/woff2",
}

PLACEHOLDER_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>总控台</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f5f5f7;color:#1d1d1f}
.card{background:#fff;border:1px solid rgba(0,0,0,.06);border-radius:14px;padding:36px 44px;box-shadow:0 8px 30px rgba(0,0,0,.08);max-width:540px;text-align:center}
h1{font-size:20px;margin:0 0 14px}p{color:#6e6e73;font-size:14px;line-height:1.8;margin:6px 0}
code{background:#f5f5f7;border:1px solid rgba(0,0,0,.05);border-radius:6px;padding:2px 7px;font-family:ui-monospace,Menlo,monospace;font-size:13px}
</style></head>
<body><div class="card">
<h1>🖥 总控台后端运行中</h1>
<p>前端文件 <code>static/index.html</code> 尚未提供，界面暂不可用。</p>
<p>API 已就绪：<code>GET /api/state</code></p>
</div></body></html>"""

APP_ROUTE_RE = re.compile(
    r"^/api/apps/([0-9a-fA-F]{8})(?:/(start|stop|restart|icon|logs|favicon|diagnose|attach|validate-launch))?$")


# ---------------------------------------------------------------- 运行目录

def _ensure_private_dir(path):
    if os.path.islink(path):
        raise OSError("私有运行目录不能是符号链接: %s" % path)
    os.makedirs(path, mode=0o700, exist_ok=True)
    if os.path.islink(path) or not os.path.isdir(path):
        raise OSError("私有运行路径不是安全目录: %s" % path)
    try:
        os.chmod(path, 0o700)
    except OSError:
        LOG.warning("无法收紧目录权限: %s", path)
    # Windows chmod 不改变 DACL。数据目录可删除/替换其中的 token 文件，
    # 因此必须在目录级别限制为当前 SID，而不只保护单个文件。
    sysops.protect_private_directory(path)


def _copy_private_regular_file(source, target):
    """不跟随符号链接地复制普通文件，目标权限固定为 0600。"""
    try:
        source_stat = os.lstat(source)
    except OSError:
        return False
    if not stat.S_ISREG(source_stat.st_mode):
        return False
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, source_flags)
    try:
        target_fd = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(os.dup(source_fd), "rb") as src, \
                    os.fdopen(target_fd, "wb") as dst:
                target_fd = -1
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
        finally:
            if target_fd >= 0:
                os.close(target_fd)
    finally:
        os.close(source_fd)
    os.chmod(target, 0o600)
    return True


def _install_migrated_directory(target, populate):
    """在目标不存在时原子安装一份迁移副本。"""
    if os.path.lexists(target):
        return False
    parent = os.path.dirname(target) or "."
    # parent 可能是用户共用的 AppData\Roaming，不能把整个目录收成 0700。
    # 只确保存在，不擅自改它的现有权限。
    os.makedirs(parent, mode=0o700, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".console-migration-", dir=parent)
    installed = False
    try:
        os.chmod(staging, 0o700)
        populate(staging)
        try:
            os.rename(staging, target)
            installed = True
        except OSError as e:
            # 另一个同时启动的实例可能已经完成迁移。
            if not os.path.lexists(target) or e.errno not in (
                    errno.EEXIST, errno.ENOTEMPTY):
                raise
        return installed
    finally:
        if not installed and os.path.isdir(staging):
            shutil.rmtree(staging)


def migrate_legacy_runtime_data(
        data_dir=DATA_DIR, logs_dir=LOGS_DIR,
        legacy_data_dir=LEGACY_DATA_DIR,
        data_overridden=DATA_DIR_OVERRIDDEN,
        logs_overridden=LOGS_DIR_OVERRIDDEN):
    """首次运行时将项目内旧数据复制到 Windows 用户数据目录。

    只在对应目标完全不存在且没有显式环境变量覆盖时执行。
    旧文件不会被删除或改权限。
    """
    result = {"dataMigrated": False, "logsMigrated": False}
    legacy_data_dir = os.path.abspath(legacy_data_dir)
    data_dir = os.path.abspath(data_dir)
    logs_dir = os.path.abspath(logs_dir)

    if (not data_overridden and data_dir != legacy_data_dir
            and os.path.isdir(legacy_data_dir)
            and not os.path.lexists(data_dir)):
        def populate_data(staging):
            for name in ("config.json", "config.json.bak"):
                _copy_private_regular_file(
                    os.path.join(legacy_data_dir, name),
                    os.path.join(staging, name))
            source_icons = os.path.join(legacy_data_dir, "icons")
            if os.path.isdir(source_icons) and not os.path.islink(source_icons):
                target_icons = os.path.join(staging, "icons")
                os.mkdir(target_icons, 0o700)
                for name in os.listdir(source_icons):
                    if os.path.basename(name) != name:
                        continue
                    _copy_private_regular_file(
                        os.path.join(source_icons, name),
                        os.path.join(target_icons, name))

        result["dataMigrated"] = _install_migrated_directory(
            data_dir, populate_data)

    legacy_logs = os.path.join(legacy_data_dir, "logs")
    if (not logs_overridden and logs_dir != legacy_logs
            and os.path.isdir(legacy_logs) and not os.path.islink(legacy_logs)
            and not os.path.lexists(logs_dir)):
        def populate_logs(staging):
            for name in os.listdir(legacy_logs):
                if os.path.basename(name) != name:
                    continue
                _copy_private_regular_file(
                    os.path.join(legacy_logs, name),
                    os.path.join(staging, name))

        result["logsMigrated"] = _install_migrated_directory(
            logs_dir, populate_logs)
    return result


def prepare_runtime_storage():
    migration = migrate_legacy_runtime_data()
    for private_dir in (DATA_DIR, ICONS_DIR, LOGS_DIR):
        _ensure_private_dir(private_dir)
    for path in (CONFIG_PATH, CONFIG_PATH + ".bak", INSTANCE_LOCK_PATH,
                 CONTROL_TOKEN_PATH):
        try:
            if stat.S_ISREG(os.lstat(path).st_mode):
                os.chmod(path, 0o600)
        except OSError:
            pass
    for directory in (ICONS_DIR, LOGS_DIR):
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        os.chmod(entry.path, 0o600)
                except OSError:
                    LOG.warning("无法收紧文件权限: %s", entry.path)
    return migration


def write_private_bytes(path, payload):
    """以 0600 权限写入用户数据文件。"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(path, 0o600)


def _read_control_token(path):
    """读取已存在的控制令牌；文件不可信或不可读时返回 None。"""
    try:
        file_stat = os.lstat(path)
        if not stat.S_ISREG(file_stat.st_mode):
            return None
        if not sysops.path_owned_by_current_user(path):
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as f:
            raw = f.read(256)
        token = raw.decode("ascii").strip()
    except (OSError, UnicodeError):
        return None
    return token if CONTROL_TOKEN_RE.fullmatch(token) else None


def load_control_token(path=None):
    """读取或首次创建受保护的本地控制能力令牌。"""
    if path is None:
        path = CONTROL_TOKEN_PATH
    directory = os.path.dirname(os.path.abspath(path)) or "."
    _ensure_private_dir(directory)
    if not sysops.path_owned_by_current_user(directory):
        raise OSError("控制令牌目录不属于当前用户: %s" % directory)
    token = _read_control_token(path)
    if token is None:
        token = secrets.token_urlsafe(32)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            token = _read_control_token(path)
            if token is None:
                raise OSError("控制令牌文件不可读或格式无效: %s" % path)
        else:
            with os.fdopen(fd, "wb") as f:
                f.write((token + "\n").encode("ascii"))
                f.flush()
                os.fsync(f.fileno())
    # Windows chmod 不能收紧 ACL；sysops 会以当前 SID 显式保护该文件。
    sysops.protect_private_file(path)
    return token


def console_url(port, token=None):
    """构造仅在浏览器 fragment 中携带控制令牌的本地 URL。"""
    url = "http://%s:%d/" % (HOST, int(port))
    if token:
        url += "#console_token=" + urllib.parse.quote(token, safe="-_")
    return url


def open_console_browser(port, token_path=None):
    """用持久化控制令牌打开现有总控台；令牌缺失时拒绝降级打开。"""
    if token_path is None:
        token_path = CONTROL_TOKEN_PATH
    token = _read_control_token(token_path)
    if not token:
        return False
    try:
        return bool(webbrowser.open(console_url(port, token)))
    except Exception:
        return False


# ---------------------------------------------------------------- 配置


class ConfigSchemaError(ValueError):
    pass


class FutureConfigSchemaError(ConfigSchemaError):
    pass


def migrate_config_v0_to_v1(raw):
    """旧配置没有 schemaVersion；v1 只建立显式版本基线。"""
    migrated = dict(raw)
    migrated["schemaVersion"] = 1
    return migrated


def migrate_config_v1_to_v2(raw):
    """Separate launch definitions from observed and running identities.

    Free-form commands are retained verbatim in ``legacy-shell`` mode so this
    schema migration cannot silently alter a user's existing shell syntax.
    Old attached cards become observation-only cards; their command and PID
    remain available as history, but are not treated as a restart definition.
    """
    migrated = dict(raw)
    apps = []
    for raw_item in raw.get("apps", []) if isinstance(raw.get("apps"), list) else []:
        if not isinstance(raw_item, dict):
            apps.append(raw_item)
            continue
        app = dict(raw_item)
        attached = bool(app.get("attached"))
        control_mode = app.get("controlMode")
        if control_mode not in ("managed", "monitor"):
            control_mode = "monitor" if attached else "managed"
        app["controlMode"] = control_mode
        if control_mode == "monitor":
            app["attached"] = True
            app["launchSpec"] = None
            app["launchConfigured"] = False
            observation = app.get("observation")
            if not isinstance(observation, dict):
                observation = None
            if observation is None and app.get("lastPid"):
                port = app.get("port")
                observation = {
                    "pid": app.get("lastPid"),
                    "createTime": app.get("lastCreateTime"),
                    "sid": None,
                    "cwd": app.get("cwd"),
                    "ports": [port] if type(port) is int else [],
                    "observedAt": None,
                }
            app["observation"] = observation
            app["runInstance"] = None
            app["readinessState"] = "unknown"
        else:
            app["attached"] = False
            # One-time v1 compatibility repair: old versions could persist
            # uv's shared Python cache as the interpreter for a managed card.
            # Observation cards keep their historic command untouched.
            old_command = app.get("command", "")
            parsed_python = _command_python_executable(old_command)
            if (parsed_python and _is_uv_python_path(parsed_python[2])
                    and app.get("cwd")):
                repaired_command = normalize_attached_python_command(
                    old_command, app.get("cwd"))
                if repaired_command != old_command:
                    app["command"] = repaired_command
            spec = app.get("launchSpec")
            if spec is None:
                spec = normalize_launch_spec(
                    None, command=app.get("command", ""),
                    cwd=app.get("cwd"), port=app.get("port"))
            app["launchSpec"] = normalize_launch_spec(
                spec, command=app.get("command", ""), cwd=app.get("cwd"),
                port=app.get("port"))
            app["command"] = command_from_launch_spec(app["launchSpec"])
            app["launchConfigured"] = is_launch_configured(app["launchSpec"])
            app["observation"] = None
            old_instance = app.get("runInstance")
            if not isinstance(old_instance, dict):
                old_instance = None
            if old_instance is None and app.get("lastPid"):
                token = app.get("runToken")
                old_instance = {
                    "runId": token or "legacy-%s-%s" % (
                        app.get("id", "unknown"), app.get("lastPid")),
                    "jobName": None,
                    "rootPid": app.get("lastPid"),
                    "rootCreateTime": app.get("lastCreateTime"),
                    "processState": "alive" if token else "absent",
                    "exitResult": app.get("lastExit"),
                }
            app["runInstance"] = old_instance
            app["readinessState"] = "unknown"
        apps.append(app)
    migrated["apps"] = apps
    migrated["schemaVersion"] = 2
    return migrated


CONFIG_MIGRATIONS = {0: migrate_config_v0_to_v1, 1: migrate_config_v1_to_v2}


def migrate_config(raw):
    """将任意已支持的旧 schema 逐版幂等迁移到当前版本。"""
    if not isinstance(raw, dict):
        raise ConfigSchemaError("配置根节点必须是 JSON 对象")
    version = raw.get("schemaVersion", 0)
    if type(version) is not int or version < 0:
        raise ConfigSchemaError("schemaVersion 必须是非负整数")
    if version > CURRENT_SCHEMA_VERSION:
        raise FutureConfigSchemaError(
            "配置 schemaVersion=%d 新于当前程序支持的 %d" %
            (version, CURRENT_SCHEMA_VERSION))
    source_version = version
    migrated = json.loads(json.dumps(raw, ensure_ascii=False))
    while version < CURRENT_SCHEMA_VERSION:
        migration = CONFIG_MIGRATIONS.get(version)
        if migration is None:
            raise ConfigSchemaError("缺少 schemaVersion=%d 的迁移器" % version)
        migrated = migration(migrated)
        next_version = migrated.get("schemaVersion")
        if next_version != version + 1:
            raise ConfigSchemaError("配置迁移器未正确递增 schemaVersion")
        version = next_version
    return migrated, source_version


def _load_config_raw(path):
    """Read a config JSON object. Missing or invalid files return None."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        LOG.warning("读取配置失败: %s", path, exc_info=True)
        return None
    return raw if isinstance(raw, dict) else None


def app_config_signature(apps):
    """Return a stable signature for the persisted launchpad definition.

    Runtime identity (PID, run token and exit history) is deliberately omitted:
    those fields change while an otherwise identical card is running. The
    launcher uses this value to reject a live console whose in-memory card has
    drifted from the on-disk card, even when both contain the same number of
    apps.
    """
    stable_defaults = {
        "id": None, "name": "", "command": "", "cwd": None,
        "port": None, "emoji": None, "glyph": None, "icon": None,
        "favicon": None, "kind": "service", "controlMode": "managed",
        "launchSpec": None,
    }
    stable_apps = []
    for app in apps or []:
        if not isinstance(app, dict) or not app.get("id"):
            continue
        stable_apps.append({key: app.get(key, default)
                            for key, default in stable_defaults.items()})
        stable_apps[-1].update(launch_signature_fields(app))
    payload = json.dumps(
        stable_apps, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class Config:
    """配置读写：显式 schema 迁移 + 原子写 + 上一份良好备份。"""

    DEFAULT = {"schemaVersion": CURRENT_SCHEMA_VERSION,
               "apps": [], "hidden": [], "pinned": [], "promoted": [],
               "watchedKeywords": [], "uiTheme": DEFAULT_UI_THEME,
               "openBrowser": True}
    APP_DEFAULT = {"id": None, "name": "", "command": "", "cwd": None,
                   "port": None, "emoji": None, "glyph": None, "icon": None,
                   "favicon": None, "kind": "service", "lastPid": None,
                   "lastPgid": None, "runToken": None,
                   "attached": False, "lastExit": None, "createdAt": 0,
                   "lastCreateTime": None, "autostart": False,
                   "launchSpec": None, "controlMode": "managed",
                   "observation": None, "runInstance": None,
                   "readinessState": "unknown", "launchConfigured": False}

    def __init__(self, path):
        self._lock = threading.RLock()
        # 应用操作锁属于配置实例，而不是 HTTP 服务器。这样启动期的自启动
        # 守护线程和 HTTP 请求可使用同一把锁，避免各自通过“尚未运行”检查。
        self._app_locks = {}
        self._app_locks_guard = threading.Lock()
        self._path = path
        self._writable = True
        self._recovered_from_backup = False
        self._migration_from = None
        self._health_issues = []
        self._data = self._load()

    @staticmethod
    def _payload(data):
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    @classmethod
    def _normalize(cls, raw):
        data = {"schemaVersion": CURRENT_SCHEMA_VERSION}
        for key, default in cls.DEFAULT.items():
            if key == "schemaVersion":
                continue
            value = raw.get(key)
            if isinstance(value, type(default)):
                data[key] = (json.loads(json.dumps(value, ensure_ascii=False))
                             if isinstance(value, (list, dict)) else value)
            else:
                data[key] = list(default) if isinstance(default, list) else default
        apps = []
        for item in data["apps"]:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            app = dict(cls.APP_DEFAULT)
            for key in app:
                if key in item:
                    app[key] = item[key]
            try:
                saved_mode = item.get("controlMode")
                app["controlMode"] = (
                    saved_mode if saved_mode in ("managed", "monitor")
                    else "monitor" if item.get("attached") else "managed")
                # A card may have a confirmed LaunchSpec while retaining the
                # identity of the external process it originally claimed.
                # Explicit controlMode therefore outranks the legacy attached
                # inference, and attached itself remains independent metadata.
                app["attached"] = (
                    True if app["controlMode"] == "monitor"
                    else bool(item.get("attached")))
                if app["controlMode"] == "monitor":
                    if isinstance(app.get("launchSpec"), dict):
                        app["launchSpec"] = normalize_launch_spec(
                            app["launchSpec"], command=app.get("command", ""),
                            cwd=app.get("cwd"), port=app.get("port"))
                        app["command"] = command_from_launch_spec(
                            app["launchSpec"])
                        app["launchConfigured"] = is_launch_configured(
                            app["launchSpec"])
                    else:
                        app["launchSpec"] = None
                        app["launchConfigured"] = False
                    if not isinstance(app["observation"], dict):
                        app["observation"] = None
                    if app["observation"] is None and app.get("lastPid"):
                        port = app.get("port")
                        app["observation"] = {
                            "pid": app.get("lastPid"),
                            "createTime": app.get("lastCreateTime"),
                            "sid": None,
                            "cwd": app.get("cwd"),
                            "ports": [port] if type(port) is int else [],
                            "observedAt": None,
                        }
                    app["runInstance"] = None
                else:
                    app["launchSpec"] = normalize_launch_spec(
                        app["launchSpec"], command=app.get("command", ""),
                        cwd=app.get("cwd"), port=app.get("port"))
                    app["command"] = command_from_launch_spec(app["launchSpec"])
                    app["launchConfigured"] = is_launch_configured(
                        app["launchSpec"])
                    if app.get("attached"):
                        if not isinstance(app.get("observation"), dict):
                            app["observation"] = None
                    else:
                        app["observation"] = None
                    if not isinstance(app["runInstance"], dict):
                        app["runInstance"] = None
                if app.get("readinessState") not in (
                        "unknown", "checking", "ready", "timeout", "failed"):
                    app["readinessState"] = "unknown"
            except LaunchSpecError as e:
                raise ConfigSchemaError(
                    "应用 %s 的 launchSpec 无效: %s" % (app.get("id"), e))
            apps.append(app)
        data["apps"] = apps
        return data

    def _load(self):
        paths = (self._path, self._path + ".bak")
        found_candidate = False
        for index, path in enumerate(paths):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                migrated, source_version = migrate_config(raw)
                data = self._normalize(migrated)
                if index:
                    self._recovered_from_backup = True
                    LOG.warning("主配置不可读，已从备份恢复: %s", path)
                if source_version < CURRENT_SCHEMA_VERSION:
                    self._migration_from = source_version
                self._persist_loaded_state(
                    data, raw, source_index=index,
                    source_version=source_version)
                return data
            except FileNotFoundError:
                continue
            except FutureConfigSchemaError as e:
                # 回退到旧程序时绝不用旧 .bak 覆盖更新 schema 的主文件。
                found_candidate = True
                self._health_issues.append(str(e))
                LOG.error("拒绝降级读取配置: %s", path)
                break
            except (OSError, UnicodeError, json.JSONDecodeError,
                    ConfigSchemaError, TypeError, ValueError):
                found_candidate = True
                LOG.exception("读取配置失败: %s", path)
        data = self._normalize(self.DEFAULT)
        if found_candidate:
            # 配置和备份都不可用时，展示空状态但禁止写入，
            # 避免一次 UI 操作就把尚可人工恢复的文件覆盖。
            self._writable = False
            self._health_issues.append(
                "主配置与备份均不可读，已进入只读保护状态")
            return data
        control_token = os.path.join(
            os.path.dirname(os.path.abspath(self._path)), "control.token")
        if os.path.lexists(control_token):
            # An established installation must not be reset merely because
            # the roaming profile was temporarily unavailable at sign-in.
            self._writable = False
            self._health_issues.append(
                "已有控制凭据但配置暂时不可见，已进入只读保护状态")
            return data
        try:
            self._write_atomic(self._path, self._payload(data))
        except OSError as e:
            self._writable = False
            self._health_issues.append("无法创建配置文件: %s" % e)
        return data

    def _persist_loaded_state(self, data, raw, source_index, source_version):
        """将已恢复/迁移的配置落回主文件，不破坏良好备份。"""
        needs_migration = source_version < CURRENT_SCHEMA_VERSION
        if not source_index and not needs_migration:
            return
        try:
            if not source_index and needs_migration:
                # 迁移前的配置是上一份良好版本。
                self._write_atomic(self._path + ".bak", self._payload(raw))
            # 从 .bak 恢复时只修复主文件，保留已验证的备份。
            self._write_atomic(self._path, self._payload(data))
        except OSError as e:
            self._writable = False
            self._health_issues.append("配置恢复/迁移落盘失败: %s" % e)
            LOG.exception("配置恢复/迁移落盘失败")


    def _apps_from_raw(self, raw):
        restored = []
        if not isinstance(raw, dict) or not isinstance(raw.get("apps"), list):
            return restored
        try:
            migrated, _ = migrate_config(raw)
            return self._normalize(migrated)["apps"]
        except (ConfigSchemaError, TypeError, ValueError):
            # Keep the last-resort raw reader tolerant. The regular config
            # loader reports invalid schemas; state reconstruction should not
            # turn a transient parse problem into an empty in-memory list.
            LOG.exception("无法规范化磁盘中的启动台卡片")
        for item in raw["apps"]:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            app = dict(self.APP_DEFAULT)
            for key in app:
                if key in item:
                    app[key] = item[key]
            restored.append(app)
        return restored

    def _read_disk_apps(self):
        """Read launchpad cards from this config path. Backup only if main is unreadable."""
        raw = _load_config_raw(self._path)
        if raw is None:
            raw = _load_config_raw(self._path + ".bak")
        return self._apps_from_raw(raw)

    def _reload_from_disk_unlocked(self):
        """Refresh the in-memory config from disk; caller must hold _lock.

        Polling is read-only: it never writes a repaired copy back to disk.
        The main file wins whenever it is readable; the backup is consulted
        only when the main file cannot be parsed. A valid main file with an
        empty ``apps`` list is an intentional empty state, not corruption.
        If neither file is readable, keep the current in-memory copy.
        """
        raw = _load_config_raw(self._path)
        from_backup = False
        if raw is None:
            raw = _load_config_raw(self._path + ".bak")
            from_backup = raw is not None
        if raw is None:
            return False
        try:
            migrated, source_version = migrate_config(raw)
            data = self._normalize(migrated)
        except FutureConfigSchemaError:
            LOG.error("拒绝降级读取配置: %s", self._path)
            return False
        except (ConfigSchemaError, TypeError, ValueError):
            LOG.exception("读取配置失败: %s", self._path)
            return False
        self._data = data
        # Keep startup diagnostics visible after a later successful poll.
        if from_backup:
            self._recovered_from_backup = True
        if source_version < CURRENT_SCHEMA_VERSION:
            self._migration_from = source_version
        return True

    def snapshot(self):
        """返回当前磁盘配置的深拷贝（数据均为 JSON 可序列化）。"""
        with self._lock:
            self._reload_from_disk_unlocked()
            return json.loads(json.dumps(self._data, ensure_ascii=False))

    @property
    def path(self):
        return self._path

    def health_info(self):
        with self._lock:
            disk_app_count = _disk_configured_app_count(self._path)
            issues = list(self._health_issues)
            if disk_app_count is None:
                issues.append("磁盘配置与备份当前均不可读")
            return {
                "writable": self._writable,
                "recoveredFromBackup": self._recovered_from_backup,
                "migratedFromSchema": self._migration_from,
                "issues": issues,
                "configPath": self._path,
                "memoryAppCount": len(self._data.get("apps") or []),
                "diskAppCount": disk_app_count,
                "appSignature": app_config_signature(self._data.get("apps")),
            }

    def update(self, fn):
        """先从磁盘载入，再在锁内执行 fn(self._data) 并原子落盘，返回 fn 的返回值。"""
        with self._lock:
            if not self._writable:
                raise OSError("配置处于只读保护状态，请先恢复配置或权限")
            self._reload_from_disk_unlocked()
            previous = json.loads(json.dumps(self._data, ensure_ascii=False))
            try:
                result = fn(self._data)
                # 内存与修改前都没有卡片时，不要把磁盘上仍在的卡片写成空列表。
                # 真正删光最后一张卡片时 previous["apps"] 非空，仍会正常落盘。
                if (not (self._data.get("apps") or [])
                        and not (previous.get("apps") or [])):
                    disk_apps = self._read_disk_apps()
                    if disk_apps:
                        self._data["apps"] = disk_apps
                        LOG.warning(
                            "kept %d disk apps while persisting config (memory list was empty)",
                            len(disk_apps))
                # Every successful write is schema v2, including cards added
                # by older API paths which still submit only ``command``.
                # Normalize after the callback so compatibility cards are
                # wrapped once and monitor cards cannot accidentally acquire a
                # launch definition from their historical command.
                normalized = self._normalize(self._data)
                self._data.clear()
                self._data.update(normalized)
                # Callbacks commonly return a shallow copy of the changed app.
                # Keep their response in sync with the normalized persisted
                # object without changing non-app result dictionaries.
                if isinstance(result, dict) and result.get("id"):
                    saved_app = find_app(self._data, result.get("id"))
                    if saved_app is not None:
                        result.update(saved_app)
                payload = self._payload(self._data)
                previous_payload = self._payload(previous)
                # 先保存上一份良好内容，再替换主文件。
                self._write_atomic(self._path + ".bak", previous_payload)
                self._write_atomic(self._path, payload)
            except Exception:
                self._data = previous
                raise
        # 缓存失效必须发生在配置锁之外。/api/state 的刷新也会读取配置；
        # 若两把锁反向获取，配置写入与状态轮询可能永久互相等待。
        invalidate_state_cache()
        return result

    def try_app_operation(self, app_id):
        """非阻塞取得单个应用的操作锁，避免并发启停/编辑竞争。"""
        with self._app_locks_guard:
            # RLock 允许 restart 在已持有锁时复用统一启动事务；不同线程仍会
            # 被非阻塞地拒绝，不会排队后基于过期状态继续执行。
            lock = self._app_locks.setdefault(app_id, threading.RLock())
        return lock if lock.acquire(blocking=False) else None

    def forget_app_lock(self, app_id):
        """应用删除后回收其操作锁（调用方应仍持有该锁）。"""
        with self._app_locks_guard:
            self._app_locks.pop(app_id, None)

    @staticmethod
    def _write_atomic(path, payload):
        _ensure_private_dir(os.path.dirname(path) or ".")
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)


def acquire_instance_lock(path=INSTANCE_LOCK_PATH):
    """Acquire the per-project process lock and keep its file object alive.

    Port fallback alone is not a single-instance guarantee: two servers on
    :9600/:9601 would still update the same config.  The lock ties exclusivity
    to this data directory and is released automatically if the process
    crashes (Windows msvcrt locking 由 sysops 封装)。
    """
    return sysops.acquire_lock(path)


def release_instance_lock(lock_file):
    sysops.release_lock(lock_file)


# ---------------------------------------------------------------- 子进程与解析

def run_cmd(args, timeout=SUBPROCESS_TIMEOUT):
    """运行命令并返回 stdout；任何异常/超时都返回空串，绝不上抛。"""
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           errors="replace", timeout=timeout)
        return r.stdout or ""
    except Exception:
        LOG.exception("命令执行失败: %r", args)
        return ""


def parse_etime(s):
    """ps 的 etime：[[dd-]hh:]mm:ss → 秒。异常返回 0。"""
    try:
        s = s.strip()
        days = 0
        if "-" in s:
            d, s = s.split("-", 1)
            days = int(d)
        parts = [int(p) for p in s.split(":")]
        if len(parts) == 2:
            hours, minutes, secs = 0, parts[0], parts[1]
        elif len(parts) == 3:
            hours, minutes, secs = parts
        else:
            return 0
        return days * 86400 + hours * 3600 + minutes * 60 + secs
    except Exception:
        return 0


def _to_float(tok, default=0.0):
    try:
        return float(tok)
    except (TypeError, ValueError):
        return default


def scan_listeners():
    """psutil 监听快照 → {(pid, port): {bind_host, ...}}。

    字典仍可像旧集合一样迭代/判断 ``(pid, port)``，同时保留监听地址，
    供前端区分仅监听 ``::1`` 的服务（需通过 localhost 打开）。
    """
    return sysops.scan_listeners()


def listener_open_host(listeners, port, pids=None):
    """返回浏览器访问监听端口时应使用的本地主机名。

    有些开发服务器只绑定 IPv6 回环 ``::1``；这时 ``127.0.0.1``
    会直接拒绝连接，而 ``localhost`` 能正确解析到它。
    对旧测试/旧调用传入的 set 快照则保持原来的 IPv4 默认值。
    """
    if not isinstance(listeners, dict):
        return "127.0.0.1"
    allowed_pids = set(pids) if pids is not None else None
    hosts = set()
    for (pid, listening_port), values in listeners.items():
        if listening_port != port or (
                allowed_pids is not None and pid not in allowed_pids):
            continue
        if isinstance(values, str):
            hosts.add(values)
        elif isinstance(values, (set, list, tuple)):
            hosts.update(value for value in values if isinstance(value, str))
    normalized = {host.strip("[]").casefold() for host in hosts if host}
    ipv4_capable = any(
        host in ("*", "0.0.0.0") or host.startswith("127.")
        for host in normalized)
    ipv6_loopback_only = bool(normalized) and not ipv4_capable and all(
        host in ("::", "::1", "localhost") for host in normalized)
    return "localhost" if ipv6_loopback_only else "127.0.0.1"


def ps_snapshot(pids=None, with_uid=True):
    """批量进程信息，包含展示用 args 与保留参数边界的 argv。

    平台实现见 sysops：psutil（comm 为 exe 路径，etime 单位为秒）。
    """
    return sysops.ps_snapshot(pids, with_uid=with_uid)


def lsof_cwds(pids):
    """{pid: cwd}。"""
    return sysops.lsof_cwds(pids)


def pid_alive(pid):
    return sysops.pid_alive(pid)


# ---------------------------------------------------------------- 状态构建

SYSTEM_PATH_PREFIXES = sysops.windows_system_dirs()

# Windows 系统进程名单（按 exe 基名匹配，无路径时也能命中）。
# comm 拿不到完整路径（如 System 进程）时按名称归为后台，避免服务监控噪音。
_WINDOWS_SYSTEM_NAMES = frozenset({
    "system", "svchost", "csrss", "wininit", "winlogon", "services",
    "lsass", "smss", "dwm", "fontdrvhost", "spoolsv", "sihost",
    "taskhostw", "runtimebroker", "audiodg", "conhost", "ctfmon",
    "explorer", "dllhost", "searchhost", "shellexperiencehost",
    "startmenuexperiencehost", "securityhealthservice", "wmiprvse",
    "logonui", "userinit", "winlogon",
})

# 开发服务关键词：命中 name/args 时优先归为 "mine"
DEV_KEYWORDS = (
    "python", "node", "ruby", "php", "nginx", "caddy", "postgres",
    "mysql", "redis", "mongo", "ollama", "docker", "deno", "bun",
    "uvicorn", "gunicorn", "hugo", "vite", "streamlit", "jupyter",
    "ngrok", "frp", "code-server", "java",
)


def classify_group(key, name, comm, args, cwd, promoted):
    if key in promoted:
        return "mine"
    text = name.lower()
    if any(k in text for k in DEV_KEYWORDS):
        return "mine"
    base = os.path.basename(text)
    if base.endswith(".exe"):
        base = base[:-4]
    if base in _WINDOWS_SYSTEM_NAMES:
        return "background"
    if comm.startswith(SYSTEM_PATH_PREFIXES):
        return "background"
    return "mine"


HOME_DIR = os.path.expanduser("~")


def project_name(cwd):
    """从工作目录推断项目名（最后一段目录名），无有效 cwd 时返回 None。"""
    if not cwd:
        return None
    cwd = os.path.normpath(cwd).rstrip(os.sep).rstrip("/\\")
    root = os.path.dirname(os.path.abspath(os.sep))
    if not cwd or cwd == os.sep or cwd == root or cwd == HOME_DIR:
        return None
    return os.path.basename(cwd) or None


# ---------------------------------------------------------------- 进程溯源
# 沿 PPID 链向上识别「是谁启动了这个服务」：AI 编程助手、编辑器、终端、
# 总控台自身。结果只是展示用的尽力判断，不影响任何启停逻辑。

# 向上爬时要跳过的包装层（按 argv[0] 基名匹配）：壳、包管理器与任务执行器
_ORIGIN_SKIP_NAMES = {
    "zsh", "bash", "sh", "dash", "fish", "login", "su", "sudo", "env",
    "command", "xargs", "nohup", "script", "expect",
    "npm", "npx", "pnpm", "yarn", "corepack", "make", "just",
    "node", "tsx", "nodemon", "deno", "bun", "bunx",
    "python", "python3", "uv", "poetry", "pip", "pipx",
    "ruby", "php", "java", "dotnet", "go", "cargo",
}

# 已知 AI 编程助手签名（在祖先 args 中做词边界匹配，按顺序取先命中者）
_ORIGIN_AGENT_PATTERNS = (
    (re.compile(r"\bcodex\b", re.I), "Codex"),
    (re.compile(r"claude-code|\bclaude\b", re.I), "Claude Code"),
    (re.compile(r"\bkimi\b", re.I), "Kimi"),
    (re.compile(r"\bgemini\b", re.I), "Gemini"),
    (re.compile(r"\baider\b", re.I), "Aider"),
    (re.compile(r"\bopencode\b", re.I), "OpenCode"),
    (re.compile(r"\bgoose\b", re.I), "Goose"),
    (re.compile(r"\bcursor-agent\b", re.I), "Cursor"),
    (re.compile(r"\bcopilot\b", re.I), "Copilot"),
    (re.compile(r"\bqwen\b", re.I), "Qwen"),
    (re.compile(r"\bqoder\b", re.I), "Qoder"),
    (re.compile(r"\bamp\b", re.I), "Amp"),
    (re.compile(r"\bcodebuddy\b", re.I), "CodeBuddy"),
)

# Windows 可执行文件名（去掉 .exe 后的小写基名）→ (展示名, 图标)。
_WINDOWS_ORIGIN_ALIASES = {
    "code": ("VS Code", "code"),
    "code - insiders": ("VS Code", "code"),
    "cursor": ("Cursor", "code"),
    "trae": ("Trae", "code"),
    "windsurf": ("Windsurf", "code"),
    "zed": ("Zed", "code"),
    "sublime_text": ("Sublime", "code"),
    "sublime": ("Sublime", "code"),
    "webstorm64": ("WebStorm", "code"),
    "idea64": ("IDEA", "code"),
    "pycharm64": ("PyCharm", "code"),
    "goland64": ("GoLand", "code"),
    "notepad++": ("Notepad++", "code"),
    "windowsterminal": ("终端", "terminal"),
    "terminal": ("终端", "terminal"),
    "cmd": ("终端", "terminal"),
    "powershell": ("终端", "terminal"),
    "pwsh": ("终端", "terminal"),
    "conhost": ("终端", "terminal"),
    "mintty": ("终端", "terminal"),
    "explorer": ("资源管理器", "package"),
    "docker desktop": ("Docker", "package"),
    "ollama": ("Ollama", "package"),
    "obsidian": ("Obsidian", "package"),
}

# 终端复用器（直接以 comm 命名，不进跳过表）
_ORIGIN_MULTIPLEXERS = {"tmux": "tmux", "screen": "screen"}


def _win_join_cmdline(cmdline):
    """把 psutil 的 argv 列表拼成可回溯的参数字符串。

    含空格的参数补上引号，使 attribute_origin 能按引号感知还原完整
    可执行路径（如 "C:\\...\\Microsoft VS Code\\Code.exe"），
    同时保持 agent 模式匹配用的大小写文本不变。
    """
    parts = []
    for tok in cmdline:
        tok = str(tok)
        if " " in tok and not (tok.startswith('"') and tok.endswith('"')):
            tok = '"%s"' % tok.replace('"', '\\"')
        parts.append(tok)
    return " ".join(parts)


def origin_snapshot(pids=None):
    """进程表 → {pid: (ppid, args)}，供来源溯源。

    只读取目标 PID 的祖先链，避免为了少量监听进程获取全机每个进程的命令行。
    """
    table = {}
    mod = sysops._psutil()
    if pids is None:
        processes = mod.process_iter(["pid", "ppid", "cmdline"])
        for proc in processes:
            try:
                info = proc.info
                cmdline = info["cmdline"] or []
                table[info["pid"]] = (
                    info["ppid"], _win_join_cmdline(cmdline))
            except (mod.NoSuchProcess, mod.AccessDenied, mod.ZombieProcess):
                continue
        return table

    queue = [(int(pid), 0) for pid in set(pids)]
    seen = set()
    while queue:
        pid, depth = queue.pop()
        if pid <= 0 or pid in seen or depth > 12:
            continue
        seen.add(pid)
        try:
            proc = mod.Process(pid)
            ppid = proc.ppid()
            cmdline = proc.cmdline() or []
            table[pid] = (ppid, _win_join_cmdline(cmdline))
            if ppid > 0 and depth < 12:
                queue.append((ppid, depth + 1))
        except (mod.NoSuchProcess, mod.AccessDenied, mod.ZombieProcess):
            continue
    return table


def attribute_origin(pid, table):
    """沿 PPID 链识别来源应用，返回 {"label", "icon"} 或 None。

    祖先 args 中带有总控台 run-token 前缀（console-run:）即判定为
    「总控台启动」——本机任一总控台实例的受管进程组都持有该标记。
    未识别的中间层先记为候选并继续上爬；AI 助手 / 编辑器 / 终端 /
    总控台是更优答案，都没有时才以最近的未识别进程命名。
    最多上爬 12 层，遇到环或缺失即终止。
    """
    cur, seen, candidate = pid, set(), None
    for _ in range(12):
        entry = table.get(cur)
        if not entry:
            break
        ppid, _ = entry
        if ppid in seen:
            break
        seen.add(ppid)
        parent_args = (table.get(ppid) or (0, ""))[1] or ""
        if ppid <= 1:
            return candidate or {"label": "系统", "icon": "server"}
        if RUN_TOKEN_ARG_PREFIX in parent_args:
            return {"label": "总控台", "icon": "rocket"}
        hay = parent_args.casefold()
        for pattern, label in _ORIGIN_AGENT_PATTERNS:
            if pattern.search(hay):
                return {"label": label, "icon": "bot"}
        # 可执行路径可能含空格并被引号包裹，split()[0] 会截断；
        # 用引号感知解析出完整 exe 路径。
        m = re.match(r'\s*(?:"([^"]*)"|(\S+))', parent_args)
        exe_path = (m.group(1) if m and m.group(1) is not None
                    else (m.group(2) if m else ""))
        base = os.path.basename(exe_path).lstrip("-")
        if base.lower().endswith(".exe"):
            base = base[:-4]
        win_alias = _WINDOWS_ORIGIN_ALIASES.get(base.lower())
        if win_alias:
            return {"label": win_alias[0], "icon": win_alias[1]}
        if base in _ORIGIN_MULTIPLEXERS:
            return {"label": _ORIGIN_MULTIPLEXERS[base], "icon": "terminal"}
        if base and base not in _ORIGIN_SKIP_NAMES and candidate is None:
            candidate = {"label": base, "icon": "package"}
        cur = ppid
    return candidate


def build_services(cfg, groups=None):
    """返回 (services, listeners)。只含当前用户进程，排除控制台自身。"""
    listeners = scan_listeners()
    snap = ps_snapshot({pid for pid, _ in listeners}, with_uid=True)
    mine_pids = [pid for pid, _ in listeners
                 if pid != SELF_PID and pid in snap
                 and is_current_user(snap[pid].get("uid"))]
    cwds = lsof_cwds(mine_pids)
    origin_table = origin_snapshot(mine_pids)

    hidden = set(cfg.get("hidden") or [])
    pinned = set(cfg.get("pinned") or [])
    promoted = set(cfg.get("promoted") or [])
    # “配置了相同端口”不代表“拥有当前监听进程”。只有 run token / 进程组
    # 校验通过（或严格命中旧版身份）的进程才关联启动台卡片。
    app_by_pid = listener_app_owners(
        cfg.get("apps") or [], listeners, snap, cwds, groups)

    services = []
    for pid, port in sorted(listeners, key=lambda x: (x[1], x[0])):
        if pid == SELF_PID:
            continue
        info = snap.get(pid)
        if not info or not is_current_user(info.get("uid")):
            continue
        comm = info.get("comm") or ""
        args = info.get("args") or comm
        name = os.path.basename(comm) if comm else "?"
        key = "%s:%d" % (name, port)
        cwd = cwds.get(pid)
        app = app_by_pid.get(pid)
        services.append({
            "key": key,
            # key 保持 name:port 以兼容既有隐藏/置顶配置；instanceKey 用于
            # 区分同名同端口在不同时间出现的新进程，以及极少数共享监听。
            "instanceKey": "%d:%d" % (pid, port),
            "pid": pid, "name": name, "port": port,
            "openHost": listener_open_host(listeners, port, {pid}),
            "cwd": cwd, "project": project_name(cwd), "cmd": args,
            "cpu": info["cpu"], "mem": info["mem"], "uptimeSec": info["etime"],
            "group": classify_group(key, name, comm, args, cwd, promoted),
            "pinned": key in pinned, "hidden": key in hidden,
            "promoted": key in promoted,
            "appId": app["id"] if app else None,
            "appName": app["name"] if app else None,
            # 来源溯源（尽力判断）：哪个应用/AI 助手启动了这个进程
            "origin": attribute_origin(pid, origin_table),
        })
    return services, listeners


def build_watched(keywords):
    """关注进程：每个 PID 只返回一次，并合并它命中的全部关键字。"""
    normalized = []
    seen_keywords = set()
    for keyword in (keywords or []):
        if not isinstance(keyword, str) or not keyword.strip():
            continue
        keyword = keyword.strip()
        lowered = keyword.casefold()
        if lowered in seen_keywords:
            continue
        seen_keywords.add(lowered)
        normalized.append((keyword, lowered))
    if not normalized:
        return []
    snap = ps_snapshot(None, with_uid=True)
    result = []
    for pid, info in sorted(snap.items()):
        if pid == SELF_PID or not is_current_user(info.get("uid")):
            continue
        name = os.path.basename(info.get("comm") or "") or "?"
        args = info.get("args") or ""
        args_lower = args.casefold()
        matched = [keyword for keyword, lowered in normalized
                   if lowered in args_lower]
        if not matched:
            continue
        result.append({"pid": pid, "name": name, "cmd": args,
                       "cpu": info["cpu"], "mem": info["mem"],
                       "uptimeSec": info["etime"],
                       # keyword 保留给旧前端，keywords 提供无损结构化数据。
                       "keyword": "、".join(matched), "keywords": matched})
    return result


def pgid_members_map():
    """Windows 无 POSIX pgid；进程树请用 sysops.group_members。"""
    return {}


def _managed_candidates(app, groups):
    token = app.get("runToken")
    pgid = app.get("lastPgid") or app.get("lastPid")
    if not isinstance(token, str) or not token or not isinstance(pgid, int) or pgid <= 0:
        return set()
    if groups:
        members = groups.get(pgid)
        if members is not None:
            return set(members)
    # Windows（或 groups 未构建）回退到进程树回溯
    return set(sysops.group_members(pgid))


def _run_job_key(app):
    instance = app.get("runInstance")
    if not isinstance(instance, dict):
        return None
    run_id = instance.get("runId")
    return (app.get("id"), run_id) if run_id else None


def _remember_run_job(app, proc, mode="repair"):
    key = _run_job_key(app)
    if key is None or proc is None:
        return None
    discard = None
    owner = proc
    with RETAINED_RUN_JOBS_LOCK:
        previous = RETAINED_RUN_JOBS.get(key)
        if previous and previous[1] is not proc:
            # Keep one canonical owner.  The incoming handle is not silently
            # leaked: close it unless it is the process currently owned by the
            # exit watcher (which will close it in its own finally block).
            LOG.warning("应用 %s 出现重复的保留 Job Object，沿用现有句柄",
                        app.get("id"))
            discard = proc
            owner = previous[1]
        else:
            RETAINED_RUN_JOBS[key] = (mode, proc)
    if discard is not None:
        with WATCHER_LOCK:
            watcher_proc = ACTIVE_EXIT_PROCS.get(key)
        if watcher_proc is discard:
            # The caller may immediately register this same process as the
            # exit watcher. Return it as the owner even though an older
            # retained handle remains as a cleanup fallback.
            owner = discard
        else:
            try:
                discard.close()
            except Exception:
                LOG.exception("关闭重复的 Job Object 句柄失败（应用 %s）",
                              app.get("id"))
    return owner


def _forget_run_job(app, proc=None, *, close=True):
    key = _run_job_key(app)
    if key is None:
        return
    retained = None
    with RETAINED_RUN_JOBS_LOCK:
        current = RETAINED_RUN_JOBS.get(key)
        if current and (proc is None or current[1] is proc):
            retained = RETAINED_RUN_JOBS.pop(key)[1]
    if close and retained is not None:
        with WATCHER_LOCK:
            watcher_proc = ACTIVE_EXIT_PROCS.get(key)
        if watcher_proc is retained:
            # The exit watcher owns this handle and will close it after its
            # wait and config update. Never block a state/HTTP caller on that
            # wait while holding RUN_JOB_ACCESS_LOCK.
            return
        try:
            retained.close()
        except Exception:
            LOG.exception("关闭已保留的 Job Object 句柄失败（应用 %s）",
                          app.get("id"))


def _release_run_job_handle(app, proc):
    with RUN_JOB_ACCESS_LOCK:
        key = _run_job_key(app)
        with RETAINED_RUN_JOBS_LOCK:
            current = RETAINED_RUN_JOBS.get(key) if key is not None else None
        if current and current[1] is proc:
            _forget_run_job(app, proc)
            return
        try:
            proc.close()
        except Exception:
            LOG.exception("关闭 Job Object 句柄失败（应用 %s）", app.get("id"))


def _run_job_is_retained(app, proc):
    key = _run_job_key(app)
    with RETAINED_RUN_JOBS_LOCK:
        current = RETAINED_RUN_JOBS.get(key) if key is not None else None
    return bool(current and current[1] is proc)


def _remember_unpersisted_run(app_id, token, proc, identity):
    """Keep a started Job controllable if config persistence is unavailable.

    The normal recovery path is still the schema v2 runInstance on disk. This
    in-memory record is the last safety net for transient disk/write failures;
    it lets this console process report and stop the exact Job until the exit
    watcher drains it.
    """
    if not app_id or not token or proc is None:
        return None
    owner = proc
    instance = identity.get("runInstance")
    if isinstance(instance, dict):
        owner = _remember_run_job(
            {"id": app_id, "runInstance": instance}, proc, "unpersisted")
        owner = owner or proc
    with RETAINED_RUN_JOBS_LOCK:
        UNPERSISTED_RUNS[app_id] = {
            "token": token,
            "proc": owner,
            "identity": dict(identity),
        }
    return owner


def _recovery_can_fill_identity(app, token):
    """Allow recovery to fill missing identity without replacing a newer run."""
    if not isinstance(app, dict) or app.get("runToken") not in (None, token):
        return False
    instance = app.get("runInstance")
    if not isinstance(instance, dict):
        return True
    return instance.get("runId") in (None, token)


def _durable_identity_matches(app, token):
    """Return whether both persisted run identity fields describe ``token``."""
    if not isinstance(app, dict) or app.get("runToken") != token:
        return False
    instance = app.get("runInstance")
    return (not isinstance(instance, dict)
            or not instance.get("runId")
            or instance.get("runId") == token)


def _hydrate_unpersisted_run(app):
    """Overlay an unpersisted live run onto a config snapshot, if still current."""
    if not isinstance(app, dict):
        return None
    app_id = app.get("id")
    with RETAINED_RUN_JOBS_LOCK:
        recovery = UNPERSISTED_RUNS.get(app_id)
        if recovery:
            recovery = dict(recovery)
            recovery["identity"] = dict(recovery.get("identity") or {})
    if not recovery:
        return None
    if not _recovery_can_fill_identity(app, recovery.get("token")):
        return None
    app.update(recovery["identity"])
    return recovery.get("proc")


def _is_unpersisted_run(app, proc):
    instance = app.get("runInstance") if isinstance(app, dict) else None
    run_id = instance.get("runId") if isinstance(instance, dict) else None
    if not run_id:
        return False
    with RETAINED_RUN_JOBS_LOCK:
        recovery = UNPERSISTED_RUNS.get(app.get("id"))
    return bool(recovery and recovery.get("token") == run_id
                and recovery.get("proc") is proc)


def _forget_unpersisted_run(app_id, token, proc=None):
    with RETAINED_RUN_JOBS_LOCK:
        recovery = UNPERSISTED_RUNS.get(app_id)
        if (not recovery or recovery.get("token") != token
                or (proc is not None and recovery.get("proc") is not proc)):
            return
        UNPERSISTED_RUNS.pop(app_id, None)
    _forget_run_job(
        {"id": app_id, "runInstance": {"runId": token}}, proc)


def _sweep_retained_run_jobs():
    """Release retained Job handles and keepers once their process trees drain.

    A retained handle is used only when anchor repair or cleanup cannot yet be
    persisted/completed. The associated service may exit later, including
    after its card has been removed from config, so sweep the small in-memory
    set independently of the current app list.
    """
    with RUN_JOB_ACCESS_LOCK:
        with RETAINED_RUN_JOBS_LOCK:
            retained = list(RETAINED_RUN_JOBS.items())
        for (app_id, run_id), (mode, proc) in retained:
            if mode == "unpersisted":
                with WATCHER_LOCK:
                    watcher_active = (app_id, run_id) in ACTIVE_EXIT_WATCHERS
                if watcher_active:
                    continue
            try:
                if proc.members():
                    continue
            except Exception as exc:
                LOG.debug("应用 %s 的保留 Job 仍待清理: %s", app_id, exc)
                continue
            if mode == "unpersisted":
                _forget_unpersisted_run(app_id, run_id, proc)
                continue
            _forget_run_job(
                {"id": app_id, "runInstance": {"runId": run_id}}, proc)


def _cleanup_retained_run_job_after_exit(app_id, run_id):
    """Stop a replacement keeper before persisting this run as exited."""
    app = {"id": app_id, "runInstance": {"runId": run_id}}
    key = (app_id, run_id)
    with RUN_JOB_ACCESS_LOCK:
        with RETAINED_RUN_JOBS_LOCK:
            retained = RETAINED_RUN_JOBS.get(key)
        if not retained:
            return True
        proc = retained[1]
        try:
            if proc.members():
                return False
        except Exception as exc:
            _remember_run_job(app, proc, "empty-cleanup")
            LOG.warning("应用 %s 已退出，但 Job keeper 清理待重试: %s",
                        app_id, exc)
            return False
        _forget_run_job(app, proc)
        return True


def _is_anchor_cleanup_failure(exc):
    return isinstance(exc, OSError) and "Job Object 保活进程" in str(exc)


def _open_run_job_unlocked(app):
    """Reopen this user's named Job Object for a structured run instance."""
    recovered = _hydrate_unpersisted_run(app)
    if recovered is not None:
        if getattr(recovered, "_closed", False):
            _forget_unpersisted_run(
                app.get("id"), (app.get("runInstance") or {}).get("runId"),
                recovered)
            return None
        return recovered
    key = _run_job_key(app)
    # A retained cleanup handle must be retried even after the watcher has
    # marked the run exited. Otherwise the exit-state guard would strand the
    # keeper forever.
    if key is not None:
        with RETAINED_RUN_JOBS_LOCK:
            retained = RETAINED_RUN_JOBS.get(key)
        if retained and retained[0] == "empty-cleanup":
            proc = retained[1]
            try:
                if proc.members():
                    _remember_run_job(app, proc, "repair")
                    return proc
                _forget_run_job(app, proc)
                return None
            except Exception as exc:
                LOG.warning("应用 %s 的空 Job keeper 清理待重试: %s",
                            app.get("id"), exc)
                return RUN_JOB_REOPEN_FAILED

    instance = app.get("runInstance")
    if (not isinstance(instance, dict)
            or instance.get("processState") == "exited"
            or not instance.get("runId") or not instance.get("jobName")):
        return None
    if not isinstance(SELF_UID, str) or not SELF_UID.startswith("S-"):
        return RUN_JOB_REOPEN_FAILED
    if key is not None:
        with RETAINED_RUN_JOBS_LOCK:
            retained = RETAINED_RUN_JOBS.get(key)
        if retained:
            mode, proc = retained
            anchor_handle = getattr(proc, "_anchor_handle", None)
            try:
                anchor_live = bool(
                    anchor_handle and
                    proc._api.poll_process(anchor_handle) is None)
            except Exception:
                anchor_live = True
            if anchor_live and not getattr(proc, "_closed", False):
                return proc
            # 原 keeper 已退出时尝试换一个。若此操作失败，继续用仍然有效的
            # Job handle 管理当前服务，避免丢掉唯一可控身份。
    try:
        proc = windows_runtime.reopen(
            instance["runId"], job_name=instance.get("jobName"),
            root_pid=instance.get("rootPid"),
            root_create_time=instance.get("rootCreateTime"), sid=SELF_UID,
            anchor_pid=instance.get("anchorPid"),
            anchor_create_time=instance.get("anchorCreateTime"))
        previous_proc = (retained[1]
                         if key is not None and 'retained' in locals()
                         and retained else None)
        if previous_proc is not None and previous_proc is not proc:
            # Access is serialized by RUN_JOB_ACCESS_LOCK. Close the stale
            # keeper first, then retain the replacement before any caller can
            # release it (notably start_app_transaction's duplicate-start
            # cleanup path).
            _forget_run_job(app, previous_proc)
        if proc is not None and proc is not previous_proc:
            proc = _remember_run_job(app, proc, "repair") or proc
        return proc
    except windows_runtime.JobAnchorCleanupError as exc:
        cleanup = getattr(exc, "managed_process", None)
        if cleanup is not None:
            _remember_run_job(app, cleanup, "empty-cleanup")
        LOG.debug("无法清理应用 %s 的空 Job keeper: %s",
                  app.get("id"), exc)
        return RUN_JOB_REOPEN_FAILED
    except (OSError, ValueError, TypeError) as exc:
        if key is not None and 'retained' in locals() and retained:
            return retained[1]
        LOG.debug("无法重连应用 %s 的 Job Object: %s", app.get("id"), exc)
        return RUN_JOB_REOPEN_FAILED


def _open_run_job(app):
    with RUN_JOB_ACCESS_LOCK:
        return _open_run_job_unlocked(app)


def observed_process_pid(app, listeners=None, snap=None, cwds=None):
    """Resolve an observation card's current listener without granting control."""
    if app.get("controlMode") != "monitor":
        return None
    observation = app.get("observation")
    port = observation_port(observation) or app.get("port")
    cwd = (observation.get("cwd") if isinstance(observation, dict) else None)
    if not isinstance(port, int) or port <= 0 or not cwd:
        return None
    if listeners is None:
        listeners = scan_listeners()
    pids = {pid for pid, listening_port in listeners
            if listening_port == port}
    if not pids:
        return None
    if snap is None:
        snap = ps_snapshot(pids, with_uid=True)
    if cwds is None:
        cwds = lsof_cwds(pids)
    expected_pid = observation.get("pid") if isinstance(observation, dict) else None
    expected_ctime = (observation.get("createTime")
                       if isinstance(observation, dict) else None)
    matches = []
    for pid in sorted(pids):
        info = snap.get(pid, {})
        if not is_current_user(info.get("uid")):
            continue
        expected_sid = (observation.get("sid")
                        if isinstance(observation, dict) else None)
        if expected_sid and info.get("uid") != expected_sid:
            continue
        if pid == expected_pid and expected_ctime is not None:
            current_ctime = info.get("ctime")
            if (current_ctime is None or current_ctime != expected_ctime):
                continue
        actual_cwd = cwds.get(pid)
        if not actual_cwd:
            continue
        try:
            if os.path.normcase(os.path.realpath(actual_cwd)) == os.path.normcase(
                    os.path.realpath(cwd)):
                matches.append(pid)
        except OSError:
            continue
    if expected_pid in matches:
        return expected_pid
    return matches[0] if len(matches) == 1 else None


def _managed_process_index_unlocked(apps, groups=None, anchor_repairs=None,
                                    unavailable_jobs=None):
    """批量校验应用的受控进程，返回 (appId -> [pid], ps, groups)。

    必须同时满足：属于记录的进程组、属于当前用户、argv 中带本次启动的
    随机 token。即使 PID/PGID 被系统复用，也不会把无关进程当成应用或停止它。
    """
    _sweep_retained_run_jobs()
    if groups is None:
        needs_groups = any(
            app.get("runToken")
            and not ((app.get("runInstance") or {}).get("jobName"))
            and isinstance(app.get("lastPgid") or app.get("lastPid"), int)
            for app in apps)
        groups = pgid_members_map() if needs_groups else {}
    candidates = {}
    all_pids = set()
    for app in apps:
        job = _open_run_job(app)
        if job is RUN_JOB_REOPEN_FAILED:
            if unavailable_jobs is not None:
                unavailable_jobs.add(app.get("id"))
            pids = set()
        elif job is not None:
            instance = app.get("runInstance") or {}
            anchor_pid = getattr(job, "anchor_pid", None)
            anchor_ctime = getattr(job, "anchor_create_time", None)
            anchor_changed = (
                instance.get("anchorPid") != anchor_pid
                or instance.get("anchorCreateTime") != anchor_ctime)
            try:
                pids = set(job.members())
            except Exception as exc:
                LOG.warning("读取应用 %s 的 Job Object 成员失败，保留身份待重试: %s",
                            app.get("id"), exc)
                if unavailable_jobs is not None:
                    unavailable_jobs.add(app.get("id"))
                if _is_anchor_cleanup_failure(exc):
                    _remember_run_job(app, job, "empty-cleanup")
                else:
                    _release_run_job_handle(app, job)
                pids = set()
            else:
                if not pids:
                    _release_run_job_handle(app, job)
                elif anchor_changed and instance.get("processState") == "stopping":
                    # Let the stop transaction own this exact handle; writing a
                    # replacement identity mid-stop could race its Job calls.
                    _remember_run_job(app, job, "repair")
                elif anchor_changed:
                    repair = {
                        "id": app.get("id"),
                        "runId": instance.get("runId"),
                        "jobName": instance.get("jobName"),
                        "anchorPid": anchor_pid,
                        "anchorCreateTime": anchor_ctime,
                        "managedProcess": job,
                    }
                    if anchor_repairs is not None:
                        anchor_repairs.append(repair)
                    _remember_run_job(app, job, "repair")
                else:
                    _release_run_job_handle(app, job)
        elif app.get("controlMode") == "monitor":
            pids = set()
        elif ((app.get("runInstance") or {}).get("jobName")):
            # A structured run is owned only by its named Job Object. If that
            # boundary cannot be reopened, never infer ownership from PPID or
            # a command-line token.
            pids = set()
        else:
            pids = _managed_candidates(app, groups)
        candidates[app.get("id")] = pids
        all_pids.update(pids)
    snap = ps_snapshot(all_pids, with_uid=True) if all_pids else {}
    result = {}
    for app in apps:
        token = app.get("runToken")
        current_user = sorted(
            pid for pid in candidates.get(app.get("id"), set())
            if is_current_user(snap.get(pid, {}).get("uid")))
        instance = app.get("runInstance")
        if (isinstance(instance, dict) and instance.get("jobName")):
            # Membership in a SID protected Job Object is the ownership
            # boundary. Run tokens remain useful diagnostics but are not proof.
            result[app.get("id")] = current_user
            continue
        marker = RUN_TOKEN_ARG_PREFIX + token if token else None
        controller_found = bool(marker and any(
            marker in snap.get(pid, {}).get("args", "") for pid in current_user))
        # 随机标记在进程组的常驻外层 shell 上；校验后整组均为受控后代。
        result[app.get("id")] = current_user if controller_found else []
    return result, snap, groups


def managed_process_index(apps, groups=None, anchor_repairs=None,
                          unavailable_jobs=None):
    with RUN_JOB_ACCESS_LOCK:
        return _managed_process_index_unlocked(
            apps, groups, anchor_repairs, unavailable_jobs)


def managed_pids(app, groups=None):
    index, _, _ = managed_process_index([app], groups)
    return index.get(app.get("id"), [])


def legacy_managed_pid(app, listeners=None, snap=None, cwds=None):
    """识别升级前身份或用户明确认领的外部监听进程。

    普通旧数据仍只接受原 lastPid。明确 ``attached`` 的卡片允许监听子进程
    换 PID，但仍必须在配置端口上按当前 UID + 真实 cwd 唯一命中；因此
    Next/Vite 等重建子进程后不会丢失关联，也不会只凭端口误认其他项目。
    """
    if app.get("controlMode") == "monitor" or app.get("runToken"):
        return None
    # A claimed external process keeps its observation identity when the user
    # later confirms a LaunchSpec. The launch definition may intentionally be
    # edited to a different cwd/port, but that must not rewrite the boundary
    # used to recognize the already-running process.
    observation = attached_observation(app)
    recorded_pid = (observation.get("pid") if observation else
                    app.get("lastPid"))
    port = (observation_port(observation) if observation else None) or app.get("port")
    expected_cwd = ((observation.get("cwd") if observation else None)
                    or app.get("cwd"))
    expected_sid = observation.get("sid") if observation else None
    expected_ctime = (observation.get("createTime")
                      if observation else app.get("lastCreateTime"))
    if (not isinstance(port, int) or port <= 0
            or not isinstance(expected_cwd, str) or not expected_cwd):
        return None
    if listeners is None:
        listeners = scan_listeners()
    port_pids = {pid for pid, listening_port in listeners
                 if listening_port == port}
    if not app.get("attached"):
        if not isinstance(recorded_pid, int) or recorded_pid <= 0:
            return None
        port_pids.intersection_update({recorded_pid})
    if not port_pids:
        return None
    if snap is None:
        snap = ps_snapshot(port_pids, with_uid=True)
    if cwds is None:
        cwds = lsof_cwds(port_pids)
    matches = []
    # PID 创建时间锚点（仅 Windows 记录）：只有仍在验证原 PID 时才比较。
    # 已认领服务允许监听子进程换 PID，新 PID 只要端口、SID、cwd 唯一匹配
    # 就应重新关联；把它拿去和旧 PID 的 ctime 比较会错误地全部排除。
    for pid in sorted(port_pids):
        current_uid = snap.get(pid, {}).get("uid")
        if not is_current_user(current_uid):
            continue
        # An observation is a complete external identity boundary.  Do not
        # let a later listener under the same user (or a stale PID reuse)
        # become the managed process after a LaunchSpec edit.
        if expected_sid and current_uid != expected_sid:
            continue
        if (expected_ctime is not None
                and pid == recorded_pid
                and snap.get(pid, {}).get("ctime") != expected_ctime):
            continue
        actual_cwd = cwds.get(pid)
        if not actual_cwd:
            continue
        try:
            same_cwd = (
                os.path.realpath(actual_cwd) == os.path.realpath(expected_cwd))
        except OSError:
            same_cwd = False
        if same_cwd:
            matches.append(pid)
    if recorded_pid in matches:
        return recorded_pid
    return matches[0] if app.get("attached") and len(matches) == 1 else None


def listener_app_owners(apps, listeners, snap, cwds, groups=None):
    """返回真实受管监听进程的 ``pid -> app`` 映射。

    端口只是配置与网络资源，不能作为进程所有权证明。映射沿用应用状态的
    run token / PGID / UID 校验，并为升级前的进程保留严格 legacy 识别。
    如果异常配置让同一 PID 同时命中多张卡片，则不做关联，避免误导 UI。
    """
    managed, _, _ = managed_process_index(apps, groups)
    candidates = {}
    for app in apps:
        if app.get("controlMode") == "monitor":
            observed = observed_process_pid(app, listeners, snap, cwds)
            live = [observed] if observed else []
        else:
            live = managed.get(app.get("id"), [])
        if not live and app.get("controlMode") != "monitor":
            legacy_pid = legacy_managed_pid(app, listeners, snap, cwds)
            live = [legacy_pid] if legacy_pid else []
        for pid in live:
            candidates.setdefault(pid, []).append(app)
    return {
        pid: owners[0]
        for pid, owners in candidates.items()
        if len(owners) == 1
    }


def build_apps(cfg, listeners, groups=None, attached_repairs=None,
               anchor_repairs=None, observation_repairs=None):
    """token 校验通过或严格命中旧版身份的进程才算 running。

    多张卡片可共享配置端口；只有当前真实监听者不属于本卡片时才返回
    “端口被其他进程占用”，不再把任意监听者误当成应用本身。
    """
    port_map = {}
    for pid, port in listeners:
        port_map.setdefault(port, []).append(pid)
    apps_cfg = cfg.get("apps") or []
    unavailable_jobs = set()
    managed, snap, _ = managed_process_index(
        apps_cfg, groups, anchor_repairs=anchor_repairs,
        unavailable_jobs=unavailable_jobs)
    listen_by_pid = {}
    for pid, port in listeners:
        listen_by_pid.setdefault(pid, []).append(port)
    configured_ports = {
        app["port"] for app in apps_cfg if app.get("port")}

    # 端口诊断需要展示占用者的真实身份，一次批量取详情，避免逐卡 ps。
    configured_listener_pids = {
        pid for port in configured_ports for pid in port_map.get(port, [])}
    listener_snap = (ps_snapshot(configured_listener_pids, with_uid=True)
                     if configured_listener_pids else {})
    listener_cwds = lsof_cwds(configured_listener_pids)
    verified_owner = listener_app_owners(
        apps_cfg, listeners, listener_snap, listener_cwds)

    apps = []
    for app in apps_cfg:
        control_mode = app.get("controlMode") or "managed"
        managed_live = managed.get(app["id"], [])
        if control_mode == "monitor":
            observed_pid = observed_process_pid(
                app, listeners, listener_snap, listener_cwds)
            live = [observed_pid] if observed_pid else []
            legacy_pid = None
        else:
            legacy_pid = None if managed_live else legacy_managed_pid(
                app, listeners, listener_snap, listener_cwds)
            if (legacy_pid and
                    (verified_owner.get(legacy_pid) or {}).get("id") != app.get("id")):
                legacy_pid = None
            live = managed_live or ([legacy_pid] if legacy_pid else [])
        if (attached_repairs is not None and legacy_pid
                and app.get("attached") and not app.get("runToken")
                and legacy_pid != app.get("lastPid")):
            # 仅在已认领卡片命中唯一替代监听 PID 时修复身份。调用方会在
            # 状态快照构建结束后再原子写入，避免在扫描过程中持有配置锁。
            attached_repairs.append({
                "id": app.get("id"),
                "recordedPid": app.get("lastPid"),
                "port": app.get("port"),
                "cwd": app.get("cwd"),
                "pid": legacy_pid,
                "ctime": (listener_snap.get(legacy_pid) or {}).get("ctime"),
            })
        lp = app.get("lastPid")
        pid = lp if lp in live else (live[0] if live else None)
        port = app.get("port")
        observation = app.get("observation")
        if control_mode == "monitor" and isinstance(observation, dict):
            observation = dict(observation)
            if observed_pid:
                info = listener_snap.get(observed_pid) or {}
                old_observation = app.get("observation") or {}
                current_ctime = info.get("ctime")
                observation.update({
                    "pid": observed_pid,
                    "createTime": (current_ctime
                                    if current_ctime is not None
                                    else old_observation.get("createTime")),
                    "sid": info.get("uid") or old_observation.get("sid"),
                    "cwd": listener_cwds.get(observed_pid)
                    or old_observation.get("cwd"),
                    "ports": [port] if isinstance(port, int) else [],
                    "observedAt": int(time.time()),
                })
                # Avoid rewriting config.json on every poll. A later CAS repair
                # persists only a changed listener identity; the response still
                # exposes the current observation timestamp immediately.
                if (observation_repairs is not None
                        and any(observation.get(key) != old_observation.get(key)
                                for key in ("pid", "createTime", "sid", "cwd", "ports"))):
                    observation_repairs.append({
                        "id": app.get("id"),
                        "port": port,
                        "recordedObservation": dict(old_observation),
                        "observation": dict(observation),
                    })
        configured_listeners = port_map.get(port, []) if port else []
        listening = bool(port and any(p in live for p in configured_listeners))
        occupied = bool(port and configured_listeners and not listening)
        owner_pid = configured_listeners[0] if occupied else None
        owner_info = listener_snap.get(owner_pid, {}) if owner_pid else {}
        owner_app = verified_owner.get(owner_pid)
        owner_cwd = listener_cwds.get(owner_pid) if owner_pid else None
        port_owner = None
        if owner_pid:
            comm = owner_info.get("comm") or ""
            port_owner = {
                "pid": owner_pid,
                "openHost": listener_open_host(
                    listeners, port, {owner_pid}),
                "name": os.path.basename(comm) or "?",
                "cmd": owner_info.get("args") or comm,
                "cwd": owner_cwd,
                "project": project_name(owner_cwd),
                "uid": owner_info.get("uid"),
                "currentUser": is_current_user(owner_info.get("uid")),
                "uptimeSec": owner_info.get("etime"),
                "appId": owner_app.get("id") if owner_app else None,
                "appName": owner_app.get("name") if owner_app else None,
            }
        actual_ports = sorted({p for member in live
                               for p in listen_by_pid.get(member, [])})
        open_hosts = {
            str(actual_port): listener_open_host(
                listeners, actual_port, set(live))
            for actual_port in actual_ports
        }
        try:
            health = inspect_app_health(app)
        except Exception as exc:
            LOG.warning("检查应用配置失败（%s）：%s", app.get("id"), exc)
            health = {"status": "unknown", "blocking": False, "issues": []}
        instance = app.get("runInstance")
        persisted_process_state = (
            instance.get("processState") if isinstance(instance, dict) else None)
        if control_mode == "monitor":
            process_state = "alive" if live else "absent"
        elif persisted_process_state in ("starting", "stopping", "exited"):
            process_state = persisted_process_state
        elif app.get("id") in unavailable_jobs:
            # Do not turn an access/reopen failure into a false exited state.
            # The start endpoint also rejects this unresolved identity.
            process_state = persisted_process_state or "absent"
        elif (not live and isinstance(instance, dict)
              and instance.get("jobName")):
            # After a console restart there is no exit watcher attached to the
            # old process handle. An empty/missing named Job Object is still a
            # reliable signal that this structured run has ended.
            process_state = "exited"
        elif live:
            process_state = "alive"
        else:
            process_state = "absent"
        public_instance = dict(instance) if isinstance(instance, dict) else instance
        if isinstance(public_instance, dict):
            public_instance["processState"] = process_state

        apps.append({
            "id": app["id"], "name": app["name"], "command": app["command"],
            "cwd": app.get("cwd"), "port": port,
            "emoji": app.get("emoji"), "glyph": app.get("glyph"), "icon": app.get("icon"),
            "favicon": app.get("favicon"),
            "running": bool(live), "pid": pid,
            "uptimeSec": ((snap.get(pid) or listener_snap.get(pid) or {}).get("etime")
                          if pid else None),
            "kind": app.get("kind") or "service",
            "attached": bool(app.get("attached")),
            "controlMode": control_mode,
            "processState": process_state,
            "identityUnavailable": app.get("id") in unavailable_jobs,
            "readiness": (
                "failed" if process_state == "exited"
                and app.get("readinessState") == "checking"
                else app.get("readinessState") or "unknown"
                if control_mode == "managed" else "unknown"),
            "readinessState": (
                "failed" if process_state == "exited"
                and app.get("readinessState") == "checking"
                else app.get("readinessState") or "unknown"
                if control_mode == "managed" else "unknown"),
            "identityStrength": (
                "observation" if control_mode == "monitor"
                else "job" if (app.get("runInstance") or {}).get("jobName")
                and managed_live else "legacy-tree" if legacy_pid else "job"
                if managed_live else "legacy-tree" if app.get("runToken")
                else "observation" if live else "job"),
            "launchConfigured": bool(app.get("launchConfigured")),
            "launchSpec": app.get("launchSpec"),
            "runInstance": public_instance,
            "observation": observation,
            "lastExit": public_last_exit(app),
            "health": health,
            "ports": actual_ports,
            "openHosts": open_hosts,
            "listening": listening,
            "portOccupied": occupied,
            "portOccupiedPid": configured_listeners[0] if occupied else None,
            "portOwner": port_owner,
            # 多张停止卡片可以共享常见开发端口；只有真正启动时的监听占用
            # 才是冲突。字段保留给旧前端兼容，但不再表示配置重复。
            "portConflict": False,
            "portConflictApps": [],
            "legacyManaged": bool(legacy_pid),
        })
    return apps


def repair_attached_app_identities(cfg, repairs):
    """持久化已认领服务的唯一替代监听 PID。

    ``build_apps`` 使用的是配置快照。落盘前再次确认卡片仍处于同一旧身份，
    避免状态刷新与用户手动认领、启动或编辑发生竞争时覆盖较新的操作。
    """
    if not repairs:
        return False

    def op(data):
        changed = False
        for repair in repairs:
            target = find_app(data, repair.get("id"))
            if (not target or not target.get("attached")
                    or target.get("runToken")
                    or target.get("lastPid") != repair.get("recordedPid")
                    or target.get("port") != repair.get("port")
                    or target.get("cwd") != repair.get("cwd")):
                continue
            if (target.get("lastPid") != repair.get("pid")
                    or target.get("lastCreateTime") != repair.get("ctime")):
                target["lastPid"] = repair.get("pid")
                target["lastCreateTime"] = repair.get("ctime")
                changed = True
        return changed

    try:
        return bool(cfg.update(op))
    except OSError as exc:
        # 身份展示仍基于本轮可信快照；只是在只读配置等故障时无法修复锚点。
        LOG.warning("无法持久化已认领服务的 PID 重关联: %s", exc)
        return False


def repair_run_instance_anchors(cfg, repairs):
    with RUN_JOB_ACCESS_LOCK:
        return _repair_run_instance_anchors_unlocked(cfg, repairs)


def _repair_run_instance_anchors_unlocked(cfg, repairs):
    """Persist a replacement Job Object keeper after PID reuse or keeper loss."""
    if not repairs:
        return False

    def op(data):
        changed = False
        for repair in repairs:
            target = find_app(data, repair.get("id"))
            instance = target.get("runInstance") if target else None
            if (not isinstance(instance, dict)
                    or instance.get("runId") != repair.get("runId")
                    or instance.get("jobName") != repair.get("jobName")
                    or instance.get("processState") in ("exited", "stopping")):
                continue
            if (instance.get("anchorPid") != repair.get("anchorPid")
                    or instance.get("anchorCreateTime") !=
                    repair.get("anchorCreateTime")):
                instance["anchorPid"] = repair.get("anchorPid")
                instance["anchorCreateTime"] = repair.get("anchorCreateTime")
                changed = True
        return changed

    try:
        updated = bool(cfg.update(op))
    except Exception as exc:
        LOG.warning("无法持久化 Job Object 保活进程身份: %s", exc)
        updated = False

    # A newly created keeper must have exactly one in-process owner until the
    # Job drains. If its identity could not be written, reuse this handle on
    # the next poll rather than opening another keeper per /api/state. When
    # persisted successfully, keep the handle until exit cleanup so a watcher
    # holding the old anchor cannot strand the replacement keeper.
    snapshot = None
    try:
        snapshot = cfg.snapshot()
    except Exception:
        if updated:
            LOG.exception("确认 Job Object keeper 身份落盘失败")
    for repair in repairs:
        proc = repair.get("managedProcess")
        if proc is None:
            continue
        app = {
            "id": repair.get("id"),
            "runInstance": {
                "runId": repair.get("runId"),
                "jobName": repair.get("jobName"),
            },
        }
        if snapshot is None:
            _remember_run_job(app, proc, "repair")
            continue
        target = find_app(snapshot, repair.get("id")) if snapshot else None
        instance = target.get("runInstance") if target else None
        persisted = (
            isinstance(instance, dict)
            and instance.get("runId") == repair.get("runId")
            and instance.get("jobName") == repair.get("jobName")
            and instance.get("anchorPid") == repair.get("anchorPid")
            and instance.get("anchorCreateTime") ==
            repair.get("anchorCreateTime")
            and instance.get("processState") not in ("exited", "stopping"))
        if persisted:
            # Keep the repaired keeper handle in-process while this run is
            # alive. The original exit watcher owns the pre-repair handle, so
            # without this reference the replacement keeper could outlive the
            # application after the watcher marks the run exited.
            _remember_run_job(app, proc, "repair")
        elif (target and isinstance(instance, dict)
              and instance.get("runId") == repair.get("runId")
              and instance.get("processState") == "stopping"):
            # stop_app_and_wait will consume this same handle under the
            # RUN_JOB_ACCESS_LOCK; leave it open for that transaction.
            _remember_run_job(app, proc, "repair")
        elif target and isinstance(instance, dict) and (
                instance.get("runId") == repair.get("runId")
                and instance.get("processState") not in ("exited", "stopping")):
            _remember_run_job(app, proc, "repair")
        else:
            # This identity was replaced or removed while the repair was being
            # committed. It no longer owns the saved application lifecycle.
            try:
                stop_anchor = getattr(proc, "_stop_anchor", None)
                if callable(stop_anchor) and not stop_anchor():
                    _remember_run_job(app, proc, "empty-cleanup")
                    continue
            except Exception:
                LOG.exception("清理过期 Job Object keeper 失败（应用 %s）",
                              repair.get("id"))
                _remember_run_job(app, proc, "empty-cleanup")
                continue
            _forget_run_job(app, proc)
    return updated


def repair_observation_identities(cfg, repairs):
    """Persist a monitor card's replacement listener identity with CAS."""
    if not repairs:
        return False

    def op(data):
        changed = False
        for repair in repairs:
            target = find_app(data, repair.get("id"))
            if (not target or target.get("controlMode") != "monitor"
                    or target.get("port") != repair.get("port")
                    or target.get("observation") !=
                    repair.get("recordedObservation")):
                continue
            observation = repair.get("observation")
            if not isinstance(observation, dict):
                continue
            target["observation"] = dict(observation)
            # Keep legacy fields coherent for migration and diagnostics. They
            # never grant control to a monitor card.
            target["lastPid"] = observation.get("pid")
            target["lastCreateTime"] = observation.get("createTime")
            changed = True
        return changed

    try:
        return bool(cfg.update(op))
    except OSError as exc:
        LOG.warning("无法持久化监控卡片的最新进程身份: %s", exc)
        return False


def build_state(cfg, console_port, config_health=None):
    degraded_reasons = []
    # 一次 pgid 快照供 build_services / build_apps 共享，避免每轮两次全量 ps。
    needs_groups = any(
        app.get("runToken")
        and isinstance(app.get("lastPgid") or app.get("lastPid"), int)
        for app in cfg.get("apps") or [])
    groups = pgid_members_map() if needs_groups else None
    try:
        services, listeners = build_services(cfg, groups)
    except Exception:
        LOG.exception("构建服务监控状态失败")
        services, listeners = [], set()
        degraded_reasons.append({"component": "services"})
    try:
        watched = build_watched(cfg.get("watchedKeywords"))
    except Exception:
        LOG.exception("构建关注进程状态失败")
        watched = []
        degraded_reasons.append({"component": "watched"})
    try:
        attached_repairs = []
        anchor_repairs = []
        observation_repairs = []
        apps = build_apps(cfg, listeners, groups, attached_repairs,
                          anchor_repairs, observation_repairs)
    except Exception:
        LOG.exception("构建启动台状态失败")
        apps = []
        degraded_reasons.append({"component": "apps"})
    if VERSION_LOAD_ERROR:
        degraded_reasons.append(
            {"component": "version", "error": VERSION_LOAD_ERROR})
    for issue in (config_health or {}).get("issues", []):
        degraded_reasons.append({"component": "config", "error": issue})
    state = {
        "services": services,
        "watched": watched,
        "apps": apps,
        "watchedKeywords": cfg.get("watchedKeywords") or [],
        "consolePort": console_port,
        "consolePid": SELF_PID,
        "consoleCwd": BASE_DIR,
        "dataDir": DATA_DIR,
        "logsDir": LOGS_DIR,
        "version": APP_VERSION,
        "schemaVersion": cfg.get("schemaVersion", CURRENT_SCHEMA_VERSION),
        "degraded": bool(degraded_reasons),
        "degradedReasons": degraded_reasons,
        "configHealth": dict(config_health or {}),
        "uiTheme": cfg.get("uiTheme") or DEFAULT_UI_THEME,
        "openBrowser": bool(cfg.get("openBrowser", True)),
        "themes": list_themes(),
        # CPU 为「占全部核心百分比」（任务管理器口径），coreCount 供前端
        # 把迷你条还原为相对满核宽度。
        "coreCount": sysops.core_count(),
    }
    # 仅在有可修复身份时附带内部字段；_refresh_state 在序列化前取走它。
    if "attached_repairs" in locals() and attached_repairs:
        state["_attachedRepairs"] = attached_repairs
    if "anchor_repairs" in locals() and anchor_repairs:
        state["_anchorRepairs"] = anchor_repairs
    if "observation_repairs" in locals() and observation_repairs:
        state["_observationRepairs"] = observation_repairs
    # Keep the expensive process scan cached, while rebuilding cards from
    # the current on-disk configuration for each response.
    state["_listeners"] = set(listeners)
    state["_groups"] = groups
    return state


# ---------------------------------------------------------------- 状态快照缓存
# 进程与端口扫描在 Windows 上也可能耗时数秒，因此只缓存这一层，并采用
# stale-while-revalidate：过期时立即复用上一份扫描结果，后台最多一个刷新。
# 启动台卡片、主题等配置每次响应都从磁盘 config.json 重建，避免内存/缓存
# 空列表盖住真实卡片。任何配置读取和慢扫描都不得发生在缓存锁内，避免与
# Config.update 形成锁顺序反转。
STATE_CACHE_TTL = 2.2  # 秒
STATE_CACHE_INITIAL_WAIT = 15.0
_state_cache_lock = threading.Lock()
_state_cache_ready = threading.Condition(_state_cache_lock)
_state_cache = {
    "mono": 0.0,
    "state": None,
    "listeners": None,
    "groups": None,
    "building": False,
    "generation": 0,
}


def invalidate_state_cache():
    with _state_cache_ready:
        # 保留上一份快照供轮询立即读取，但将其标记为过期。generation
        # 防止配置变更前启动的刷新被误标为最新结果。
        _state_cache["mono"] = 0.0
        _state_cache["generation"] = _state_cache.get("generation", 0) + 1
        _state_cache_ready.notify_all()


def _copy_cached_scan_locked():
    """Copy cached process-scan fields; caller must hold _state_cache_ready."""
    listeners = _state_cache.get("listeners")
    groups = _state_cache.get("groups")
    listeners_copy = set(listeners) if listeners else set()
    if isinstance(groups, dict):
        groups_copy = {key: list(value) for key, value in groups.items()}
    else:
        groups_copy = groups
    return listeners_copy, groups_copy


def _finish_state_refresh(state, generation, listeners=None, groups=None):
    with _state_cache_ready:
        if (state is not None
                and generation == _state_cache.get("generation", 0)):
            _state_cache["state"] = state
            _state_cache["listeners"] = set(listeners or set())
            _state_cache["groups"] = groups
            _state_cache["mono"] = time.monotonic()
        _state_cache["building"] = False
        _state_cache_ready.notify_all()


def _refresh_state(cfg, console_port, generation, raise_errors=False):
    try:
        # snapshot() 自己从磁盘读配置。Config 锁与缓存锁绝不同时持有。
        cfg_snapshot = cfg.snapshot()
        config_health = cfg.health_info()
        state = build_state(cfg_snapshot, console_port, config_health)
        attached_repairs = state.pop("_attachedRepairs", [])
        anchor_repairs = state.pop("_anchorRepairs", [])
        observation_repairs = state.pop("_observationRepairs", [])
        listeners = state.pop("_listeners", set())
        groups = state.pop("_groups", None)
        repair_attached_app_identities(cfg, attached_repairs)
        repair_run_instance_anchors(cfg, anchor_repairs)
        repair_observation_identities(cfg, observation_repairs)
    except Exception:
        _finish_state_refresh(None, generation)
        if raise_errors:
            raise
        LOG.exception("后台刷新状态快照失败")
        return None
    _finish_state_refresh(state, generation, listeners, groups)
    return state


def _start_state_refresh(cfg, console_port, generation):
    thread = threading.Thread(
        target=_refresh_state,
        args=(cfg, console_port, generation),
        name="console-state-refresh",
        daemon=True)
    try:
        thread.start()
    except Exception:
        _finish_state_refresh(None, generation)
        raise
    return thread


def warm_state_cache(cfg, console_port):
    """服务启动后预热首份快照；HTTP 健康检查不等待慢扫描。"""
    with _state_cache_ready:
        if _state_cache.get("building"):
            return False
        cached = _state_cache.get("state")
        if (cached is not None
                and time.monotonic() - _state_cache.get("mono", 0.0)
                < STATE_CACHE_TTL):
            return False
        generation = _state_cache.get("generation", 0)
        _state_cache["building"] = True
    _start_state_refresh(cfg, console_port, generation)
    return True


def _live_config_object(cfg):
    """Return whether cfg looks like the real filesystem-backed Config."""
    path = getattr(cfg, "path", None)
    return isinstance(path, str) and bool(path)


def _overlay_launchpad_from_disk(cfg, state, listeners=None, groups=None):
    """Rebuild launchpad cards from disk while keeping cached process scans.

    This function intentionally runs outside the state-cache lock: snapshot()
    takes the Config lock, while Config.update() invalidates the state cache
    after releasing that lock.
    """
    if not _live_config_object(cfg) or not isinstance(state, dict):
        return state
    try:
        snapshot = cfg.snapshot()
        health = cfg.health_info()
    except Exception:
        LOG.exception("读取磁盘配置以覆盖启动台失败")
        return state
    overlaid = dict(state)
    try:
        anchor_repairs = []
        observation_repairs = []
        overlaid["apps"] = build_apps(
            snapshot, listeners or set(), groups,
            anchor_repairs=anchor_repairs,
            observation_repairs=observation_repairs)
        repair_run_instance_anchors(cfg, anchor_repairs)
        repair_observation_identities(cfg, observation_repairs)
    except Exception:
        LOG.exception("按磁盘配置重建启动台失败")
        overlaid["apps"] = list(snapshot.get("apps") or [])
    overlaid["uiTheme"] = snapshot.get("uiTheme") or DEFAULT_UI_THEME
    overlaid["openBrowser"] = bool(snapshot.get("openBrowser", True))
    overlaid["watchedKeywords"] = list(snapshot.get("watchedKeywords") or [])
    overlaid["schemaVersion"] = snapshot.get(
        "schemaVersion", CURRENT_SCHEMA_VERSION)
    if isinstance(health, dict):
        overlaid["configHealth"] = dict(health)
    return overlaid


def get_state_snapshot(cfg, console_port):
    now = time.monotonic()
    build_here = False
    start_background = False
    cached = None
    listeners = set()
    groups = None
    generation = None
    with _state_cache_ready:
        cached = _state_cache.get("state")
        listeners, groups = _copy_cached_scan_locked()
        if (cached is not None
                and now - _state_cache.get("mono", 0.0) < STATE_CACHE_TTL):
            pass
        elif cached is not None:
            if not _state_cache.get("building"):
                generation = _state_cache.get("generation", 0)
                _state_cache["building"] = True
                start_background = True
        elif not _state_cache.get("building"):
            generation = _state_cache.get("generation", 0)
            _state_cache["building"] = True
            build_here = True
        else:
            deadline = time.monotonic() + STATE_CACHE_INITIAL_WAIT
            while (_state_cache.get("state") is None
                   and _state_cache.get("building")):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                _state_cache_ready.wait(remaining)
            cached = _state_cache.get("state")
            listeners, groups = _copy_cached_scan_locked()
            if cached is None:
                generation = _state_cache.get("generation", 0)
                _state_cache["building"] = True
                build_here = True

    if start_background:
        _start_state_refresh(cfg, console_port, generation)
        return _overlay_launchpad_from_disk(cfg, cached, listeners, groups)
    if build_here:
        state = _refresh_state(
            cfg, console_port, generation, raise_errors=True)
        with _state_cache_ready:
            listeners, groups = _copy_cached_scan_locked()
        return _overlay_launchpad_from_disk(cfg, state, listeners, groups)
    return _overlay_launchpad_from_disk(cfg, cached, listeners, groups)


def build_health(cfg):
    """不执行 ps/lsof 的轻量健康检查。"""
    # snapshot() first refreshes the filesystem-backed Config. Returning
    # health_info() from before that refresh can falsely report zero cards to
    # the launcher and trigger replacement of a healthy instance.
    snapshot = cfg.snapshot()
    health = cfg.health_info()
    issues = list(health.get("issues") or [])
    if VERSION_LOAD_ERROR:
        issues.append("VERSION 读取失败: %s" % VERSION_LOAD_ERROR)
    for label, path in (("data", DATA_DIR), ("icons", ICONS_DIR),
                        ("logs", LOGS_DIR)):
        if not os.path.isdir(path):
            issues.append("%s 目录不存在" % label)
        elif not os.access(path, os.R_OK | os.W_OK | os.X_OK):
            issues.append("%s 目录不可读写" % label)
        else:
            try:
                mode = os.lstat(path).st_mode
                if stat.S_ISLNK(mode):
                    issues.append("%s 目录不能是符号链接" % label)
            except OSError as e:
                issues.append("无法检查 %s 目录: %s" % (label, e))
    for label, path in (("config", CONFIG_PATH),
                        ("configBackup", CONFIG_PATH + ".bak")):
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            if label == "config":
                issues.append("主配置文件不存在")
            continue
        except OSError as e:
            issues.append("无法检查 %s: %s" % (label, e))
            continue
        if not stat.S_ISREG(mode):
            issues.append("%s 不是普通文件" % label)
    degraded = bool(issues)
    return {
        "ok": not degraded,
        "status": "degraded" if degraded else "ok",
        "version": APP_VERSION,
        "schemaVersion": snapshot.get(
            "schemaVersion", CURRENT_SCHEMA_VERSION),
        "degraded": degraded,
        "issues": issues,
        "config": health,
    }


def list_themes():
    """扫描 static/themes/*.json 主题清单（css 文件必须存在），供注册切换。
    默认主题固定排在首位，其余按文件名排序。"""
    themes = []
    try:
        names = sorted(os.listdir(THEMES_DIR))
    except OSError:
        return themes
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(THEMES_DIR, name), "r", encoding="utf-8") as f:
                meta = json.load(f)
            theme_id = str(meta.get("id") or os.path.splitext(name)[0])
            if not theme_id or not os.path.isfile(
                    os.path.join(THEMES_DIR, theme_id + ".css")):
                continue
            themes.append({
                "id": theme_id,
                "name": str(meta.get("name") or theme_id),
                "author": str(meta.get("author") or ""),
                "desc": str(meta.get("desc") or ""),
                "colors": [str(c) for c in (meta.get("colors") or [])][:6],
            })
        except Exception:
            LOG.exception("读取主题清单失败: %s", name)
    themes.sort(key=lambda t: t["id"] != DEFAULT_UI_THEME)
    return themes


# ---------------------------------------------------------------- 进程/应用操作

def process_uid(pid):
    """返回进程 uid；进程不存在返回 None（Windows 统一视为当前用户）。"""
    return sysops.process_uid(pid)


def kill_process(pid, force):
    """结束单个进程；只允许当前用户的进程。返回 (ok, error)。"""
    if pid == SELF_PID:
        return False, "不能结束总控台自身进程"
    uid = process_uid(pid)
    if uid is None:
        return False, "进程不存在"
    if not is_current_user(uid):
        return False, "只能结束当前用户的进程"
    return sysops.kill_process(pid, force)


def stop_pid_tree(pid, sig=signal.SIGTERM):
    """向受控进程组/进程树发信号；返回 (ok, error)。

    ProcessLookupError means the target completed between validation and the
    signal and is therefore an idempotent success. Permission and other OS
    failures must never be swallowed: callers use them to retain management
    identity instead of creating an orphan process.
    """
    return sysops.signal_group(int(pid), sig)


def app_running(app, listeners=None):
    if app.get("controlMode") == "monitor":
        return False
    return app_identity_state(app, listeners) == "alive"


_DEFAULT_APP_RUNNING = app_running


def app_identity_state(app, listeners=None):
    """Return ``alive``, ``absent`` or conservative ``unknown``.

    A failed Job reopen/member query is deliberately distinct from an empty
    Job. Callers changing or deleting a card must reject ``unknown`` so an
    active service cannot lose its only recovery identity.
    """
    if not isinstance(app, dict) or app.get("controlMode") == "monitor":
        return "absent"
    recovered = _hydrate_unpersisted_run(app)
    if recovered is not None:
        try:
            members = recovered.members()
        except Exception as exc:
            LOG.warning("无法读取应用 %s 的保留 Job 状态: %s", app.get("id"), exc)
            return "unknown"
        return "alive" if members else "absent"
    instance = app.get("runInstance")
    if (isinstance(instance, dict) and instance.get("jobName")
            and instance.get("processState") != "exited"):
        with RUN_JOB_ACCESS_LOCK:
            job = _open_run_job_unlocked(app)
            if job is RUN_JOB_REOPEN_FAILED:
                return "unknown"
            if job is not None:
                try:
                    members = job.members()
                except Exception as exc:
                    if _is_anchor_cleanup_failure(exc):
                        _remember_run_job(app, job, "empty-cleanup")
                    else:
                        _release_run_job_handle(app, job)
                    LOG.warning("读取应用 %s 的 Job Object 状态失败: %s",
                                app.get("id"), exc)
                    return "unknown"
                if members:
                    if not _run_job_is_retained(app, job):
                        _release_run_job_handle(app, job)
                    return "alive"
                if not _run_job_is_retained(app, job):
                    _release_run_job_handle(app, job)
                return "absent"
            # A named Job that is not reopenable is not proof of an active
            # process only when the persisted state is already exited.
            return "absent"
    try:
        if managed_pids(app):
            return "alive"
        if legacy_managed_pid(app, listeners):
            return "alive"
    except Exception as exc:
        LOG.warning("无法验证应用 %s 的旧版进程身份: %s", app.get("id"), exc)
        return "unknown"
    return "absent"


def lifecycle_identity_state(app, listeners=None):
    """Resolve a lifecycle state while retaining the legacy app_running seam.

    Older integrations and tests override ``app_running`` to supply a verified
    legacy identity.  Keep that override usable, but never let it override an
    explicit ``unknown`` Job state, which must remain fail-closed.
    """
    state = app_identity_state(app, listeners)
    # Preserve the historical injectable app_running seam used by API clients
    # and tests without recursing through the production implementation.
    if state == "absent" and app_running is not _DEFAULT_APP_RUNNING:
        try:
            if app_running(app, listeners):
                return "alive"
        except Exception:
            return "unknown"
    if state == "absent" and app_alive_sign is not _DEFAULT_APP_ALIVE_SIGN:
        try:
            if app_alive_sign(app, listeners):
                return "alive"
        except Exception:
            return "unknown"
    return state


def app_alive_sign(app, listeners=None):
    """start/stop 的存活判断：新版 token 或严格校验通过的旧版身份。"""
    return app_running(app, listeners)


_DEFAULT_APP_ALIVE_SIGN = app_alive_sign


def build_launch_env(token, environ=None):
    """无窗口启动时仍可找到常见开发工具的环境。

    pythonw 不会读取用户 shell 配置，因此显式补入 npm/pnpm、NVM、fnm
    与系统目录。
    """
    env = dict(os.environ if environ is None else environ)
    home = os.path.expanduser("~")
    preferred = []
    appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    preferred.extend([
        os.path.join(appdata, "npm"),
        os.path.join(home, "AppData", "Roaming", "npm"),
        os.path.join(home, "AppData", "Local", "pnpm"),
        os.path.join(home, ".bun", "bin"),
        os.path.join(home, ".asdf", "shims"),
    ])
    preferred.extend(sorted(
        glob.glob(os.path.join(home, ".nvm", "versions", "node", "*")),
        reverse=True))
    preferred.extend(sorted(
        glob.glob(os.path.join(home, ".fnm", "node-versions", "*", "installation")),
        reverse=True))
    preferred.extend((env.get("PATH") or "").split(os.pathsep))
    preferred.extend((
        os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32"),
        os.path.join(os.environ.get("SystemRoot") or r"C:\Windows"),
    ))
    seen = set()
    env["PATH"] = os.pathsep.join(
        path for path in preferred if path and not (path in seen or seen.add(path)))
    env[RUN_TOKEN_ENV] = token
    return env


def start_app(app):
    """返回 (ok, error, proc|None, pgid|None, token|None)。"""
    _ensure_private_dir(LOGS_DIR)
    log_path = os.path.join(LOGS_DIR, "%s.log" % app["id"])
    rotate_log_file(log_path)
    cwd = app.get("cwd") or os.path.expanduser("~")
    try:
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                         0o600)
        if hasattr(os, "fchmod"):
            os.fchmod(log_fd, 0o600)
        else:
            os.chmod(log_path, 0o600)
        logf = os.fdopen(log_fd, "ab", buffering=0)
    except OSError as e:
        return False, "无法打开日志文件: %s" % e, None, None, None
    try:
        header = "\n===== 启动于 %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S")
        logf.write(header.encode("utf-8"))
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    spec = app.get("launchSpec")
    try:
        spec = normalize_launch_spec(
            spec, command=app.get("command", ""), cwd=cwd,
            port=app.get("port"))
        env = build_launch_env(token)
        for key, value in spec.get("env", {}).items():
            # Windows environment names are case insensitive; remove an older
            # spelling before applying the user's explicit overlay.
            for previous in list(env):
                if previous.casefold() == key.casefold():
                    del env[previous]
                    break
            env[key] = value
        if spec["mode"] == "legacy-shell":
            proc = windows_runtime.launch(
                command=spec.get("legacyCommand", app.get("command", "")),
                args=(), cwd=cwd, env=env, run_id=token,
                stdout=logf, stderr=subprocess.STDOUT,
                mode="legacy-shell", sid=SELF_UID)
        else:
            proc = windows_runtime.launch(
                executable=spec["executable"], args=spec["args"],
                cwd=spec.get("cwd") or cwd, env=env, run_id=token,
                stdout=logf, stderr=subprocess.STDOUT,
                mode=spec["mode"], sid=SELF_UID)
    except Exception as e:
        logf.close()
        return False, "启动失败: %s" % e, None, None, None
    logf.close()  # 子进程已持有副本，父进程关闭避免 fd 泄漏
    return True, None, proc, proc.pid, token


def startup_failure_message(app_id, code):
    """从日志末尾提取一行可直接显示给用户的启动错误。"""
    text = read_log_tail(app_id, 30)
    for line in reversed(text.splitlines()):
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line).strip()
        if line and not line.startswith("====="):
            if len(line) > 180:
                line = line[:179] + "…"
            return "启动命令立即退出（exit %s）：%s" % (code, line)
    return "启动命令立即退出（exit %s），请查看日志" % code


def _update_config_with_retry(cfg, operation, description, attempts=3):
    """Retry watcher-owned state writes briefly before logging a hard failure."""
    for attempt in range(max(1, int(attempts))):
        try:
            return cfg.update(operation)
        except Exception:
            if attempt + 1 >= attempts:
                LOG.exception("%s：配置写入重试耗尽", description)
                return None
            LOG.warning("%s：配置写入失败，将重试（%d/%d）",
                        description, attempt + 1, attempts, exc_info=True)
            time.sleep(0.1 * (attempt + 1))
    return None


def watch_app_exit(cfg, app_id, proc, token, started_at=None):
    """Wait for the root and every Job Object member, then persist its exit."""
    started_at = time.time() if started_at is None else started_at
    watcher_key = (app_id, token)
    with WATCHER_LOCK:
        if watcher_key in ACTIVE_EXIT_WATCHERS:
            # A duplicate registration owns a different handle and can be
            # closed immediately. The active watcher keeps its own handle
            # until its finally block; never close that same object here. A
            # reopened replacement may already be the retained handle for
            # this run; closing it here would leave RETAINED_RUN_JOBS pointing
            # at a closed object and lose the only recovery boundary.
            active_proc = ACTIVE_EXIT_PROCS.get(watcher_key)
            with RETAINED_RUN_JOBS_LOCK:
                retained = RETAINED_RUN_JOBS.get(watcher_key)
            retained_proc = retained[1] if retained else None
            if active_proc is not proc and retained_proc is not proc:
                close = getattr(proc, "close", None)
                if close:
                    close()
            return None
        ACTIVE_EXIT_WATCHERS.add(watcher_key)
        ACTIVE_EXIT_PROCS[watcher_key] = proc

    def _wait():
        try:
            code = proc.wait()
            # A service may launch a background child and then exit. Wait until
            # the Job Object is empty, so no descendant is left unmanaged.
            wait_for_empty = getattr(proc, "wait_for_empty", None)
            if callable(wait_for_empty):
                wait_for_empty()
            else:
                members_method = getattr(proc, "members", None)
                while callable(members_method):
                    try:
                        members = members_method()
                    except (AttributeError, OSError):
                        break
                    # RuntimeManager returns concrete PID collections. Treat
                    # opaque test doubles and older process wrappers as no
                    # descendants, rather than spinning forever on Mock truth.
                    if not isinstance(members, (list, tuple, set, frozenset)) or not members:
                        break
                    time.sleep(0.05)
            _cleanup_retained_run_job_after_exit(app_id, token)
            ended_at = time.time()
            duration = round(max(0.0, ended_at - started_at), 3)

            with MANUAL_STOP_LOCK:
                manually_stopped = (app_id, token) in MANUAL_STOP_TOKENS

            def op(c):
                target = find_app(c, app_id)
                instance = target.get("runInstance") if target else None
                if target and target.get("runToken") != token:
                    with RETAINED_RUN_JOBS_LOCK:
                        recovery = UNPERSISTED_RUNS.get(app_id)
                        recovery = (dict(recovery) if recovery else None)
                    # A retained identity may fill in a config write that
                    # failed for this same run, but it must never overwrite a
                    # newer run that has since been persisted for the card.
                    # Without the ``is None`` guard, a stale exit watcher for
                    # run T could replace run T2's identity and clear it.
                    if (recovery and recovery.get("token") == token
                            and _recovery_can_fill_identity(target, token)):
                        target.update(recovery.get("identity") or {})
                        instance = target.get("runInstance")
                if (not manually_stopped and target
                        and target.get("lastPid") == proc.pid
                        and _durable_identity_matches(target, token)):
                    last_exit = {
                        "code": code,
                        "at": int(ended_at),
                        "startedAt": int(started_at * 1000),
                        "durationSec": duration,
                    }
                    if (target.get("kind") or "service") == "task":
                        last_exit["status"] = (
                            classify_task_exit(code) if code is not None
                            else "unknown")
                    if code is not None or (target.get("kind") or "service") == "task":
                        target["lastExit"] = last_exit
                    if isinstance(instance, dict) and instance.get("runId") == token:
                        instance["processState"] = "exited"
                        instance["exitResult"] = last_exit
                    if target.get("readinessState") == "checking":
                        target["readinessState"] = "failed"
            _update_config_with_retry(
                cfg, op, "应用 %s 退出状态" % app_id)
            rotate_log_file(os.path.join(LOGS_DIR, "%s.log" % app_id))
        finally:
            close = getattr(proc, "close", None)
            if close:
                close()
            _forget_unpersisted_run(app_id, token, proc)
            with WATCHER_LOCK:
                ACTIVE_EXIT_WATCHERS.discard(watcher_key)
                ACTIVE_EXIT_PROCS.pop(watcher_key, None)
    thread = threading.Thread(target=_wait, daemon=True)
    try:
        thread.start()
    except Exception:
        with WATCHER_LOCK:
            ACTIVE_EXIT_WATCHERS.discard(watcher_key)
            ACTIVE_EXIT_PROCS.pop(watcher_key, None)
        raise
    return thread


def persist_started_app(cfg, app_id, proc, pgid, token):
    """保存新的受控身份并启动退出监视线程。"""
    started_at = time.time()

    # A previous start may have been kept only in UNPERSISTED_RUNS after a
    # transient config-write failure.  Do not replace a still-live recovery
    # record with a new run: that would make the old process uncontrollable.
    # An already-empty/closed record is safe to retire once this run is saved.
    stale_recovery = None
    with RETAINED_RUN_JOBS_LOCK:
        previous = UNPERSISTED_RUNS.get(app_id)
        if previous and previous.get("token") != token:
            stale_recovery = dict(previous)
    if stale_recovery:
        previous_proc = stale_recovery.get("proc")
        try:
            members = (previous_proc.members()
                       if callable(getattr(previous_proc, "members", None))
                       else [])
            if members:
                LOG.warning("应用 %s 仍有未落盘运行实例，拒绝覆盖其恢复身份",
                            app_id)
                return False
        except Exception as exc:
            # A failed membership query is not proof that the old process is
            # gone. Keep the old recovery entry and fail closed.
            LOG.warning("无法确认应用 %s 的旧恢复实例是否已退出: %s",
                        app_id, exc)
            return False

    def scalar(value, types):
        return value if isinstance(value, types) and not isinstance(value, bool) else None

    def op(c):
        target = find_app(c, app_id)
        if target:
            pid = scalar(getattr(proc, "pid", None), (int,))
            target["lastPid"] = pid
            target["lastPgid"] = scalar(pgid, (int,))
            target["runToken"] = token
            target["attached"] = False
            target["controlMode"] = "managed"
            target["observation"] = None
            creation_time = scalar(getattr(proc, "creation_time", None),
                                   (int, float))
            target["lastCreateTime"] = creation_time
            job_name = scalar(getattr(proc, "job_name", None), (str,))
            run_id = scalar(getattr(proc, "run_id", token), (str,)) or token
            anchor_pid = scalar(getattr(proc, "anchor_pid", None), (int,))
            anchor_create_time = scalar(
                getattr(proc, "anchor_create_time", None), (int, float))
            target["runInstance"] = ({
                "runId": run_id,
                "jobName": job_name,
                "rootPid": pid,
                "rootCreateTime": creation_time,
                "anchorPid": anchor_pid,
                "anchorCreateTime": anchor_create_time,
                "startedAt": int(started_at * 1000),
                "processState": "starting",
                "exitResult": None,
            } if job_name and pid else None)
            target["launchConfigured"] = is_launch_configured(
                target.get("launchSpec"))
            target["readinessState"] = (
                "checking" if (target.get("kind") or "service") == "service"
                and (target.get("launchSpec") or {}).get("readiness", {}).get("type")
                in ("tcp", "http") else "unknown")
            # 批处理任务运行时先保留上一次结果；自然退出或手动停止后再原子覆盖。
            if (target.get("kind") or "service") != "task":
                target["lastExit"] = None
            return True
        return False
    saved = cfg.update(op)
    if saved:
        if stale_recovery:
            # The old process is already empty/closed, so release its retained
            # handle and remove the stale token before registering this run.
            _forget_unpersisted_run(
                app_id, stale_recovery.get("token"),
                stale_recovery.get("proc"))
        watch_app_exit(cfg, app_id, proc, token, started_at)
    return saved


def started_app_identity(app, proc, pgid, token, started_at=None):
    """Build the schema v2 runtime identity without mutating config."""
    started_at = time.time() if started_at is None else started_at

    def scalar(value, types):
        return value if isinstance(value, types) and not isinstance(value, bool) else None

    pid = scalar(getattr(proc, "pid", None), (int,))
    creation_time = scalar(getattr(proc, "creation_time", None), (int, float))
    job_name = scalar(getattr(proc, "job_name", None), (str,))
    run_id = scalar(getattr(proc, "run_id", token), (str,)) or token
    anchor_pid = scalar(getattr(proc, "anchor_pid", None), (int,))
    anchor_create_time = scalar(
        getattr(proc, "anchor_create_time", None), (int, float))
    result = {
        "lastPid": pid,
        "lastPgid": scalar(pgid, (int,)),
        "runToken": token,
        "attached": False,
        "controlMode": "managed",
        "observation": None,
        "lastCreateTime": creation_time,
        "runInstance": ({
            "runId": run_id,
            "jobName": job_name,
            "rootPid": pid,
            "rootCreateTime": creation_time,
            "anchorPid": anchor_pid,
            "anchorCreateTime": anchor_create_time,
            "startedAt": int(started_at * 1000),
            "processState": "starting",
            "exitResult": None,
        } if job_name and pid else None),
        "launchConfigured": is_launch_configured(app.get("launchSpec")),
        "readinessState": (
            "checking" if (app.get("kind") or "service") == "service"
            and (app.get("launchSpec") or {}).get("readiness", {}).get("type")
            in ("tcp", "http") else "unknown"),
    }
    if (app.get("kind") or "service") != "task":
        result["lastExit"] = None
    return result


def _saved_started_identity(cfg, app_id, token):
    try:
        app = find_app(cfg.snapshot(), app_id)
    except Exception:
        return None
    instance = app.get("runInstance") if app else None
    if (app and app.get("runToken") == token
            and isinstance(instance, dict)
            and instance.get("runId") == token):
        return app
    return None


def _ensure_started_run_recovery(cfg, app_id, proc, pgid, token):
    """Persist or retain identity after compensation could not stop a run."""
    saved = _saved_started_identity(cfg, app_id, token)
    started_at = time.time()
    # Build a fallback identity before consulting config. The app can be
    # deleted (or the config can become temporarily unreadable) between
    # spawn and compensation; a live Job still needs an in-memory recovery
    # record and exit watcher in that case.
    identity = started_app_identity({}, proc, pgid, token, started_at)
    if saved:
        started_at = ((saved.get("runInstance") or {}).get("startedAt") or 0) / 1000
        if not started_at:
            started_at = time.time()
    else:
        try:
            snapshot = cfg.snapshot()
            current = find_app(snapshot, app_id)
            if current is not None:
                identity = started_app_identity(current, proc, pgid, token,
                                                started_at)

                def op(data):
                    target = find_app(data, app_id)
                    if target:
                        target.update(identity)
                        return True
                    return False

                saved_identity = bool(cfg.update(op))
                if saved_identity:
                    saved = _saved_started_identity(cfg, app_id, token)
        except Exception:
            LOG.exception("启动补偿失败后无法写入运行身份（应用 %s）", app_id)

    if saved:
        instance = saved.get("runInstance") or {}
        watch_proc = proc
        if instance.get("jobName"):
            watch_proc = _remember_run_job(saved, proc, "repair") or proc
        else:
            # Test doubles and legacy wrappers may expose no Job name. Keep
            # the exact process in the in-memory recovery table until the
            # watcher observes its exit; this still prevents a duplicate
            # start and gives stop/DELETE a conservative identity.
            watch_proc = _remember_unpersisted_run(app_id, token, proc, {
                key: saved.get(key) for key in (
                    "lastPid", "lastPgid", "runToken", "attached",
                    "controlMode", "observation", "lastCreateTime",
                    "runInstance", "launchConfigured", "readinessState",
                    "lastExit")})
        watcher_key = (app_id, token)
        with WATCHER_LOCK:
            watcher_active = watcher_key in ACTIVE_EXIT_WATCHERS
        if not watcher_active:
            try:
                watch_app_exit(cfg, app_id, watch_proc, token, started_at)
            except Exception:
                # Durable Job/run identity is still sufficient for state poll
                # recovery even if an in-process exit watcher cannot start.
                LOG.exception("启动补偿后无法启动退出监视线程（应用 %s）", app_id)
        return True

    if identity is None:
        return False
    watch_proc = _remember_unpersisted_run(app_id, token, proc, identity)
    watch_proc = watch_proc or proc
    with WATCHER_LOCK:
        watcher_active = (app_id, token) in ACTIVE_EXIT_WATCHERS
    if not watcher_active:
        try:
            watch_app_exit(cfg, app_id, watch_proc, token, started_at)
        except Exception:
            # The retained record remains available to state, stop, PUT and DELETE.
            LOG.exception("未落盘的运行身份无法启动退出监视线程（应用 %s）", app_id)
    return True


def abort_started_app(cfg, app_id, proc, token, reason, pgid=None):
    """Compensate a failed post-spawn start before returning an API error.

    A failed config or watcher step must not leave an untracked Job Object.
    Keep persisted identity when termination cannot be verified so a later
    request can still reconnect and control the process.
    """
    stopped = False
    cleanup_error = None
    try:
        poll = getattr(proc, "poll", None)
        members = getattr(proc, "members", None)
        try:
            already_exited = (callable(poll) and poll() is not None
                              and (not callable(members) or not members()))
        except Exception:
            already_exited = False
        if already_exited:
            stopped = True
        else:
            result = proc.terminate(force=True)
            if isinstance(result, tuple):
                stopped = bool(result and result[0])
                if not stopped:
                    cleanup_error = (result[1] if len(result) > 1 else
                                     "强制停止返回失败")
            else:
                stopped = result is not False
            wait_empty = getattr(proc, "wait_for_empty", None)
            if stopped and callable(wait_empty):
                try:
                    wait_empty(timeout=5.0)
                except TypeError:
                    wait_empty()
            elif stopped and callable(members) and members():
                stopped = False
                cleanup_error = "Job Object 中仍有进程"
    except Exception as exc:
        stopped = False
        cleanup_error = str(exc) or type(exc).__name__
        LOG.exception("启动失败后的 Job Object 清理异常（应用 %s）", app_id)

    if stopped:
        try:
            clear_app_runtime(cfg, app_id, expected_token=token)
        except Exception:
            LOG.exception("已停止的应用运行身份无法回滚（应用 %s）", app_id)
    else:
        LOG.critical(
            "启动失败后无法确认应用进程已停止；保留可用身份（应用 %s, PID %s, runId %s）：%s",
            app_id, getattr(proc, "pid", None),
            getattr(proc, "run_id", token), cleanup_error or "未知清理错误")
        _ensure_started_run_recovery(cfg, app_id, proc, pgid, token)

    # When persist_started_app registered its exit watcher, let that watcher
    # finish its wait and release handles before the compensation closes them.
    watcher_key = (app_id, token)
    watcher_active = False
    watcher_deadline = time.monotonic() + 5.0
    while time.monotonic() < watcher_deadline:
        with WATCHER_LOCK:
            watcher_active = watcher_key in ACTIVE_EXIT_WATCHERS
        if not watcher_active:
            break
        time.sleep(0.02)

    # A watcher owns the same native handles while it waits.  Closing them
    # here would race WaitForSingleObject; let the watcher close them in its
    # finally block.  This also preserves the exact job identity if the
    # process is still alive after compensation failed.
    if watcher_active:
        LOG.warning("退出 watcher 仍在等待，保留进程句柄由 watcher 清理（应用 %s）",
                    app_id)
    else:
        close = getattr(proc, "close", None)
        if stopped and callable(close):
            try:
                close()
            except Exception:
                LOG.exception("启动失败后的进程句柄关闭失败（应用 %s）", app_id)

    if stopped:
        return {"ok": False, "status": 500,
                "error": "%s；本次进程已终止" % reason}
    return {"ok": False, "status": 500,
            "error": "%s；无法确认进程已终止，请查看日志并重试停止操作" % reason}


def mark_app_alive(cfg, app_id, run_id):
    def op(data):
        target = find_app(data, app_id)
        instance = target.get("runInstance") if target else None
        if (isinstance(instance, dict) and instance.get("runId") == run_id
                and instance.get("processState") == "starting"):
            instance["processState"] = "alive"
    cfg.update(op)


def _probe_readiness(readiness):
    probe_type = readiness.get("type")
    if probe_type == "none":
        return None
    host = readiness.get("host") or "localhost"
    port = readiness.get("port")
    if not isinstance(port, int):
        return False
    if probe_type == "tcp":
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            return False
    if probe_type == "http":
        url = readiness.get("url") or "/"
        try:
            url, target_host = http_readiness_url(host, port, url)

            class SameLoopbackRedirectHandler(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    try:
                        http_readiness_url(
                            host, port, newurl, expected_host=target_host)
                    except LaunchSpecError:
                        return None
                    return super().redirect_request(
                        req, fp, code, msg, headers, newurl)

            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), SameLoopbackRedirectHandler())
            request = urllib.request.Request(url, method="GET")
            with opener.open(request, timeout=1.0) as response:
                return 200 <= int(response.status) < 400
        except Exception:
            return False
    return False


def watch_app_readiness(cfg, app_id, run_id, app):
    """Update readiness from a TCP/HTTP probe without inferring ownership."""
    if (app.get("kind") or "service") != "service":
        return None
    spec = app.get("launchSpec") or {}
    readiness = spec.get("readiness") or default_readiness(app.get("port"))
    if readiness.get("type") == "none":
        return None
    watcher_key = (app_id, run_id)
    with WATCHER_LOCK:
        if watcher_key in ACTIVE_READINESS_WATCHERS:
            return None
        ACTIVE_READINESS_WATCHERS.add(watcher_key)
    instance = app.get("runInstance") or {}
    started_ms = instance.get("startedAt")
    if type(started_ms) not in (int, float) or started_ms <= 0:
        started_at = time.time()
    else:
        started_at = float(started_ms) / 1000.0
    timeout = float(readiness.get("timeoutSec", 20))
    deadline = time.monotonic() + max(0.0, timeout - max(0.0, time.time() - started_at))

    def _probe():
        try:
            state = "timeout"
            first_probe = True
            while first_probe or time.monotonic() < deadline:
                first_probe = False
                current = find_app(cfg.snapshot(), app_id)
                current_instance = current.get("runInstance") if current else None
                if (not current or not isinstance(current_instance, dict)
                        or current_instance.get("runId") != run_id
                        or current_instance.get("processState") in ("exited", "stopping")):
                    state = "failed"
                    break
                if _probe_readiness(readiness):
                    state = "ready"
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.25)

            def op(data):
                target = find_app(data, app_id)
                current_instance = target.get("runInstance") if target else None
                if (isinstance(current_instance, dict)
                        and current_instance.get("runId") == run_id):
                    # A stop/exit that won the race keeps its terminal process state.
                    if target.get("readinessState") == "checking":
                        target["readinessState"] = state

            _update_config_with_retry(
                cfg, op, "应用 %s readiness 状态" % app_id)
        finally:
            with WATCHER_LOCK:
                ACTIVE_READINESS_WATCHERS.discard(watcher_key)

    thread = threading.Thread(target=_probe, daemon=True,
                              name="app-readiness-%s" % app_id)
    try:
        thread.start()
    except Exception:
        with WATCHER_LOCK:
            ACTIVE_READINESS_WATCHERS.discard(watcher_key)
        raise
    return thread


def _record_recovered_empty_job(cfg, app_id, run_id, instance):
    """Persist a terminal state when the named Job Object is already empty.

    Windows no longer exposes the root exit code after its process handle was
    lost with the previous console. Keep that fact explicit for task history.
    """
    now = time.time()
    started_ms = instance.get("startedAt")
    if type(started_ms) not in (int, float) or started_ms <= 0:
        started_ms = int(now * 1000)
    started_at = float(started_ms) / 1000.0
    exit_result = {
        "code": None,
        "at": int(now),
        "startedAt": int(started_ms),
        "durationSec": round(max(0.0, now - started_at), 3),
    }
    def op(data):
        target = find_app(data, app_id)
        current = target.get("runInstance") if target else None
        if (not isinstance(current, dict)
                or current.get("runId") != run_id
                or current.get("processState") == "exited"):
            return
        current["processState"] = "exited"
        current["exitResult"] = dict(exit_result)
        if (target.get("kind") or "service") == "task":
            target["lastExit"] = dict(exit_result, status="unknown")
        if target.get("readinessState") == "checking":
            target["readinessState"] = "failed"
    _update_config_with_retry(
        cfg, op, "应用 %s Job Object 退出状态" % app_id)


def restore_run_watchers(cfg):
    """Reconnect exit/readiness watchers for structured runs after restart."""
    for app in cfg.snapshot().get("apps", []):
        instance = app.get("runInstance")
        if (app.get("controlMode") == "monitor"
                or not isinstance(instance, dict)
                or not instance.get("jobName")
                or not instance.get("runId")
                or instance.get("processState") == "exited"):
            continue
        proc = _open_run_job(app)
        run_id = instance["runId"]
        if proc is RUN_JOB_REOPEN_FAILED:
            LOG.warning("应用 %s 的 Job Object 暂时无法重连，保留原运行状态",
                        app.get("id"))
            if app.get("readinessState") == "checking":
                # Readiness only describes the endpoint; it does not establish
                # process ownership, so this independent probe remains safe.
                watch_app_readiness(cfg, app.get("id"), run_id, app)
            continue
        if proc is None:
            _record_recovered_empty_job(cfg, app.get("id"), run_id, instance)
            continue
        started_ms = instance.get("startedAt")
        started_at = (float(started_ms) / 1000.0
                      if type(started_ms) in (int, float) and started_ms > 0
                      else time.time())
        if instance.get("processState") == "starting":
            mark_app_alive(cfg, app.get("id"), run_id)
        watch_app_exit(cfg, app.get("id"), proc, run_id, started_at)
        current = find_app(cfg.snapshot(), app.get("id"))
        if current and current.get("readinessState") == "checking":
            watch_app_readiness(cfg, app.get("id"), run_id, current)


def start_app_transaction(cfg, app_id, require_autostart=False):
    """执行唯一的受控启动事务并返回 ``{ok, status, ...}``。

    手动启动、重启后的再次启动和开机自启动都必须经过这里。锁覆盖读取
    状态、健康检查、端口检查、派生进程及身份落盘，避免两个调用方各自
    通过旧快照后重复启动。
    """
    lock = cfg.try_app_operation(app_id)
    if lock is None:
        return {"ok": False, "status": 409,
                "error": "该应用正在执行其他操作，请稍后重试"}
    try:
        current = find_app(cfg.snapshot(), app_id)
        if current is None:
            return {"ok": False, "status": 404, "error": "应用不存在"}
        # A transient write failure may leave the live Job only in this
        # console's recovery registry. Rehydrate it before duplicate-start and
        # launch-config checks so a retry cannot spawn a second service.
        _hydrate_unpersisted_run(current)
        if (current.get("controlMode") == "monitor"
                or ("launchSpec" in current
                    and (not current.get("launchConfigured")
                         or not is_launch_configured(current.get("launchSpec"))))):
            return {"ok": False, "status": 409,
                    "error": "请先确认启动配置，再由总控台托管此服务",
                    "launchSpecRequired": True}
        with RUN_JOB_ACCESS_LOCK:
            current_instance = current.get("runInstance")
            if (isinstance(current_instance, dict)
                    and current_instance.get("jobName")
                    and current_instance.get("processState") != "exited"):
                job = _open_run_job(current)
                if job is RUN_JOB_REOPEN_FAILED:
                    return {"ok": False, "status": 409,
                            "error": "无法验证当前 Job Object 状态，请检查总控台日志后重试"}
                if job is not None:
                    try:
                        try:
                            members = job.members()
                        except Exception as exc:
                            LOG.warning("读取应用 %s 的 Job Object 状态失败: %s",
                                        app_id, exc)
                            if _is_anchor_cleanup_failure(exc):
                                _remember_run_job(current, job, "empty-cleanup")
                            return {"ok": False, "status": 409,
                                    "error": "无法验证当前 Job Object 状态，请稍后重试"}
                        if members:
                            return {"ok": False, "status": 409,
                                    "error": "应用已在运行"}
                    finally:
                        if not _run_job_is_retained(current, job):
                            _release_run_job_handle(current, job)
        if require_autostart and (
                (current.get("kind") or "service") != "service"
                or not current.get("autostart")):
            return {"ok": False, "status": 409,
                    "error": "应用已不符合开机自启动条件"}
        if app_alive_sign(current):
            return {"ok": False, "status": 409, "error": "应用已在运行"}
        health = inspect_app_health(current)
        if health.get("blocking"):
            issue = (health.get("issues") or [{}])[0]
            return {
                "ok": False,
                "status": 422,
                "error": "%s：%s" % (
                    issue.get("title", "配置不健康"),
                    issue.get("detail", "请检查应用配置")),
                "health": health,
            }
        port = current.get("port")
        occupied = ([(pid, listening_port) for pid, listening_port
                     in scan_listeners() if listening_port == port]
                    if port else [])
        if occupied:
            return {
                "ok": False,
                "status": 409,
                "error": "端口 %d 已被 PID %d 占用" %
                         (port, occupied[0][0]),
            }
        ok, error, proc, pgid, token = start_app(current)
        if not ok:
            return {"ok": False, "status": 500, "error": error}
        persisted = False
        try:
            persisted = persist_started_app(cfg, app_id, proc, pgid, token)
            if not persisted:
                return abort_started_app(
                    cfg, app_id, proc, token,
                    "应用已被删除，已取消启动", pgid) | {"status": 409}
            mark_app_alive(cfg, app_id, token)
            watch_app_readiness(cfg, app_id, token, current)
            # 一次性任务的正常形态就是快速退出，不能把成功任务误判成启动失败。
            if (current.get("kind") or "service") == "task":
                return {"ok": True, "status": 200, "pid": proc.pid}
            deadline = time.monotonic() + STARTUP_PROBE_SEC
            code = proc.poll()
            while code is None and time.monotonic() < deadline:
                time.sleep(0.025)
                code = proc.poll()
            members = getattr(proc, "members", None)
            live_members = members() if callable(members) else []
            if (code is not None
                    and isinstance(live_members, (list, tuple, set, frozenset))
                    and not live_members):
                return {"ok": False, "status": 422,
                        "error": startup_failure_message(app_id, code)}
            return {"ok": True, "status": 200, "pid": proc.pid,
                    "readiness": "checking" if (current.get("launchSpec") or {}).get(
                        "readiness", {}).get("type") in ("tcp", "http") else "unknown"}
        except Exception as exc:
            LOG.exception("启动后的状态保存或初始化失败（应用 %s）", app_id)
            reason = "启动后的应用状态保存或初始化失败（%s）" % (
                str(exc) or type(exc).__name__)
            return abort_started_app(cfg, app_id, proc, token, reason, pgid)
    finally:
        lock.release()


def clear_app_runtime(cfg, app_id, expected_token=None, last_exit=None):
    """清除受控身份；可用 token 防竞态，并可原子写入本次退出结果。"""
    recovery_token = None
    if expected_token is not None:
        with RETAINED_RUN_JOBS_LOCK:
            recovery = UNPERSISTED_RUNS.get(app_id)
            if recovery:
                recovery_token = recovery.get("token")

    def op(c):
        target = find_app(c, app_id)
        if not target:
            return False
        instance = target.get("runInstance")
        durable_identity_matches = _durable_identity_matches(
            target, expected_token)
        if (expected_token is not None
                and not durable_identity_matches
                # The in-memory recovery exception is only valid when the
                # durable card has no newer identity.  A stale recovery token
                # must never authorize clearing a subsequently started run.
                and not (target.get("runToken") is None
                         and _recovery_can_fill_identity(target, expected_token)
                         and recovery_token == expected_token)):
            return False
        target["lastPid"] = None
        target["lastPgid"] = None
        target["runToken"] = None
        target["attached"] = False
        target["lastCreateTime"] = None
        if isinstance(instance, dict):
            instance["processState"] = "exited"
            if last_exit is not None:
                instance["exitResult"] = last_exit
        target["readinessState"] = "unknown"
        if last_exit is not None:
            target["lastExit"] = last_exit
        return True
    cleared = cfg.update(op)
    if cleared and recovery_token == expected_token:
        _forget_unpersisted_run(app_id, expected_token)
    return cleared


def stop_app_for_update(cfg, app, timeout=5.0):
    """为修改运行参数安全停止应用；返回 (ok, error, stopped)。"""
    if not app_alive_sign(app):
        return True, None, False
    ok, error = stop_app_and_clear(cfg, app, timeout)
    return ok, error, bool(ok)


def pick_path(what):
    """系统文件/目录选择框（tkinter）。返回 (path|None, canceled)。"""
    return sysops.pick_path(what)


def _project_python(cwd):
    """Return the first existing project-local Windows Python interpreter."""
    if not isinstance(cwd, str) or not cwd:
        return None
    for directory in (".venv", "venv", "env"):
        candidate = os.path.abspath(os.path.join(
            cwd, directory, "Scripts", "python.exe"))
        if _windows_command_file(candidate):
            return candidate
    return None


def _runtime_path():
    """PATH used by project candidates, including paths added for pythonw."""
    try:
        return build_launch_env("project-detect").get("PATH")
    except Exception:
        return os.environ.get("PATH")


def _resolve_runtime(executable, *, cwd=None, python_project=False):
    """Resolve a generated candidate executable to an absolute file path."""
    name = str(executable or "").strip()
    if not name:
        return None
    base = os.path.basename(name).lower()
    if python_project and re.fullmatch(
            r"(?:py|python|pythonw)(?:\d+(?:\.\d+)*)?(?:\.exe)?", base):
        project_python = _project_python(cwd)
        if project_python:
            return project_python
    if _looks_like_command_path(name) or os.path.isabs(name):
        candidate = _resolve_command_path(name, cwd or os.getcwd())
        return (os.path.abspath(candidate)
                if _windows_command_file(candidate) else None)
    found = shutil.which(name, path=_runtime_path())
    if found and _windows_command_file(found):
        return os.path.abspath(found)
    return None


def _powershell_executable():
    """Resolve Windows PowerShell or pwsh without relying on a shell profile."""
    for name in ("powershell.exe", "pwsh.exe", "powershell", "pwsh"):
        found = _resolve_runtime(name)
        if found:
            return found
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    candidate = os.path.join(
        system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return os.path.abspath(candidate) if _windows_command_file(candidate) else None


def _bash_runtime():
    """Resolve Bash or WSL for explicit .sh project scripts."""
    for name in ("bash.exe", "bash"):
        found = _resolve_runtime(name)
        if found:
            return found, "bash"
    for name in ("wsl.exe", "wsl"):
        found = _resolve_runtime(name)
        if found:
            return found, "wsl"
    return None, None


def _windows_command_file(path):
    """Check whether a file has a Windows-executable format or extension."""
    if not isinstance(path, str) or not os.path.isfile(path):
        return False
    suffix = os.path.splitext(path)[1].casefold()
    if suffix in (".bat", ".cmd"):
        return bool(_resolve_runtime("cmd.exe"))
    if suffix == ".ps1":
        return bool(_powershell_executable())
    if suffix != ".exe":
        return False
    try:
        with open(path, "rb") as handle:
            header = handle.read(64)
            if len(header) < 64 or header[:2] != b"MZ":
                return False
            pe_offset = int.from_bytes(header[0x3C:0x40], "little")
            if pe_offset < 64 or pe_offset > 16 * 1024 * 1024:
                return False
            handle.seek(pe_offset)
            return handle.read(4) == b"PE\0\0"
    except (OSError, ValueError):
        return False


def _launch_spec_for_script(path, cwd=None, port=None):
    """Build a structured LaunchSpec for an explicitly selected script.

    The working directory is used to find a project virtual environment; it
    defaults to the script's parent so selecting a script works on its own.
    """
    normalized = os.path.abspath(os.path.expanduser(str(path)))
    working_dir = os.path.abspath(os.path.expanduser(
        cwd or os.path.dirname(normalized)))
    suffix = os.path.splitext(normalized)[1].lower()
    executable = None
    mode = "exec"
    args = []
    if suffix == ".py":
        executable = _project_python(working_dir)
        if not executable:
            executable = (_resolve_runtime("python.exe") or
                          _resolve_runtime("python"))
        args = [normalized]
    elif suffix in (".bat", ".cmd"):
        executable = normalized if _windows_command_file(normalized) else None
        mode = "cmd"
    elif suffix == ".ps1":
        executable = _powershell_executable()
        args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", normalized]
        mode = "powershell"
    elif suffix in (".sh", ".bash"):
        executable, shell = _bash_runtime()
        if executable and shell == "wsl":
            args = ["bash", "--", normalized]
        elif executable:
            args = ["--", normalized]
    else:
        executable = normalized if os.path.isfile(normalized) else None

    if not executable:
        reason = {
            ".py": "找不到 Python 运行时；请安装 Python，或在项目中创建 .venv/venv/env。",
            ".ps1": "找不到 Windows PowerShell 或 pwsh。",
            ".sh": "找不到 Bash 或 WSL，无法运行此 Shell 脚本。",
            ".bash": "找不到 Bash 或 WSL，无法运行此 Shell 脚本。",
        }.get(suffix, "找不到可运行此脚本的程序。")
        return None, reason
    spec = {
        "mode": mode, "executable": executable, "args": args,
        "cwd": working_dir, "env": {},
        "readiness": default_readiness(port),
    }
    try:
        return normalize_launch_spec(spec, cwd=working_dir, port=port), None
    except LaunchSpecError as exc:
        return None, str(exc)


def command_for_script(path, cwd=None):
    """Return display text for a selected script using its project runtime."""
    spec, _reason = _launch_spec_for_script(path, cwd)
    if spec:
        return command_from_launch_spec(spec)
    normalized = os.path.abspath(os.path.expanduser(str(path)))
    suffix = os.path.splitext(normalized)[1].lower()
    quoted = _quote_win(normalized)
    if suffix == ".py":
        return "python -- %s" % quoted
    if suffix == ".ps1":
        return "powershell -NoProfile -ExecutionPolicy Bypass -File %s" % quoted
    if suffix in (".bat", ".cmd"):
        return quoted
    if suffix in (".sh", ".bash"):
        return "bash -- %s" % quoted
    return quoted


def _quote_win(path):
    """Windows 命令行安全引用（覆盖 CMD 的分隔和控制字符）。"""
    path = str(path)
    if '"' in path:
        path = path.replace('"', '\\"')
    if any(char.isspace() or char in "&|<>()^!" for char in path):
        return '"%s"' % path
    return path


SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".bat", ".cmd", ".ps1"}
PYTHON_CMD = "python"
SHELL_BUILTINS = {
    ".", ":", "[", "alias", "break", "cd", "command", "continue", "echo",
    "eval", "exec", "exit", "export", "false", "printf", "pwd", "read",
    "return", "set", "shift", "source", "test", "true", "type", "ulimit",
    "umask", "unalias", "unset", "wait",
}


def _simple_windows_command_tokens(command):
    """解析无 CMD 元字符展开的简单 Windows 命令。

    这里只服务于静态健康检查，不尝试实现完整的 ``cmd.exe`` 语法。路径选择器
    生成的直接脚本、``python.exe -- script.py`` 与 ``powershell -File`` 都由
    此解析器覆盖；任何变量、重定向、管道或转义语义一律降级为 unknown。
    """
    tokens = []
    current = []
    quoted = False
    for char in command.strip():
        if char == '"':
            quoted = not quoted
            continue
        if not quoted and char in "|&;<>()%^!":
            return None
        if char.isspace() and not quoted:
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)
    if quoted:
        return None
    if current:
        tokens.append("".join(current))
    if any(any(char in token for char in ("$", "*", "?", "[", "]", "`"))
           for token in tokens):
        return None
    return tokens


def _simple_command_tokens(command):
    """解析无管道/重定向/展开的简单命令；不确定时返回 None。"""
    if not isinstance(command, str) or not command.strip():
        return []
    return _simple_windows_command_tokens(command)


def _split_windows_command_line(command):
    """Parse a Windows command line into argv without invoking a shell.

    This follows the Microsoft C runtime backslash/quote rules used by
    ``CreateProcessW``. Unquoted shell operators are rejected so a command
    such as ``python app.py && echo done`` cannot silently change meaning.
    Operators inside quoted argv remain ordinary argument data.
    """
    if not isinstance(command, str) or not command.strip():
        raise LaunchSpecError("请填写启动命令")
    if "\x00" in command or len(command) > 32760:
        raise LaunchSpecError("启动命令包含无效字符或超出 Windows 长度限制")

    argv = []
    length = len(command)
    index = 0
    while index < length:
        while index < length and command[index].isspace():
            index += 1
        if index >= length:
            break
        token = []
        quoted = False
        while index < length:
            if command[index].isspace() and not quoted:
                break
            slash_count = 0
            while index < length and command[index] == "\\":
                slash_count += 1
                index += 1
            if index < length and command[index] == '"':
                token.extend("\\" * (slash_count // 2))
                if slash_count % 2:
                    token.append('"')
                    index += 1
                else:
                    quoted = not quoted
                    index += 1
            else:
                token.extend("\\" * slash_count)
                if index < length and not (command[index].isspace() and not quoted):
                    if not quoted and command[index] in "&|<>^":
                        raise LaunchSpecError(
                            "命令包含未加引号的 Shell 运算符（&、|、<、>、^）；"
                            "结构化启动不执行 Shell 语法，请将命令写入 .bat/.cmd "
                            "或 .ps1 脚本后选择该脚本")
                    token.append(command[index])
                    index += 1
        if quoted:
            raise LaunchSpecError("启动命令中的双引号没有闭合")
        argv.append("".join(token))
        while index < length and command[index].isspace():
            index += 1
    if not argv or not argv[0]:
        raise LaunchSpecError("启动命令必须以可执行程序或脚本开头")
    return argv


def normalize_attached_python_command(command, cwd):
    """Prefer a project virtualenv over Windows' reported base Python.

    A venv Python process can appear in the Windows process table as its base
    interpreter. Saving that executable verbatim makes a reclaimed service
    fail on restart because project modules such as uvicorn are not installed
    in the base interpreter.
    """
    if not isinstance(cwd, str) or not cwd or not os.path.isdir(cwd):
        return command
    # The small static parser below is not a full Windows command-line parser.
    # In particular, it cannot preserve quotes escaped inside a -c argument.
    # Leave those commands untouched instead of rebuilding them incorrectly.
    if re.search(r'\\+"', command):
        return command
    tokens = _simple_command_tokens(command)
    if not tokens:
        return command
    executable_index = 0
    while (executable_index < len(tokens)
           and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*",
                            tokens[executable_index])):
        executable_index += 1
    if executable_index >= len(tokens):
        return command
    executable = tokens[executable_index]
    if not re.fullmatch(
            r"(?:py|python|pythonw)(?:\d+(?:\.\d+)*)?(?:\.exe)?",
            os.path.basename(executable), re.IGNORECASE):
        return command
    executable_base = os.path.basename(executable).lower()
    executable_name = ("pythonw.exe" if executable_base.startswith("pythonw")
                       else "python.exe")
    # The Windows ``py`` launcher consumes a version selector before starting
    # Python. The venv executable does not understand that launcher selector,
    # so remove it when translating e.g. ``py -3 -m ...`` to the project venv.
    if executable_base in ("py", "py.exe") and (
            executable_index + 1 < len(tokens)):
        selector = tokens[executable_index + 1]
        if re.fullmatch(r"-\d+(?:\.\d+)*(?:-\d+)?|-V:\S+",
                        selector, re.IGNORECASE):
            tokens.pop(executable_index + 1)
    for directory in (".venv", "venv", "env"):
        candidate = os.path.join(cwd, directory, "Scripts", executable_name)
        if not os.path.isfile(candidate):
            continue
        try:
            if os.path.realpath(candidate) == os.path.realpath(executable):
                return command
        except OSError:
            pass
        tokens[executable_index] = candidate
        return subprocess.list2cmdline(tokens)
    return command


def _command_python_executable(command):
    """Return ``(tokens, executable_index, executable)`` for Python commands.

    Process command lines are not always quoted consistently, so this helper
    deliberately reuses the conservative token parser used by health checks.
    ``None`` means the command is too complex or does not start with Python.
    """
    tokens = _simple_command_tokens(command)
    if not tokens:
        return None
    index = 0
    while (index < len(tokens)
           and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[index])):
        index += 1
    if index >= len(tokens):
        return None
    executable = tokens[index]
    if not re.fullmatch(
            r"(?:py|python|pythonw)(?:\d+(?:\.\d+)*)?(?:\.exe)?",
            os.path.basename(executable), re.IGNORECASE):
        return None
    return tokens, index, executable


def _is_uv_python_path(value):
    """Whether an executable came from uv's managed Python cache."""
    if not isinstance(value, str):
        return False
    normalized = os.path.normcase(os.path.expanduser(value)).replace("\\", "/")
    return "/uv/python/" in normalized or "/.cache/uv/python/" in normalized


def repair_legacy_app_commands(cfg):
    """Persist safe virtualenv replacements for old claimed service cards.

    Older versions saved the interpreter path reported by the running process.
    That path can be uv's shared Python cache, whose environment does not have
    the project's packages. A card created through port claiming is marked
    ``attached`` until the first controlled start; after a failed old start
    that marker is cleared, so the uv-cache path is also treated as evidence of
    the legacy claim flow. Only a detected project virtualenv is substituted,
    and command arguments are preserved byte-for-byte semantically.
    """
    try:
        snapshot = cfg.snapshot()
    except Exception:
        return False
    repairs = []
    for app in snapshot.get("apps") or []:
        if not isinstance(app, dict) or not app.get("id"):
            continue
        if app.get("controlMode") == "monitor":
            continue
        parsed = _command_python_executable(app.get("command"))
        if not parsed:
            continue
        _, _, executable = parsed
        if not app.get("attached") and not _is_uv_python_path(executable):
            continue
        normalized = normalize_attached_python_command(
            app.get("command"), app.get("cwd"))
        if normalized != app.get("command"):
            repairs.append((app["id"], app.get("command"), normalized))
    if not repairs:
        return False

    def op(data):
        changed = False
        for app_id, old_command, new_command in repairs:
            target = find_app(data, app_id)
            if target and target.get("command") == old_command:
                target["command"] = new_command
                # The compatibility command is derived from LaunchSpec, so
                # update both fields atomically when migrating a legacy card.
                spec = target.get("launchSpec")
                if (isinstance(spec, dict)
                        and spec.get("mode") == "legacy-shell"):
                    spec["legacyCommand"] = new_command
                changed = True
        return changed

    try:
        return bool(cfg.update(op))
    except OSError as exc:
        LOG.warning("无法修复旧认领卡片的启动解释器: %s", exc)
        return False


def _resolve_command_path(value, cwd):
    value = os.path.expanduser(value)
    if os.path.isabs(value):
        return os.path.normpath(value)
    return os.path.normpath(os.path.join(cwd, value))


def _command_path_is_absolute(value):
    expanded = os.path.expanduser(value)
    if os.path.isabs(expanded):
        return True
    return bool(re.match(r"^[A-Za-z]:[\\/]", expanded))


def _looks_like_command_path(value):
    return "/" in value or "\\" in value


def _script_target(tokens, cwd):
    """提取 (路径, 是否直接执行, 原路径是否相对)，否则返回空。"""
    if not tokens:
        return None, False, False
    index = 0
    while index < len(tokens) and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[index]):
        index += 1
    if index >= len(tokens):
        return None, False, False
    executable = tokens[index]
    base = os.path.basename(executable).casefold()
    args = tokens[index + 1:]

    if re.fullmatch(r"py(?:thon(?:\d+(?:\.\d+)*)?)?(?:\.exe)?", base):
        if "-m" in args or "-c" in args:
            return None, False, False
        if args and args[0] == "--":
            args = args[1:]
        candidate = next((arg for arg in args if not arg.startswith("-")), None)
        if candidate and (os.path.splitext(candidate)[1].lower() in SCRIPT_SUFFIXES
                          or _looks_like_command_path(candidate)):
            return (_resolve_command_path(candidate, cwd), False,
                    not _command_path_is_absolute(candidate))
        return None, False, False

    if base in {"bash", "sh", "zsh"}:
        if any(arg == "--command"
               or (arg.startswith("-") and "c" in arg[1:])
               for arg in args):
            return None, False, False
        if args and args[0] == "--":
            args = args[1:]
        candidate = next((arg for arg in args if not arg.startswith("-")), None)
        if candidate and (os.path.splitext(candidate)[1].lower() in SCRIPT_SUFFIXES
                          or _looks_like_command_path(candidate)):
            return (_resolve_command_path(candidate, cwd), False,
                    not _command_path_is_absolute(candidate))
        return None, False, False

    if base in {
            "powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        if any(arg.casefold() in {
                "-command", "-c", "-encodedcommand", "-encodedcommands"}
               for arg in args):
            return None, False, False
        candidate = None
        for index, arg in enumerate(args):
            if arg.casefold() in {"-file", "/file"} and index + 1 < len(args):
                candidate = args[index + 1]
                break
        if candidate and (os.path.splitext(candidate)[1].lower() in SCRIPT_SUFFIXES
                          or _looks_like_command_path(candidate)):
            return (_resolve_command_path(candidate, cwd), False,
                    not _command_path_is_absolute(candidate))
        return None, False, False

    suffix = os.path.splitext(executable)[1].lower()
    if suffix in SCRIPT_SUFFIXES or _looks_like_command_path(executable):
        return (_resolve_command_path(executable, cwd), True,
                not _command_path_is_absolute(executable))
    return None, False, False


def inspect_launch_spec(spec, app=None):
    """Read-only validation of the structured launch definition."""
    app = app or {}
    issues = []

    def add(kind, title, detail, fix, action="edit-command"):
        issues.append({
            "kind": kind, "severity": "error", "title": title,
            "detail": detail, "fix": fix, "action": action,
        })

    cwd = spec.get("cwd") or app.get("cwd") or os.path.expanduser("~")
    cwd = os.path.abspath(os.path.expanduser(cwd))
    if not os.path.isdir(cwd):
        add("cwd-missing", "工作目录不可用",
            "找不到配置的工作目录：%s" % cwd,
            "编辑这个项目，重新选择工作区文件夹。", "pick-cwd")

    mode = spec.get("mode")
    executable = spec.get("executable")
    if mode not in ("exec", "cmd", "powershell") or not isinstance(executable, str):
        add("launch-spec-invalid", "启动配置无效",
            "结构化启动配置缺少有效的执行模式或程序路径。",
            "重新选择项目启动候选或脚本。")
        return {"status": "error", "blocking": True, "issues": issues}

    executable = os.path.abspath(os.path.expanduser(executable))
    suffix = os.path.splitext(executable)[1].casefold()
    if mode == "cmd":
        if suffix not in (".bat", ".cmd"):
            add("launch-mode-mismatch", "CMD 模式需要批处理文件",
                "CMD 启动配置必须直接指定 .bat 或 .cmd 文件：%s" % executable,
                "选择对应的批处理脚本，或改用直接执行模式。")
        elif not os.path.isfile(executable):
            add("script-missing", "脚本不可用",
                "找不到批处理脚本：%s" % executable,
                "重新选择存在的 .bat 或 .cmd 文件。", "pick-script")
        elif not _windows_command_file(executable):
            add("runtime-missing", "找不到 cmd.exe",
                "Windows 命令解释器不可用，无法运行批处理脚本。",
                "确认 Windows 系统目录可访问。")
        for argument in spec.get("args") or []:
            if any(char in argument for char in ('"', "%", "\r", "\n")):
                add("cmd-argument-unsafe", "批处理参数无法安全引用",
                    "CMD 无法无损表示包含百分号、双引号或换行的参数。",
                    "把参数移入脚本配置，或改用可直接执行的运行时。")
                break
    elif mode == "powershell":
        if not _windows_command_file(executable):
            add("runtime-missing", "找不到 PowerShell",
                "PowerShell 执行程序不存在或不是有效的 Windows 程序：%s" % executable,
                "确认 Windows PowerShell 或 pwsh 已安装。")
    else:
        if suffix in (".bat", ".cmd"):
            add("launch-mode-mismatch", "批处理文件需要 CMD 模式",
                "直接执行模式不能运行批处理文件：%s" % executable,
                "重新选择批处理脚本以生成 CMD 启动配置。")
        elif suffix == ".ps1":
            add("launch-mode-mismatch", "PowerShell 脚本需要 PowerShell 模式",
                "直接执行模式不能运行 PowerShell 脚本：%s" % executable,
                "重新选择 .ps1 脚本以生成 PowerShell 启动配置。")
        elif not _windows_command_file(executable):
            add("runtime-missing", "找不到启动程序",
                "Windows 执行程序不存在或格式无效：%s" % executable,
                "重新选择可用运行时或项目脚本。")

    args = spec.get("args") or []
    script_candidates = []
    if mode == "powershell":
        for index, arg in enumerate(args[:-1]):
            if arg.casefold() in ("-file", "/file"):
                script_candidates.append(args[index + 1])
                break
    elif mode == "exec":
        exe_base = os.path.basename(executable).casefold()
        if re.fullmatch(r"(?:python|pythonw|py)(?:\d+(?:\.\d+)*)?(?:\.exe)?", exe_base):
            if "-c" in args or "-m" in args:
                pass
            else:
                script_args = list(args)
                if script_args and script_args[0] == "--":
                    script_args = script_args[1:]
                script_candidates.extend(
                    arg for arg in script_args
                    if not arg.startswith("-") and
                    os.path.splitext(arg)[1].casefold() in SCRIPT_SUFFIXES)
        else:
            script_candidates.extend(
                arg for arg in args
                if not arg.startswith("-") and os.path.splitext(arg)[1].casefold()
                in SCRIPT_SUFFIXES.union({".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}))

    for raw_script in script_candidates:
        script_path = (raw_script if os.path.isabs(raw_script)
                       else os.path.join(cwd, raw_script))
        script_path = os.path.abspath(script_path)
        if not os.path.isfile(script_path):
            add("script-missing", "脚本不可用",
                "找不到启动脚本：%s" % script_path,
                "重新选择存在的项目脚本或修改启动参数。", "pick-script")
        elif not os.access(script_path, os.R_OK):
            add("path-unreadable", "脚本不可读取",
                "当前用户没有读取权限：%s" % script_path,
                "检查脚本权限，或重新选择一个可读取的脚本。", "pick-script")
        break

    return {"status": "error" if issues else "ok",
            "blocking": bool(issues), "issues": issues}


def inspect_app_health(app):
    """静态检查配置是否可运行；只读文件系统，绝不执行或展开用户命令。"""
    launch_spec = app.get("launchSpec")
    if (isinstance(launch_spec, dict)
            and launch_spec.get("mode") != "legacy-shell"):
        return inspect_launch_spec(launch_spec, app)
    issues = []

    def add(kind, title, detail, fix, action):
        issues.append({
            "kind": kind,
            "severity": "error",
            "title": title,
            "detail": detail,
            "fix": fix,
            "action": action,
        })

    configured_cwd = app.get("cwd")
    cwd = configured_cwd or os.path.expanduser("~")
    cwd_ok = os.path.isdir(cwd)
    if configured_cwd and not cwd_ok:
        add(
            "cwd-missing", "工作目录不可用",
            "找不到配置的工作目录：%s" % configured_cwd,
            "编辑这个项目，重新选择工作区文件夹。",
            "pick-cwd",
        )

    tokens = _simple_command_tokens(app.get("command") or "")
    if tokens is None:
        return {
            "status": "error" if issues else "unknown",
            "blocking": bool(issues),
            "issues": issues,
        }

    script_path, direct, script_was_relative = _script_target(tokens, cwd)
    if script_path and (cwd_ok or not script_was_relative):
        if not os.path.isfile(script_path):
            add(
                "script-missing", "脚本不可用",
                "找不到脚本：%s" % script_path,
                "编辑这个任务，重新选择脚本或修改执行命令。",
                "pick-script",
            )
        elif not os.access(script_path, os.R_OK):
            add(
                "path-unreadable", "脚本不可读取",
                "当前用户没有读取权限：%s" % script_path,
                "检查脚本权限，或重新选择一个可读取的脚本。",
                "pick-script",
            )
        elif direct and not _windows_command_file(script_path):
            add(
                "script-not-executable", "脚本不能由 Windows 直接运行",
                "无法识别可直接运行的 Windows 文件格式或脚本扩展名：%s" % script_path,
                "选择对应的 Python、CMD 或 PowerShell 启动方式，或选择有效的 Windows 可执行文件。",
                "edit-command",
            )

    # 直接脚本已由上面的文件检查覆盖；其他简单命令检查首个运行时。
    index = 0
    while tokens and index < len(tokens) and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[index]):
        index += 1
    executable = tokens[index] if tokens and index < len(tokens) else ""
    executable_base = os.path.basename(executable)
    if executable and not direct and executable_base not in SHELL_BUILTINS:
        if _looks_like_command_path(executable):
            runtime = _resolve_command_path(executable, cwd)
            runtime_ok = _windows_command_file(runtime)
        else:
            runtime = executable
            resolved = _resolve_runtime(executable, cwd=cwd)
            runtime_ok = bool(resolved and _windows_command_file(resolved))
        if not runtime_ok:
            add(
                "runtime-missing", "找不到 %s" % executable_base,
                "总控台的运行环境里找不到命令：%s" % executable,
                "安装对应运行时，或在编辑中修改执行命令。",
                "edit-command",
            )

    return {
        "status": "error" if issues else "ok",
        "blocking": bool(issues),
        "issues": issues,
    }


# ---------------------------------------------------------------- 项目启动识别

def _read_project_text(root, name):
    """只读取项目根目录下的小型文本配置；不存在、过大或不可读均返回 None。"""
    path = os.path.join(root, name)
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > MAX_DETECT_FILE_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(MAX_DETECT_FILE_BYTES + 1)
    except OSError:
        return None


def _port_from_command(command):
    """从常见 CLI 参数和环境变量中提取显式端口。"""
    patterns = (
        r"(?:^|\s)--port(?:=|\s+)(\d{1,5})(?=\s|$)",
        r"(?:^|\s)-p\s+(\d{1,5})(?=\s|$)",
        r"(?:^|\s)PORT\s*=\s*(\d{1,5})(?=\s|$)",
        r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{1,5})",
        r"\bhttp\.server\s+(\d{1,5})(?=\s|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, command, re.IGNORECASE)
        if match:
            port = int(match.group(1))
            if 1 <= port <= 65535:
                return port
    return None


def _package_default_port(script_name, command, dependencies):
    """根据直接依赖和脚本内容给出开发服务器的惯用端口。"""
    haystack = " ".join((script_name, command, " ".join(dependencies))).lower()
    defaults = (
        (("hexo",), 4000),
        (("gatsby",), 8000),
        (("@docusaurus/", "docusaurus"), 3000),
        (("vuepress",), 8080),
        (("docsify",), 3000),
        (("eleventy", "@11ty/eleventy"), 8080),
        (("astro",), 4321),
        (("next", "nextjs"), 3000),
        (("nuxt",), 3000),
        (("react-scripts",), 3000),
        (("vue-cli-service", "@vue/cli-service"), 8080),
        (("vite",), 4173 if script_name == "preview" else 5173),
    )
    for needles, port in defaults:
        if any(needle in haystack for needle in needles):
            return port
    return None


def _launch_spec_for_candidate(command, cwd, port=None):
    """Resolve a generated, shell-free project command to a LaunchSpec."""
    tokens = _simple_command_tokens(command)
    if not tokens:
        return None, "启动命令包含无法安全解析的 Shell 语法。"
    raw_executable = tokens[0]
    args = tokens[1:]
    base = os.path.basename(raw_executable).casefold()
    mode = "exec"

    if re.fullmatch(r"(?:py|python|pythonw)(?:\d+(?:\.\d+)*)?(?:\.exe)?",
                    base, re.IGNORECASE):
        executable = None
        if base.startswith("py"):
            executable = _project_python(cwd)
            if executable and args and re.fullmatch(
                    r"-\d+(?:\.\d+)*(?:-\d+)?|-V:\S+", args[0],
                    re.IGNORECASE):
                args = args[1:]
        if not executable:
            executable = _resolve_runtime(
                raw_executable, cwd=cwd, python_project=True)
        if not executable and base.startswith("python"):
            # The Python launcher is a reliable Windows fallback when a
            # python.exe alias is not on PATH. Prefer a concrete executable.
            executable = _resolve_runtime("py.exe", cwd=cwd)
            if executable:
                args = ["-3"] + args
    elif base in ("powershell", "powershell.exe", "pwsh", "pwsh.exe"):
        executable = _powershell_executable()
        mode = "powershell"
    elif base in ("bash", "bash.exe"):
        executable, shell = _bash_runtime()
        if executable and shell == "wsl":
            args = ["bash"] + args
    else:
        executable = _resolve_runtime(raw_executable, cwd=cwd)
        suffix = os.path.splitext(executable or raw_executable)[1].casefold()
        if suffix in (".bat", ".cmd"):
            mode = "cmd"
        elif suffix == ".ps1":
            mode = "powershell"
            args = ["-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", _resolve_command_path(raw_executable, cwd)]
            executable = _powershell_executable()
        elif base in ("sh", "sh.exe", "wsl", "wsl.exe"):
            bash, shell = _bash_runtime()
            executable = bash
            if shell == "wsl":
                args = ["bash"] + args

    if not executable:
        runtime_name = os.path.basename(raw_executable)
        return None, "找不到运行时 %s；请安装它，或在项目配置中选择可用运行时。" % runtime_name
    spec = {
        "mode": mode,
        "executable": os.path.abspath(executable),
        "args": args,
        "cwd": cwd,
        "env": {},
        "readiness": default_readiness(port),
    }
    try:
        return normalize_launch_spec(spec, cwd=cwd, port=port), None
    except LaunchSpecError as exc:
        return None, str(exc)


def resolve_launch_spec(command, cwd=None, port=None, kind="service"):
    """Resolve a user's command line to a shell-free Windows launch spec.

    The only modes inferred here are direct execution, explicit PowerShell,
    and batch-file execution. A free-form legacy shell command is never
    generated for a newly configured card.
    """
    if kind not in ("service", "task"):
        raise LaunchSpecError("kind 必须是 service/task")
    if kind == "task":
        port = None
    elif port is not None and (type(port) is not int or not 1 <= port <= 65535):
        raise LaunchSpecError("port 必须是 1-65535 的整数")
    if cwd is not None and not isinstance(cwd, str):
        raise LaunchSpecError("cwd 必须是字符串或 null")
    working_dir = os.path.abspath(os.path.expanduser(
        cwd.strip() if cwd and cwd.strip() else os.path.expanduser("~")))
    argv = _split_windows_command_line(command)
    raw_executable, args = argv[0], argv[1:]
    suffix = os.path.splitext(raw_executable)[1].casefold()
    base = os.path.basename(raw_executable).casefold()
    mode = "exec"
    executable = None

    # Script selection and typed script paths share the same project-aware
    # runtime resolution (Python venv, PowerShell, CMD, Bash/WSL).
    if suffix in SCRIPT_SUFFIXES:
        script_path = _resolve_command_path(raw_executable, working_dir)
        spec, reason = _launch_spec_for_script(script_path, working_dir, port)
        if spec is None:
            raise LaunchSpecError(reason or "找不到可运行此脚本的程序")
        if args:
            spec["args"].extend(args)
        return normalize_launch_spec(spec, cwd=working_dir, port=port)

    if base in ("powershell", "powershell.exe", "pwsh", "pwsh.exe"):
        executable = _powershell_executable()
        mode = "powershell"
        if not executable:
            raise LaunchSpecError("找不到 Windows PowerShell 或 pwsh")
    elif re.fullmatch(r"(?:py|python|pythonw)(?:\d+(?:\.\d+)*)?(?:\.exe)?",
                      base):
        executable = _resolve_runtime(
            raw_executable, cwd=working_dir, python_project=True)
        if executable:
            if base.startswith("py") and _project_python(working_dir) and args and re.fullmatch(
                    r"-\d+(?:\.\d+)*(?:-\d+)?|-V:\S+", args[0], re.IGNORECASE):
                args = args[1:]
        elif base.startswith("python"):
            executable = _resolve_runtime("py.exe", cwd=working_dir)
            if executable:
                args = ["-3"] + args
        if not executable:
            raise LaunchSpecError("找不到 Python 运行时；请安装 Python 或在项目中创建 .venv/venv/env")
    else:
        executable = _resolve_runtime(raw_executable, cwd=working_dir)
        if not executable:
            raise LaunchSpecError("找不到 Windows 运行时或可执行文件：%s" % raw_executable)
        resolved_suffix = os.path.splitext(executable)[1].casefold()
        if resolved_suffix in (".bat", ".cmd"):
            mode = "cmd"
        elif resolved_suffix == ".ps1":
            powershell = _powershell_executable()
            if not powershell:
                raise LaunchSpecError("找不到 Windows PowerShell 或 pwsh")
            args = ["-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", os.path.abspath(executable)] + args
            executable = powershell
            mode = "powershell"

    spec = {
        "mode": mode,
        "executable": os.path.abspath(executable),
        "args": args,
        "cwd": working_dir,
        "env": {},
        "readiness": default_readiness(port),
    }
    return normalize_launch_spec(spec, cwd=working_dir, port=port)


def detect_project(root):
    """只读分析项目根目录，返回已解析的 Windows 启动候选。"""
    if not isinstance(root, str) or not root.strip():
        return None, "请选择项目文件夹"
    root = os.path.abspath(os.path.expanduser(root.strip()))
    if not os.path.isdir(root):
        return None, "项目文件夹不存在或不可访问"

    candidates = []
    detected_files = []

    def note_file(name, text=None):
        path = os.path.join(root, name)
        exists = text is not None or os.path.isfile(path)
        if exists and name not in detected_files:
            detected_files.append(name)
        return exists

    def add(command, label, source, port=None, priority=50, detail=None,
            kind="service", launch_spec=None, unavailable_reason=None):
        if not command or any(item["command"] == command for item in candidates):
            return
        if port is not None and not (isinstance(port, int) and 1 <= port <= 65535):
            port = None
        if launch_spec is None and unavailable_reason is None:
            launch_spec, unavailable_reason = _launch_spec_for_candidate(
                command, root, port if kind != "task" else None)
        candidate = {
            "command": command,
            "label": label,
            "source": source,
            "port": port,
            "kind": "task" if kind == "task" else "service",
            "detail": detail,
            "_priority": priority,
            "available": launch_spec is not None,
        }
        if launch_spec is not None:
            candidate["launchSpec"] = launch_spec
        else:
            candidate["unavailableReason"] = unavailable_reason or "无法解析启动运行时。"
        candidates.append(candidate)

    # Node / 前端 / 博客项目：优先读取 package.json 的 scripts。
    package = {}
    scripts = {}
    deps = set()
    hexo_config = os.path.isfile(os.path.join(root, "_config.yml"))
    is_hexo = hexo_config and (
        os.path.isdir(os.path.join(root, "source")) or
        os.path.isdir(os.path.join(root, "scaffolds")) or
        os.path.isdir(os.path.join(root, "themes")))
    package_text = _read_project_text(root, "package.json")
    if package_text is not None:
        note_file("package.json", package_text)
        try:
            package = json.loads(package_text)
        except (TypeError, ValueError):
            package = {}
        scripts = package.get("scripts") if isinstance(package, dict) else {}
        if not isinstance(scripts, dict):
            scripts = {}
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            values = package.get(key) if isinstance(package, dict) else None
            if isinstance(values, dict):
                deps.update(str(name).lower() for name in values)
        is_hexo = (is_hexo or "hexo" in deps or
                   (isinstance(package, dict) and isinstance(package.get("hexo"), dict)))

        if os.path.isfile(os.path.join(root, "pnpm-lock.yaml")):
            runner = "pnpm run"
            note_file("pnpm-lock.yaml")
        elif (os.path.isfile(os.path.join(root, "bun.lock")) or
              os.path.isfile(os.path.join(root, "bun.lockb"))):
            runner = "bun run"
            note_file("bun.lock" if os.path.isfile(os.path.join(root, "bun.lock")) else "bun.lockb")
        elif os.path.isfile(os.path.join(root, "yarn.lock")):
            runner = "yarn"
            note_file("yarn.lock")
        else:
            runner = "npm run"

        labels = {
            "dev": "开发服务器", "develop": "开发服务器",
            "start": "正式启动", "serve": "本地服务", "server": "本地服务",
            "preview": "本地预览", "docs": "文档站",
            "storybook": "组件预览",
        }
        preferred = ("dev", "develop", "start", "serve", "server", "preview", "docs", "storybook")
        ordered = [name for name in preferred if name in scripts]
        service_name = re.compile(r"(?:^|[:_-])(dev|develop|start|serve|server|preview|watch|docs|storybook|web|blog)(?:$|[:_-])", re.I)
        ordered.extend(name for name in scripts if name not in ordered and service_name.search(str(name)))
        for index, name in enumerate(ordered[:8]):
            script = scripts.get(name)
            if not isinstance(script, str):
                continue
            if is_hexo and str(name).lower() == "server" and re.search(
                    r"\bhexo\s+(?:s|server)\b", script, re.I):
                continue  # 下方提供更短、更通用的 hexo s，不重复同一操作
            name = str(name)
            command = "%s %s" % (
                runner, name if re.fullmatch(r"[\w:-]+", name) else _quote_win(name))
            port = _port_from_command(script)
            if port is None:
                port = _package_default_port(str(name).lower(), script, deps)
            add(command, labels.get(str(name).lower(), "项目脚本：%s" % name),
                "package.json · scripts.%s" % name, port,
                10 + index, "由项目自己的脚本定义")

    # Hexo 即使没有 scripts 也有稳定 CLI：服务与清缓存分别作为服务/任务。
    if is_hexo:
        if hexo_config:
            note_file("_config.yml")
        add("hexo s", "Hexo 本地服务", "Hexo 项目结构", 4000, 8,
            "等同于 hexo server")
        add("hexo cl", "Hexo 清除缓存", "Hexo 项目结构", None, 9,
            "清除缓存和已生成文件，不启动服务", kind="task")

    # 常见博客与静态站点生成器。
    hugo_config = next((name for name in ("hugo.toml", "hugo.yaml", "hugo.yml")
                        if os.path.isfile(os.path.join(root, name))), None)
    if hugo_config or (os.path.isdir(os.path.join(root, "content")) and
                       os.path.isdir(os.path.join(root, "layouts")) and
                       os.path.isfile(os.path.join(root, "config.toml"))):
        source = hugo_config or "config.toml"
        note_file(source)
        add("hugo server -D", "Hugo 本地预览", source, 1313, 18,
            "包含草稿内容")

    gemfile = _read_project_text(root, "Gemfile")
    if gemfile is not None:
        note_file("Gemfile", gemfile)
        if "jekyll" in gemfile.lower():
            add("bundle exec jekyll serve", "Jekyll 本地预览", "Gemfile", 4000, 19)

    # Python Web 项目。
    pyproject = _read_project_text(root, "pyproject.toml")
    requirements = _read_project_text(root, "requirements.txt")
    if pyproject is not None:
        note_file("pyproject.toml", pyproject)
    if requirements is not None:
        note_file("requirements.txt", requirements)
    py_deps = "\n".join(text for text in (pyproject, requirements) if text).lower()
    python_command = PYTHON_CMD
    python_module_runner = PYTHON_CMD + " -m"
    if os.path.isfile(os.path.join(root, "uv.lock")):
        note_file("uv.lock")
        python_command = "uv run python"
        python_module_runner = "uv run"
    elif os.path.isfile(os.path.join(root, "poetry.lock")):
        note_file("poetry.lock")
        python_command = "poetry run python"
        python_module_runner = "poetry run"
    if os.path.isfile(os.path.join(root, "manage.py")):
        note_file("manage.py")
        add(python_command + " manage.py runserver", "Django 开发服务器", "manage.py", 8000, 20)
    else:
        for module_file in ("app.py", "main.py", "server.py"):
            module_text = _read_project_text(root, module_file)
            if module_text is None:
                continue
            module = os.path.splitext(module_file)[0]
            imports_streamlit = re.search(
                r"(?m)^\s*(?:import\s+streamlit\b|from\s+streamlit\b)", module_text)
            imports_fastapi = re.search(
                r"(?m)^\s*(?:import\s+fastapi\b|from\s+fastapi\b)", module_text)
            imports_flask = re.search(
                r"(?m)^\s*(?:import\s+flask\b|from\s+flask\b)", module_text)
            if "streamlit" in py_deps or imports_streamlit:
                note_file(module_file, module_text)
                add(python_module_runner + " streamlit run " + module_file,
                    "Streamlit 应用", module_file, 8501, 22)
                break
            if "fastapi" in py_deps or imports_fastapi:
                note_file(module_file, module_text)
                add(python_module_runner + " uvicorn %s:app --reload" % module,
                    "FastAPI 开发服务器", module_file, 8000, 23)
                break
            if "flask" in py_deps or imports_flask:
                note_file(module_file, module_text)
                add(python_module_runner + " flask --app %s run --debug" % module,
                    "Flask 开发服务器", module_file, 5000, 24)
                break

    # Docker Compose、Go、Rust 和已有的常用启动脚本。
    compose_name = next((name for name in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
                         if os.path.isfile(os.path.join(root, name))), None)
    if compose_name:
        compose_text = _read_project_text(root, compose_name)
        note_file(compose_name, compose_text)
        port = None
        if compose_text:
            match = re.search(r"[\"']?(\d{2,5})\s*:\s*\d{2,5}[\"']?", compose_text)
            if match and 1 <= int(match.group(1)) <= 65535:
                port = int(match.group(1))
        add("docker compose up", "Docker Compose", compose_name, port, 55,
            "以前台方式运行，停止按钮可正常关闭")
    if os.path.isfile(os.path.join(root, "go.mod")):
        note_file("go.mod")
        add("go run .", "Go 项目", "go.mod", None, 60)
    if os.path.isfile(os.path.join(root, "Cargo.toml")):
        note_file("Cargo.toml")
        add("cargo run", "Rust 项目", "Cargo.toml", None, 61)

    for script_name in ("start.bat", "start.cmd", "dev.bat", "run.bat",
                        "start.ps1", "start.sh", "dev.sh", "run.sh"):
        script_path = os.path.join(root, script_name)
        if not os.path.isfile(script_path):
            continue
        suffix = os.path.splitext(script_name)[1].lower()
        spec, reason = _launch_spec_for_script(script_path, root)
        # A .sh file is not a useful candidate unless this machine can execute
        # it through Bash or WSL. Do not advertise a command that can only fail.
        if suffix in (".sh", ".bash") and spec is None:
            continue
        note_file(script_name)
        if spec:
            command = command_from_launch_spec(spec)
        elif suffix in (".bat", ".cmd"):
            command = _quote_win(os.path.abspath(script_path))
        elif suffix == ".ps1":
            command = "powershell -NoProfile -ExecutionPolicy Bypass -File %s" % _quote_win(
                os.path.abspath(script_path))
        else:
            command = "bash -- %s" % _quote_win(os.path.abspath(script_path))
        add(command,
            "现有启动脚本", script_name, None, 70,
            "也可以继续使用“选择脚本”手动指定",
            launch_spec=spec, unavailable_reason=reason)
        break

    # 纯静态站点最后兜底，避免把 Vite/Next 等项目误当成普通文件目录。
    if not candidates and os.path.isfile(os.path.join(root, "index.html")):
        note_file("index.html")
        add(PYTHON_CMD + " -m http.server 8000", "静态网站预览", "index.html", 8000, 90)

    candidates.sort(key=lambda item: item.pop("_priority"))
    return {
        "ok": True,
        "cwd": root,
        "name": os.path.basename(root) or root,
        "files": detected_files,
        "candidates": candidates[:8],
    }, None


def _current_user_group_members(pgid):
    """Return live current-user members of a previously verified group.

    Once SIGTERM is sent the token-bearing controller may exit before a child
    that ignores SIGTERM.  Requiring the marker again would incorrectly report
    success, so the wait phase follows the already-verified PGID until empty.
    """
    members = sysops.group_members(pgid)
    if not members:
        return []
    snap = ps_snapshot(members, with_uid=True)
    return sorted(pid for pid in members
                  if is_current_user(snap.get(pid, {}).get("uid")))


def resolve_app_stop_target(app, listeners=None):
    """Resolve and validate a stop target before any signal is sent."""
    instance = app.get("runInstance")
    if (app.get("controlMode") == "managed"
            and isinstance(instance, dict) and instance.get("jobName")
            and instance.get("processState") != "exited"):
        job = _open_run_job(app)
        if job is RUN_JOB_REOPEN_FAILED:
            return None, "无法重连应用的 Job Object，未执行停止"
        if job is not None:
            try:
                members = job.members()
            except Exception as exc:
                LOG.exception("读取应用 %s 的 Job Object 成员失败",
                              app.get("id"))
                if _is_anchor_cleanup_failure(exc):
                    _remember_run_job(app, job, "empty-cleanup")
                else:
                    _release_run_job_handle(app, job)
                return None, "无法读取 Job Object 状态，未执行停止：%s" % (
                    str(exc) or type(exc).__name__)
            if members:
                return {"kind": "job", "id": instance.get("runId"),
                        "members": list(members), "job": job}, None
            try:
                _release_run_job_handle(app, job)
            except Exception as exc:
                LOG.exception("关闭应用 %s 的 Job Object 句柄失败",
                              app.get("id"))
                return None, "无法关闭 Job Object 句柄，未执行停止：%s" % (
                    str(exc) or type(exc).__name__)
    current = managed_pids(app)
    if current:
        pgid = app.get("lastPgid") or app.get("lastPid")
        if isinstance(pgid, int) and pgid > 0:
            return {"kind": "group", "id": pgid, "members": list(current)}, None
        return None, "受控进程组信息无效"
    legacy_pid = legacy_managed_pid(app, listeners)
    if legacy_pid:
        return {"kind": "pid", "id": legacy_pid, "members": [legacy_pid]}, None
    return None, "无法确认受控进程，未执行停止"


def signal_app_stop(target, sig=signal.SIGTERM,
                    timeout=APP_STOP_TIMEOUT_SEC):
    """Signal a target returned by resolve_app_stop_target."""
    if target["kind"] == "job":
        return target["job"].terminate(
            force=False, timeout=max(0.0, float(timeout)))
    ident = target["id"]
    if target["kind"] == "group":
        members = target.get("members")
        return sysops.signal_group(ident, sig, members=members)
    return sysops.kill_process(ident, force=False)


def stop_target_alive(target, expected_uid=None):
    if target["kind"] == "job":
        try:
            return bool(target["job"].members())
        except OSError:
            # Unknown Job state is not proof of exit. Keep the app managed and
            # let the caller return a timeout instead of clearing live state.
            return True
    if target["kind"] == "group":
        return any(pid_alive(pid) for pid in target.get("members") or [])
    if not sysops.pid_alive(target["id"]):
        return False
    if expected_uid is None:
        expected_uid = process_uid(target["id"])
    return is_current_user(expected_uid)


def stop_app_and_wait(app, timeout=APP_STOP_TIMEOUT_SEC, listeners=None):
    with RUN_JOB_ACCESS_LOCK:
        return _stop_app_and_wait_unlocked(app, timeout, listeners)


def _stop_app_and_wait_unlocked(app, timeout=APP_STOP_TIMEOUT_SEC,
                                listeners=None):
    """Signal a verified app and wait until the exact target is gone.

    Returns (ok, error).  A timeout is deliberately not escalated to SIGKILL;
    the caller keeps the runtime token so the user can retry or choose a force
    action without losing control of a still-live process.
    """
    target, error = resolve_app_stop_target(app, listeners)
    if target is None:
        return False, error
    try:
        ok, error = signal_app_stop(target, timeout=timeout)
        if not ok:
            return False, error
        deadline = time.monotonic() + max(0.0, timeout)
        # uid 只查一次：信号已在循环外发出，循环仅做存活探测。
        expected_uid = (process_uid(target["id"])
                        if target["kind"] == "pid" else None)
        while stop_target_alive(target, expected_uid):
            if time.monotonic() >= deadline:
                if target["kind"] == "pid":
                    remaining = target["members"]
                else:
                    remaining = [pid for pid in target.get("members") or []
                                 if pid_alive(pid)]
                suffix = ("（PID %s）" % "、".join(str(p) for p in remaining)
                          if remaining else "")
                return False, "应用未在 %.1f 秒内退出%s，仍保留管理状态" % (
                    timeout, suffix)
            time.sleep(0.05)
        return True, None
    finally:
        if target.get("kind") == "job":
            _release_run_job_handle(app, target["job"])


def stop_app_and_clear(cfg, app, timeout=APP_STOP_TIMEOUT_SEC, listeners=None):
    """Manual stop transaction: wait first, clear persisted identity last."""
    marker = (app.get("id"), app.get("runToken"))
    with MANUAL_STOP_LOCK:
        MANUAL_STOP_TOKENS.add(marker)

    def set_process_state(state):
        def op(data):
            target = find_app(data, app.get("id"))
            instance = target.get("runInstance") if target else None
            if (target and target.get("runToken") == app.get("runToken")
                    and isinstance(instance, dict)
                    and instance.get("runId") == (app.get("runInstance") or {}).get("runId")):
                instance["processState"] = state
        cfg.update(op)

    try:
        set_process_state("stopping")
        ok, error = stop_app_and_wait(app, timeout, listeners)
        if not ok:
            set_process_state("alive" if app_alive_sign(app, listeners) else "exited")
            return False, error
        last_exit = None
        if (app.get("kind") or "service") == "task":
            # 覆盖可能保留的旧成功记录，避免“刚刚手动停止”仍显示上次成功。
            last_exit = {
                "status": "stopped",
                "code": None,
                "at": int(time.time()),
            }
        if not clear_app_runtime(
                cfg, app["id"], app.get("runToken"), last_exit=last_exit):
            return False, "进程已停止，但应用状态已变化，请刷新后重试"
        return True, None
    finally:
        with MANUAL_STOP_LOCK:
            MANUAL_STOP_TOKENS.discard(marker)


def inspect_attach_process(cfg, app, pid):
    """只读校验待认领进程，返回其可信工作目录。

    创建卡片时先调用本函数，再把卡片与运行身份一次写入配置，避免前端
    “先创建、再认领”只完成一半。已有卡片的手动认领也复用同一套校验。"""
    if (app.get("kind") or "service") != "service":
        return False, "批处理任务没有端口，无法认领进程", {"status": 422}
    port = app.get("port")
    if not isinstance(port, int) or port <= 0:
        return False, "卡片未配置端口，无法认领进程", {"status": 422}
    if app_alive_sign(app):
        return False, "应用已在运行", {"status": 409}
    if pid == os.getpid():
        return False, "不能认领总控台自身", {"status": 409}
    listeners = scan_listeners()
    if (pid, port) not in listeners:
        return False, "PID %d 并未监听端口 %d，进程可能已退出" % (pid, port), {"status": 409}
    snap = ps_snapshot({pid}, with_uid=True)
    if not is_current_user(snap.get(pid, {}).get("uid")):
        return False, "该进程不属于当前用户，不能认领", {"status": 403}
    cfg_now = cfg.snapshot()
    owners = listener_app_owners(cfg_now.get("apps") or [], listeners, snap, None)
    if pid in owners and owners[pid].get("id") != app.get("id"):
        return False, "该进程已由卡片「%s」管理" % owners[pid].get("name", ""), {"status": 409}
    actual_cwd = lsof_cwds({pid}).get(pid)
    if not actual_cwd:
        return False, "无法读取进程工作目录，已取消认领", {"status": 409}
    # 记录进程创建时间（仅 Windows 提供），供后续身份校验识别 PID 复用。
    return True, None, {
        "status": 200, "cwd": actual_cwd,
        "ctime": snap.get(pid, {}).get("ctime"),
        "sid": snap.get(pid, {}).get("uid"),
    }


def observation_from_identity(pid, port, identity):
    return {
        "pid": pid,
        "createTime": identity.get("ctime"),
        "sid": identity.get("sid") or SELF_UID,
        "cwd": identity.get("cwd"),
        "ports": [port] if isinstance(port, int) else [],
        "observedAt": int(time.time()),
    }


def attach_identity_still_matches(pid, port, identity):
    """Revalidate an attach target immediately before the config commit.

    The initial inspection happens before the new card exists. A process can
    exit or its PID can be reused during candidate parsing, so the commit path
    repeats the listener, SID, creation-time and cwd checks while Config's
    write lock is held.
    """
    if not isinstance(pid, int) or not isinstance(port, int):
        return False
    listeners = scan_listeners()
    if (pid, port) not in listeners:
        return False
    snap = ps_snapshot({pid}, with_uid=True)
    current = snap.get(pid) or {}
    if not is_current_user(current.get("uid")):
        return False
    expected_sid = identity.get("sid") if isinstance(identity, dict) else None
    if expected_sid and current.get("uid") != expected_sid:
        return False
    expected_ctime = identity.get("ctime") if isinstance(identity, dict) else None
    current_ctime = current.get("ctime")
    if expected_ctime is not None and current_ctime != expected_ctime:
        return False
    expected_cwd = identity.get("cwd") if isinstance(identity, dict) else None
    actual_cwd = lsof_cwds({pid}).get(pid)
    if not expected_cwd or not actual_cwd:
        return False
    try:
        return (os.path.normcase(os.path.realpath(actual_cwd)) ==
                os.path.normcase(os.path.realpath(expected_cwd)))
    except (OSError, TypeError, ValueError):
        return actual_cwd == expected_cwd


def observation_port(observation):
    """Return the port recorded with an external process observation.

    A claimed process is allowed to keep running while the user edits the
    future LaunchSpec.  In that case ``app.port`` describes the next launch,
    while this value remains the port on which the already running process
    was observed.  Older v2 records only have ``ports``; accept both forms so
    migrations and hand-written test fixtures remain compatible.
    """
    if not isinstance(observation, dict):
        return None
    value = observation.get("port")
    if type(value) is int and value > 0:
        return value
    ports = observation.get("ports")
    if isinstance(ports, (list, tuple)):
        for value in ports:
            if type(value) is int and value > 0:
                return value
    return None


def attached_observation(app):
    """Return the immutable external identity for an attached card.

    ``attached`` intentionally stays independent from ``controlMode``.  A
    monitor card promoted to managed still controls the old external process
    through this observation until it is stopped; changing cwd/port in the
    LaunchSpec must not silently retarget that process.
    """
    if not isinstance(app, dict) or not app.get("attached"):
        return None
    observation = app.get("observation")
    return observation if isinstance(observation, dict) else None


def _observation_matches_identity(observation, pid, identity,
                                 fallback_ctime=None):
    """Return whether a saved observation still denotes this PID instance."""
    if not isinstance(observation, dict) or observation.get("pid") != pid:
        return False
    saved_ctime = observation.get("createTime")
    if saved_ctime is None:
        saved_ctime = fallback_ctime
    current_ctime = identity.get("ctime") if isinstance(identity, dict) else None
    if saved_ctime is not None and current_ctime is None:
        return False
    if saved_ctime is not None and current_ctime is not None:
        try:
            if abs(float(saved_ctime) - float(current_ctime)) > 0.0001:
                return False
        except (TypeError, ValueError):
            return False
    saved_sid = observation.get("sid")
    current_sid = identity.get("sid") if isinstance(identity, dict) else None
    return not (saved_sid and current_sid and saved_sid != current_sid)


def attach_app_process(cfg, app_id, app, pid):
    """Record an observation for a listener without claiming control over it."""
    ok, error, identity = inspect_attach_process(cfg, app, pid)
    if not ok:
        return False, error, identity
    pid_conflict = False
    attach_result = {}

    def op(c):
        nonlocal pid_conflict
        target = find_app(c, app_id)
        if not target:
            return False
        # 认领检查与写入必须同锁：inspect 用的是旧快照，并发请求可能同时
        # 通过校验。在写锁内重验 pid 是否已被其他卡片认领。
        if any(
                (_observation_matches_identity(
                    other.get("observation"), pid, identity,
                    other.get("lastCreateTime"))
                 or (other.get("observation") is None
                     and other.get("lastPid") == pid
                     and (other.get("lastCreateTime") is None
                          or identity.get("ctime") is None
                          or other.get("lastCreateTime") == identity.get("ctime"))))
                for other in c.get("apps") or [] if other.get("id") != app_id):
            pid_conflict = True
            return False
        target["lastPid"] = pid
        target["lastPgid"] = None
        target["runToken"] = None
        target["controlMode"] = "monitor"
        target["attached"] = True
        target["lastCreateTime"] = identity.get("ctime")
        target["runInstance"] = None
        target["readinessState"] = "unknown"
        previous_cwd = target.get("cwd")
        actual_cwd = identity.get("cwd")
        if actual_cwd:
            try:
                cwd_updated = (
                    os.path.normcase(os.path.realpath(actual_cwd))
                    != os.path.normcase(os.path.realpath(previous_cwd or "")))
            except (OSError, TypeError, ValueError):
                cwd_updated = actual_cwd != previous_cwd
            target["cwd"] = actual_cwd
        else:
            cwd_updated = False
        target["observation"] = observation_from_identity(
            pid, target.get("port"), identity)
        attach_result.update({
            "cwd": target.get("cwd"),
            "cwdUpdated": cwd_updated,
            "observation": target["observation"],
        })
        return True

    if not cfg.update(op):
        if pid_conflict:
            return False, "该进程已由其他卡片管理", {"status": 409}
        return False, "应用已被删除", {"status": 404}
    return True, None, {"controlMode": "monitor", **attach_result}


# ---------------------------------------------------------------- 日志

def rotate_log_file(path, max_bytes=MAX_LOG_BYTES, backups=LOG_BACKUPS):
    """超限后 copy-truncate，保持子进程已打开的文件描述符继续可写。"""
    with LOG_LOCK:
        try:
            if os.path.getsize(path) <= max_bytes:
                return False
        except OSError:
            return False
        try:
            for index in range(backups, 1, -1):
                older = "%s.%d" % (path, index - 1)
                newer = "%s.%d" % (path, index)
                if os.path.exists(older):
                    os.replace(older, newer)
            shutil.copyfile(path, path + ".1")
            os.chmod(path + ".1", 0o600)
            with open(path, "r+b") as f:
                f.truncate(0)
            os.chmod(path, 0o600)
            return True
        except OSError:
            LOG.exception("轮转日志失败: %s", path)
            return False


def _tail_file_lines(path, count, block_size=65536):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            chunks = []
            newlines = 0
            while pos > 0 and newlines <= count:
                size = min(block_size, pos)
                pos -= size
                f.seek(pos)
                chunk = f.read(size)
                if not chunk.strip(b"\x00"):
                    break  # 空洞/被外部截断后残留的 NUL 段：之前没有内容，停止回扫
                chunks.append(chunk)
                newlines += chunk.count(b"\n")
        data = b"".join(reversed(chunks))
        return data.decode("utf-8", errors="replace").splitlines()[-count:]
    except OSError:
        return []


def read_log_tail(app_id, count):
    """从当前日志和轮转备份中高效读取最后 count 行。"""
    path = os.path.join(LOGS_DIR, "%s.log" % app_id)
    rotate_log_file(path)
    collected = []
    with LOG_LOCK:
        for candidate in [path] + ["%s.%d" % (path, i)
                                   for i in range(1, LOG_BACKUPS + 1)]:
            remaining = count - len(collected)
            if remaining <= 0:
                break
            lines = _tail_file_lines(candidate, remaining)
            collected = lines + collected
    return "\n".join(collected[-count:])


def start_log_maintenance():
    def _maintain():
        while True:
            try:
                for name in os.listdir(LOGS_DIR):
                    if name.endswith(".log"):
                        rotate_log_file(os.path.join(LOGS_DIR, name))
            except OSError:
                LOG.exception("日志维护失败")
            time.sleep(LOG_MAINTENANCE_SEC)
    threading.Thread(target=_maintain, daemon=True).start()


def sniff_image(data):
    """magic bytes 校验 → "png" / "jpg" / "webp" / None。"""
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


# ---------------------------------------------------------------- 站点图标抓取

ICON_LINK_RE = re.compile(
    r"<link[^>]+rel=[\"'][^\"']*icon[^\"']*[\"'][^>]*>", re.I)
HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.I)


def is_loopback_service_url(url, port):
    """仅允许抓取指定端口的明文 loopback URL，避免 favicon SSRF。"""
    try:
        parsed = urllib.parse.urlsplit(url)
        return (parsed.scheme == "http"
                and (parsed.hostname or "").lower() in (
                    "127.0.0.1", "localhost", "::1")
                and parsed.port == port
                and not parsed.username and not parsed.password)
    except (TypeError, ValueError, UnicodeError):
        return False


class LoopbackRedirectHandler(urllib.request.HTTPRedirectHandler):
    """只跟随仍停留在同一 loopback 端口的重定向。"""

    def __init__(self, port):
        super().__init__()
        self.port = port

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_loopback_service_url(newurl, self.port):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get(url, port, timeout=3, limit=262144):
    """GET → (bytes, content-type) | (None, None)。仅抓同一 loopback 端口。"""
    if not is_loopback_service_url(url, port):
        return None, None
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Console/1.0", "Accept": "*/*"})
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), LoopbackRedirectHandler(port))
        with opener.open(req, timeout=timeout) as r:
            return r.read(limit), (r.headers.get("Content-Type") or "")
    except Exception:
        return None, None


def sniff_icon_bytes(data, ctype=""):
    """→ "png" / "jpg" / "webp" / "ico" / None。拒绝主动 SVG 内容。"""
    if len(data) >= 4 and data[:4] == b"\x00\x00\x01\x00":
        return "ico"
    ext = sniff_image(data)
    if ext:
        return ext
    return None


def fetch_favicon(port, host="127.0.0.1"):
    """抓本地站点图标 → (bytes, ext) | (None, None)。
    先解析首页 <link rel=...icon...>（含 apple-touch-icon），兜底 /favicon.ico。"""
    if host not in ("127.0.0.1", "localhost"):
        host = "127.0.0.1"
    base = "http://%s:%d" % (host, port)
    candidates = []
    html, _ = http_get(base + "/", port)
    if html:
        text = html.decode("utf-8", errors="replace")
        for m in ICON_LINK_RE.finditer(text):
            hm = HREF_RE.search(m.group(0))
            if hm:
                url = urllib.parse.urljoin(base + "/", hm.group(1))
                if is_loopback_service_url(url, port):
                    candidates.append(url)
    candidates.append(base + "/favicon.ico")
    for url in candidates[:4]:
        data, ctype = http_get(url, port, limit=1024 * 1024)
        if data:
            ext = sniff_icon_bytes(data, ctype)
            if ext:
                return data, ext
    return None, None


def find_app(cfg, app_id):
    for app in cfg.get("apps") or []:
        if app.get("id") == app_id:
            return app
    return None


def diagnose_app(cfg, app):
    """规则诊断：退出码 + 日志模式 + 文件系统检查 → 可执行的修复建议列表。

    覆盖常见失败：依赖未装、命令/脚本不存在、运行时缺失、npm 脚本名错误、
    端口占用、权限不足、Python 包缺失。
    """
    issues = []

    def add(kind, title, detail, fix, action=None):
        if not any(i["kind"] == kind for i in issues):
            issue = {"kind": kind, "title": title,
                     "detail": detail, "fix": fix}
            if action:
                issue["action"] = action
            issues.append(issue)

    app_id = app.get("id") or ""
    cwd = app.get("cwd") or ""
    last_exit = app.get("lastExit") or {}
    code = last_exit.get("code")
    port = app.get("port")
    log_tail = read_log_tail(app_id, 150) if app_id else ""
    log_lower = log_tail.lower()

    # ---- 配置层检查（不依赖日志） ----
    for health_issue in inspect_app_health(app).get("issues", []):
        add(
            health_issue["kind"],
            health_issue["title"],
            health_issue["detail"],
            health_issue["fix"],
            health_issue.get("action"),
        )

    pkg_json = os.path.join(cwd, "package.json") if cwd else ""
    has_pkg = bool(cwd) and os.path.isfile(pkg_json)
    has_node_modules = bool(cwd) and os.path.isdir(os.path.join(cwd, "node_modules"))
    if has_pkg and not has_node_modules:
        mgr = ("yarn" if os.path.isfile(os.path.join(cwd, "yarn.lock"))
               else "pnpm" if os.path.isfile(os.path.join(cwd, "pnpm-lock.yaml"))
               else "npm")
        add("deps-missing", "依赖未安装（node_modules 缺失）",
            "目录里有 package.json，但没有 node_modules。",
            "终端执行：cd \"%s\" && %s install，装完再启动。" % (cwd, mgr))

    # ---- 日志模式匹配 ----
    m = re.search(r"cannot find module '([^']+)'", log_lower)
    if m:
        add("deps-missing", "找不到模块 %s" % m.group(1),
            "日志报 Cannot find module '%s'，通常是依赖没装或装坏了。" % m.group(1),
            "终端执行：cd \"%s\" && npm install（仍报错再 rm -rf node_modules 后重装）。" % (cwd or "<项目目录>"))

    m = re.search(r"(?:env: )?(\S+): (?:no such file or directory|command not found)", log_lower)
    if m and "cannot find module" not in log_lower:
        add("runtime-missing", "找不到运行时：%s" % m.group(1),
            "系统里找不到 %s 这个命令。" % m.group(1),
            "确认该运行时已安装（如 node / python / pnpm）；总控台启动时会补常见 PATH，但程序本身需要存在。")

    if "missing script" in log_lower and has_pkg:
        script_names = []
        try:
            with open(pkg_json, "r", encoding="utf-8") as f:
                script_names = list((json.load(f).get("scripts") or {}).keys())
        except Exception:
            pass
        hint = ("package.json 里可用的脚本：%s。" % "、".join(script_names)
                if script_names else "package.json 里没有 scripts。")
        add("npm-script", "npm 脚本名写错了",
            "日志报 missing script。%s" % hint,
            "把启动命令改成上面列出的脚本名，例如 npm run %s。" % (script_names[0] if script_names else "dev"))

    if "eaddrinuse" in log_lower or "address already in use" in log_lower:
        add("port-busy", "端口被占用",
            "日志报地址已占用%s。" % ("（:%s）" % port if port else ""),
            "点卡片上的端口数字看是谁占用的，停掉它或给本应用换个端口。")

    if "eacces" in log_lower or "permission denied" in log_lower:
        add("perm", "权限不足",
            "日志报权限不足（EACCES / permission denied）。",
            "检查文件/目录权限；Windows 上请确认当前用户可读写该路径，不要用管理员权限硬跑。")

    m = re.search(r"modulenotfounderror: no module named '([^']+)'", log_lower)
    if m:
        venv_hint = "%s -m venv .venv && .venv\\Scripts\\pip install %s"
        add("pip-missing", "缺少 Python 包：%s" % m.group(1),
            "日志报 ModuleNotFoundError: No module named '%s'。" % m.group(1),
            "建议在项目目录建虚拟环境再装：%s" % (venv_hint % m.group(1)))

    if re.search(r"no such file or directory", log_lower) and not issues:
        add("file-missing", "命令里的文件/脚本不存在",
            "日志报 No such file or directory，命令里引用的路径可能写错了。",
            "检查启动命令和工作目录里的相对路径是否正确。")

    # ---- 退出码兜底 ----
    if not issues:
        if code == 126:
            add("not-exec", "命令没有执行权限（exit 126）",
                "退出码 126 表示文件不可执行。",
                "改用 python / powershell 启动脚本，或检查文件是否存在、当前用户是否可执行。")
        elif code == 127:
            add("not-found", "命令不存在（exit 127）",
                "退出码 127 表示 shell 找不到这个命令。",
                "确认命令已安装且在 PATH 里；总控台会补常见路径，但程序本身要存在。")
        elif (isinstance(code, int) and code == 0
              and (app.get("kind") or "service") != "task"):
            add("quick-exit", "命令立即正常退出（exit 0）",
                "进程启动后马上正常结束——长期服务命令不应立刻退出。",
                "确认写的是常驻命令（如 hexo s / npm run dev），而不是一次就完成的命令。")
        elif isinstance(code, int) and code < 0:
            add("signaled", "进程被信号终止（signal %d）" % -code,
                "进程不是自然退出，是被系统信号杀掉的。",
                "常见于内存不足被系统回收或外部 kill；查看系统日志确认原因。")

    # ---- 汇总 ----
    if issues:
        summary = "发现 %d 个可能原因，按「修复建议」处理后再启动。" % len(issues)
    elif not log_tail.strip():
        summary = "暂无日志可供诊断；先启动一次让日志产生，再看完整日志定位。"
    elif code is None:
        summary = "该应用还没有退出记录；当前日志未见明显异常。"
    else:
        summary = "日志里没有命中常见错误模式，建议打开完整日志人工排查。"
    return {"ok": True, "issues": issues, "summary": summary}


def validate_port(value):
    """→ (port|None, error|None)。接受 null / 整数 / 数字字符串，范围 1-65535。"""
    if value is None or value == "":
        return None, None
    if isinstance(value, bool):
        return None, "port 必须是 1-65535 的整数"
    if isinstance(value, int):
        port = value
    elif isinstance(value, str) and value.strip().isdigit():
        port = int(value.strip())
    else:
        return None, "port 必须是 1-65535 的整数"
    if not (1 <= port <= 65535):
        return None, "port 必须在 1-65535 之间"
    return port, None


def validate_app_fields(data, partial):
    """校验/规范化应用字段。partial=True 时仅校验出现的字段。
    返回 (fields, error)：fields 为规范化后的字段子集。"""
    fields = {}
    for key in ("name", "command"):
        if key in data:
            v = data[key]
            if not isinstance(v, str) or not v.strip():
                return None, "字段 %s 必须是非空字符串" % key
            fields[key] = v.strip()
        elif not partial:
            return None, "缺少字段 %s" % key
    if "cwd" in data:
        v = data["cwd"]
        if v is not None and not isinstance(v, str):
            return None, "cwd 必须是字符串或 null"
        fields["cwd"] = (v or "").strip() or None if isinstance(v, str) else None
    elif not partial:
        fields["cwd"] = None
    if "port" in data:
        port, err = validate_port(data["port"])
        if err:
            return None, err
        fields["port"] = port
    elif not partial:
        fields["port"] = None
    if "emoji" in data:
        v = data["emoji"]
        if v is not None and not isinstance(v, str):
            return None, "emoji 必须是字符串或 null"
        fields["emoji"] = (v or None)
    elif not partial:
        fields["emoji"] = None
    if "glyph" in data:
        v = data["glyph"]
        if v is not None and (not isinstance(v, str) or len(v) > 40):
            return None, "glyph 必须是字符串或 null"
        fields["glyph"] = (v or None)
    elif not partial:
        fields["glyph"] = None
    if "kind" in data:
        if data["kind"] not in ("service", "task"):
            return None, "kind 必须是 service/task"
        fields["kind"] = data["kind"]
    elif not partial:
        fields["kind"] = "service"
    if "autostart" in data:
        if not isinstance(data["autostart"], bool):
            return None, "autostart 必须是布尔值"
        fields["autostart"] = data["autostart"]
    elif not partial:
        fields["autostart"] = False
    if fields.get("kind") == "task":
        fields["port"] = None  # 批处理任务无端口语义
        fields["autostart"] = False  # 批处理任务无开机自启意义
    return fields, None


def canonicalize_app_launch_spec(requested, *, command, cwd, port, kind,
                                existing=None):
    """Resolve a card's launch definition and keep compatibility fields aligned.

    New cards always resolve to a shell-free LaunchSpec. ``legacy-shell`` is
    accepted only when updating a card which already has that migrated legacy
    mode. Callers pass the card's top-level cwd/port/kind as canonical values;
    the LaunchSpec's display command is derived from the normalized result.
    """
    if kind not in ("service", "task"):
        raise LaunchSpecError("kind 必须是 service/task")
    if kind == "task":
        port = None

    old_spec = existing.get("launchSpec") if isinstance(existing, dict) else None
    legacy_compat = (isinstance(old_spec, dict)
                     and old_spec.get("mode") == "legacy-shell")

    if requested is None:
        if legacy_compat:
            # Preserve old shell semantics only for a card already migrated
            # with that explicit mode. A command-only edit cannot downgrade a
            # structured card into a shell command.
            requested = dict(old_spec)
            requested["legacyCommand"] = command
        else:
            requested = resolve_launch_spec(command, cwd, port, kind)

    if not isinstance(requested, dict):
        raise LaunchSpecError("launchSpec 必须是对象")
    if (requested.get("mode") == "legacy-shell"
            and not legacy_compat):
        raise LaunchSpecError(
            "新应用必须使用结构化 LaunchSpec；请确认可执行程序、批处理或 PowerShell 配置")

    value = dict(requested)
    # Top-level fields are the API's canonical compatibility representation.
    # Do not let an inconsistent nested cwd or probe port change actual runtime
    # behavior behind what the card displays.
    value["cwd"] = cwd
    readiness = value.get("readiness")
    if kind == "task":
        value["readiness"] = default_readiness(None)
    elif isinstance(readiness, dict) and readiness.get(
            "type", "tcp" if port is not None else "none") in ("tcp", "http"):
        readiness = dict(readiness)
        readiness["port"] = port
        value["readiness"] = readiness

    spec = normalize_launch_spec(
        value, command=command, cwd=cwd, port=port)
    if spec.get("mode") == "legacy-shell" and not legacy_compat:
        raise LaunchSpecError(
            "新应用必须使用结构化 LaunchSpec；请确认可执行程序、批处理或 PowerShell 配置")
    return spec, command_from_launch_spec(spec)


def app_launch_field_values(data, fields, existing=None):
    """Choose canonical cwd/port values for a launch API request.

    Explicit top-level fields win. If omitted, an explicit LaunchSpec can
    provide cwd and a TCP/HTTP port; otherwise partial updates retain the
    existing card values.
    """
    old = existing if isinstance(existing, dict) else {}
    requested = data.get("launchSpec") if isinstance(data, dict) else None
    if "cwd" in data:
        cwd = fields.get("cwd")
    elif isinstance(requested, dict) and "cwd" in requested:
        cwd = requested.get("cwd")
    else:
        cwd = fields.get("cwd", old.get("cwd"))

    kind = fields.get("kind", old.get("kind") or "service")
    if kind == "task":
        port = None
    elif "port" in data:
        port = fields.get("port")
    else:
        readiness = (requested.get("readiness")
                     if isinstance(requested, dict) else None)
        if (isinstance(readiness, dict)
                and readiness.get("type") in ("tcp", "http")
                and "port" in readiness):
            port = readiness.get("port")
        else:
            port = fields.get("port", old.get("port"))
    return cwd, port, kind


# ---------------------------------------------------------------- HTTP 处理

def serialized_app_operation(fn):
    """Reject overlapping mutations for one app instead of racing/queueing."""
    @functools.wraps(fn)
    def wrapped(self, app_id, *args, **kwargs):
        lock = self.server.cfg.try_app_operation(app_id)
        if lock is None:
            self.send_err(409, "该应用正在执行其他操作，请稍后重试")
            return None
        try:
            return fn(self, app_id, *args, **kwargs)
        finally:
            lock.release()
    return wrapped


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler_cls, cfg, port, control_token=None):
        if control_token is None:
            token_path = os.path.join(
                os.path.dirname(os.path.abspath(cfg.path)), "control.token")
            control_token = load_control_token(token_path)
        super().__init__(addr, handler_cls)
        self.cfg = cfg
        self.console_port = self.server_address[1]
        self.control_token = control_token
        self._console_action_guard = threading.Lock()
        self._console_action = None
        self._console_helper_pid = None

    def handle_error(self, request, client_address):
        """空闲连接超时 / 客户端中途断开属正常现象，不刷 traceback。"""
        exc_type, exc, _ = sys.exc_info()
        if exc_type and isinstance(exc, (TimeoutError, BrokenPipeError,
                                         ConnectionResetError)):
            return
        super().handle_error(request, client_address)

    def reserve_console_action(self, action):
        with self._console_action_guard:
            if self._console_action is not None:
                return False, self._console_action, self._console_helper_pid
            self._console_action = action
            return True, action, None

    def set_console_helper_pid(self, pid):
        with self._console_action_guard:
            self._console_helper_pid = pid

    def release_console_action(self, action):
        with self._console_action_guard:
            if self._console_action == action:
                self._console_action = None
                self._console_helper_pid = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Console/%s" % APP_VERSION
    # 每连接 socket 超时：慢速/谎报 Content-Length 的客户端无法无限占住
    # 线程（默认 None 会永久阻塞 rfile.read）；空闲 keep-alive 连接也会回收。
    SOCKET_TIMEOUT_SEC = 30.0

    def setup(self):
        super().setup()
        try:
            self.connection.settimeout(self.SOCKET_TIMEOUT_SEC)
        except OSError:
            pass

    # ---------- 基础工具 ----------

    def log_message(self, fmt, *args):
        try:
            if self.path.startswith("/api/state"):
                return  # 2s 轮询不刷日志
        except Exception:
            pass
        sys.stderr.write("%s - %s\n" % (self.client_address[0], fmt % args))

    def _parsed_request_host(self):
        """Return (hostname, port) only for the exact local console origin."""
        raw = (self.headers.get("Host") or "").strip()
        if not raw or any(ch in raw for ch in "\r\n,@/"):
            return None
        try:
            parsed = urllib.parse.urlsplit("http://" + raw)
            hostname = (parsed.hostname or "").lower()
            port = parsed.port
        except (ValueError, UnicodeError):
            return None
        if hostname not in ("127.0.0.1", "localhost", "::1"):
            return None
        if port != self.server.console_port:
            return None
        return hostname, port

    def _request_host_allowed(self):
        if self._parsed_request_host() is None:
            return False
        try:
            return self.client_address[0] in ("127.0.0.1", "::1")
        except (AttributeError, IndexError):
            return False

    def _same_origin(self, origin, host):
        try:
            parsed = urllib.parse.urlsplit(origin)
            port = parsed.port or (80 if parsed.scheme == "http" else 443)
            return (parsed.scheme == "http"
                    and (parsed.hostname or "").lower() == host[0]
                    and port == host[1]
                    and not parsed.username and not parsed.password
                    and not parsed.path and not parsed.query and not parsed.fragment)
        except (ValueError, UnicodeError):
            return False

    def _has_control_token(self):
        values = self.headers.get_all("X-Console-Token") or []
        return (len(values) == 1
                and secrets.compare_digest(values[0], self.server.control_token))

    def _deny_request(self, status, message):
        # Do not consume attacker-controlled bodies. Closing after the bounded
        # JSON error prevents keep-alive request smuggling via leftover bytes.
        self.close_connection = True
        self.send_err(status, message)
        return False

    def _handle_request_error(self, method, exc):
        """请求处理异常统一入口：细节只进日志，响应不回内部信息。"""
        LOG.exception("%s %s 处理失败", method, self.path)
        try:
            self.send_err(500, "服务器错误")
        except Exception:
            pass

    def authorize_request(self, mutating=False, content_kind=None):
        """Enforce the loopback browser trust boundary.

        所有写请求都必须携带只保存在当前用户私有文件中的能力令牌。浏览器
        从启动 URL fragment 读取令牌，fragment 不会发给服务器或 Referer；
        Origin/Sec-Fetch-Site 校验继续阻断跨站请求。
        """
        host = self._parsed_request_host()
        if host is None or not self._request_host_allowed():
            return self._deny_request(421, "请求 Host 不是当前本地控制台")
        if not mutating:
            return True

        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        origin = (self.headers.get("Origin") or "").strip()
        if site and site not in ("same-origin", "none"):
            return self._deny_request(403, "拒绝跨站控制请求")
        if origin and not self._same_origin(origin, host):
            return self._deny_request(403, "请求 Origin 不是当前控制台")
        if not self._has_control_token():
            return self._deny_request(403, "控制凭据缺失或已失效，请通过启动器重新打开总控台")

        if self.headers.get("Transfer-Encoding"):
            return self._deny_request(400, "不支持 Transfer-Encoding 请求体")

        media_type = (self.headers.get("Content-Type") or "").split(";", 1)[0]
        media_type = media_type.strip().lower()
        if content_kind == "json" and media_type != "application/json":
            return self._deny_request(415, "接口仅接受 application/json")
        if content_kind == "image" and media_type not in (
                "image/png", "image/jpeg", "image/webp",
                "application/octet-stream"):
            return self._deny_request(415, "图标接口仅接受 PNG/JPEG/WebP 原始数据")
        if content_kind:
            lengths = self.headers.get_all("Content-Length") or []
            if len(lengths) != 1:
                return self._deny_request(400, "请求必须包含唯一的 Content-Length")
            try:
                length = int(lengths[0])
            except ValueError:
                return self._deny_request(400, "非法的 Content-Length")
            limit = MAX_ICON_BYTES if content_kind == "image" else MAX_JSON_BYTES
            if length < 0 or length > limit:
                return self._deny_request(413, "请求体过大")
        return True

    def _send(self, body, status=200, ctype="text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; connect-src 'self'; img-src 'self' data: blob:; "
            "font-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'")
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_json(self, obj, status=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   status, "application/json; charset=utf-8")

    def send_err(self, status, msg):
        self.send_json({"ok": False, "error": msg}, status)

    def discard_body(self):
        """读掉并丢弃请求体。keep-alive 连接复用前必须清空，
        否则残留字节会污染同一连接上的下一个请求（method 解析错乱 → 501）。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 0:
            try:
                self.rfile.read(length)
            except OSError:
                pass

    def read_json_body(self):
        """→ (data|None, error|None)。非法 JSON / 非对象 / 超限都返回 error。"""
        media_type = (self.headers.get("Content-Type") or "").split(";", 1)[0]
        if media_type.strip().lower() != "application/json":
            return None, "Content-Type 必须是 application/json"
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, "非法的 Content-Length"
        if length < 0 or length > MAX_JSON_BYTES:
            return None, "请求体过大"
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return None, "请求体不是合法 JSON"
        if not isinstance(data, dict):
            return None, "请求体必须是 JSON 对象"
        return data, None

    def _get_app_or_404(self, app_id):
        cfg = self.server.cfg.snapshot()
        app = find_app(cfg, app_id)
        if app is None:
            self.send_err(404, "应用不存在")
            return None, None
        return cfg, app

    # ---------- GET ----------

    def do_GET(self):
        try:
            if not self.authorize_request():
                return
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/favicon.ico":
                self.serve_static("/assets/favicon.ico")
                return
            if path == "/api/health":
                self.send_json(build_health(self.server.cfg))
                return
            if path == "/api/state":
                self.send_json(get_state_snapshot(self.server.cfg,
                                                  self.server.console_port))
                return
            if path == "/api/console/log":
                self.handle_console_log(parsed.query)
                return
            m = APP_ROUTE_RE.match(path)
            if m and m.group(2) == "logs":
                self.handle_logs(m.group(1), parsed.query)
                return
            if path.startswith("/api/"):
                self.send_err(404, "接口不存在")
                return
            if path.startswith("/icons/"):
                self.serve_icon(path)
                return
            self.serve_static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._handle_request_error("GET", e)

    def serve_static(self, path):
        rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        # realpath 解析后必须仍在 STATIC_DIR 内，防路径穿越与符号链接逃逸。
        try:
            inside = os.path.commonpath(
                [os.path.realpath(STATIC_DIR), os.path.realpath(full)]
            ) == os.path.realpath(STATIC_DIR)
        except (ValueError, OSError):
            inside = False
        if not inside or not os.path.isfile(full):
            if rel == "index.html":
                self._send(PLACEHOLDER_HTML.encode("utf-8"), 200,
                           "text/html; charset=utf-8")
            else:
                self._send(b"404 Not Found", 404)
            return
        ctype = STATIC_TYPES.get(os.path.splitext(full)[1].lower(),
                                 "application/octet-stream")
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            self._send(b"404 Not Found", 404)
            return
        self._send(data, 200, ctype)

    def serve_icon(self, path):
        name = os.path.basename(urllib.parse.unquote(path[len("/icons/"):]))
        ext = os.path.splitext(name)[1].lower()
        if ext not in ICON_EXTS:
            self._send(b"404 Not Found", 404)
            return
        full = os.path.join(ICONS_DIR, name)
        if not os.path.isfile(full):
            self._send(b"404 Not Found", 404)
            return
        ctype = STATIC_TYPES.get(ext, "application/octet-stream")
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            self._send(b"404 Not Found", 404)
            return
        self._send(data, 200, ctype)

    def handle_logs(self, app_id, query):
        _, app = self._get_app_or_404(app_id)
        if app is None:
            return
        tail = self._parse_log_tail(query)
        self.send_json({"text": read_log_tail(app_id, tail)})

    def handle_console_log(self, query):
        """总控台自身日志（data/logs/console.log），与维护线程共用轮转。"""
        tail = self._parse_log_tail(query)
        self.send_json({"text": read_log_tail("console", tail)})

    @staticmethod
    def _parse_log_tail(query, default=300):
        try:
            tail = int(urllib.parse.parse_qs(query).get("tail", [default])[0])
        except (ValueError, IndexError):
            tail = default
        return max(1, min(tail, 5000))

    # ---------- POST ----------

    def do_POST(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            route_match = APP_ROUTE_RE.match(path)
            content_kind = ("image" if route_match and
                            route_match.group(2) == "icon" else "json")
            if not self.authorize_request(mutating=True,
                                          content_kind=content_kind):
                return
            if path == "/api/kill":
                self.handle_kill()
                return
            if path == "/api/services/flag":
                self.handle_flag()
                return
            if path == "/api/watch":
                self.handle_watch()
                return
            if path == "/api/ui/theme":
                self.handle_ui_theme()
                return
            if path == "/api/settings":
                self.handle_settings()
                return
            if path == "/api/pick":
                self.handle_pick()
                return
            if path == "/api/project/detect":
                self.handle_project_detect()
                return
            if path == "/api/launch/resolve":
                self.handle_launch_resolve()
                return
            if path == "/api/console/restart":
                self.discard_body()
                self.handle_console_restart()
                return
            if path == "/api/console/stop":
                self.discard_body()
                self.handle_console_stop()
                return
            if path == "/api/apps":
                self.handle_app_create()
                return
            if path == "/api/apps/reorder":
                self.handle_apps_reorder()
                return
            m = APP_ROUTE_RE.match(path)
            if m:
                app_id, action = m.group(1), m.group(2)
                if action == "start":
                    self.discard_body()
                    self.handle_app_start(app_id)
                    return
                if action == "stop":
                    self.discard_body()
                    self.handle_app_stop(app_id)
                    return
                if action == "restart":
                    self.discard_body()
                    self.handle_app_restart(app_id)
                    return
                if action == "diagnose":
                    self.discard_body()
                    self.handle_app_diagnose(app_id)
                    return
                if action == "attach":
                    self.handle_app_attach(app_id)
                    return
                if action == "validate-launch":
                    self.handle_app_validate_launch(app_id)
                    return
                if action == "icon":
                    self.handle_icon_upload(app_id)
                    return
                if action == "favicon":
                    self.discard_body()
                    self.handle_fetch_favicon(app_id)
                    return
            self.send_err(404, "接口不存在")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._handle_request_error("POST", e)

    def handle_pick(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        what = data.get("what")
        if what not in ("dir", "script"):
            self.send_err(400, "what 必须是 dir/script")
            return
        path, canceled = pick_path(what)
        if canceled:  # 用户取消不是错误，前端静默
            self.send_json({"ok": True, "canceled": True})
        elif not path:
            self.send_json({"ok": False, "error": "无法打开系统选择框"})
        else:
            result = {"ok": True, "path": path}
            if what == "script":
                spec, reason = _launch_spec_for_script(
                    path, data.get("cwd"), data.get("port"))
                result["command"] = (command_from_launch_spec(spec) if spec
                                     else command_for_script(path, data.get("cwd")))
                result["available"] = spec is not None
                if spec is not None:
                    result["launchSpec"] = spec
                else:
                    result["unavailableReason"] = reason
            self.send_json(result)

    def handle_project_detect(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        result, err = detect_project(data.get("cwd"))
        if err:
            self.send_err(400, err)
            return
        self.send_json(result)

    def handle_launch_resolve(self):
        """Resolve a manually entered command without running it."""
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        port, err = validate_port(data.get("port"))
        if err:
            self.send_err(400, err)
            return
        kind = data.get("kind", "service")
        try:
            spec = resolve_launch_spec(
                data.get("command"), data.get("cwd"), port, kind)
        except LaunchSpecError as exc:
            self.send_err(422, str(exc))
            return
        command = command_from_launch_spec(spec)
        app = {
            "command": command, "cwd": spec.get("cwd"), "port": port,
            "kind": kind, "launchSpec": spec,
        }
        health = inspect_app_health(app)
        self.send_json({"ok": True, "launchSpec": spec,
                        "command": command, "health": health})

    def handle_app_validate_launch(self, app_id):
        """Validate a candidate launch definition using static checks only."""
        _, existing = self._get_app_or_404(app_id)
        if existing is None:
            return
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        fields, err = validate_app_fields(data, partial=True)
        if err:
            self.send_err(400, err)
            return
        kind = fields.get("kind", existing.get("kind") or "service")
        cwd, port, kind = app_launch_field_values(
            data, fields, existing)
        command = fields.get("command", existing.get("command", ""))
        try:
            requested = (data.get("launchSpec")
                         if "launchSpec" in data else None)
            spec, canonical_command = canonicalize_app_launch_spec(
                requested, command=command, cwd=cwd, port=port, kind=kind,
                existing=existing)
        except (LaunchSpecError, TypeError) as exc:
            self.send_err(422, str(exc))
            return

        health_app = dict(existing)
        health_app.update(fields)
        health_app.update({"kind": kind, "port": port, "cwd": cwd,
                           "command": canonical_command,
                           "launchSpec": spec})
        health = inspect_app_health(health_app)
        self.send_json({"ok": True, "launchSpec": spec,
                        "command": canonical_command,
                        "launchConfigured": is_launch_configured(spec),
                        "health": health})

    def handle_app_diagnose(self, app_id):
        cfg = self.server.cfg.snapshot()
        app = find_app(cfg, app_id)
        if not app:
            self.send_err(404, "应用不存在")
            return
        self.send_json(diagnose_app(cfg, app))

    def handle_settings(self):
        """持久设置:目前支持 openBrowser(启动时是否自动打开浏览器)。"""
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        if "openBrowser" not in data:
            self.send_err(400, "缺少 openBrowser")
            return
        value = data.get("openBrowser")
        if not isinstance(value, bool):
            self.send_err(400, "openBrowser 必须是布尔值")
            return
        self.server.cfg.update(
            lambda d: d.__setitem__("openBrowser", value))
        self.send_json({"ok": True, "openBrowser": value})

    def handle_ui_theme(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        theme_id = str(data.get("theme") or "")
        known = {t["id"] for t in list_themes()}
        if theme_id not in known:
            self.send_err(400, "未知主题: %s" % theme_id)
            return
        self.server.cfg.update(lambda d: d.__setitem__("uiTheme", theme_id))
        self.send_json({"ok": True, "theme": theme_id})

    def handle_console_restart(self):
        reserved, current, helper_pid = self.server.reserve_console_action("restart")
        if not reserved:
            if current == "restart":
                self.send_json({"ok": True, "pid": SELF_PID,
                                "helperPid": helper_pid,
                                "port": self.server.console_port,
                                "alreadyScheduled": True})
            else:
                self.send_err(409, "总控台正在停止，无法重复重启")
            return
        try:
            helper_pid = schedule_console_restart(
                self.server, self.server.console_port)
        except OSError as e:
            self.server.release_console_action("restart")
            self.send_err(500, "无法启动重启程序: %s" % e)
            return
        self.server.set_console_helper_pid(helper_pid)
        invalidate_state_cache()
        self.send_json({"ok": True, "pid": SELF_PID,
                        "helperPid": helper_pid,
                        "port": self.server.console_port})

    def handle_console_stop(self):
        reserved, current, _ = self.server.reserve_console_action("stop")
        if not reserved:
            if current == "stop":
                self.send_json({"ok": True, "pid": SELF_PID,
                                "port": self.server.console_port,
                                "alreadyScheduled": True})
            else:
                self.send_err(409, "总控台正在重启，无法同时停止")
            return
        schedule_console_stop(self.server)
        invalidate_state_cache()
        self.send_json({"ok": True, "pid": SELF_PID,
                        "port": self.server.console_port})

    def handle_kill(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        pid = data.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            self.send_err(400, "缺少字段 pid（正整数）")
            return
        ok, err = kill_process(pid, bool(data.get("force")))
        if ok:
            invalidate_state_cache()
        self.send_json({"ok": True} if ok else {"ok": False, "error": err})

    def handle_flag(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        key, flag, value = data.get("key"), data.get("flag"), data.get("value")
        if not isinstance(key, str) or not key:
            self.send_err(400, "缺少字段 key")
            return
        if flag not in ("hidden", "pinned", "promoted"):
            self.send_err(400, "flag 必须是 hidden/pinned/promoted")
            return
        if not isinstance(value, bool):
            self.send_err(400, "value 必须是布尔值")
            return

        def op(c):
            lst = c.setdefault(flag, [])
            if value and key not in lst:
                lst.append(key)
            elif not value and key in lst:
                lst.remove(key)

        self.server.cfg.update(op)
        self.send_json({"ok": True})

    def handle_watch(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        keyword, action = data.get("keyword"), data.get("action")
        if not isinstance(keyword, str) or not keyword.strip():
            self.send_err(400, "缺少字段 keyword")
            return
        if action not in ("add", "remove"):
            self.send_err(400, "action 必须是 add/remove")
            return
        keyword = keyword.strip()

        def op(c):
            kws = c.setdefault("watchedKeywords", [])
            if action == "add" and keyword not in kws:
                kws.append(keyword)
            elif action == "remove":
                c["watchedKeywords"] = [k for k in kws if k != keyword]
            return list(c["watchedKeywords"])

        keywords = self.server.cfg.update(op)
        self.send_json({"ok": True, "keywords": keywords})

    def handle_app_create(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        attach_pid = data.get("attachPid")
        if attach_pid is not None and (
                not isinstance(attach_pid, int)
                or isinstance(attach_pid, bool)
                or attach_pid <= 0):
            self.send_err(400, "attachPid 必须是正整数")
            return
        fields, err = validate_app_fields(data, partial=False)
        if err:
            self.send_err(400, err)
            return

        snapshot = self.server.cfg.snapshot()
        new_id = secrets.token_hex(4)
        while find_app(snapshot, new_id):
            new_id = secrets.token_hex(4)
        app = {"id": new_id, "name": fields["name"],
               "command": fields["command"], "cwd": fields["cwd"],
               "port": fields["port"], "emoji": fields["emoji"],
               "glyph": fields["glyph"], "kind": fields["kind"],
               "autostart": fields.get("autostart", False),
               "icon": None, "favicon": None, "lastPid": None,
               "lastPgid": None, "runToken": None,
               "attached": False, "controlMode": "managed",
               "launchSpec": None, "launchConfigured": False,
               "observation": None, "runInstance": None,
               "readinessState": "unknown", "lastExit": None,
               "lastCreateTime": None, "createdAt": int(time.time())}
        try:
            if attach_pid is None:
                launch_cwd, launch_port, launch_kind = app_launch_field_values(
                    data, fields)
                app["launchSpec"], app["command"] = (
                    canonicalize_app_launch_spec(
                        data.get("launchSpec"), command=fields["command"],
                        cwd=launch_cwd, port=launch_port,
                        kind=launch_kind))
                app["cwd"] = app["launchSpec"].get("cwd")
                app["port"] = launch_port
                app["controlMode"] = "managed"
                app["launchConfigured"] = is_launch_configured(
                    app["launchSpec"])
        except LaunchSpecError as exc:
            self.send_err(400, str(exc))
            return
        cwd_updated = False
        if attach_pid is not None:
            ok, error, identity = inspect_attach_process(
                self.server.cfg, app, attach_pid)
            if not ok:
                self.send_json(
                    {"ok": False, "error": error},
                    identity.get("status", 409),
                )
                return
            try:
                cwd_updated = bool(identity.get("cwd") and
                                   os.path.normcase(os.path.realpath(identity.get("cwd"))) !=
                                   os.path.normcase(os.path.realpath(app.get("cwd") or "")))
            except (OSError, TypeError, ValueError):
                cwd_updated = bool(identity.get("cwd") and identity.get("cwd") != app.get("cwd"))
            attached_cwd = identity.get("cwd") or app.get("cwd")
            app["cwd"] = attached_cwd
            app["attached"] = True
            requested_launch = data.get("launchSpec")
            if requested_launch is not None:
                if not isinstance(requested_launch, dict):
                    self.send_err(400, "launchSpec 必须是对象")
                    return
                try:
                    spec, canonical_command = canonicalize_app_launch_spec(
                        requested_launch, command=fields["command"],
                        cwd=attached_cwd, port=fields["port"],
                        kind=fields["kind"])
                except (LaunchSpecError, TypeError) as exc:
                    self.send_err(400, str(exc))
                    return
                app["launchSpec"] = spec
                app["command"] = canonical_command
                app["controlMode"] = "managed"
                app["launchConfigured"] = is_launch_configured(spec)
            else:
                # Legacy service-monitor claims remain observation-only unless
                # this same atomic create confirms a structured launch spec.
                app["controlMode"] = "monitor"
                app["launchSpec"] = None
                app["launchConfigured"] = False
            app["observation"] = observation_from_identity(
                attach_pid, app.get("port"), identity)
            app["lastPid"] = attach_pid
            app["lastCreateTime"] = identity.get("ctime")

        attach_conflict = [False]
        attach_identity_stale = [False]

        def op(c):
            if find_app(c, new_id):
                return None
            if (attach_pid is not None
                    and not attach_identity_still_matches(
                        attach_pid, fields["port"], identity)):
                attach_identity_stale[0] = True
                return None
            # 与 attach_app_process 同规则：写锁内重验 pid 未被其他卡片认领。
            if attach_pid is not None and any(
                    (_observation_matches_identity(
                        other.get("observation"), attach_pid, identity,
                        other.get("lastCreateTime"))
                     or (other.get("observation") is None
                         and other.get("lastPid") == attach_pid
                         and (other.get("lastCreateTime") is None
                              or identity.get("ctime") is None
                              or other.get("lastCreateTime") == identity.get("ctime"))))
                    for other in c.get("apps") or []):
                attach_conflict[0] = True
                return None
            c["apps"].append(app)
            return dict(app)

        created = self.server.cfg.update(op)
        if created is None:
            if attach_identity_stale[0]:
                self.send_json({
                    "ok": False,
                    "error": "认领进程在保存前已退出或身份发生变化，请刷新后重试",
                }, 409)
            elif attach_conflict[0]:
                self.send_json(
                    {"ok": False, "error": "该进程已由其他卡片管理"}, 409)
            else:
                self.send_err(409, "应用标识发生冲突，请重试")
            return
        if attach_pid is not None:
            created.update({
                "attached": True,
                "controlMode": app["controlMode"],
                "processState": "alive",
                "identityStrength": "observation",
                "launchConfigured": app["launchConfigured"],
                "running": True,
                "pid": attach_pid,
                "cwdUpdated": cwd_updated,
            })
        self.send_json(created)

    @serialized_app_operation
    def handle_fetch_favicon(self, app_id):
        """抓取应用有效端口对应站点的 favicon，存为 data/icons/fav-{id}.{ext}。
        优先级低于用户自定义 icon/glyph，仅作兜底。"""
        _, app = self._get_app_or_404(app_id)
        if app is None:
            return
        port = None
        listeners = scan_listeners()
        if app.get("controlMode") == "monitor":
            observed_pid = observed_process_pid(app, listeners=listeners)
            live = {observed_pid} if observed_pid is not None else set()
        else:
            live = set(managed_pids(app))
        configured_port = app.get("port")
        if configured_port and any(pid in live and p == configured_port
                                   for pid, p in listeners):
            port = configured_port
        if not port:
            owned_ports = sorted({p for pid, p in listeners if pid in live})
            port = owned_ports[0] if owned_ports else None
        if not port:
            self.send_json({"ok": False, "error": "应用未运行或无可用端口"})
            return
        host = listener_open_host(listeners, port, live)
        data, ext = fetch_favicon(port, host)
        if not data:
            self.send_json({"ok": False, "error": "未找到站点图标"})
            return
        fname = "fav-%s.%s" % (app_id, ext)
        try:
            _ensure_private_dir(ICONS_DIR)
            write_private_bytes(os.path.join(ICONS_DIR, fname), data)
        except OSError as e:
            self.send_json({"ok": False, "error": "图标保存失败: %s" % e})
            return
        url = "/icons/" + fname

        def op(c):
            target = find_app(c, app_id)
            if target:
                target["favicon"] = url

        self.server.cfg.update(op)
        self.send_json({"ok": True, "favicon": url})

    def handle_apps_reorder(self):
        """按收到的 id 顺序重排 apps（Python sort 稳定：未涉及的 id 相对顺序不变，
        服务/任务两区可独立排序互不干扰）。"""
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        ids = data.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            self.send_err(400, "ids 必须是字符串数组")
            return
        order = {i: n for n, i in enumerate(ids)}

        def op(c):
            c["apps"].sort(key=lambda a: order.get(a.get("id"), len(order)))

        self.server.cfg.update(op)
        self.send_json({"ok": True})

    def handle_app_start(self, app_id):
        result = start_app_transaction(self.server.cfg, app_id)
        status = result.pop("status", 200)
        self.send_json(result, status)

    @serialized_app_operation
    def handle_app_stop(self, app_id):
        _, app = self._get_app_or_404(app_id)
        if app is None:
            return
        if app.get("controlMode") == "monitor":
            self.send_json({
                "ok": False,
                "error": "监控卡片不能停止外部进程，请先确认启动配置",
                "launchSpecRequired": True,
            }, 409)
            return
        identity_state = lifecycle_identity_state(app)
        if identity_state == "unknown":
            self.send_json({
                "ok": False,
                "error": "无法验证当前 Job Object 状态；未执行停止，请稍后重试",
                "identityUnavailable": True,
            }, 409)
            return
        if identity_state != "alive":
            self.send_json({"ok": False, "error": "应用未在运行"})
            return
        ok, error = stop_app_and_clear(self.server.cfg, app)
        if not ok:
            self.send_json({"ok": False, "error": error}, 409)
            return
        self.send_json({"ok": True})

    @serialized_app_operation
    def handle_app_attach(self, app_id):
        _, app = self._get_app_or_404(app_id)
        if app is None:
            return
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        pid = data.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            self.send_err(400, "pid 必须是正整数")
            return
        ok, error, info = attach_app_process(self.server.cfg, app_id, app, pid)
        if not ok:
            self.send_json({"ok": False, "error": error}, info.get("status", 409))
            return
        resp = {"ok": True, "pid": pid}
        resp.update(info)
        self.send_json(resp)

    @serialized_app_operation
    def handle_app_restart(self, app_id):
        _, app = self._get_app_or_404(app_id)
        if app is None:
            return
        if app.get("controlMode") == "monitor":
            self.send_json({
                "ok": False,
                "error": "监控卡片不能重启外部进程，请先确认启动配置",
                "launchSpecRequired": True,
            }, 409)
            return
        identity_state = lifecycle_identity_state(app)
        if identity_state == "unknown":
            self.send_json({
                "ok": False,
                "error": "无法验证当前 Job Object 状态；未执行重启，请稍后重试",
                "identityUnavailable": True,
            }, 409)
            return
        if identity_state != "alive":
            self.send_err(409, "应用未在运行")
            return
        # 必须在停止旧服务前预检；配置已失效时保留仍在工作的旧进程。
        health = inspect_app_health(app)
        if health["blocking"]:
            issue = health["issues"][0]
            self.send_json({
                "ok": False,
                "error": "%s：%s。旧服务仍在运行" %
                         (issue["title"], issue["detail"]),
                "health": health,
            }, 422)
            return

        stopped, error = stop_app_and_clear(self.server.cfg, app)
        if not stopped:
            self.send_err(409, error or "旧进程停止失败，已取消重启")
            return

        result = start_app_transaction(self.server.cfg, app_id)
        status = result.pop("status", 200)
        self.send_json(result, status)

    @serialized_app_operation
    def handle_icon_upload(self, app_id):
        _, app = self._get_app_or_404(app_id)
        if app is None:
            return
        try:
            length = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            length = -1
        if length < 0:
            self.send_err(400, "缺少 Content-Length")
            return
        if length > MAX_ICON_BYTES:
            self.send_err(400, "图标大小不能超过 5MB")
            return
        raw = self.rfile.read(length)
        kind = sniff_image(raw)
        if kind is None:
            self.send_err(400, "仅支持 PNG / JPEG / WebP 图片")
            return
        _ensure_private_dir(ICONS_DIR)
        for ext in ICON_EXTS:
            old = os.path.join(ICONS_DIR, app_id + ext)
            if ext != "." + kind and os.path.isfile(old):
                try:
                    os.remove(old)
                except OSError:
                    pass
        fname = "%s.%s" % (app_id, kind)
        try:
            write_private_bytes(os.path.join(ICONS_DIR, fname), raw)
        except OSError as e:
            self.send_err(500, "图标保存失败: %s" % e)
            return
        icon_url = "/icons/" + fname

        def op(c):
            target = find_app(c, app_id)
            if target:
                target["icon"] = icon_url

        self.server.cfg.update(op)
        self.send_json({"ok": True, "icon": icon_url})

    # ---------- PUT ----------

    def do_PUT(self):
        operation_lock = None
        try:
            if not self.authorize_request(mutating=True,
                                          content_kind="json"):
                return
            path = urllib.parse.urlparse(self.path).path
            m = APP_ROUTE_RE.match(path)
            if not (m and m.group(2) is None):
                self.send_err(404, "接口不存在")
                return
            operation_lock = self.server.cfg.try_app_operation(m.group(1))
            if operation_lock is None:
                self.send_err(409, "该应用正在执行其他操作，请稍后重试")
                return
            data, err = self.read_json_body()
            if err:
                self.send_err(400, err)
                return
            stop_before_update = data.get("stopBeforeUpdate", False)
            if not isinstance(stop_before_update, bool):
                self.send_err(400, "stopBeforeUpdate 必须是布尔值")
                return
            _, app = self._get_app_or_404(m.group(1))
            if app is None:
                return
            fields, err = validate_app_fields(data, partial=True)
            if err:
                self.send_err(400, err)
                return
            launch_fields = ("command", "cwd", "port", "kind")
            has_launch_field_change = any(
                key in fields and fields[key] != app.get(key)
                for key in launch_fields)
            if (app.get("controlMode") == "monitor"
                    and has_launch_field_change and "launchSpec" not in data):
                self.send_json({
                    "ok": False,
                    "error": "监控卡片修改启动字段时必须同时确认 launchSpec",
                    "launchSpecRequired": True,
                }, 409)
                return
            if (app.get("controlMode") == "monitor"
                    and "launchSpec" in data
                    and data.get("launchSpec") is None):
                self.send_json({
                    "ok": False,
                    "error": "监控卡片必须保存已确认的结构化启动配置",
                    "launchSpecRequired": True,
                }, 422)
                return

            should_canonicalize_launch = (
                "launchSpec" in data or has_launch_field_change)
            if should_canonicalize_launch:
                try:
                    selected_cwd, selected_port, selected_kind = (
                        app_launch_field_values(data, fields, app))
                    requested = (data.get("launchSpec")
                                 if "launchSpec" in data else None)
                    spec, canonical_command = canonicalize_app_launch_spec(
                        requested,
                        command=fields.get("command", app.get("command", "")),
                        cwd=selected_cwd, port=selected_port,
                        kind=selected_kind, existing=app)
                except (LaunchSpecError, TypeError) as exc:
                    self.send_err(400, str(exc))
                    return
                fields["launchSpec"] = spec
                fields["command"] = canonical_command
                fields["cwd"] = selected_cwd
                fields["port"] = selected_port
                fields["controlMode"] = "managed"
                promoted_observation = (dict(app.get("observation"))
                                        if isinstance(app.get("observation"), dict)
                                        else None)
                # ``attached`` records the external process identity claimed
                # from the service monitor. It remains useful after a
                # LaunchSpec is confirmed (the card is then managed for future
                # launches, while the existing listener is still external).
                promoted_attached = bool(
                    app.get("attached") and promoted_observation)
                fields["attached"] = promoted_attached
                fields["observation"] = (promoted_observation
                                          if promoted_attached else None)
                if (not promoted_attached and
                        (app.get("controlMode") == "monitor"
                         or spec != app.get("launchSpec"))):
                    fields["lastPid"] = None
                    fields["lastPgid"] = None
                    fields["runToken"] = None
                    fields["lastCreateTime"] = None
                    fields["runInstance"] = None
                fields["launchConfigured"] = is_launch_configured(spec)
                fields["readinessState"] = "unknown"
            if not fields:
                self.send_err(400, "没有可更新的字段")
                return
            lifecycle_fields = {"command", "cwd", "port", "kind",
                                "launchSpec", "controlMode"}
            lifecycle_changed = any(
                key in fields and fields[key] != app.get(key)
                for key in lifecycle_fields)
            identity_state = "absent"
            if lifecycle_changed:
                identity_state = lifecycle_identity_state(app)
                if identity_state == "unknown":
                    self.send_json({
                        "ok": False,
                        "error": "无法验证当前 Job Object 状态；为保留运行身份，已拒绝修改",
                        "identityUnavailable": True,
                    }, 409)
                    return
            stopped_for_update = False
            if lifecycle_changed and identity_state == "alive":
                if not stop_before_update:
                    stop_label = ("中止任务"
                                  if (app.get("kind") or "service") == "task"
                                  else "停止服务")
                    self.send_json({
                        "ok": False,
                        "error": "应用正在运行，请先在当前编辑面板%s；填写内容会保留" %
                                 stop_label,
                        "requiresStop": True,
                    }, 409)
                    return
                ok, stop_error, stopped_for_update = stop_app_for_update(
                    self.server.cfg, app)
                if not ok:
                    self.send_err(409, stop_error)
                    return

            def op(c):
                target = find_app(c, m.group(1))
                target.update(fields)
                if fields.get("controlMode") == "managed" and not fields.get("attached"):
                    target["observation"] = None
                return dict(target)

            updated = self.server.cfg.update(op)
            if stopped_for_update:
                updated = dict(updated)
                updated["stoppedForUpdate"] = True
            self.send_json(updated)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._handle_request_error("PUT", e)
        finally:
            if operation_lock is not None:
                operation_lock.release()

    # ---------- DELETE ----------

    def do_DELETE(self):
        try:
            if not self.authorize_request(mutating=True):
                return
            path = urllib.parse.urlparse(self.path).path
            m = APP_ROUTE_RE.match(path)
            if not m:
                self.send_err(404, "接口不存在")
                return
            app_id, action = m.group(1), m.group(2)
            if action is None:
                self.handle_app_delete(app_id)
                return
            if action == "icon":
                self.handle_icon_delete(app_id)
                return
            self.send_err(404, "接口不存在")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._handle_request_error("DELETE", e)

    def do_OPTIONS(self):
        # No CORS endpoint exists. An explicit denial is clearer than the
        # BaseHTTPRequestHandler HTML 501 response and never grants ACAO.
        self._deny_request(403, "控制台不接受跨域预检请求")

    @serialized_app_operation
    def handle_app_delete(self, app_id):
        _, app = self._get_app_or_404(app_id)
        if app is None:
            return
        identity_state = lifecycle_identity_state(app)
        if identity_state == "unknown":
            self.send_json({
                "ok": False,
                "error": "删除已取消：无法验证当前 Job Object 状态，运行身份已保留",
                "identityUnavailable": True,
            }, 409)
            return
        if identity_state == "alive":
            stopped, error = stop_app_and_clear(self.server.cfg, app)
            if not stopped:
                self.send_err(409, "删除已取消：%s" %
                              (error or "应用未能正常退出"))
                return

        def op(c):
            before = len(c["apps"])
            c["apps"] = [a for a in c["apps"] if a.get("id") != app_id]
            return len(c["apps"]) != before

        if not self.server.cfg.update(op):
            self.send_err(404, "应用不存在")
            return
        self.server.cfg.forget_app_lock(app_id)

        for ext in ICON_EXTS:
            for fname in (app_id + ext, "fav-" + app_id + ext):
                try:
                    os.remove(os.path.join(ICONS_DIR, fname))
                except OSError:
                    pass
        log_path = os.path.join(LOGS_DIR, "%s.log" % app_id)
        for candidate in [log_path] + ["%s.%d" % (log_path, i)
                                       for i in range(1, LOG_BACKUPS + 1)]:
            try:
                os.remove(candidate)
            except OSError:
                pass

        self.send_json({"ok": True})

    @serialized_app_operation
    def handle_icon_delete(self, app_id):
        _, app = self._get_app_or_404(app_id)
        if app is None:
            return
        for ext in ICON_EXTS:
            try:
                os.remove(os.path.join(ICONS_DIR, app_id + ext))
            except OSError:
                pass

        def op(c):
            target = find_app(c, app_id)
            if target:
                target["icon"] = None

        self.server.cfg.update(op)
        self.send_json({"ok": True})


# ---------------------------------------------------------------- 启动

def open_browser_later(port, token, delay=0.8):
    def _open():
        try:
            time.sleep(delay)
            webbrowser.open(console_url(port, token))
        except Exception:
            pass
    threading.Thread(target=_open, daemon=True).start()


_PYTHON_PROCESS_RE = re.compile(
    r"python(?:w)?(?:\d+(?:\.\d+)*)?\.exe\Z", re.IGNORECASE)


def _is_console_server_process(info):
    """只识别解释器直接执行本项目 server.py 的进程。

    args 是为展示拼接的字符串，参数边界已经丢失，不能用于决定是否杀进程。
    argv 来自 psutil.cmdline()，因此 PowerShell 文本或 ``python -c`` 代码里
    即使出现 server.py 也不会被误判。
    """
    executable = os.path.basename(info.get("comm") or "")
    argv = info.get("argv")
    if not _PYTHON_PROCESS_RE.fullmatch(executable):
        return False
    if not isinstance(argv, list) or len(argv) < 2:
        return False
    script = argv[1]
    return (isinstance(script, str)
            and os.path.basename(script).casefold() == "server.py")


def find_console_instances():
    """查找从同一项目目录启动的总控台，用于双击启动器去重。"""
    snap = ps_snapshot(None, with_uid=True)
    candidates = []
    for pid, info in snap.items():
        if (pid == SELF_PID or not is_current_user(info.get("uid"))
                or not _is_console_server_process(info)
                or "--restart-helper" in (info.get("argv") or [])):
            continue
        candidates.append(pid)
    if not candidates:
        return []
    cwds = lsof_cwds(candidates)
    listener_map = {}
    for pid, port in scan_listeners():
        listener_map.setdefault(pid, []).append(port)
    result = []
    for pid in candidates:
        cwd = cwds.get(pid)
        try:
            same_dir = cwd and os.path.realpath(cwd) == os.path.realpath(BASE_DIR)
        except OSError:
            same_dir = False
        if not same_dir:
            continue
        info = snap.get(pid, {})
        result.append({
            "pid": pid,
            "ports": sorted(listener_map.get(pid, [])),
            "cmd": info.get("args") or "",
            "cwd": cwd,
            "uptimeSec": info.get("etime"),
        })
    return sorted(result, key=lambda item: (item["ports"] or [65536], item["pid"]))


def _disk_configured_app_count(path=None):
    """Count disk cards, or None for an established unreadable config."""
    path = path or CONFIG_PATH
    raw = _load_config_raw(path)
    found_candidate = os.path.lexists(path)
    if raw is None:
        backup_path = path + ".bak"
        raw = _load_config_raw(backup_path)
        found_candidate = found_candidate or os.path.lexists(backup_path)
    if raw is None:
        control_token = os.path.join(
            os.path.dirname(os.path.abspath(path)), "control.token")
        return None if (found_candidate or os.path.lexists(control_token)) else 0
    if not isinstance(raw, dict) or not isinstance(raw.get("apps"), list):
        return 0
    return sum(1 for item in raw["apps"]
               if isinstance(item, dict) and item.get("id"))


def _disk_app_config_signature(path=None):
    """Return the signature of the current disk-backed launchpad cards."""
    path = path or CONFIG_PATH
    raw = _load_config_raw(path)
    if raw is None:
        raw = _load_config_raw(path + ".bak")
    if not isinstance(raw, dict) or not isinstance(raw.get("apps"), list):
        return app_config_signature([])
    return app_config_signature(raw["apps"])


def require_expected_disk_apps(path, expected_count, timeout=3.0):
    """Refuse startup until disk config is readable and meets expectations."""
    try:
        expected_count = max(0, int(expected_count))
    except (TypeError, ValueError):
        expected_count = 0
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        actual = _disk_configured_app_count(path)
        if actual is not None and actual >= expected_count:
            return actual
        if time.monotonic() >= deadline:
            if actual is None:
                raise RuntimeError(
                    "启动前配置校验失败：主配置与备份当前均不可读")
            raise RuntimeError(
                "启动前配置校验失败：启动器检测到 %d 张卡片，当前进程只读到 %d 张" %
                (expected_count, actual))
        time.sleep(0.1)


def _http_json_localhost(port, path, timeout):
    url = "http://127.0.0.1:%d%s" % (int(port), path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def console_instance_status(item, disk_app_count=0):
    """Classify a same-project console process: healthy / stale.

    stale = no listen port, health/state probe failed, or /api/state has
    zero apps while the config file still has cards (in-memory desync).
    Never classifies by port occupancy of unknown processes.
    """
    ports = [p for p in (item.get("ports") or []) if isinstance(p, int)]
    if disk_app_count is None:
        return "stale"
    if not ports:
        return "stale"
    port = min(ports)
    health = _http_json_localhost(port, "/api/health", 2.0)
    if not isinstance(health, dict) or not health.get("ok"):
        return "stale"
    health_config = health.get("config")
    disk_signature = (_disk_app_config_signature()
                      if disk_app_count > 0 else app_config_signature([]))
    if disk_app_count > 0 and isinstance(health_config, dict):
        if (health_config.get("memoryAppCount") == 0
                or health_config.get("diskAppCount") == 0):
            return "stale"
        live_signature = health_config.get("appSignature")
        if (live_signature is not None
                and live_signature != disk_signature):
            return "stale"
        if (isinstance(health_config.get("memoryAppCount"), int)
                and health_config.get("memoryAppCount") > 0
                and live_signature is not None):
            return "healthy"
    state = _http_json_localhost(port, "/api/state", 4.0)
    if not isinstance(state, dict):
        # /api/state performs process scans and can time out transiently. The
        # lightweight health endpoint already proved this is a live console;
        # do not kill it solely because one expensive probe missed its window.
        return "healthy"
    apps = state.get("apps")
    live_count = len(apps) if isinstance(apps, list) else 0
    if live_count == 0 and disk_app_count > 0:
        return "stale"
    if (disk_app_count > 0 and isinstance(apps, list)
            and app_config_signature(apps) != disk_signature):
        return "stale"
    return "healthy"


def _reap_console_pids(pids, force=False):
    """Stop current-user console PIDs; skip self. Soft then optional hard."""
    targets = []
    for pid in pids:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or pid == SELF_PID:
            continue
        if not is_current_user(process_uid(pid)):
            continue
        targets.append(pid)
    if not targets:
        return []
    for pid in targets:
        sysops.kill_process(pid, force=False)
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and any(pid_alive(pid) for pid in targets):
        time.sleep(0.1)
    if force:
        for pid in [p for p in targets if pid_alive(p)]:
            sysops.kill_process(pid, force=True)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and any(pid_alive(pid) for pid in targets):
            time.sleep(0.05)
    return [pid for pid in targets if pid_alive(pid)]


def reap_stale_console_processes():
    """Kill same-project leftover/unhealthy console processes.

    Only current-user Python processes directly executing this project's
    server.py are eligible. Returns True if any stale process was reaped.
    """
    instances = find_console_instances()
    if not instances:
        return False
    disk_apps = _disk_configured_app_count()
    stale_pids = []
    healthy = 0
    for item in instances:
        if console_instance_status(item, disk_apps) == "healthy":
            healthy += 1
        else:
            stale_pids.append(item["pid"])
    if not stale_pids:
        return False
    print("发现残留或异常总控台进程，正在清理: %s" %
          ", ".join(str(pid) for pid in stale_pids), flush=True)
    LOG.warning("reaping stale console pids %s", stale_pids)
    survivors = _reap_console_pids(stale_pids, force=True)
    if survivors:
        LOG.warning("stale console still alive: %s", survivors)
        return False
    return True


def _launcher_dialog(message):
    return sysops.launcher_dialog(message)


def _launcher_alert(message):
    sysops.launcher_alert(message)


def launcher_main():
    """启动器入口：识别已有实例，可打开、重启或取消。"""
    instances = find_console_instances()
    if not instances:
        try:
            main(log_to_file=True)
        except Exception:
            _launcher_alert("总控台启动失败。请检查数据目录权限和 console.log。")
            raise
        return
    labels = []
    for item in instances:
        ports = " / ".join(":%d" % p for p in item["ports"]) or "未监听"
        labels.append("%s  ·  PID %d" % (ports, item["pid"]))
    extra = ("\n\n检测到 %d 个同项目实例，重启时会合并为一个。" % len(instances)
             if len(instances) > 1 else "")
    choice = _launcher_dialog(
        "总控台已在运行：\n" + "\n".join(labels) + extra)
    if choice == "打开控制台":
        ports = [p for item in instances for p in item["ports"]]
        port = min(ports) if ports else PORT_START
        if not open_console_browser(port):
            _launcher_alert("无法读取本地控制凭据，请重启总控台后再打开。")
        return
    if choice != "重新启动":
        return

    preferred_ports = [p for item in instances for p in item["ports"]]
    preferred = min(preferred_ports) if preferred_ports else PORT_START
    targets = [item["pid"] for item in instances]
    for pid in targets:
        if is_current_user(process_uid(pid)):
            sysops.kill_process(pid, force=False)
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and any(pid_alive(pid) for pid in targets):
        time.sleep(0.1)
    survivors = [pid for pid in targets if pid_alive(pid)]
    if survivors:
        _launcher_alert("旧总控台未能正常退出（PID %s），未强制结束。" %
                        "、".join(str(pid) for pid in survivors))
        return
    try:
        main(preferred_port=preferred, log_to_file=True)
    except Exception:
        _launcher_alert("总控台重启失败。请检查数据目录权限和 console.log。")
        raise


def schedule_console_restart(server, preferred_port):
    """启动独立 helper，响应发出后关闭当前 HTTP 服务。"""
    helper = sysops.spawn_detached(
        [sys.executable, os.path.abspath(__file__), "--restart-helper",
         str(SELF_PID), str(int(preferred_port))], BASE_DIR)

    def _shutdown():
        time.sleep(0.25)
        server.shutdown()
    threading.Thread(target=_shutdown, daemon=True).start()
    return helper.pid


def schedule_console_stop(server):
    """响应发送完成后关闭 HTTP 服务，不结束启动台里的独立进程组。"""
    def _shutdown():
        time.sleep(0.25)
        server.shutdown()
    threading.Thread(target=_shutdown, daemon=True).start()


def restart_helper(old_pid, preferred_port):
    """等旧进程退出后，交给独立启动器重新读盘并启动总控台。"""
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline and pid_alive(old_pid):
        time.sleep(0.1)
    if pid_alive(old_pid):
        return 1
    launcher_python = sys.executable
    if os.path.basename(launcher_python).lower() == "pythonw.exe":
        console_python = os.path.join(os.path.dirname(launcher_python),
                                      "python.exe")
        if os.path.isfile(console_python):
            launcher_python = console_python
    args = [launcher_python, os.path.join(BASE_DIR, "launcher_check.py"),
            "launch", str(int(preferred_port))]
    # 让独立启动器重新读取磁盘配置，并由它计算 expected-app-count。
    # 标准输出不继承 pythonw 的无效句柄；候选服务自己会写 console.log。
    subprocess.Popen(
        args, cwd=BASE_DIR, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _autostart_managed_apps(cfg):
    """开机自启：启动标记 autostart 的 service 应用（复用统一启动事务）。

    独立守护线程执行：延迟 AUTOSTART_DELAY_SEC 等状态就绪，随后按配置顺序
    逐个启动 autostart=true 且未运行的 service；每个之间间隔
    AUTOSTART_INTERVAL_SEC。失败记录日志、不阻塞总控台、不自动重试
    （退出监视线程会记录快速失败任务的结果）。task 批处理无自启意义，跳过。
    """
    time.sleep(AUTOSTART_DELAY_SEC)
    for app in (cfg.snapshot().get("apps") or []):
        if (app.get("kind") or "service") != "service":
            continue
        if not app.get("autostart"):
            continue
        result = start_app_transaction(
            cfg, app.get("id"), require_autostart=True)
        if not result.get("ok"):
            # 应用已运行或被用户关闭自启动不属于异常，其他原因保留日志以便诊断。
            if result.get("error") not in (
                    "应用已在运行", "应用已不符合开机自启动条件"):
                LOG.warning("autostart 跳过 %s：%s",
                            app.get("name"), result.get("error", "启动失败"))
            continue
        LOG.info("autostart 已启动 %s", app.get("name"))
        time.sleep(AUTOSTART_INTERVAL_SEC)


def _start_autostart_thread(cfg):
    """启动开机自启守护线程（随主进程退出）。"""
    threading.Thread(target=_autostart_managed_apps, args=(cfg,),
                     name="console-autostart", daemon=True).start()


def _run_console(preferred_port=None, open_browser=True,
                 expected_app_count=0):
    configure_console_encoding()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for private_dir in (DATA_DIR, ICONS_DIR, LOGS_DIR):
        _ensure_private_dir(private_dir)
    start_log_maintenance()
    startup_disk_apps = require_expected_disk_apps(
        CONFIG_PATH, expected_app_count)
    cfg = Config(CONFIG_PATH)
    loaded = len(cfg.snapshot().get("apps") or [])
    disk_apps = _disk_configured_app_count(cfg.path)
    required_apps = max(int(expected_app_count), startup_disk_apps)
    if disk_apps is None or loaded < max(required_apps, disk_apps or 0):
        raise RuntimeError(
            "启动前配置校验失败：内存 %d 张，磁盘 %s 张，至少应加载 %d 张" %
            (loaded, "不可读" if disk_apps is None else str(disk_apps),
             required_apps))
    print("已加载 %d 个应用卡片（磁盘 %d，%s）" %
          (loaded, disk_apps, cfg.path), flush=True)
    if loaded == 0 and disk_apps > 0:
        LOG.warning(
            "launchpad still empty after restore: memory=0 disk=%d path=%s",
            disk_apps, cfg.path)
    control_token = load_control_token(os.path.join(DATA_DIR, "control.token"))
    restore_run_watchers(cfg)

    server, port = None, None
    candidates = list(range(PORT_START, PORT_START + PORT_TRIES))
    if isinstance(preferred_port, int) and preferred_port in candidates:
        candidates.remove(preferred_port)
        candidates.insert(0, preferred_port)
    for p in candidates:
        try:
            server = ConsoleServer((HOST, p), Handler, cfg, p, control_token)
            port = p
            break
        except OSError:
            continue
    if server is None:
        print("错误：端口 %d-%d 均被占用，无法启动。" %
              (PORT_START, PORT_START + PORT_TRIES - 1))
        sys.exit(1)

    print("总控台已启动: http://%s:%d/  (Ctrl+C 停止)" % (HOST, port), flush=True)
    warm_state_cache(cfg, port)
    # CLI(--no-browser)与设置中心(openBrowser)任一关闭则不自动打开浏览器。
    # Config 无 .get()，经 snapshot() 读取（启动时单次调用，开销可忽略）。
    if open_browser and cfg.snapshot().get("openBrowser", True):
        open_browser_later(port, server.control_token)
    # 开机自启：延迟拉起标记 autostart 的 service（守护线程，失败不阻塞）。
    _start_autostart_thread(cfg)
    tray_icon = None
    if _tray_mod is not None:
        url = console_url(port, server.control_token)
        tray_icon = _tray_mod.TrayIcon(
            "总控台 · %s:%d · 运行中" % (HOST, port),
            lambda: webbrowser.open(url),
            lambda: schedule_console_restart(server, port),
            lambda: schedule_console_stop(server),
            _TRAY_PNG)
        tray_icon.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if tray_icon is not None:
            tray_icon.stop()
        server.server_close()
        print("已停止", flush=True)


def redirect_console_output():
    """将总控台输出安全追加到日志目录 console.log。

    供 Windows 无窗口后台运行（pythonw --log-to-file）使用；
    在 pythonw 下 sys.stdout/stderr 为 None、fd 1/2 无效，均已保护。
    """
    path = os.path.join(LOGS_DIR, "console.log")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            else:
                os.chmod(path, 0o600)
        except (AttributeError, OSError):
            pass
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (AttributeError, OSError):
                pass
        # pythonw（无控制台）下 fd 1/2 可能无效，逐个保护
        for target_fd in (1, 2):
            try:
                os.dup2(fd, target_fd)
            except OSError:
                pass
        # 重新绑定 stdout/stderr，保证 pythonw 下 print 不因 None 崩溃。
        # 注意：os.fdopen 的 line_buffering 参数在 Python 3.14 已移除，
        # 统一用 open(fd, buffering=1)（行缓冲）跨版本兼容。
        try:
            out_fd = os.dup(fd)
            sys.stdout = open(out_fd, "w", encoding="utf-8",
                              errors="replace", buffering=1, closefd=False)
            err_fd = os.dup(fd)
            sys.stderr = open(err_fd, "w", encoding="utf-8",
                              errors="replace", buffering=1, closefd=False)
        except OSError:
            pass
    finally:
        os.close(fd)


def main(preferred_port=None, open_browser=True, log_to_file=False,
         expected_app_count=0):
    """Run exactly one console for this project/data directory."""
    configure_console_encoding()
    migration = prepare_runtime_storage()
    if log_to_file:
        redirect_console_output()
    if migration["dataMigrated"]:
        print("已将项目内旧配置和图标复制到: %s" % DATA_DIR,
              flush=True)
    if migration["logsMigrated"]:
        print("已将项目内旧日志复制到: %s" % LOGS_DIR,
              flush=True)
    instance_lock = acquire_instance_lock()
    if instance_lock is None:
        if reap_stale_console_processes():
            instance_lock = acquire_instance_lock()
    if instance_lock is None:
        print("总控台已在运行：同一数据目录只允许一个实例。", flush=True)
        if open_browser:
            instances = find_console_instances()
            ports = [port for item in instances for port in item.get("ports", [])]
            if ports:
                open_console_browser(min(ports))
        return False
    try:
        leftovers = find_console_instances()
        if leftovers:
            pids = [item["pid"] for item in leftovers]
            print("发现残留总控台进程，正在清理: %s" %
                  ", ".join(str(pid) for pid in pids), flush=True)
            _reap_console_pids(pids, force=True)
        _run_console(preferred_port, open_browser, expected_app_count)
        return True
    finally:
        release_instance_lock(instance_lock)


if __name__ == "__main__":
    if "--prepare-storage" in sys.argv:
        # 供安装/诊断流程预先验证迁移和目录权限，不启动 HTTP。
        prepare_runtime_storage()
    elif "--launcher" in sys.argv:
        launcher_main()
    elif "--restart-helper" in sys.argv:
        index = sys.argv.index("--restart-helper")
        try:
            old = int(sys.argv[index + 1])
            preferred = int(sys.argv[index + 2])
        except (ValueError, IndexError):
            sys.exit(2)
        sys.exit(restart_helper(old, preferred))
    else:
        preferred = None
        if "--preferred-port" in sys.argv:
            index = sys.argv.index("--preferred-port")
            try:
                preferred = int(sys.argv[index + 1])
            except (ValueError, IndexError):
                sys.exit(2)
        expected_app_count = 0
        if "--expected-app-count" in sys.argv:
            index = sys.argv.index("--expected-app-count")
            try:
                expected_app_count = max(0, int(sys.argv[index + 1]))
            except (ValueError, IndexError):
                sys.exit(2)
        # --log-to-file：无窗口后台运行（Windows pythonw / start.bat），
        # 输出写入 LOGS_DIR/console.log，避免无控制台时 print 崩溃。
        log_to_file = "--log-to-file" in sys.argv
        main(preferred_port=preferred,
             open_browser="--no-browser" not in sys.argv,
             log_to_file=log_to_file,
             expected_app_count=expected_app_count)
