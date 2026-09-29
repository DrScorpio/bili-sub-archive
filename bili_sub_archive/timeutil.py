"""时间工具：Unix 秒 ↔ 带时区 datetime，目录日期按北京时间。

平台返回的 ``created`` / ``pub_ts`` / ``publish_time`` 都是 Unix 秒；
需求与计划要求"目录日期按北京时间生成"，因此统一用固定 +08:00 时区
（中国自 1991 年起无夏令时，固定偏移是正确且稳定的选择）。
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")


def ts_to_dt(ts: object) -> datetime | None:
    """Unix 秒 → 北京时间 datetime；非法输入返回 None。"""
    try:
        value = int(str(ts).strip())
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=BEIJING)
    except (OverflowError, OSError, ValueError):
        return None


def dt_to_iso(dt: datetime | None) -> str:
    return dt.isoformat() if dt else ""


def now_iso() -> str:
    """当前时间（北京时间，秒级），用于 metadata / index 的更新时间。"""
    return datetime.now(tz=BEIJING).replace(microsecond=0).isoformat()


def parse_date(text: str) -> date:
    """``YYYY-MM-DD``（也接受 ``YYYY/MM/DD`` 与 ``YYYYMMDD``）→ date。"""
    raw = str(text).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"日期格式应为 YYYY-MM-DD，收到 {text!r}")


def day_start(day: date) -> datetime:
    """北京时间当日 00:00:00（含）。"""
    return datetime(day.year, day.month, day.day, tzinfo=BEIJING)


def day_end(day: date) -> datetime:
    """北京时间当日 23:59:59（含）。"""
    return day_start(day) + timedelta(hours=23, minutes=59, seconds=59)


def fmt_local(dt: datetime | None) -> str:
    if not dt:
        return "-"
    return dt.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def fmt_local_date(dt: datetime | None) -> str:
    if not dt:
        return "-"
    return dt.astimezone(BEIJING).strftime("%Y-%m-%d")


def monotonic() -> float:
    return time.monotonic()
