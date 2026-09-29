"""终端呈现层：彩色、面板、表格与实时进度（README 第 5.6 节）。

四条硬约束：

1. **核心零依赖**：模块顶层绝不 import 第三方库。``rich`` 只在真的要渲染时才惰性
   导入，缺了就用等价的内置纯文本实现 —— 功能、退出码、产物完全一致；
2. **机读输出不受影响**：``--json`` 与 ``_runs/*.json`` 走另一条分支，不经过本模块；
3. **非 TTY 自动降级**：重定向到文件/管道时不写 ANSI、不画进度条，输出保持可 grep；
4. **美化失败不影响退出码**：任何渲染异常都一次性降级为纯文本并继续跑完。

样式词汇统一用"语气"（tone）表达，``cli.py`` 只写一份版式，两套皮肤各自渲染：

===========  ====================  ==========================
tone         纯文本                rich
===========  ====================  ==========================
``ok``       原样                 绿色
``warn``     原样                 黄色
``error``    原样                 粗体红
``dim``      原样                 暗色
``info``     原样                 青色
``title``    原样                 粗体青
===========  ====================  ==========================
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import unicodedata

#: 输出宽度上限/下限（非 TTY 固定用 DEFAULT_WIDTH，保证重定向结果稳定）
MIN_WIDTH = 60
MAX_WIDTH = 140
DEFAULT_WIDTH = 100

#: 进度条刷新频率（每秒）；过高会拖慢下载线程
REFRESH_PER_SECOND = 8

#: 表格里"枚举列"的宽度底限：整体不超过这个宽度的列不参与压缩，
#: 免得 ``partial``/``dynamic`` 被截成 ``pa...`` 把信息丢掉
COLUMN_FLOOR = 12

#: 语气 → rich 样式。纯文本皮肤只忽略样式，不改动文字本身。
TONE_STYLES: dict[str, str] = {
    "": "",
    "ok": "green",
    "warn": "yellow",
    "error": "bold red",
    "dim": "dim",
    "muted": "dim",
    "info": "cyan",
    "title": "bold cyan",
    "accent": "magenta",
}

#: 语气 → ANSI 码。纯文本皮肤自己上色（这样没装 rich 也能有颜色），
#: 但 ``--color=never`` / 非 TTY / 旧控制台下一律不写转义。
ANSI_CODES: dict[str, str] = {
    "ok": "\x1b[32m",
    "warn": "\x1b[33m",
    "error": "\x1b[1;31m",
    "dim": "\x1b[2m",
    "muted": "\x1b[2m",
    "info": "\x1b[36m",
    "title": "\x1b[1;36m",
    "accent": "\x1b[35m",
}
ANSI_RESET = "\x1b[0m"

#: 结果状态 → (rich 符号, 纯文本符号)。纯文本一律 ASCII：cp936 重定向下不会崩。
FLAGS: dict[str, tuple[str, str]] = {
    "done": ("✔", "[ok]"),
    "partial": ("~", "[~]"),
    "skipped": ("=", "[=]"),
    "failed": ("✘", "[x]"),
    "denied": ("✘", "[x]"),
    "invisible": ("✘", "[x]"),
    "not_found": ("✘", "[x]"),
}
UNKNOWN_FLAG = ("?", "[?]")

#: 结果状态 → 语气（运行摘要的配色依据）
OUTCOME_TONES: dict[str, str] = {
    "done": "ok",
    "partial": "warn",
    "skipped": "dim",
    "failed": "error",
    "denied": "error",
    "invisible": "error",
    "not_found": "error",
}


# --------------------------------------------------------------------------- #
# 能力判定
# --------------------------------------------------------------------------- #
_VT_STATE: dict[int, bool] = {}


def ansi_capable(stream) -> bool:
    """该流能否安全接收 ANSI 转义（Windows 旧控制台需要显式打开 VT）。"""
    if os.name != "nt":
        return True
    try:
        fd = stream.fileno()
    except Exception:
        # 不是真实文件描述符（StringIO、测试替身）：没有"旧控制台乱码"的问题
        return True
    if fd in _VT_STATE:
        return _VT_STATE[fd]
    _VT_STATE[fd] = _enable_vt(fd)
    return _VT_STATE[fd]


def _enable_vt(fd: int) -> bool:
    """打开 ENABLE_VIRTUAL_TERMINAL_PROCESSING；失败返回 False（不抛异常）。"""
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        handle = msvcrt.get_osfhandle(fd)
        kernel32 = ctypes.windll.kernel32
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_vt = 0x0004
        if mode.value & enable_vt:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | enable_vt))
    except Exception:  # pragma: no cover - 平台相关，失败即退让
        return False


def _isatty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _env_forced() -> bool:
    """``CLICOLOR_FORCE`` 非空且不为 0 时强制彩色（业界通行约定）。"""
    return str(os.environ.get("CLICOLOR_FORCE") or "").strip() not in ("", "0")


def resolve_color(mode: str, stream) -> bool:
    """是否输出彩色。

    优先级：``never`` > ``always`` > ``NO_COLOR`` > ``CLICOLOR_FORCE`` > TTY 探测。
    ``always`` 会覆盖 ``NO_COLOR``（用户显式在命令行上要求了就按他说的做），
    但"真控制台且打不开 VT"这种情况仍然退让 —— 否则屏幕上全是乱码。
    """
    mode = (mode or "auto").strip().lower()
    if mode == "never":
        return False
    is_tty = _isatty(stream)
    if mode == "always":
        if os.name == "nt" and is_tty:
            return ansi_capable(stream)
        return True
    if str(os.environ.get("NO_COLOR") or ""):
        return False
    if _env_forced():
        return ansi_capable(stream) if (os.name == "nt" and is_tty) else True
    if not is_tty:
        return False
    if str(os.environ.get("TERM") or "").strip().lower() == "dumb":
        return False
    return ansi_capable(stream) if os.name == "nt" else True


def terminal_width(stream=None) -> int:
    """输出宽度：非 TTY 固定 DEFAULT_WIDTH，避免重定向结果随终端变化。"""
    try:
        columns = shutil.get_terminal_size((DEFAULT_WIDTH, 25)).columns
    except Exception:
        columns = DEFAULT_WIDTH
    if stream is not None and not _isatty(stream):
        columns = min(columns, DEFAULT_WIDTH)
    return max(MIN_WIDTH, min(MAX_WIDTH, int(columns)))


def load_rich():
    """惰性导入 rich 的渲染件；不可用返回 ``None``（绝不抛给调用方）。"""
    try:
        from rich import box
        from rich.console import Console
        from rich.panel import Panel
        from rich.progress import (
            BarColumn,
            DownloadColumn,
            Progress,
            SpinnerColumn,
            TaskProgressColumn,
            TextColumn,
            TimeElapsedColumn,
        )
        from rich.rule import Rule
        from rich.table import Table
        from rich.text import Text
    except Exception:
        return None
    return {
        "box": box,
        "Console": Console,
        "Panel": Panel,
        "Progress": Progress,
        "Rule": Rule,
        "Table": Table,
        "Text": Text,
        "bar": BarColumn,
        "download": DownloadColumn,
        "spinner": SpinnerColumn,
        "task_progress": TaskProgressColumn,
        "text_column": TextColumn,
        "elapsed": TimeElapsedColumn,
    }


# --------------------------------------------------------------------------- #
# 宽度感知的排版工具（中文/emoji 按两列宽计算，纯文本表格才能对齐）
# --------------------------------------------------------------------------- #
def display_width(text) -> int:
    width = 0
    for char in str(text):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def truncate(text, width: int) -> str:
    text = str(text)
    if display_width(text) <= width:
        return text
    if width <= 3:
        return text[:max(0, width)]
    out: list[str] = []
    used = 0
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + char_width > width - 3:
            break
        out.append(char)
        used += char_width
    return "".join(out) + "..."


def pad(text, width: int) -> str:
    text = str(text)
    return text + " " * max(0, width - display_width(text))


def layout_columns(columns: list[str], rows: list[list], width: int) -> list[int]:
    """按可用宽度分配列宽。

    压缩策略：**只压长文本列**。状态/类型/结果这类枚举列整体不超过
    :data:`COLUMN_FLOOR` 列时原样保留（否则 ``partial`` 会被截成 ``pa...``，
    等于把信息丢掉），由标题/说明这类自由文本列吸收差额。极端窄终端下
    所有列都已到底限时，才退化为等比压缩。
    """
    count = len(columns)
    if not count:
        return []
    natural: list[int] = []
    for index in range(count):
        cells = [display_width(row[index]) for row in rows if index < len(row)]
        headers = display_width(columns[index])
        natural.append(max([headers] + cells) if cells else headers)
    available = max(20, width - 2 * (count - 1))
    if sum(natural) <= available:
        return natural

    floors = [min(value, COLUMN_FLOOR) for value in natural]
    widths = list(natural)
    deficit = sum(widths) - available
    flexible = [index for index in range(count) if natural[index] > floors[index]]
    while deficit > 0 and flexible:
        progressed = False
        for index in flexible:
            if deficit <= 0:
                break
            if widths[index] <= floors[index]:
                continue
            # 每轮每列只让 1 列：差额由所有自由文本列平摊，而不是第一列独吞
            widths[index] -= 1
            deficit -= 1
            progressed = True
        if not progressed:
            break
    if deficit > 0:      # 连底限都放不下：等比压到最后（此时必然会截断内容）
        total = sum(widths) or 1
        widths = [max(3, int(value * available / total)) for value in widths]
    return widths


# --------------------------------------------------------------------------- #
# 进度任务句柄
# --------------------------------------------------------------------------- #
class TaskHandle:
    """一个进度任务的句柄；无进度时是 no-op，所有方法都可安全调用。

    下载是并发的（线程池），因此内部用一把共享锁保护 rich 的 ``Progress.update``。

    ``keep=False`` 的子任务（分 P 下载、配图、转写）一完成就从显示里撤下，
    否则一次运行下来终帧会堆成几十行进度条；``keep=True`` 的条目级任务保留，
    收尾时停在 100%，留在摘要上方作为"跑完了"的凭据。
    """

    __slots__ = ("_progress", "_task_id", "_lock", "_keep", "_total")

    def __init__(self, progress=None, task_id=None, lock=None, keep: bool = True, total=None):
        self._progress = progress
        self._task_id = task_id
        self._lock = lock
        self._keep = bool(keep)
        self._total = total

    @property
    def active(self) -> bool:
        return self._progress is not None

    def advance(self, amount: int = 1) -> None:
        if not self.active or not amount:
            return
        self.update(advance=amount)

    def update(self, *, advance=None, completed=None, total=None, description=None) -> None:
        if not self.active:
            return
        if total is not None:
            self._total = total
        payload = {}
        if advance is not None:
            payload["advance"] = advance
        if completed is not None:
            payload["completed"] = completed
        if total is not None:
            payload["total"] = total
        if description is not None:
            payload["description"] = description
        if not payload:
            return
        try:
            with self._lock:
                self._progress.update(self._task_id, **payload)
        except Exception:  # pragma: no cover - 进度绘制失败绝不影响业务
            pass

    def done(self, description: str = "") -> None:
        """标记完成：已知总量就补满（停掉转圈），未知总量则只更新描述。

        用句柄自己记的总量，而不是回查 ``Progress.tasks[id]`` —— 后者是按
        **列表下标**索引，对第 0 个之后的任务会取错甚至越界。
        """
        if not self.active:
            return
        progress, task_id = self._progress, self._task_id
        try:
            with self._lock:
                if self._total is not None:
                    progress.update(task_id, completed=self._total)
                if description:
                    progress.update(task_id, description=description)
                if not self._keep:
                    progress.remove_task(task_id)
                    self._progress = None      # 撤下之后这个句柄彻底失效
        except Exception:  # pragma: no cover
            pass


#: 全局无进度句柄：``ui`` 为 None（离线测试、非 TTY）时统一用它，调用方无需判空
NULL_TASK = TaskHandle()


# --------------------------------------------------------------------------- #
# Ui
# --------------------------------------------------------------------------- #
class Ui:
    """终端呈现门面。``stream`` 决定写到哪里（摘要 → stdout，日志/进度 → stderr）。"""

    def __init__(self, stream=None, *, color: str = "auto", progress: bool = True,
                 backend: str = "auto", width: int | None = None):
        self.stream = stream if stream is not None else sys.stdout
        self.mode = (color or "auto").strip().lower()
        self.color_enabled = resolve_color(self.mode, self.stream)
        self.width = int(width) if width else terminal_width(self.stream)
        self._wanted_progress = bool(progress)
        self._rich = None
        self._console = None
        self._progress = None
        self._lock = threading.Lock()
        self._degraded = False
        self._closed = False

        if backend == "rich":
            self._use_rich = self._init_rich()
        elif backend == "plain":
            self._use_rich = False
        else:
            self._use_rich = self._init_rich() if self._rich_allowed() else False

    # ---------------- 后端 ---------------- #
    def _rich_allowed(self) -> bool:
        if self.mode == "never":
            return False
        # 只有交互终端或用户显式 --color=always 时才值得上富文本皮肤；
        # 重定向/管道保持行式纯文本，便于 grep 与 Out-File
        return _isatty(self.stream) or self.mode == "always"

    def _init_rich(self) -> bool:
        parts = load_rich()
        if parts is None:
            return False
        try:
            force_terminal = None
            if self.mode == "always" and not _isatty(self.stream):
                force_terminal = True
            console = parts["Console"](
                file=self.stream, width=self.width, force_terminal=force_terminal,
                no_color=not self.color_enabled, highlight=False, soft_wrap=False,
                # markup=False 是**必须**的：产物里到处是方括号（[发现]、bili-sub-archive[ui]、
                # 标题里的 [xxx]），rich 默认会把它们当样式标签吞掉或报错
                markup=False,
            )
        except Exception:
            return False
        self._rich = parts
        self._console = console
        return True

    def _degrade(self) -> None:
        """一次性降级：rich 出问题后再也不用它，业务照常跑完。"""
        self._degraded = True
        self._use_rich = False

    @property
    def rich_backend(self) -> bool:
        return bool(self._use_rich and not self._degraded)

    @property
    def progress_requested(self) -> bool:
        """用户是否要进度（``--no-progress`` / ``--quiet`` 会关掉）。"""
        return bool(self._wanted_progress)

    @property
    def progress_enabled(self) -> bool:
        return bool(self._wanted_progress and self.rich_backend and not self._closed)

    # ---------------- 底层写出 ---------------- #
    def paint(self, text: str, tone: str = "") -> str:
        """纯文本皮肤的着色；不可着色时原样返回。"""
        code = ANSI_CODES.get(tone or "", "")
        if not code or not self.color_enabled or self.rich_backend:
            return text
        return f"{code}{text}{ANSI_RESET}"

    def _write(self, text: str) -> None:
        if not text:
            return
        if not text.endswith("\n"):
            text += "\n"
        try:
            self.stream.write(text)
        except UnicodeEncodeError:
            # 兜底：极端编码环境下也不让美化把运行打断
            encoding = getattr(self.stream, "encoding", None) or "ascii"
            try:
                self.stream.write(text.encode(encoding, "replace").decode(encoding, "replace"))
            except Exception:  # pragma: no cover
                pass
        except (OSError, ValueError):  # pragma: no cover - 管道关闭等
            pass
        try:
            self.stream.flush()
        except Exception:
            pass

    def _emit(self, builder, plain_text: str) -> None:
        """``builder()`` 产出 rich 可渲染对象；构建或渲染失败都一次性降级为纯文本。

        先渲染到缓冲再整块写出：rich 中途报错时不会留下半截输出。
        """
        text = plain_text
        if self.rich_backend:
            try:
                renderable = builder()
                with self._console.capture() as capture:
                    self._console.print(renderable)
                text = capture.get()
            except Exception:
                self._degrade()
                text = plain_text
        self._write(text)

    # ---------------- 语义原语 ---------------- #
    def line(self, text: str = "", tone: str = "", indent: int = 0) -> None:
        plain = " " * indent + str(text)
        if not self.rich_backend:
            self._write(self.paint(plain, tone))
            return
        style, rich_text = TONE_STYLES.get(tone, ""), self._rich["Text"]
        self._emit(lambda: rich_text(plain, style=style), plain)

    def blank(self) -> None:
        self._write("")

    def rule(self, title: str = "", tone: str = "title") -> None:
        plain = self._plain_rule(title, tone=tone)
        if self.rich_backend:
            style, rule = TONE_STYLES.get(tone, "bold cyan"), self._rich["Rule"]
            self._emit(lambda: rule(title, style=style), self._plain_rule(title))
            return
        self._write(plain)

    def _plain_rule(self, title: str, tone: str = "") -> str:
        bar = "=" * self.width
        if not title:
            return f"\n{bar}"
        return f"\n{bar}\n{self.paint(title, tone)}\n{bar}"

    def panel(self, title: str = "", lines: list[str] | None = None, tone: str = "") -> None:
        body = list(lines or [])
        style = TONE_STYLES.get(tone, "")
        plain_lines = ([f"[{title}]"] if title else []) + [f"  {item}" for item in body]
        plain = "\n".join(plain_lines)
        if self.rich_backend:
            text_cls, panel_cls = self._rich["Text"], self._rich["Panel"]

            def build():
                return panel_cls(text_cls("\n".join(body), style=style), title=title or None,
                                 border_style=style or "cyan", expand=False, padding=(0, 1))

            self._emit(build, plain)
            return
        self._write(plain)

    def kv(self, rows: list[tuple[str, str]], indent: int = 2) -> None:
        rows = [(str(key), str(value)) for key, value in rows]
        if not rows:
            return
        key_width = min(max(display_width(key) for key, _ in rows) + 2, 24)
        plain = "\n".join(
            " " * indent + pad(key, key_width) + value for key, value in rows
        )
        if self.rich_backend:
            def build():
                table = self._rich["Table"].grid(padding=(0, 1))
                table.add_column(style="dim", no_wrap=True)
                table.add_column()
                for key, value in rows:
                    table.add_row(key, value)
                return table

            self._emit(build, plain)
            return
        self._write(plain)

    def badges(self, pairs: list[tuple[str, object, str]]) -> None:
        plain = "、".join(f"{label} {value}" for label, value, _tone in pairs)
        if self.rich_backend:
            def build():
                text = self._rich["Text"]()
                for index, (label, value, tone) in enumerate(pairs):
                    if index:
                        text.append(" · ", style="dim")
                    text.append(f"{label} ", style=TONE_STYLES.get(tone, ""))
                    text.append(str(value), style=TONE_STYLES.get(tone, ""))
                return text

            self._emit(build, plain)
            return
        if self.color_enabled:
            self._write("、".join(self.paint(f"{label} {value}", tone)
                                  for label, value, tone in pairs))
            return
        self._write(plain)

    def table(self, columns: list[str], rows: list[list], *, tones: list[str] | None = None,
              cell_tones: list[list[str]] | None = None) -> None:
        rows = [[str(cell) for cell in row] for row in rows]
        if not rows:
            return
        # rich 每个单元格左右各留 1 列 padding，预算不扣掉它，表格就会超出控制台宽度，
        # 然后 rich 自己去压最窄的那列（表头被截成"状…"）。纯文本皮肤没有这个开销。
        budget = self.width - (2 * len(columns) if self.rich_backend else 0)
        widths = layout_columns(columns, rows, budget)
        # 截断由我们自己做（CJK 宽度自算），两套皮肤的列宽因此完全一致；
        # 不给 rich 设 max_width —— 它按自己的宽度模型算，会和中文表头打架
        headers = [truncate(name, width) for name, width in zip(columns, widths)]
        body = [[truncate(cell, width) for cell, width in zip(row, widths)] for row in rows]

        header_line = "  ".join(pad(name, width)
                                for name, width in zip(headers, widths)).rstrip()
        underline = "  ".join("-" * width for width in widths).rstrip()
        lines = [header_line, underline]
        for row in body:
            lines.append("  ".join(pad(cell, width)
                                   for cell, width in zip(row, widths)).rstrip())
        plain = "\n".join(lines)

        if not self.rich_backend:
            if not self.color_enabled:
                self._write(plain)
                return
            colored = [header_line, underline]
            for row_index, row in enumerate(body):
                cells = []
                for column_index, (cell, column_width) in enumerate(zip(row, widths)):
                    tone = ""
                    if cell_tones and row_index < len(cell_tones):
                        row_tones = cell_tones[row_index]
                        if column_index < len(row_tones):
                            tone = row_tones[column_index]
                    if not tone and tones and row_index < len(tones):
                        tone = tones[row_index]
                    cells.append(self.paint(pad(cell, column_width), tone))
                colored.append("  ".join(cells).rstrip())
            self._write("\n".join(colored))
            return

        def build():
            text_cls = self._rich["Text"]
            table = self._rich["Table"](box=self._rich["box"].SIMPLE_HEAD, show_edge=False,
                                        pad_edge=False, expand=False)
            for name in headers:
                table.add_column(text_cls(name), no_wrap=True)
            for row_index, row in enumerate(body):
                tone = (tones or [""] * len(rows))[row_index] if tones else ""
                cells = []
                for column_index, cell in enumerate(row):
                    cell_tone = ""
                    if cell_tones and row_index < len(cell_tones):
                        row_tones = cell_tones[row_index]
                        if column_index < len(row_tones):
                            cell_tone = row_tones[column_index]
                    cells.append(text_cls(cell, style=TONE_STYLES.get(cell_tone or tone, "")))
                table.add_row(*cells)
            return table

        self._emit(build, plain)

    def flag(self, outcome: str) -> str:
        """结果符号：纯文本皮肤用 ASCII，避免 cp936 重定向崩溃。"""
        rich_flag, plain_flag = FLAGS.get(str(outcome), UNKNOWN_FLAG)
        return rich_flag if self.rich_backend else plain_flag

    # ---------------- 进度 ---------------- #
    def task(self, total: int | None = None, label: str = "", kind: str = "item",
             keep: bool = True) -> TaskHandle:
        """新建一个进度任务；``keep=False`` 表示完成即撤下（子任务）。

        ``close()`` 之后一律返回 no-op：**收尾之后绝不能再冒出进度输出**，
        否则 rich 的 ``Live.stop()`` 会把它印在运行摘要后面。
        """
        if not self.progress_enabled:
            return NULL_TASK
        try:
            progress = self._ensure_progress()
            task_id = progress.add_task(label, total=total)
        except Exception:
            self._degrade_progress()
            return NULL_TASK
        return TaskHandle(progress=progress, task_id=task_id, lock=self._lock,
                          keep=keep, total=total)

    def _ensure_progress(self):
        if self._progress is not None:
            return self._progress
        parts = self._rich
        progress = parts["Progress"](
            parts["spinner"](style="cyan"),
            parts["text_column"]("[progress.description]{task.description}", justify="left"),
            parts["bar"](bar_width=18),
            parts["task_progress"](),
            parts["download"](),
            parts["elapsed"](),
            console=self._console, transient=False, refresh_per_second=REFRESH_PER_SECOND,
            auto_refresh=True,
        )
        progress.start()
        self._progress = progress
        return progress

    def _degrade_progress(self) -> None:
        self._wanted_progress = False

    def close(self) -> None:
        """停止进度渲染（恢复光标）并**封盘**。

        必须在对人类可读输出（运行摘要 / check 报告 / JSON）之前调用：
        rich 的 ``Live.stop()`` 会再渲染最后一帧，若等到 ``finally`` 才收，
        那一帧就会印在摘要**后面**（表现为"跑完了又冒出一条归档进度"）。
        幂等：重复调用无副作用。
        """
        self._closed = True
        progress, self._progress = self._progress, None
        if progress is None:
            return
        try:
            progress.stop()
        except Exception:  # pragma: no cover
            pass

    # ---------------- 日志 ---------------- #
    def log_handler(self, *, verbose: int = 0):
        """给 ``logging`` 用的 handler；用不了 rich 时返回 ``None``（调用方退回 StreamHandler）。"""
        if not self.rich_backend:
            return None
        try:
            from rich.logging import RichHandler

            return RichHandler(
                console=self._console,
                show_time=False, show_path=False, show_level=False,
                markup=False,          # 日志正文含 [发现] [登录] 等方括号，markup 会吞掉
                rich_tracebacks=bool(verbose),
                tracebacks_show_locals=False,
            )
        except Exception:
            self._degrade()
            return None

    # ---------------- 便捷构造 ---------------- #
    @classmethod
    def from_args(cls, args, stream=None, *, progress: bool | None = None) -> "Ui":
        mode = str(getattr(args, "color", "auto") or "auto")
        if progress is None:
            progress = not bool(getattr(args, "no_progress", False)) \
                and not bool(getattr(args, "quiet", False))
        return cls(stream, color=mode, progress=progress)


__all__ = [
    "DEFAULT_WIDTH",
    "FLAGS",
    "NULL_TASK",
    "OUTCOME_TONES",
    "TONE_STYLES",
    "TaskHandle",
    "Ui",
    "ansi_capable",
    "display_width",
    "layout_columns",
    "load_rich",
    "pad",
    "resolve_color",
    "terminal_width",
    "truncate",
]
