"""终端呈现层（``bili_sub_archive.ui``）的离线测试。

锁死的是**呈现契约**，不是样式细节：

1. 非交互 / ``--color=never`` / ``NO_COLOR`` → 不写任何 ANSI，符号退化为 ASCII
   （cp936 重定向不再 ``UnicodeEncodeError``）；
2. 没装 rich（或 rich 渲染炸了）→ 一次性降级为纯文本，业务照常跑完；
3. 进度句柄在没有进度时是 no-op，且 ``--quiet`` / ``--no-progress`` 能关掉它；
4. 日志正文里的 ``[发现]`` 这类方括号必须原样保留（rich 的 markup 会吞掉它们）；
5. 机读分支（``--json``）不经过本模块，因此不受任何装饰影响。

整套用例不联网、不落盘。
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import time
import unittest
from unittest import mock

from bili_sub_archive import ui as ui_module
from bili_sub_archive.cli import build_parser, print_check_report, print_summary
from bili_sub_archive.log import configure_stdio, setup_logger
from bili_sub_archive.models import EntryResult, RunSummary
from bili_sub_archive.ui import NULL_TASK, Ui, display_width, layout_columns, resolve_color

RICH_PRESENT = ui_module.load_rich() is not None
skip_without_rich = unittest.skipUnless(RICH_PRESENT, "环境未安装 rich（可选依赖）")


class FakeTty(io.StringIO):
    """看起来像交互终端、但没有真实文件描述符的流。"""

    def isatty(self) -> bool:
        return True


def make_summary(**kwargs) -> RunSummary:
    summary = RunSummary(run_id="abc12345", mode="sync", uid=1039025435,
                         author="战国时代", author_dir="1039025435_战国时代",
                         discovered=30, selected=2, http_requests=12, http_retries=1,
                         bytes_downloaded=2048)
    summary.counts = {"done": 1, "failed": 1}
    summary.results = [
        EntryResult(kind="video", platform_id="BV1", dir="d1", title="标题一",
                    published_at=None, outcome="done", message="3/3 P 有文字"),
        EntryResult(kind="article", platform_id="cv1", dir="d2", title="标题二",
                    published_at=None, outcome="failed", message="详情接口失败"),
    ]
    summary.warnings = ["一条警告"]
    summary.notes = ["一条说明"]
    for key, value in kwargs.items():
        setattr(summary, key, value)
    return summary


class EnvGuard(unittest.TestCase):
    """清掉会影响能力判定的环境变量，测试结束后还原。"""

    WATCHED = ("NO_COLOR", "CLICOLOR_FORCE", "TERM")

    def setUp(self) -> None:
        self._saved = {key: os.environ.pop(key, None) for key in self.WATCHED}

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# --------------------------------------------------------------------------- #
# 能力判定
# --------------------------------------------------------------------------- #
class CapabilityTest(EnvGuard):
    def test_auto_off_for_non_tty(self):
        self.assertFalse(resolve_color("auto", io.StringIO()))

    def test_auto_on_for_tty(self):
        self.assertTrue(resolve_color("auto", FakeTty()))

    def test_never_wins_over_everything(self):
        self.assertFalse(resolve_color("never", FakeTty()))
        with mock.patch.dict(os.environ, {"CLICOLOR_FORCE": "1"}):
            self.assertFalse(resolve_color("never", FakeTty()))

    def test_no_color_env_disables_auto(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertFalse(resolve_color("auto", FakeTty()))

    def test_always_beats_no_color(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertTrue(resolve_color("always", io.StringIO()))

    def test_clicolor_force_enables_non_tty(self):
        with mock.patch.dict(os.environ, {"CLICOLOR_FORCE": "1"}):
            self.assertTrue(resolve_color("auto", io.StringIO()))

    def test_term_dumb_disables_auto(self):
        with mock.patch.dict(os.environ, {"TERM": "dumb"}):
            self.assertFalse(resolve_color("auto", FakeTty()))

    def test_non_tty_width_is_stable(self):
        """非 TTY 固定用默认宽度：重定向结果不随终端大小变化。"""
        self.assertEqual(ui_module.terminal_width(io.StringIO()), ui_module.DEFAULT_WIDTH)


# --------------------------------------------------------------------------- #
# 纯文本皮肤
# --------------------------------------------------------------------------- #
class PlainBackendTest(unittest.TestCase):
    def make(self, **kwargs):
        stream = io.StringIO()
        kwargs.setdefault("backend", "plain")
        kwargs.setdefault("color", "never")
        kwargs.setdefault("width", 80)
        return Ui(stream=stream, **kwargs), stream

    def render_all(self, ui: Ui) -> None:
        ui.blank()
        ui.rule("运行摘要 abc12345（sync）")
        ui.kv([("UP", "战国时代（UID 1039025435）"), ("目录", "1039025435_战国时代")])
        ui.badges([("成功", 1, "ok"), ("失败", 1, "error")])
        ui.line("请求 12 次", "dim")
        ui.table(["状态", "类型", "标题", "结果", "说明"],
                 [["[ok]", "video", "标题一", "done", "3/3 P 有文字"],
                  ["[x]", "article", "标题二", "failed", "详情接口失败"]],
                 tones=["ok", "error"], cell_tones=[["ok", "", "", "ok", "dim"],
                                                    ["error", "", "", "error", "dim"]])
        ui.panel("警告", ["一条警告"], "warn")
        ui.rule()

    def test_no_ansi_and_ascii_flags(self):
        ui, stream = self.make()
        self.render_all(ui)
        out = stream.getvalue()
        self.assertNotIn("\x1b", out)
        self.assertIn("[ok]", out)
        self.assertIn("[x]", out)
        self.assertNotIn("✔", out)
        self.assertNotIn("✘", out)
        self.assertIn("运行摘要 abc12345（sync）", out)

    def test_always_paints_without_rich(self):
        ui, stream = self.make(color="always")
        self.assertTrue(ui.color_enabled)
        ui.line("成功", "ok")
        self.assertIn("\x1b[32m", stream.getvalue())

    def test_empty_table_and_kv_are_quiet(self):
        ui, stream = self.make()
        ui.table(["a"], [])
        ui.kv([])
        self.assertEqual(stream.getvalue(), "")

    def test_layout_columns_accounts_for_cjk(self):
        widths = layout_columns(["标题"], [["中文标题四字"]], 40)
        self.assertGreaterEqual(widths[0], 12)      # 6 个汉字 = 12 列
        self.assertEqual(display_width("中文"), 4)

    def test_long_text_is_truncated_to_column(self):
        ui, stream = self.make(width=60)
        ui.table(["标题"], [["这是一个非常非常非常长的标题" * 3]])
        for line in stream.getvalue().splitlines()[2:]:
            self.assertLessEqual(display_width(line), 60)

    def test_enum_columns_survive_squeeze(self):
        """枚举列（状态/类型/结果）不许被压掉：``partial`` 变成 ``pa...`` 就是丢信息。"""
        ui, stream = self.make(width=100)
        ui.table(["状态", "类型", "标题", "结果", "说明"],
                 [["[ok]", "video", "很长的中文标题" * 8, "done", "说明" * 20],
                  ["[~]", "video", "另一个很长的标题" * 8, "partial", "更长的说明" * 20]])
        out = stream.getvalue()
        for token in ("[ok]", "[~]", "partial", "状态", "类型", "结果"):
            self.assertIn(token, out)
        for line in out.splitlines():
            self.assertLessEqual(display_width(line), 100)


# --------------------------------------------------------------------------- #
# rich 皮肤
# --------------------------------------------------------------------------- #
@skip_without_rich
class RichBackendTest(unittest.TestCase):
    def make(self, **kwargs):
        stream = io.StringIO()
        kwargs.setdefault("backend", "rich")
        kwargs.setdefault("color", "never")
        kwargs.setdefault("width", 80)
        return Ui(stream=stream, **kwargs), stream

    def test_summary_renders_key_facts(self):
        ui, stream = self.make()
        print_summary(make_summary(), ui=ui)
        out = stream.getvalue()
        self.assertIn("运行摘要 abc12345（sync）", out)
        self.assertIn("战国时代", out)
        self.assertIn("成功", out)
        self.assertIn("失败", out)
        self.assertIn("标题一", out)
        self.assertIn("详情接口失败", out)
        self.assertIn("一条警告", out)
        self.assertNotIn("\x1b", out)          # --color=never

    def test_check_report_keeps_long_values(self):
        """长值允许被 rich 折行，但**一个字符都不能丢**（安装命令必须完整可见）。"""
        long_value = "已安装：版本 3.20.0；安装：pip install \"bili-sub-archive[ui]\"（或 pip install rich）"
        ui, stream = self.make()
        print_check_report({"可选依赖": [("rich", long_value)]}, ui=ui)
        rendered = " ".join(stream.getvalue().split())
        self.assertIn(" ".join(long_value.split()), rendered)
        self.assertIn("bili-sub-archive[ui]", rendered)

    def test_brackets_survive_in_panel(self):
        ui, stream = self.make()
        ui.panel("说明", ["[发现] 扫描完成"], "dim")
        self.assertIn("[发现] 扫描完成", stream.getvalue())

    def test_rich_color_always_emits_ansi(self):
        ui, stream = self.make(color="always")
        ui.line("成功", "ok")
        self.assertIn("\x1b[", stream.getvalue())

    def test_table_fits_width_and_keeps_enum_columns(self):
        """rich 的单元格 padding 也要算进宽度预算，否则它会自己压掉最窄的列。"""
        ui, stream = self.make(width=100)
        ui.table(["状态", "类型", "标题", "结果", "说明"],
                 [["✔", "video", "很长的中文标题" * 8, "done", "说明" * 20],
                  ["✘", "dynamic", "另一个很长的标题" * 8, "partial", "更长的说明" * 20]])
        out = stream.getvalue()
        for token in ("状态", "类型", "结果", "partial", "dynamic"):
            self.assertIn(token, out)
        for line in out.splitlines():
            self.assertLessEqual(display_width(line), 100)

    def test_render_failure_degrades_to_plain(self):
        ui, stream = self.make()

        def boom(*_args, **_kwargs):
            raise RuntimeError("rich 炸了")

        ui._rich["Table"] = boom                                  # noqa: SLF001 - 故意注入故障
        ui.table(["状态", "标题"], [["[ok]", "标题一"]])
        out = stream.getvalue()
        self.assertIn("标题一", out)
        self.assertFalse(ui.rich_backend)                          # 一次性降级
        ui.line("[发现] 后续输出仍然可读")                           # 降级后照常工作
        self.assertIn("[发现] 后续输出仍然可读", stream.getvalue())

    def test_log_handler_keeps_brackets_and_no_markup(self):
        ui, stream = self.make()
        handler = ui.log_handler(verbose=0)
        self.assertIsNotNone(handler)
        logger = logging.getLogger("bili_sub_archive.test.ui")
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            logger.info("[发现] 开始扫描 video 列表…")
        finally:
            logger.handlers.clear()
        out = stream.getvalue()
        self.assertIn("[发现] 开始扫描 video 列表…", out)
        self.assertNotIn("\x1b", out)

    def test_progress_task_renders_and_closes(self):
        ui, stream = self.make(progress=True)
        self.assertTrue(ui.progress_requested)
        self.assertTrue(ui.progress_enabled)
        task = ui.task(total=2, label="下载 P01")
        self.assertTrue(task.active)
        task.advance()
        task.update(completed=2, description="下载 P01 完成")
        time.sleep(0.2)          # 让后台刷新线程写一帧
        ui.close()
        self.assertFalse(ui._progress)                             # noqa: SLF001
        self.assertIn("下载 P01", stream.getvalue())
        # 收尾之后再用句柄不能抛
        task.advance()

    def test_progress_disabled_by_flag(self):
        ui, _stream = self.make(progress=False)
        task = ui.task(total=2, label="不该出现")
        self.assertFalse(task.active)
        task.advance()
        task.done()

    def test_close_seals_progress_for_good(self):
        """收尾之后绝不能再创建进度任务。

        否则 ``Live.stop()`` 会把最后一帧再渲染一次 —— 表现就是"跑完了，
        运行摘要后面又冒出一条归档进度"。
        """
        ui, stream = self.make(progress=True)
        task = ui.task(total=2, label="归档条目")
        task.done()
        ui.close()
        frozen = stream.getvalue()
        ui.close()                       # 幂等
        self.assertFalse(ui.progress_enabled)
        self.assertFalse(ui.task(total=2, label="收尾后不该再出现").active)
        # 关闭时渲染最后一帧是正常的；这里锁的是"关闭之后再无新输出"
        self.assertEqual(stream.getvalue(), frozen)

    def test_child_task_is_removed_on_done(self):
        """子任务（分 P/配图/转写）完成即撤下，不在终帧里堆成一墙进度条。"""
        ui, _stream = self.make(progress=True)
        parent = ui.task(total=1, label="归档条目")
        child = ui.task(total=3, label="配图（body）", keep=False)
        child.advance()
        child.done("配图完成")
        self.assertFalse(child.active)                   # 撤下后句柄失效
        progress = ui._progress                          # noqa: SLF001
        labels = [task.description for task in progress.tasks]
        self.assertEqual(labels, ["归档条目"])            # 只剩条目级任务
        child.advance()                                  # 再动也不抛
        self.assertTrue(parent.active)
        ui.close()


# --------------------------------------------------------------------------- #
# 没有 rich / 不用进度
# --------------------------------------------------------------------------- #
class DegradeTest(unittest.TestCase):
    def test_missing_rich_falls_back_to_plain(self):
        with mock.patch.object(ui_module, "load_rich", lambda: None):
            stream = FakeTty()
            ui = Ui(stream=stream, backend="auto", color="auto", width=80)
            self.assertFalse(ui.rich_backend)
            ui.line("成功", "ok")
            ui.table(["状态"], [["[ok]"]])
            self.assertIn("[ok]", stream.getvalue())

    def test_forced_rich_without_rich_module_falls_back(self):
        with mock.patch.object(ui_module, "load_rich", lambda: None):
            stream = io.StringIO()
            ui = Ui(stream=stream, backend="rich", color="never", width=80)
            self.assertFalse(ui.rich_backend)
            self.assertIsNone(ui.log_handler())
            self.assertFalse(ui.task(total=1, label="x").active)

    def test_null_task_is_safe(self):
        NULL_TASK.advance()
        NULL_TASK.update(completed=3, total=5, description="x")
        NULL_TASK.done("y")
        self.assertFalse(NULL_TASK.active)

    def test_plain_backend_progress_is_inactive(self):
        ui = Ui(stream=io.StringIO(), backend="plain", color="never", progress=True)
        self.assertTrue(ui.progress_requested)
        self.assertFalse(ui.progress_enabled)
        self.assertFalse(ui.task(total=3, label="x").active)


# --------------------------------------------------------------------------- #
# 与 CLI / 日志的接线
# --------------------------------------------------------------------------- #
class WiringTest(EnvGuard):
    def test_print_summary_writes_to_redirected_stdout(self):
        """默认参数必须按调用时的 ``sys.stdout`` 解析（否则 redirect_stdout 抓不到）。"""
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            print_summary(make_summary())
        out = stream.getvalue()
        self.assertIn("运行摘要 abc12345（sync）", out)
        self.assertNotIn("\x1b", out)

    def test_configure_stdio_tolerates_stringio(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            configure_stdio()          # StringIO 没有 reconfigure，必须静默跳过

    def test_setup_logger_without_ui_keeps_old_behaviour(self):
        logger = setup_logger(verbose=0, quiet=False, redactor=None)
        self.assertEqual(len(logger.handlers), 1)
        self.assertEqual(logger.handlers[0].formatter._fmt, "%(message)s")   # noqa: SLF001
        logger.handlers.clear()

    @skip_without_rich
    def test_setup_logger_with_rich_ui_uses_rich_handler(self):
        from rich.logging import RichHandler

        ui = Ui(stream=io.StringIO(), backend="rich", color="never", width=80)
        logger = setup_logger(verbose=0, quiet=False, redactor=None, ui=ui)
        self.assertIsInstance(logger.handlers[0], RichHandler)
        logger.handlers.clear()

    def test_cli_flags_control_color_and_progress(self):
        parser = build_parser()
        args = parser.parse_args(["sync", "--uid", "1"])
        self.assertEqual(args.color, "auto")
        self.assertFalse(args.no_progress)
        self.assertTrue(Ui.from_args(args, stream=io.StringIO()).progress_requested)

        bare = parser.parse_args(["sync", "--uid", "1", "--color"])
        self.assertEqual(bare.color, "always")
        self.assertTrue(Ui.from_args(bare, stream=io.StringIO()).color_enabled)

        never = parser.parse_args(["sync", "--uid", "1", "--color=never"])
        self.assertFalse(Ui.from_args(never, stream=FakeTty()).color_enabled)

        for flag in ("--no-progress", "--quiet"):
            ns = parser.parse_args(["sync", "--uid", "1", flag])
            self.assertFalse(Ui.from_args(ns, stream=io.StringIO()).progress_requested)

    def test_cli_parser_uses_new_command_name(self):
        """改名后帮助文本里的程序名必须是 ``bsa``，而不是包名或旧名。"""
        self.assertEqual(build_parser().prog, "bsa")

    def test_legacy_script_name_gets_deprecation_hint(self):
        """旧命令名 ``subvideo`` 仍可用一个版本，但必须在 stderr 提示改用 ``bsa``。"""
        from bili_sub_archive import cli as cli_module

        for invoked, expect_hint in (("subvideo", True), ("subvideo.exe", True),
                                     ("bsa", False), ("bili-sub-archive.exe", False)):
            stderr = io.StringIO()
            with mock.patch.object(cli_module.sys, "argv", [invoked]), \
                    contextlib.redirect_stderr(stderr):
                cli_module._warn_legacy_script_name()          # noqa: SLF001 - 直接守这条接线
            if expect_hint:
                self.assertIn("bsa", stderr.getvalue(), f"{invoked} 应提示改用 bsa")
            else:
                self.assertEqual(stderr.getvalue(), "", f"{invoked} 不该有任何提示")


# --------------------------------------------------------------------------- #
# 输出时序：进度必须先收尾，再输出运行摘要
# --------------------------------------------------------------------------- #
class OutputOrderTest(unittest.TestCase):
    """回归守卫：``Live.stop()`` 会补渲染最后一帧。

    如果收进度发生在打印摘要之后（例如只在 ``finally`` 里收），那一帧就会
    出现在摘要**后面** —— 用户看到的就是"归档进度在完成状态后又输出了一次"。
    """

    def test_progress_is_closed_before_summary_is_printed(self):
        from bili_sub_archive import cli as cli_module

        events: list[str] = []

        class FakeUi(Ui):
            def close(self):
                events.append("close")
                return super().close()

        class FakeLoaded:
            credentials = type("C", (), {"cookie": "", "llm_api_key": ""})()

        class FakeRunner:
            def __init__(self, *_args, **_kwargs):
                pass

            def sync(self, _options):
                events.append("sync")
                return make_summary()

            def retry(self, _options):  # pragma: no cover - 本用例只走 sync
                return self.sync(_options)

        stdout = io.StringIO()
        with mock.patch.object(cli_module, "load_config", lambda **_kw: FakeLoaded()), \
                mock.patch.object(cli_module, "Ui", FakeUi), \
                mock.patch.object(cli_module, "Runner", FakeRunner), \
                mock.patch.object(cli_module, "_redactor_for", lambda _loaded: None), \
                mock.patch.object(cli_module, "print_summary",
                                  lambda summary, ui=None: events.append("summary")), \
                contextlib.redirect_stdout(stdout):
            code = cli_module.main(["sync", "--uid", "1"])

        self.assertEqual(code, 0)
        # finally 里还会各收一次（幂等），这里只看前三个事件：
        # 收进度必须发生在打印摘要之前
        self.assertEqual(events[:3], ["sync", "close", "summary"],
                         "必须先收进度再打印摘要，否则进度条会印在摘要后面")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
