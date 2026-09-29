"""动态单张长 PNG 渲染（需求 3.2.2）。

需求原文：

> 每条动态生成一张 PNG 图片，至少包含 UP 名称、头像、发布时间、动态正文和动态
> 配图。长文本/多图应自动排版成单张高度随内容增长的长图，不截断主要内容、不分页。

实现要点：

- **模块顶层不导入 Pillow**：缺失时 ``render_long_png`` 返回
  ``RenderResult(ok=False, error_kind="dependency_missing")``，绝不抛 ``ImportError``；
- 单栏纵向流排版：头部卡片 → 分隔线 → 标题 → 正文块（heading/text/image/note）
  → 页脚，高度随内容增长，**不分页**；
- 超过 ``max_height`` 时停止追加后续内容，``clamped=True`` 并在 ``message`` 里
  明确说明被截断（不允许静默截断）；
- 换行按字符类别分段测量：CJK（含全角标点）逐字换行、拉丁/数字按词换行、超长
  单词强制断字；宽度一律用 ``ImageFont.getlength()``（Pillow ≥ 10，``getsize`` 已废弃）；
- 落盘走 :func:`bili_sub_archive.paths.atomic_write_bytes`：PNG 先写 ``BytesIO`` 再原子替换，
  不直接 ``img.save(path)``；
- ``images_rendered`` / ``images_missing`` 只统计正文配图（``RenderItem.kind == "image"``），
  头部头像单独处理、不计入这两个计数。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..paths import atomic_write_bytes, ensure_dir
from .fonts import iter_font_files

# --------------------------------------------------------------------------- #
# 依赖提示
# --------------------------------------------------------------------------- #
#: Pillow 缺失时的中文提示（契约要求：明确、可直接展示给用户）
PILLOW_MISSING_MESSAGE = "未安装 Pillow：动态长图需要 Pillow（pip install Pillow），当前跳过渲染"

# --------------------------------------------------------------------------- #
# 浅色主题色板（集中在此便于调整）
# --------------------------------------------------------------------------- #
COLOR_BG = (255, 255, 255)              # 页面底色 #FFFFFF
COLOR_TEXT = (31, 35, 41)               # 正文 #1F2329
COLOR_MUTED = (107, 114, 128)           # 次要文字 #6B7280
COLOR_RULE = (229, 231, 235)            # 分隔线 #E5E7EB
COLOR_BADGE_BG = (239, 246, 255)        # badge 底色 #EFF6FF
COLOR_BADGE_TEXT = (29, 78, 216)        # badge 文字 #1D4ED8
COLOR_LINK = (37, 99, 235)              # 链接 #2563EB（页脚原链接按需求用灰色小字，
                                        # 该常量保留给调用方/后续可点击版式调整）
COLOR_CARD_BG = (248, 250, 252)         # 头部卡片底色
COLOR_PLACEHOLDER_BG = (249, 250, 251)  # 配图占位框底色
COLOR_PLACEHOLDER_BORDER = (229, 231, 235)  # 配图占位框边框
COLOR_AVATAR_BG = (226, 232, 240)       # 头像缺失时的灰底
COLOR_AVATAR_FG = (100, 116, 139)       # 头像缺失时的首字
COLOR_NOTE_BAR = (203, 213, 225)        # note 块左侧竖线（比分隔线略深，保证可见）

# --------------------------------------------------------------------------- #
# 版式常量
# --------------------------------------------------------------------------- #
MARGIN = 48                 # 左右页边距
AVATAR_SIZE = 96            # 头像边长
CARD_PAD = 26               # 头部卡片内边距
CARD_RADIUS = 18            # 头部卡片圆角
NAME_SIZE = 34              # UP 名称字号（加粗）
TITLE_SIZE = 38             # 标题字号（加粗）
BODY_SIZE = 26              # 正文字号
BODY_LINE_SPACING = 1.7     # 正文行高倍数
META_SIZE = 22              # 发布时间 / 统计等次要信息字号
NOTE_SIZE = 23              # note 块字号
PILL_SIZE = 20              # 胶囊标记字号
PILL_HEIGHT = 34            # 胶囊高度
FOOTER_SIZE = 20            # 页脚字号
MIN_HEIGHT = 240            # 图片最小高度（内容很少时不至于太扁）
DEFAULT_HEADING_LEVEL = 3   # level 缺失/越界时的标题级别
MISSING_IMAGE_TEXT = "（配图未取得）"
FOOTER_NOTE = "由 bili-sub-archive 归档生成"

#: heading 级别 → 字号（需求 3.2.2 的"按 level 映射字号"）
HEADING_SIZES: dict[int, int] = {1: 34, 2: 30, 3: 27, 4: 25, 5: 23, 6: 22}

#: CJK / 全角 / emoji 区间：这些字符**逐字换行**，其余字符按单词换行
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x1100, 0x11FF),    # 谚文字母
    (0x2E80, 0x303F),    # CJK 部首扩展 + CJK 标点（含全角空格）
    (0x3040, 0x30FF),    # 假名
    (0x3100, 0x31BF),    # 注音 / 谚文兼容
    (0x3200, 0x4DBF),    # 带圈字符 + CJK 扩展 A
    (0x4E00, 0x9FFF),    # CJK 统一表意文字
    (0xA960, 0xA97F),
    (0xAC00, 0xD7FF),    # 谚文音节
    (0xF900, 0xFAFF),    # CJK 兼容表意文字
    (0xFE10, 0xFE1F),    # 竖排标点
    (0xFE30, 0xFE4F),    # CJK 兼容形式
    (0xFF00, 0xFF60),    # 全角 ASCII
    (0xFFE0, 0xFFE6),    # 全角符号
    (0x1F300, 0x1FAFF),  # emoji（按单字符处理，避免与相邻拉丁字母粘成一个"单词"）
    (0x20000, 0x3FFFD),  # CJK 扩展 B 及以后
)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class RenderHeader:
    """长图头部信息（全部有默认值，允许字段缺失）。"""

    author: str = ""        # UP 名称
    avatar: str = ""        # 头像本地绝对路径（空串 = 未下载到）
    published: str = ""     # 发布时间文本
    title: str = ""         # 动态标题 / 摘要
    url: str = ""           # 原链接
    badge: str = ""         # 平台标记（置顶 / 充电专属 …）
    visible: str = ""       # 可见范围文本
    stats: str = ""         # 数据文本（点赞 / 评论 / 转发）
    forward_note: str = ""  # 转发来源说明（渲染成一行次要说明）


@dataclass
class RenderItem:
    """长图正文块。"""

    kind: str = "text"      # text | heading | image | note
    text: str = ""          # text / heading / note 的文本内容
    level: int = 0          # heading 级别 1~6（0/越界按 3 处理）
    image: str = ""         # kind == "image" 时的本地绝对路径
    caption: str = ""       # 图片缺失/读取失败时显示的替代文本


@dataclass
class RenderResult:
    """渲染结果（任何异常都转成 ``ok=False`` 的这个结构，不向上抛）。"""

    ok: bool = False
    path: Path | None = None
    width: int = 0
    height: int = 0
    bytes_written: int = 0
    images_rendered: int = 0
    images_missing: int = 0
    clamped: bool = False
    error_kind: str = ""    # dependency_missing | render_error | ""
    message: str = ""


@dataclass
class _Op:
    """一次绘制操作（先测量排版、最后一次性画到图上）。"""

    kind: str               # text | paste | rect | round_rect | line | ellipse
    box: tuple
    text: str = ""
    font: Any = None
    fill: Any = None
    outline: Any = None
    width: int = 1
    radius: int = 0
    image: Any = None
    mask: Any = None


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _log(logger, level: str, message: str) -> None:
    """防御式日志：logger 为 None 或没有该方法时静默。"""
    func = getattr(logger, level, None)
    if not callable(func):
        return
    try:
        func(message)
    except Exception:  # 日志失败绝不影响渲染
        pass


def _field(obj: Any, name: str, default: Any = "") -> Any:
    """按字段名取值，兼容 dataclass 与 dict 两种入参。"""
    if isinstance(obj, dict):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    return default if value is None else value


def is_cjk(ch: str) -> bool:
    """该字符是否按"逐字换行"处理（CJK、全角标点、emoji）。"""
    if not ch:
        return False
    code = ord(ch)
    for low, high in _CJK_RANGES:
        if low <= code <= high:
            return True
    return False


def _text_width(text: str, font) -> float:
    """文本宽度：Pillow ≥ 10 用 ``getlength()``（``getsize`` 已废弃）。"""
    if not text:
        return 0.0
    getlength = getattr(font, "getlength", None)
    if callable(getlength):
        try:
            return float(getlength(text))
        except Exception:
            pass
    try:  # 极旧 Pillow 兜底（仅在 getlength 不可用时走到这里）
        return float(font.getsize(text)[0])
    except Exception:
        return float(len(text) * 10)


def _font_metrics(font, fallback_size: int) -> tuple[int, int]:
    """``(ascent, descent)``；取不到时按字号估算。"""
    try:
        ascent, descent = font.getmetrics()
        return int(ascent), int(descent)
    except Exception:
        return int(fallback_size * 0.8), int(fallback_size * 0.2)


def _tokenize(segment: str) -> list[str]:
    """把一段文本切成换行单元：CJK 单字、空白单字符、其余连续串（单词）。"""
    tokens: list[str] = []
    buf: list[str] = []
    for ch in segment:
        if ch.isspace() or is_cjk(ch):
            if buf:
                tokens.append("".join(buf))
                buf = []
            tokens.append(ch)
        else:
            buf.append(ch)
    if buf:
        tokens.append("".join(buf))
    return tokens


def _break_word(word: str, font, max_width: float) -> list[str]:
    """超长单词强制断字（保证不溢出；单字符本身超宽时该行仍然超宽，不再递归）。"""
    pieces: list[str] = []
    cur = ""
    for ch in word:
        if cur and _text_width(cur + ch, font) > max_width:
            pieces.append(cur)
            cur = ""
        cur += ch
    pieces.append(cur)
    return pieces


def _wrap_segment(segment: str, font, max_width: float) -> list[str]:
    """单段（不含 ``\\n``）换行。"""
    if segment == "":
        return [""]
    lines: list[str] = []
    cur = ""
    for token in _tokenize(segment):
        if token.isspace():
            if cur:                     # 行首不留空格
                cur += token
            continue
        candidate = cur + token
        if cur and _text_width(candidate, font) > max_width:
            lines.append(cur.rstrip())  # 当前行放不下 → 断行
            cur, candidate = "", token
        if _text_width(candidate, font) > max_width:
            pieces = _break_word(token, font, max_width)
            lines.extend(pieces[:-1])
            cur = pieces[-1]
            continue
        cur = candidate
    lines.append(cur.rstrip())
    return lines


def wrap_text(text: str, font, max_width: float) -> list[str]:
    """按字符类别换行，返回每行文本（行尾空格已去掉）。

    规则（需求 3.2.2 的"自动排版"）：

    - CJK（含全角标点、emoji）**逐字换行**，不依赖空格；
    - 拉丁字母/数字按**单词**换行，超长单词强制断字；
    - 中英混排按字符类别分段测量宽度；
    - ``\\n`` 保留为换行（空段产生一个空行）；空串返回空列表。

    纯函数：只依赖 ``font.getlength``，可脱离渲染流程单独测试。
    """
    if text is None:
        return []
    raw = str(text)
    if raw == "":
        return []
    if max_width is None or max_width <= 0:
        return [raw]
    lines: list[str] = []
    for segment in raw.split("\n"):
        lines.extend(_wrap_segment(segment, font, float(max_width)))
    return lines


def _paragraph_lines(text: str, font, max_width: float) -> list[str]:
    """正文换行 + 连续空行折叠为一个空行 + 去掉首尾空行。"""
    lines: list[str] = []
    for line in wrap_text(text, font, max_width):
        if line == "" and lines and lines[-1] == "":
            continue
        lines.append(line)
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _level(value: Any) -> int:
    """heading 级别规整：1~6 之外一律按 3 处理。"""
    try:
        level = int(value)
    except (TypeError, ValueError):
        return DEFAULT_HEADING_LEVEL
    return level if 1 <= level <= 6 else DEFAULT_HEADING_LEVEL


def _resample():
    """缩放算法：新版 Pillow 用 ``Resampling.LANCZOS``，旧版回退 ``LANCZOS``。"""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - 调用前已确认 Pillow 可用
        return None
    resampling = getattr(Image, "Resampling", None)
    if resampling is not None and hasattr(resampling, "LANCZOS"):
        return resampling.LANCZOS
    return getattr(Image, "LANCZOS", 1)


def _load_image(path: str):
    """打开本地图片并转成 RGB；任何失败（缺失/损坏/非图片）返回 None。"""
    if not path:
        return None
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        target = Path(str(path))
        if not target.is_file():
            return None
        with Image.open(target) as im:
            im.load()
            try:
                transposed = ImageOps.exif_transpose(im)
                if transposed is not None:
                    im = transposed
            except Exception:
                pass
            return im.convert("RGB")
    except Exception:
        return None


def _cover_resize(img, width: int, height: int):
    """等比缩放并居中裁剪到 ``width×height``（头像用）。"""
    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0:
        raise ValueError("图片尺寸异常")
    ratio = max(width / float(src_w), height / float(src_h))
    new_size = (max(1, int(round(src_w * ratio))), max(1, int(round(src_h * ratio))))
    resized = img.resize(new_size, _resample())
    left = (new_size[0] - width) // 2
    top = (new_size[1] - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _initial(author: str) -> str:
    """头像缺失时显示的名称首字。"""
    for ch in str(author or ""):
        if not ch.isspace():
            return ch
    return "?"


# --------------------------------------------------------------------------- #
# 字体缓存
# --------------------------------------------------------------------------- #
class _FontSet:
    """字体缓存：同一 ``(bold, size)`` 只加载一次，加载失败逐个候选回退。

    全部候选都失败时降级到 ``ImageFont.load_default()``（中文可能显示为方块），
    并置 ``used_default``，由调用方写进 ``RenderResult.message``。
    """

    def __init__(self, font_path: str = "", logger=None):
        self.font_path = font_path or ""
        self.logger = logger
        self._cache: dict[tuple[bool, int], Any] = {}
        self.used_default = False
        self.used_paths: list[str] = []

    def get(self, size: int, bold: bool = False):
        key = (bool(bold), int(size))
        if key in self._cache:
            return self._cache[key]
        font = self._load(int(size), bool(bold))
        self._cache[key] = font
        return font

    def _load(self, size: int, bold: bool):
        from PIL import ImageFont

        last_error: Exception | None = None
        for path in iter_font_files(bold=bold, font_path=self.font_path):
            try:
                # .ttc 需要 index=0；报错（字体损坏/不被 Pillow 支持）就换下一个候选
                font = ImageFont.truetype(path, size, index=0)
            except Exception as exc:
                last_error = exc
                _log(self.logger, "debug", f"字体不可用，回退下一个候选：{path}（{exc}）")
                continue
            if path not in self.used_paths:
                self.used_paths.append(path)
            return font
        self.used_default = True
        if last_error is not None:
            _log(self.logger, "warning",
                 f"全部候选字体加载失败（{last_error}），改用内置位图字体，中文可能显示为方块")
        else:
            _log(self.logger, "warning",
                 "未找到中文字体（Windows 字体缺失），改用内置位图字体，中文可能显示为方块")
        try:
            return ImageFont.load_default()
        except Exception:  # pragma: no cover - 极端兜底；draw.text(font=None) 用内置字体
            return None


# --------------------------------------------------------------------------- #
# 排版（先测量、后绘制）
# --------------------------------------------------------------------------- #
class _LongImageBuilder:
    """单栏纵向流排版器：累计高度、记录绘制操作，最后一次性画到图上。"""

    def __init__(self, width: int, max_height: int, fonts: _FontSet, logger=None):
        self.width = int(width)
        self.max_height = int(max_height)
        self.margin = max(8, min(MARGIN, self.width // 6))
        self.content_width = max(1, self.width - 2 * self.margin)
        self.fonts = fonts
        self.logger = logger
        self.y = self.margin
        self.ops: list[_Op] = []
        self.clamped = False
        self.images_rendered = 0
        self.images_missing = 0

    # ---------------- 基础 ---------------- #
    def _reserve(self, height: int) -> bool:
        """检查剩余空间：放不下就标记截断并停止后续内容（不静默截断）。"""
        if self.clamped:
            return False
        if self.y + height > self.max_height:
            self.clamped = True
            _log(self.logger, "warning",
                 f"内容高度超过 max_height={self.max_height}，已截断（当前 y={self.y}）")
            return False
        return True

    def _text(self, x: float, y: float, text: str, font, fill) -> None:
        self.ops.append(_Op("text", (x, y), text=text, font=font, fill=fill))

    def _rule(self, y: float) -> None:
        self.ops.append(_Op("line", (self.margin, y, self.width - self.margin, y),
                            fill=COLOR_RULE, width=1))

    # ---------------- 头部 ---------------- #
    def header(self, header: RenderHeader) -> None:
        pad = CARD_PAD
        gap = 22
        inner_x = self.margin + pad
        right_x = inner_x + AVATAR_SIZE + gap
        right_w = max(1, self.width - self.margin - pad - right_x)
        blocks = self._header_blocks(header, right_w)
        right_h = sum(block[2] for block in blocks)
        card_h = max(AVATAR_SIZE, right_h) + 2 * pad
        if not self._reserve(card_h + 24):
            return
        y0 = self.y
        self.ops.append(_Op("round_rect",
                            (self.margin, y0, self.width - self.margin, y0 + card_h),
                            fill=COLOR_CARD_BG, outline=COLOR_RULE, width=1, radius=CARD_RADIUS))
        self._avatar(header.avatar, inner_x, y0 + pad, AVATAR_SIZE, header.author)
        cursor = y0 + pad
        for kind, payload, height in blocks:
            if kind == "text":
                text, font, fill = payload
                self._text(right_x, cursor, text, font, fill)
            elif kind == "pills":
                self._pills(payload, right_x, cursor, right_w)
            cursor += height
        self.y = y0 + card_h + 24

    def _header_blocks(self, header: RenderHeader, width: float) -> list[tuple]:
        """头部右列内容块：``(kind, payload, height)``，先测量后绘制。"""
        blocks: list[tuple] = []
        name = str(header.author or "").strip() or "（未知 UP）"
        name_font = self.fonts.get(NAME_SIZE, bold=True)
        name_lh = int(NAME_SIZE * 1.35)
        for line in wrap_text(name, name_font, width)[:3]:
            blocks.append(("text", (line, name_font, COLOR_TEXT), name_lh))

        meta_font = self.fonts.get(META_SIZE)
        meta_lh = int(META_SIZE * 1.5)
        published = str(header.published or "").strip()
        if published:
            blocks.append(("text", (f"发布时间：{published}", meta_font, COLOR_MUTED), meta_lh))

        pills: list[str] = []
        for value in (header.badge, header.visible):
            text = str(value or "").strip()
            if text and text not in pills:
                pills.append(text)
        if pills:
            blocks.append(("pills", pills, PILL_HEIGHT + 10))

        stats = str(header.stats or "").strip()
        if stats:
            blocks.append(("text", (stats, meta_font, COLOR_MUTED), meta_lh))

        forward_note = str(header.forward_note or "").strip()
        if forward_note:
            for line in wrap_text(forward_note, meta_font, width):
                blocks.append(("text", (line, meta_font, COLOR_MUTED), meta_lh))
        return blocks

    def _pills(self, values: list[str], x: float, y: float, max_width: float) -> None:
        font = self.fonts.get(PILL_SIZE)
        pad_x = 14
        ascent, descent = _font_metrics(font, PILL_SIZE)
        cursor = x
        for value in values:
            box_w = _text_width(value, font) + 2 * pad_x
            if cursor > x and cursor + box_w > x + max_width:
                break  # 头部胶囊只摆一行，放不下就省略（不做多行胶囊）
            self.ops.append(_Op("round_rect", (cursor, y, cursor + box_w, y + PILL_HEIGHT),
                                fill=COLOR_BADGE_BG, radius=PILL_HEIGHT // 2))
            self._text(cursor + pad_x, y + (PILL_HEIGHT - (ascent + descent)) / 2.0,
                       value, font, COLOR_BADGE_TEXT)
            cursor += box_w + 10

    def _avatar(self, path: str, x: float, y: float, size: int, author: str) -> None:
        from PIL import Image, ImageDraw

        img = _load_image(path)
        if img is not None:
            try:
                fitted = _cover_resize(img, size, size)
                mask = Image.new("L", (size, size), 0)
                ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
                self.ops.append(_Op("paste", (x, y), image=fitted, mask=mask))
                self.ops.append(_Op("ellipse", (x, y, x + size - 1, y + size - 1),
                                    outline=COLOR_RULE, width=1))
                return
            except Exception as exc:
                _log(self.logger, "debug", f"头像处理失败，降级为占位头像：{exc}")
        # 头像缺失/损坏：灰底圆 + UP 名称首字
        self.ops.append(_Op("ellipse", (x, y, x + size - 1, y + size - 1), fill=COLOR_AVATAR_BG))
        initial = _initial(author)
        font = self.fonts.get(int(size * 0.42), bold=True)
        ascent, descent = _font_metrics(font, int(size * 0.42))
        self._text(x + (size - _text_width(initial, font)) / 2.0,
                   y + (size - (ascent + descent)) / 2.0, initial, font, COLOR_AVATAR_FG)

    # ---------------- 分隔线 / 标题 ---------------- #
    def divider(self, before: int = 26, after: int = 26) -> None:
        if not self._reserve(before + 1 + after):
            return
        self.y += before
        self._rule(self.y)
        self.y += 1 + after

    def title(self, title: str) -> None:
        text = str(title or "").strip()
        if not text:
            return
        font = self.fonts.get(TITLE_SIZE, bold=True)
        line_h = int(TITLE_SIZE * 1.4)
        lines = wrap_text(text, font, self.content_width)
        if not self._reserve(8 + len(lines) * line_h + 22):
            return
        self.y += 8
        for line in lines:
            self._text(self.margin, self.y, line, font, COLOR_TEXT)
            self.y += line_h
        self.y += 22

    # ---------------- 正文块 ---------------- #
    def heading(self, text: str, level: int) -> None:
        raw = str(text or "").strip()
        if not raw:
            return
        size = HEADING_SIZES[_level(level)]
        font = self.fonts.get(size, bold=True)
        line_h = int(size * 1.45)
        lines = wrap_text(raw, font, self.content_width)
        if not self._reserve(28 + len(lines) * line_h + 16):
            return
        self.y += 28
        for line in lines:
            self._text(self.margin, self.y, line, font, COLOR_TEXT)
            self.y += line_h
        self.y += 16

    def text_block(self, text: str) -> None:
        raw = str(text or "")
        if not raw.strip():
            return
        font = self.fonts.get(BODY_SIZE)
        line_h = int(round(BODY_SIZE * BODY_LINE_SPACING))
        lines = _paragraph_lines(raw, font, self.content_width)
        if not lines:
            return
        if not self._reserve(len(lines) * line_h + 12):
            return
        for line in lines:
            if line:
                self._text(self.margin, self.y, line, font, COLOR_TEXT)
            self.y += line_h
        self.y += 12

    def note(self, text: str) -> None:
        raw = str(text or "").strip()
        if not raw:
            return
        font = self.fonts.get(NOTE_SIZE)
        line_h = int(NOTE_SIZE * 1.6)
        lines = _paragraph_lines(raw, font, self.content_width - 30) or [raw]
        block_h = max(len(lines) * line_h, line_h)
        if not self._reserve(14 + block_h + 14):
            return
        self.y += 14
        y0 = self.y
        self.ops.append(_Op("rect", (self.margin, y0 + 2, self.margin + 4, y0 + block_h - 2),
                            fill=COLOR_NOTE_BAR))
        cursor = y0
        for line in lines:
            if line:
                self._text(self.margin + 18, cursor, line, font, COLOR_MUTED)
            cursor += line_h
        self.y = y0 + block_h + 14

    def image(self, path: str, caption: str) -> None:
        img = _load_image(path)
        if img is None:
            if self.placeholder(str(caption or "").strip() or MISSING_IMAGE_TEXT):
                self.images_missing += 1
            return
        src_w, src_h = img.size
        if src_w <= 0 or src_h <= 0:
            if self.placeholder(str(caption or "").strip() or MISSING_IMAGE_TEXT):
                self.images_missing += 1
            return
        ratio = min(1.0, self.content_width / float(src_w))  # 只缩不放，避免小图糊成马赛克
        draw_w = max(1, int(round(src_w * ratio)))
        draw_h = max(1, int(round(src_h * ratio)))
        if not self._reserve(18 + draw_h + 18):
            return
        self.images_rendered += 1
        self.y += 18
        x = self.margin + (self.content_width - draw_w) // 2
        self.ops.append(_Op("paste", (x, self.y), image=img.resize((draw_w, draw_h), _resample())))
        self.y += draw_h + 18

    def placeholder(self, text: str, min_height: int = 120) -> bool:
        """配图缺失时的浅底占位框（浅底 + 边框 + 居中说明文字）。"""
        font = self.fonts.get(NOTE_SIZE)
        line_h = int(NOTE_SIZE * 1.6)
        lines = _paragraph_lines(text, font, self.content_width - 40) or [MISSING_IMAGE_TEXT]
        box_h = max(min_height, len(lines) * line_h + 48)
        if not self._reserve(18 + box_h + 18):
            return False
        self.y += 18
        y0 = self.y
        self.ops.append(_Op("rect", (self.margin, y0, self.width - self.margin, y0 + box_h),
                            fill=COLOR_PLACEHOLDER_BG, outline=COLOR_PLACEHOLDER_BORDER, width=1))
        cursor = y0 + (box_h - len(lines) * line_h) // 2
        for line in lines:
            self._text(self.margin + (self.content_width - _text_width(line, font)) / 2.0,
                       cursor, line, font, COLOR_MUTED)
            cursor += line_h
        self.y = y0 + box_h + 18
        return True

    # ---------------- 页脚 ---------------- #
    def footer(self, url: str) -> None:
        font = self.fonts.get(FOOTER_SIZE)
        line_h = int(FOOTER_SIZE * 1.6)
        lines: list[str] = []
        link = str(url or "").strip()
        if link:
            lines.extend(wrap_text(f"原链接：{link}", font, self.content_width))
        lines.append(FOOTER_NOTE)
        block_h = len(lines) * line_h
        if not self._reserve(24 + 1 + 18 + block_h + self.margin):
            return
        self.y += 24
        self._rule(self.y)
        self.y += 1 + 18
        for line in lines:
            self._text(self.margin, self.y, line, font, COLOR_MUTED)
            self.y += line_h
        self.y += self.margin


def _paint(img, ops: list[_Op]) -> None:
    """把排版阶段记录的绘制操作画到图上。"""
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    for op in ops:
        if op.kind == "text":
            draw.text((op.box[0], op.box[1]), op.text, font=op.font, fill=op.fill)
        elif op.kind == "paste":
            img.paste(op.image, (int(op.box[0]), int(op.box[1])), op.mask)
        elif op.kind == "rect":
            draw.rectangle(op.box, fill=op.fill, outline=op.outline, width=op.width)
        elif op.kind == "round_rect":
            draw.rounded_rectangle(op.box, radius=op.radius, fill=op.fill,
                                   outline=op.outline, width=op.width)
        elif op.kind == "line":
            draw.line(op.box, fill=op.fill, width=op.width)
        elif op.kind == "ellipse":
            draw.ellipse(op.box, fill=op.fill, outline=op.outline, width=op.width)


# --------------------------------------------------------------------------- #
# 对外主函数
# --------------------------------------------------------------------------- #
def render_long_png(
    header: RenderHeader,
    items: list[RenderItem],
    out_path: Path,
    *,
    width: int = 1080,
    max_height: int = 20000,
    font_path: str = "",
    scale: int = 1,
    logger=None,
) -> RenderResult:
    """渲染单张长图（需求 3.2.2）。任何异常都不得抛出，一律转成 ``RenderResult(ok=False, ...)``。

    :param header: 头部信息（UP 名称/头像/发布时间/标题/链接/标记/统计）
    :param items: 正文块（text / heading / image / note），按顺序纵向排列
    :param out_path: 输出 PNG 路径（先写内存再 ``atomic_write_bytes`` 原子替换）
    :param width: 图片宽度；``scale`` 为预留参数（当前仅支持 1，即按 ``width`` 直接渲染）
    :param max_height: 高度上限；超过时停止追加后续内容并置 ``clamped=True``
    :param font_path: 显式字体文件；空串则自动挑系统中文字体
    :param logger: 可选最小日志接口（``debug`` / ``info`` / ``warning`` / ``error``）
    """
    try:
        return _render_long_png(
            header, items, out_path,
            width=width, max_height=max_height, font_path=font_path,
            scale=scale, logger=logger,
        )
    except Exception as exc:  # 契约：绝不向上抛异常
        _log(logger, "error", f"动态长图渲染失败：{exc}")
        return RenderResult(ok=False, error_kind="render_error", message=f"渲染失败：{exc}")


def _render_long_png(header, items, out_path, *, width, max_height, font_path, scale, logger) -> RenderResult:
    """实际渲染流程（异常由 :func:`render_long_png` 兜底）。"""
    try:
        from PIL import Image  # 惰性导入：模块顶层不得 import PIL
    except ImportError:
        _log(logger, "warning", PILLOW_MISSING_MESSAGE)
        return RenderResult(ok=False, error_kind="dependency_missing",
                            message=PILLOW_MISSING_MESSAGE)

    if int(scale or 1) != 1:
        _log(logger, "debug", f"scale={scale} 暂未实现（预留参数），按 scale=1 渲染")
    try:
        width = int(width)
    except (TypeError, ValueError):
        width = 1080
    width = max(64, width)
    try:
        max_height = int(max_height)
    except (TypeError, ValueError):
        max_height = 20000
    max_height = max(64, max_height)

    header = header if header is not None else RenderHeader()
    items = list(items or [])

    fonts = _FontSet(font_path=font_path, logger=logger)
    builder = _LongImageBuilder(width, max_height, fonts, logger=logger)
    builder.header(header)
    builder.divider()
    builder.title(str(_field(header, "title", "")))
    for item in items:
        kind = str(_field(item, "kind", "text") or "text").strip().lower()
        if kind == "heading":
            builder.heading(str(_field(item, "text", "")), _level(_field(item, "level", 0)))
        elif kind == "image":
            builder.image(str(_field(item, "image", "")), str(_field(item, "caption", "")))
        elif kind == "note":
            builder.note(str(_field(item, "text", "")))
        else:
            builder.text_block(str(_field(item, "text", "")))
    builder.footer(str(_field(header, "url", "")))

    content_height = int(builder.y)
    height = max(MIN_HEIGHT, content_height)
    if height > max_height:
        height = max_height
    clamped = bool(builder.clamped) or content_height > max_height
    builder.clamped = clamped

    img = Image.new("RGB", (width, height), COLOR_BG)
    _paint(img, builder.ops)

    out_path = Path(out_path)
    ensure_dir(out_path.parent)
    buf = io.BytesIO()
    img.save(buf, format="PNG")          # 先写内存，再原子落盘
    blob = buf.getvalue()
    atomic_write_bytes(out_path, blob)

    notes: list[str] = []
    if clamped:
        notes.append(f"内容高度超过 max_height={max_height}，已截断；正文可能不完整")
    if fonts.used_default:
        notes.append("未找到可用的中文字体（Windows 字体缺失），已降级为内置位图字体：中文可能显示为方块")
    if builder.images_missing:
        notes.append(f"{builder.images_missing} 张配图缺失或读取失败，已渲染占位说明")
    message = "；".join(notes)
    _log(logger, "info",
         f"动态长图已生成：{out_path}（{width}×{height}，{len(blob)} 字节，"
         f"配图 {builder.images_rendered} 张、缺失 {builder.images_missing} 张）")
    if message:
        _log(logger, "warning", message)

    return RenderResult(
        ok=True,
        path=out_path,
        width=width,
        height=height,
        bytes_written=len(blob),
        images_rendered=builder.images_rendered,
        images_missing=builder.images_missing,
        clamped=clamped,
        error_kind="",
        message=message,
    )
