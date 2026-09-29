"""发现：三类列表分页 → 统一条目 → 去重与扫描统计。

计划 3.1 节：

- 每类列表分别翻页，直到**能够证明后续页不会进入结果集**，或到达页尾；
- 若某列表无法保证发布时间排序，不因已凑满 N 条就提前停止；
- 仅保存已实际发现的条目；"未发现"不写成"权限不足"。

各列表的排序语义（阶段 0 第 3.3.1 / 3.4.1 / 3.5.2 节）：

| 列表 | 分页 | 排序 | 发布时间 |
| --- | --- | --- | --- |
| ``arc/search`` | ``pn`` / ``ps`` + ``page.count`` | ``created`` 严格降序（置顶项可能插在首位） | 有 |
| ``feed/space`` | ``offset`` 游标 + ``has_more`` | 降序（置顶项可能插在首位） | 有 |
| ``opus/feed/space`` | ``page`` + ``offset`` + ``has_more`` | 降序 | **无**（须逐条取详情） |
| ``/x/space/wbi/article`` | ``pn`` / ``ps`` | ``publish_time`` 降序 | 有 |

"停止翻页"的时间边界由 :func:`bili_sub_archive.filters.boundary_ts` 给出；置顶项
（发布时间早于后一条）**不参与**边界判断，避免把置顶当成"已经翻到底"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .bili.api import (
    SPACE_VIDEO_PS,
    dynamic_item,
    legacy_article_item,
    mark_pinned_videos,
    opus_item,
    path_of,
    video_item,
)
from .bili.parse import parse_opus_detail
from .errors import (
    FATAL_KINDS,
    describe_kind,
)
from .filters import boundary_ts, dedupe
from .models import (
    KIND_ARTICLE,
    KIND_DYNAMIC,
    KIND_VIDEO,
    Item,
    ScanStats,
)


@dataclass
class DiscoveryOutcome:
    items: list[Item] = field(default_factory=list)
    stats: list[ScanStats] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fatal_kind: str = ""
    fatal_message: str = ""

    @property
    def discovered(self) -> int:
        return len(self.items)


def _min_ts(items: list[Item]) -> float | None:
    """非置顶条目里最早的发布时间（边界判断用）。"""
    values = [i.published_ts for i in items if i.published_at is not None and not i.extras.get("pinned")]
    return min(values) if values else None


class Discoverer:
    """按类型分页发现；不依赖 count 做终止判断（仅交叉校验）。"""

    def __init__(self, api, config, logger, redactor=None, detail_cache: dict | None = None):
        self.api = api
        self.config = config
        self.logger = logger
        self.redactor = redactor
        self.detail_cache = detail_cache if detail_cache is not None else {}

    # ---------------- 入口 ---------------- #
    def run(self, mid: int, author_name: str) -> DiscoveryOutcome:
        outcome = DiscoveryOutcome()
        handlers: dict[str, Callable[[int, str], tuple[list[Item], ScanStats]]] = {
            KIND_VIDEO: self._videos,
            KIND_DYNAMIC: self._dynamics,
            KIND_ARTICLE: self._articles,
        }
        for kind in self.config.enabled_kinds:
            self.logger.info(f"[发现] 开始扫描 {kind} 列表…")
            items, stats = handlers[kind](mid, author_name)
            outcome.stats.append(stats)
            outcome.items.extend(items)
            for err in stats.errors:
                if err.get("kind") in FATAL_KINDS and not outcome.fatal_kind:
                    outcome.fatal_kind = str(err.get("kind"))
                    outcome.fatal_message = str(err.get("message") or "")
            outcome.notes.extend(stats.notes)
            self.logger.info(
                f"[发现] {kind}：{stats.discovered} 条，翻 {stats.pages} 页 / {stats.requests} 次请求，"
                f"停止原因 {stats.stopped_by}"
            )
            if outcome.fatal_kind:
                outcome.notes.append(
                    f"发现阶段遇到致命错误（{describe_kind(outcome.fatal_kind)}），已停止后续列表扫描"
                )
                break
        deduped, dropped = dedupe(outcome.items)
        if dropped:
            outcome.notes.append(f"跨列表/跨页重复条目已去重 {dropped} 条")
        outcome.items = deduped
        return outcome

    # ---------------- 通用 ---------------- #
    def _record_error(self, stats: ScanStats, result, context: str) -> None:
        stats.errors.append({
            "context": context,
            "path": getattr(result, "path", ""),
            "http_status": getattr(result, "http_status", 0),
            "code": getattr(result, "code", None),
            "kind": getattr(result, "error_kind", ""),
            "message": (self.redactor.redact(getattr(result, "message", "")) if self.redactor
                        else getattr(result, "message", "")),
        })
        self.logger.warning(f"[发现] {context} 失败：{getattr(result, 'brief', lambda: '')()}")

    # ---------------- 视频 ---------------- #
    def _videos(self, mid: int, author_name: str) -> tuple[list[Item], ScanStats]:
        stats = ScanStats(kind=KIND_VIDEO)
        collected: list[Item] = []
        seen: set[str] = set()

        def scan(special_type: str, label: str) -> None:
            nonlocal collected
            pn = 1
            local: list[Item] = []
            while pn <= self.config.max_pages:
                result = self.api.video_list(mid, pn=pn, ps=SPACE_VIDEO_PS,
                                             order="pubdate", special_type=special_type)
                stats.requests += 1
                stats.pages += 1
                if not result.ok:
                    self._record_error(stats, result, f"{label} 第 {pn} 页")
                    stats.stopped_by = "error"
                    break
                data = result.data if isinstance(result.data, dict) else {}
                page_info = data.get("page") if isinstance(data.get("page"), dict) else {}
                if page_info.get("count") is not None:
                    stats.count_reported = int(page_info.get("count") or 0)
                vlist = ((data.get("list") or {}).get("vlist")
                         if isinstance(data.get("list"), dict) else None) or []
                page_items = [video_item(raw, author_name) for raw in vlist]
                page_items = [i for i in page_items if i is not None]
                if pn == 1 and not special_type:
                    mark_pinned_videos(page_items)
                local.extend(page_items)
                new = [i for i in page_items if i.platform_id not in seen]
                for item in page_items:
                    seen.add(item.platform_id)
                collected.extend(new)
                self.logger.debug(f"[发现] 视频 {label} 第 {pn} 页：{len(page_items)} 条（新增 {len(new)}）")

                boundary = boundary_ts(self.config.date_from, self.config.latest, collected)
                if not vlist:
                    stats.stopped_by = "list_end"
                    break
                if len(vlist) < SPACE_VIDEO_PS:
                    stats.stopped_by = "list_end"
                    break
                min_ts = _min_ts(page_items)
                if boundary is not None and min_ts is not None and min_ts < boundary:
                    stats.stopped_by = "date_from" if self.config.date_from else "boundary"
                    if self.config.date_from:
                        stats.notes.append(
                            f"{label}：已翻到早于 {self.config.date_from} 的条目，"
                            "后续页只可能更早，停止翻页"
                        )
                    else:
                        stats.notes.append(
                            f"{label}：已凑满最新 {self.config.latest} 条候选，"
                            "后续页不可能进入全局最新 N，停止翻页"
                        )
                    break
                if stats.count_reported and pn * SPACE_VIDEO_PS >= stats.count_reported:
                    stats.stopped_by = "list_end"
                    break
                pn += 1
            else:
                stats.stopped_by = "max_pages"
                stats.notes.append(
                    f"{label}：达到 max_pages={self.config.max_pages} 仍未见底，"
                    "结果可能不完整（可提高 max_pages 后重跑）"
                )
            if special_type:
                extra = [i for i in local if i.platform_id not in {c.platform_id for c in collected}]
                if extra:
                    stats.notes.append(f"充电子集列表含公开列表未出现的条目 {len(extra)} 条，已补入候选")

        scan("", "公开投稿")
        if self.config.scan_charging_video_subset:
            before = len(collected)
            scan("charging", "充电专属子集")
            if len(collected) > before:
                stats.notes.append(
                    f"充电专属子集额外贡献 {len(collected) - before} 条公开列表未覆盖的条目"
                )

        stats.discovered = len(collected)
        if stats.count_reported is not None and stats.stopped_by == "date_from":
            stats.notes.append("按日期边界提前停止，未翻完全部分页（这是预期行为，非漏抓）")
        return collected, stats

    # ---------------- 动态 ---------------- #
    def _dynamics(self, mid: int, author_name: str) -> tuple[list[Item], ScanStats]:
        stats = ScanStats(kind=KIND_DYNAMIC)
        collected: list[Item] = []
        seen: set[str] = set()
        offset = ""
        page = 0
        while page < self.config.max_pages:
            result = self.api.dynamic_feed(mid, offset)
            stats.requests += 1
            stats.pages += 1
            page += 1
            if not result.ok:
                self._record_error(stats, result, f"动态列表 offset={offset or '首页'}")
                stats.stopped_by = "error"
                break
            data = result.data if isinstance(result.data, dict) else {}
            raw_items = data.get("items") or []
            page_items: list[Item] = []
            for raw in raw_items:
                item, _doc = dynamic_item(raw, author_name, mid)
                if item is None:
                    continue
                if item.platform_id in seen:
                    stats.duplicates += 1
                    continue
                seen.add(item.platform_id)
                collected.append(item)
                page_items.append(item)
            self.logger.debug(f"[发现] 动态第 {page} 页：{len(page_items)} 条")
            has_more = bool(data.get("has_more"))
            next_offset = str(data.get("offset") or "")
            boundary = boundary_ts(self.config.date_from, self.config.latest, collected)
            min_ts = _min_ts(page_items)
            if boundary is not None and min_ts is not None and min_ts < boundary:
                stats.stopped_by = "date_from" if self.config.date_from else "boundary"
                break
            if not has_more or not next_offset:
                stats.stopped_by = "list_end"
                break
            if next_offset == offset:
                stats.stopped_by = "cursor_stalled"
                stats.notes.append("动态列表 offset 游标未推进，提前结束（平台分页异常，已记录）")
                break
            offset = next_offset
        else:
            stats.stopped_by = "max_pages"
            stats.notes.append(f"动态列表达到 max_pages={self.config.max_pages}，结果可能不完整")

        # 次级来源：opus 图文列表（覆盖充电专属图文；缺发布时间的条目逐条取详情补时间）
        if self.config.scan_dynamic_secondary_source and stats.stopped_by != "error":
            extra, secondary_notes = self._dynamic_secondary(mid, author_name, seen, collected, stats)
            collected.extend(extra)
            stats.notes.extend(secondary_notes)
        stats.discovered = len(collected)
        return collected, stats

    def _dynamic_secondary(self, mid: int, author_name: str, seen: set[str],
                           collected: list[Item], stats: ScanStats) -> tuple[list[Item], list[str]]:
        notes: list[str] = []
        extra: list[Item] = []
        offset = ""
        page = 1
        fetched = 0
        while page <= self.config.max_pages:
            result = self.api.opus_feed(mid, page=page, offset=offset, type="all")
            stats.requests += 1
            stats.pages += 1
            if not result.ok:
                self._record_error(stats, result, f"opus 图文列表 第 {page} 页")
                break
            data = result.data if isinstance(result.data, dict) else {}
            raw_items = data.get("items") or []
            if not raw_items:
                break
            for raw in raw_items:
                opus_id = str((raw or {}).get("opus_id") or "")
                if not opus_id or opus_id in seen:
                    continue
                seen.add(opus_id)
                # 该列表不提供发布时间 → 逐条取详情补时间（阶段 0 第 3.4.1 节）
                detail = self.api.dynamic_detail(opus_id)
                stats.requests += 1
                fetched += 1
                if not detail.ok:
                    self._record_error(stats, detail, f"opus 图文详情 id={opus_id}")
                    continue
                item, doc = dynamic_item((detail.data or {}).get("item") or {}, author_name, mid)
                if item is None:
                    continue
                if doc is not None:
                    self.detail_cache[("dynamic", opus_id)] = {
                        "doc": doc, "opus": None, "opus_tried": False,
                        "sources": [path_of("dynamic_detail")], "missing": [],
                    }
                extra.append(item)
                boundary = boundary_ts(self.config.date_from, self.config.latest, collected + extra)
                if boundary is not None and item.published_at is not None and item.published_ts < boundary:
                    notes.append(
                        f"opus 图文次级来源：补入 {len(extra)} 条（含缺时间条目，逐条取详情 {fetched} 次）"
                    )
                    return extra, notes
            has_more = bool(data.get("has_more"))
            next_offset = str(data.get("offset") or "")
            if not has_more or not next_offset or next_offset == offset:
                break
            offset = next_offset
            page += 1
        if extra or fetched:
            notes.append(
                f"opus 图文次级来源：补入 {len(extra)} 条公开动态流未覆盖的条目，"
                f"逐条取详情 {fetched} 次（该列表不提供发布时间，成本已计入统计）"
            )
        return extra, notes

    # ---------------- 专栏 ---------------- #
    def _articles(self, mid: int, author_name: str) -> tuple[list[Item], ScanStats]:
        stats = ScanStats(kind=KIND_ARTICLE)
        collected: list[Item] = []
        seen: set[str] = set()

        # 1) 新格式：opus feed type=article（无发布时间 → 逐条详情）
        offset = ""
        page = 1
        unknown_time = 0
        while page <= self.config.max_pages:
            result = self.api.opus_feed(mid, page=page, offset=offset, type="article")
            stats.requests += 1
            stats.pages += 1
            if not result.ok:
                self._record_error(stats, result, f"专栏列表 第 {page} 页")
                stats.stopped_by = "error"
                break
            data = result.data if isinstance(result.data, dict) else {}
            raw_items = data.get("items") or []
            if not raw_items:
                stats.stopped_by = "list_end"
                break
            for raw in raw_items:
                item = opus_item(raw, author_name, mid)
                if item is None or item.platform_id in seen:
                    stats.duplicates += 1 if item is not None else 0
                    continue
                seen.add(item.platform_id)
                detail = self.api.opus_detail(item.platform_id)
                stats.requests += 1
                if not detail.ok:
                    self._record_error(stats, detail, f"专栏详情 id={item.platform_id}")
                    unknown_time += 1
                else:
                    doc = parse_opus_detail(detail.data)
                    if doc is not None:
                        item.published_at = doc.published_at
                        item.title = doc.title or item.title
                        item.extras["is_only_fans"] = doc.is_only_fans
                        item.extras["article_type"] = doc.article_type
                        item.extras["unknown_publish_time"] = False
                        self.detail_cache[("opus", item.platform_id)] = doc
                if item.published_at is None:
                    unknown_time += 1
                collected.append(item)
                boundary = boundary_ts(self.config.date_from, self.config.latest, collected)
                if boundary is not None and item.published_at is not None and item.published_ts < boundary:
                    stats.stopped_by = "date_from" if self.config.date_from else "boundary"
                    break
            else:
                pass
            if stats.stopped_by in ("date_from", "boundary", "error"):
                break
            has_more = bool(data.get("has_more"))
            next_offset = str(data.get("offset") or "")
            if not has_more or not next_offset or next_offset == offset:
                stats.stopped_by = "list_end"
                break
            offset = next_offset
            page += 1
        else:
            stats.stopped_by = "max_pages"
        if stats.stopped_by == "max_pages":
            stats.notes.append(f"专栏列表达到 max_pages={self.config.max_pages}，结果可能不完整")
        stats.notes.append(
            "新格式专栏列表不提供发布时间，已逐条取详情补时间；这是接口限制导致的额外请求"
        )

        # 2) 旧格式：/x/space/wbi/article 的 articles[]（含 count 交叉校验）
        if stats.stopped_by != "error":
            pn = 1
            legacy_total = 0
            while pn <= self.config.max_pages:
                result = self.api.article_list(mid, pn=pn)
                stats.requests += 1
                stats.pages += 1
                if not result.ok:
                    self._record_error(stats, result, f"旧格式专栏列表 第 {pn} 页")
                    break
                data = result.data if isinstance(result.data, dict) else {}
                if data.get("count") is not None:
                    stats.count_reported = int(data.get("count") or 0)
                raw_items = data.get("articles") or []
                for raw in raw_items:
                    item = legacy_article_item(raw, author_name, mid)
                    if item is None or item.platform_id in seen:
                        stats.duplicates += 1 if item is not None else 0
                        continue
                    seen.add(item.platform_id)
                    collected.append(item)
                    legacy_total += 1
                if not raw_items:
                    break
                pn += 1
            if legacy_total:
                stats.notes.append(f"旧格式（read/cv）专栏补入 {legacy_total} 条")
            if stats.count_reported is not None:
                stats.notes.append(
                    f"计数接口 count={stats.count_reported} 仅作交叉校验，"
                    "分页终止一律以列表 has_more/条目数为准（阶段 0 第 3.5.6 节）"
                )

        stats.unknown_time = len([i for i in collected if i.published_at is None])
        stats.discovered = len(collected)
        return collected, stats


def items_from_entries(entries) -> list[Item]:
    """把 index.json 里的条目状态还原成 Item（retry 用，不依赖列表扫描）。"""
    out: list[Item] = []
    for entry in entries:
        published = None
        if entry.published_at:
            try:
                from datetime import datetime

                published = datetime.fromisoformat(entry.published_at)
            except ValueError:
                published = None
        out.append(
            Item(
                kind=entry.kind,
                platform_id=entry.platform_id,
                title=entry.title,
                url=entry.url,
                author=entry.author,
                published_at=published,
                raw_ref=dict(entry.source),
                extras=dict(entry.flags),
            )
        )
    return out
