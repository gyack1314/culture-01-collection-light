"""用小型合成资料覆盖规则边界，不依赖业务数据的具体数值。"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from loanlight.calibration import correct_reading, resolve_calibration
from loanlight.data import (
    Blackout,
    Cabinet,
    Calibration,
    Dataset,
    Instrument,
    Placement,
    Reading,
    Work,
)
from loanlight.exposure import ExposureEngine
from loanlight.plan import Planner
from loanlight.timeutil import merge, parse_dt, show_days, split_by_year, subtract

TZ = timezone(timedelta(hours=8))


def dt(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def make_dataset(
    readings,
    placements,
    *,
    cabinet_max_lux=100.0,
    max_gap=60,
    work_cap=10000.0,
    work_max_lux=100.0,
    blackouts=(),
    calibrations=(),
    valid_from=None,
    valid_to=None,
):
    dataset = Dataset(root=None)  # type: ignore[arg-type]
    dataset.works = {"W": Work("W", "工艺", work_cap, work_max_lux)}
    dataset.cabinets = {"C": Cabinet("C", "A", cabinet_max_lux, max_gap)}
    dataset.instruments = {
        "D": Instrument("D", valid_from or dt("2026-01-01T00:00:00"), valid_to or dt("2027-01-01T00:00:00"))
    }
    dataset.readings = list(readings)
    dataset.placements = list(placements)
    dataset.blackouts = list(blackouts)
    dataset.calibrations = list(calibrations)
    return dataset


def reading(identifier, hour, lux, device="D", cabinet="C"):
    return Reading(identifier, device, cabinet, dt("2026-05-01T00:00:00") + timedelta(hours=hour), lux)


def placement(start_hour=0, end_hour=4):
    return Placement(
        "P",
        "W",
        "C",
        dt("2026-05-01T00:00:00") + timedelta(hours=start_hour),
        dt("2026-05-01T00:00:00") + timedelta(hours=end_hour),
    )


def account_for(dataset, as_of=None):
    return ExposureEngine(dataset).exposure("W", as_of or dt("2026-05-01T04:00:00"))


def test_adjacent_readings_connect_at_exact_max_gap():
    dataset = make_dataset([reading("r1", 0, 40), reading("r2", 1, 60)], [placement()])
    account = account_for(dataset)
    measured = [s for s in account.segments if s.kind == "measured"]
    assert len(measured) == 1
    # 只有两端读数之间的区间可连接；0-1 h，其余是缺测。
    assert measured[0].hours == 1.0
    assert measured[0].lux_low == pytest.approx(50.0)
    assert account.measured_lux_hours == pytest.approx(50.0)
    assert account.hours_missing == pytest.approx(3.0)


def test_gap_beyond_limit_is_missing_not_interpolated():
    dataset = make_dataset(
        [reading("r1", 0, 40), reading("r2", 2, 60)],
        [placement()],
        max_gap=60,
    )
    account = account_for(dataset)
    assert all(s.kind == "missing" for s in account.segments)
    assert account.measured_lux_hours == 0.0
    assert account.missing_upper_lux_hours == pytest.approx(400.0)
    assert account.safe_remaining(2026) == pytest.approx(10000.0 - 400.0)


def test_device_change_breaks_connection():
    dataset = make_dataset([reading("r1", 0, 40), reading("r2", 1, 60, device="E")], [placement()])
    dataset.instruments["E"] = Instrument("E", dt("2026-01-01T00:00:00"), dt("2027-01-01T00:00:00"))
    account = account_for(dataset)
    assert all(s.kind == "missing" for s in account.segments)
    assert account.measured_lux_hours == 0.0


def test_readings_outside_instrument_validity_are_dropped():
    dataset = make_dataset(
        [reading("r1", 0, 40), reading("r2", 1, 60), reading("r3", 2, 40), reading("r4", 3, 60)],
        [placement()],
        valid_from=dt("2026-05-01T01:00:00"),
        valid_to=dt("2026-05-01T03:00:00"),
    )
    account = account_for(dataset)
    measured = [s for s in account.segments if s.kind == "measured"]
    assert len(measured) == 1
    assert measured[0].start == dt("2026-05-01T01:00:00")
    # r4 在 valid_to 端点（左闭右开）已失效，因此连接止于 r3。
    assert measured[0].end == dt("2026-05-01T02:00:00")
    assert account.hours_missing == pytest.approx(3.0)


def test_single_reading_is_not_extrapolated():
    dataset = make_dataset([reading("r1", 1, 60)], [placement()])
    account = account_for(dataset)
    assert account.measured_lux_hours == 0.0
    assert account.hours_missing == pytest.approx(4.0)
    assert any("未外推" in warning for warning in account.warnings)


def test_blackout_priority_over_measurement():
    dataset = make_dataset(
        [reading("r1", 0, 40), reading("r2", 3.5, 40)],
        [placement()],
        max_gap=300,
        blackouts=[Blackout("B", "C", dt("2026-05-01T01:00:00"), dt("2026-05-01T03:00:00"))],
    )
    account = account_for(dataset)
    assert account.hours_blackout == pytest.approx(2.0)
    assert account.measured_lux_hours == pytest.approx(40.0 * 1.5)
    assert account.hours_measured + account.hours_blackout + account.hours_missing == pytest.approx(4.0)


def test_calibration_window_and_issued_at():
    cal_old = Calibration(
        "old", "D", dt("2026-01-01T00:00:00"), dt("2027-01-01T00:00:00"), 1.0, 0.0,
        dt("2026-01-01T00:00:00"), None,
    )
    cal_new = Calibration(
        "new", "D", dt("2026-05-01T00:00:00"), dt("2026-05-01T02:00:00"), 2.0, 5.0,
        dt("2026-05-02T00:00:00"), "old",
    )
    inside = resolve_calibration([cal_old, cal_new], dt("2026-05-01T01:00:00"), dt("2026-05-02T00:00:00"))
    after_window = resolve_calibration([cal_old, cal_new], dt("2026-05-01T03:00:00"), dt("2026-05-02T00:00:00"))
    before_issue = resolve_calibration([cal_old, cal_new], dt("2026-05-01T01:00:00"), dt("2026-05-01T23:00:00"))
    assert (inside.calibration_id, inside.gain, inside.offset) == ("new", 2.0, 5.0)
    assert (after_window.calibration_id, after_window.gain) == ("old", 1.0)
    assert before_issue.calibration_id == "old"


def test_correct_reading_truncates_negative_before_gain():
    from loanlight.calibration import CalibrationUse

    use = CalibrationUse("x", 2.0, 3.0, dt("2026-01-01T00:00:00"), True)
    assert correct_reading(-5.0, use) == pytest.approx(3.0)
    assert correct_reading(10.0, use) == pytest.approx(23.0)


def test_no_cross_year_borrowing():
    dataset = make_dataset(
        [reading("r1", 0, 100), reading("r2", 1, 100)],
        [placement(0, 2)],
        max_gap=600,
        work_cap=150.0,
    )
    # 把布展挪到跨年：2026-12-31 23:00 ~ 2027-01-01 01:00
    dataset.placements = [
        Placement(
            "P",
            "W",
            "C",
            dt("2026-12-31T22:00:00"),
            dt("2027-01-01T03:00:00"),
        )
    ]
    dataset.readings = [
        Reading("r1", "D", "C", dt("2026-12-31T22:00:00"), 100.0),
        Reading("r2", "D", "C", dt("2027-01-01T02:00:00"), 100.0),
    ]
    dataset.instruments["D"] = Instrument("D", dt("2026-01-01T00:00:00"), dt("2028-01-01T00:00:00"))
    dataset.cabinets["C"] = Cabinet("C", "A", 100.0, 600)
    account = ExposureEngine(dataset).exposure("W", dt("2027-01-02T00:00:00"))
    # 有效连接跨年，但额度按日历年分别累计。
    assert set(account.years) == {2026, 2027}
    assert account.years[2026].measured_lux_hours == pytest.approx(200.0)
    assert account.years[2027].measured_lux_hours == pytest.approx(200.0)
    # 2027 年不能用 2026 年剩余额度；每年各自 150 上限都已超。
    assert account.remaining(2026)["exceeds"] is True
    assert account.remaining(2027)["exceeds"] is True
    assert account.safe_remaining(2026) == 0.0
    assert account.safe_remaining(2027) == 0.0


def test_show_days_same_day_and_overnight():
    start = dt("2026-08-01T09:00:00")
    assert show_days(start, start + timedelta(hours=1)) == 1
    assert show_days(start, start + timedelta(hours=15)) == 1
    assert show_days(start, start + timedelta(days=1)) == 1
    assert show_days(start, start + timedelta(days=2)) == 2


def test_interval_helpers():
    a = (dt("2026-05-01T00:00:00"), dt("2026-05-01T02:00:00"))
    b = (dt("2026-05-01T01:00:00"), dt("2026-05-01T03:00:00"))
    assert merge([a, b]) == [(dt("2026-05-01T00:00:00"), dt("2026-05-01T03:00:00"))]
    pieces = subtract([a], [(dt("2026-05-01T00:30:00"), dt("2026-05-01T01:30:00"))])
    assert pieces[0][0] == a[0] and pieces[-1][1] == a[1]
    assert sum((e - s).total_seconds() for s, e in pieces) == 3600
    years = list(split_by_year((dt("2026-12-31T23:00:00"), dt("2027-01-01T01:00:00"))))
    assert [year for year, _ in years] == [2026, 2027]


def test_planner_respects_cancel_cost_boundary():
    from loanlight.data import Show

    dataset = Dataset(root=None)  # type: ignore[arg-type]
    dataset.works = {"W": Work("W", "工艺", 10000.0, 100.0)}
    dataset.cabinets = {"C": Cabinet("C", "A", 100.0, 60)}
    dataset.instruments = {"D": Instrument("D", dt("2026-01-01T00:00:00"), dt("2027-01-01T00:00:00"))}
    dataset.shows = {
        "S": Show(
            "S", "A", dt("2026-07-01T09:00:00"), dt("2026-07-02T09:00:00"),
            "confirmed", datetime(2026, 6, 15).date(), 500.0, 1, "工艺", "standard",
        )
    }
    dataset.eligibility = []
    from loanlight.data import Eligibility

    dataset.eligibility = [
        Eligibility("e", "W", "A", "工艺", "standard", datetime(2026, 1, 1).date(), datetime(2027, 1, 1).date())
    ]
    planner = Planner(dataset, ExposureEngine(dataset))
    show = dataset.shows["S"]
    assert planner.cancel_cost(show, dt("2026-06-14T23:59:00")) == 0.0
    assert planner.cancel_cost(show, dt("2026-06-15T00:01:00")) == 500.0


def test_planner_never_books_same_work_in_overlapping_shows():
    from loanlight.data import Eligibility, Show

    dataset = Dataset(root=None)  # type: ignore[arg-type]
    dataset.works = {"W": Work("W", "工艺", 10000.0, 100.0)}
    dataset.cabinets = {"C": Cabinet("C", "A", 100.0, 60)}
    dataset.instruments = {"D": Instrument("D", dt("2026-01-01T00:00:00"), dt("2027-01-01T00:00:00"))}
    dataset.shows = {
        "S1": Show("S1", "A", dt("2026-07-01T09:00:00"), dt("2026-07-02T09:00:00"), "candidate", None, 0.0, 1, "工艺"),
        "S2": Show("S2", "B", dt("2026-07-01T09:00:00"), dt("2026-07-02T09:00:00"), "candidate", None, 0.0, 1, "工艺"),
    }
    dataset.eligibility = [
        Eligibility("e1", "W", "A", "工艺", "standard", datetime(2026, 1, 1).date(), datetime(2027, 1, 1).date()),
        Eligibility("e2", "W", "B", "工艺", "standard", datetime(2026, 1, 1).date(), datetime(2027, 1, 1).date()),
    ]
    planner = Planner(dataset, ExposureEngine(dataset))
    plan = planner.plan(dt("2026-05-01T00:00:00"))
    kept = [d for d in plan.decisions if d.decision == "keep"]
    assert len(kept) == 1
    assert plan.candidate_coverage == 1
