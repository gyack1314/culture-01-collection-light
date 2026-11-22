"""光暴露账户：连接有效读数、扣除遮光、给出缺测上下界并保留证据链。

核心规则（见 docs/业务约定.md）：

* 光暴露 = 照度 × 实际展出小时数；运输与遮光不消耗额度。
* 相邻读数只有同柜、同设备有效期且间隔不超过 ``max_gap_minutes`` 才能连接。
* 连接区间照度取两端校正后读数的均值；单点不外推。
* 缺测段下界 0、上界按所在柜体 ``max_lux``。
* 校正按 ``issued_at`` 判断截至时点是否已知；未发出的更正不改变历史结论。
* 年度额度按日历年分别核算，不跨年借用。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .calibration import CalibrationUse, correct_reading, resolve_calibration
from .data import Cabinet, Dataset, Placement, Reading
from .timeutil import (
    Interval,
    clip,
    interval_hours,
    merge,
    split_by_year,
    subtract,
)


@dataclass(frozen=True)
class ExposureSegment:
    """一段有归属的时间区间及其照度证据。"""

    kind: str  # "measured" | "missing"
    cabinet_id: str
    placement_id: str | None
    start: object
    end: object
    lux_low: float
    lux_high: float
    hours: float
    lower_lux_hours: float
    upper_lux_hours: float
    reading_ids: tuple[str, ...] = ()
    device_ids: tuple[str, ...] = ()
    calibration_ids: tuple[str, ...] = ()
    note: str = ""
    shared_placement_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "cabinet_id": self.cabinet_id,
            "placement_id": self.placement_id,
            "shared_placement_ids": list(self.shared_placement_ids),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "hours": round(self.hours, 6),
            "lux_low": round(self.lux_low, 6),
            "lux_high": round(self.lux_high, 6),
            "lower_lux_hours": round(self.lower_lux_hours, 6),
            "upper_lux_hours": round(self.upper_lux_hours, 6),
            "reading_ids": list(self.reading_ids),
            "device_ids": list(self.device_ids),
            "calibration_ids": list(self.calibration_ids),
            "note": self.note,
        }


@dataclass
class YearAccount:
    year: int
    measured_lux_hours: float = 0.0
    missing_upper_lux_hours: float = 0.0
    hours_measured: float = 0.0
    hours_missing: float = 0.0
    hours_blackout: float = 0.0
    peak_lux: float = 0.0
    peak_upper: float = 0.0

    @property
    def lower_lux_hours(self) -> float:
        return self.measured_lux_hours

    @property
    def upper_lux_hours(self) -> float:
        return self.measured_lux_hours + self.missing_upper_lux_hours


@dataclass
class WorkExposure:
    work_id: str
    as_of: object
    cap_lux_hours: float
    max_lux: float
    segments: list[ExposureSegment] = field(default_factory=list)
    years: dict[int, YearAccount] = field(default_factory=dict)
    known_calibrations: list[str] = field(default_factory=list)
    pending_calibrations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def measured_lux_hours(self) -> float:
        return sum(y.measured_lux_hours for y in self.years.values())

    @property
    def missing_upper_lux_hours(self) -> float:
        return sum(y.missing_upper_lux_hours for y in self.years.values())

    @property
    def lower_lux_hours(self) -> float:
        return self.measured_lux_hours

    @property
    def upper_lux_hours(self) -> float:
        return self.lower_lux_hours + self.missing_upper_lux_hours

    @property
    def hours_measured(self) -> float:
        return sum(y.hours_measured for y in self.years.values())

    @property
    def hours_missing(self) -> float:
        return sum(y.hours_missing for y in self.years.values())

    @property
    def hours_blackout(self) -> float:
        return sum(y.hours_blackout for y in self.years.values())

    @property
    def peak_lux(self) -> float:
        return max((y.peak_lux for y in self.years.values()), default=0.0)

    @property
    def peak_upper(self) -> float:
        return max((y.peak_upper for y in self.years.values()), default=0.0)

    def year(self, year: int) -> YearAccount:
        return self.years.setdefault(year, YearAccount(year))

    def remaining(self, year: int) -> dict:
        cap = self.cap_lux_hours
        account = self.years.get(year)
        if account is None:
            return {
                "year": year,
                "cap_lux_hours": cap,
                "lower_lux_hours": 0.0,
                "upper_lux_hours": 0.0,
                "safe_remaining_lux_hours": cap,
                "optimistic_remaining_lux_hours": cap,
                "proven": True,
                "exceeds": False,
            }
        upper = account.upper_lux_hours
        return {
            "year": year,
            "cap_lux_hours": cap,
            "lower_lux_hours": account.lower_lux_hours,
            "upper_lux_hours": upper,
            "safe_remaining_lux_hours": cap - upper,
            "optimistic_remaining_lux_hours": cap - account.lower_lux_hours,
            "proven": upper <= cap,
            "exceeds": upper > cap,
        }

    def safe_remaining(self, year: int) -> float:
        """上界已无法证明安全时返回 0，绝不把下界当作剩余额度。"""
        return max(0.0, self.remaining(year)["safe_remaining_lux_hours"])

    def as_dict(self) -> dict:
        years = []
        for year in sorted(self.years):
            item = self.remaining(year)
            item["peak_lux"] = round(self.years[year].peak_lux, 6)
            item["peak_upper"] = round(self.years[year].peak_upper, 6)
            item["hours_measured"] = round(self.years[year].hours_measured, 6)
            item["hours_missing"] = round(self.years[year].hours_missing, 6)
            item["hours_blackout"] = round(self.years[year].hours_blackout, 6)
            years.append(item)
        return {
            "work_id": self.work_id,
            "as_of": self.as_of.isoformat(),
            "cap_lux_hours": self.cap_lux_hours,
            "max_lux": self.max_lux,
            "lower_lux_hours": round(self.lower_lux_hours, 6),
            "upper_lux_hours": round(self.upper_lux_hours, 6),
            "uncertainty_lux_hours": round(self.missing_upper_lux_hours, 6),
            "hours_measured": round(self.hours_measured, 6),
            "hours_missing": round(self.hours_missing, 6),
            "hours_blackout": round(self.hours_blackout, 6),
            "peak_lux": round(self.peak_lux, 6),
            "peak_upper": round(self.peak_upper, 6),
            "years": years,
            "known_calibrations": self.known_calibrations,
            "pending_calibrations": self.pending_calibrations,
            "warnings": self.warnings,
            "measured_segments": [s.as_dict() for s in self.segments if s.kind == "measured"],
            "missing_segments": [s.as_dict() for s in self.segments if s.kind == "missing"],
        }


class ExposureEngine:
    """按藏品核算截至某时点的年度光暴露账户。"""

    def __init__(self, dataset: Dataset):
        self.dataset = dataset
        self._readings = dataset.readings_by_cabinet()
        self._placements = dataset.placements_by_work()
        self._blackouts = dataset.blackouts_by_cabinet()
        self._calibrations: dict[str, list] = defaultdict(list)
        for calibration in dataset.calibrations:
            self._calibrations[calibration.device_id].append(calibration)

    # -- 读数校正 -----------------------------------------------------
    def calibration_at(self, reading: Reading, as_of) -> CalibrationUse:
        return resolve_calibration(self._calibrations.get(reading.device_id, []), reading.at, as_of)

    def corrected(self, reading: Reading, as_of) -> tuple[float, CalibrationUse]:
        use = self.calibration_at(reading, as_of)
        return correct_reading(reading.lux, use), use

    # -- 窗口与遮光 ---------------------------------------------------
    def _placement_window(self, placement: Placement, as_of) -> Interval | None:
        return clip((placement.start, placement.end), (placement.start, as_of))

    def _blackout_intervals(self, cabinet_id: str, window: Interval) -> list[Interval]:
        cuts = []
        for blackout in self._blackouts.get(cabinet_id, []):
            piece = clip((blackout.start, blackout.end), window)
            if piece:
                cuts.append(piece)
        return cuts

    def _single_point_notes(self, placement: Placement, connectable: list[Interval], as_of) -> list[str]:
        """无法连接的单点读数不外推，但显式提示，避免看起来像“没读到”。"""
        cabinet = self.dataset.cabinets[placement.cabinet_id]
        window = self._placement_window(placement, as_of)
        if window is None:
            return []
        notes = []
        covered = merge(connectable)
        for reading in self._readings.get(cabinet.cabinet_id, []):
            if not (window[0] <= reading.at < window[1]) or not _instrument_valid(self.dataset, reading):
                continue
            if any(piece[0] <= reading.at < piece[1] for piece in covered):
                continue
            notes.append(
                f"{placement.placement_id} 的单点读数 {reading.reading_id}（{reading.at.isoformat()}）"
                "无法与相邻读数连接，未外推暴露"
            )
        return notes

    # -- 连接读数 -----------------------------------------------------
    def _connect(
        self, readings: list[Reading], cabinet: Cabinet, as_of
    ) -> list[tuple[Reading, Reading, float, CalibrationUse, CalibrationUse]]:
        pairs = []
        for left, right in zip(readings, readings[1:]):
            span_minutes = (right.at - left.at).total_seconds() / 60.0
            if left.device_id != right.device_id:
                continue  # 换设备：不能跨接
            if span_minutes <= 0 or span_minutes > cabinet.max_gap_minutes:
                continue  # 间隔过大或时间顺序异常
            left_value, left_use = self.corrected(left, as_of)
            right_value, right_use = self.corrected(right, as_of)
            pairs.append((left, right, (left_value + right_value) / 2.0, left_use, right_use))
        return pairs

    def _placement_segments(
        self, placement: Placement, as_of
    ) -> tuple[list[ExposureSegment], list[Interval], list[Interval]]:
        window = self._placement_window(placement, as_of)
        if window is None:
            return [], [], []
        cabinet = self.dataset.cabinets[placement.cabinet_id]
        blackouts = self._blackout_intervals(cabinet.cabinet_id, window)
        start, end = window

        readings = [
            r
            for r in self._readings.get(cabinet.cabinet_id, [])
            if start <= r.at < end and _instrument_valid(self.dataset, r)
        ]
        readings.sort(key=lambda r: r.at)

        measured: list[tuple[Interval, float, Reading, Reading, tuple[str, ...]]] = []
        connectable: list[Interval] = []
        for left, right, value, left_use, right_use in self._connect(readings, cabinet, as_of):
            piece = clip((left.at, right.at), window)
            if piece is None:
                continue
            connectable.append(piece)
            cal_ids = tuple(sorted({u.calibration_id for u in (left_use, right_use) if u.calibration_id}))
            for sub in subtract([piece], blackouts):
                measured.append((sub, value, left, right, cal_ids))

        segments = [
            ExposureSegment(
                kind="measured",
                cabinet_id=cabinet.cabinet_id,
                placement_id=placement.placement_id,
                start=interval[0],
                end=interval[1],
                lux_low=value,
                lux_high=value,
                hours=interval_hours(interval),
                lower_lux_hours=value * interval_hours(interval),
                upper_lux_hours=value * interval_hours(interval),
                reading_ids=tuple(sorted({left.reading_id, right.reading_id})),
                device_ids=(left.device_id,),
                calibration_ids=cal_ids,
            )
            for interval, value, left, right, cal_ids in measured
        ]

        covered = merge([(s.start, s.end) for s in segments])
        for interval in subtract(subtract([window], blackouts), covered):
            segments.append(
                ExposureSegment(
                    kind="missing",
                    cabinet_id=cabinet.cabinet_id,
                    placement_id=placement.placement_id,
                    start=interval[0],
                    end=interval[1],
                    lux_low=0.0,
                    lux_high=cabinet.max_lux,
                    hours=interval_hours(interval),
                    lower_lux_hours=0.0,
                    upper_lux_hours=cabinet.max_lux * interval_hours(interval),
                    note="缺测：下界按 0，上界按柜体 max_lux；单点读数不外推",
                )
            )
        segments.sort(key=lambda s: (s.start, s.end, s.kind))
        return segments, blackouts, connectable

    # -- 账户 ---------------------------------------------------------
    def exposure(self, work_id: str, as_of) -> WorkExposure:
        work = self.dataset.works[work_id]
        result = WorkExposure(
            work_id=work_id,
            as_of=as_of,
            cap_lux_hours=work.annual_lux_hours,
            max_lux=work.max_lux,
            known_calibrations=sorted(
                c.calibration_id for c in self.dataset.calibrations if c.issued_at is None or c.issued_at <= as_of
            ),
            pending_calibrations=sorted(
                c.calibration_id for c in self.dataset.calibrations if c.issued_at is not None and c.issued_at > as_of
            ),
        )

        raw_segments: list[ExposureSegment] = []
        blackout_intervals: list[Interval] = []
        single_points: list[str] = []
        for placement in self._placements.get(work_id, []):
            segments, blackouts, connectable = self._placement_segments(placement, as_of)
            raw_segments.extend(segments)
            blackout_intervals.extend(blackouts)
            single_points.extend(self._single_point_notes(placement, connectable, as_of))

        resolved = resolve_overlaps(raw_segments)
        result.segments = resolved
        result.warnings.extend(overlap_warnings(resolved))
        result.warnings.extend(single_points)

        for segment in resolved:
            for year, piece in split_by_year((segment.start, segment.end)):
                piece_hours = interval_hours(piece)
                if piece_hours <= 0:
                    continue
                account = result.year(year)
                if segment.kind == "measured":
                    account.measured_lux_hours += segment.lux_low * piece_hours
                    account.hours_measured += piece_hours
                    account.peak_lux = max(account.peak_lux, segment.lux_high)
                    account.peak_upper = max(account.peak_upper, segment.lux_high)
                else:
                    account.missing_upper_lux_hours += segment.lux_high * piece_hours
                    account.hours_missing += piece_hours
                    account.peak_upper = max(account.peak_upper, segment.lux_high)

        # 遮光不消耗额度，单独记录小时数。
        for interval in merge(blackout_intervals):
            for year, piece in split_by_year(interval):
                result.year(year).hours_blackout += interval_hours(piece)

        if resolved and result.peak_upper > work.max_lux:
            result.warnings.append(
                f"峰值上界 {result.peak_upper:.3f} lx 高于藏品瞬时上限 {work.max_lux:.3f} lx"
            )
        for year in sorted(result.years):
            account = result.years[year]
            if account.upper_lux_hours > work.annual_lux_hours:
                result.warnings.append(
                    f"{year} 年暴露上界 {account.upper_lux_hours:.3f} lx·h 已超过年度上限 "
                    f"{work.annual_lux_hours:.3f} lx·h；剩余可用额度不能按下界认定"
                )
        if resolved and not any(s.kind == "measured" for s in resolved):
            result.warnings.append("没有任何可连接的有效读数；全部按缺测上界处理")
        return result

    def safe_remaining(self, work_id: str, as_of, year: int) -> float:
        """规划可用的保守剩余额度；上界不明时返回 0。"""
        return self.exposure(work_id, as_of).safe_remaining(year)

    def peak(self, work_id: str, as_of) -> float:
        return self.exposure(work_id, as_of).peak_upper

    def peak_proven(self, work_id: str, as_of) -> float:
        """规划用的峰值上界：有缺测时按柜体 max_lux，而不是仅取已读到的峰值。"""
        return self.exposure(work_id, as_of).peak_upper

    def reconcile(self, work_id: str, before_as_of, after_as_of) -> dict:
        """比较两个截至时点的账户，指出更正带来的变化来源。"""
        before = self.exposure(work_id, before_as_of)
        after = self.exposure(work_id, after_as_of)
        delta = {
            "work_id": work_id,
            "before_as_of": before_as_of.isoformat(),
            "after_as_of": after_as_of.isoformat(),
            "delta_lower_lux_hours": after.lower_lux_hours - before.lower_lux_hours,
            "delta_upper_lux_hours": after.upper_lux_hours - before.upper_lux_hours,
            "delta_peak_lux": after.peak_lux - before.peak_lux,
            "new_calibrations": sorted(set(after.known_calibrations) - set(before.known_calibrations)),
            "new_segments": [],
            "changed_segments": [],
            "retired_segments": [],
        }

        def key(segment: ExposureSegment) -> tuple:
            return (segment.start, segment.end, segment.cabinet_id, segment.kind, segment.placement_id)

        before_map = {key(s): s for s in before.segments}
        after_map = {key(s): s for s in after.segments}
        for k, segment in after_map.items():
            old = before_map.get(k)
            if old is None:
                delta["new_segments"].append(segment.as_dict())
            elif (
                abs(old.lux_low - segment.lux_low) > 1e-9
                or abs(old.lux_high - segment.lux_high) > 1e-9
                or old.calibration_ids != segment.calibration_ids
            ):
                delta["changed_segments"].append(
                    {
                        "start": segment.start.isoformat(),
                        "end": segment.end.isoformat(),
                        "cabinet_id": segment.cabinet_id,
                        "placement_id": segment.placement_id,
                        "before_lux": [old.lux_low, old.lux_high],
                        "after_lux": [segment.lux_low, segment.lux_high],
                        "before_calibrations": list(old.calibration_ids),
                        "after_calibrations": list(segment.calibration_ids),
                        "reading_ids": list(segment.reading_ids),
                        "delta_lower_lux_hours": segment.lower_lux_hours - old.lower_lux_hours,
                        "delta_upper_lux_hours": segment.upper_lux_hours - old.upper_lux_hours,
                    }
                )
        for k, segment in before_map.items():
            if k not in after_map:
                delta["retired_segments"].append(segment.as_dict())

        # 按（设备、校正前后）归并，便于说明变化来自哪批读数、哪支仪器。
        groups: dict[tuple, dict] = {}
        for item in delta["changed_segments"]:
            before_key = tuple(item["before_calibrations"]) or ("unit",)
            after_key = tuple(item["after_calibrations"]) or ("unit",)
            group_key = (item["cabinet_id"], before_key, after_key)
            group = groups.setdefault(
                group_key,
                {
                    "cabinet_id": item["cabinet_id"],
                    "before_calibrations": list(before_key),
                    "after_calibrations": list(after_key),
                    "segment_count": 0,
                    "first_start": item["start"],
                    "last_end": item["end"],
                    "delta_lower_lux_hours": 0.0,
                    "delta_upper_lux_hours": 0.0,
                },
            )
            group["segment_count"] += 1
            group["first_start"] = min(group["first_start"], item["start"])
            group["last_end"] = max(group["last_end"], item["end"])
            group["delta_lower_lux_hours"] += item["delta_lower_lux_hours"]
            group["delta_upper_lux_hours"] += item["delta_upper_lux_hours"]
        delta["changed_groups"] = [
            {
                **group,
                "delta_lower_lux_hours": round(group["delta_lower_lux_hours"], 6),
                "delta_upper_lux_hours": round(group["delta_upper_lux_hours"], 6),
            }
            for group in sorted(groups.values(), key=lambda g: (g["cabinet_id"], g["after_calibrations"]))
        ]
        delta["before"] = before.as_dict()
        delta["after"] = after.as_dict()
        return delta


def _instrument_valid(dataset: Dataset, reading: Reading) -> bool:
    instrument = dataset.instruments.get(reading.device_id)
    if instrument is None:
        return False
    return instrument.valid_from <= reading.at < instrument.valid_to


def resolve_overlaps(segments: list[ExposureSegment]) -> list[ExposureSegment]:
    """同一藏品同一时段只归属一个布展；重叠区间取上界更高者并显式标记。

    数据不应把同一实物同时安排在两馆；若历史资料出现重叠，按最保守的
    证据归属（较高的暴露上界）核算，避免低估已用额度。
    """
    if not segments:
        return []
    boundaries = sorted({s.start for s in segments} | {s.end for s in segments})
    resolved: list[ExposureSegment] = []
    for start, end in zip(boundaries, boundaries[1:]):
        if end <= start:
            continue
        covering = [s for s in segments if s.start < end and start < s.end]
        if not covering:
            continue
        covering.sort(key=lambda s: (-s.upper_lux_hours / s.hours if s.hours else 0, s.placement_id or "", s.cabinet_id))
        winner = covering[0]
        placements = tuple(sorted({s.placement_id for s in covering if s.placement_id}))
        shared = placements[1:] if len(placements) > 1 else ()
        piece_hours = interval_hours((start, end))
        resolved.append(
            ExposureSegment(
                kind=winner.kind,
                cabinet_id=winner.cabinet_id,
                placement_id=winner.placement_id,
                start=start,
                end=end,
                lux_low=winner.lux_low,
                lux_high=winner.lux_high,
                hours=piece_hours,
                lower_lux_hours=winner.lux_low * piece_hours,
                upper_lux_hours=winner.lux_high * piece_hours,
                reading_ids=winner.reading_ids,
                device_ids=winner.device_ids,
                calibration_ids=winner.calibration_ids,
                note=winner.note,
                shared_placement_ids=shared,
            )
        )
    return _coalesce(resolved)


def _coalesce(segments: list[ExposureSegment]) -> list[ExposureSegment]:
    result: list[ExposureSegment] = []
    for segment in segments:
        last = result[-1] if result else None
        if (
            last is not None
            and last.end == segment.start
            and last.kind == segment.kind
            and last.cabinet_id == segment.cabinet_id
            and last.placement_id == segment.placement_id
            and abs(last.lux_low - segment.lux_low) <= 1e-9
            and abs(last.lux_high - segment.lux_high) <= 1e-9
            and last.reading_ids == segment.reading_ids
            and last.note == segment.note
        ):
            result[-1] = ExposureSegment(
                kind=last.kind,
                cabinet_id=last.cabinet_id,
                placement_id=last.placement_id,
                start=last.start,
                end=segment.end,
                lux_low=last.lux_low,
                lux_high=last.lux_high,
                hours=last.hours + segment.hours,
                lower_lux_hours=last.lower_lux_hours + segment.lower_lux_hours,
                upper_lux_hours=last.upper_lux_hours + segment.upper_lux_hours,
                reading_ids=last.reading_ids,
                device_ids=last.device_ids,
                calibration_ids=last.calibration_ids,
                note=last.note,
                shared_placement_ids=tuple(sorted(set(last.shared_placement_ids) | set(segment.shared_placement_ids))),
            )
        else:
            result.append(segment)
    return result


def overlap_warnings(segments: list[ExposureSegment]) -> list[str]:
    warnings = []
    for segment in segments:
        if segment.shared_placement_ids:
            warnings.append(
                f"{segment.start.isoformat()} 起同一藏品被多个布展同时占用："
                f"{segment.placement_id} 与 {', '.join(segment.shared_placement_ids)}"
            )
    return sorted(set(warnings))
