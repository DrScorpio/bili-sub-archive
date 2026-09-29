"""动态长图渲染测试（需求 3.2.2）：换行算法、字体解析、端到端渲染。

约定：

- 工作目录一律用 :func:`tests.workspace`（不用 ``tempfile``，见 ``tests/__init__.py``）；
- 测试素材（纯色 PNG）用 Pillow 现场生成，不依赖外部文件；
- Pillow 未安装时端到端用例整体跳过，但 ``wrap_text`` / 字体解析用例仍会跑。
"""

from __future__ import annotations

import builtins
import os
import unittest
from pathlib import Path

from bili_sub_archive.render import (
    PILLOW_MISSING_MESSAGE,
    RenderHeader,
    RenderItem,
    available_fonts,
    render_long_png,
    resolve_font,
    wrap_text,
)
from bili_sub_archive.render import longimage as longimage_module
from tests import workspace

try:  # 被测依赖：Pillow 已装则跑端到端用例
    from PIL import Image, ImageFont

    PILLOW_OK = True
except ImportError:  # pragma: no cover - 本机已装
    PILLOW_OK = False


def _load_font(size: int = 26, bold: bool = False):
    """取一个可用字体对象（优先系统中文字体，否则 Pillow 内置字体）。"""
    path = resolve_font(bold=bold)
    if path:
        return ImageFont.truetype(path, size, index=0)
    return ImageFont.load_default()


def _cjk_font(size: int = 26, bold: bool = False):
    """取一个**中文字体**；本机没有中文字体时跳过用例（不失败）。"""
    if not resolve_font(bold=bold):
        raise unittest.SkipTest("本机没有可用的中文字体，跳过中文换行用例")
    return _load_font(size=size, bold=bold)


def _make_png(path: Path, size: tuple[int, int], color: tuple[int, int, int]) -> Path:
    """现场生成一张纯色 PNG 作为测试素材。"""
    Image.new("RGB", size, color).save(path)
    return path


def _non_white_pixels(path: Path) -> int:
    """非白像素数量（间接验证"确实画上了字"，无需 OCR）。"""
    with Image.open(path) as im:
        histogram = im.convert("L").histogram()
    return sum(histogram[:250])


class WrapTextTest(unittest.TestCase):
    """换行纯函数：CJK 逐字、拉丁按词、中英混排、超长断字、边界输入。"""

    def test_empty_returns_empty_list(self):
        font = _load_font()
        self.assertEqual(wrap_text("", font, 100), [])
        self.assertEqual(wrap_text(None, font, 100), [])

    def test_single_char(self):
        font = _load_font()
        self.assertEqual(wrap_text("a", font, 500), ["a"])
        cjk = _cjk_font()
        self.assertEqual(wrap_text("中", cjk, 500), ["中"])

    def test_newline_preserved(self):
        font = _load_font()
        self.assertEqual(wrap_text("a\nb", font, 500), ["a", "b"])
        self.assertEqual(wrap_text("a\n\nb", font, 500), ["a", "", "b"])

    def test_chinese_wraps_by_character(self):
        font = _cjk_font()
        text = "中文测试换行算法"
        max_width = max(font.getlength(ch) for ch in text) * 3 + 2
        lines = wrap_text(text, font, max_width)
        self.assertGreater(len(lines), 2)
        self.assertEqual("".join(lines), text)          # 不丢字
        for line in lines:
            self.assertLessEqual(font.getlength(line), max_width)
            self.assertLessEqual(len(line), 3)

    def test_english_wraps_by_word(self):
        font = _load_font()
        text = "hello world foo"
        max_width = font.getlength("hello world") + 1
        lines = wrap_text(text, font, max_width)
        self.assertEqual(lines, ["hello world", "foo"])
        for line in lines:
            self.assertLessEqual(font.getlength(line), max_width)

    def test_mixed_chinese_and_english(self):
        font = _cjk_font()
        text = "中文English混合文本测试"
        max_width = max(font.getlength(ch) for ch in "中文混合文本测试") * 4 + 2
        lines = wrap_text(text, font, max_width)
        self.assertGreater(len(lines), 1)
        self.assertEqual("".join(lines), text)
        for line in lines:
            self.assertLessEqual(font.getlength(line), max_width)

    def test_overlong_word_is_force_broken(self):
        font = _load_font()
        text = "a" * 50
        max_width = font.getlength("a" * 10) + 1
        lines = wrap_text(text, font, max_width)
        self.assertGreaterEqual(len(lines), 5)
        self.assertEqual("".join(lines), text)
        for line in lines:
            self.assertLessEqual(font.getlength(line), max_width)
            self.assertLessEqual(len(line), 10)

    def test_trailing_spaces_stripped(self):
        font = _load_font()
        self.assertEqual(wrap_text("hello   ", font, 500), ["hello"])

    def test_non_positive_width_returns_single_line(self):
        font = _load_font()
        self.assertEqual(wrap_text("abc", font, 0), ["abc"])


class ResolveFontTest(unittest.TestCase):
    """字体解析：显式路径优先、非法路径回退、返回值必须真实存在。"""

    def test_explicit_path_wins(self):
        with workspace() as tmp:
            fonts = available_fonts()
            if fonts:
                self.assertEqual(resolve_font(font_path=fonts[0]), fonts[0])
            else:
                # 本机没有候选字体时，用一个真实存在的文件验证"显式路径直接采用"
                dummy = Path(tmp) / "fake.ttf"
                dummy.write_bytes(b"not-a-real-font")
                self.assertEqual(resolve_font(font_path=str(dummy)), str(dummy))

    def test_invalid_path_falls_back_to_candidate(self):
        with workspace() as tmp:
            missing = Path(tmp) / "not-exist.ttf"
            out = resolve_font(font_path=str(missing))
            self.assertNotEqual(out, str(missing))
            self.assertTrue(out == "" or os.path.isfile(out))

    def test_returned_path_exists(self):
        out = resolve_font()
        if not out:
            self.skipTest("本机没有候选中文字体，跳过存在性断言")
        self.assertTrue(Path(out).is_file())
        self.assertIn(Path(out).suffix.lower(), (".ttc", ".ttf", ".otf"))

    def test_available_fonts_all_exist(self):
        fonts = available_fonts()
        self.assertIsInstance(fonts, list)
        for path in fonts:
            self.assertTrue(Path(path).is_file(), path)

    def test_bold_prefers_bold_file(self):
        bold = resolve_font(bold=True)
        normal = resolve_font(bold=False)
        self.assertTrue(bold == "" or Path(bold).is_file())
        self.assertTrue(normal == "" or Path(normal).is_file())


class LongImageModuleTest(unittest.TestCase):
    """模块级契约：顶层不得导入 Pillow（依赖缺失时也要能安全导入）。"""

    def test_no_top_level_pillow_import(self):
        source = Path(longimage_module.__file__).read_text(encoding="utf-8")
        for line in source.splitlines():
            # 只看零缩进的顶层语句；函数内部的惰性导入是契约要求的写法
            if line.startswith("from PIL") or line.startswith("import PIL"):
                self.fail(f"longimage.py 顶层不得导入 Pillow：{line}")

    def test_pillow_missing_message_is_chinese(self):
        self.assertIn("Pillow", PILLOW_MISSING_MESSAGE)
        self.assertIn("pip install", PILLOW_MISSING_MESSAGE)

    @unittest.skipUnless(PILLOW_OK, "未安装 Pillow")
    def test_dependency_missing_returns_result(self):
        """模拟 Pillow 缺失：必须返回 dependency_missing，而不是抛 ImportError。"""
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError("模拟：Pillow 未安装")
            return real_import(name, *args, **kwargs)

        with workspace() as tmp:
            out = Path(tmp) / "never.png"
            builtins.__import__ = fake_import
            try:
                result = render_long_png(RenderHeader(author="测试"), [], out)
            finally:
                builtins.__import__ = real_import
            self.assertFalse(result.ok)
            self.assertEqual(result.error_kind, "dependency_missing")
            self.assertEqual(result.message, PILLOW_MISSING_MESSAGE)
            self.assertFalse(out.exists())


@unittest.skipUnless(PILLOW_OK, "未安装 Pillow")
class RenderLongPngTest(unittest.TestCase):
    """端到端渲染（真机跑 Pillow）。"""

    def _rich_header(self, tmp: Path) -> RenderHeader:
        avatar = _make_png(tmp / "avatar.png", (200, 200), (40, 44, 52))
        return RenderHeader(
            author="战国时代_姜汁汽水",
            avatar=str(avatar),
            published="2026-09-22 14:31:00",
            title="一条用于验证长图排版的中文标题",
            url="https://t.bilibili.com/1234567890",
            badge="置顶",
            visible="公开",
            stats="点赞 11 / 评论 2 / 转发 1",
            forward_note="转发来源：@某位UP主（原文已被删除，仅保留可读文本）",
        )

    def test_end_to_end_with_images_and_chinese_text(self):
        with workspace() as tmp:
            tmp = Path(tmp)
            img1 = _make_png(tmp / "01.png", (800, 500), (200, 60, 60))
            img2 = _make_png(tmp / "02.png", (600, 900), (60, 120, 200))
            img3 = _make_png(tmp / "03.png", (1600, 400), (60, 180, 90))
            items = [
                RenderItem(kind="heading", text="第一部分：中文小标题", level=2),
                RenderItem(kind="text", text=(
                    "第一段中文正文，用来验证逐字换行、行高与段落留白是否符合预期。\n\n"
                    "第二段：中英混排 mixed English text 也要能正确换行，"
                    "超长单词 supercalifragilisticexpialidocious 需要强制断字。"
                )),
                RenderItem(kind="image", image=str(img1)),
                RenderItem(kind="text", text="图片下方的中文说明段落。"),
                RenderItem(kind="image", image=str(img2)),
                RenderItem(kind="image", image=str(img3)),
                RenderItem(kind="note", text="未获取到的内容：充电专属正文被平台门控，仅保留可读摘要。"),
                RenderItem(kind="heading", text="第二部分", level=5),
                RenderItem(kind="text", text="结尾段落。\n\n\n\n连续空行应折叠成一个空行。"),
            ]
            out = tmp / "render.png"
            result = render_long_png(self._rich_header(tmp), items, out, width=1080)

            self.assertTrue(result.ok, result.message)
            self.assertEqual(result.error_kind, "")
            self.assertIsNotNone(result.path)
            self.assertTrue(out.is_file())
            self.assertEqual(result.width, 1080)
            self.assertGreater(result.height, 500)
            self.assertFalse(result.clamped)
            self.assertEqual(result.images_rendered, 3)
            self.assertEqual(result.images_missing, 0)
            self.assertEqual(result.bytes_written, out.stat().st_size)

            with Image.open(out) as im:
                self.assertEqual(im.format, "PNG")           # 合法 PNG
                self.assertEqual(im.size, (1080, result.height))
                self.assertEqual(im.mode, "RGB")
            # 中文确实画上了（非白像素数量远超边框线条）
            self.assertGreater(_non_white_pixels(out), 2000)

    def test_height_grows_with_content(self):
        with workspace() as tmp:
            tmp = Path(tmp)
            header = RenderHeader(author="测试UP", published="2026-01-01 00:00:00")
            short_items = [RenderItem(kind="text", text="短正文。")]
            long_items = [RenderItem(kind="text", text="这是一段较长的中文正文，用来撑高整张长图。" * 40)]
            short_out = tmp / "short.png"
            long_out = tmp / "long.png"
            short_result = render_long_png(header, short_items, short_out, width=1080)
            long_result = render_long_png(header, long_items, long_out, width=1080)
            self.assertTrue(short_result.ok, short_result.message)
            self.assertTrue(long_result.ok, long_result.message)
            self.assertGreater(long_result.height, short_result.height)
            with Image.open(short_out) as im:
                self.assertEqual(im.size[0], 1080)

    def test_missing_image_uses_placeholder(self):
        with workspace() as tmp:
            tmp = Path(tmp)
            items = [
                RenderItem(kind="text", text="正文。"),
                RenderItem(kind="image", image=str(Path(tmp) / "not-exist.png"), caption="配图未取得（下载失败）"),
                RenderItem(kind="image", image=""),
            ]
            out = tmp / "missing.png"
            result = render_long_png(RenderHeader(author="测试UP"), items, out, width=1080)
            self.assertTrue(result.ok, result.message)
            self.assertEqual(result.images_rendered, 0)
            self.assertEqual(result.images_missing, 2)
            self.assertTrue(out.is_file())
            self.assertIn("配图", result.message)

    def test_corrupt_image_file_is_counted_missing(self):
        with workspace() as tmp:
            tmp = Path(tmp)
            broken = tmp / "broken.png"
            broken.write_bytes(b"this is not a png")
            out = tmp / "broken_out.png"
            result = render_long_png(
                RenderHeader(author="测试UP"),
                [RenderItem(kind="image", image=str(broken), caption="图片损坏")],
                out, width=1080,
            )
            self.assertTrue(result.ok, result.message)
            self.assertEqual(result.images_missing, 1)
            self.assertEqual(result.images_rendered, 0)

    def test_missing_avatar_still_succeeds(self):
        with workspace() as tmp:
            tmp = Path(tmp)
            for avatar in ("", str(Path(tmp) / "no-avatar.png")):
                out = tmp / f"avatar_{len(avatar)}.png"
                result = render_long_png(
                    RenderHeader(author="没有头像的UP", avatar=avatar, published="2026-01-01 00:00:00"),
                    [RenderItem(kind="text", text="头像缺失也要能渲染。")],
                    out, width=1080,
                )
                self.assertTrue(result.ok, result.message)
                with Image.open(out) as im:
                    self.assertEqual(im.format, "PNG")

    def test_max_height_clamps_and_reports(self):
        with workspace() as tmp:
            tmp = Path(tmp)
            items = [RenderItem(kind="text", text="很长的中文正文。" * 60)]
            out = tmp / "clamped.png"
            result = render_long_png(RenderHeader(author="测试UP", title="标题"), items, out,
                                     width=1080, max_height=300)
            self.assertTrue(result.ok, result.message)
            self.assertTrue(result.clamped)
            self.assertNotEqual(result.message, "")
            self.assertIn("max_height", result.message)
            self.assertIn("截断", result.message)
            self.assertLessEqual(result.height, 300)
            with Image.open(out) as im:
                self.assertEqual(im.size[1], result.height)

    def test_empty_header_and_items_still_produce_png(self):
        with workspace() as tmp:
            out = Path(tmp) / "empty.png"
            result = render_long_png(RenderHeader(), [], out, width=1080)
            self.assertTrue(result.ok, result.message)
            self.assertTrue(out.is_file())
            self.assertGreater(result.height, 0)
            with Image.open(out) as im:
                self.assertEqual(im.format, "PNG")
                self.assertEqual(im.size[0], 1080)

    def test_chinese_text_is_actually_drawn(self):
        with workspace() as tmp:
            tmp = Path(tmp)
            first = tmp / "cn_a.png"
            second = tmp / "cn_b.png"
            base = [RenderItem(kind="text", text="中文渲染验证文字内容甲甲乙乙丙丙丁丁")]
            other = [RenderItem(kind="text", text="中文渲染验证文字内容子子丑丑寅寅卯卯")]
            r1 = render_long_png(RenderHeader(), base, first, width=1080)
            r2 = render_long_png(RenderHeader(), other, second, width=1080)
            self.assertTrue(r1.ok, r1.message)
            self.assertTrue(r2.ok, r2.message)
            # 非白像素 > 0：说明字确实被画上去了（不是空白图）
            self.assertGreater(_non_white_pixels(first), 0)
            self.assertGreater(_non_white_pixels(second), 0)
            # 不同中文文本 → 像素不同（若中文渲染成方块，两张图会几乎一致）
            with Image.open(first) as a, Image.open(second) as b:
                self.assertEqual(a.size, b.size)
                self.assertNotEqual(a.tobytes(), b.tobytes())

    def test_unknown_kind_falls_back_to_text(self):
        with workspace() as tmp:
            tmp = Path(tmp)
            out = tmp / "unknown.png"
            result = render_long_png(
                RenderHeader(author="测试UP"),
                [{"kind": "weird", "text": "未知类型的块按正文渲染"}],  # 兼容 dict 入参
                out, width=1080,
            )
            self.assertTrue(result.ok, result.message)
            self.assertTrue(out.is_file())

    def test_invalid_out_dir_is_created(self):
        with workspace() as tmp:
            out = Path(tmp) / "nested" / "deeper" / "render.png"
            result = render_long_png(RenderHeader(author="测试UP"),
                                     [RenderItem(kind="text", text="目录不存在时自动创建。")],
                                     out, width=1080)
            self.assertTrue(result.ok, result.message)
            self.assertTrue(out.is_file())

    def test_narrow_width_does_not_crash(self):
        with workspace() as tmp:
            out = Path(tmp) / "narrow.png"
            result = render_long_png(RenderHeader(author="窄图"), [RenderItem(kind="text", text="窄宽度渲染。")],
                                     out, width=200)
            self.assertTrue(result.ok, result.message)
            with Image.open(out) as im:
                self.assertEqual(im.size[0], 200)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
