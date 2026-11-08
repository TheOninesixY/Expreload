import os
import re
import sys
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
    RichTextBlock,
    RowDefinition,
    ScrollBarVisibility,
    StackPanel,
    ScrollViewer,
    TextBlock,
    ToggleSwitch,
    XamlControlsResources,
)
from winrt.microsoft.ui.xaml.documents import Paragraph, Run
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
APP_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(APP_DIR, "expreload.ico")
VERSION_PATH = os.path.join(APP_DIR, "VERSION.md")

FALLBACK_VERSION = "unknown"

# 窗口初始大小
WINDOW_WIDTH = 400
WINDOW_HEIGHT = 270

# 设置窗口大小
SETTINGS_WIDTH = 400
SETTINGS_HEIGHT = 400

# 监护关闭时窗口高度
MINIMODE_HEIGHT = 130

TEXT_PRIMARY = "TextFillColorPrimaryBrush"
TEXT_SECONDARY = "TextFillColorSecondaryBrush"
TEXT_TERTIARY = "TextFillColorTertiaryBrush"
BODY_BACKGROUND = "ApplicationPageBackgroundThemeBrush"


def _resource_path(name):
    base = getattr(sys, "_MEIPASS", APP_DIR)
    return os.path.join(base, name)


def read_version_info():
    version = FALLBACK_VERSION
    changelog = ""
    path = _resource_path("VERSION.md")
    if not os.path.exists(path):
        return version, changelog
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().splitlines()
    except OSError:
        return version, changelog
    lines = [ln.rstrip() for ln in raw]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return version, changelog
    version = lines[0].strip()
    rest = lines[1:]
    while rest and not rest[0].strip():
        rest.pop(0)
    changelog = "\n".join(rest).rstrip()
    return version, changelog


class MarkdownBlock:
    def __init__(self, kind, text="", level=0, items=None):
        self.kind = kind
        self.text = text
        self.level = level
        self.items = items or []


def parse_markdown(source):
    blocks = []
    for raw in source.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        heading = 0
        while heading < 6 and stripped[: heading + 1] == "#" * (heading + 1):
            heading += 1
        if heading and stripped[heading: heading + 1] == " ":
            blocks.append(
                MarkdownBlock("heading", stripped[heading + 1:].strip(), heading)
            )
            continue
        if stripped.startswith(("- ", "* ", "+ ")):
            text = stripped[2:].strip()
            if blocks and blocks[-1].kind == "list":
                blocks[-1].items.append(text)
            else:
                blocks.append(MarkdownBlock("list", items=[text]))
            continue
        blocks.append(MarkdownBlock("text", stripped))
    return blocks


INLINE_PATTERN = re.compile(
    r"(\*\*.+?\*\*|`.+?`|\*[^*]+?\*|\[[^\]]+\]\([^)]+\))"
)


def iter_inline_segments(text):
    result = []
    for part in INLINE_PATTERN.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            result.append(("bold", part[2:-2]))
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            result.append(("code", part[1:-1]))
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            result.append(("italic", part[1:-1]))
        else:
            m = re.fullmatch(r"\[([^\]]+)\]\(([^)]+)\)", part)
            if m:
                result.append(("link", m.group(1), m.group(2)))
            else:
                result.append(("plain", part))
    return result


SHELL_TRAY_CLASS = "Shell_TrayWnd"


def _shell_window_present():
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(SHELL_TRAY_CLASS, None)
        return bool(hwnd)
    except Exception as e:
        print(f"[Expreload] 检测桌面窗口失败: {e}")
        return None


def _process_scan_for_explorer():
    try:
        for pid in psutil.pids():
            try:
                name = psutil.Process(pid).name()
            except Exception:
                continue
            if name and name.lower() == EXPLORER_NAME:
                return True
    except Exception as e:
        print(f"[Expreload] 枚举进程失败: {e}")
    return False


class ExplorerMonitor:
    def __init__(self):
        self.missing_since = None
        self.running = True
        self.on_state_change = None
        self._lock = threading.Lock()

    def is_explorer_running(self) -> bool:
        shell = _shell_window_present()
        if shell is not None:
            return shell
        return _process_scan_for_explorer()

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


def _patch_pystray_message_filter():
    try:
        import pystray._util.win32 as w
    except Exception as e:
        print(f"[Expreload] 无法加载 pystray win32 后端: {e}")
        return

    original = getattr(w, "ChangeWindowMessageFilterEx", None)
    if original is None:
        return
    if getattr(original, "_expreload_patched", False):
        return

    def safe_filter(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except Exception as e:
            print(f"[Expreload] 跳过消息过滤设置(非提权运行): {e}")
            return True

    safe_filter._expreload_patched = True
    w.ChangeWindowMessageFilterEx = safe_filter


def run_tray_icon(icon):
    try:
        import ctypes

        ctypes.windll.ole32.CoInitializeEx(None, 0x2)
    except Exception as e:
        print(f"[Expreload] 托盘线程 COM 初始化失败: {e}")
    _patch_pystray_message_filter()
    try:
        icon.run()
    except Exception as e:
        print(f"[Expreload] 托盘图标启动失败: {e}")
        import traceback

        traceback.print_exc()


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
        self._settings_root = None
        self._settings_inner = None
        self._closing = False
        self._root_host = None
        self._inner_host = None
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
        built = self._build_ui()
        self._window.content = built
        self._apply_theme_to_window(self._window)
        self._window.activate()

        self._tray_icon = create_tray_icon(self, self._monitor)
        threading.Thread(
            target=run_tray_icon, args=(self._tray_icon,), daemon=True
        ).start()

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
        inner = Grid()
        inner.padding = Thickness(24, 20, 24, 20)
        inner.children.append(self._build_main_page())
        self._root_host.children.append(inner)
        self._inner_host = inner
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

        page = self._build_settings_page(win)
        outer = Grid()
        outer.children.append(page)
        self._settings_root = outer
        self._settings_inner = page
        win.content = outer
        win.add_closed(self._on_settings_closed)
        self._settings_window = win
        self._apply_theme_to_window(win)
        win.activate()

    def _on_settings_closed(self, sender, args):
        self._settings_window = None
        self._settings_root = None
        self._settings_inner = None

    def _build_settings_page(self, win):
        grid = Grid()
        grid.padding = Thickness(24, 20, 24, 20)
        grid.row_definitions.append(_row(GridUnitType.AUTO))
        grid.row_definitions.append(_row(GridUnitType.STAR))
        grid.row_definitions.append(_row(GridUnitType.AUTO))
        grid.row_spacing = 12

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

        scroll = ScrollViewer()
        scroll.vertical_scroll_bar_visibility = ScrollBarVisibility.AUTO
        scroll.content = body
        Grid.set_row(scroll, 1)
        body.vertical_alignment = VerticalAlignment.TOP
        grid.children.append(scroll)

        about = self._build_about_section()
        Grid.set_row(about, 2)
        grid.children.append(about)

        return grid

    def _build_about_section(self):
        version, changelog = read_version_info()

        wrap = StackPanel()
        wrap.spacing = 6

        head = StackPanel()
        head.orientation = Orientation.HORIZONTAL
        head.spacing = 8

        about_label = self._text("关于", 15, TEXT_PRIMARY)
        about_label.vertical_alignment = VerticalAlignment.CENTER
        head.children.append(about_label)

        ver_label = self._text(version, 12, TEXT_TERTIARY)
        ver_label.vertical_alignment = VerticalAlignment.CENTER
        head.children.append(ver_label)

        wrap.children.append(head)

        if changelog:
            rtb = RichTextBlock()
            rtb.text_wrapping = TextWrapping.WRAP
            rtb.margin = Thickness(0, 2, 0, 0)
            for block in parse_markdown(changelog):
                rtb.blocks.append(self._markdown_paragraph(block))
            wrap.children.append(rtb)

        return wrap

    def _markdown_paragraph(self, block):
        para = Paragraph()
        if block.kind == "heading":
            size = {1: 17, 2: 15, 3: 14}.get(block.level, 13)
            para.font_size = size
            for seg in iter_inline_segments(block.text):
                para.inlines.append(self._inline_run(seg, bold_default=True))
            return para

        if block.kind == "list":
            for idx, item in enumerate(block.items):
                if idx:
                    br = Run()
                    br.text = "\n"
                    para.inlines.append(br)
                bullet = Run()
                bullet.text = "• "
                para.inlines.append(bullet)
                for seg in iter_inline_segments(item):
                    para.inlines.append(self._inline_run(seg))
            return para

        para.font_size = 12
        for seg in iter_inline_segments(block.text):
            para.inlines.append(self._inline_run(seg))
        return para

    def _inline_run(self, seg, bold_default=False):
        kind = seg[0]
        run = Run()
        run.text = seg[1]
        if kind == "code":
            run.font_family = "Consolas"
        if kind == "bold" or bold_default:
            run.font_weight = (700, 5)
        return run

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
            threading.Thread(
                target=run_tray_icon, args=(self._tray_icon,), daemon=True
            ).start()
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
        roots = []
        if win is self._window:
            roots = [self._root_host, self._inner_host]
        elif win is self._settings_window:
            roots = [self._settings_root, self._settings_inner]
        for elem in roots:
            if elem is None:
                continue
            try:
                elem.requested_theme = themes[idx]
            except Exception as e:
                print(f"[Expreload] 设置主题失败: {e}", flush=True)
            try:
                elem.background = None
            except Exception as e:
                print(f"[Expreload] 清除背景失败: {e}", flush=True)
        try:
            win.app_window.title_bar.preferred_theme = title_themes[idx]
        except Exception as e:
            print(f"[Expreload] 应用主题失败(标题栏): {e}", flush=True)

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