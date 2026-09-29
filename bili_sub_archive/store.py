"""本地状态：``metadata.json`` + 作者级 ``index.json``（原子写入）。

计划 3.2 节契约：

- ``metadata.json`` 含原链接、作者、发布时间、分 P、处理步骤状态、错误码、
  更新时间、格式版本；**不含** Cookie、API 密钥或临时下载 URL；
- ``index.json`` 是可重建的条目索引，条目 ``metadata.json`` 为步骤状态的依据；
- 每步状态取 ``pending/running/done/skipped/failed``；产物可读且校验通过才标 ``done``；
- 重跑跳过有效产物，失败步骤及其下游依赖可重试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import FORMAT_VERSION
from .errors import OutputError
from .models import (
    KIND_LABEL,
    STEPS_BY_KIND,
    STEP_DONE,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    STEP_SKIPPED,
    Item,
    StepState,
)
from .paths import (
    atomic_write_json,
    ensure_dir,
    entry_dir_name,
    read_json,
    rel_path,
)
from .timeutil import dt_to_iso, now_iso

INDEX_NAME = "index.json"
METADATA_NAME = "metadata.json"
RUNS_DIR = "_runs"
INDEX_BUILDABLE_NOTE = "本文件可由各条目 metadata.json 重建"


# --------------------------------------------------------------------------- #
# 条目状态
# --------------------------------------------------------------------------- #
@dataclass
class EntryState:
    kind: str
    platform_id: str
    dir: str
    title: str = ""
    url: str = ""
    published_at: str = ""
    author: str = ""
    source: dict = field(default_factory=dict)
    flags: dict = field(default_factory=dict)
    steps: dict[str, StepState] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    updated_at: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.platform_id)

    def step(self, name: str) -> StepState:
        if name not in self.steps:
            self.steps[name] = StepState()
        return self.steps[name]

    @property
    def rollup(self) -> str:
        """条目级汇总状态（index.json 里的 ``status``）。"""
        statuses = [s.status for s in self.steps.values()]
        if not statuses:
            return STEP_PENDING
        if any(s == STEP_FAILED for s in statuses):
            return STEP_FAILED
        if any(s in (STEP_PENDING, STEP_RUNNING) for s in statuses):
            return STEP_PENDING
        return STEP_DONE

    def to_metadata(self) -> dict:
        return {
            "format_version": FORMAT_VERSION,
            "kind": self.kind,
            "kind_label": KIND_LABEL.get(self.kind, self.kind),
            "platform_id": self.platform_id,
            "title": self.title,
            "url": self.url,
            "author": self.author,
            "published_at": self.published_at,
            "dir": self.dir,
            "source": dict(self.source),
            "flags": dict(self.flags),
            "steps": {name: state.to_json() for name, state in self.steps.items()},
            "errors": list(self.errors),
            "extra": dict(self.extra),
            "updated_at": self.updated_at or now_iso(),
            "note": "不含 Cookie、API 密钥与临时下载 URL；临时 CDN 地址仅保存在原链接形式",
        }

    def to_index_entry(self) -> dict:
        return {
            "kind": self.kind,
            "platform_id": self.platform_id,
            "dir": self.dir,
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at,
            "status": self.rollup,
            "steps": {name: state.status for name, state in self.steps.items()},
            "errors": [
                {"step": e.get("step"), "kind": e.get("kind"), "code": e.get("code"),
                 "message": e.get("message"), "at": e.get("at")}
                for e in self.errors
            ],
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_metadata(cls, data: dict, dir_name: str) -> "EntryState":
        steps = {
            name: StepState.from_json(raw)
            for name, raw in (data.get("steps") or {}).items()
        }
        return cls(
            kind=str(data.get("kind") or ""),
            platform_id=str(data.get("platform_id") or ""),
            dir=dir_name,
            title=str(data.get("title") or ""),
            url=str(data.get("url") or ""),
            published_at=str(data.get("published_at") or ""),
            author=str(data.get("author") or ""),
            source=dict(data.get("source") or {}),
            flags=dict(data.get("flags") or {}),
            steps=steps,
            errors=list(data.get("errors") or []),
            extra=dict(data.get("extra") or {}),
            updated_at=str(data.get("updated_at") or ""),
        )


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class Store:
    """一个作者目录的读写入口（单进程串行使用，锁由 :class:`bili_sub_archive.paths.DirLock` 提供）。"""

    def __init__(self, author_dir: Path, uid: int = 0, author_name: str = "", author_mid: int = 0,
                 logger=None, redactor=None):
        self.author_dir = Path(author_dir)
        self.uid = int(uid or author_mid or 0)
        self.author_name = author_name
        self.author_mid = int(author_mid or uid or 0)
        self.entries: dict[tuple[str, str], EntryState] = {}
        self.notes: list[str] = []
        self.logger = logger
        # 唯一的落盘出口都过一遍脱敏（计划第 4 节：日志和异常输出统一屏蔽敏感字段）
        self.redactor = redactor

    def _clean(self, obj):
        return self.redactor.redact_obj(obj) if self.redactor else obj

    # ---------------- 索引读写 ---------------- #
    @property
    def index_path(self) -> Path:
        return self.author_dir / INDEX_NAME

    @property
    def runs_dir(self) -> Path:
        return self.author_dir / RUNS_DIR

    def load(self) -> "Store":
        """读取 ``index.json``；缺失/损坏时回落到扫描条目目录的 ``metadata.json``。"""
        ensure_dir(self.author_dir)
        data = read_json(self.index_path, None)
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            for raw in data["entries"]:
                state = EntryState(
                    kind=str(raw.get("kind") or ""),
                    platform_id=str(raw.get("platform_id") or ""),
                    dir=str(raw.get("dir") or ""),
                    title=str(raw.get("title") or ""),
                    url=str(raw.get("url") or ""),
                    published_at=str(raw.get("published_at") or ""),
                    steps={n: StepState.from_json({"status": s}) for n, s in (raw.get("steps") or {}).items()},
                    errors=list(raw.get("errors") or []),
                    updated_at=str(raw.get("updated_at") or ""),
                )
                if not state.dir:
                    continue
                # 以 metadata.json 为准（index.json 是可重建索引）
                self.entries[state.key] = self._load_entry_dir(state.dir) or state
            if self.uid and int(data.get("uid") or 0) != self.uid:
                self.notes.append(
                    f"index.json 记录的 uid={data.get('uid')} 与本次目标 uid={self.uid} 不一致，"
                    "按目录内 metadata 重建为准"
                )
            return self
        return self.rebuild()

    def rebuild(self) -> "Store":
        """扫描作者目录下的条目目录，用 ``metadata.json`` 重建索引条目。"""
        if self.author_dir.exists():
            for child in sorted(self.author_dir.iterdir()):
                if not child.is_dir() or child.name.startswith("_"):
                    continue
                state = self._load_entry_dir(child.name)
                if state is not None and state.kind and state.platform_id:
                    self.entries[state.key] = state
        if self.entries and self.index_path.exists():
            self.notes.append("index.json 不可用，已按条目 metadata.json 重建索引")
        return self

    def _load_entry_dir(self, dir_name: str) -> EntryState | None:
        meta_path = self.author_dir / dir_name / METADATA_NAME
        data = read_json(meta_path, None)
        if not isinstance(data, dict):
            return None
        state = EntryState.from_metadata(data, dir_name)
        if not state.steps:
            state.steps = {name: StepState() for name in STEPS_BY_KIND.get(state.kind, ())}
        return state

    def save_index(self, last_run: dict | None = None) -> Path:
        payload = {
            "format_version": FORMAT_VERSION,
            "uid": self.uid,
            "author": {"mid": self.author_mid, "name": self.author_name},
            "author_dir": self.author_dir.name,
            "updated_at": now_iso(),
            "note": INDEX_BUILDABLE_NOTE,
            "entries": [state.to_index_entry() for state in self._sorted_entries()],
        }
        if last_run is not None:
            payload["last_run"] = last_run
        return atomic_write_json(self.index_path, self._clean(payload))

    def _sorted_entries(self) -> list[EntryState]:
        def key(state: EntryState):
            return (state.published_at or "", state.kind, state.platform_id)

        return sorted(self.entries.values(), key=key, reverse=True)

    # ---------------- 条目 ---------------- #
    def get(self, kind: str, platform_id: str) -> EntryState | None:
        return self.entries.get((kind, platform_id))

    def allocate(self, item: Item, title_budget: int = 60) -> EntryState:
        """为新条目分配目录（重名不覆盖）并落一份初始 ``metadata.json``。"""
        existing = self.get(item.kind, item.platform_id)
        if existing is not None:
            return existing
        base_name = entry_dir_name(item.date_stamp, item.kind, item.title, item.platform_id,
                                   budget=title_budget)
        dir_name = base_name
        candidate = self.author_dir / dir_name
        idx = 2
        while candidate.exists():
            # 目录存在但未被索引登记（上次中断/手工残留）→ 换名，不覆盖
            dir_name = f"{base_name}_{idx}"
            candidate = self.author_dir / dir_name
            idx += 1
            if idx > 100:
                raise OutputError(f"无法为 {item.key} 分配目录：同名目录过多")
        ensure_dir(candidate)
        state = EntryState(
            kind=item.kind,
            platform_id=item.platform_id,
            dir=dir_name,
            title=item.title,
            url=item.url,
            published_at=dt_to_iso(item.published_at),
            author=self.author_name,
            source=dict(item.raw_ref),
            flags=dict(item.extras),
            steps={name: StepState() for name in STEPS_BY_KIND.get(item.kind, ())},
            extra={},
            updated_at=now_iso(),
        )
        self.entries[state.key] = state
        self.write_metadata(state)
        return state

    def write_metadata(self, state: EntryState) -> Path:
        state.updated_at = now_iso()
        path = self.author_dir / state.dir / METADATA_NAME
        return atomic_write_json(path, self._clean(state.to_metadata()))

    def set_step(
        self,
        state: EntryState,
        name: str,
        status: str,
        *,
        reason: str = "",
        error_kind: str = "",
        error_code: Any = None,
        message: str = "",
        artifacts: list[str] | None = None,
        count_attempt: bool = True,
        write: bool = True,
    ) -> StepState:
        step = state.step(name)
        step.status = status
        step.reason = reason
        step.error_kind = error_kind
        step.error_code = error_code
        step.message = message
        if artifacts is not None:
            step.artifacts = [rel_path(Path(p), self.author_dir) if Path(str(p)).is_absolute() else str(p)
                              for p in artifacts]
        if count_attempt and status in (STEP_DONE, STEP_FAILED):
            step.attempts += 1
        step.updated_at = now_iso()
        if status == STEP_FAILED:
            self.add_error(state, name, error_kind, error_code, message)
        if write:
            self.write_metadata(state)
        return step

    def add_error(self, state: EntryState, step: str, error_kind: str, error_code: Any,
                  message: str) -> None:
        state.errors.append(
            {
                "step": step,
                "kind": error_kind,
                "code": error_code,
                "message": str(message)[:500],
                "at": now_iso(),
            }
        )
        # 同一错误反复出现时保留最近 20 条，避免 metadata 无限膨胀
        if len(state.errors) > 20:
            del state.errors[:-20]

    def pending_steps(self, state: EntryState, only: set[str] | None = None,
                      stale_map: dict[str, set[str]] | None = None,
                      extra: "Callable[[EntryState], set[str]] | None" = None) -> list[str]:
        """未结算的步骤；``stale_map`` 命中的 ``skipped`` 视为"应重做"。

        阶段 2 起，``skipped`` 不再一律等于"终态"：``stage2_not_implemented``
        这类历史遗留、以及因开关关闭而跳过的步骤，在阶段推进或配置变化后
        必须能被 ``retry`` 接管（见 :func:`bili_sub_archive.models.stale_skip_map`）。

        ``extra`` 是阶段 3 新增的钩子：某些"已完成但已过时"的步骤（总结/导图的输入
        指纹变了 —— 换了模型、prompt 或分块参数）在状态上看不出差异，需要由调用方
        （:class:`bili_sub_archive.runner.Runner`）按产物内容判断并在这里追加。
        """
        stale = stale_map or {}
        names = list(state.steps) or list(STEPS_BY_KIND.get(state.kind, ()))
        forced = extra(state) if extra is not None else set()
        out = []
        for name in names:
            if only and name not in only:
                continue
            step = state.step(name)
            if not step.settled:
                out.append(name)
            elif name in forced:
                out.append(name)
            elif step.status == STEP_SKIPPED and step.reason in stale.get(name, ()):
                out.append(name)
        return out

    def entries_with_pending(self, only: set[str] | None = None,
                             stale_map: dict[str, set[str]] | None = None,
                             extra: "Callable[[EntryState], set[str]] | None" = None
                             ) -> list[EntryState]:
        return [state for state in self._sorted_entries()
                if self.pending_steps(state, only, stale_map, extra)]

    # ---------------- 运行摘要 ---------------- #
    def write_run_summary(self, summary: dict) -> Path:
        ensure_dir(self.runs_dir)
        stamp = str(summary.get("finished_at") or summary.get("started_at") or now_iso())
        safe = stamp.replace(":", "").replace("-", "").replace("+", "_")
        return atomic_write_json(self.runs_dir / f"{safe}.json", self._clean(summary))

    def stats(self) -> dict:
        counts: dict[str, int] = {}
        for state in self.entries.values():
            counts[state.rollup] = counts.get(state.rollup, 0) + 1
        return {
            "entries": len(self.entries),
            "by_status": counts,
        }
