"""动态长图渲染（需求 3.2.2）：把一条动态渲染成单张高度随内容增长的长 PNG。

对外只暴露 :func:`render_long_png` 与配套的数据类、换行纯函数和字体工具。

Pillow 是可选依赖：未安装时 :func:`render_long_png` 返回
``RenderResult(ok=False, error_kind="dependency_missing")``，**不抛异常**，
调用方（``archive/dynamic.py`` 的 render 步骤）可以据此记 ``skipped``。
"""

from __future__ import annotations

from .fonts import available_fonts, resolve_font
from .longimage import (
    PILLOW_MISSING_MESSAGE,
    RenderHeader,
    RenderItem,
    RenderResult,
    render_long_png,
    wrap_text,
)

__all__ = [
    "PILLOW_MISSING_MESSAGE",
    "RenderHeader",
    "RenderItem",
    "RenderResult",
    "available_fonts",
    "render_long_png",
    "resolve_font",
    "wrap_text",
]
