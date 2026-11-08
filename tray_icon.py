import ctypes
import threading
import time

import win32api
import win32con
import win32gui

WM_TRAY = win32con.WM_USER + 20
WM_APP_QUIT = win32con.WM_APP + 1

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800

TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002

CMD_OPEN = 1001
CMD_RESTART = 1002
CMD_EXIT = 1003

INTEGRITY_NAMES = {
    0x0000: "UNTRUSTED",
    0x1000: "LOW",
    0x2000: "MEDIUM",
    0x3000: "HIGH",
    0x4000: "SYSTEM",
}


class TrayError(Exception):
    def __init__(self, cause):
        self.cause = cause
        lvl = current_integrity()
        self.integrity = lvl
        name = INTEGRITY_NAMES.get(lvl, str(lvl))
        msg = f"托盘图标注册失败: {cause}"
        if cause.__class__.__name__ == "error" and getattr(cause, "winerror", None) == 5:
            msg += (
                f"\n当前进程完整性级别: {name}"
                "\nWindows 不允许低完整性进程注册托盘图标，请以管理员身份运行。"
            )
        super().__init__(msg)


def current_integrity():
    try:
        k = ctypes.WinDLL("kernel32")
        adv = ctypes.WinDLL("advapi32")

        k.GetCurrentProcess.restype = ctypes.c_void_p
        h = k.GetCurrentProcess()

        adv.OpenProcessToken.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        adv.OpenProcessToken.restype = ctypes.c_int
        tok = ctypes.c_void_p()
        if not adv.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):
            return None

        adv.GetTokenInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        adv.GetTokenInformation.restype = ctypes.c_int

        size = ctypes.c_ulong(0)
        adv.GetTokenInformation(tok, 25, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value or 256)
        got = ctypes.c_ulong(0)
        if not adv.GetTokenInformation(tok, 25, buf, len(buf), ctypes.byref(got)):
            return None

        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        adv.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        adv.GetSidSubAuthority.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        adv.GetSidSubAuthority.restype = ctypes.POINTER(ctypes.c_ulong)

        count = adv.GetSidSubAuthorityCount(sid)[0]
        value = adv.GetSidSubAuthority(sid, count - 1)[0]
        return value
    except Exception:
        return None


class TrayIcon:
    def __init__(self, tooltip, icon_path, on_open, on_restart, on_exit):
        self._tooltip = tooltip
        self._icon_path = icon_path
        self._on_open = on_open
        self._on_restart = on_restart
        self._on_exit = on_exit

        self._hwnd = None
        self._hicon = None
        self._ready = threading.Event()
        self._error = None
        self._thread = None
        self._stopping = False

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=10)
        if self._error:
            raise TrayError(self._error)

    def _run(self):
        try:
            hinst = win32api.GetModuleHandle(None)
            wc = win32gui.WNDCLASS()
            wc.hInstance = hinst
            wc.lpszClassName = f"ExpreloadTray{id(self)}"
            wc.lpfnWndProc = self._wndproc
            win32gui.RegisterClass(wc)

            self._hwnd = win32gui.CreateWindow(
                wc.lpszClassName,
                "Expreload Tray",
                win32con.WS_OVERLAPPED,
                0,
                0,
                0,
                0,
                0,
                0,
                hinst,
                None,
            )

            if self._icon_path:
                try:
                    self._hicon = win32gui.LoadImage(
                        0,
                        self._icon_path,
                        win32con.IMAGE_ICON,
                        0,
                        0,
                        win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE,
                    )
                except Exception as e:
                    print(f"[Expreload] 加载托盘图标失败: {e}", flush=True)

            try:
                win32gui.Shell_NotifyIcon(NIM_ADD, self._nid())
            except Exception as e:
                self._error = e
                print(f"[Expreload] 托盘注册失败: {e}", flush=True)
                self._ready.set()
                return

            self._ready.set()
            win32gui.PumpMessages()

        except Exception as e:
            self._error = e
            print(f"[Expreload] 托盘线程异常: {e}", flush=True)
            import traceback

            traceback.print_exc()
            self._ready.set()

    def _nid(self):
        return (
            self._hwnd,
            0,
            NIF_ICON | NIF_MESSAGE | NIF_TIP,
            WM_TRAY,
            self._hicon,
            self._tooltip,
        )

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            event = lparam & 0xFFFF
            if event in (win32con.WM_LBUTTONUP, win32con.WM_LBUTTONDBLCLK):
                self._invoke(self._on_open)
                return 0
            if event in (win32con.WM_RBUTTONUP, win32con.WM_CONTEXTMENU):
                self._popup(hwnd)
                return 0

        if msg == WM_APP_QUIT:
            win32gui.PostQuitMessage(0)
            return 0

        if msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0

        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _invoke(self, fn):
        try:
            fn()
        except Exception as e:
            print(f"[Expreload] 托盘动作失败: {e}", flush=True)

    def _popup(self, hwnd):
        menu = win32gui.CreatePopupMenu()
        try:
            win32gui.AppendMenu(menu, MF_STRING, CMD_OPEN, "打开主界面")
            win32gui.AppendMenu(menu, MF_STRING, CMD_RESTART, "重启 Explorer")
            win32gui.AppendMenu(menu, MF_SEPARATOR, 0, "")
            win32gui.AppendMenu(menu, MF_STRING, CMD_EXIT, "退出")

            pos = win32gui.GetCursorPos()

            # TrackPopupMenu requires the owner window to be foreground.
            # A hidden tray window cannot become foreground directly, so
            # briefly show it, force foreground, then hide again.
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
            try:
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:
                pass

            cmd = win32gui.TrackPopupMenu(
                menu,
                TPM_RETURNCMD | TPM_RIGHTBUTTON,
                pos[0],
                pos[1],
                0,
                hwnd,
                None,
            )

            win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)

            if cmd == CMD_OPEN:
                self._invoke(self._on_open)
            elif cmd == CMD_RESTART:
                self._invoke(self._on_restart)
            elif cmd == CMD_EXIT:
                self._invoke(self._on_exit)
        finally:
            win32gui.DestroyMenu(menu)

    def stop(self):
        if self._stopping:
            return
        self._stopping = True
        try:
            if self._hwnd:
                win32gui.Shell_NotifyIcon(NIM_DELETE, self._nid())
                win32gui.PostMessage(self._hwnd, WM_APP_QUIT, 0, 0)
        except Exception:
            pass
        if self._hicon:
            try:
                win32gui.DestroyIcon(self._hicon)
            except Exception:
                pass
            self._hicon = None
