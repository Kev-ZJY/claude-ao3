"""Native fullscreen Textual client; the engine owns content and navigation."""
from __future__ import annotations

import asyncio
import math
import re
import time
import webbrowser
from typing import Any, Callable, Coroutine
from urllib.parse import urlsplit

from rich.cells import cell_len, set_cell_size
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button, Input, OptionList, SelectionList, Static, Tab, Tabs
from textual.widgets.option_list import OptionDoesNotExist

from .models import CATEGORY_OPTIONS, WARNING_OPTIONS, SearchFilters
from .terminal_theme import palette_for, terminal_is_dark, theme_for
from .rendering import LayoutLine, layout_document, line_for_anchor
from .source import SourceError


WORK_NOTES = (
    "分页组件在窗口变窄时重新计算可用列数。先检查长段落的边界，再核对输入区域与状态信息的位置。",
    "ResizeObserver 只在有效宽度变化时更新布局。列表通过稳定的 item ID 和偏移恢复位置，避免页面尺寸变化后回到顶部。",
    "缓存键包含 query、page 和过滤条件。相同请求复用已有结果，取消过时的响应，避免慢请求覆盖最新界面。",
    "列表加载失败时保留上一次有效内容，错误信息提供可执行的重试入口。空结果与网络错误使用不同状态。",
    "数据格式发生变化时先验证必填字段，再进入渲染流程。界面只接收结构完整的条目。",
    "键盘事件按焦点分发。输入框保留常规编辑行为，列表使用方向键，快捷操作不抢占中文输入法的组合状态。",
    "配置标签保留尚未提交的内容。确认与取消需要清晰目标，避免焦点移动造成误提交。",
    "滚动抵达边界时停止。触控板的惯性事件单独测试，不把每一帧当作新指令。",
    "明暗主题复用同一套间距和布局变量。重要状态有文字标签，不能单靠颜色区分。",
    "核对输入提示符、行距和选中背景。不同宽度下的信息顺序保持一致，字号由宿主终端控制。",
) * 5


auto_dark = terminal_is_dark

# Presentation only: requests continue to use AO3's unmodified filter IDs.
WARNING_LABELS = {
    "14": "作者选择不使用内容警告", "17": "直白暴力描写",
    "18": "主要角色死亡", "16": "不适用上述内容警告",
    "19": "强奸／非自愿性行为", "20": "未成年人性行为",
}
CATEGORY_LABELS = {
    "116": "女性／女性关系", "22": "女性／男性关系", "21": "无恋爱关系或非重点",
    "23": "男性／男性关系", "2246": "多种关系类别", "24": "其他关系类别",
}

class ReaderViewport(Widget, can_focus=True):
    """Only visible rows become Rich strips; full layout is cached across scrolling."""

    BINDINGS = [
        Binding("down,j", "step(1)", show=False),
        Binding("up,k", "step(-1)", show=False),
        Binding("pagedown,space", "page(1)", show=False),
        Binding("pageup", "page(-1)", show=False),
        Binding("home", "beginning", show=False),
        Binding("end", "end", show=False),
        Binding("n", "next_unit", show=False),
        Binding("p", "previous_unit", show=False),
        Binding("right_square_bracket", "skip_work", show=False),
        Binding("r", "retry", show=False),
    ]
    DEFAULT_CSS = """
    ReaderViewport { width: 1fr; height: 1fr; overflow: hidden; padding: 0; }
    ReaderViewport:focus { border: none; }
    """

    class PositionChanged(Message):
        def __init__(self, viewport: ReaderViewport) -> None:
            super().__init__()
            self.viewport = viewport

    def __init__(self, *, id: str, navigable: bool = True) -> None:
        super().__init__(id=id)
        self.navigable = navigable
        self.paragraphs: tuple[str, ...] = ()
        self.document_title = ""
        self.section = 1
        self.rows: tuple[LayoutLine, ...] = ()
        self.line_offset = 0
        self.layout_width = 0
        self.layout_count = 0
        self.strip_build_count = 0
        self._strips: dict[tuple[int, bool, int], Strip] = {}
        self._last_key = 0.0
        self._reading_height = 1
        self._last_wheel = 0.0
        self._wheel_switched = False

    @property
    def max_offset(self) -> int:
        return max(0, len(self.rows) - self._reading_height)

    @property
    def percent(self) -> int:
        return round(100 * self.line_offset / self.max_offset) if self.max_offset else (100 if self.paragraphs else 0)

    def anchor(self) -> dict:
        if not self.rows:
            return {"paragraph": 0, "offset": 0}
        if self.line_offset == 0:
            return {"paragraph": 0, "offset": 0, "at_start": True}
        index = min(self.line_offset, len(self.rows)-1)
        while index < len(self.rows)-1 and self.rows[index].paragraph < 0:
            index += 1
        row = self.rows[index]
        return {"paragraph": max(0, row.paragraph), "offset": row.offset}

    def set_document(self, paragraphs: list[str] | tuple[str, ...], *, title: str = "", section: int = 1, anchor: dict | None = None) -> None:
        values = tuple(paragraphs)
        changed = (values, title, section) != (self.paragraphs, self.document_title, self.section)
        self.paragraphs, self.document_title, self.section = values, title, section
        self.reflow(anchor or {}, force=changed)

    def reflow(self, anchor: dict | None = None, *, force: bool = False) -> None:
        width = max(2, self.size.width - 4)
        saved = self.anchor() if anchor is None else anchor
        if force or width != self.layout_width or not self.rows:
            self.rows = layout_document(self.paragraphs, width, self.document_title, self.section)
            self.layout_width = width
            self.layout_count += 1
            self._strips.clear()
        self.line_offset = 0 if not saved or saved.get("at_start") else min(self.max_offset, line_for_anchor(self.rows, saved))
        self.refresh()
        self.post_message(self.PositionChanged(self))

    def on_resize(self, event: events.Resize) -> None:
        if self.display and event.size.height > 0 and event.size.width > 0:
            self._reading_height = event.size.height
            self.reflow()

    def invalidate_colors(self) -> None:
        self._strips.clear()
        self.refresh()

    def render_line(self, y: int) -> Strip:
        app = self.app
        dark = getattr(app, "dark_palette", True)
        fg, bg, muted, accent, block = app.palette(dark)
        base = Style(color=fg, bgcolor=bg)
        width = self.size.width
        if width < 24 or self.size.height < 2:
            value = "请扩大终端；Ctrl+G / Ctrl+C" if y == 0 else ""
            return Strip([Segment(value, base)]).adjust_cell_length(width, base)
        index = self.line_offset + y
        if index >= len(self.rows):
            return Strip.blank(width, base)
        key = (index, app.theme, width)
        if key not in self._strips:
            row = self.rows[index]
            style = Style(color=app.terminal_palette.block_foreground if row.kind == "command" else (muted if row.kind == "meta" else fg), bgcolor=block if row.kind == "command" else bg, dim=row.kind == "meta", reverse=bool(row.kind == "command" and app.no_color))
            visual = row.text.replace("\t", " " * min(4, self.layout_width))
            text = Text("  " + visual, style=style, no_wrap=True, overflow="crop")
            latin = len(re.findall(r"[A-Za-z]", visual)) / max(1, len(visual))
            pattern = r"\b\d+(?::\d+)?\b" if latin > .55 else r"[A-Za-z][A-Za-z0-9_.\/-]*|\d+(?::\d+)?"
            for match in re.finditer(pattern, visual):
                # A host-default accent need not contrast with a shaded block.
                text.stylize(Style(color=app.terminal_palette.block_foreground, bold=True) if row.kind == "command" else accent, match.start()+2, match.end()+2)
            segments = [Segment(segment.text, segment.style or style, segment.control) for segment in text.render(app.console)]
            self._strips[key] = Strip(segments).adjust_cell_length(width, style)
            self.strip_build_count += 1
        return self._strips[key]

    def move(self, amount: int) -> None:
        self.line_offset = max(0, min(self.max_offset, self.line_offset + amount))
        self.refresh()
        self.post_message(self.PositionChanged(self))

    def _step(self, direction: int, amount: int, *, independent: bool) -> None:
        edge = self.line_offset >= self.max_offset if direction > 0 else self.line_offset <= 0
        if edge and independent and self.navigable:
            self.app.navigate(direction)
        else:
            self.move(direction * amount)

    def action_step(self, direction: int) -> None:
        now = time.monotonic()
        independent = now - self._last_key > .18
        self._last_key = now
        self._step(direction, 1, independent=independent)

    def action_page(self, direction: int) -> None:
        now = time.monotonic()
        independent = now - self._last_key > .18
        self._last_key = now
        self._step(direction, max(1, self.size.height - 1), independent=independent)

    def action_beginning(self) -> None:
        self.move(-len(self.rows))

    def action_end(self) -> None:
        self.move(len(self.rows))

    def action_next_unit(self) -> None:
        if self.navigable:
            self.app.navigate(1)

    def action_previous_unit(self) -> None:
        if self.navigable:
            self.app.navigate(-1)

    def action_skip_work(self) -> None:
        if self.navigable:
            self.app.skip_content()

    def action_retry(self) -> None:
        if self.navigable:
            self.app.action_retry()

    def on_key(self, event: events.Key) -> None:
        if self.navigable and event.character == "/":
            event.stop()
            event.prevent_default()
            self.app.focus_command("/")

    def _wheel(self, event: events.MouseScrollDown | events.MouseScrollUp, direction: int) -> None:
        event.stop()
        event.prevent_default()
        now = time.monotonic()
        fresh = now - self._last_wheel > .36
        self._last_wheel = now
        if fresh:
            self._wheel_switched = False
        if self._wheel_switched:
            return
        edge = self.line_offset >= self.max_offset if direction > 0 else self.line_offset <= 0
        if fresh and edge and self.navigable:
            self._wheel_switched = True
            self.app.navigate(direction)
        else:
            self.move(3 * direction)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._wheel(event, 1)

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._wheel(event, -1)


class SearchSelection(SelectionList[str]):
    BINDINGS = [Binding("enter", "submit", priority=True, show=False)]

    def action_submit(self) -> None:
        self.app.action_submit_search()

    def render_line(self, y: int) -> Strip:
        # Textual's default X glyph encodes checked/unchecked only by color.
        # Keep OptionList's hit targets and scrolling, but print a literal state.
        line = OptionList.render_line(self, y)
        index = self.scroll_offset.y + y
        try:
            selection = self.get_option_at_index(index)
        except OptionDoesNotExist:
            return line
        style = (next(iter(line)).style or self.rich_style) + Style(meta={"option": index})
        marker = "[x] " if selection.value in self.selected else "[ ] "
        return Strip([Segment(marker, style), *line]).crop(0, self.size.width)


class ResultOptions(OptionList):
    BINDINGS = [Binding("n,right", "more", show=False), Binding("p,left", "previous", show=False)]

    def action_more(self) -> None:
        self.app.action_more_results()

    def action_previous(self) -> None:
        self.app.action_previous_results()


class CommandInput(Input):
    BINDINGS = [Binding("up", "menu_up", show=False), Binding("down", "menu_down", show=False), Binding("tab", "complete", priority=True, show=False)]

    def action_complete(self) -> None:
        if self.app.command_menu_open:
            self.app.complete_command_menu()
        else:
            self.app.action_focus_next()

    def action_menu_up(self) -> None:
        self.app.move_command_menu(-1)

    def action_menu_down(self) -> None:
        self.app.move_command_menu(1)

    async def action_submit(self) -> None:
        if self.app.command_menu_open and self.app.command_matches:
            self.app.accept_command_menu()
        else:
            await super().action_submit()

    def on_focus(self) -> None:
        self.app.refresh_command_menu()

    def on_blur(self) -> None:
        # Selection clicks are handled by OptionSelected before the next refresh.
        self.app.call_after_refresh(self.app.hide_menu_if_blurred)


class CommandMenu(OptionList):
    can_focus = False


COMMANDS = (
    ("/search", "搜索与筛选"), ("/next", "下一章/篇（n）"),
    ("/prev", "上一章/篇（p）"), ("/results", "返回最近搜索结果"),
    ("/skip-work", "跳过本作余章，读下一部作品"),
    ("/storage", "存储管理与清理缓存"),
    ("/retry", "重试上次未完成的请求"),
    ("/theme", "自动 / 暗色 / 亮色"),
    ("/help", "全部操作"), ("/quit", "保存并退出"),
)


class WorkMask(Screen):
    """A separate opaque screen: async results never become visible through it."""

    def compose(self) -> ComposeResult:
        yield ReaderViewport(id="mask-body", navigable=False)
        with Vertical(id="mask-footer", classes="fixed-footer"):
            yield Static("local · commands", classes="status-above")
            yield Static("", classes="rule", id="mask-rule-top")
            yield Static(">", classes="prompt-line")
            yield Static("", classes="rule", id="mask-rule-bottom")
            yield Static("Workspace / local", classes="footer-muted", id="mask-footer-first")
            yield Static(">> document mode · 1 session", classes="footer-accent")

    def on_mount(self) -> None:
        self.query_one("#mask-body", ReaderViewport).set_document(WORK_NOTES)
        self.query_one("#mask-body").focus()
        self.resize_rules()

    def on_resize(self) -> None:
        self.resize_rules()

    def resize_rules(self) -> None:
        for item in self.query(".rule"):
            item.update("─" * max(1, self.size.width - 2))


class MiniNotesApp(App[None]):
    TOAST_SECONDS = 3.0
    RETRY_ATTEMPTS = 3
    RETRY_BUDGET = 45.0
    RETRY_DELAYS = (2.0, 5.0)
    EXHAUSTION_NOTICE = "搜索结果已浏览完毕，正在为您推荐近期新作"
    TITLE = "Workspace"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+g", "toggle_mask", priority=True, show=False),
        Binding("ctrl+c", "exit_reader", priority=True, show=False),
        Binding("escape", "back", show=False),
    ]
    CSS = """
    Screen { background: $background; color: $foreground; layout: vertical; }
    #body { height: 1fr; width: 1fr; min-height: 1; }
    #reader, #search-panel, #results-panel, #error-panel, #info-panel, #storage-panel { height: 1fr; width: 1fr; }
    #search-panel, #results-panel, #error-panel, #info-panel, #storage-panel { padding: 0 2; }
    .fixed-footer { height: 6; min-height: 6; max-height: 6; width: 1fr; dock: bottom; }
    .fixed-footer Static { height: 1; width: 1fr; padding: 0 1; }
    .status-above { text-align: right; color: $text-muted; }
    .rule { color: $text-muted; }
    .footer-muted { color: $text-muted; }
    .footer-accent { color: $accent; }
    #input-row { height: 1; width: 1fr; }
    #prompt { width: 2; height: 1; padding: 0 0 0 1; }
    #command { width: 1fr; height: 1; min-height: 1; border: none; padding: 0; background: $background; color: $foreground; }
    #command:focus { border: none; background-tint: transparent; }
    #search-tabs { height: 2; }
    .compact #search-tabs { height: 2; }
    .compact #filter-help { height: 1; }
    #result-buttons .text-action { padding: 0; }
    #search-panel { overflow-y: auto; }
    #search-buttons, #result-buttons, #error-buttons { overflow-x: auto; }
    #error-confirm { display: none; }
    #query-label { height: 1; color: $text-muted; }
    #any-field { height: 3; min-height: 3; margin: 0; border: solid $border; padding: 0 1; background: $background; color: $foreground; }
    #any-field:focus { border: solid $accent; background-tint: transparent; }
    #any-field > .input--placeholder, #command > .input--placeholder { color: $text-muted; text-style: none; }
    #any-field > .input--cursor, #command > .input--cursor { color: $input-cursor-foreground; background: $input-cursor-background; text-style: none; }
    #filter-help { height: auto; color: $text-muted; }
    #warning-options, #category-options { height: 1fr; border: none; padding: 0; }
    #search-buttons, #result-buttons, #storage-buttons { height: 1; dock: bottom; }
    #storage-content { height: 1fr; }
    #storage-message { height: auto; }
    #error-buttons, #info-buttons { height: 3; }
    .text-action { height: 1; min-height: 1; min-width: 8; width: auto; padding: 0 1 0 0; margin: 0 1 0 0; border: none; background: $background; color: $foreground; text-style: none; content-align: left middle; }
    .text-action:focus { text-style: reverse; border: none; background-tint: transparent; }
    .text-action:hover { background: $background; text-style: underline; }
    #command-menu { overlay: screen; constrain: inside; layer: overlay; height: 6; width: 1fr; padding: 0 2; border: none; background: $background; }
    #command-menu > .option-list--option-highlighted, #results-list > .option-list--option-highlighted, #warning-options > .option-list--option-highlighted, #category-options > .option-list--option-highlighted { color: $block-cursor-foreground; background: $block-cursor-background; text-style: bold; }
    #results-list, #warning-options, #category-options { background: $background; }
    Button { min-width: 10; margin: 0 1 0 0; }
    #results-summary { height: auto; color: $text-muted; }
    #results-list { height: 1fr; border: none; padding: 0; }
    #error-message, #info-message { height: auto; margin: 1 0; }
    WorkMask { background: $background; }
    """

    def __init__(self, engine: Any, *, theme_mode: str = "auto", source_url: str | None = None) -> None:
        super().__init__(ansi_color=True)
        self.engine = engine
        self.theme_mode = theme_mode
        self.dark_palette = auto_dark() if theme_mode == "auto" else theme_mode != "light"
        self.source_url = source_url or str(getattr(getattr(engine, "source", None), "base_url", "https://archiveofourown.org"))
        self.mode = "reader"
        self.masked = False
        self.busy = False
        self.last_error: Exception | None = None
        self.error_code = ""
        self._retry: tuple | None = None
        self._retry_pending = False
        self._operation_serial = 0
        self._progress: dict | None = None
        self._recovery: dict | None = None
        self._operation_task: asyncio.Task | None = None
        self._prefetch_task: asyncio.Task | None = None
        self._masked_focus: Widget | None = None
        self._masked_mode = "reader"
        self._ui_mounted = False
        self._deferred_focus: Widget | None = None
        self._exit_requested = False
        self._displayed_url = ""
        self._local_notice = ""
        self._local_notice_timer = None
        self._observed_engine_notice = ""
        self._toast_text = ""
        self._toast_timer = None
        self._initial_search_form = (engine.filters.query, set(engine.filters.warnings), set(engine.filters.categories))
        self.command_matches: list[str] = []
        self._command_menu_dismissed: str | None = None
        self._filling_results = False
        self._save_timer = None
        self._restoring_form = False
        self.color_capability = self.console.color_system
        self.terminal_palette = palette_for(theme_mode, self.color_capability, self.dark_palette)
        self._register_themes()
        self.theme = "mini-host" if theme_mode == "auto" else "mini-" + theme_mode

    def _register_themes(self) -> None:
        for mode in ("auto", "dark", "light"):
            self.register_theme(theme_for(mode, self.color_capability, self.dark_palette))

    def palette(self, dark: bool) -> tuple[str, str, str, str, str]:
        return self.terminal_palette.tuple()

    def compose(self) -> ComposeResult:
        filters = self.engine.filters
        with Container(id="body"):
            yield ReaderViewport(id="reader")
            with Vertical(id="search-panel"):
                yield Tabs(Tab("搜索", id="tab-search"), Tab("内容警告", id="tab-warnings"), Tab("关系类别", id="tab-categories"), id="search-tabs")
                yield Static("关键词 · 标题 / 作者 / 摘要 / 标签", id="query-label")
                yield Input(filters.query, placeholder="输入关键词；不检索正文", id="any-field")
                yield Static("", id="filter-help")
                yield SearchSelection(*[(label, key, key in filters.warnings) for key, label in WARNING_LABELS.items()], id="warning-options")
                yield SearchSelection(*[(label, key, key in filters.categories) for key, label in CATEGORY_LABELS.items()], id="category-options")
                with Horizontal(id="search-buttons"):
                    yield Button("Enter 搜索", id="search-submit", classes="text-action")
                    yield Button("Esc 返回", id="search-back", classes="text-action")
            with Vertical(id="results-panel"):
                yield Static("", id="results-summary")
                yield ResultOptions(id="results-list")
                with Horizontal(id="result-buttons"):
                    yield Button("p 上一页", id="results-previous", classes="text-action")
                    yield Button("n 下一页", id="results-more", classes="text-action")
                    yield Button("修改搜索", id="results-search", classes="text-action")
                    yield Button("返回阅读", id="results-back", classes="text-action")
            with VerticalScroll(id="error-panel"):
                yield Static("", id="error-message", markup=False)
                with Horizontal(id="error-buttons"):
                    yield Button("确认并继续", id="error-confirm", variant="primary")
                    yield Button("重试", id="error-retry", variant="primary")
                    yield Button("原站查看", id="error-source")
                    yield Button("跳过", id="error-skip")
                    yield Button("返回", id="error-back")
            with VerticalScroll(id="info-panel"):
                yield Static("", id="info-message", markup=False)
                with Horizontal(id="info-buttons"):
                    yield Button("返回", id="info-back")
            with Vertical(id="storage-panel"):
                with VerticalScroll(id="storage-content"):
                    yield Static("", id="storage-message", markup=False)
                with Horizontal(id="storage-buttons"):
                    yield Button("清理缓存", id="storage-clear", classes="text-action")
                    yield Button("刷新", id="storage-refresh", classes="text-action")
                    yield Button("Esc 返回", id="storage-back", classes="text-action")
        yield CommandMenu(id="command-menu")
        with Vertical(id="main-footer", classes="fixed-footer"):
            yield Static("", id="above-input", classes="status-above")
            yield Static("", id="rule-top", classes="rule")
            with Horizontal(id="input-row"):
                yield Static(">", id="prompt")
                yield CommandInput(id="command", select_on_focus=False)
            yield Static("", id="rule-bottom", classes="rule")
            yield Static("尚未打开作品", id="footer-first", classes="footer-muted")
            yield Static("/ 搜索与命令", id="footer-second", classes="footer-accent")

    @property
    def reader(self) -> ReaderViewport:
        return self.screen_stack[0].query_one("#reader", ReaderViewport)

    def main_query(self, selector: str, kind: type | None = None):
        return self.screen_stack[0].query_one(selector, kind) if kind else self.screen_stack[0].query_one(selector)

    def on_mount(self) -> None:
        self._ui_mounted = True
        self.screen_stack[0].set_class(self.size.height < 22, "compact")
        self.main_query("#command-menu").display = False
        self.set_interval(.2, self.update_footer)
        self._set_mode("reader")
        self._search_tab("tab-search")
        self.reader.set_document(("正在连接书源。输入 /search 搜索，Ctrl+G 可随时切换工作界面。",), title="")
        self.reader.focus()
        self._resize_rules()
        self.queue_operation("正在准备内容…", self.engine.start, "reader", save=False)

    def on_resize(self) -> None:
        if self._ui_mounted:
            self.screen_stack[0].set_class(self.size.height < 22, "compact")
        self._resize_rules()
        self.position_command_menu()
        self.update_footer()

    def _resize_rules(self) -> None:
        if not self._ui_mounted or not self.is_running:
            return
        for item in self.screen_stack[0].query(".rule"):
            item.update("─" * max(1, self.size.width - 2))

    def focus_main(self, widget: Widget) -> None:
        if self.masked:
            self._deferred_focus = widget
        else:
            widget.focus()

    def focus_command(self, value: str = "") -> None:
        command = self.main_query("#command", Input)
        self._command_menu_dismissed = None
        command.value = value
        command.cursor_position = len(value)
        self.focus_main(command)
        self.call_after_refresh(self.refresh_command_menu)

    @property
    def command_menu_open(self) -> bool:
        return self._ui_mounted and self.main_query("#command-menu").display

    def refresh_command_menu(self) -> None:
        if not self._ui_mounted or not self.is_running or self.masked:
            return
        command = self.main_query("#command", Input)
        value = command.value
        menu = self.main_query("#command-menu", CommandMenu)
        if self.focused is not command or not value.startswith("/") or " " in value or value == self._command_menu_dismissed:
            menu.display = False
            return
        matches = [(name, label) for name, label in self.available_commands() if name.startswith(value.lower())]
        names = [name for name, _ in matches]
        selected = menu.highlighted or 0 if names == self.command_matches else 0
        self.command_matches = names
        menu.clear_options()
        for index, (name, label) in enumerate(matches):
            line = Text(("> " if index == selected else "  ") + name, style="bold")
            line.append("  " + label, style="dim")
            menu.add_option(line)
        if not matches:
            menu.add_option(Text("无匹配命令；Esc 关闭", style="dim"))
        menu.highlighted = min(selected, max(0, len(matches) - 1))
        menu.display = True
        self.position_command_menu()

    def available_commands(self) -> list[tuple[str, str]]:
        has_series = bool(getattr(self.engine, "series", []))
        return [(name, label if name != "/skip-work" or has_series else "跳过本作余章，读下一部作品")
                for name, label in COMMANDS]

    def update_menu_markers(self) -> None:
        menu = self.main_query("#command-menu", CommandMenu)
        labels = dict(self.available_commands())
        for index, name in enumerate(self.command_matches):
            line = Text(("> " if index == menu.highlighted else "  ") + name, style="bold")
            line.append("  " + labels.get(name, ""), style="dim")
            menu.replace_option_prompt_at_index(index, line)

    @on(OptionList.OptionHighlighted, "#command-menu")
    def command_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self._ui_mounted and self.main_query("#command-menu").option_count == len(self.command_matches):
            self.update_menu_markers()

    def complete_command_menu(self) -> None:
        menu = self.main_query("#command-menu", CommandMenu)
        index = menu.highlighted or 0
        if index < len(self.command_matches):
            self.focus_command(self.command_matches[index])

    def position_command_menu(self) -> None:
        if not self._ui_mounted or not self.is_running:
            return
        menu = self.main_query("#command-menu", CommandMenu)
        rows = min(max(1, len(self.command_matches)), max(1, self.size.height - 7), 6)
        menu.styles.height = rows
        menu.styles.offset = (0, max(0, self.size.height - 6 - rows))

    def hide_menu_if_blurred(self) -> None:
        if self._ui_mounted and self.is_running and not self.masked and self.focused is not self.main_query("#command"):
            self.main_query("#command-menu").display = False

    def move_command_menu(self, direction: int) -> None:
        if self.command_menu_open:
            menu = self.main_query("#command-menu", CommandMenu)
            if direction > 0:
                menu.action_cursor_down()
            else:
                menu.action_cursor_up()

    def accept_command_menu(self) -> None:
        menu = self.main_query("#command-menu", CommandMenu)
        index = menu.highlighted or 0
        if index >= len(self.command_matches):
            return
        command = self.command_matches[index]
        self.main_query("#command", Input).value = ""
        menu.display = False
        self.execute_command(command)

    @on(OptionList.OptionSelected, "#command-menu")
    def command_selected(self, event: OptionList.OptionSelected) -> None:
        self.accept_command_menu()

    @on(Input.Changed)
    def input_changed(self, event: Input.Changed) -> None:
        if not self._ui_mounted or not self.is_running:
            return
        if event.input.id == "command":
            self.refresh_command_menu()
        elif event.input.id == "any-field":
            self.remember_search_draft()

    @on(SelectionList.SelectedChanged)
    def search_selection_changed(self, event: SelectionList.SelectedChanged) -> None:
        if self._ui_mounted and event.selection_list.id in ("warning-options", "category-options"):
            self.remember_search_draft()

    def remember_search_draft(self) -> None:
        if self._restoring_form:
            return
        self.engine.search_draft = {
            "query": self.main_query("#any-field", Input).value,
            "warnings": list(self.main_query("#warning-options", SearchSelection).selected),
            "categories": list(self.main_query("#category-options", SearchSelection).selected),
            "language": "zh",
        }
        self.schedule_save()

    def schedule_save(self) -> None:
        if hasattr(self.engine, "save") and not self._exit_requested:
            if self._save_timer is not None:
                self._save_timer.stop()
            self._save_timer = self.set_timer(1.0, self.flush_save)

    def flush_save(self) -> None:
        self._save_timer = None
        if hasattr(self.engine, "save"):
            try:
                if self.engine.save() is False:
                    if not getattr(self.engine, "storage_notice", ""):
                        self.set_local_notice("当前章节未保存；请在 /storage 查看", persistent=True)
                elif "未保存" in self._local_notice:
                    self.set_local_notice("")
                self.update_footer()
            except Exception:
                self.set_local_notice("本地进度暂未保存；退出时将重试", persistent=True)
                self.update_footer()

    def prefetch_label(self) -> str:
        status = getattr(self.engine, "prefetch_status", {})
        state = status.get("state", "idle")
        if status.get("cooldown"):
            return f"后台冷却 {math.ceil(max(0, status.get('retry_in', 0)))}s · 已准备内容仍可用"
        if status.get("retrying"):
            remaining = math.ceil(max(0, status.get("retry_in", 0)))
            attempt, total = status.get("retry_attempt", 1), status.get("retry_total", 3)
            elapsed = math.floor(status.get("retry_elapsed", 0))
            return f"后台重试 {attempt}/{total} · {remaining}s 后 · 已耗时 {elapsed}s"
        if state == "loading":
            return "后台正在准备后续内容"
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def skip_content(self) -> None:
        if self.masked:
            return
        method = getattr(self.engine, "skip_work", None)
        if method is None:
            method = getattr(self.engine, "skip", None)
        if method:
            self.queue_operation("正在跳过本作剩余章节…", method, "reader")

    def cancel_pending(self, *, invalidate: bool = False, allow_retry: bool = True,
                       notice: str = "已取消等待，当前位置保留") -> None:
        self._operation_serial += 1
        if self._operation_task and not self._operation_task.done():
            self._operation_task.cancel()
        cancel_engine = getattr(self.engine, "cancel_foreground", None) if invalidate else None
        if cancel_engine is None:
            cancel_engine = getattr(self.engine, "pause_prefetch", None)
        if cancel_engine is not None:
            cancel_engine()
        self.busy = False
        self._retry_pending = allow_retry
        if not allow_retry:
            self._retry = None
        self._progress = None
        self._recovery = None
        self.set_local_notice(notice)
        if self.mode == "reader":
            self.focus_main(self.reader)
        self.update_footer()

    def _set_mode(self, mode: str) -> None:
        self.mode = mode
        self.main_query("#command-menu").display = False
        for value in ("reader", "search", "results", "error", "info", "storage"):
            self.main_query("#reader" if value == "reader" else f"#{value}-panel").display = value == mode
        self.update_footer()

    def save_position(self) -> None:
        if self.mode == "reader" and self.engine.current and self._displayed_url == self.engine.current.url:
            self.engine.set_position(self.reader.anchor())
            self.schedule_save()

    def on_reader_viewport_position_changed(self, message: ReaderViewport.PositionChanged) -> None:
        if not self.is_running:
            return
        if message.viewport is self.reader:
            if not self.masked:
                self.save_position()
            self.update_footer()

    def update_footer(self) -> None:
        if not self._ui_mounted or not self.is_running:
            return
        chapter = self.engine.current
        engine_notice = self._engine_footer_notice()
        storage_notice = str(getattr(self.engine, "storage_notice", ""))
        above = self.operation_status() or storage_notice or self._local_notice or engine_notice or self.prefetch_label()
        self.main_query("#above-input", Static).update(Text(above, overflow="ellipsis", no_wrap=True))
        width = max(1, self.size.width - 2)
        if chapter:
            work = chapter.work
            count = work.chapter_total or work.chapter_count or "?"
            suffix = f"第{chapter.position}/{count}章 · 本章进度{self.reader.percent}%"
            if cell_len(suffix) > width:
                suffix = f"{chapter.position}/{count}章 {self.reader.percent}%"
            title_space = width - cell_len(suffix) - 2
            title = work.title
            if cell_len(title) > title_space:
                title = set_cell_size(title, max(0, title_space - 1)).rstrip() + "…" if title_space > 1 else ""
            footer = (title + "  " if title else "") + suffix
        else:
            footer = "尚未打开作品 · /search 搜索"
        self.main_query("#footer-first", Static).update(Text(footer, overflow="crop", no_wrap=True))
        if self.command_menu_open:
            hints = "↑↓ 选择 · Tab 补全 · Enter 执行 · Esc 关闭"
        elif self.mode == "reader":
            hints = self.reading_hints(width)
        elif self.mode == "search":
            hints = "↑↓ 选择 · 空格勾选" if self.main_query("#search-tabs", Tabs).active != "tab-search" else "/ 命令 · Ctrl+G 切换"
        elif self.mode == "storage":
            hints = "清理保留当前章节、进度与配置"
        else:
            hints = "↑↓ 选择 · Enter 确认 · Esc 返回"
        if self._recovery:
            hints = "r 或 /retry 重试 · /search 搜索 · Ctrl+G 切换"
        self.main_query("#footer-second", Static).update(hints)

    def reading_hints(self, width: int) -> str:
        candidates = (
            "n 下一章/篇 · p 上一章/篇 · ] 跳过本作 · / 命令 · Ctrl+G 秒切工作",
            "n/p 前后章 · ]跳作 · / · Ctrl+G 秒切工作",
            "n/p · ]跳作 · / · Ctrl+G 秒切工作",
        )
        return next((hint for hint in candidates if cell_len(hint) <= width), "n/p · ] · ^G")

    def operation_status(self) -> str:
        now = time.monotonic()
        if self._progress:
            progress = self._progress
            elapsed = math.floor(now - progress["started"])
            attempt = progress["attempt"]
            reason = progress.get("reason", "")
            prefix = reason + " · " if reason else ""
            if progress.get("retry_at"):
                delay = math.ceil(max(0, progress["retry_at"] - now))
                return f"{prefix}重试 {attempt}/{self.RETRY_ATTEMPTS} · {delay}s后 · 耗时{elapsed}s · Esc取消"
            prepared = getattr(self.engine, "prefetch_status", {})
            if prepared.get("retrying"):
                return self.prefetch_label().removeprefix("后台") + " · Esc 取消"
            health = getattr(getattr(self.engine, "source", None), "health", {})
            phase = {"queued": "排队", "reading": "接收", "cooldown": "冷却", "interval_wait": "节流等待"}.get(health.get("state"), "请求")
            return f"{prefix}{phase} {attempt}/{self.RETRY_ATTEMPTS} · 耗时{elapsed}s · Esc取消"
        if self._recovery:
            reason = self._recovery.get("reason", "暂未完成")
            remaining = math.ceil(max(0, self._recovery.get("cooldown_until", 0) - now))
            if remaining:
                return f"{reason} · 冷却 {remaining}s · 到时可 /retry"
            return f"{reason} · {self._recovery.get('attempts', 1)}/{self.RETRY_ATTEMPTS} 次 · r 或 /retry 重试"
        return ""

    def _engine_footer_notice(self) -> str:
        """Observe state changes once; ordinary repainting never renews the toast."""
        notice = str(getattr(self.engine, "notice", ""))
        if notice != self._observed_engine_notice:
            self._observed_engine_notice = notice
            if self._toast_timer is not None:
                self._toast_timer.stop()
                self._toast_timer = None
            self._toast_text = ""
            if notice:
                self._toast_text = notice
                self._toast_timer = self.set_timer(self.TOAST_SECONDS, self._expire_engine_toast)
        return self._toast_text

    def _expire_engine_toast(self) -> None:
        # Keep loading/error notices and the independent reading progress intact.
        self._toast_text = ""
        self._toast_timer = None
        self.update_footer()

    def set_local_notice(self, value: str, *, persistent: bool = False) -> None:
        """Information yields to live readiness; loading and errors remain explicit."""
        if self._local_notice_timer is not None:
            self._local_notice_timer.stop()
            self._local_notice_timer = None
        self._local_notice = value
        if value and not persistent:
            self._local_notice_timer = self.set_timer(self.TOAST_SECONDS, self._expire_local_notice)

    def _expire_local_notice(self) -> None:
        self._local_notice_timer = None
        self._local_notice = ""
        self.update_footer()

    def _restore_search_form(self) -> None:
        """Apply asynchronously restored filters once, without replacing an in-flight draft."""
        query = self.main_query("#any-field", Input)
        warnings = self.main_query("#warning-options", SearchSelection)
        categories = self.main_query("#category-options", SearchSelection)
        form = (query.value, set(warnings.selected), set(categories.selected))
        if form != self._initial_search_form:
            return
        filters = self.engine.filters
        draft = getattr(self.engine, "search_draft", {}) or {}
        self._restoring_form = True
        query.value = str(draft.get("query", filters.query))
        warnings.deselect_all()
        categories.deselect_all()
        for value in draft.get("warnings", filters.warnings):
            if value in WARNING_OPTIONS:
                warnings.select(value)
        for value in draft.get("categories", filters.categories):
            if value in CATEGORY_OPTIONS:
                categories.select(value)
        self._restoring_form = False
        self.remember_search_draft()

    def _sync_chapter(self) -> None:
        chapter = self.engine.current
        if chapter is None:
            self.reader.set_document((getattr(self.engine, "notice", "") or "暂无可读内容。使用 /search 搜索作品。",))
            return
        self._displayed_url = chapter.url
        self.reader.set_document(chapter.paragraphs, title="", section=chapter.position, anchor=self.engine.position)

    def queue_operation(self, label: str, operation: Callable[[], Coroutine], success_mode: str, *, save: bool = True) -> None:
        if self.busy or self._exit_requested:
            self.set_local_notice("当前请求尚未结束；Ctrl+G 仍可切换", persistent=True)
            self.update_footer()
            return
        if save:
            self.save_position()
        self._recovery = None
        self._retry_pending = False
        self._operation_serial += 1
        serial = self._operation_serial
        self._progress = {"started": time.monotonic(), "attempt": 1, "retry_at": 0}
        self.busy = True
        self.set_local_notice(label, persistent=True)
        self._retry = (label, operation, success_mode)
        self.update_footer()
        self._operation_task = asyncio.create_task(self._perform(operation, success_mode, serial))

    async def _perform(self, operation: Callable[[], Coroutine], success_mode: str, serial: int) -> None:
        try:
            await self._run_with_retries(operation)
            if serial != self._operation_serial:
                return
            if operation == self.engine.start:
                self._restore_search_form()
            self.last_error = None
            self.set_local_notice("", persistent=True)
            if success_mode == "results":
                self._fill_results()
                self._set_mode("results")
                self.focus_main(self.main_query("#results-list"))
            else:
                self._sync_chapter()
                self._set_mode("reader")
                self.focus_main(self.reader)
                if self.engine.current and hasattr(self.engine, "prefetch"):
                    if self._prefetch_task is None or self._prefetch_task.done():
                        self._prefetch_task = asyncio.create_task(self._prefetch())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if serial == self._operation_serial:
                self._retry_pending = True
                self.show_error(exc)
        finally:
            if serial == self._operation_serial:
                self.busy = False
                self._progress = None
                if self.is_mounted:
                    self.update_footer()

    @staticmethod
    def is_transient(exc: Exception) -> bool:
        return bool(getattr(exc, "retryable", getattr(exc, "code", "") in {"network", "unavailable", "rate_limited", "timeout"}))

    @staticmethod
    def failure_reason(exc: Exception) -> str:
        """An allowlisted classification, never a raw exception or proxy URL."""
        status = getattr(exc, "http_status", None)
        if isinstance(status, int) and 100 <= status <= 599:
            return str(status)
        kind = getattr(exc, "failure_kind", None) or getattr(exc, "code", "")
        return {"read_timeout": "读取超时", "connect_timeout": "连接超时", "pool_timeout": "连接等待", "operation_timeout": "等待超时", "network": "连接失败", "timeout": "请求超时", "rate_limited": "访问限流", "unavailable": "服务暂忙", "circuit_open": "连接冷却", "dns_error": "域名解析失败", "proxy_dns_error": "代理域名错误", "proxy_error": "代理连接失败", "tls_certificate_error": "证书校验失败", "tls_handshake_error": "TLS 握手失败", "tls_ca_error": "本机证书配置错误"}.get(kind, "请求未完成")

    async def _run_with_retries(self, operation: Callable[[], Coroutine]) -> None:
        """One front-end operation window; source retries are disabled by the CLI."""
        started = time.monotonic()
        attempt = 1
        reason = ""
        while True:
            self._progress = {"started": started, "attempt": attempt, "retry_at": 0, "reason": reason}
            self.update_footer()
            try:
                remaining = self.RETRY_BUDGET - (time.monotonic() - started)
                if remaining <= 0:
                    raise asyncio.TimeoutError
                await asyncio.wait_for(operation(), timeout=remaining)
                return
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                exc = SourceError("network", "本次等待已到时间预算，当前位置保留。")
                exc.retryable = True
                exc.retry_budget_exhausted = True
                exc.failure_kind = "operation_timeout"
            except Exception as error:
                exc = error
            # A promoted prefetch worker already spends its own bounded attempt
            # window. Never add a second three-attempt loop after that worker.
            spent = max(attempt, int(getattr(exc, "attempts", 0) or 0))
            reason = self.failure_reason(exc)
            exc.ui_attempts = min(spent, self.RETRY_ATTEMPTS)
            if not self.is_transient(exc) or getattr(exc, "retry_budget_exhausted", False) or spent >= self.RETRY_ATTEMPTS:
                raise exc
            delay = max(float(getattr(exc, "retry_after", 0) or 0), self.RETRY_DELAYS[min(spent - 1, len(self.RETRY_DELAYS) - 1)])
            remaining = self.RETRY_BUDGET - (time.monotonic() - started)
            if delay >= remaining:
                exc.retry_budget_exhausted = True
                raise exc
            attempt = spent + 1
            self._progress = {"started": started, "attempt": attempt, "retry_at": time.monotonic() + delay, "reason": reason}
            self.update_footer()
            await asyncio.sleep(delay)

    async def _prefetch(self) -> None:
        try:
            while not self._exit_requested and self.engine.current:
                position = self.engine.current.url
                await self.engine.prefetch()
                if (
                    self._exit_requested or self.busy or not self.engine.current
                    or self.engine.current.url == position
                    or getattr(self.engine, "prefetch_status", {}).get("paused")
                ):
                    break
        except (Exception, asyncio.CancelledError):
            pass  # Speculation never replaces a valid visible chapter with an error.
        finally:
            if self._ui_mounted:
                self.update_footer()

    def show_error(self, exc: Exception) -> None:
        self.last_error = exc
        self.error_code = str(getattr(exc, "code", "unexpected"))
        if self.is_transient(exc):
            retry_after = float(getattr(exc, "retry_after", 0) or 0)
            self._recovery = {"attempts": getattr(exc, "ui_attempts", 1), "cooldown_until": time.monotonic() + retry_after, "reason": self.failure_reason(exc)}
            self.set_local_notice("", persistent=True)
            if self.engine.current is None:
                self.reader.set_document(("暂未连接到可读内容。已保留搜索条件；稍后按 r 重试，或输入 /search 修改条件。", str(exc)))
            if self.mode == "reader":
                self.focus_main(self.reader)
            self.update_footer()
            return
        self.set_local_notice("请求未完成，原内容与草稿已保留", persistent=True)
        can_confirm = bool(
            self.error_code == "adult_confirmation"
            and getattr(exc, "confirmation_url", None)
            and hasattr(self.engine, "confirm_content_warning")
            and self._retry_pending and self._retry
        )
        self.main_query("#error-confirm", Button).display = can_confirm
        self.main_query("#error-retry", Button).display = not can_confirm
        labels = {"adult_confirmation": "此作品有内容提示", "login_required": "此作品要求登录原站", "forbidden": "书源拒绝访问", "rate_limited": "书源暂时限流", "unavailable": "书源暂时不可用"}
        message = labels.get(self.error_code, "无法获取内容") + f"\n\n{exc}\n\n不会将错误或提示页作为正文。可重试、在原站查看、返回或明确跳过。"
        if can_confirm:
            message = (
                "此作品可能包含成人内容\n\n"
                "书源要求你确认是否愿意查看。选择「确认并继续」后，将在终端继续打开这部作品。\n\n"
                "本次运行中，同一作品后续章节无需重复确认；其他作品仍单独处理。\n\n"
                f"目标：{exc.confirmation_url}"
            )
            self.set_local_notice("等待你确认当前作品的内容提示", persistent=True)
            if self.engine.current is None:
                self.reader.set_document(("尚未打开作品。按 r 返回内容提示，或输入 /search 搜索其他作品。",))
        if self.engine.current is None and not can_confirm:
            self.reader.set_document((labels.get(self.error_code, "无法获取内容") + "。", str(exc), "输入 /search 搜索其他作品。需要登录或内容确认时请在原站自行处理。"))
            self._set_mode("reader")
            self.focus_main(self.reader)
            return
        self.main_query("#error-message", Static).update(message)
        self.main_query("#error-skip", Button).disabled = self.engine.current is None or not hasattr(self.engine, "skip")
        self._set_mode("error")
        self.focus_main(self.main_query("#error-back"))

    def action_retry(self) -> None:
        if self.masked or self.busy:
            return
        if self._recovery and self._recovery.get("cooldown_until", 0) > time.monotonic():
            self.update_footer()
            return
        if self._retry_pending and self._retry:
            self.queue_operation(*self._retry)
        elif getattr(self.engine, "prefetch_status", {}).get("errors") or getattr(self.engine, "prefetch_status", {}).get("state") == "error" or getattr(self.engine, "prefetch_status", {}).get("paused"):
            retry = getattr(self.engine, "retry_prefetch", None)
            if retry is not None and (self._prefetch_task is None or self._prefetch_task.done()):
                self._prefetch_task = asyncio.create_task(self._retry_background(retry))
        else:
            self.set_local_notice("当前没有未完成的请求")
            self.update_footer()

    def confirm_content_warning(self) -> None:
        if self.busy or self.masked or not self._retry_pending or not self._retry:
            return
        url = getattr(self.last_error, "confirmation_url", None)
        if self.error_code != "adult_confirmation" or not url:
            return
        try:
            self.engine.confirm_content_warning(url)
        except SourceError as exc:
            self.show_error(exc)
            return
        # Retry the original operation: open_result keeps its search/series
        # queue, and next/previous keep the original chapter direction.
        self._set_mode("reader")
        self.focus_main(self.reader)
        self.action_retry()

    async def _retry_background(self, retry: Callable[[], Coroutine]) -> None:
        try:
            await retry()
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            self.update_footer()

    def navigate(self, direction: int) -> None:
        if self.masked:
            return
        method = self.engine.next if direction > 0 else self.engine.previous
        status = getattr(self.engine, "prefetch_status", {})
        label = "正在读取已准备的内容…" if status.get("ready", 0) else "下一内容准备中… Esc 取消等待"
        self.queue_operation(label if direction > 0 else "正在加载前序内容…", method, "reader")

    def action_toggle_mask(self) -> None:
        if self.masked:
            self.masked = False
            self.pop_screen()
            focus = self._masked_focus
            # Preserve the exact old focus if its view still exists; otherwise use the completed operation's target.
            if self.mode == self._masked_mode and focus is not None and focus.is_attached and focus.display:
                self.call_after_refresh(focus.focus)
            elif self._deferred_focus is not None:
                self.call_after_refresh(self._deferred_focus.focus)
            self._deferred_focus = None
        else:
            self.save_position()
            self._masked_focus = self.focused
            self._masked_mode = self.mode
            self.masked = True
            self.push_screen(WorkMask())

    def action_back(self) -> None:
        if self.masked:
            return
        if self.command_menu_open:
            self._command_menu_dismissed = self.main_query("#command", Input).value
            self.main_query("#command-menu").display = False
            return
        if self.mode == "search":
            self.return_to_reader()
            return
        if self.busy:
            self.cancel_pending()
            return
        if self.mode == "results":
            self.open_search()
        elif self.mode != "reader":
            self._set_mode("reader")
            self.focus_main(self.reader)
        else:
            self.focus_main(self.reader)

    def return_to_reader(self) -> None:
        if self.mode == "search":
            self.remember_search_draft()
        if self.busy:
            leaving_search = self.mode in ("search", "results")
            self.cancel_pending(invalidate=leaving_search, allow_retry=not leaving_search,
                                notice="已取消搜索，正文与草稿保留" if leaving_search else "已取消等待，当前位置保留")
        self._set_mode("reader")
        self.focus_main(self.reader)

    def open_search(self) -> None:
        self.save_position()
        self._set_mode("search")
        active = self.main_query("#search-tabs", Tabs).active or "tab-search"
        self._search_tab(active)

    def _search_tab(self, tab_id: str) -> None:
        help_text = "" if tab_id == "tab-search" else "勾选项须同时包含，并非排除"
        self.main_query("#filter-help", Static).update(help_text)
        self.main_query("#filter-help").display = tab_id != "tab-search"
        self.main_query("#query-label").display = tab_id == "tab-search"
        self.main_query("#any-field").display = tab_id == "tab-search"
        self.main_query("#warning-options").display = tab_id == "tab-warnings"
        self.main_query("#category-options").display = tab_id == "tab-categories"
        if self.mode == "search":
            target = {"tab-search": "#any-field", "tab-warnings": "#warning-options", "tab-categories": "#category-options"}[tab_id]
            self.focus_main(self.main_query(target))

    @on(Tabs.TabActivated, "#search-tabs")
    def search_tab_changed(self, event: Tabs.TabActivated) -> None:
        self._search_tab(event.tab.id or "tab-search")

    def action_submit_search(self) -> None:
        old = self.engine.filters
        filters = SearchFilters(query=self.main_query("#any-field", Input).value, warnings=list(self.main_query("#warning-options", SearchSelection).selected), categories=list(self.main_query("#category-options", SearchSelection).selected), rating=old.rating, language="zh")
        self.queue_operation("正在搜索…", lambda: self.engine.search(filters), "results")

    def _fill_results(self) -> None:
        page = self.engine.results
        options = self.main_query("#results-list", ResultOptions)
        self._filling_results = True
        options.clear_options()
        items = page.items if page else []
        for work in items:
            text = Text(work.title + "\n", style="bold")
            text.append(" · ".join(work.authors) or "未署名作者", style="dim")
            text.append(f" · {work.chapter_count or '–'} 章\n")
            translations = {**{value: CATEGORY_LABELS[key] for key, value in CATEGORY_OPTIONS.items()}, **{value: WARNING_LABELS[key] for key, value in WARNING_OPTIONS.items()}}
            text.append("，".join(translations.get(label, label) for label in work.categories + work.warnings), style="dim")
            options.add_option(text)
        if items:
            options.highlighted = min(int(getattr(self.engine, "results_selection", 0)), len(items)-1)
        page_number = getattr(self.engine, "results_page_number", 1)
        self.main_query("#results-summary", Static).update(f"第 {page_number} 页 · {len(items)} 部作品 · {self.engine.filters.query or '最近更新'}" if items else "没有匹配作品。可修改条件或返回当前内容。")
        self.main_query("#results-more", Button).disabled = not getattr(self.engine, "has_next_results", bool(page and page.next_url))
        self.main_query("#results-previous", Button).disabled = not getattr(self.engine, "has_previous_results", False)
        self.call_after_refresh(self._finish_results_fill)

    def _finish_results_fill(self) -> None:
        self._filling_results = False

    def _remember_result(self) -> None:
        index = self.main_query("#results-list", ResultOptions).highlighted
        if index is not None and hasattr(self.engine, "set_result_selection"):
            self.engine.set_result_selection(index)

    @on(OptionList.OptionHighlighted, "#results-list")
    def result_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if not self._filling_results:
            self._remember_result()

    def action_more_results(self) -> None:
        if getattr(self.engine, "has_next_results", bool(self.engine.results and self.engine.results.next_url)):
            self._remember_result()
            operation = getattr(self.engine, "next_results", self.engine.more_results)
            self.queue_operation("正在读取下一页…", operation, "results")

    def action_previous_results(self) -> None:
        if getattr(self.engine, "has_previous_results", False):
            self._remember_result()
            self.queue_operation("正在读取上一页…", self.engine.prev_results, "results")

    @on(OptionList.OptionSelected, "#results-list")
    def result_selected(self, event: OptionList.OptionSelected) -> None:
        self._remember_result()
        self.schedule_save()
        self.queue_operation("正在打开作品…", lambda: self.engine.open_result(event.option_index), "reader")

    @on(Input.Submitted)
    def input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "any-field":
            self.action_submit_search()
            return
        if event.input.id != "command":
            return
        value = event.value
        event.input.value = ""
        self.execute_command(value)

    def execute_command(self, value: str) -> None:
        self.main_query("#command-menu").display = False
        command, _, argument = value.strip().partition(" ")
        if self.mode == "search" and self.busy and command in {
            "/title", "/results", "/storage", "/cache", "/series", "/help", "/",
            "/next", "/prev", "/skip-work",
        }:
            self.remember_search_draft()
            self.cancel_pending(invalidate=True, allow_retry=False,
                                notice="已取消搜索，正文与草稿保留")
        if command == "/search":
            self.open_search()
        elif command == "/retry":
            self.action_retry()
        elif command == "/skip-work":
            self.skip_content()
        elif command in ("/storage", "/cache"):
            self.open_storage()
        elif command == "/language":
            self.set_local_notice("当前仅支持中文（普通话），无需切换语言")
            self.update_footer()
        elif command in ("/next", "/prev"):
            self.navigate(1 if command == "/next" else -1)
        elif command == "/results":
            restore = getattr(self.engine, "restore_results", None)
            page = restore() if restore else self.engine.results
            if page is None:
                self.open_search()
                self.set_local_notice("尚无最近搜索结果；先搜索作品")
                self.update_footer()
            else:
                self._fill_results()
                self._set_mode("results")
                self.focus_main(self.main_query("#results-list"))
        elif command in ("/title", "/series"):
            # Retired UI entry points retain only a safe return for old scripts.
            # Do not toggle the document, select a series or issue a request.
            self.return_to_reader()
            if not self._local_notice:
                self.set_local_notice("此入口已移除；书名见页脚，使用 /search 搜索作品")
            self.update_footer()
        elif command == "/theme":
            self.set_color_mode(argument or ("light" if self.dark_palette else "dark"))
        elif command == "/source":
            self.set_local_notice("当前书源：" + (urlsplit(self.source_url).hostname or "未配置"))
            self.update_footer()
        elif command == "/open":
            self.set_local_notice("链接入口已移除；请使用 /search 搜索作品")
            self.update_footer()
        elif command in ("/help", "/"):
            self.show_info("Claude 凹3 · 终端摸鱼阅读器\n\n/search 搜索中文作品 · /next 下一章/篇\n/prev 上一章/篇 · /results 最近搜索\n/skip-work 跳过本作剩余章节\n/storage 存储管理\n/retry 或 r 重试未完成请求 · Esc 取消等待\n/theme auto|dark|light 主题 · /quit 退出\n\n作品可以有很多章；系列是作者组织的多部作品。\nn 下一章/篇 · p 上一章/篇\n] 跳过本作：有系列时读同系列下一部；否则回搜索/近期。\n命令菜单 ↑↓ 选择；Tab 补全；Enter 执行。\n上下滚动；边界松开后独立操作才切换。\nCtrl+G 切换工作界面；Ctrl+C 保存并退出。\n工作界面是本地预置便笺，不执行开发任务。")
        elif command == "/quit":
            self.action_exit_reader()
        elif command:
            self.set_local_notice("未知命令；输入 /help 查看")
            self.update_footer()

    def show_info(self, text: str) -> None:
        self.save_position()
        self.main_query("#info-message", Static).update(text)
        self._set_mode("info")
        self.focus_main(self.main_query("#info-back"))

    @staticmethod
    def _mib(value: int | float) -> str:
        return f"{value / (1024 * 1024):.2f} MiB"

    def refresh_storage(self) -> None:
        method = getattr(getattr(self.engine, "store", None), "storage_info", None)
        clearable = callable(getattr(self.engine, "clear_prepared_cache", None))
        self.main_query("#storage-clear", Button).disabled = not clearable
        if not callable(method):
            self.main_query("#storage-message", Static).update("存储管理\n当前会话未提供本地存储统计。")
            return
        try:
            info = method()
            limits = info.get("limits", {})
            size = self._mib
            rows = ["存储管理 · 当前书源", "",
                f"数据库文件  {size(info.get('db_bytes', 0))} / {size(limits.get('database_bytes', 0))}",
                f"数据库附属文件  {size(sum(info.get(key, 0) for key in ('journal_bytes', 'wal_bytes', 'shm_bytes')))}",
                f"临时缓存  {size(info.get('cache_payload_bytes', 0))} / {size(limits.get('cache_bytes', 0))}",
                f"缓存条目  {info.get('cache_entries', 0)} / {limits.get('cache_entries', 0)}",
                f"当前会话  {size(info.get('session_bytes', 0))} / {size(limits.get('session_bytes', 0))}",
                f"偏好设置  {size(info.get('preferences_bytes', 0))} / {size(limits.get('preferences_bytes', 0))}"]
            summary_method = getattr(self.engine, "storage_summary", None)
            if callable(summary_method):
                summary = summary_method()
                rows += ["", f"全部书源  {summary.get('database_count', 0)} 个数据库",
                    f"持久数据库  {size(summary.get('database_bytes', 0))} / {size(summary.get('persistent_budget_bytes', 0))}",
                    f"含附属文件  {size(summary.get('managed_file_bytes', 0))} / {size(summary.get('managed_budget_bytes', 0))}（工程预算）"]
            rows += ["", "清理准备内容与磁盘临时缓存；保留当前章节、进度、搜索和配置。", "清理后暂停预取，继续阅读或 /retry 恢复。当前正文与配置仍占用空间。"]
            if info.get("legacy_oversized"):
                rows += ["", "旧会话或偏好超过新限额，原记录保留；需保存符合限额的新记录。"]
            last_error = info.get("last_error") or {}
            if last_error:
                rows += ["", f"最近存储错误：{last_error.get('code', 'storage_error')} / {last_error.get('scope', '本地存储')}"]
            storage_notice = getattr(self.engine, "storage_notice", "")
            if storage_notice:
                rows += ["", str(storage_notice)]
            self.main_query("#storage-message", Static).update("\n".join(rows))
        except Exception:
            self.main_query("#storage-message", Static).update("存储统计暂不可用；当前阅读位置保留。")

    def open_storage(self) -> None:
        self.save_position()
        self.refresh_storage()
        self._set_mode("storage")
        self.focus_main(self.main_query("#storage-clear"))

    def clear_storage(self) -> None:
        method = getattr(self.engine, "clear_prepared_cache", None)
        if not callable(method):
            return
        if self.busy:
            self.cancel_pending()
        try:
            result = method()
            if result.get("cleared"):
                self.set_local_notice("准备内容与临时缓存已清；预取暂停")
            else:
                self.set_local_notice(getattr(self.engine, "storage_notice", "") or "缓存清理未完成，请重试", persistent=True)
        except Exception:
            self.set_local_notice("缓存清理未完成；当前章节与配置保留", persistent=True)
        self.refresh_storage()
        self.update_footer()

    def set_color_mode(self, mode: str) -> None:
        if mode not in ("auto", "dark", "light"):
            self.set_local_notice("主题可选 auto、dark、light")
            self.update_footer()
            return
        self.theme_mode = mode
        self.dark_palette = auto_dark() if mode == "auto" else mode == "dark"
        self.terminal_palette = palette_for(mode, self.color_capability, self.dark_palette)
        self._register_themes()
        self.theme = "mini-host" if mode == "auto" else "mini-" + mode
        self.reader.invalidate_colors()
        if self.masked:
            self.screen.query_one("#mask-body", ReaderViewport).invalidate_colors()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        identity = event.button.id
        if identity == "search-submit":
            self.action_submit_search()
        elif identity in ("search-back", "results-back", "error-back", "info-back", "storage-back"):
            self.return_to_reader()
        elif identity == "results-search":
            self.open_search()
        elif identity == "results-more":
            self.action_more_results()
        elif identity == "results-previous":
            self.action_previous_results()
        elif identity == "storage-clear":
            self.clear_storage()
        elif identity == "storage-refresh":
            self.refresh_storage()
        elif identity == "error-confirm":
            self.confirm_content_warning()
        elif identity == "error-retry" and self._retry:
            self.action_retry()
        elif identity == "error-source":
            url = str(getattr(self.last_error, "url", "") or getattr(self.engine, "pending_url", "") or self.source_url)
            webbrowser.open(url)
        elif identity == "error-skip" and hasattr(self.engine, "skip"):
            self.queue_operation("正在跳过…", self.engine.skip, "reader")

    def action_exit_reader(self) -> None:
        if not self._exit_requested:
            self._exit_requested = True
            asyncio.create_task(self._close_reader())

    async def _close_reader(self) -> None:
        if self._save_timer is not None:
            self._save_timer.stop()
            self._save_timer = None
        self.save_position()
        for task in (self._operation_task, self._prefetch_task):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (self._operation_task, self._prefetch_task) if task), return_exceptions=True)
        try:
            await asyncio.wait_for(self.engine.close(), timeout=4)
        except Exception as exc:
            self.exit(message=f"关闭时保存或连接清理失败：{exc}", return_code=2)
        else:
            self.exit()


ReaderApp = MiniNotesApp
