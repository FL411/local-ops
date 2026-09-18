#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""总控台 Windows 系统操作层。

在 server.py 与操作系统之间提供统一接口：进程快照、监听端口、进程
工作目录、进程树识别、SID 归属、信号终止、单实例锁、系统对话框。

本仓库是 Windows 专用版本。进程扫描基于 psutil（唯一运行时第三方依赖），
用进程树（root pid 为锚点、沿 ppid 向上回溯）表达受控进程组。
非 Windows 平台在导入时立即退出，并引导 macOS 用户使用上游仓库。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

if sys.platform != "win32":
    sys.stderr.write(
        "总控台是 Windows 专用版本。macOS 请使用上游仓库: "
        "https://github.com/laogou717/local-ops\n")
    raise SystemExit(2)

try:
    import psutil
except ImportError:  # pragma: no cover - 首次启动由启动器安装
    psutil = None

IS_WINDOWS = True

LOG_LOCK = threading.RLock()

# ------------------------------------------------------------------ 平台常量


def _windows_process_sid(pid):
    """返回 Windows 进程 TokenUser SID 字符串；无法可靠读取时返回 None。"""
    try:
        import ctypes
        import ctypes.wintypes as wt

        process_query_limited_information = 0x1000
        token_query = 0x0008
        token_user_class = 1

        class SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]

        class TOKEN_USER(ctypes.Structure):
            _fields_ = [("User", SID_AND_ATTRIBUTES)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel32.OpenProcess.restype = wt.HANDLE
        kernel32.CloseHandle.argtypes = [wt.HANDLE]
        kernel32.CloseHandle.restype = wt.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD,
                                              ctypes.POINTER(wt.HANDLE)]
        advapi32.OpenProcessToken.restype = wt.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wt.HANDLE, ctypes.c_uint, ctypes.c_void_p, wt.DWORD,
            ctypes.POINTER(wt.DWORD)]
        advapi32.GetTokenInformation.restype = wt.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR)]
        advapi32.ConvertSidToStringSidW.restype = wt.BOOL

        process = kernel32.OpenProcess(
            process_query_limited_information, False, int(pid))
        if not process:
            return None
        try:
            token = wt.HANDLE()
            if not advapi32.OpenProcessToken(process, token_query,
                                             ctypes.byref(token)):
                return None
            try:
                size = wt.DWORD()
                # 首次调用预期失败并返回所需缓冲区长度。
                advapi32.GetTokenInformation(
                    token, token_user_class, None, 0, ctypes.byref(size))
                if not size.value:
                    return None
                buffer = ctypes.create_string_buffer(size.value)
                if not advapi32.GetTokenInformation(
                        token, token_user_class, buffer, size,
                        ctypes.byref(size)):
                    return None
                token_user = ctypes.cast(
                    buffer, ctypes.POINTER(TOKEN_USER)).contents
                sid_text = wt.LPWSTR()
                if not advapi32.ConvertSidToStringSidW(
                        token_user.User.Sid, ctypes.byref(sid_text)):
                    return None
                try:
                    return sid_text.value or None
                finally:
                    kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
            finally:
                kernel32.CloseHandle(token)
        finally:
            kernel32.CloseHandle(process)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def self_uid():
    """当前用户身份标识（Windows TokenUser SID）。"""
    return _windows_process_sid(os.getpid())


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
    base = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
    return os.path.join(base, "总控台")


def default_logs_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
    return os.path.join(base, "总控台")


def _protect_private_windows_dacl(path):
    """将 Windows 路径 owner 和 DACL 收紧到当前 TokenUser SID。"""
    sid = SELF_UID
    if not isinstance(sid, str) or not sid:
        raise OSError("无法读取当前 Windows 用户 SID")
    try:
        import ctypes
        import ctypes.wintypes as wt

        class TRUSTEE_W(ctypes.Structure):
            _fields_ = [
                ("pMultipleTrustee", ctypes.c_void_p),
                ("MultipleTrusteeOperation", wt.DWORD),
                ("TrusteeForm", wt.DWORD),
                ("TrusteeType", wt.DWORD),
                ("ptstrName", ctypes.c_void_p),
            ]

        class EXPLICIT_ACCESS_W(ctypes.Structure):
            _fields_ = [
                ("grfAccessPermissions", wt.DWORD),
                ("grfAccessMode", wt.DWORD),
                ("grfInheritance", wt.DWORD),
                ("Trustee", TRUSTEE_W),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        advapi32.ConvertStringSidToSidW.argtypes = [
            wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
        advapi32.ConvertStringSidToSidW.restype = wt.BOOL
        advapi32.SetEntriesInAclW.argtypes = [
            wt.ULONG, ctypes.POINTER(EXPLICIT_ACCESS_W), ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p)]
        advapi32.SetEntriesInAclW.restype = wt.DWORD
        advapi32.SetNamedSecurityInfoW.argtypes = [
            wt.LPWSTR, ctypes.c_int, wt.DWORD, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        advapi32.SetNamedSecurityInfoW.restype = wt.DWORD

        sid_ptr = ctypes.c_void_p()
        if not advapi32.ConvertStringSidToSidW(sid, ctypes.byref(sid_ptr)):
            raise OSError(ctypes.get_last_error(), "无法解析当前 Windows 用户 SID")
        acl_ptr = ctypes.c_void_p()
        try:
            access = EXPLICIT_ACCESS_W()
            access.grfAccessPermissions = 0x10000000  # GENERIC_ALL
            access.grfAccessMode = 1                  # GRANT_ACCESS
            access.grfInheritance = 0                 # NO_INHERITANCE
            access.Trustee.TrusteeForm = 0             # TRUSTEE_IS_SID
            access.Trustee.TrusteeType = 1             # TRUSTEE_IS_USER
            access.Trustee.ptstrName = sid_ptr
            error = advapi32.SetEntriesInAclW(
                1, ctypes.byref(access), None, ctypes.byref(acl_ptr))
            if error:
                raise OSError(error, "无法创建 Windows 私有文件 ACL")
            try:
                # OWNER_SECURITY_INFORMATION + DACL_SECURITY_INFORMATION +
                # PROTECTED_DACL_SECURITY_INFORMATION：统一 owner，并移除继承
                # 和所有旧 ACE。Runner 等管理员令牌可能默认用 Administrators
                # 作为新文件 owner，不能只改 DACL 后再按 TokenUser 校验 owner。
                error = advapi32.SetNamedSecurityInfoW(
                    os.path.abspath(path), 1, 0x80000005,
                    sid_ptr, None, acl_ptr, None)
                if error:
                    raise OSError(error, "无法设置 Windows 私有文件 ACL")
            finally:
                if acl_ptr:
                    kernel32.LocalFree(acl_ptr)
        finally:
            kernel32.LocalFree(sid_ptr)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, OSError):
            raise
        raise OSError("无法设置 Windows 私有文件 ACL: %s" % exc) from exc


def protect_private_file(path):
    """将私有文件限制为当前用户可读写。"""
    os.chmod(path, 0o600)
    _protect_private_windows_dacl(path)


def protect_private_directory(path):
    """将私有目录限制为当前用户可访问，阻止令牌被替换或预先放置。"""
    os.chmod(path, 0o700)
    _protect_private_windows_dacl(path)


def windows_acl_ace_count(path):
    """返回 Windows 路径实际 DACL 的 ACE 数量。"""
    try:
        import ctypes
        import ctypes.wintypes as wt

        class ACL_SIZE_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("AceCount", wt.DWORD),
                ("AclBytesInUse", wt.DWORD),
                ("AclBytesFree", wt.DWORD),
            ]

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32.GetNamedSecurityInfoW.argtypes = [
            wt.LPWSTR,
            wt.DWORD,
            wt.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.GetNamedSecurityInfoW.restype = wt.DWORD
        advapi32.GetAclInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            wt.DWORD,
            wt.DWORD,
        ]
        advapi32.GetAclInformation.restype = wt.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        error = advapi32.GetNamedSecurityInfoW(
            os.path.abspath(path),
            1,  # SE_FILE_OBJECT
            0x00000004,  # DACL_SECURITY_INFORMATION
            None,
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if error:
            raise OSError(error, "无法读取 Windows DACL")
        try:
            if not dacl.value:
                return 0
            info = ACL_SIZE_INFORMATION()
            if not advapi32.GetAclInformation(
                dacl,
                ctypes.byref(info),
                ctypes.sizeof(info),
                2,  # AclSizeInformation
            ):
                raise OSError(ctypes.get_last_error(), "无法读取 Windows ACL 信息")
            return int(info.AceCount)
        finally:
            if descriptor:
                kernel32.LocalFree(descriptor)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, OSError):
            raise
        raise OSError("无法读取 Windows DACL: %s" % exc) from exc


# ------------------------------------------------------------------ 单实例锁


def acquire_lock(path):
    """获取单实例文件锁，返回保持打开的锁对象（进程退出自动释放）。

    Windows 用 msvcrt.locking 锁首字节。返回 None 表示已被其他实例持有。
    """
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    lock_file = os.fdopen(fd, "r+", encoding="ascii")
    # 先锁字节 0（固定位置）再写 pid：锁位置与 pid 字符串长度无关，
    # 避免不同位数 pid 的实例锁到不同字节导致单实例失效。
    # 未获锁的进程会在此抛 PermissionError，必须优雅返回 None；
    # close 也可能因文件仍被锁定而抛错，需要二次保护。
    import msvcrt
    try:
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        lock_file.seek(0)
        try:
            lock_file.truncate()
        except OSError:
            pass
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
    global psutil
    if psutil is None:
        try:
            import psutil as _mod
        except ImportError:
            raise RuntimeError(
                "Windows 平台需要 psutil：pip install psutil"
                "（或用 start.bat / LocalOpsConsole.exe 自动安装）")
        psutil = _mod
    return psutil


def pid_alive(pid):
    """进程是否存活（不要求有权限发送信号）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        return _psutil().pid_exists(pid)
    except Exception:
        return False


def process_uid(pid):
    """返回进程 SID；进程不存在或不可读返回 None。"""
    return _windows_process_sid(pid)


def _windows_path_owner_sid(path):
    """返回文件/目录所有者 SID；无法可靠读取时返回 None。"""
    try:
        import ctypes
        import ctypes.wintypes as wt

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32.GetNamedSecurityInfoW.argtypes = [
            wt.LPWSTR, ctypes.c_int, wt.DWORD, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)]
        advapi32.GetNamedSecurityInfoW.restype = wt.DWORD
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR)]
        advapi32.ConvertSidToStringSidW.restype = wt.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        owner = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        error = advapi32.GetNamedSecurityInfoW(
            os.path.abspath(path), 1, 0x00000001, ctypes.byref(owner), None,
            None, None, ctypes.byref(descriptor))
        if error or not owner:
            return None
        try:
            sid_text = wt.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(
                    owner, ctypes.byref(sid_text)):
                return None
            try:
                return sid_text.value or None
            finally:
                kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        finally:
            kernel32.LocalFree(descriptor)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def path_owned_by_current_user(path):
    """路径是否明确由当前用户拥有；身份未知必须返回 False。"""
    try:
        owner = _windows_path_owner_sid(path)
        return owner is not None and SELF_UID is not None and owner == SELF_UID
    except OSError:
        return False


# ------------------------------------------------------------------ 进程快照

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
                # psutil 不提供 SID；通过 TokenUser 读取失败时保留 None，
                # 调用方必须把未知身份当成非当前用户。
                "uid": _windows_process_sid(pid) if with_uid else -1,
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
    """逻辑核心数。用于把 CPU 归一为「占全部核心百分比」，
    与任务管理器口径一致（吃满 1 核 = 100/核心数 %）。
    """
    global _CORE_COUNT
    if _CORE_COUNT is None:
        try:
            _CORE_COUNT = _psutil().cpu_count() or 1
        except Exception:
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
    """批量进程信息 → {pid: {"uid","comm","args","cpu","mem","etime","ctime"}}。"""
    return _ps_snapshot_windows(pids, with_uid)


# ------------------------------------------------------------------ 监听端口


def scan_listeners():
    """监听快照 → {(pid, port): {bind_host, ...}}。"""
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
    """{pid: cwd}。"""
    pids = [int(p) for p in pids]
    if not pids:
        return {}
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
    """返回进程树的全部成员 pid 列表（不含过滤，含根）。"""
    return _group_members_windows(pgid)


def process_group_id(pid):
    """返回进程组锚点；Windows 以进程自身 pid 为锚点。"""
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
    """向进程树发信号。返回 (ok, error)。

    对调用方已验证的冻结成员列表（未提供时才重新扫描）逐个终止。
    非 force 时先走 WM_CLOSE 软通道（带窗口进程可自行清理），宽限后
    对仍存活成员执行硬杀兜底。
    """
    mod = _psutil()
    # Windows 的 signal 模块没有 SIGKILL 常量，用数值 9 等价判断
    force = (sig == getattr(signal, "SIGKILL", 9))
    members = (list(members) if members is not None
               else _group_members_windows(pgid))
    if not members:
        return True, None
    if not force:
        for pid in reversed(members):
            _wm_close_soft(pid)
        time.sleep(GRACE_SOFT_STOP_SEC)
    errors = []
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
    """进程树中是否仍有存活成员。"""
    return any(pid_alive(p) for p in _group_members_windows(pgid))


def kill_process(pid, force):
    """结束单个进程。返回 (ok, error)；调用方需先完成用户归属校验。"""
    pid = int(pid)
    mod = _psutil()
    if not force:
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

    cmd /c "echo <marker> & <command>"，CREATE_NEW_PROCESS_GROUP +
    DETACHED_PROCESS 脱离控制台。/c 后的命令必须作为原始命令行传给
    CreateProcess；若使用 argv 列表，subprocess 会把内层引号转义成 \",
    cmd 会将带空格的可执行路径误当成字面命令名。
    """
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
    try:
        import ctypes
        res = ctypes.windll.user32.MessageBoxW(
            0, message, "总控台", 0x00000040 | 0x00000002 | 0x00000000)
        if res == 3:
            return "重新启动"
        if res == 4:
            return "打开控制台"
        return "取消"
    except Exception:
        return None


def launcher_alert(message):
    """错误提示对话框；失败静默。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, "总控台", 0x00000010)
    except Exception:
        pass


def spawn_detached(args, cwd):
    """启动完全脱离当前进程的新进程（独立进程组、无窗口）。"""
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | \
        getattr(subprocess, "DETACHED_PROCESS", 0) | \
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(args, cwd=cwd, creationflags=creationflags,
                            close_fds=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
