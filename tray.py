# -*- coding: utf-8 -*-
"""Windows 系统托盘图标（纯 ctypes + 标准库，零第三方依赖）。

仅 Windows 且正常运行总控台时由 server 创建（测试与其它平台不创建）。
能力：tooltip 状态（端口/运行中）、左键打开控制台、右键菜单
（打开控制台 / 重启总控台 / 停止总控台 / 退出）。

图标：内嵌 PNG 解码（zlib 标准库）→ CreateIcon 直接生成 HICON，
不依赖任何外部图标文件。PNG 支持 RGBA/RGB 的 8 位色深与四种 filter。
"""
import ctypes
import ctypes.wintypes as wt
import os
import struct
import threading
import zlib

WM_APP = 0x8000
WM_TRAY = WM_APP + 1          # 托盘回调消息（wParam=事件, lParam=命令）
WM_TRAY_CMD = WM_APP + 2      # 主线程 → 托盘线程命令

# 托盘回调事件
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
# 命令 ID（菜单与消息共用）
CMD_OPEN = 1
CMD_RESTART = 2
CMD_STOP = 3
CMD_EXIT = 4
CMD_UPDATE_TIP = 5

# NOTIFYICONDATA 标志
NIF_MESSAGE = 0x1
NIF_ICON = 0x2
NIF_TIP = 0x4
NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIM_SETVERSION = 4

# 菜单标志
MF_STRING = 0x0
MF_SEPARATOR = 0x800
TPM_RIGHTBUTTON = 0x2
TPM_RETURNCMD = 0x100

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 64 位下必须显式声明 argtypes，否则指针/句柄参数按 32 位截断。
user32.GetMessageW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.UINT]
user32.GetMessageW.restype = wt.BOOL
user32.TranslateMessage.argtypes = [ctypes.c_void_p]
user32.TranslateMessage.restype = wt.BOOL
user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
user32.DispatchMessageW.restype = wt.LPARAM
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = wt.LPARAM
user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.RegisterClassW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.DestroyWindow.argtypes = [wt.HWND]
user32.DestroyWindow.restype = wt.BOOL
user32.CreatePopupMenu.argtypes = []
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
user32.AppendMenuW.restype = wt.BOOL
user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wt.HWND,
                                  ctypes.c_void_p]
user32.TrackPopupMenu.restype = wt.BOOL
user32.GetCursorPos.argtypes = [ctypes.c_void_p]
user32.GetCursorPos.restype = wt.BOOL
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.DestroyMenu.restype = wt.BOOL
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.c_void_p]
shell32.Shell_NotifyIconW.restype = wt.BOOL

try:
    LR_LOADFROMFILE = 0x0010
    IMAGE_ICON = 1
    CreateIcon = user32.CreateIcon
    CreateIcon.restype = wt.HICON
    CreateIcon.argtypes = [wt.HINSTANCE, ctypes.c_int, ctypes.c_int,
                           ctypes.c_ubyte, ctypes.c_ubyte,
                           ctypes.c_void_p, ctypes.c_void_p]
    LoadImageW = user32.LoadImageW
    LoadImageW.restype = wt.HANDLE
    LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, ctypes.c_uint,
                           ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    DestroyIcon = user32.DestroyIcon
    DestroyIcon.argtypes = [wt.HICON]
except (AttributeError, OSError):
    CreateIcon = None
    LoadImageW = None
    DestroyIcon = None


class NOTIFYICONDATAW(ctypes.Structure):
    """Vista+ 版 NOTIFYICONDATAW（含 guidItem 的完整布局）。"""
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uTimeoutOrVersion", wt.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wt.HICON),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND), ("message", wt.UINT),
        ("wParam", wt.WPARAM), ("lParam", wt.LPARAM),
        ("time", wt.DWORD), ("pt", wt.POINT),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HANDLE),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


def _decode_png(data):
    """解码 PNG → (width, height, bgra 字节, 是否含 alpha)。

    支持 8 位色深的灰度/灰度+alpha/RGB/RGBA 与四种 filter，
    仅需标准库（zlib/struct）。
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG 文件")
    pos = 8
    width = height = bit_depth = color_type = 0
    idat = b""
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            (width, height, bit_depth, color_type, _comp, _filt,
             _interlace) = struct.unpack(">IIBBBBB", payload)
        elif tag == b"IDAT":
            idat += payload
        elif tag == b"IEND":
            break
        pos += 12 + length
    if bit_depth != 8:
        raise ValueError("仅支持 8 位色深, 实际 %d" % bit_depth)
    if color_type not in (0, 2, 4, 6):
        raise ValueError("不支持的色彩类型 %d" % color_type)
    ch = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    has_alpha = color_type in (4, 6)
    raw = zlib.decompress(idat)
    stride = width * ch
    rows = []
    prev = bytearray(stride)
    off = 0
    for _ in range(height):
        (filt,) = raw[off:off + 1]
        line = bytearray(raw[off + 1:off + 1 + stride])
        off += 1 + stride
        if filt == 1:
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 0xFF
        elif filt == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filt == 3:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif filt == 4:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                b = prev[i]
                c = prev[i - ch] if i >= ch else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        rows.append(bytes(line))
        prev = line
    # 转 BGRA（自底向上，CreateIcon 的 XOR mask 需要）
    out = bytearray()
    for row in reversed(rows):
        for i in range(width):
            if color_type == 6:
                r, g, b, a = row[i * 4:i * 4 + 4]
            elif color_type == 2:
                r, g, b = row[i * 3:i * 3 + 3]
                a = 255
            elif color_type == 4:
                g, a = row[i * 2:i * 2 + 2]
                r = g
                b = g
            else:
                g = row[i]
                r = g
                b = g
                a = 255
            out += bytes((b, g, r, a))
    return width, height, bytes(out), has_alpha


def _hicon_from_png(data):
    """PNG → HICON（CreateIcon, 32bpp XOR + 1bpp AND 掩码）。"""
    if CreateIcon is None:
        return None
    w, h, bgra, _alpha = _decode_png(data)
    # XOR mask: 32bpp BGRA（CreateIcon 需要 DIB 样式，与解码输出一致）
    xor_mask = (ctypes.c_ubyte * len(bgra)).from_buffer_copy(bgra)
    # AND mask: 1bpp, 每行补齐到 4 字节
    row_bytes = ((w + 31) // 32) * 4
    and_mask = (ctypes.c_ubyte * (row_bytes * h))()
    icon = CreateIcon(None, w, h, 1, 32, and_mask, xor_mask)
    if not icon:
        return None
    return icon


def _load_hicon_from_file(path):
    """备选：从 .ico 文件加载 HICON（若将来提供 ico 资源）。"""
    if LoadImageW is None:
        return None
    hicon = LoadImageW(None, path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
    return hicon if hicon else None


class TrayIcon:
    """总控台系统托盘图标。

    用法：start() 后由内部线程注册图标与消息循环；update_tip() 修改 tooltip；
    stop() 销毁图标并退出线程。回调 on_open/on_restart/on_stop 在主线程外
    （托盘线程）调用，调用方自行保证线程安全。
    """

    def __init__(self, tip, on_open, on_restart, on_stop, icon_png=None):
        self._tip = tip
        self._on_open = on_open
        self._on_restart = on_restart
        self._on_stop = on_stop
        self._icon_png = icon_png
        self._thread = None
        self._hwnd = None
        self._nid = None
        self._icon = None
        self._ready = threading.Event()

    # -------------------------------------------------- 对外 API
    def start(self):
        if os.name != "nt" or CreateIcon is None:
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._thread = threading.Thread(
            target=self._run, name="tray-icon", daemon=True)
        self._thread.start()
        return True

    def update_tip(self, text):
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_TRAY_CMD, CMD_UPDATE_TIP, 0)
            self._tip = text

    def stop(self):
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_TRAY_CMD, CMD_EXIT, 0)

    # -------------------------------------------------- 托盘线程
    def _run(self):
        try:
            self._icon = self._create_hicon()
            self._hwnd = self._create_message_window()
            if not self._hwnd or not self._icon:
                self._ready.set()
                return
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self._hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = self._icon
            nid.szTip = self._tip[:127]
            if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                self._ready.set()
                return
            self._nid = nid
            self._ready.set()
            self._message_loop()
        finally:
            self._cleanup()

    def _create_hicon(self):
        if self._icon_png:
            icon = _hicon_from_png(self._icon_png)
            if icon:
                return icon
        here = os.path.dirname(os.path.abspath(__file__))
        ico = os.path.join(here, "static", "favicon.ico")
        if os.path.isfile(ico):
            icon = _load_hicon_from_file(ico)
            if icon:
                return icon
        return None

    def _create_message_window(self):
        wc = WNDCLASSW()
        self._wndproc_ref = WNDPROC(self._wnd_proc)  # 防 GC 回收回调
        wc.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p)
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = "LocalOpsTrayWindow"
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            return None
        hwnd = user32.CreateWindowExW(
            0, wc.lpszClassName, "LocalOpsTray", 0, 0, 0, 0, 0,
            wt.HWND(-3), None, wc.hInstance, None)
        return hwnd if hwnd else None

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            self._on_tray_event(lparam, wparam)
            return 0
        if msg == WM_TRAY_CMD:
            self._on_command(wparam)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_tray_event(self, event, _pos):
        if event == WM_LBUTTONUP:
            self._safe(self._on_open)
        elif event == WM_RBUTTONUP:
            self._show_menu()

    def _show_menu(self):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, CMD_OPEN, "打开控制台")
        user32.AppendMenuW(menu, MF_STRING, CMD_RESTART, "重启总控台")
        user32.AppendMenuW(menu, MF_STRING, CMD_STOP, "停止总控台")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, CMD_STOP, "退出")
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self._hwnd)
        cmd = user32.TrackPopupMenu(
            menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
            pt.x, pt.y, 0, self._hwnd, None)
        user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if cmd:
            self._on_command(cmd)

    def _on_command(self, cmd):
        if cmd == CMD_OPEN:
            self._safe(self._on_open)
        elif cmd == CMD_RESTART:
            self._safe(self._on_restart)
        elif cmd == CMD_STOP:
            # 停止总控台:server 停止后由 finally 调用 stop() 退出托盘。
            self._safe(self._on_stop)
        elif cmd == CMD_UPDATE_TIP:
            if self._nid:
                self._nid.szTip = self._tip[:127]
                shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

    @staticmethod
    def _safe(fn):
        try:
            if fn:
                fn()
        except Exception:
            pass

    def _message_loop(self):
        msg = MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            if msg.message == WM_TRAY_CMD and msg.wParam == CMD_EXIT:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _cleanup(self):
        if self._nid:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._nid = None
        if self._hwnd:
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None
        if self._icon and DestroyIcon:
            DestroyIcon(self._icon)
            self._icon = None
