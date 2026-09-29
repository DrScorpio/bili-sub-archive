"""筛选：日期范围 + 跨类最新 N + 稳定排序。

计划 3.1 节：

- 同时指定日期与最新 N 时，**先筛日期，再在三类候选中按发布时间降序选 N 条**；
- 同秒以类型、平台 ID 固定顺序打破平局；
- 置顶项仍按真实发布时间排序（置顶只影响发现阶段的"翻页终止"判断，
  不影响最终排序）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .models import Item
from .timeutil import BEIJING, day_end, day_start


@dataclass
class Selection:
    """筛选结果与"被排除原因"统计（不静默丢条目）。"""

    items: list[Item] = field(default_factory=list)
    candidates: int = 0
    out_of_range: int = 0
    unknown_time: list[Item] = field(default_factory=list)
    dropped_by_latest: int = 0

    @property
    def unknown_time_count(self) -> int:
        return len(self.unknown_time)


def bounds(date_from: date | None, date_to: date | None) -> tuple[datetime | None, datetime | None]:
    """日期范围 → 北京时间闭区间 ``[当日 00:00:00, 当日 23:59:59]``。"""
    if date_from and date_to and date_from > date_to:
        raise ValueError(f"起始日期 {date_from} 晚于结束日期 {date_to}")
    lo = day_start(date_from) if date_from else None
    hi = day_end(date_to) if date_to else None
    return lo, hi


def in_range(published_at: datetime | None, lo: datetime | None, hi: datetime | None) -> bool:
    if published_at is None:
        return False
    dt = published_at.astimezone(BEIJING)
    if lo is not None and dt < lo:
        return False
    if hi is not None and dt > hi:
        return False
    return True


def sort_items(items: list[Item]) -> list[Item]:
    """发布时间降序 → 类型固定顺序 → 平台 ID。"""
    return sorted(items, key=lambda item: item.sort_key)


def dedupe(items: list[Item]) -> tuple[list[Item], int]:
    """按 ``(kind, platform_id)`` 去重，保留首次出现（列表顺序即时间降序）。"""
    seen: set[tuple[str, str]] = set()
    out: list[Item] = []
    dropped = 0
    for item in items:
        if item.key in seen:
            dropped += 1
            continue
        seen.add(item.key)
        out.append(item)
    return out, dropped


def select(
    items: list[Item],
    date_from: date | None = None,
    date_to: date | None = None,
    latest: int | None = None,
) -> Selection:
    """先按日期筛选，再按发布时间降序取最新 N 条。"""
    lo, hi = bounds(date_from, date_to)
    result = Selection()
    kept: list[Item] = []
    for item in items:
        if item.published_at is None:
            # 发布时间未取到（详情请求失败）→ 无法参与范围判断与 N 排序，
            # 如实回报而不是猜一个时间（运行摘要会提示重跑补做）。
            result.unknown_time.append(item)
            continue
        if lo is None and hi is None:
            kept.append(item)
        elif in_range(item.published_at, lo, hi):
            kept.append(item)
        else:
            result.out_of_range += 1

    kept = sort_items(kept)
    result.candidates = len(kept)
    if latest is not None and latest > 0 and len(kept) > latest:
        result.dropped_by_latest = len(kept) - latest
        kept = kept[:latest]
    result.items = kept
    return result


def boundary_ts(
    date_from: date | None,
    latest: int | None,
    candidates: list[Item],
) -> float | None:
    """发现阶段的"可以停止翻页"时间边界（含）。

    列表按发布时间降序（置顶项除外，调用方需自行剔除置顶项）：

    - 有日期下限 → 早于 ``from`` 当日的条目都不可能入选；
    - 无日期下限但有最新 N → 已拿到 N 条候选后，更早的条目不可能进入全局最新 N，
      但它们仍可能属于某一类"更早但必须纳入"的候选？不会：全局最新 N 一定是
      "各类前 N 条"的子集，因此各类只要拿到 N 条候选即可停止。
    """
    if date_from is not None:
        return day_start(date_from).timestamp()
    if latest is not None and latest > 0 and len(candidates) >= latest:
        ts_list = [c.published_ts for c in candidates if c.published_at is not None]
        if not ts_list:
            return None
        ts_list.sort(reverse=True)
        return ts_list[min(latest, len(ts_list)) - 1]
    return None
