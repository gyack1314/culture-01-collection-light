"""仪器校正的生效与追溯规则。

约定：
- 读数原始值先截到 0，再按 ``lux * gain + offset`` 校正。
- 校正记录只在自身 ``[start, end)`` 适用期间生效。
- 只有 ``issued_at <= 截至时点`` 的校正记录才算已知。
- ``supersedes`` 记录在自身适用期间内取代被指向记录；其他期间不受影响。
- 没有可用校正记录时采用单位增益与零偏移。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .data import Calibration, Dataset
from .timeutil import parse_dt


@dataclass(frozen=True)
class CalibrationUse:
    calibration_id: str | None
    gain: float
    offset: float
    issued_at: object | None
    applied: bool
    ambiguous: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return self.calibration_id or "unit"

    def as_dict(self) -> dict:
        return {
            "calibration_id": self.calibration_id,
            "gain": self.gain,
            "offset": self.offset,
            "issued_at": _iso(self.issued_at),
            "unit_default": not self.applied,
            "ambiguous_candidates": list(self.ambiguous),
        }


def _iso(value):
    return value.isoformat() if value is not None else None


def calibrations_for_device(dataset: Dataset, device_id: str) -> list[Calibration]:
    return [c for c in dataset.calibrations if c.device_id == device_id]


def resolve_calibration(
    calibrations: Iterable[Calibration], at, as_of
) -> CalibrationUse:
    """返回读数时点 ``at`` 在截至 ``as_of`` 时已知的生效校正。"""
    candidates = [
        c
        for c in calibrations
        if c.start <= at < c.end and (c.issued_at is None or c.issued_at <= as_of)
    ]
    if not candidates:
        return CalibrationUse(None, 1.0, 0.0, None, False)
    ids = {c.calibration_id for c in candidates}
    superseded: set[str] = set()
    for calibration in candidates:
        pointer = calibration.supersedes
        seen: set[str] = set()
        while pointer and pointer not in seen:
            seen.add(pointer)
            if pointer in ids:
                superseded.add(pointer)
            pointer = next(
                (c.supersedes for c in candidates if c.calibration_id == pointer),
                None,
            )
    remaining = [c for c in candidates if c.calibration_id not in superseded]
    if not remaining:  # pragma: no cover - 数据成环时防御
        return CalibrationUse(None, 1.0, 0.0, None, False, tuple(sorted(ids)))
    remaining.sort(key=lambda c: (c.issued_at or parse_dt("1970-01-01T00:00:00+00:00"), c.calibration_id))
    chosen = remaining[-1]
    ambiguous = tuple(sorted(c.calibration_id for c in remaining[:-1]))
    return CalibrationUse(chosen.calibration_id, chosen.gain, chosen.offset, chosen.issued_at, True, ambiguous)


def correct_reading(raw_lux: float, use: CalibrationUse) -> float:
    return max(0.0, raw_lux) * use.gain + use.offset


def known_calibration_ids(dataset: Dataset, as_of) -> list[str]:
    """截至时点已知的校正记录编号。"""
    return sorted(
        c.calibration_id
        for c in dataset.calibrations
        if c.issued_at is None or c.issued_at <= as_of
    )


def reading_correction_details(dataset: Dataset, reading, as_of) -> dict:
    uses = calibrations_for_device(dataset, reading.device_id)
    use = resolve_calibration(uses, reading.at, as_of)
    return {
        "reading_id": reading.reading_id,
        "device_id": reading.device_id,
        "cabinet_id": reading.cabinet_id,
        "at": reading.at.isoformat(),
        "raw_lux": reading.lux,
        "truncated_lux": max(0.0, reading.lux),
        "corrected_lux": correct_reading(reading.lux, use),
        "calibration": use.as_dict(),
    }
