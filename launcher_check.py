#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 启动器辅助：探测实例状态 / 打开控制台 / 重启控制台。

供 start.bat 调用，输出保持纯 ASCII（Windows cmd 按代码页解析，
非 ASCII 输出会乱码并破坏分支判断）：

    python launcher_check.py status          -> RUNNING <port> | STOPPED
    python launcher_check.py open <port>     -> 打开浏览器
    python launcher_check.py restart <port>  -> POST /api/console/restart

探测不只检查端口连通，还请求 /api/health 确认是总控台实例，
避免把占用 9600-9609 的无关程序误判为控制台。
"""
import json
import socket
import sys
import urllib.request

PORT_START = 9600
PORT_TRIES = 10
HEALTH_TIMEOUT = 1.0


def _is_console(port):
    """端口开放且 /api/health 返回 ok 才算总控台实例。"""
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/health" % port,
                timeout=HEALTH_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
        return bool(data.get("ok"))
    except Exception:
        return False


def find_console_port():
    for port in range(PORT_START, PORT_START + PORT_TRIES):
        s = socket.socket()
        s.settimeout(0.3)
        try:
            s.connect(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            s.close()
        if _is_console(port):
            return port
    return None


def main(argv):
    action = argv[1] if len(argv) > 1 else "status"
    port = None
    if len(argv) > 2:
        try:
            port = int(argv[2])
        except ValueError:
            port = None
    if port is None:
        port = find_console_port()

    if action == "open":
        if port is None:
            print("STOPPED")
            return 0
        import webbrowser
        webbrowser.open("http://127.0.0.1:%d/" % port)
        print("OPENED %d" % port)
        return 0

    if action == "restart":
        if port is None:
            print("STOPPED")
            return 0
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/console/restart" % port,
            data=b"{}", method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            r.read()
        print("RESTARTING %d" % port)
        return 0

    # 默认 status
    if port is None:
        print("STOPPED")
    else:
        print("RUNNING %d" % port)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
