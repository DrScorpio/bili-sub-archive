"""``mindmap.mmd`` → ``mindmap.png``：调用 Mermaid CLI（``mmdc``）并校验产物。

需求 3.3.4：除 ``mindmap.mmd`` 之外还要渲染 ``mindmap.png``；计划 3.3 补充
"**PNG 失败保留 ``.mmd`` 供重试**"，因此本模块：

1. 只认外部可执行文件 ``mmdc``（Mermaid CLI），不做"自己实现一个渲染器"这种重复造轮子；
2. 渲染到临时文件名再原子替换 —— 半张图不会被当成成功产物；
3. 校验 PNG 签名与 IHDR 尺寸（纯标准库读头，不需要 Pillow），空文件/HTML 错误页会被
   判为 ``render_invalid``；
4. 失败一律返回 :class:`RenderOutcome`，不抛异常：``.mmd`` 已经落盘，``retry`` 只补渲染。
"""

from __future__ import annotations

import os
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..deps import executable_path
from ..paths import ensure_dir

MMDC_HINT = "npm install -g @mermaid-js/mermaid-cli（需要 Node.js；首次渲染会下载 Chromium）"
MMDC_EXE = "mmdc"
PNG_NAME = "mindmap.png"

#: PNG 最小合理体积（再小基本是空白图或错误页）
MIN_PNG_BYTES = 200
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@dataclass
class RenderOutcome:
    """一次渲染尝试的结果。"""

    ok: bool = False
    png: str = ""
    bytes_written: int = 0
    width: int = 0
    height: int = 0
    error_kind: str = ""
    message: str = ""
    argv: list[str] = field(default_factory=list)
    stdout_tail: str = ""

    def summary(self) -> str:
        if not self.ok:
            return f"渲染失败（{self.error_kind or 'unknown'}）：{self.message[:120]}"
        return (f"{self.width}×{self.height}，{self.bytes_written / 1024:.1f} KiB"
                if self.bytes_written else "已渲染")


def probe_mmdc(config=None) -> str:
    """定位 ``mmdc``：显式配置优先（可为可执行文件或所在目录），其次 PATH。"""
    explicit = str(getattr(config, "mindmap_mmdc_path", "") or "")
    return executable_path(MMDC_EXE, explicit)


def png_size(path: str | Path) -> tuple[int, int]:
    """读 PNG 的 IHDR 宽高（不是 PNG 则返回 ``(0, 0)``）。"""
    try:
        with Path(path).open("rb") as fh:
            head = fh.read(33)
    except OSError:
        return (0, 0)
    if len(head) < 24 or not head.startswith(PNG_MAGIC):
        return (0, 0)
    if head[12:16] != b"IHDR":
        return (0, 0)
    width, height = struct.unpack(">II", head[16:24])
    return (int(width), int(height))


def build_argv(mmdc: str, mmd_path: Path, png_path: Path, *, width: int = 1600,
               background: str = "white", puppeteer_config: str = "") -> list[str]:
    argv = [str(mmdc), "-i", str(mmd_path), "-o", str(png_path), "-w", str(int(width))]
    if background:
        argv += ["-b", str(background)]
    if puppeteer_config:
        argv += ["-p", str(puppeteer_config)]
    return argv


def _default_runner(timeout: float):
    def runner(argv: list[str]) -> tuple[int, str, str]:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)  # noqa: S603
        return (proc.returncode,
                proc.stdout.decode("utf-8", "replace"),
                proc.stderr.decode("utf-8", "replace"))

    return runner


def render_mindmap(
    *,
    mmd_path: str | Path,
    png_path: str | Path,
    mmdc: str = "",
    width: int = 1600,
    background: str = "white",
    timeout: float = 120.0,
    puppeteer_config: str = "",
    runner=None,
    logger=None,
) -> RenderOutcome:
    """渲染 ``.mmd`` 为 ``.png``（失败返回结果对象，不抛异常）。"""
    mmd_path = Path(mmd_path)
    png_path = Path(png_path)
    outcome = RenderOutcome()
    if not mmdc:
        outcome.error_kind = "dependency_missing"
        outcome.message = f"未找到 mermaid-cli（mmdc）：{MMDC_HINT}"
        return outcome
    if not mmd_path.is_file():
        outcome.error_kind = "missing_source"
        outcome.message = f"缺少 Mermaid 源文件：{mmd_path}"
        return outcome

    ensure_dir(png_path.parent)
    tmp = png_path.with_name(png_path.stem + ".tmp" + png_path.suffix)
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:  # pragma: no cover - 上一次残留被占用
            pass

    outcome.argv = build_argv(mmdc, mmd_path, tmp, width=width, background=background,
                              puppeteer_config=puppeteer_config)
    execute = runner or _default_runner(float(timeout))
    try:
        code, stdout, stderr = execute(outcome.argv)
    except subprocess.TimeoutExpired:
        _cleanup(tmp)
        outcome.error_kind = "timeout"
        outcome.message = f"mmdc 超过 {timeout:.0f}s 未完成（可加大 [mindmap] timeout_seconds）"
        return outcome
    except FileNotFoundError:
        _cleanup(tmp)
        outcome.error_kind = "dependency_missing"
        outcome.message = f"无法执行 {mmdc}：{MMDC_HINT}"
        return outcome
    except Exception as exc:  # noqa: BLE001 - 渲染失败不该让整条归档崩掉
        _cleanup(tmp)
        outcome.error_kind = "render_failed"
        outcome.message = f"{type(exc).__name__}: {exc}"[:300]
        return outcome

    outcome.stdout_tail = (stderr or stdout or "").strip()[-400:]
    if code != 0:
        _cleanup(tmp)
        outcome.error_kind = "render_failed"
        outcome.message = f"mmdc 退出码 {code}：{outcome.stdout_tail[:300] or '无输出'}"
        return outcome

    ok, note = _validate_png(tmp)
    if not ok:
        _cleanup(tmp)
        outcome.error_kind = "render_invalid"
        outcome.message = note
        return outcome

    try:
        os.replace(tmp, png_path)
    except OSError as exc:
        _cleanup(tmp)
        outcome.error_kind = "output_error"
        outcome.message = f"PNG 落盘失败：{exc}"
        return outcome

    width_px, height_px = png_size(png_path)
    outcome.ok = True
    outcome.png = png_path.name
    outcome.bytes_written = png_path.stat().st_size
    outcome.width = width_px
    outcome.height = height_px
    outcome.message = outcome.summary()
    if logger is not None:
        logger.info(f"[导图] {png_path.name} 渲染完成：{outcome.message}")
    return outcome


def _validate_png(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, "mmdc 报告成功但没有产出 PNG 文件"
    size = path.stat().st_size
    if size < MIN_PNG_BYTES:
        return False, f"PNG 体积异常（{size} 字节，可能是空白图或错误页）"
    width, height = png_size(path)
    if not width or not height:
        return False, "产物不是合法 PNG（缺少 PNG 签名或 IHDR）"
    return True, ""


def _cleanup(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:  # pragma: no cover
        pass


__all__ = [
    "MIN_PNG_BYTES",
    "MMDC_EXE",
    "MMDC_HINT",
    "PNG_MAGIC",
    "PNG_NAME",
    "RenderOutcome",
    "build_argv",
    "png_size",
    "probe_mmdc",
    "render_mindmap",
]
