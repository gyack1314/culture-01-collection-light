"""年度账户、区间证据与计量更正传播的自动检查。"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from loanlight.calibration import resolve_calibration
from loanlight.data import load_dataset
from loanlight.exposure import ExposureEngine
from loanlight.timeutil import interval_hours, parse_dt

ROOT = Path(__file__).resolve().parents[1]
BEFORE = parse_dt("2026-06-04T09:00:00+08:00")
AFTER = parse_dt("2026-06-05T10:00:00+08:00")
PLACEMENT_HOURS = 72.0


@pytest.fixture(scope="module")
def dataset():
    return load_dataset(ROOT)


@pytest.fixture(scope="module")
def engine(dataset):
    return ExposureEngine(dataset)


def test_measured_readings_need_same_device_validity_and_gap(dataset, engine):
    account = engine.exposure("W006", AFTER)
    # C1 在 05-01 17:00 之后缺测 4 小时，超过 max_gap_minutes=60，不能跨接。
    missing = [s for s in account.segments if s.kind == "missing"]
    assert any(s.cabinet_id == "C1" for s in missing)
    longest = max((s.hours for s in missing if s.cabinet_id == "C1"), default=0)
    assert longest >= 4.0


def test_blackout_is_excluded_and_does_not_consume_quota(dataset, engine):
    account = engine.exposure("W006", AFTER)
    # C1 在 05-02 08:00-10:00 遮光 2 小时，不计入额度和缺测。
    assert account.hours_blackout == pytest.approx(2.0)
    assert account.hours_measured + account.hours_missing == pytest.approx(PLACEMENT_HOURS - 2.0)
    assert account.hours_measured + account.hours_missing + account.hours_blackout == pytest.approx(PLACEMENT_HOURS)
    blackout_start = parse_dt("2026-05-02T08:00:00+08:00")
    blackout_end = parse_dt("2026-05-02T10:00:00+08:00")
    for segment in account.segments:
        assert not (segment.start < blackout_end and blackout_start < segment.end)


def test_missing_bounds_use_zero_and_cabinet_max(dataset, engine):
    account = engine.exposure("W017", AFTER)
    missing = [s for s in account.segments if s.kind == "missing"]
    assert missing, "C1 的 4 小时空档应产生缺测段"
    for segment in missing:
        assert segment.lux_low == 0.0
        assert segment.lux_high == dataset.cabinets[segment.cabinet_id].max_lux
        assert segment.upper_lux_hours == pytest.approx(segment.lux_high * segment.hours)


def test_account_never_uses_lower_bound_as_remaining(dataset, engine):
    account = engine.exposure("W017", AFTER)
    year = account.remaining(2026)
    assert year["safe_remaining_lux_hours"] == pytest.approx(
        account.cap_lux_hours - year["upper_lux_hours"]
    )
    assert year["safe_remaining_lux_hours"] < year["optimistic_remaining_lux_hours"]
    assert year["proven"] is True


def test_unproven_safety_returns_zero_remaining():
    from loanlight.exposure import WorkExposure

    account = WorkExposure(work_id="X", as_of=AFTER, cap_lux_hours=100.0, max_lux=50.0)
    account.year(2026).measured_lux_hours = 120.0
    assert account.safe_remaining(2026) == 0.0
    assert account.remaining(2026)["proven"] is False


def test_measurement_correction_only_from_its_issued_at(dataset, engine):
    before = engine.exposure("W017", BEFORE)
    after = engine.exposure("W017", AFTER)
    assert "C02" not in before.known_calibrations
    assert "C02" in after.known_calibrations
    assert after.lower_lux_hours > before.lower_lux_hours + 2000
    # 更正前使用单位/旧校正，读数不被追溯改写。
    assert before.peak_lux < 70
    assert after.peak_lux > 100


def test_correction_has_applicable_window_only():
    from loanlight.data import Calibration

    def cal(identifier, start, end, gain, issued, supersedes=None):
        return Calibration(
            identifier, "D", parse_dt(start), parse_dt(end), gain, 0.0, parse_dt(issued), supersedes
        )

    base = cal("C1", "2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00", 1.0, "2026-01-01T00:00:00+00:00")
    fix = cal(
        "C2",
        "2026-05-01T00:00:00+00:00",
        "2026-05-04T00:00:00+00:00",
        2.0,
        "2026-06-05T00:00:00+00:00",
        "C1",
    )
    as_of = parse_dt("2026-06-06T00:00:00+00:00")
    inside = resolve_calibration([base, fix], parse_dt("2026-05-02T00:00:00+00:00"), as_of)
    outside = resolve_calibration([base, fix], parse_dt("2026-05-05T00:00:00+00:00"), as_of)
    before_issue = resolve_calibration([base, fix], parse_dt("2026-05-02T00:00:00+00:00"), BEFORE)
    assert inside.calibration_id == "C2" and inside.gain == 2.0
    assert outside.calibration_id == "C1" and outside.gain == 1.0
    assert before_issue.calibration_id == "C1"


def test_reconcile_attributes_delta_to_correction(engine):
    delta = engine.reconcile("W017", BEFORE, AFTER)
    assert delta["new_calibrations"] == ["C02"]
    assert delta["delta_upper_lux_hours"] > 0
    assert delta["changed_groups"], "应汇总哪些仪器/柜体的区间被更正"
    group = delta["changed_groups"][0]
    assert group["cabinet_id"] == "C6"
    assert group["before_calibrations"] == ["C01"]
    assert group["after_calibrations"] == ["C02"]
    assert group["segment_count"] > 0
    for item in delta["changed_segments"]:
        assert item["reading_ids"]
        assert item["after_lux"][0] > item["before_lux"][0]


def test_segment_evidence_is_queryable(dataset):
    ids = {reading.reading_id for reading in dataset.readings}
    engine = ExposureEngine(dataset)
    account = engine.exposure("W017", AFTER)
    measured = [s for s in account.segments if s.kind == "measured"]
    assert len(measured) == 71
    for segment in measured:
        assert set(segment.reading_ids) <= ids
        assert segment.device_ids == ("D6",)
        assert set(segment.calibration_ids) <= {"C01", "C02"}
    assert all(not s.reading_ids for s in account.segments if s.kind == "missing")


def test_no_cross_device_or_cross_cabinet_connection(dataset):
    engine = ExposureEngine(dataset)
    for work_id in dataset.works:
        for segment in engine.exposure(work_id, AFTER).segments:
            if segment.kind != "measured":
                continue
            assert len(segment.device_ids) == 1
            for reading_id in segment.reading_ids:
                reading = next(r for r in dataset.readings if r.reading_id == reading_id)
                assert reading.cabinet_id == segment.cabinet_id
                assert reading.device_id == segment.device_ids[0]


def test_annual_cap_is_per_calendar_year(dataset):
    engine = ExposureEngine(dataset)
    account = engine.exposure("W017", AFTER)
    assert set(account.years) == {2026}
    # 每次布展只跨一个年度；不把上一年剩余额度借到本年度。
    for placement in dataset.placements:
        if placement.work_id != "W017":
            continue
        assert placement.start.year == placement.end.year == 2026


def test_readings_outside_instrument_validity_are_ignored(dataset):
    # 仪器有效期 [valid_from, valid_to)；全部读数都在有效期内，故每段都有设备。
    for reading in dataset.readings:
        instrument = dataset.instruments[reading.device_id]
        assert instrument.valid_from <= reading.at < instrument.valid_to
