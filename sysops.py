#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""总控台跨平台系统操作层。

在 server.py 与操作系统之间提供统一接口：进程快照、监听端口、进程
工作目录、进程组识别、UID 概念、信号终止、单实例锁、系统对话框。

设计原则：
- POSIX（macOS）分支保持原实现：调用系统自带 ps/lsof/osascript，
  保持“运行时零第三方依赖”；
- Windows 分支基于 psutil（唯一运行时第三方依赖，pip install psutil），
  用进程树（root pid 为锚点、沿 ppid 向上回溯）模拟 macOS 的
  进程组语义；
- 对外函数签名与返回结构与 server.py 原有调用保持一致，两个平台
  的调用方代码无需分叉。
"""

from __future__ import annotations

import errno
import os
import re
import signal
import subprocess
import sys
import threading
import time

try:
    import psutil
except ImportError:  # pragma: no cover - 仅 Windows 需要
    psutil = None

IS_WINDOWS = sys.platform == "win32"
IS_POSIX = os.name == "posix"

LOG_LOCK = threading.RLock()

# ------------------------------------------------------------------ 平台常量


def self_uid():
    """当前用户身份标识。

    POSIX 返回真实 uid；Windows 没有与 Unix 等价的 uid，psutil 也不暴露
    跨进程 uid，这里统一返回 0 表示“本机交互用户”，配合本地回环边界使用。
    单用户桌面场景可接受；多用户共享主机请改用 POSIX 平台。
    """
    if IS_POSIX:
        return os.getuid()
    return 0


SELF_UID = self_uid()

# Windows 系统进程目录前缀，用于归类“后台/系统进程”
_WINDOWS_SYSTEM_DIRS = None


def windows_system_dirs():
    global _WINDOWS_SYSTEM_DIRS
    if _WINDOWS_SYSTEM_DIRS is None:
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        _WINDOWS_SYSTEM_DIRS = tuple(
            os.path.normpath(os.path.join(root, d)) + os.sep
            for d in ("System32", "SysWOW64", "system32", "WinSxS"))
    return _WINDOWS_SYSTEM_DIRS


def default_data_dir():
    if IS_POSIX:
        return os.path.expanduser("~/Library/Application Support/总控台")
    base = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
    return os.path.join(base, "总控台")


def default_logs_dir():
    if IS_POSIX:
        return os.path.expanduser("~/Library/Logs/总控台")
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
    return os.path.join(base, "总控台")


# ------------------------------------------------------------------ 单实例锁


def acquire_lock(path):
    """获取单实例文件锁，返回保持打开的锁对象（进程退出自动释放）。

    POSIX 用 flock；Windows 用 msvcrt.locking 锁首字节。
    返回 None 表示已被其他实例持有。
    """
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    lock_file = os.fdopen(fd, "r+", encoding="ascii")
    if IS_POSIX:
        import fcntl
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            lock_file.close()
            if e.errno in (errno.EACCES, errno.EAGAIN):
                return None
            raise
        try:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write("%d\n" % os.getpid())
            lock_file.flush()
            os.fsync(lock_file.fileno())
        except OSError:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
            raise
        return lock_file
    # Windows: msvcrt.locking 一次锁 1 字节。
    # 先锁字节 0（固定位置）再写 pid：锁位置与 pid 字符串长度无关，
    # 避免不同位数 pid 的实例锁到不同字节导致单实例失效。
    # 未获锁的进程会在此抛 PermissionError，必须优雅返回 None；
    # close 也可能因文件仍被锁定而抛错，需要二次保护。
    import msvcrt
    try:
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        lock_file.seek(0)
        lock_file.write("%d\n" % os.getpid())
        lock_file.flush()
    except OSError:
        try:
            lock_file.close()
        except OSError:
            pass
        return None
    return lock_file


def release_lock(lock_file):
    if lock_file is None:
        return
    try:
        if IS_POSIX:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        else:
            import msvcrt
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
    finally:
        try:
            lock_file.close()
        except OSError:
            pass


# ------------------------------------------------------------------ 进程基础


def _psutil():
    if psutil is None:
        raise RuntimeError(
            "Windows 平台需要 psutil：pip install psutil（或用 start.bat 自动安装）")
    return psutil


def pid_alive(pid):
    """进程是否存活（不要求有权限发送信号）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_POSIX:
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False
    try:
        return _psutil().pid_exists(pid)
    except Exception:
        return False


def process_uid(pid):
    """返回进程 uid；进程不存在或不可读返回 None。"""
    if IS_POSIX:
        try:
            r = subprocess.run(
                ["ps", "-o", "uid=", "-p", str(int(pid))],
                capture_output=True, text=True, timeout=5)
        except Exception:
            return None
        toks = r.stdout.split()
        if not toks:
            return None
        try:
            return int(toks[0])
        except ValueError:
            return None
    # Windows：统一视为当前交互用户
    if pid_alive(pid):
        return 0
    return None


# ------------------------------------------------------------------ 进程快照


def _ps_snapshot_posix(pids=None, with_uid=True):
    """原 macOS 实现：ps -ax -o pid[,uid],etime,%cpu,%mem,comm + pid,args。"""
    def run(args):
        try:
            r = subprocess.run(args, capture_output=True, text=True,
                               errors="replace", timeout=5)
            return r.stdout or ""
        except Exception:
            return ""

    base = ["ps"]
    if pids is None:
        base.append("-ax")
    else:
        pids = [int(p) for p in pids]
        if not pids:
            return {}
        base += ["-p", ",".join(str(p) for p in pids)]
    fields = ["pid"] + (["uid"] if with_uid else []) + \
             ["etime", "%cpu", "%mem", "comm"]
    out1 = run(base + ["-o", ",".join(fields)])
    out2 = run(base + ["-o", "pid,args"])

    def parse_etime(s):
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

    snap = {}
    fixed = 5 if with_uid else 4
    for line in out1.splitlines():
        toks = line.split()
        if len(toks) < fixed + 1:
            continue
        try:
            pid = int(toks[0])
        except ValueError:
            continue
        i = 1
        entry = {"args": ""}
        if with_uid:
            try:
                entry["uid"] = int(toks[1])
            except ValueError:
                entry["uid"] = -1
            i = 2
        entry["etime"] = parse_etime(toks[i])
        try:
            entry["cpu"] = float(toks[i + 1])
        except (TypeError, ValueError):
            entry["cpu"] = 0.0
        try:
            entry["mem"] = float(toks[i + 2])
        except (TypeError, ValueError):
            entry["mem"] = 0.0
        entry["comm"] = " ".join(toks[i + 3:])
        snap[pid] = entry
    for line in out2.splitlines():
        toks = line.split(None, 1)
        if not toks:
            continue
        try:
            pid = int(toks[0])
        except ValueError:
            continue
        if pid in snap:
            snap[pid]["args"] = toks[1] if len(toks) > 1 else ""
    return snap


def _ps_snapshot_windows(pids=None, with_uid=True):
    """Windows 实现：psutil 遍历，etime 为秒（与 POSIX 语义一致）。

    CPU 为两次快照间的增量百分比，口径为「占全部逻辑核心的百分比」
    （0-100，任务管理器风格，见 core_count()）；首轮采样建立基准返回 0，
    后续轮询返回真实值；缓存按 TTL 清理，兼容全量/子集交替调用。
    """
    mod = _psutil()
    wanted = set(int(p) for p in pids) if pids is not None else None
    snap = {}
    now = time.time()
    mono = time.monotonic()
    attrs = ["name", "exe", "create_time", "memory_percent", "cmdline",
             "cpu_times"]
    if wanted is None:
        processes = mod.process_iter(["pid"] + attrs)
    else:
        processes = []
        for pid in wanted:
            try:
                processes.append(mod.Process(pid))
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
    # 单次遍历：同时收集完整信息与 cpu_times
    entries = []  # (proc, info, cpu_ticks)
    for proc in processes:
        if proc.pid == 0:  # System Idle Process：无实际进程语义
            continue
        try:
            info = proc.info if wanted is None else proc.as_dict(attrs=attrs)
            ct = info["cpu_times"]
            ticks = (ct.user + ct.system) if ct else 0.0
            entries.append((proc, info, ticks))
        except (psutil.NoSuchProcess, psutil.AccessDenied,
                psutil.ZombieProcess, TypeError, ValueError, OSError):
            continue
    cpu_by_pid = _diff_cpu_windows(mono, [(p.pid, t) for p, _, t in entries])
    for proc, info, _ in entries:
        try:
            pid = proc.pid
            cmdline = info["cmdline"] or []
            if not cmdline and wanted is not None:
                cmdline = [info["name"] or ""]
            args = " ".join(str(t) for t in cmdline)
            comm = info["exe"] or info["name"] or ""
            create_time = info["create_time"]
            etime = int(max(0.0, now - create_time)) if create_time else 0
            snap[pid] = {
                "uid": 0 if with_uid else -1,
                "comm": comm,
                "args": args,
                "cpu": cpu_by_pid.get(pid, 0.0),
                "mem": round(info["memory_percent"] or 0.0, 2),
                "etime": etime,
                # 进程创建时间戳（epoch 秒）。用于身份校验时识别 PID 复用：
                # attach 记录后，若同 PID 的 ctime 不同则说明已被新进程占用。
                "ctime": create_time,
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess,
                TypeError, ValueError, OSError):
            continue
    return snap


# 跨快照 CPU 采样缓存：{pid: (monotonic, user+system cpu_times)}
_CPU_SAMPLES = {}
_CPU_LOCK = threading.Lock()
_CPU_SAMPLE_TTL = 30.0  # 秒；超过则视为新进程重新建立基准

_CORE_COUNT = None


def core_count():
    """逻辑核心数。Windows 用于把 CPU 归一为「占全部核心百分比」，
    与任务管理器口径一致（吃满 1 核 = 100/核心数 %）；macOS 保持
    单核口径返回 1，避免改变原有展示语义。
    """
    global _CORE_COUNT
    if _CORE_COUNT is None:
        if IS_WINDOWS:
            try:
                _CORE_COUNT = _psutil().cpu_count() or 1
            except Exception:
                _CORE_COUNT = 1
        else:
            _CORE_COUNT = 1
    return _CORE_COUNT


def _diff_cpu_windows(mono, samples):
    """samples=[(pid, cpu_ticks)] → {pid: cpu_percent}（两次采样差分）。

    Windows 输出「占全部逻辑核心的百分比」（0-100，任务管理器口径）：
    单核百分比 / 核心数，多线程进程并行再多核也不会超过 100。
    """
    cores = core_count()
    result = {}
    with _CPU_LOCK:
        stale = [pid for pid, (t, _) in _CPU_SAMPLES.items()
                 if mono - t > _CPU_SAMPLE_TTL]
        for pid in stale:
            del _CPU_SAMPLES[pid]
        for pid, ticks in samples:
            prev = _CPU_SAMPLES.get(pid)
            if prev is not None:
                prev_mono, prev_ticks = prev
                dt = mono - prev_mono
                dc = ticks - prev_ticks
                single = (dc / dt * 100.0) if dt > 0.01 else 0.0
                result[pid] = round(single / cores, 2)
            else:
                result[pid] = 0.0
            _CPU_SAMPLES[pid] = (mono, ticks)
    return result


def ps_snapshot(pids=None, with_uid=True):
    """批量进程信息 → {pid: {"uid","comm","args","cpu","mem","etime"}}。"""
    if IS_POSIX:
        return _ps_snapshot_posix(pids, with_uid)
    return _ps_snapshot_windows(pids, with_uid)


# ------------------------------------------------------------------ 监听端口


def scan_listeners():
    """监听快照 → {(pid, port): {bind_host, ...}}。

    POSIX 用 lsof；Windows 用 psutil.net_connections。
    """
    if IS_POSIX:
        try:
            r = subprocess.run(
                ["lsof", "-iTCP", "-sTCP:LISTEN", "-P", "-n"],
                capture_output=True, text=True, errors="replace", timeout=5)
        except Exception:
            return {}
        found = {}
        for line in r.stdout.splitlines():
            if not line or line.startswith("COMMAND"):
                continue
            parts = line.split()
            if len(parts) < 9:
                continue
            try:
                pid = int(parts[1])
            except ValueError:
                continue
            port = None
            bind_host = None
            for tok in reversed(parts):
                m = re.search(r":(\d+)$", tok)
                if m:
                    port = int(m.group(1))
                    bind_host = tok[:m.start()]
                    if bind_host.startswith("[") and bind_host.endswith("]"):
                        bind_host = bind_host[1:-1]
                    break
            if port is None:
                continue
            found.setdefault((pid, port), set()).add(bind_host or "")
        return found

    mod = _psutil()
    found = {}
    try:
        conns = mod.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError):
        return {}
    for conn in conns:
        if conn.status != "LISTEN" or conn.pid is None:
            continue
        laddr = conn.laddr
        if not laddr:
            continue
        port = int(laddr.port)
        host = laddr.ip or ""
        found.setdefault((conn.pid, port), set()).add(host)
    return found


# ------------------------------------------------------------------ 进程 cwd


def lsof_cwds(pids):
    """{pid: cwd}。POSIX 用 lsof -d cwd；Windows 用 psutil.Process.cwd()。"""
    pids = [int(p) for p in pids]
    if not pids:
        return {}
    if IS_POSIX:
        try:
            r = subprocess.run(
                ["lsof", "-a", "-p", ",".join(str(p) for p in pids),
                 "-d", "cwd", "-Fn"],
                capture_output=True, text=True, errors="replace", timeout=5)
        except Exception:
            return {}
        result = {}
        cur = None
        for line in r.stdout.splitlines():
            if line.startswith("p"):
                try:
                    cur = int(line[1:])
                except ValueError:
                    cur = None
            elif line.startswith("n") and cur is not None:
                result[cur] = line[1:]
        return result
    mod = _psutil()
    result = {}
    for pid in pids:
        try:
            cwd = mod.Process(pid).cwd()
            if cwd:
                result[pid] = cwd
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return result


# ------------------------------------------------------------------ 进程组（PGID 语义）


def _pgid_map_posix():
    """ps -axo pid=,pgid= → {pgid: [pid, ...]}（macOS 原逻辑）。"""
    groups = {}
    try:
        r = subprocess.run(["ps", "-axo", "pid=,pgid="],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return groups
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, pgid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        groups.setdefault(pgid, []).append(pid)
    return groups


def _group_members_windows(root):
    """Windows 版进程组：以 root pid 为锚点，沿 ppid 向上回溯。

    进程退出后其子进程仍保留原 ppid（Windows 内核不回收该信息），
    因此即使根进程（cmd 包装）已退出，仍能按树找到存活的成员。
    """
    mod = _psutil()
    root = int(root)
    if root <= 0:
        return []
    children = {}
    try:
        for proc in mod.process_iter(["pid", "ppid"]):
            try:
                info = proc.info
                children.setdefault(info["ppid"], []).append(info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
    except Exception:
        return []
    if not children:
        return [root] if mod.pid_exists(root) else []
    result, queue, seen = [], [root], set()
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        result.append(cur)
        for child in children.get(cur, []):
            if child not in seen:
                queue.append(child)
    if root not in result:
        result.append(root)
    return result


def group_members(pgid):
    """返回进程组/进程树的全部成员 pid 列表（不含过滤，含根）。"""
    if IS_POSIX:
        return _pgid_map_posix().get(int(pgid), [])
    return _group_members_windows(pgid)


def process_group_id(pid):
    """返回进程所在进程组 id；POSIX 为 pgid，Windows 返回进程自身。"""
    if IS_POSIX:
        try:
            return os.getpgid(int(pid))
        except (ProcessLookupError, PermissionError, OSError):
            return None
    if pid_alive(pid):
        return int(pid)
    return None


# ------------------------------------------------------------------ 信号与终止

# Windows 无 POSIX 信号模型：软终止使用 WM_CLOSE（taskkill 无 /F），
# 硬杀使用 TerminateProcess。软终止后等待这个宽限期再兜底强杀，
# 给带窗口的服务（GUI dev server）自行清理落盘的机会。
GRACE_SOFT_STOP_SEC = 0.4


def _wm_close_soft(pid):
    """Windows：向带窗口进程发送 WM_CLOSE（taskkill 无 /F）。

    无窗口进程（cmd/python 服务等）会返回非零并保持存活，静默跳过；
    带窗口进程收到 WM_CLOSE 后自行退出。返回是否成功发送。
    """
    try:
        r = subprocess.run(
            ["taskkill", "/PID", str(int(pid))],
            capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


def signal_group(pgid, sig=signal.SIGTERM, members=None):
    """向进程组/进程树发信号。返回 (ok, error)。

    POSIX：os.killpg；Windows：对调用方已验证的冻结成员列表（未提供时
    才重新扫描）逐个终止。非 force 时先走 WM_CLOSE 软通道（带窗口进程
    可自行清理），宽限后对仍存活成员执行硬杀兜底。
    """
    if IS_POSIX:
        try:
            os.killpg(int(pgid), sig)
            return True, None
        except ProcessLookupError:
            return True, None
        except PermissionError:
            return False, "没有权限停止受控进程组"
        except OSError as e:
            return False, "停止受控进程组失败: %s" % e
    mod = _psutil()
    # Windows 的 signal 模块没有 SIGKILL 常量（POSIX 专属），用数值 9 等价判断
    force = (sig == getattr(signal, "SIGKILL", 9))
    members = (list(members) if members is not None
               else _group_members_windows(pgid))
    if not members:
        return True, None
    if not force:
        # 软终止阶段：WM_CLOSE 通道优先；无窗口进程静默失败。
        for pid in reversed(members):
            _wm_close_soft(pid)
        time.sleep(GRACE_SOFT_STOP_SEC)
    errors = []
    # 完整树已冻结；从叶子到根终止，避免父进程先退出后丢失后代关联。
    for pid in reversed(members):
        try:
            proc = mod.Process(pid)
            if force:
                proc.kill()
            else:
                proc.terminate()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            errors.append("PID %d 没有权限" % pid)
        except Exception as e:  # noqa: BLE001
            errors.append("PID %d: %s" % (pid, e))
    if errors:
        return False, "；".join(errors[:3])
    return True, None


def group_alive(pgid):
    """进程组/树中是否仍有存活成员。"""
    if IS_POSIX:
        try:
            os.killpg(int(pgid), 0)
            return True
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
    return any(pid_alive(p) for p in _group_members_windows(pgid))


def kill_process(pid, force):
    """结束单个进程。返回 (ok, error)；调用方需先完成用户归属校验。"""
    pid = int(pid)
    if IS_POSIX:
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.kill(pid, sig)
            return True, None
        except ProcessLookupError:
            return False, "进程不存在"
        except PermissionError:
            return False, "没有权限结束该进程"
        except OSError as e:
            return False, "结束失败: %s" % e
    mod = _psutil()
    if not force:
        # 软终止阶段：WM_CLOSE 通道优先；进程若已退出则视为成功（幂等）。
        _wm_close_soft(pid)
        try:
            proc = mod.Process(pid)
        except psutil.NoSuchProcess:
            return True, None
    else:
        try:
            proc = mod.Process(pid)
        except psutil.NoSuchProcess:
            return False, "进程不存在"
    try:
        if force:
            proc.kill()
        else:
            proc.terminate()
        return True, None
    except psutil.NoSuchProcess:
        return (True, None) if not force else (False, "进程不存在")
    except psutil.AccessDenied:
        return False, "没有权限结束该进程"
    except Exception as e:  # noqa: BLE001
        return False, "结束失败: %s" % e


# ------------------------------------------------------------------ 受控应用启动


def spawn_managed(command, cwd, env, marker, log_fd):
    """启动受控应用进程，返回 Popen 对象。

    POSIX：双层 bash 包装（外层持有随机标记、等待内层后台作业），
    独立会话（setsid）。Windows：cmd /c "echo <marker> & <command>"，
    CREATE_NEW_PROCESS_GROUP + DETACHED_PROCESS 脱离控制台。
    """
    if IS_POSIX:
        outer_script = '/bin/bash -c "$1"\nconsole_status=$?\nexit "$console_status"'
        inner_script = (command + '\nconsole_status=$?\nwait\nexit "$console_status"')
        return subprocess.Popen(
            ["/bin/bash", "-c", outer_script, marker, inner_script],
            cwd=cwd, stdout=log_fd, stderr=subprocess.STDOUT,
            start_new_session=True, env=env)
    # Windows：cmd 常驻外层，echo 携带标记供受控校验。/c 后的命令必须
    # 作为原始命令行传给 CreateProcess；若使用 argv 列表，subprocess 会把
    # 内层引号转义成 \"，cmd 会将带空格的可执行路径误当成字面命令名。
    inner = "echo %s & %s" % (marker, command)
    command_line = 'cmd.exe /d /s /c "%s"' % inner
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | \
        getattr(subprocess, "DETACHED_PROCESS", 0)
    return subprocess.Popen(
        command_line,
        cwd=cwd, stdout=log_fd, stderr=subprocess.STDOUT,
        creationflags=creationflags, env=env,
        stdin=subprocess.DEVNULL)


# ------------------------------------------------------------------ 系统对话框


def pick_path(what):
    """打开系统文件/目录选择框。返回 (path|None, canceled)。"""
    if IS_POSIX:
        if what == "dir":
            script = 'POSIX path of (choose folder with prompt "选择工作目录")'
        else:
            script = 'POSIX path of (choose file with prompt "选择批处理脚本")'
        try:
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=180)
        except Exception:
            return None, False
        if r.returncode != 0:
            return None, True
        return r.stdout.strip().rstrip("/") or None, False
    return _pick_path_windows(what)


def _pick_path_windows(what):
    """Windows 用 tkinter 原生对话框（标准库）。无显示环境时返回失败。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return None, False
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        if what == "dir":
            path = filedialog.askdirectory(title="选择工作目录")
        else:
            path = filedialog.askopenfilename(title="选择批处理脚本")
        root.destroy()
    except Exception:
        return None, False
    if not path:
        return None, True
    return path.replace("/", os.sep).rstrip(os.sep) or None, False


def launcher_dialog(message):
    """多选对话框：返回 "取消"/"重新启动"/"打开控制台" 之一；失败返回 None。"""
    if IS_POSIX:
        script = """on run argv
set messageText to item 1 of argv
display dialog messageText with title "总控台" buttons {"取消", "重新启动", "打开控制台"} default button "打开控制台" cancel button "取消" with icon note
return button returned of result
end run"""
        try:
            r = subprocess.run(["osascript", "-e", script, message],
                               capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout.strip() if r.returncode == 0 else None
    try:
        import ctypes
        res = ctypes.windll.user32.MessageBoxW(
            0, message, "总控台", 0x00000040 | 0x00000002 | 0x00000000)
        # 0x2 = AbortRetryIgnore 风格映射：3=重试(重新启动) 4=忽略(打开控制台)
        if res == 3:
            return "重新启动"
        if res == 4:
            return "打开控制台"
        return "取消"
    except Exception:
        return None


def launcher_alert(message):
    """错误提示对话框；失败静默。"""
    if IS_POSIX:
        script = """on run argv
display alert "总控台" message (item 1 of argv) as critical
end run"""
        try:
            subprocess.run(["osascript", "-e", script, message],
                           capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, "总控台", 0x00000010)
    except Exception:
        pass


def spawn_detached(args, cwd):
    """启动完全脱离当前进程的新进程（POSIX 独立会话；Windows 独立进程组）。"""
    if IS_POSIX:
        return subprocess.Popen(args, cwd=cwd, start_new_session=True,
                                close_fds=True)
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | \
        getattr(subprocess, "DETACHED_PROCESS", 0) | \
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(args, cwd=cwd, creationflags=creationflags,
                            close_fds=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
