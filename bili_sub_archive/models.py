"""领域模型：条目身份、步骤状态、运行统计。

计划 3.1 / 3.2 节的数据契约：

- 适配层对三类条目统一输出 ``kind`` / ``id`` / ``published_at`` / ``url`` /
  ``title`` / ``author`` / ``raw_ref``；
- 条目身份 = ``(kind, platform_id)``，目录路径首次创建后记录于 ``index.json``；
- 每步状态取 ``pending/running/done/skipped/failed``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .timeutil import dt_to_iso, fmt_local_date, now_iso

# --------------------------------------------------------------------------- #
# 内容类型
# --------------------------------------------------------------------------- #
KIND_DYNAMIC = "dynamic"
KIND_VIDEO = "video"
KIND_ARTICLE = "article"
ALL_KINDS: tuple[str, ...] = (KIND_DYNAMIC, KIND_VIDEO, KIND_ARTICLE)

#: 目录名里的类型标签（计划 3.2：``YYYY-MM-DD_<类型>_<标题>_<平台ID>``）
KIND_LABEL = {KIND_DYNAMIC: "动态", KIND_VIDEO: "视频", KIND_ARTICLE: "专栏"}

#: 打平局用的固定类型顺序（计划 3.1：同秒以类型、平台 ID 固定顺序）
KIND_ORDER = {KIND_ARTICLE: 0, KIND_DYNAMIC: 1, KIND_VIDEO: 2}

# --------------------------------------------------------------------------- #
# 步骤状态
# --------------------------------------------------------------------------- #
STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_DONE = "done"
STEP_SKIPPED = "skipped"
STEP_FAILED = "failed"

#: 各类型的步骤序列（阶段 1 只实现到 fetch/content/images；后续阶段留位）
STEPS_BY_KIND: dict[str, tuple[str, ...]] = {
    KIND_DYNAMIC: ("fetch", "content", "images", "render"),
    KIND_ARTICLE: ("fetch", "content", "images"),
    KIND_VIDEO: ("fetch", "media", "transcript", "summary", "mindmap"),
}

#: 当前实现到的阶段。阶段推进时**只改这一个常量**：历史遗留的
#: ``skipped: stageN_not_implemented`` 会自动变成"可重做"，由 `retry` 接管。
#: 阶段 4（稳定与交付）没有新增功能步骤，因此这里保持 3 —— 它表示
#: "步骤实现到第几阶段"，不是"项目走到第几阶段"。
IMPLEMENTED_STAGE = 3

#: 各步骤从哪个阶段开始真正实现（用于判断"当时未实现"的跳过是否已过时）
STEP_IMPLEMENTED_AT: dict[str, int] = {
    "render": 2,
    "media": 2,
    "transcript": 2,
    "summary": 3,
    "mindmap": 3,
}


def stale_skip_map(config=None) -> dict[str, set[str]]:
    """返回 ``{步骤名: 该步骤"现在应该重做"的 skipped 原因}``。

    三类来源：

    1. **阶段推进**：``stageN_not_implemented``，且该步骤现在已实现
       （``N <= IMPLEMENTED_STAGE``）。阶段 1 把 ``media``/``transcript``/``render``
       记成 ``skipped: stage2_not_implemented`` 并已结算，若不特殊处理，
       ``retry`` 永远不会碰它们；
    2. **配置变化**：当时因为开关关闭而跳过，现在开关打开了
       （``media_disabled`` → 已开启媒体下载；``asr_disabled`` → 已开启 ASR；
       ``summary_disabled``/``mindmap_disabled`` → 已开启总结/导图）；
    3. **阶段 3 新增**：``llm_not_configured`` —— 当时没配 LLM，现在配好了
       （``[summary] base_url`` 与 ``model`` 都非空）。

    **不包含**终态跳过（如 ``permission_preview_only``：重跑不会让权限变好；
    ``no_transcript``：ASR 跑过了确实没有文字），避免 ``retry`` 反复打接口。

    按**步骤**而不是按原因匹配，因为同一个原因串在不同步骤下语义不同：
    ``no_transcript`` 对 ``transcript`` 是终态，对 ``summary``/``mindmap`` 是
    "确实没有文字可总结" —— 它**不**进这张表（否则每条无字幕视频每次 retry 都会被
    选中并重打详情接口）。文字稿后来补上了（例如开启 ASR 后 ``transcript`` 被重做），
    同一次 ``retry`` 会重跑整条视频流程，总结与导图自然跟着做。
    """
    out: dict[str, set[str]] = {}
    for step, stage in STEP_IMPLEMENTED_AT.items():
        if stage <= IMPLEMENTED_STAGE:
            out.setdefault(step, set()).add(f"stage{stage}_not_implemented")
    if config is not None:
        for step, reason, attr in (
            ("media", "media_disabled", "download_media"),
            ("render", "render_disabled", "render_dynamic_png"),
            ("images", "download_disabled", "download_images"),
            ("transcript", "subtitle_disabled", "prefer_subtitle"),
            ("transcript", "asr_disabled", "asr_enabled"),
            ("summary", "summary_disabled", "summary_enabled"),
            ("mindmap", "mindmap_disabled", "mindmap_enabled"),
            ("mindmap", "summary_disabled", "summary_enabled"),
        ):
            if getattr(config, attr, False):
                out.setdefault(step, set()).add(reason)
        if summary_configured(config):
            for step in ("summary", "mindmap"):
                out.setdefault(step, set()).add("llm_not_configured")
    return out


def summary_configured(config) -> bool:
    """LLM 端点与模型名都配好了（密钥可选：本地端点常不需要鉴权）。"""
    return bool(str(getattr(config, "summary_base_url", "") or "").strip()
                and str(getattr(config, "summary_model", "") or "").strip())


@dataclass
class StepState:
    status: str = STEP_PENDING
    reason: str = ""
    attempts: int = 0
    error_kind: str = ""
    error_code: Any = None
    message: str = ""
    artifacts: list[str] = field(default_factory=list)
    updated_at: str = ""

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "attempts": self.attempts,
            "error_kind": self.error_kind,
            "error_code": self.error_code,
            "message": self.message,
            "artifacts": list(self.artifacts),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, data: dict | None) -> "StepState":
        data = data or {}
        return cls(
            status=str(data.get("status") or STEP_PENDING),
            reason=str(data.get("reason") or ""),
            attempts=int(data.get("attempts") or 0),
            error_kind=str(data.get("error_kind") or ""),
            error_code=data.get("error_code"),
            message=str(data.get("message") or ""),
            artifacts=list(data.get("artifacts") or []),
            updated_at=str(data.get("updated_at") or ""),
        )

    @property
    def settled(self) -> bool:
        """已结算（不需要重跑）的步骤。"""
        return self.status in (STEP_DONE, STEP_SKIPPED)


@dataclass
class Item:
    """适配层统一输出的条目。"""

    kind: str
    platform_id: str
    title: str
    url: str
    author: str
    published_at: datetime | None
    raw_ref: dict = field(default_factory=dict)
    extras: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.platform_id)

    @property
    def published_ts(self) -> float:
        if not self.published_at:
            return float("-inf")
        return self.published_at.timestamp()

    @property
    def date_stamp(self) -> str:
        if not self.published_at:
            return "0000-00-00"
        return fmt_local_date(self.published_at)

    @property
    def sort_key(self) -> tuple[float, int, str]:
        """跨类排序：发布时间降序 → 类型固定顺序 → 平台 ID。"""
        return (-self.published_ts, KIND_ORDER.get(self.kind, 9), str(self.platform_id))

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "platform_id": self.platform_id,
            "title": self.title,
            "url": self.url,
            "author": self.author,
            "published_at": dt_to_iso(self.published_at),
            "raw_ref": dict(self.raw_ref),
            "extras": dict(self.extras),
        }


@dataclass
class ScanStats:
    """一类内容的发现过程统计（计划 3.1：把额外扫描成本显式报告）。"""

    kind: str
    pages: int = 0
    requests: int = 0
    discovered: int = 0
    duplicates: int = 0
    unknown_time: int = 0
    stopped_by: str = "list_end"          # date_from | boundary | list_end | max_pages | error
    count_reported: int | None = None
    notes: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "pages": self.pages,
            "requests": self.requests,
            "discovered": self.discovered,
            "duplicates": self.duplicates,
            "unknown_time": self.unknown_time,
            "stopped_by": self.stopped_by,
            "count_reported": self.count_reported,
            "notes": list(self.notes),
            "errors": list(self.errors),
        }

    @classmethod
    def from_json(cls, data: dict) -> "ScanStats":
        data = data or {}
        return cls(
            kind=str(data.get("kind") or ""),
            pages=int(data.get("pages") or 0),
            requests=int(data.get("requests") or 0),
            discovered=int(data.get("discovered") or 0),
            duplicates=int(data.get("duplicates") or 0),
            unknown_time=int(data.get("unknown_time") or 0),
            stopped_by=str(data.get("stopped_by") or "list_end"),
            count_reported=data.get("count_reported"),
            notes=list(data.get("notes") or []),
            errors=list(data.get("errors") or []),
        )


@dataclass
class EntryResult:
    """单条目处理结果（运行摘要用）。"""

    kind: str
    platform_id: str
    dir: str
    title: str
    published_at: datetime | None
    outcome: str = "done"      # done | partial | failed | skipped | not_found | invisible | denied
    error_kind: str = ""
    error_code: Any = None
    message: str = ""
    pages: int = 1
    artifacts: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "platform_id": self.platform_id,
            "dir": self.dir,
            "title": self.title,
            "published_at": dt_to_iso(self.published_at),
            "outcome": self.outcome,
            "error_kind": self.error_kind,
            "error_code": self.error_code,
            "message": self.message,
            "pages": self.pages,
            "artifacts": list(self.artifacts),
        }


@dataclass
class RunSummary:
    """一次运行的结构化摘要（写入 ``_runs/<stamp>.json`` 并回填 index.json）。"""

    run_id: str = ""
    mode: str = "sync"                      # sync | retry
    started_at: str = field(default_factory=now_iso)
    finished_at: str = ""
    uid: int = 0
    author: str = ""
    author_dir: str = ""
    output_dir: str = ""
    kinds: list[str] = field(default_factory=list)
    date_from: str = ""
    date_to: str = ""
    latest: int | None = None
    selected: int = 0
    discovered: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    scans: list[ScanStats] = field(default_factory=list)
    results: list[EntryResult] = field(default_factory=list)
    http_requests: int = 0
    http_retries: int = 0
    bytes_downloaded: int = 0
    media_bytes: int = 0
    risk_events: int = 0
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    exit_code: int = 0

    def bump(self, outcome: str) -> None:
        self.counts[outcome] = self.counts.get(outcome, 0) + 1

    def to_json(self) -> dict:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "uid": self.uid,
            "author": self.author,
            "author_dir": self.author_dir,
            "output_dir": self.output_dir,
            "kinds": list(self.kinds),
            "date_from": self.date_from,
            "date_to": self.date_to,
            "latest": self.latest,
            "selected": self.selected,
            "discovered": self.discovered,
            "counts": dict(self.counts),
            "scans": [s.to_json() for s in self.scans],
            "results": [r.to_json() for r in self.results],
            "http_requests": self.http_requests,
            "http_retries": self.http_retries,
            "bytes_downloaded": self.bytes_downloaded,
            "media_bytes": self.media_bytes,
            "risk_events": self.risk_events,
            "warnings": list(self.warnings),
            "notes": list(self.notes),
            "exit_code": self.exit_code,
        }
