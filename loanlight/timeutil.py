"""时间与区间工具。

所有时间均为带时区的 datetime，区间统一为左闭右开 ``(start, end)``。
本模块只做纯计算，不读取业务资料。
"""
from __future__ import annotations

from datetime import date, datetime, time, tzinfo
from typing import Iterable, Iterator

Interval = tuple[datetime, datetime]


class TimeError(ValueError):
    """时间字段无法按约定解释。"""


def parse_dt(value: str) -> datetime:
    """解析带时区的 ISO 8601 时间。"""
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:  # pragma: no cover - 防御性分支
        raise TimeError(f"无法解析时间：{value!r}") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise TimeError(f"时间缺少时区：{value!r}")
    return result


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:  # pragma: no cover - 防御性分支
        raise TimeError(f"无法解析日期：{value!r}") from exc


def hours(start: datetime, end: datetime) -> float:
    if end <= start:
        return 0.0
    return (end - start).total_seconds() / 3600.0


def interval_hours(interval: Interval) -> float:
    return hours(*interval)


def _valid(start: datetime, end: datetime) -> bool:
    return end > start


def clip(interval: Interval, bounds: Interval) -> Interval | None:
    """把区间裁到 bounds 内；无交集时返回 None。"""
    start = max(interval[0], bounds[0])
    end = min(interval[1], bounds[1])
    return (start, end) if _valid(start, end) else None


def merge(intervals: Iterable[Interval]) -> list[Interval]:
    """合并有序或无序的重叠/相接区间。"""
    ordered = sorted((s, e) for s, e in intervals if _valid(s, e))
    result: list[Interval] = []
    for start, end in ordered:
        if result and start <= result[-1][1]:
            if end > result[-1][1]:
                result[-1] = (result[-1][0], end)
        else:
            result.append((start, end))
    return result


def subtract(intervals: Iterable[Interval], cuts: Iterable[Interval]) -> list[Interval]:
    """从 intervals 中扣除 cuts，返回剩余区间，结果已合并。"""
    remaining = merge(intervals)
    for cut_start, cut_end in merge(cuts):
        pieces: list[Interval] = []
        for start, end in remaining:
            if cut_end <= start or cut_start >= end:
                pieces.append((start, end))
                continue
            if start < cut_start:
                pieces.append((start, cut_start))
            if cut_end < end:
                pieces.append((cut_end, end))
        remaining = pieces
    return merge(remaining)


def intersect(a: Iterable[Interval], b: Iterable[Interval]) -> list[Interval]:
    left = merge(a)
    right = merge(b)
    result: list[Interval] = []
    i = j = 0
    while i < len(left) and j < len(right):
        start = max(left[i][0], right[j][0])
        end = min(left[i][1], right[j][1])
        if _valid(start, end):
            result.append((start, end))
        if left[i][1] <= right[j][1]:
            i += 1
        else:
            j += 1
    return result


def overlaps(a: Interval, b: Interval) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def year_of(moment: datetime) -> int:
    return moment.year


def split_by_year(interval: Interval) -> Iterator[tuple[int, Interval]]:
    """按区间起点时区的日历年切分。"""
    start, end = interval
    cursor = start
    while cursor < end:
        boundary = datetime(cursor.year + 1, 1, 1, tzinfo=cursor.tzinfo)
        piece_end = min(end, boundary)
        yield cursor.year, (cursor, piece_end)
        cursor = piece_end


def show_days(start: datetime, end: datetime) -> int:
    """场次覆盖的当地日期数。

    按当地日期差计算，不把 ``end`` 所在日期再加一天；同日场次按 1 天计。
    场次实际展出小时数另按真实时间计算。
    """
    if end <= start:
        return 0
    return max(1, (end.date() - start.date()).days)


def local_date(moment: datetime) -> date:
    return moment.date()


def at_local_midnight(day: date, reference: datetime) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=reference.tzinfo)


def explain_offset(moment: datetime) -> str:
    offset = moment.utcoffset()
    if offset is None:  # pragma: no cover - parse_dt 已阻止
        return "无时区"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"UTC{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"
