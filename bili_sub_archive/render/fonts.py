"""字体解析：为动态长图挑选可用的中文字体（需求 3.2.2）。

长图必须包含 UP 名称、发布时间、正文等中文文本，因此字体选择以**中文覆盖**为
第一优先级：

- :func:`resolve_font` 先认调用方显式给的 ``font_path``，再按候选顺序找系统字体；
- :func:`available_fonts` 返回实际存在的候选字体绝对路径清单（供 ``check`` 命令报告）；
- :func:`iter_font_files` 给出"显式路径 + 候选"的完整回退顺序，供渲染层逐个尝试
  ``ImageFont.truetype``（``.ttc`` 报错就换下一个候选，不崩溃）；
- 一个都找不到时 :func:`resolve_font` 返回空串，由渲染层降级到
  ``ImageFont.load_default()`` 并在 ``RenderResult.message`` 里说明"中文可能显示为方块"。

本模块**不导入 Pillow**：未安装 Pillow 时仍可安全调用（依赖自检 / ``check`` 命令用）。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 常规字重候选（按"中文覆盖 + 可读性"排序：微软雅黑 → 等线 → 黑体 → 宋体 → 雅黑细体）
NORMAL_CANDIDATES: tuple[str, ...] = (
    "msyh.ttc",
    "msyhbd.ttc",
    "Deng.ttf",
    "simhei.ttf",
    "simsun.ttc",
    "msyhl.ttc",
)

#: 加粗字重候选（把雅黑粗体提到最前；找不到粗体时退回常规字重，不影响可读性）
BOLD_CANDIDATES: tuple[str, ...] = (
    "msyhbd.ttc",
    "msyh.ttc",
    "simhei.ttf",
    "Deng.ttf",
    "simsun.ttc",
    "msyhl.ttc",
)


def font_dirs() -> list[Path]:
    """系统字体目录候选（Windows 优先，按 ``WINDIR`` / ``SystemRoot`` 定位）。"""
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    out: list[Path] = []
    for raw in (Path(windir) / "Fonts", Path(r"C:\Windows\Fonts")):
        if raw not in out:
            out.append(raw)
    return out


def candidate_files(bold: bool = False) -> list[str]:
    """按优先级返回**实际存在**的候选字体绝对路径（名称优先于目录）。"""
    names = BOLD_CANDIDATES if bold else NORMAL_CANDIDATES
    dirs = [d for d in font_dirs() if d.is_dir()]
    out: list[str] = []
    for name in names:
        for directory in dirs:
            path = directory / name
            if path.is_file():
                text = str(path)
                if text not in out:
                    out.append(text)
    return out


def available_fonts() -> list[str]:
    """实际存在的候选字体绝对路径清单（空列表 = 本机没有可用中文字体）。"""
    return candidate_files(bold=False)


def iter_font_files(bold: bool = False, font_path: str = ""):
    """字体回退顺序：显式 ``font_path``（存在时）在前，其后是候选清单。"""
    seen: set[str] = set()
    if font_path:
        try:
            explicit = Path(font_path)
        except (TypeError, ValueError):
            explicit = None
        if explicit is not None and explicit.is_file():
            text = str(explicit)
            seen.add(text)
            yield text
    for path in candidate_files(bold=bold):
        if path not in seen:
            seen.add(path)
            yield path


def resolve_font(bold: bool = False, font_path: str = "") -> str:
    """返回一个可用的字体文件绝对路径；找不到返回空串。

    ``font_path`` 非空且是文件时直接采用（调用方显式指定优先）；否则按
    :data:`BOLD_CANDIDATES` / :data:`NORMAL_CANDIDATES` 顺序取第一个存在的候选。
    这里只判断**文件存在**，能否被 Pillow 打开由渲染层逐个回退验证。
    """
    if font_path:
        try:
            explicit = Path(font_path)
            if explicit.is_file():
                return str(explicit)
        except (TypeError, ValueError):
            pass
    files = candidate_files(bold=bold)
    return files[0] if files else ""
