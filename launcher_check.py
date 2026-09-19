#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 启动器辅助：探测实例状态 / 打开控制台 / 重启控制台。

供 start.bat 调用，输出保持纯 ASCII（Windows cmd 按代码页解析，
非 ASCII 输出会乱码并破坏分支判断）：

    python launcher_check.py status          -> RUNNING <port> | STALE <port> | STOPPED
    python launcher_check.py ensure-runtime  -> OK | ERROR ...
    python launcher_check.py launch [port]   -> RUNNING <port> | ERROR ...
    python launcher_check.py open <port>     -> 打开浏览器
    python launcher_check.py restart <port>  -> POST /api/console/restart

探测不只检查端口连通，还请求 /api/health 确认是总控台实例，
避免把占用 9600-9609 的无关程序误判为控制台。
"""
import json
import os
import socket
import stat
import subprocess
import sys
import re
import sysconfig
import time
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT_START = 9600
PORT_TRIES = 10
HEALTH_TIMEOUT = 1.0
STATE_TIMEOUT = 5.0
LAUNCH_ATTEMPTS = 2
LAUNCH_WAIT_SEC = 15.0
CONTROL_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")


def _control_token_path():
    raw = (os.environ.get("CONSOLE_DATA_DIR") or "").strip()
    if raw:
        return os.path.join(os.path.abspath(os.path.expanduser(raw)),
                            "control.token")
    base = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
    return os.path.join(base, "总控台", "control.token")


def _config_path():
    return os.path.join(os.path.dirname(_control_token_path()), "config.json")


def _read_control_token():
    path = _control_token_path()
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as f:
            token = f.read(256).decode("ascii").strip()
    except (OSError, UnicodeError):
        return None
    return token if CONTROL_TOKEN_RE.fullmatch(token) else None


def _console_url(port, token):
    return "http://127.0.0.1:%d/#console_token=%s" % (
        port, urllib.parse.quote(token, safe="-_"))


def _read_json(port, path, timeout):
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d%s" % (port, path),
                timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _configured_app_count():
    try:
        with open(_config_path(), "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return 0
    apps = raw.get("apps") if isinstance(raw, dict) else None
    if not isinstance(apps, list):
        return 0
    return sum(1 for app in apps
               if isinstance(app, dict) and app.get("id"))


def _console_status(port, disk_app_count=None):
    """Return RUNNING/STALE for a console, or None for a foreign port."""
    health = _read_json(port, "/api/health", HEALTH_TIMEOUT)
    if not isinstance(health, dict) or not health.get("ok"):
        return None
    if disk_app_count is None:
        disk_app_count = _configured_app_count()
    if disk_app_count <= 0:
        return "RUNNING"

    config = health.get("config")
    if isinstance(config, dict):
        memory_count = config.get("memoryAppCount")
        reported_disk_count = config.get("diskAppCount")
        if memory_count == 0 or reported_disk_count == 0:
            return "STALE"
        if isinstance(memory_count, int) and memory_count > 0:
            return "RUNNING"

    # Older backends do not expose config counts in /api/health. Confirm an
    # actual mismatch when possible, but never replace an otherwise healthy
    # instance merely because the expensive state request timed out.
    state = _read_json(port, "/api/state", STATE_TIMEOUT)
    if isinstance(state, dict) and isinstance(state.get("apps"), list):
        return "RUNNING" if state["apps"] else "STALE"
    return "RUNNING"


def _is_console(port):
    """端口开放且 /api/health 返回 ok 才算总控台实例。"""
    return _console_status(port) is not None


def find_console_status():
    disk_app_count = _configured_app_count()
    for port in range(PORT_START, PORT_START + PORT_TRIES):
        s = socket.socket()
        s.settimeout(0.3)
        try:
            s.connect(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            s.close()
        status = _console_status(port, disk_app_count)
        if status:
            return status, port
    return "STOPPED", None


def find_console_port():
    _, port = find_console_status()
    return port


def _pythonw_executable():
    candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return candidate if os.path.isfile(candidate) else sys.executable


def _start_console_candidate(expected_app_count, preferred_port=None):
    args = [
        _pythonw_executable(), os.path.join(BASE_DIR, "server.py"),
        "--no-browser", "--log-to-file",
        "--expected-app-count", str(max(0, int(expected_app_count))),
    ]
    if isinstance(preferred_port, int):
        args.extend(["--preferred-port", str(preferred_port)])
    return subprocess.Popen(
        args, cwd=BASE_DIR, close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def launch_console(preferred_port=None, attempts=LAUNCH_ATTEMPTS,
                   wait_sec=LAUNCH_WAIT_SEC):
    """Start and verify a non-empty console, retrying one stale candidate."""
    expected_app_count = _configured_app_count()
    for _ in range(max(1, int(attempts))):
        status, port = find_console_status()
        if status == "RUNNING":
            return port
        try:
            candidate = _start_console_candidate(
                expected_app_count, preferred_port)
        except OSError:
            continue
        deadline = time.monotonic() + max(0.1, float(wait_sec))
        while time.monotonic() < deadline:
            status, port = find_console_status()
            if status == "RUNNING":
                return port
            if candidate.poll() is not None:
                break
            time.sleep(0.25)
    status, port = find_console_status()
    return port if status == "RUNNING" else None


PSUTIL_SPEC = "psutil>=7.2"


def _psutil_importable_without_user_site():
    """Match pythonw: user-site packages may be invisible."""
    try:
        completed = subprocess.run(
            [sys.executable, "-s", "-c", "import psutil"],
            capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _install_psutil():
    pip = [sys.executable, "-m", "pip", "install", PSUTIL_SPEC]
    try:
        completed = subprocess.run(
            pip, capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        completed = None
    if (completed is not None and completed.returncode == 0
            and _psutil_importable_without_user_site()):
        return True
    target = os.path.realpath(sysconfig.get_path("purelib"))
    try:
        completed = subprocess.run(
            pip + ["--target", target],
            capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and _psutil_importable_without_user_site()


def ensure_runtime():
    # pythonw may not see user-site; require psutil under sys.prefix.
    if sys.version_info < (3, 12):
        print("ERROR Python 3.12+ required")
        return 1
    if sys.platform != "win32":
        print("ERROR this build is Windows-only")
        return 1
    if _psutil_importable_without_user_site():
        print("OK")
        return 0
    print("INFO installing %s into this interpreter" % PSUTIL_SPEC)
    if not _install_psutil():
        print("ERROR psutil install failed")
        print('Run: python -m pip install "psutil>=7.2"')
        return 1
    if not _psutil_importable_without_user_site():
        print("ERROR psutil is not visible to pythonw")
        return 1
    print("OK")
    return 0


def main(argv):
    action = argv[1] if len(argv) > 1 else "status"
    if action == "ensure-runtime":
        return ensure_runtime()
    if action == "status":
        status, port = find_console_status()
        print(status if port is None else "%s %d" % (status, port))
        return 0
    port = None
    if len(argv) > 2:
        try:
            port = int(argv[2])
        except ValueError:
            port = None
    if port is None:
        port = find_console_port()

    if action == "launch":
        port = launch_console(port)
        if port is None:
            print("ERROR console failed readiness check")
            return 1
        print("RUNNING %d" % port)
        return 0

    if action == "open":
        if port is None:
            print("STOPPED")
            return 0
        token = _read_control_token()
        if not token:
            print("TOKEN_UNAVAILABLE")
            return 1
        import webbrowser
        webbrowser.open(_console_url(port, token))
        print("OPENED %d" % port)
        return 0

    if action == "restart":
        if port is None:
            print("STOPPED")
            return 0
        token = _read_control_token()
        if not token:
            print("TOKEN_UNAVAILABLE")
            return 1
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/console/restart" % port,
            data=b"{}", method="POST",
            headers={"Content-Type": "application/json",
                     "X-Console-Token": token})
        with urllib.request.urlopen(req, timeout=8) as r:
            r.read()
        print("RESTARTING %d" % port)
        return 0

    print("ERROR unknown action")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
