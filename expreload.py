import os
import time
import subprocess
import threading

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
    ElementTheme,
    GridLength,
    GridUnitType,
    Thickness,
    HorizontalAlignment,
    VerticalAlignment,
    Visibility,
    TextWrapping,
)
from winrt.microsoft.ui.xaml.controls import (
    Button,
    ColumnDefinition,
    ComboBox,
    ComboBoxItem,
    FontIcon,
    Grid,
    InfoBar,
    InfoBarSeverity,
    Orientation,
    ProgressRing,
    RowDefinition,
    StackPanel,
    TextBlock,
    ToggleSwitch,
    XamlControlsResources,
)
from winrt.microsoft.ui.xaml.markup import (
    IXamlMetadataProvider,
    IXamlType,
    XmlnsDefinition,
)
from winrt.microsoft.ui.xaml.media import MicaBackdrop
from winrt.microsoft.ui.xaml.xamltypeinfo import XamlControlsXamlMetaDataProvider
from winrt.microsoft.ui.windowing import OverlappedPresenter, TitleBarTheme
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
ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "expreload.ico")

# 窗口初始大小
WINDOW_WIDTH = 400
WINDOW_HEIGHT = 270

# 设置窗口大小
SETTINGS_WIDTH = 400
SETTINGS_HEIGHT = 320

# 监护关闭时窗口高度
MINIMODE_HEIGHT = 130

TEXT_PRIMARY = "TextFillColorPrimaryBrush"
TEXT_SECONDARY = "TextFillColorSecondaryBrush"
TEXT_TERTIARY = "TextFillColorTertiaryBrush"
BODY_BACKGROUND = "ApplicationPageBackgroundThemeBrush"


class ExplorerMonitor:
    def __init__(self):
        self.missing_since = None
        self.running = True
        self.on_state_change = None
        self._lock = threading.Lock()

    def is_explorer_running(self) -> bool:
        for proc in psutil.process_iter(["name"]):
            try:
                name = proc.info["name"]
                if name and name.lower() == EXPLORER_NAME:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False

    def monitor_loop(self):
        last = None
        while self.running:
            alive = self.is_explorer_running()
            if alive:
                with self._lock:
                    self.missing_since = None
            else:
                with self._lock:
                    if self.missing_since is None:
                        self.missing_since = time.time()
                    elif time.time() - self.missing_since >= MISSING_THRESHOLD:
                        self.missing_since = None
                        self.ask_and_restart()
            if alive != last:
                last = alive
                if self.on_state_change is not None:
                    self.on_state_change(alive)
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
    image = Image.new("RGB", (64, 64), color=(58, 130, 246))
    if os.path.exists(ICON_PATH):
        image = Image.open(ICON_PATH)

    def on_open(icon, item):
        app.show_window()

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


def _col(unit):
    c = ColumnDefinition()
    c.width = GridLength(1, unit)
    return c


def _row(unit):
    r = RowDefinition()
    r.height = GridLength(1, unit)
    return r


class ExpreloadApp(Application, IXamlMetadataProvider):
    def __init__(self):
        super().__init__()
        self._provider = XamlControlsXamlMetaDataProvider()
        self._window = None
        self._monitor = ExplorerMonitor()
        self._tray_icon = None
        self._info = None
        self._ring = None
        self._footer = None
        self._restart_btn = None
        self._settings_btn = None
        self._settings_window = None
        self._closing = False
        self._root_host = None
        self._background_guard = True
        self._theme_index = 0
        self._guard_toggle = None
        self._theme_combo = None

    @override
    def _on_launched(self, args: LaunchActivatedEventArgs) -> None:
        self.resources.merged_dictionaries.append(XamlControlsResources())

        self._monitor.on_state_change = self._apply_state
        threading.Thread(target=self._monitor.monitor_loop, daemon=True).start()
        register_hotkey()

        self._window = Window()
        self._window.title = "Expreload"
        self._window.system_backdrop = MicaBackdrop()

        presenter = OverlappedPresenter.create()
        presenter.is_maximizable = False
        presenter.is_resizable = False
        self._window.app_window.set_presenter(presenter)

        self._window.app_window.resize((WINDOW_WIDTH, WINDOW_HEIGHT))
        if os.path.exists(ICON_PATH):
            self._window.app_window.set_icon(ICON_PATH)
            self._window.app_window.set_taskbar_icon(ICON_PATH)

        self._window.app_window.add_closing(self._on_closing)
        self._window.content = self._build_ui()
        self._apply_theme_to_window(self._window)
        self._window.activate()

        self._tray_icon = create_tray_icon(self, self._monitor)
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

        self._apply_state(self._monitor.is_explorer_running())

    def _text(self, content, size, brush_key, weight=None):
        tb = TextBlock()
        tb.text = content
        tb.font_size = size
        if brush_key == TEXT_TERTIARY:
            tb.opacity = 0.55
        elif brush_key == TEXT_SECONDARY:
            tb.opacity = 0.72
        return tb

    def _build_ui(self):
        self._root_host = Grid()
        self._root_host.padding = Thickness(24, 20, 24, 20)
        self._root_host.children.append(self._build_main_page())
        return self._root_host

    def _build_main_page(self):
        grid = Grid()
        grid.row_definitions.append(_row(GridUnitType.AUTO))
        grid.row_definitions.append(_row(GridUnitType.AUTO))
        grid.row_definitions.append(_row(GridUnitType.STAR))
        grid.row_spacing = 16

        header = self._build_header()
        Grid.set_row(header, 0)
        grid.children.append(header)

        self._info = InfoBar()
        self._info.severity = InfoBarSeverity.SUCCESS
        self._info.title = "Explorer 运行正常"
        self._info.message = "守护进程中，异常时将提示重启"
        self._info.is_open = True
        self._info.is_closable = False
        Grid.set_row(self._info, 1)
        grid.children.append(self._info)

        footer = self._build_footer()
        footer.vertical_alignment = VerticalAlignment.BOTTOM
        self._footer = footer
        Grid.set_row(footer, 2)
        grid.children.append(footer)

        return grid

    def _build_header(self):
        head = Grid()
        head.column_definitions.append(_col(GridUnitType.STAR))
        head.column_definitions.append(_col(GridUnitType.AUTO))

        title = self._text("Expreload", 28, TEXT_PRIMARY)
        Grid.set_column(title, 0)
        head.children.append(title)

        actions = StackPanel()
        actions.orientation = Orientation.HORIZONTAL
        actions.spacing = 8
        actions.vertical_alignment = VerticalAlignment.CENTER

        self._ring = ProgressRing()
        self._ring.is_active = False
        self._ring.width = 16
        self._ring.height = 16
        self._ring.vertical_alignment = VerticalAlignment.CENTER
        self._ring.visibility = Visibility.COLLAPSED
        actions.children.append(self._ring)

        self._restart_btn = Button()
        self._restart_btn.padding = Thickness(12, 6, 12, 6)

        btn_row = StackPanel()
        btn_row.orientation = Orientation.HORIZONTAL
        btn_row.spacing = 6

        icon = FontIcon()
        icon.glyph = "\uE72C"
        icon.font_size = 14
        icon.vertical_alignment = VerticalAlignment.CENTER
        btn_row.children.append(icon)

        caption = TextBlock()
        caption.text = "重启 Explorer"
        caption.font_size = 13
        caption.vertical_alignment = VerticalAlignment.CENTER
        btn_row.children.append(caption)

        self._restart_btn.content = btn_row
        self._restart_btn.add_click(self._on_restart_click)
        actions.children.append(self._restart_btn)

        self._settings_btn = Button()
        self._settings_btn.width = 34
        self._settings_btn.height = 34
        self._settings_btn.min_width = 34
        self._settings_btn.padding = Thickness(0, 0, 0, 0)
        settings_icon = FontIcon()
        settings_icon.glyph = "\uE713"
        settings_icon.font_size = 16
        settings_icon.horizontal_alignment = HorizontalAlignment.CENTER
        settings_icon.vertical_alignment = VerticalAlignment.CENTER
        self._settings_btn.content = settings_icon
        self._settings_btn.add_click(self._on_settings_click)
        actions.children.append(self._settings_btn)

        Grid.set_column(actions, 1)
        head.children.append(actions)
        return head

    def _build_footer(self):
        foot = StackPanel()
        foot.spacing = 4

        foot.children.append(
            self._text("Ctrl + Shift + E 可随时重启 Explorer", 12, TEXT_TERTIARY)
        )
        foot.children.append(
            self._text("关闭窗口后程序仍在托盘运行", 12, TEXT_TERTIARY)
        )
        return foot

    def _on_restart_click(self, sender, args):
        threading.Thread(target=restart_explorer, daemon=True).start()

    def _on_settings_click(self, sender, args):
        self._open_settings_window()

    def _open_settings_window(self):
        if self._settings_window is not None:
            try:
                self._settings_window.activate()
                return
            except Exception:
                self._settings_window = None

        win = Window()
        win.title = "设置"
        win.system_backdrop = MicaBackdrop()

        presenter = OverlappedPresenter.create()
        presenter.is_maximizable = False
        presenter.is_minimizable = False
        presenter.is_resizable = False
        win.app_window.set_presenter(presenter)
        win.app_window.resize((SETTINGS_WIDTH, SETTINGS_HEIGHT))
        if os.path.exists(ICON_PATH):
            win.app_window.set_icon(ICON_PATH)
            win.app_window.set_taskbar_icon(ICON_PATH)

        win.content = self._build_settings_page(win)
        win.add_closed(self._on_settings_closed)
        self._settings_window = win
        self._apply_theme_to_window(win)
        win.activate()

    def _on_settings_closed(self, sender, args):
        self._settings_window = None

    def _build_settings_page(self, win):
        grid = Grid()
        grid.padding = Thickness(24, 20, 24, 20)
        grid.row_definitions.append(_row(GridUnitType.AUTO))
        grid.row_definitions.append(_row(GridUnitType.AUTO))
        grid.row_definitions.append(_row(GridUnitType.STAR))
        grid.row_spacing = 16

        title = self._text("设置", 28, TEXT_PRIMARY)
        Grid.set_row(title, 0)
        grid.children.append(title)

        body = StackPanel()
        body.spacing = 14

        body.children.append(self._text("后台监护", 15, TEXT_PRIMARY))

        guard_caption = self._text(
            "关闭窗口后在托盘中继续监视 Explorer", 12, TEXT_TERTIARY
        )
        guard_caption.text_wrapping = TextWrapping.WRAP
        body.children.append(guard_caption)

        self._guard_toggle = ToggleSwitch()
        self._guard_toggle.on_content = box_string("开启")
        self._guard_toggle.off_content = box_string("关闭")
        self._guard_toggle.is_on = self._background_guard
        self._guard_toggle.add_toggled(self._on_guard_toggled)
        body.children.append(self._guard_toggle)

        body.children.append(self._text("颜色模式", 15, TEXT_PRIMARY))

        self._theme_combo = ComboBox()
        self._theme_combo.width = 180
        for label in ("系统", "浅色", "深色"):
            item = ComboBoxItem()
            item.content = box_string(label)
            self._theme_combo.items.append(item)
        self._theme_combo.selected_index = self._theme_index
        self._theme_combo.add_selection_changed(self._on_theme_changed)
        body.children.append(self._theme_combo)

        Grid.set_row(body, 1)
        body.vertical_alignment = VerticalAlignment.TOP
        grid.children.append(body)

        return grid

    def _on_guard_toggled(self, sender, args):
        enabled = self._guard_toggle.is_on
        if enabled == self._background_guard:
            return
        self._background_guard = enabled
        if enabled:
            self._start_guard()
        else:
            self._stop_guard()

    def _start_guard(self):
        if not self._monitor.running:
            self._monitor.running = True
            threading.Thread(
                target=self._monitor.monitor_loop, daemon=True
            ).start()
        register_hotkey()
        if self._tray_icon is None:
            self._tray_icon = create_tray_icon(self, self._monitor)
            threading.Thread(target=self._tray_icon.run, daemon=True).start()
        self._apply_state(self._monitor.is_explorer_running())

    def _stop_guard(self):
        self._monitor.stop()
        keyboard.unhook_all_hotkeys()
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
            self._tray_icon = None
        self._apply_state(self._monitor.is_explorer_running())

    def _on_theme_changed(self, sender, args):
        idx = self._theme_combo.selected_index
        if idx < 0:
            return
        self._theme_index = idx
        self._apply_theme_to_window(self._window)
        if self._settings_window is not None:
            self._apply_theme_to_window(self._settings_window)

    def _apply_theme_to_window(self, win):
        if win is None:
            return
        themes = (ElementTheme.DEFAULT, ElementTheme.LIGHT, ElementTheme.DARK)
        title_themes = (
            TitleBarTheme.USE_DEFAULT_APP_MODE,
            TitleBarTheme.LIGHT,
            TitleBarTheme.DARK,
        )
        idx = self._theme_index
        try:
            content = win.content
            if content is not None:
                content.requested_theme = themes[idx]
        except Exception:
            pass
        try:
            win.app_window.title_bar.preferred_theme = title_themes[idx]
        except Exception:
            pass

    def _resize_window(self, w, h):
        try:
            self._window.app_window.resize((w, h))
        except Exception:
            pass

    def _apply_state(self, alive: bool):
        def update():
            if self._info is None:
                return
            if not self._background_guard:
                self._info.visibility = Visibility.COLLAPSED
                self._footer.visibility = Visibility.COLLAPSED
                self._ring.visibility = Visibility.COLLAPSED
                self._ring.is_active = False
                self._resize_window(WINDOW_WIDTH, MINIMODE_HEIGHT)
                return
            self._footer.visibility = Visibility.VISIBLE
            self._info.visibility = Visibility.VISIBLE
            self._resize_window(WINDOW_WIDTH, WINDOW_HEIGHT)
            if alive:
                self._info.severity = InfoBarSeverity.SUCCESS
                self._info.title = "Explorer 运行正常"
                self._info.message = "守护进程中，异常时将提示重启"
                self._ring.visibility = Visibility.COLLAPSED
            else:
                self._info.severity = InfoBarSeverity.ERROR
                self._info.title = "Explorer 未在运行"
                self._info.message = f"缺失 {MISSING_THRESHOLD} 秒后自动询问重启"
                self._ring.visibility = Visibility.VISIBLE
            self._ring.is_active = not alive

        try:
            self._window.dispatcher_queue.try_enqueue(update)
        except Exception:
            pass

    def _on_closing(self, sender, args):
        if self._closing:
            return
        if not self._background_guard:
            self._closing = True
            self._monitor.stop()
            keyboard.unhook_all_hotkeys()
            tray = self._tray_icon
            self._tray_icon = None
            if tray is not None:
                try:
                    tray.stop()
                except Exception:
                    pass
            return
        args.cancel = True
        self._hide_window()

    def show_window(self):
        self._run_on_ui(self._show_window)

    def _show_window(self):
        try:
            self._window.app_window.show()
            self._window.activate()
        except Exception as e:
            print(f"[Expreload] 显示窗口失败: {e}")

    def _hide_window(self):
        try:
            self._window.app_window.hide()
        except Exception as e:
            print(f"[Expreload] 隐藏窗口失败: {e}")

    def _run_on_ui(self, fn):
        queue = self._window.dispatcher_queue
        if queue.has_thread_access():
            fn()
        else:
            queue.try_enqueue(fn)

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