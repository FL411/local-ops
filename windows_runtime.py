"""Windows-native process lifecycle management for the console.

Processes launched through this module are assigned to a named Windows Job
Object before they are resumed.  The job has no ``KILL_ON_JOB_CLOSE`` limit,
so closing the console's handles does not stop the application; a later console
instance can reopen the same job by name.

This module is import-safe outside Windows so the pure command/environment
helpers can be tested elsewhere.  Actual process operations require Windows.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from typing import Mapping, Sequence


_RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_CREATE_SUSPENDED = 0x00000004
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_DETACHED_PROCESS = 0x00000008
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_STARTF_USESTDHANDLES = 0x00000100
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_STILL_ACTIVE = 259
_INFINITE = 0xFFFFFFFF
_MAX_WINDOWS_COMMAND_LINE_CHARS = 32767
_MAX_WINDOWS_ENVIRONMENT_CHARS = 32767


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _checked_command_line(value: str) -> str:
    if _utf16_units(value) + 1 > _MAX_WINDOWS_COMMAND_LINE_CHARS:
        raise ValueError("Windows process command line exceeds 32767 UTF-16 characters")
    return value


def job_name_for(run_id: str, sid: str) -> str:
    """Return a stable, session-local job name scoped to the current SID."""
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run_id must contain 1-128 ASCII letters, digits, '_' or '-'")
    if not isinstance(sid, str) or not sid.startswith("S-"):
        raise ValueError("a Windows user SID is required for a managed job")
    sid_tag = hashlib.sha256(sid.encode("ascii", "strict")).hexdigest()[:16]
    return "Local\\LocalOps-%s-%s" % (sid_tag, run_id)


def _validate_text(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError("%s must be a string" % label)
    if "\x00" in value:
        raise ValueError("%s cannot contain NUL" % label)
    return value


def _environment_block(overlay: Mapping[str, str] | None) -> str | None:
    """Merge an explicit overlay with the current environment for CreateProcess."""
    if overlay is None:
        return None
    if not isinstance(overlay, Mapping):
        raise TypeError("env must be a mapping or None")

    # Environment variable names are case-insensitive on Windows.  Keep the
    # most recently supplied spelling while making overlays replace old keys.
    by_folded: dict[str, tuple[str, str]] = {}
    for key, value in os.environ.items():
        if isinstance(key, str) and isinstance(value, str):
            by_folded[key.casefold()] = (key, value)
    for key, value in overlay.items():
        _validate_text(key, "environment variable name")
        _validate_text(value, "environment variable value")
        if not key or ("=" in key and not key.startswith("=")):
            raise ValueError("invalid environment variable name: %r" % key)
        if key.startswith("=") and (len(key) < 3 or key[2] != ":"):
            raise ValueError("invalid Windows drive environment entry: %r" % key)
        by_folded[key.casefold()] = (key, value)

    # Windows requires environment entries to be sorted case-insensitively and
    # terminated by two NUL characters.  The unusual =C: drive entries sort
    # first, as they do in the native environment block.
    entries = ["%s=%s" % pair for pair in by_folded.values()]
    entries.sort(key=lambda item: item.casefold())
    block = "\x00".join(entries) + "\x00\x00"
    if _utf16_units(block) > _MAX_WINDOWS_ENVIRONMENT_CHARS:
        raise ValueError("Windows process environment block exceeds 32767 UTF-16 characters")
    return block


def _resolve_executable(executable: str, cwd: str | None,
                        env_overlay: Mapping[str, str] | None) -> str:
    executable = _validate_text(executable, "executable").strip()
    if not executable:
        raise ValueError("executable is required")
    search_path = next((value for key, value in (env_overlay or {}).items()
                        if isinstance(key, str) and key.casefold() == "path"), None)
    if not search_path:
        search_path = os.environ.get("PATH")
    candidate = executable
    if not os.path.isabs(candidate):
        if os.path.dirname(candidate):
            candidate = os.path.abspath(os.path.join(cwd or os.getcwd(), candidate))
        else:
            found = shutil.which(candidate, path=search_path)
            if found:
                candidate = found
            else:
                candidate = os.path.abspath(os.path.join(cwd or os.getcwd(), candidate))
    candidate = os.path.abspath(candidate)
    if not os.path.isfile(candidate):
        raise FileNotFoundError("executable does not exist: %s" % candidate)
    return candidate


def _cmd_quote_argument(value: str) -> str:
    """Quote one argument for a deliberately selected CMD script launch.

    CMD has no general lossless argv representation: it expands percent
    variables and does not use the Windows CRT's backslash-before-quote rules.
    Quoting protects normal command metacharacters.  Literal percent signs,
    quotes, and line breaks cannot be represented reliably for a batch file and
    are rejected instead of being silently rewritten.
    """
    value = _validate_text(value, "cmd argument")
    if '"' in value or "\r" in value or "\n" in value:
        raise ValueError("CMD mode cannot safely represent quotes or line breaks in an argument")
    if "%" in value:
        raise ValueError("CMD mode cannot safely represent percent signs in batch arguments")
    return '"%s"' % value


def _cmd_line(executable: str, args: Sequence[str]) -> tuple[str, str]:
    """Build (cmd.exe path, raw /c command) for a .bat/.cmd launch."""
    if os.path.splitext(executable)[1].casefold() not in (".bat", ".cmd"):
        raise ValueError("CMD mode requires a .bat or .cmd executable")
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    cmd_exe = os.path.join(system_root, "System32", "cmd.exe")
    if not os.path.isfile(cmd_exe):
        found = shutil.which("cmd.exe")
        if not found:
            raise FileNotFoundError("cmd.exe could not be located")
        cmd_exe = found
    # A batch file must be quoted as the first command token.  Quoting each
    # argument keeps &, |, < and > out of CMD's command syntax.
    inner = _cmd_quote_argument(executable)
    if args:
        inner += " " + " ".join(_cmd_quote_argument(arg) for arg in args)
    cmd_exe = os.path.abspath(cmd_exe)
    command_line = '%s /d /s /c "%s"' % (
        subprocess.list2cmdline([cmd_exe]), inner)
    return cmd_exe, _checked_command_line(command_line)


def _command_for(mode: str, executable: str, args: Sequence[str], cwd: str | None,
                 env: Mapping[str, str] | None) -> tuple[str, str]:
    if mode not in {"exec", "powershell", "cmd", "legacy-shell"}:
        raise ValueError("unsupported launch mode: %s" % mode)
    if mode == "legacy-shell":
        # This is an explicit compatibility path for pre-LaunchSpec commands.
        # The caller supplies the original command string in executable.
        command = _validate_text(executable, "legacy command").strip()
        if not command:
            raise ValueError("legacy command is empty")
        if args:
            raise ValueError("legacy-shell accepts a single original command string")
        system_root = os.environ.get("SystemRoot") or r"C:\Windows"
        cmd_exe = os.path.join(system_root, "System32", "cmd.exe")
        if not os.path.isfile(cmd_exe):
            cmd_exe = shutil.which("cmd.exe") or cmd_exe
        cmd_exe = os.path.abspath(cmd_exe)
        command_line = '%s /d /s /c "%s"' % (
            subprocess.list2cmdline([cmd_exe]), command)
        return cmd_exe, _checked_command_line(command_line)
    if mode == "cmd":
        resolved = _resolve_executable(executable, cwd, env)
        return _cmd_line(resolved, args)
    resolved = _resolve_executable(executable, cwd, env)
    return resolved, _checked_command_line(
        subprocess.list2cmdline([resolved, *args]))


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL)]


class _STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _STARTUPINFOEX(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _STARTUPINFO),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD)]


class _NativeApi:
    """Small, lazy Win32 binding layer; no DLL access occurs at module import."""

    def __init__(self):
        if sys.platform != "win32":
            raise OSError("Windows Job Objects are available only on Windows")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._bind()

    def _bind(self):
        k, a = self.kernel32, self.advapi32
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        k.CreateJobObjectW.argtypes = [ctypes.POINTER(_SECURITY_ATTRIBUTES), wintypes.LPCWSTR]
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k.OpenJobObjectW.restype = wintypes.HANDLE
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                               ctypes.c_void_p, wintypes.DWORD]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 ctypes.c_void_p, wintypes.DWORD,
                                                 ctypes.POINTER(wintypes.DWORD)]
        k.QueryInformationJobObject.restype = wintypes.BOOL
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.TerminateJobObject.restype = wintypes.BOOL
        k.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR,
                                     ctypes.c_void_p, ctypes.c_void_p,
                                     wintypes.BOOL, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.LPCWSTR,
                                     ctypes.POINTER(_STARTUPINFO),
                                     ctypes.POINTER(_PROCESS_INFORMATION)]
        k.CreateProcessW.restype = wintypes.BOOL
        k.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t)]
        k.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        k.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]
        k.UpdateProcThreadAttribute.restype = wintypes.BOOL
        k.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        k.DeleteProcThreadAttributeList.restype = None
        k.ResumeThread.argtypes = [wintypes.HANDLE]
        k.ResumeThread.restype = wintypes.DWORD
        k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.TerminateProcess.restype = wintypes.BOOL
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.GetExitCodeProcess.restype = wintypes.BOOL
        k.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FILETIME),
                                      ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
                                      ctypes.POINTER(_FILETIME)]
        k.GetProcessTimes.restype = wintypes.BOOL
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
                                      wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE),
                                      wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.DuplicateHandle.restype = wintypes.BOOL
        k.GetCurrentProcess.argtypes = []
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(_SECURITY_ATTRIBUTES), wintypes.DWORD,
                                  wintypes.DWORD, wintypes.HANDLE]
        k.CreateFileW.restype = wintypes.HANDLE
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD)]
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.HANDLE)]
        a.OpenProcessToken.restype = wintypes.BOOL
        a.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                          ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]
        a.GetTokenInformation.restype = wintypes.BOOL
        a.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                             ctypes.POINTER(wintypes.LPWSTR)]
        a.ConvertSidToStringSidW.restype = wintypes.BOOL
        k.LocalFree.argtypes = [ctypes.c_void_p]
        k.LocalFree.restype = ctypes.c_void_p

    @staticmethod
    def _check(ok, action):
        if not ok:
            error = ctypes.get_last_error()
            raise OSError(error, "%s: %s" % (action, ctypes.FormatError(error).strip()))

    def _security_attributes_for_sid(self, sid):
        if not isinstance(sid, str) or not sid.startswith("S-"):
            raise ValueError("cannot create Job Object without current user SID")
        # Protected DACL, owned by the current user, grants only that SID full
        # access.  A null/default descriptor would leave named jobs inheriting
        # the desktop's broader default ACL.
        sddl = "O:%sG:%sD:P(A;;GA;;;%s)" % (sid, sid, sid)
        descriptor = ctypes.c_void_p()
        self._check(self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None),
            "build Job Object security descriptor")
        attrs = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES),
                                     descriptor, False)
        return descriptor, attrs

    def create_job(self, name, sid):
        descriptor, attrs = self._security_attributes_for_sid(sid)
        try:
            handle = self.kernel32.CreateJobObjectW(ctypes.byref(attrs), name)
            if not handle:
                self._check(False, "CreateJobObjectW")
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                self.close_handle(handle)
                raise FileExistsError("Job Object already exists: %s" % name)
            # Do not set JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.  The default job
            # limits deliberately leave child services alive across console restarts.
            return handle
        finally:
            self.kernel32.LocalFree(descriptor)

    def open_job(self, name):
        # JOB_OBJECT_QUERY | JOB_OBJECT_TERMINATE | JOB_OBJECT_SET_ATTRIBUTES
        access = 0x0004 | 0x0008 | 0x0002
        handle = self.kernel32.OpenJobObjectW(access, False, name)
        if not handle:
            self._check(False, "OpenJobObjectW(%s)" % name)
        return handle

    def duplicate_job_anchor(self, job):
        """Create a query-only inheritable reference for the private keeper."""
        duplicate = wintypes.HANDLE()
        current = self.kernel32.GetCurrentProcess()
        self._check(self.kernel32.DuplicateHandle(
            current, job, current, ctypes.byref(duplicate), 0x0004, True, 0),
            "DuplicateHandle for Job Object anchor")
        return duplicate

    def assign_process(self, job, process):
        self._check(self.kernel32.AssignProcessToJobObject(job, process),
                    "AssignProcessToJobObject")

    def _duplicated_inheritable_handle(self, value):
        import msvcrt

        if isinstance(value, int):
            fd = value
        elif hasattr(value, "fileno"):
            fd = value.fileno()
        else:
            raise TypeError("redirected standard stream must be a file or file descriptor")
        source = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
        duplicate = wintypes.HANDLE()
        current = self.kernel32.GetCurrentProcess()
        self._check(self.kernel32.DuplicateHandle(
            current, source, current, ctypes.byref(duplicate), 0, True, 0x00000002),
            "DuplicateHandle for child standard stream")
        return duplicate

    def _null_handle(self):
        attrs = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), None, True)
        handle = self.kernel32.CreateFileW(
            "NUL", 0xC0000000, 0x00000003, ctypes.byref(attrs), 3, 0x00000080, None)
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, invalid):
            self._check(False, "CreateFileW(NUL)")
        return wintypes.HANDLE(handle)

    def _prepare_stdio(self, stdout, stderr):
        created: list[wintypes.HANDLE] = []
        if stdout is subprocess.PIPE or stderr is subprocess.PIPE:
            raise ValueError("PIPE redirection is not supported for managed services")
        try:
            null_handle = self._null_handle()
            created.append(null_handle)
            stdin_handle = null_handle
            if stdout is None or stdout == subprocess.DEVNULL:
                stdout_handle = null_handle
            else:
                stdout_handle = self._duplicated_inheritable_handle(stdout)
                created.append(stdout_handle)
            if stderr == subprocess.STDOUT:
                stderr_handle = stdout_handle
            elif stderr is None or stderr == subprocess.DEVNULL:
                stderr_handle = stdout_handle
            else:
                stderr_handle = self._duplicated_inheritable_handle(stderr)
                created.append(stderr_handle)
            return stdin_handle, stdout_handle, stderr_handle, created
        except BaseException:
            # create_suspended_process cannot clean up handles until this
            # helper returns them.  Own partial construction cleanup here.
            for handle in reversed(created):
                try:
                    self.close_handle(handle)
                except Exception:
                    pass
            raise

    @staticmethod
    def _environment_buffer(block):
        if block is None:
            return None
        return ctypes.create_unicode_buffer(block)

    def create_suspended_process(self, executable, command_line, cwd, env,
                                 stdout, stderr, inherited_handles=()):
        # Validate and materialize the environment before allocating inheritable
        # stdio handles.  In particular, an oversized/invalid overlay must not
        # leave prepared handles behind if validation raises.
        environment = self._environment_buffer(_environment_block(env))
        stdin_h, stdout_h, stderr_h, handles = self._prepare_stdio(stdout, stderr)
        all_handles = []
        seen = set()
        for handle in (*handles, *tuple(inherited_handles or ())):
            value = handle.value if hasattr(handle, "value") else int(handle)
            if value and value not in seen:
                seen.add(value)
                all_handles.append(wintypes.HANDLE(value))
        startup = _STARTUPINFOEX()
        startup.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEX)
        startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = stdin_h
        startup.StartupInfo.hStdOutput = stdout_h
        startup.StartupInfo.hStdError = stderr_h
        handle_array = (wintypes.HANDLE * len(all_handles))(
            *(handle.value for handle in all_handles))
        attribute_bytes = ctypes.c_size_t()
        self.kernel32.InitializeProcThreadAttributeList(
            None, 1, 0, ctypes.byref(attribute_bytes))
        if not attribute_bytes.value:
            for handle in handles:
                self.close_handle(handle)
            error = ctypes.get_last_error()
            raise OSError(error, "InitializeProcThreadAttributeList(size): %s" %
                          ctypes.FormatError(error).strip())
        attribute_buffer = ctypes.create_string_buffer(attribute_bytes.value)
        startup.lpAttributeList = ctypes.cast(attribute_buffer, ctypes.c_void_p)
        attributes_initialized = False
        info = _PROCESS_INFORMATION()
        mutable_command = ctypes.create_unicode_buffer(command_line)
        try:
            self._check(self.kernel32.InitializeProcThreadAttributeList(
                startup.lpAttributeList, 1, 0, ctypes.byref(attribute_bytes)),
                "InitializeProcThreadAttributeList")
            attributes_initialized = True
            self._check(self.kernel32.UpdateProcThreadAttribute(
                startup.lpAttributeList, 0, _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                ctypes.cast(handle_array, ctypes.c_void_p),
                ctypes.sizeof(handle_array), None, None),
                "UpdateProcThreadAttribute(HANDLE_LIST)")
            flags = (_CREATE_SUSPENDED | _CREATE_NEW_PROCESS_GROUP |
                     _CREATE_UNICODE_ENVIRONMENT | _EXTENDED_STARTUPINFO_PRESENT |
                     _DETACHED_PROCESS)
            self._check(self.kernel32.CreateProcessW(
                executable, mutable_command, None, None,
                bool(all_handles), flags,
                ctypes.cast(environment, ctypes.c_void_p) if environment else None,
                cwd,
                ctypes.cast(ctypes.byref(startup), ctypes.POINTER(_STARTUPINFO)),
                ctypes.byref(info)),
                "CreateProcessW(%s)" % executable)
            try:
                creation_time = self.process_creation_time(info.hProcess)
            except Exception:
                self.kernel32.TerminateProcess(info.hProcess, 1)
                self.kernel32.WaitForSingleObject(info.hProcess, 5000)
                self.close_handle(info.hThread)
                self.close_handle(info.hProcess)
                raise
            return info.hProcess, info.hThread, int(info.dwProcessId), creation_time
        finally:
            if attributes_initialized:
                self.kernel32.DeleteProcThreadAttributeList(startup.lpAttributeList)
            for handle in handles:
                self.close_handle(handle)

    def process_creation_time(self, process):
        created, exited, kernel, user = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
        self._check(self.kernel32.GetProcessTimes(
            process, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user)), "GetProcessTimes")
        ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        return ticks / 10_000_000 - 11644473600

    def process_sid(self, process):
        token = wintypes.HANDLE()
        self._check(self.advapi32.OpenProcessToken(
            process, 0x0008, ctypes.byref(token)), "OpenProcessToken")
        try:
            size = wintypes.DWORD()
            self.advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            if not size.value:
                self._check(False, "GetTokenInformation(TokenUser size)")
            buffer = ctypes.create_string_buffer(size.value)
            self._check(self.advapi32.GetTokenInformation(
                token, 1, buffer, size, ctypes.byref(size)),
                "GetTokenInformation(TokenUser)")

            class _SID_AND_ATTRIBUTES(ctypes.Structure):
                _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

            class _TOKEN_USER(ctypes.Structure):
                _fields_ = [("User", _SID_AND_ATTRIBUTES)]

            sid_ptr = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_USER)).contents.User.Sid
            string_sid = wintypes.LPWSTR()
            self._check(self.advapi32.ConvertSidToStringSidW(
                sid_ptr, ctypes.byref(string_sid)), "ConvertSidToStringSidW")
            try:
                return string_sid.value
            finally:
                self.kernel32.LocalFree(ctypes.cast(string_sid, ctypes.c_void_p))
        finally:
            self.kernel32.CloseHandle(token)

    def resume(self, thread):
        result = self.kernel32.ResumeThread(thread)
        if result == 0xFFFFFFFF:
            self._check(False, "ResumeThread")
        if result == 0:
            raise OSError("ResumeThread reported that the process was not suspended")

    def open_process(self, pid):
        # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE | PROCESS_TERMINATE
        access = 0x1000 | 0x00100000 | 0x0001
        handle = self.kernel32.OpenProcess(access, False, int(pid))
        if not handle:
            error = ctypes.get_last_error()
            if error in (87, 1168):  # invalid parameter / not found
                return None
            raise OSError(error, "OpenProcess(%d): %s" %
                          (pid, ctypes.FormatError(error).strip()))
        return handle

    def poll_process(self, process):
        code = wintypes.DWORD()
        self._check(self.kernel32.GetExitCodeProcess(process, ctypes.byref(code)),
                    "GetExitCodeProcess")
        return None if code.value == _STILL_ACTIVE else int(code.value)

    def wait_process(self, process, timeout):
        if timeout is None:
            milliseconds = _INFINITE
        else:
            milliseconds = max(0, min(0xFFFFFFFE, int(timeout * 1000)))
        result = self.kernel32.WaitForSingleObject(process, milliseconds)
        if result == _WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired("managed process", timeout)
        if result != _WAIT_OBJECT_0:
            self._check(False, "WaitForSingleObject(process)")
        return self.poll_process(process)

    def query_job_members(self, job):
        capacity = 64
        pid_size = ctypes.sizeof(ctypes.c_size_t)
        header_size = 2 * ctypes.sizeof(wintypes.DWORD)
        while capacity <= 1_048_576:
            size = header_size + capacity * pid_size
            buffer = ctypes.create_string_buffer(size)
            returned = wintypes.DWORD()
            ok = self.kernel32.QueryInformationJobObject(
                job, 3, buffer, size, ctypes.byref(returned))
            assigned = int(ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))[0])
            count = int(ctypes.cast(
                ctypes.byref(buffer, ctypes.sizeof(wintypes.DWORD)),
                ctypes.POINTER(wintypes.DWORD))[0])
            if ok and count <= capacity:
                pid_array = (ctypes.c_size_t * count).from_buffer(buffer, header_size)
                return [int(pid) for pid in pid_array if pid]
            error = ctypes.get_last_error()
            if error not in (122, 234) and not ok:
                raise OSError(error, "QueryInformationJobObject: %s" %
                              ctypes.FormatError(error).strip())
            capacity = max(capacity * 2, assigned + 1, count + 1)
        raise OSError("Job Object contains too many processes to enumerate")

    def terminate_job(self, job, exit_code=1):
        self._check(self.kernel32.TerminateJobObject(job, int(exit_code)),
                    "TerminateJobObject")

    def terminate_process(self, process, exit_code=1):
        if not self.kernel32.TerminateProcess(process, int(exit_code)):
            error = ctypes.get_last_error()
            if error == 5:
                try:
                    if self.poll_process(process) is not None:
                        return
                except OSError:
                    pass
            raise OSError(error, "TerminateProcess: %s" %
                          ctypes.FormatError(error).strip())

    def close_handle(self, handle):
        if handle:
            self.kernel32.CloseHandle(handle)


class JobAnchorCleanupError(OSError):
    """An empty Job Object could not release its keeper process.

    ``managed_process`` retains the Job Object and exact keeper process
    handles so a caller can retry cleanup instead of losing the only safe
    references after a timeout.
    """

    def __init__(self, message, managed_process):
        super().__init__(message)
        self.managed_process = managed_process


class ManagedProcess:
    """A subprocess-like handle plus its Job Object process boundary."""

    def __init__(self, api, job_handle, run_id, job_name, pid,
                 process_handle=None, thread_handle=None, creation_time=None,
                 root_exit_code=None, anchor_pid=None, anchor_create_time=None,
                 anchor_handle=None):
        self._api = api
        self._job_handle = job_handle
        self._process_handle = process_handle
        self._thread_handle = thread_handle
        self.run_id = run_id
        self.job_name = job_name
        self.pid = int(pid)
        self.creation_time = creation_time
        self.anchor_pid = int(anchor_pid) if anchor_pid is not None else None
        self.anchor_create_time = anchor_create_time
        self._anchor_handle = anchor_handle
        self._root_exit_code = root_exit_code
        self._closed = False
        self._lock = threading.RLock()
        self._wait_done = threading.Condition(self._lock)
        # Native operations that use one of the owned handles must finish
        # before close() releases it.  Keep this count separate from the
        # instance lock so terminate() can still run while a wait or poll is
        # blocked in the OS API.
        self._active_handle_ops = 0

    def poll(self):
        # Capture the process HANDLE and register the native operation while
        # holding the lock, then release it so terminate() remains available.
        # close() waits for the operation count before releasing the handle.
        with self._lock:
            process_handle = self._process_handle
            if not process_handle:
                return self._root_exit_code
            self._active_handle_ops += 1
        try:
            code = self._api.poll_process(process_handle)
            if code is not None:
                with self._lock:
                    self._root_exit_code = code
            return code
        finally:
            with self._lock:
                self._active_handle_ops -= 1
                if not self._active_handle_ops:
                    self._wait_done.notify_all()

    def wait(self, timeout=None):
        """Wait for the root process; after a console restart, wait for job drain.

        The original root may have exited before the console restarted while a
        child remains in the job.  In that case Windows no longer exposes its
        exit code, so this waits until the remaining job processes exit and
        returns ``None``.
        """
        # ``close()`` must not invalidate a native process/job handle while
        # WaitForSingleObject is using it.  Register the wait while holding
        # the instance lock, then release it so terminate() can still stop a
        # blocked process.  close() waits for this reference to finish.
        with self._lock:
            if self._closed:
                raise OSError("Managed process handle is closed")
            self._active_handle_ops += 1
            process_handle = self._process_handle
        try:
            if process_handle:
                code = self._api.wait_process(process_handle, timeout)
                if code is not None:
                    with self._lock:
                        self._root_exit_code = code
                return code
            deadline = (None if timeout is None else
                        time.monotonic() + max(0, timeout))
            while self.members():
                if deadline is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired("managed job", timeout)
                time.sleep(0.05)
            with self._lock:
                return self._root_exit_code
        finally:
            with self._lock:
                self._active_handle_ops -= 1
                if not self._active_handle_ops:
                    self._wait_done.notify_all()

    def members(self):
        with self._lock:
            if not self._job_handle:
                return []
            members = self._api.query_job_members(self._job_handle)
            if not members and not self._stop_anchor():
                raise OSError("无法清理 Job Object 保活进程")
            return members

    def wait_for_empty(self, timeout=None):
        """Wait until every application process in the Job Object has exited."""
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while self.members():
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("managed job", timeout)
            time.sleep(0.05)
        return None

    def _stop_anchor(self):
        with self._lock:
            if not self._anchor_handle:
                return True
            try:
                # The handle is bound to the exact process opened at launch or
                # reopen; never re-resolve its PID, so PID reuse cannot redirect.
                if self._api.poll_process(self._anchor_handle) is not None:
                    return True
                try:
                    self._api.terminate_process(self._anchor_handle, 0)
                except OSError:
                    # Process exit can race the poll. Treat it as success only
                    # if the original process handle is now signaled.
                    if self._api.poll_process(self._anchor_handle) is not None:
                        return True
                    raise
                try:
                    self._api.wait_process(self._anchor_handle, 2.0)
                except subprocess.TimeoutExpired:
                    # A timeout is only a failed cleanup if the exact process
                    # remains alive; it may have exited as the wait expired.
                    pass
                return self._api.poll_process(self._anchor_handle) is not None
            except OSError:
                return False

    def terminate(self, force=False, timeout=5.0):
        """Stop the job as a unit; graceful stop falls back to job termination.

        Returns the established ``(ok, error)`` pair used by ``sysops``.  A
        graceful request uses taskkill's window-close path for each job member,
        then terminates only this Job Object if processes remain after timeout.
        """
        with self._lock:
            return self._terminate_locked(force=force, timeout=timeout)

    def _terminate_locked(self, force=False, timeout=5.0):
        if self._closed or not self._job_handle:
            return False, "Job Object handle is closed"
        try:
            if force:
                self._api.terminate_job(self._job_handle, 1)
                if not self._stop_anchor():
                    return False, "无法清理 Job Object 保活进程"
                return True, None
            members = self.members()
            for pid in members:
                try:
                    subprocess.run(["taskkill", "/PID", str(pid)],
                                   capture_output=True, timeout=3,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except (OSError, subprocess.TimeoutExpired):
                    # Keep waiting; the job boundary still makes the forced
                    # fallback safe and prevents touching another service.
                    pass
            deadline = time.monotonic() + max(0.0, float(timeout))
            while self.members():
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            if self.members():
                self._api.terminate_job(self._job_handle, 1)
            # The keeper lives outside the service Job Object so it does not
            # make a naturally exited application look alive.
            if not self._stop_anchor():
                return False, "无法在超时内清理 Job Object 保活进程"
            return True, None
        except OSError as exc:
            return False, str(exc)

    def close(self):
        """Release handles without terminating the job or its processes."""
        with self._lock:
            if self._closed:
                return
            while self._active_handle_ops:
                self._wait_done.wait()
            self._closed = True
            if self._thread_handle:
                self._api.close_handle(self._thread_handle)
                self._thread_handle = None
            if self._process_handle:
                self._api.close_handle(self._process_handle)
                self._process_handle = None
            if self._anchor_handle:
                self._api.close_handle(self._anchor_handle)
                self._anchor_handle = None
            if self._job_handle:
                self._api.close_handle(self._job_handle)
                self._job_handle = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __del__(self):  # pragma: no cover - best-effort cleanup at interpreter exit
        try:
            self.close()
        except Exception:
            pass


class WindowsRuntimeManager:
    """Create and reconnect Job Object-backed managed process instances."""

    def __init__(self, api=None):
        self._api = api or _NativeApi()

    def _create_keeper(self, job_handle, cwd=None):
        """Start an unassigned pythonw waiter that holds a query handle.

        Windows removes a named Job Object from the object namespace when the
        last handle closes, even if assigned applications remain alive.  A
        dedicated keeper makes the name reopenable after the console exits.
        The keeper is outside the managed job, so it cannot make an exited app
        appear alive; its PID and creation time are persisted for safe cleanup.
        """
        duplicate = self._api.duplicate_job_anchor(job_handle)
        runtime = os.path.abspath(sys.executable)
        if os.path.basename(runtime).casefold() != "pythonw.exe":
            sibling = os.path.join(os.path.dirname(runtime), "pythonw.exe")
            if os.path.isfile(sibling):
                runtime = sibling
            else:
                # A minimal embedded distribution may only ship python.exe;
                # DETACHED_PROCESS still keeps that fallback windowless.
                runtime = runtime if os.path.isfile(runtime) else ""
        if not runtime or not os.path.isfile(runtime):
            self._api.close_handle(duplicate)
            raise FileNotFoundError(
                "pythonw.exe is required to keep the named Job Object open")
        # Do not use `cmd.exe` plus `timeout.exe`: that creates children which
        # can outlive the keeper and retain inherited Job handles.  A detached
        # pythonw waiter creates no console host or subprocess and can be
        # stopped through its exact process handle.
        wait_script = "import time\nwhile True: time.sleep(3600)"
        command_line = subprocess.list2cmdline([
            runtime, "-c", wait_script])
        keeper_handle = thread_handle = None
        try:
            keeper_handle, thread_handle, pid, created = \
                self._api.create_suspended_process(
                    runtime, command_line, None, None,
                    subprocess.DEVNULL, subprocess.STDOUT,
                    inherited_handles=(duplicate,))
            self._api.resume(thread_handle)
            self._api.close_handle(thread_handle)
            thread_handle = None
            return keeper_handle, pid, created
        except Exception:
            if keeper_handle:
                try:
                    self._api.terminate_process(keeper_handle, 1)
                except Exception:
                    pass
                self._api.close_handle(keeper_handle)
            if thread_handle:
                self._api.close_handle(thread_handle)
            raise
        finally:
            self._api.close_handle(duplicate)

    def launch(self, executable=None, args=(), cwd=None, env=None, run_id=None,
               stdout=None, stderr=subprocess.STDOUT, mode="exec", sid=None,
               command=None):
        if sys.platform != "win32" and isinstance(self._api, _NativeApi):
            raise OSError("managed process launch is available only on Windows")
        if run_id is None:
            run_id = uuid.uuid4().hex
        if sid is None:
            # Resolve TokenUser SID without importing sysops (which intentionally
            # exits on non-Windows); the server can pass its already-read SID.
            sid = current_user_sid()
        name = job_name_for(run_id, sid)
        if command is not None:
            if mode != "legacy-shell":
                raise ValueError("command is accepted only for legacy-shell mode")
            if executable not in (None, ""):
                raise ValueError("pass either command or executable, not both")
            executable = command
        if executable is None:
            raise ValueError("executable or legacy command is required")
        if isinstance(args, (str, bytes, bytearray)):
            raise TypeError("args must be a sequence of strings")
        args = tuple(_validate_text(arg, "argument") for arg in args)
        if cwd is not None:
            cwd = os.path.abspath(_validate_text(cwd, "cwd"))
            if not os.path.isdir(cwd):
                raise NotADirectoryError("working directory does not exist: %s" % cwd)
        app_name, command_line = _command_for(mode, executable, args, cwd, env)

        job = self._api.create_job(name, sid)
        process = thread = keeper_handle = None
        try:
            keeper_handle, keeper_pid, keeper_created = self._create_keeper(job, cwd)
            process, thread, pid, created = self._api.create_suspended_process(
                app_name, command_line, cwd, env, stdout, stderr)
            # Assignment is intentionally before ResumeThread.  There is no
            # fallback to an unmanaged child if Windows refuses the assignment.
            self._api.assign_process(job, process)
            self._api.resume(thread)
            self._api.close_handle(thread)
            thread = None
            return ManagedProcess(self._api, job, run_id, name, pid,
                                  process, None, created,
                                  anchor_pid=keeper_pid,
                                  anchor_create_time=keeper_created,
                                  anchor_handle=keeper_handle)
        except Exception:
            try:
                self._api.terminate_job(job, 1)
            except Exception:
                pass
            if process:
                try:
                    self._api.terminate_process(process, 1)
                    self._api.wait_process(process, 5)
                except Exception:
                    pass
            if thread:
                self._api.close_handle(thread)
            if process:
                self._api.close_handle(process)
            if keeper_handle:
                try:
                    self._api.terminate_process(keeper_handle, 1)
                    self._api.wait_process(keeper_handle, 2)
                except Exception:
                    pass
                self._api.close_handle(keeper_handle)
            self._api.close_handle(job)
            raise

    def reopen(self, run_id, job_name=None, root_pid=None, root_create_time=None,
               sid=None, anchor_pid=None, anchor_create_time=None):
        if sid is None:
            sid = current_user_sid()
        if anchor_pid is not None and anchor_create_time is None:
            raise ValueError(
                "persisted Job Object anchor creation time is required")
        expected_name = job_name_for(run_id, sid)
        if job_name is not None and job_name != expected_name:
            raise ValueError("persisted Job Object name does not match run_id/current user")
        job_name = expected_name
        job = self._api.open_job(job_name)
        process = anchor_handle = None
        try:
            members = self._api.query_job_members(job)
            if anchor_pid is not None:
                anchor_handle = self._api.open_process(int(anchor_pid))
                if anchor_handle:
                    try:
                        valid_anchor = (
                            anchor_create_time is not None
                            and abs(self._api.process_creation_time(anchor_handle) -
                                    float(anchor_create_time)) <= 0.0001
                            and self._api.process_sid(anchor_handle) == sid)
                    except OSError:
                        valid_anchor = False
                    if not valid_anchor:
                        self._api.close_handle(anchor_handle)
                        anchor_handle = None
            if not members:
                if anchor_handle:
                    # Transfer ownership before cleanup so a failed wait can
                    # carry the exact handles back to the caller for retry.
                    cleanup = ManagedProcess(
                        self._api, job, run_id, job_name,
                        int(root_pid or anchor_pid or 1),
                        anchor_pid=anchor_pid,
                        anchor_create_time=anchor_create_time,
                        anchor_handle=anchor_handle)
                    job = None
                    anchor_handle = None
                    if not cleanup._stop_anchor():
                        raise JobAnchorCleanupError(
                            "无法清理空 Job Object 的保活进程", cleanup)
                    cleanup.close()
                else:
                    self._api.close_handle(job)
                    job = None
                return None
            if not anchor_handle:
                anchor_handle, anchor_pid, anchor_create_time = \
                    self._create_keeper(job)
            if root_pid is not None:
                process = self._api.open_process(int(root_pid))
                if process:
                    actual_create_time = self._api.process_creation_time(process)
                    if (root_create_time is not None and
                            abs(actual_create_time - float(root_create_time)) > 0.0001):
                        self._api.close_handle(process)
                        process = None
                    elif int(root_pid) not in members:
                        # A live PID is not a member merely because it matches
                        # the saved number; discard it to prevent PID reuse.
                        self._api.close_handle(process)
                        process = None
            return ManagedProcess(self._api, job, run_id, job_name,
                                  int(root_pid or members[0]),
                                  process, None, root_create_time,
                                  anchor_pid=anchor_pid,
                                  anchor_create_time=anchor_create_time,
                                  anchor_handle=anchor_handle)
        except Exception:
            if process:
                self._api.close_handle(process)
            if anchor_handle:
                self._api.close_handle(anchor_handle)
            if job:
                self._api.close_handle(job)
            raise


def current_user_sid() -> str:
    """Read the current process TokenUser SID using only Win32 APIs."""
    if sys.platform != "win32":
        raise OSError("a Windows user SID is available only on Windows")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    process = kernel32.GetCurrentProcess()
    token = wintypes.HANDLE()
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    if not advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
        error = ctypes.get_last_error()
        raise OSError(error, "OpenProcessToken: %s" % ctypes.FormatError(error).strip())
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                  ctypes.c_void_p, wintypes.DWORD,
                                                  ctypes.POINTER(wintypes.DWORD)]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            error = ctypes.get_last_error()
            raise OSError(error, "GetTokenInformation: %s" % ctypes.FormatError(error).strip())
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            error = ctypes.get_last_error()
            raise OSError(error, "GetTokenInformation: %s" % ctypes.FormatError(error).strip())

        class _SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

        class _TOKEN_USER(ctypes.Structure):
            _fields_ = [("User", _SID_AND_ATTRIBUTES)]

        sid_ptr = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_USER)).contents.User.Sid
        string_sid = wintypes.LPWSTR()
        advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                                     ctypes.POINTER(wintypes.LPWSTR)]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(string_sid)):
            error = ctypes.get_last_error()
            raise OSError(error, "ConvertSidToStringSidW: %s" %
                          ctypes.FormatError(error).strip())
        try:
            return string_sid.value
        finally:
            kernel32.LocalFree(ctypes.cast(string_sid, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


def launch(executable=None, args=(), cwd=None, env=None, run_id=None,
           stdout=None, stderr=subprocess.STDOUT, mode="exec", sid=None,
           command=None):
    """Convenience function used by the server's launch lifecycle."""
    return WindowsRuntimeManager().launch(
        executable, args, cwd, env, run_id, stdout, stderr, mode, sid,
        command=command)


def launch_spec(spec, run_id=None, stdout=None, stderr=subprocess.STDOUT, sid=None):
    """Launch a canonical LaunchSpec dictionary produced by launch_spec.py."""
    if not isinstance(spec, Mapping):
        raise TypeError("launch spec must be a mapping")
    mode = spec.get("mode", "exec")
    executable = spec.get("executable")
    command = spec.get("command")
    if mode == "legacy-shell" and command is not None:
        executable = None
    return launch(
        executable=executable, command=command, args=spec.get("args") or (),
        cwd=spec.get("cwd"), env=spec.get("env"), run_id=run_id,
        stdout=stdout, stderr=stderr, mode=mode, sid=sid)


def reopen(run_id, job_name=None, root_pid=None, root_create_time=None, sid=None,
           anchor_pid=None, anchor_create_time=None):
    """Convenience function to reconnect a saved managed run after restart."""
    return WindowsRuntimeManager().reopen(
        run_id, job_name, root_pid, root_create_time, sid,
        anchor_pid, anchor_create_time)
