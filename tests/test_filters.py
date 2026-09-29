"""筛选测试：日期范围（北京时间闭区间）、跨类最新 N、平局顺序、置顶排序。"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from bili_sub_archive.filters import boundary_ts, bounds, dedupe, in_range, select, sort_items
from bili_sub_archive.models import KIND_ARTICLE, KIND_DYNAMIC, KIND_VIDEO, Item
from bili_sub_archive.timeutil import BEIJING


def make_item(kind: str, pid: str, ts: datetime | None, title: str = "t") -> Item:
    return Item(kind=kind, platform_id=pid, title=title,
                url=f"https://example.invalid/{pid}", author="UP",
                published_at=ts)


class DateRangeTest(unittest.TestCase):
    def test_inclusive_day_bounds(self):
        lo, hi = bounds(date(2026, 9, 23), date(2026, 9, 23))
        self.assertEqual(lo, datetime(2026, 9, 23, 0, 0, 0, tzinfo=BEIJING))
        self.assertEqual(hi, datetime(2026, 9, 23, 23, 59, 59, tzinfo=BEIJING))
        self.assertTrue(in_range(lo, lo, hi))
        self.assertTrue(in_range(hi, lo, hi))
        self.assertFalse(in_range(datetime(2026, 9, 22, 23, 59, 59, tzinfo=BEIJING), lo, hi))
        self.assertFalse(in_range(datetime(2026, 9, 24, 0, 0, 0, tzinfo=BEIJING), lo, hi))

    def test_select_by_date_range(self):
        items = [
            make_item(KIND_DYNAMIC, "d1", datetime(2026, 9, 23, 12, 0, tzinfo=BEIJING)),
            make_item(KIND_VIDEO, "v1", datetime(2026, 9, 20, 12, 0, tzinfo=BEIJING)),
            make_item(KIND_ARTICLE, "a1", datetime(2026, 10, 1, 12, 0, tzinfo=BEIJING)),
        ]
        result = select(items, date(2026, 9, 23), date(2026, 9, 23))
        self.assertEqual([i.platform_id for i in result.items], ["d1"])
        self.assertEqual(result.out_of_range, 2)

    def test_utc_timestamp_normalized_to_beijing(self):
        # 2026-09-22 17:00 UTC == 2026-09-23 01:00 北京时间 → 落在 23 日范围内
        ts = datetime(2026, 9, 22, 17, 0, tzinfo=timezone.utc)
        item = make_item(KIND_DYNAMIC, "d1", ts)
        self.assertEqual(item.date_stamp, "2026-09-23")
        result = select([item], date(2026, 9, 23), date(2026, 9, 23))
        self.assertEqual(len(result.items), 1)


class LatestNTest(unittest.TestCase):
    def setUp(self):
        base = datetime(2026, 9, 23, 12, 0, tzinfo=BEIJING)
        self.items = [
            make_item(KIND_DYNAMIC, "d1", base),
            make_item(KIND_VIDEO, "v1", base - timedelta(hours=1)),
            make_item(KIND_ARTICLE, "a1", base - timedelta(hours=2)),
            make_item(KIND_DYNAMIC, "d2", base - timedelta(hours=3)),
            make_item(KIND_VIDEO, "v2", base - timedelta(hours=4)),
        ]

    def test_latest_across_kinds(self):
        result = select(self.items, latest=3)
        self.assertEqual([i.platform_id for i in result.items], ["d1", "v1", "a1"])
        self.assertEqual(result.dropped_by_latest, 2)

    def test_date_then_latest(self):
        # 先按日期筛（只留 23 日 10:00 之后），再取最新 2 条
        result = select(self.items, date_from=date(2026, 9, 23), latest=2)
        self.assertEqual([i.platform_id for i in result.items], ["d1", "v1"])

    def test_latest_does_not_overflow(self):
        result = select(self.items, latest=99)
        self.assertEqual(len(result.items), 5)
        self.assertEqual(result.dropped_by_latest, 0)


class SortTieBreakTest(unittest.TestCase):
    def test_same_second_uses_kind_then_id(self):
        ts = datetime(2026, 9, 23, 12, 0, tzinfo=BEIJING)
        items = [
            make_item(KIND_VIDEO, "BV2", ts),
            make_item(KIND_ARTICLE, "cv1", ts),
            make_item(KIND_DYNAMIC, "99", ts),
            make_item(KIND_VIDEO, "BV1", ts),
        ]
        ordered = [i.platform_id for i in sort_items(items)]
        self.assertEqual(ordered, ["cv1", "99", "BV1", "BV2"])

    def test_pinned_uses_real_publish_time(self):
        ts = datetime(2026, 9, 23, 12, 0, tzinfo=BEIJING)
        pinned = make_item(KIND_DYNAMIC, "old", ts - timedelta(days=40))
        pinned.extras["pinned"] = True
        newest = make_item(KIND_DYNAMIC, "new", ts)
        result = select([pinned, newest])
        self.assertEqual([i.platform_id for i in result.items], ["new", "old"])


class UnknownTimeTest(unittest.TestCase):
    def test_unknown_time_reported_not_silently_dropped(self):
        items = [make_item(KIND_ARTICLE, "a1", None), make_item(KIND_DYNAMIC, "d1",
                                                               datetime(2026, 9, 23, tzinfo=BEIJING))]
        result = select(items, latest=5)
        self.assertEqual([i.platform_id for i in result.items], ["d1"])
        self.assertEqual(result.unknown_time_count, 1)

    def test_unknown_time_excluded_even_without_filter(self):
        result = select([make_item(KIND_ARTICLE, "a1", None)])
        self.assertEqual(result.items, [])
        self.assertEqual(result.candidates, 0)


class BoundaryTest(unittest.TestCase):
    def test_boundary_from_date(self):
        value = boundary_ts(date(2026, 9, 23), None, [])
        self.assertEqual(value, datetime(2026, 9, 23, 0, 0, 0, tzinfo=BEIJING).timestamp())

    def test_boundary_from_latest_candidates(self):
        base = datetime(2026, 9, 23, 12, 0, tzinfo=BEIJING)
        candidates = [make_item(KIND_DYNAMIC, f"d{i}", base - timedelta(hours=i)) for i in range(3)]
        value = boundary_ts(None, 2, candidates)
        self.assertEqual(value, (base - timedelta(hours=1)).timestamp())

    def test_no_boundary_without_filters(self):
        self.assertIsNone(boundary_ts(None, None, []))


class DedupeTest(unittest.TestCase):
    def test_dedupe_by_kind_and_id(self):
        ts = datetime(2026, 9, 23, tzinfo=BEIJING)
        items = [make_item(KIND_DYNAMIC, "d1", ts), make_item(KIND_DYNAMIC, "d1", ts),
                 make_item(KIND_VIDEO, "d1", ts)]
        out, dropped = dedupe(items)
        self.assertEqual(len(out), 2)
        self.assertEqual(dropped, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
