import os
import sys
import time
import threading
import subprocess
import psutil
import keyboard
from PIL import Image
import pystray
from pystray import MenuItem, Menu
from typing_extensions import override

from winrt.microsoft.ui.xaml import (
    Application,
    ApplicationInitializationCallbackParams,
    LaunchActivatedEventArgs,
    Window,
)
from winrt.microsoft.ui.xaml.controls import (
    Button,
    StackPanel,
    TextBlock,
    XamlControlsResources,
)
from winrt.microsoft.ui.xaml.markup import (
    IXamlMetadataProvider,
    IXamlType,
    XmlnsDefinition,
)
from winrt.microsoft.ui.xaml.xamltypeinfo import XamlControlsXamlMetaDataProvider
from winrt.microsoft.windows.applicationmodel.dynamicdependency.bootstrap import (
    InitializeOptions,
    initialize,
)
from winrt.runtime import ApartmentType, init_apartment
from winrt.system import Array, box_string
from winrt.windows.ui.xaml.interop import TypeKind, TypeName

EXPLORER_NAME = "explorer.exe"
MONITOR_INTERVAL = 2
MISSING_THRESHOLD = 10
ICON_PATH = "expreload.ico"


class ExplorerMonitor:
    def __init__(self):
        self.missing_since = None
        self.running = True
        self._lock = threading.Lock()

    def is_explorer_running(self) -> bool:
        for proc in psutil.process_iter(['name']):
            try:
                if proc.info['name'] and proc.info['name'].lower() == EXPLORER_NAME:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False

    def monitor_loop(self):
        while self.running:
            if self.is_explorer_running():
                with self._lock:
                    self.missing_since = None
            else:
                with self._lock:
                    if self.missing_since is None:
                        self.missing_since = time.time()
                    elif time.time() - self.missing_since >= MISSING_THRESHOLD:
                        self.missing_since = None
                        self.ask_and_restart()
            time.sleep(MONITOR_INTERVAL)

    def ask_and_restart(self):
        import ctypes
        result = ctypes.windll.user32.MessageBoxW(
            0,
            "Explorer 已停止运行超过 10 秒。\n是否立即重启 Explorer？",
            "Expreload 提示",
            0x00000004 | 0x00000020,
        )
        if result == 6:
            restart_explorer()

    def stop(self):
        self.running = False


def restart_explorer():
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", EXPLORER_NAME],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        time.sleep(0.5)
        subprocess.Popen(["explorer.exe"], creationflags=subprocess.CREATE_NO_WINDOW)
        print("[Expreload] Explorer 已重启")
    except Exception as e:
        print(f"[Expreload] 重启失败: {e}")


def register_hotkey():
    try:
        keyboard.add_hotkey("ctrl+shift+e", restart_explorer)
        print("[Expreload] 快捷键 Ctrl+Shift+E 已注册")
    except Exception as e:
        print(f"[Expreload] 快捷键注册失败: {e}")


def create_tray_icon(app, monitor):
    image = Image.new("RGB", (64, 64), color=(0, 120, 215))
    if os.path.exists(ICON_PATH):
        image = Image.open(ICON_PATH)

    def on_open(icon, item):
        if app._window:
            app._window.activate()

    def on_exit(icon, item):
        monitor.stop()
        keyboard.unhook_all_hotkeys()
        icon.stop()
        os._exit(0)

    menu = Menu(
        MenuItem("打开主界面", on_open, default=True),
        MenuItem("重启 Explorer", lambda icon, item: restart_explorer()),
        Menu.SEPARATOR,
        MenuItem("退出", on_exit),
    )
    return pystray.Icon("Expreload", image, "Expreload - Explorer 守护", menu)


class ExpreloadApp(Application, IXamlMetadataProvider):
    def __init__(self):
        super().__init__()
        self._provider = XamlControlsXamlMetaDataProvider()
        self._window = None
        self._monitor = ExplorerMonitor()
        self._tray_icon = None

    @override
    def _on_launched(self, args: LaunchActivatedEventArgs) -> None:
        threading.Thread(target=self._monitor.monitor_loop, daemon=True).start()
        register_hotkey()

        resources = XamlControlsResources()
        self.resources.merged_dictionaries.append(resources)

        self._window = Window()
        self._window.title = "Expreload"

        root = StackPanel()
        root.spacing = 10

        self._status_text = TextBlock()
        self._status_text.text = "Explorer 状态：监控中..."
        root.children.append(self._status_text)

        btn_restart = Button()
        btn_restart.content = box_string("重启 Explorer")
        btn_restart.add_click(lambda s, e: restart_explorer())
        root.children.append(btn_restart)

        self._window.content = root
        self._window.add_closed(self._on_window_closed)
        self._window.activate()

        self._tray_icon = create_tray_icon(self, self._monitor)
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def _on_window_closed(self, sender, args):
        print("[Expreload] 窗口已关闭，程序继续在后台运行")

    @override
    def get_xaml_type(self, type: TypeName | tuple[str, TypeKind]) -> IXamlType:
        return self._provider.get_xaml_type(type)

    @override
    def get_xaml_type_by_full_name(self, full_name: str) -> IXamlType:
        return self._provider.get_xaml_type_by_full_name(full_name)

    @override
    def get_xmlns_definitions(self) -> Array[XmlnsDefinition]:
        return self._provider.get_xmlns_definitions()


def init(params: ApplicationInitializationCallbackParams) -> None:
    ExpreloadApp()


def main():
    init_apartment(ApartmentType.SINGLE_THREADED)
    with initialize(options=InitializeOptions.ON_NO_MATCH_SHOW_UI):
        Application.start(init)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()