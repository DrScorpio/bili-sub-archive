"""归档器：把已发现的条目落成本地文件。

各模块的约定：

- 入口函数签名 ``archive_<kind>(ctx, item, entry) -> ArchiveResult``；
- 每个已实现的步骤完成后显式 ``ctx.finish_step(...)``，未实现的步骤记
  ``skipped`` + 原因（阶段 2/3 接管）；
- 任何一步失败都**不删除**已有产物，也不写"看起来成功"的空文件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..bili.client import ApiResult, DownloadResult
from ..models import KIND_LABEL, STEP_DONE, STEP_FAILED, STEP_SKIPPED
from ..store import EntryState


@dataclass
class ArchiveResult:
    """单条目的归档结果。"""

    outcome: str = "done"          # done | partial | failed | invisible | denied | not_found | skipped
    error_kind: str = ""
    error_code: Any = None
    message: str = ""
    artifacts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: 媒体下载字节数（阶段 2）：yt-dlp 不走 HttpClient，需单独计入运行摘要
    media_bytes: int = 0

@dataclass
class ArchiveContext:
    """归档器共享上下文（api / store / 配置 / 日志 / 脱敏 / 条目级缓存）。"""

    api: Any
    store: Any
    config: Any
    logger: Any
    redactor: Any
    detail_cache: dict = field(default_factory=dict)
    #: 登录 Cookie：阶段 2 的 yt-dlp 下载需要它（yt-dlp 不走 HttpClient）
    cookie: str = ""
    #: LLM 密钥：阶段 3 的总结需要它（只用于请求头，绝不落盘/入日志）
    llm_api_key: str = ""
    #: 阶段 2 的可注入依赖（离线测试注入替身；生产为 None = 用真实实现）
    downloader: Any = None
    transcriber: Any = None
    #: ffmpeg/ffprobe 的命令执行器替身：``(argv) -> (returncode, stdout, stderr)``
    ffmpeg_runner: Any = None
    #: 阶段 3 的可注入依赖：总结用的 Chat 客户端 + mmdc 命令执行器
    #: （``chat_client`` 为 None 时按配置装配；``mmdc_runner`` 签名同 ffmpeg_runner）
    chat_client: Any = None
    mmdc_runner: Any = None
    #: ``--force``：忽略"输入指纹一致就复用"，强制重做（总结/导图会重新调用模型）
    force: bool = False
    #: 终端呈现层（实时进度）：``None`` = 不显示进度（离线测试 / 非 TTY）。
    #: 归档器把它透传给 ``download_media(ui=...)`` / ``download_images(ui=...)`` /
    #: ``build_transcript(ui=...)``，由那些函数按需创建子进度任务。
    ui: Any = None

    def cache_get(self, key: tuple) -> Any:
        return self.detail_cache.get(key)

    def cache_put(self, key: tuple, value: Any) -> None:
        self.detail_cache[key] = value

    # ---------------- 步骤状态 ----------------
    def finish_step(self, entry: EntryState, name: str, artifacts: list[str] | None = None,
                    reason: str = "", message: str = "") -> None:
        self.store.set_step(entry, name, STEP_DONE, reason=reason, message=message,
                            artifacts=artifacts or [])

    def skip_step(self, entry: EntryState, name: str, reason: str, message: str = "") -> None:
        self.store.set_step(entry, name, STEP_SKIPPED, reason=reason,
                            message=self.redactor.redact(message) if message else "",
                            write=True)

    def skip_with_error(self, entry: EntryState, name: str, reason: str,
                        error_kind: str, message: str, error_code: Any = None) -> None:
        """有意跳过并记录原因（如充电正文被门控）：记 ``skipped`` + 错误记录。

        这类条目**不应**被自动 retry 反复打接口 —— 权限不会因为重跑而改变，
        但错误码与原因必须留在 ``metadata.json`` 里（需求 3.1.4/3.1.5）。
        """
        self.store.add_error(entry, name, error_kind, error_code, message)
        self.store.set_step(entry, name, STEP_SKIPPED, reason=reason,
                            error_kind=error_kind, error_code=error_code,
                            message=self.redactor.redact(message) if message else "",
                            write=True)

    def fail_step(self, entry: EntryState, name: str, result: ApiResult | DownloadResult | None = None,
                  *, error_kind: str = "", error_code: Any = None, message: str = "",
                  reason: str = "") -> None:
        if result is not None:
            error_kind = error_kind or getattr(result, "error_kind", "")
            error_code = error_code if error_code is not None else getattr(result, "code", None)
            message = message or getattr(result, "message", "")
        self.store.set_step(
            entry, name, STEP_FAILED,
            reason=reason, error_kind=error_kind, error_code=error_code,
            message=self.redactor.redact(message) if message else "",
            write=True,
        )

    def entry_dir(self, entry: EntryState) -> Path:
        return Path(self.store.author_dir) / entry.dir

    def rel(self, path: Path | str, entry: EntryState) -> str:
        from ..paths import rel_path

        return rel_path(Path(path), self.entry_dir(entry))

    # ---------------- 结果装配 ----------------
    @staticmethod
    def outcome_from_result(result: ApiResult | DownloadResult) -> str:
        kind = getattr(result, "error_kind", "")
        code = getattr(result, "code", None)
        if code == 62002:
            return "invisible"
        if kind == "invisible":
            return "invisible"
        if kind == "denied":
            return "denied"
        if kind == "not_found":
            return "not_found"
        return "failed"

    def problem(self, entry: EntryState, step: str, result: ApiResult | DownloadResult,
                *, note: str = "") -> ArchiveResult:
        """统一的"取数失败"处理：记步骤失败 + 返回结果。"""
        self.fail_step(entry, step, result)
        message = self.redactor.redact(getattr(result, "message", "") or "")
        outcome = self.outcome_from_result(result)
        suffix = f"（{KIND_LABEL.get(entry.kind, entry.kind)} {entry.platform_id}）"
        return ArchiveResult(
            outcome=outcome,
            error_kind=getattr(result, "error_kind", ""),
            error_code=getattr(result, "code", None),
            message=f"{step} 失败：{message}{suffix}" + (f"；{note}" if note else ""),
            notes=[note] if note else [],
        )


def markdown_header(title: str, pairs: list[tuple[str, str]]) -> str:
    """统一的 markdown 头部：一级标题 + 元信息列表。"""
    lines = [f"# {title}", ""]
    for key, value in pairs:
        if value in (None, ""):
            continue
        lines.append(f"- {key}：{value}")
    lines.append("")
    return "\n".join(lines)


def render_blocks(blocks, path_for=None, start: int = 1, image_label: str = "图片") -> tuple[str, int]:
    """正文块（text / heading / image / unknown）→ markdown。

    ``path_for(index)`` 返回该序号图片的可用路径（本地相对路径或原始链接）；
    返回 ``(markdown, 下一个可用序号)``。图片缺失时保留占位说明，不用空白替代。
    """
    cursor = start
    chunks: list[str] = []
    for block in blocks or []:
        kind = getattr(block, "kind", "text")
        if kind == "image":
            for _pic in getattr(block, "pics", []) or []:
                path = path_for(cursor) if path_for else ""
                chunks.append(f"![{image_label}{cursor}]({path})" if path
                              else f"（{image_label}{cursor}未取得）")
                cursor += 1
        elif kind == "heading":
            text = (getattr(block, "text", "") or "").strip()
            if text:
                level = min(max(int(getattr(block, "level", 3) or 3), 2), 6)
                chunks.append(f"{'#' * level} {text}")
        elif kind == "text":
            text = (getattr(block, "text", "") or "").strip()
            if text:
                chunks.append(text)
        else:
            note = getattr(block, "note", "") or "未识别的内容块"
            chunks.append(f"（{note}）")
    return "\n\n".join(chunks).strip(), cursor
