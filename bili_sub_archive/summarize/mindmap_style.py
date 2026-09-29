"""思维导图渲染样式：预设 → Mermaid 配置 JSON + 页面 CSS（渲染步骤的样式层）。

**为什么样式不写进 ``mindmap.mmd``**：``.mmd`` 是"确定性序列化"的源文件
（见 :mod:`bili_sub_archive.summarize.mermaid`），把它绑死在某个配色上会让
"同一棵大纲 → 同一份源文件"的约定失效，也会让 ``%%{init}%%`` 指令和标签转义互相干扰。
样式属于**渲染**，因此走 mermaid-cli 的两个官方入口：

- ``-c/--configFile``：一份 Mermaid 配置 JSON（``theme`` / ``themeVariables`` /
  ``mindmap`` 选项 / ``look``）；
- ``--cssFile``：注入页面的 CSS，用来做配置做不到的事（mermaid 把节点填充色与描边色
  绑在同一个 ``cScale*`` 上，白底卡片 + 分支色描边只能靠 CSS 覆盖）。

两者的颜色都从同一份 :data:`BRANCHES` 调色板生成，配置与 CSS 不会漂移。

预设（``[mindmap] style``）：

===== ==========================================================
值     观感
===== ==========================================================
paper  **默认**：浅色卡片（白底 + 分支色描边 + 圆角光影），适合放进笔记
pastel 柔和实色块（经典 mindmap 形状 + 柔和配色 + 深色文字）
dark   深色底（``#0f172a``），亮色分支描边，适合深色主题的笔记
classic 不加任何配置：等同本次改造前的 mermaid 默认观感
===== ==========================================================
"""

from __future__ import annotations

import contextlib
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

#: 内置默认样式（``[mindmap] style`` 为空时用它）
DEFAULT_STYLE = "paper"
#: 可选样式（顺序即 ``check`` 与错误提示里的展示顺序）
STYLE_NAMES: tuple[str, ...] = ("paper", "pastel", "dark", "classic")
#: 每个样式的一句话说明（``check`` 报告与帮助文本共用）
STYLE_LABELS: dict[str, str] = {
    "paper": "浅色卡片",
    "pastel": "柔和实色块",
    "dark": "深色",
    "classic": "Mermaid 默认",
}
#: 需要渲染配置的样式（``classic`` 什么都不加，保持向后兼容）
STYLED_NAMES: frozenset[str] = frozenset({"paper", "pastel", "dark"})

#: 中文字体栈：mermaid 默认是 ``trebuchet ms, verdana, arial``，中文会掉到浏览器默认字体
DEFAULT_FONT_STACK = ("Microsoft YaHei UI, Microsoft YaHei, PingFang SC, "
                      "Hiragino Sans GB, Noto Sans CJK SC, Source Han Sans SC, sans-serif")
#: 各预设的默认字号（``[mindmap] font_size`` 显式设置时优先）
DEFAULT_FONT_SIZE = 17
#: 深色预设的页面底色（``[mindmap] background`` 保持默认 ``white`` 时用它替换）
DARK_BACKGROUND = "#0f172a"
#: 卡片最大宽度：mermaid mindmap 的换行宽度
DEFAULT_MAX_NODE_WIDTH = 220
#: 分支调色板：``fill`` 是柔和填充色，``stroke`` 是浅色底上的描边/连线色，
#: ``fill_dark`` / ``stroke_dark`` 供深色预设使用。索引与 ``cScale0..11`` 一一对应：
#: 一级分支超过 12 个时，多出来的分支只能用 mermaid 默认配色（``max_nodes`` 默认 60，
#: 实际大纲极少超过 12 个一级分支）。
BRANCHES: tuple[dict[str, str], ...] = (
    {"fill": "#e3ecfe", "stroke": "#4c7df0", "fill_dark": "#1c2b4d", "stroke_dark": "#6ea2ff"},
    {"fill": "#dcf3ef", "stroke": "#12a594", "fill_dark": "#12332f", "stroke_dark": "#35c2ae"},
    {"fill": "#fdeadd", "stroke": "#e8833a", "fill_dark": "#3a2a1c", "stroke_dark": "#f5a259"},
    {"fill": "#eae3fd", "stroke": "#7c5cf0", "fill_dark": "#2c2149", "stroke_dark": "#a68cf7"},
    {"fill": "#fce1e6", "stroke": "#d9455f", "fill_dark": "#3c1d24", "stroke_dark": "#f2748c"},
    {"fill": "#e0f2e1", "stroke": "#2f9e44", "fill_dark": "#16311b", "stroke_dark": "#5cc46f"},
    {"fill": "#dcedf8", "stroke": "#0f8bd1", "fill_dark": "#142c3a", "stroke_dark": "#45b0e8"},
    {"fill": "#f7eeda", "stroke": "#a06a1f", "fill_dark": "#342a17", "stroke_dark": "#d19a45"},
    {"fill": "#d8f0f7", "stroke": "#0e7490", "fill_dark": "#102b33", "stroke_dark": "#22d3ee"},
    {"fill": "#e9f5d8", "stroke": "#4d7c0f", "fill_dark": "#22300f", "stroke_dark": "#a3e635"},
    {"fill": "#fbe3f3", "stroke": "#c026d3", "fill_dark": "#35123a", "stroke_dark": "#e879f9"},
    {"fill": "#e6ebf2", "stroke": "#475569", "fill_dark": "#1e2733", "stroke_dark": "#94a3b8"},
)

#: 根节点（``section-root``）在三种预设下的配色
_ROOT_STYLES: dict[str, dict[str, str]] = {
    "paper": {"fill": "#eef3ff", "stroke": "#4c7df0", "text": "#17223b"},
    "pastel": {"fill": "#3f6fe0", "stroke": "#3f6fe0", "text": "#ffffff"},
    "dark": {"fill": "#16233c", "stroke": "#60a5fa", "text": "#e8eefc"},
}
#: 卡片／色块上的文字色
_TEXT_COLORS: dict[str, str] = {"paper": "#1f2d3d", "pastel": "#22303f", "dark": "#e2e8f0"}
#: 卡片填充色（``pastel`` 用调色板的 ``fill``，因此留空）
_CARD_FILLS: dict[str, str] = {"paper": "#ffffff", "dark": "#1b2537"}
#: 描边宽度与连线宽度
_STROKE_WIDTH = {"paper": "1.8px", "pastel": "1.6px", "dark": "1.8px"}
_EDGE_WIDTH = {"paper": "2.4px", "pastel": "2.6px", "dark": "2.4px"}


@dataclass
class StyleBundle:
    """一个样式的完整渲染参数。

    ``config`` / ``css`` 为空表示对应文件不需要传（``classic`` 两者都空）。
    """

    name: str = DEFAULT_STYLE
    config: dict = field(default_factory=dict)
    css: str = ""
    #: 预设建议的页面底色（空 = 不干预 ``[mindmap] background``）
    background: str = ""

    @property
    def styled(self) -> bool:
        return bool(self.config or self.css)

    @property
    def label(self) -> str:
        return STYLE_LABELS.get(self.name, self.name)


def normalize_style(name: str | None) -> str:
    """把配置里的样式名规整成合法值（未知值退回默认，不抛异常）。

    真正的"拼错了要报错"发生在 :func:`bili_sub_archive.config._validate`，
    这里只做防御：渲染步骤永远不该因为一个样式名崩溃。
    """
    key = str(name or "").strip().lower()
    if not key:
        return DEFAULT_STYLE
    return key if key in STYLE_NAMES else DEFAULT_STYLE


def is_known_style(name: str | None) -> bool:
    return str(name or "").strip().lower() in STYLE_NAMES


def style_choices_text() -> str:
    """给错误提示用的"值 → 说明"清单。"""
    return "、".join(f"{name}（{STYLE_LABELS[name]}）" for name in STYLE_NAMES)


def build_style(name: str | None = None, *, font_size: int = 0,
                font_family: str = "") -> StyleBundle:
    """按预设生成渲染参数；``font_size`` / ``font_family`` 非空时覆盖预设默认。"""
    key = normalize_style(name)
    if key not in STYLED_NAMES:
        return StyleBundle(name=key)

    family = str(font_family or "").strip() or DEFAULT_FONT_STACK
    try:
        size = int(font_size or 0)
    except (TypeError, ValueError):
        size = 0
    size = size if size > 0 else DEFAULT_FONT_SIZE

    dark = key == "dark"
    palette = [branch["stroke_dark"] if dark else branch["stroke"] for branch in BRANCHES]
    theme_variables: dict[str, object] = {
        "fontFamily": family,
        "fontSize": f"{size}px",
        "lineColor": "#334155" if dark else "#dbe2ef",
        "primaryColor": _ROOT_STYLES[key]["fill"],
        "primaryTextColor": _ROOT_STYLES[key]["text"],
        "primaryBorderColor": _ROOT_STYLES[key]["stroke"],
    }
    if dark:
        theme_variables["background"] = DARK_BACKGROUND
    for index, color in enumerate(palette):
        theme_variables[f"cScale{index}"] = color

    config = {
        "look": "neo",
        "theme": "base",
        "themeVariables": theme_variables,
        "mindmap": {
            "padding": 16 if key == "paper" else 14,
            "maxNodeWidth": DEFAULT_MAX_NODE_WIDTH,
            # 关掉"缩放到容器宽度"：宽导图不再被压扁成小字，PNG 随内容变宽
            "useMaxWidth": False,
        },
    }
    return StyleBundle(name=key, config=config, css=_style_css(key), background=_background(key))


def _background(key: str) -> str:
    return DARK_BACKGROUND if key == "dark" else ""


def _style_css(key: str) -> str:
    """按调色板生成页面 CSS（形状、描边、文字色、连线粗细、根节点）。"""
    dark = key == "dark"
    root = _ROOT_STYLES[key]
    text = _TEXT_COLORS[key]
    stroke_width = _STROKE_WIDTH[key]
    edge_width = _EDGE_WIDTH[key]
    card_fill = _CARD_FILLS.get(key, "")

    lines = [
        f"/* bili-sub-archive mindmap style={key}（渲染期注入，源文件 .mmd 不含样式） */",
        "#my-svg .mindmap-node path,",
        "#my-svg .mindmap-node rect,",
        "#my-svg .mindmap-node circle,",
        "#my-svg .mindmap-node polygon {",
        f"  stroke-width: {stroke_width} !important;",
    ]
    if card_fill:
        lines.append(f"  fill: {card_fill} !important;")
    lines += [
        "}",
        "#my-svg .mindmap-node text,",
        "#my-svg .mindmap-node tspan,",
        "#my-svg .mindmap-node span,",
        "#my-svg .mindmap-node .nodeLabel {",
        f"  fill: {text} !important;",
        f"  color: {text} !important;",
        "}",
        f'#my-svg [class*="edge-depth"] {{ stroke-width: {edge_width} !important; }}',
    ]

    for index, branch in enumerate(BRANCHES):
        fill = branch["fill_dark"] if dark else branch["fill"]
        stroke = branch["stroke_dark"] if dark else branch["stroke"]
        # 浅色卡片：填充统一走上面的 card_fill，这里只上分支描边；pastel 连填充一起给
        fill_rule = f"  fill: {fill} !important;\n" if not card_fill else ""
        lines += [
            f"#my-svg .section-{index} path,",
            f"#my-svg .section-{index} rect,",
            f"#my-svg .section-{index} circle,",
            f"#my-svg .section-{index} polygon {{",
            fill_rule + f"  stroke: {stroke} !important;",
            "}",
            f"#my-svg .section-edge-{index} {{ stroke: {stroke} !important; }}",
        ]

    lines += [
        "#my-svg .section-root path,",
        "#my-svg .section-root rect,",
        "#my-svg .section-root circle,",
        "#my-svg .section-root polygon {",
        f"  fill: {root['fill']} !important;",
        f"  stroke: {root['stroke']} !important;",
        "  stroke-width: 2.4px !important;",
        "}",
        "#my-svg .section-root text,",
        "#my-svg .section-root tspan,",
        "#my-svg .section-root span,",
        "#my-svg .section-root .nodeLabel {",
        f"  fill: {root['text']} !important;",
        f"  color: {root['text']} !important;",
        "  font-weight: 700 !important;",
        "}",
    ]
    return "\n".join(lines) + "\n"


def config_json(bundle: StyleBundle) -> str:
    """样式的 Mermaid 配置 JSON（缩进稳定，便于人工对照）。"""
    return json.dumps(bundle.config, ensure_ascii=False, indent=2) + "\n"


def resolve_background(bundle: StyleBundle, configured: str) -> str:
    """页面底色：用户显式设置优先，只有保持默认 ``white`` 时才用预设建议的底色。

    这样 ``style = "dark"`` 不需要额外改 ``background`` 就能得到深色底，
    而显式写了 ``background = "transparent"`` 的用户不会被预设覆盖。
    """
    current = str(configured or "").strip()
    if current and current.lower() != "white":
        return current
    return bundle.background or current or "white"


@contextlib.contextmanager
def staged_style_files(bundle: StyleBundle, *, config_file: str = "", css_file: str = ""):
    """把预设落成临时文件，产出 ``(config_path, css_path)`` 供 mmdc 使用。

    显式给了 ``config_file`` / ``css_file`` 的那一份以用户文件为准（整份替换）；
    都不需要时返回 ``("", "")``，渲染命令与改造前完全一致。
    需要落盘时用临时目录，退出即清理，不在产物目录留下样式文件。
    """
    explicit_config = str(config_file or "").strip()
    explicit_css = str(css_file or "").strip()
    needs_config = bool(bundle.config) and not explicit_config
    needs_css = bool(bundle.css) and not explicit_css
    if not needs_config and not needs_css:
        yield explicit_config, explicit_css
        return

    with tempfile.TemporaryDirectory(prefix="bsa-mindmap-style-") as raw_dir:
        directory = Path(raw_dir)
        config_path = explicit_config
        css_path = explicit_css
        if needs_config:
            path = directory / "mermaid-config.json"
            path.write_text(config_json(bundle), encoding="utf-8")
            config_path = str(path)
        if needs_css:
            path = directory / "mindmap.css"
            path.write_text(bundle.css, encoding="utf-8")
            css_path = str(path)
        yield config_path, css_path


__all__ = [
    "BRANCHES",
    "DARK_BACKGROUND",
    "DEFAULT_FONT_SIZE",
    "DEFAULT_FONT_STACK",
    "DEFAULT_MAX_NODE_WIDTH",
    "DEFAULT_STYLE",
    "STYLE_LABELS",
    "STYLE_NAMES",
    "STYLED_NAMES",
    "StyleBundle",
    "build_style",
    "config_json",
    "is_known_style",
    "normalize_style",
    "resolve_background",
    "staged_style_files",
    "style_choices_text",
]
