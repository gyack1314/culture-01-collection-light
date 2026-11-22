"""读取本地合成资料并做结构校验。

资料目录见 docs/资料目录.md。加载器只解释字段，不推断未声明的业务操作。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .timeutil import parse_date, parse_dt

TABLE_NAMES = (
    "works",
    "cabinets",
    "instruments",
    "readings",
    "placements",
    "calibrations",
    "blackouts",
    "shows",
    "commitments",
    "eligibility",
    "actions",
)

PRIMARY_KEYS = {
    "works": "work_id",
    "cabinets": "cabinet_id",
    "instruments": "device_id",
    "readings": "reading_id",
    "placements": "placement_id",
    "calibrations": "calibration_id",
    "blackouts": "blackout_id",
    "shows": "show_id",
    "commitments": "commitment_id",
    "eligibility": "eligibility_id",
    "actions": "action_id",
}

TIME_FIELDS = {
    "readings": ("at",),
    "placements": ("start", "end"),
    "instruments": ("valid_from", "valid_to"),
    "calibrations": ("start", "end", "issued_at"),
    "blackouts": ("start", "end"),
    "shows": ("start", "end"),
    "actions": ("at",),
}

DATE_FIELDS = {
    "shows": ("cancel_before",),
    "eligibility": ("start", "end"),
}

STATUSES = {"opened", "confirmed", "candidate"}


class DataError(ValueError):
    """资料缺失或字段无法按业务约定解释。"""


@dataclass(frozen=True)
class Work:
    work_id: str
    theme: str
    annual_lux_hours: float
    max_lux: float


@dataclass(frozen=True)
class Cabinet:
    cabinet_id: str
    venue_id: str
    max_lux: float
    max_gap_minutes: float


@dataclass(frozen=True)
class Instrument:
    device_id: str
    valid_from: Any
    valid_to: Any


@dataclass(frozen=True)
class Reading:
    reading_id: str
    device_id: str
    cabinet_id: str
    at: Any
    lux: float


@dataclass(frozen=True)
class Placement:
    placement_id: str
    work_id: str
    cabinet_id: str
    start: Any
    end: Any


@dataclass(frozen=True)
class Calibration:
    calibration_id: str
    device_id: str
    start: Any
    end: Any
    gain: float
    offset: float
    issued_at: Any
    supersedes: str | None


@dataclass(frozen=True)
class Blackout:
    blackout_id: str
    cabinet_id: str
    start: Any
    end: Any


@dataclass(frozen=True)
class Show:
    show_id: str
    venue_id: str
    start: Any
    end: Any
    status: str
    cancel_before: Any
    cancel_cost: float
    min_items: int
    theme: str
    venue_grade: str = "standard"


@dataclass(frozen=True)
class Commitment:
    commitment_id: str
    show_id: str
    work_id: str
    planned_lux: float
    daily_hours: float


@dataclass(frozen=True)
class Eligibility:
    eligibility_id: str
    work_id: str
    venue_id: str
    theme: str
    venue_grade: str
    start: Any
    end: Any


@dataclass(frozen=True)
class Action:
    action_id: str
    at: Any
    action: str
    object_id: str


@dataclass
class Dataset:
    root: Path
    works: dict[str, Work] = field(default_factory=dict)
    cabinets: dict[str, Cabinet] = field(default_factory=dict)
    instruments: dict[str, Instrument] = field(default_factory=dict)
    readings: list[Reading] = field(default_factory=list)
    placements: list[Placement] = field(default_factory=list)
    calibrations: list[Calibration] = field(default_factory=list)
    blackouts: list[Blackout] = field(default_factory=list)
    shows: dict[str, Show] = field(default_factory=dict)
    commitments: list[Commitment] = field(default_factory=list)
    eligibility: list[Eligibility] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)

    def readings_by_cabinet(self) -> dict[str, list[Reading]]:
        grouped: dict[str, list[Reading]] = {}
        for reading in self.readings:
            grouped.setdefault(reading.cabinet_id, []).append(reading)
        for rows in grouped.values():
            rows.sort(key=lambda r: r.at)
        return grouped

    def placements_by_work(self) -> dict[str, list[Placement]]:
        grouped: dict[str, list[Placement]] = {}
        for placement in self.placements:
            grouped.setdefault(placement.work_id, []).append(placement)
        return grouped

    def blackouts_by_cabinet(self) -> dict[str, list[Blackout]]:
        grouped: dict[str, list[Blackout]] = {}
        for blackout in self.blackouts:
            grouped.setdefault(blackout.cabinet_id, []).append(blackout)
        return grouped


def _coerce(raw: dict[str, Any], table: str) -> Any:
    row = dict(raw)
    for name in TIME_FIELDS.get(table, ()):
        if row.get(name) is not None:
            row[name] = parse_dt(row[name])
    for name in DATE_FIELDS.get(table, ()):
        if row.get(name) is not None:
            row[name] = parse_date(row[name])
    if table == "works":
        return Work(row["work_id"], row["theme"], float(row["annual_lux_hours"]), float(row["max_lux"]))
    if table == "cabinets":
        return Cabinet(row["cabinet_id"], row["venue_id"], float(row["max_lux"]), float(row["max_gap_minutes"]))
    if table == "instruments":
        return Instrument(row["device_id"], row["valid_from"], row["valid_to"])
    if table == "readings":
        return Reading(row["reading_id"], row["device_id"], row["cabinet_id"], row["at"], float(row["lux"]))
    if table == "placements":
        return Placement(row["placement_id"], row["work_id"], row["cabinet_id"], row["start"], row["end"])
    if table == "calibrations":
        return Calibration(
            row["calibration_id"],
            row["device_id"],
            row["start"],
            row["end"],
            float(row["gain"]),
            float(row["offset"]),
            row["issued_at"],
            row["supersedes"],
        )
    if table == "blackouts":
        return Blackout(row["blackout_id"], row["cabinet_id"], row["start"], row["end"])
    if table == "shows":
        status = row["status"]
        if status not in STATUSES:
            raise DataError(f"未知场次状态：{status!r}")
        venue_grade = row.get("venue_grade") or "standard"
        return Show(
            row["show_id"],
            row["venue_id"],
            row["start"],
            row["end"],
            status,
            row["cancel_before"],
            float(row["cancel_cost"]),
            int(row["min_items"]),
            row["theme"],
            venue_grade,
        )
    if table == "commitments":
        return Commitment(
            row["commitment_id"], row["show_id"], row["work_id"], float(row["planned_lux"]), float(row["daily_hours"])
        )
    if table == "eligibility":
        return Eligibility(
            row["eligibility_id"],
            row["work_id"],
            row["venue_id"],
            row["theme"],
            row["venue_grade"],
            row["start"],
            row["end"],
        )
    if table == "actions":
        return Action(row["action_id"], row["at"], row["action"], row["object_id"])
    raise DataError(f"未知资料表：{table}")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise DataError(f"缺少资料文件：{path}")
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise DataError(f"{path.name} 第 {number} 行不是合法 JSON：{exc}") from exc
    return rows


def _check_references(dataset: Dataset) -> None:
    for reading in dataset.readings:
        if reading.cabinet_id not in dataset.cabinets:
            raise DataError(f"读数 {reading.reading_id} 引用了未知柜体 {reading.cabinet_id}")
        if reading.device_id not in dataset.instruments:
            raise DataError(f"读数 {reading.reading_id} 引用了未知仪器 {reading.device_id}")
    for placement in dataset.placements:
        if placement.work_id not in dataset.works:
            raise DataError(f"布展 {placement.placement_id} 引用了未知藏品 {placement.work_id}")
        if placement.cabinet_id not in dataset.cabinets:
            raise DataError(f"布展 {placement.placement_id} 引用了未知柜体 {placement.cabinet_id}")
    for calibration in dataset.calibrations:
        if calibration.device_id not in dataset.instruments:
            raise DataError(f"校正 {calibration.calibration_id} 引用了未知仪器 {calibration.device_id}")
        if calibration.supersedes and calibration.supersedes not in {
            c.calibration_id for c in dataset.calibrations
        }:
            raise DataError(f"校正 {calibration.calibration_id} 取代了未知记录 {calibration.supersedes}")
    for commitment in dataset.commitments:
        if commitment.show_id not in dataset.shows:
            raise DataError(f"承诺 {commitment.commitment_id} 引用了未知场次 {commitment.show_id}")
        if commitment.work_id not in dataset.works:
            raise DataError(f"承诺 {commitment.commitment_id} 引用了未知藏品 {commitment.work_id}")
    for blackout in dataset.blackouts:
        if blackout.cabinet_id not in dataset.cabinets:
            raise DataError(f"遮光 {blackout.blackout_id} 引用了未知柜体 {blackout.cabinet_id}")


def load_dataset(root: str | Path) -> Dataset:
    """从项目根目录读取 data/ 下的全部资料。"""
    root = Path(root)
    dataset = Dataset(root=root)
    for name in TABLE_NAMES:
        rows = _read_jsonl(root / "data" / f"{name}.jsonl")
        key = PRIMARY_KEYS[name]
        seen: set[str] = set()
        converted = []
        for row in rows:
            if key not in row:
                raise DataError(f"{name} 缺少主键 {key}")
            identifier = row[key]
            if identifier in seen:
                raise DataError(f"{name} 主键重复：{identifier}")
            seen.add(identifier)
            converted.append(_coerce(row, name))
        if name in {"works", "cabinets", "instruments", "shows"}:
            index = {getattr(item, key): item for item in converted}
            setattr(dataset, name, index)
        else:
            setattr(dataset, name, converted)
    _check_references(dataset)
    return dataset
