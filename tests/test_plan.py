"""轴展跨馆轮换、承诺兑现与馆方确认后的持续复核。"""
from __future__ import annotations

from pathlib import Path

import pytest

from loanlight.data import load_dataset
from loanlight.exposure import ExposureEngine
from loanlight.plan import Confirmation, PlanAssumptions, Planner, review_confirmations
from loanlight.store import (
    latest_confirmations,
    load_confirmations,
    make_confirmation,
    save_confirmations,
)
from loanlight.timeutil import parse_dt, show_days

ROOT = Path(__file__).resolve().parents[1]
BEFORE = parse_dt("2026-06-04T09:00:00+08:00")
AFTER = parse_dt("2026-06-05T10:00:00+08:00")


@pytest.fixture(scope="module")
def dataset():
    return load_dataset(ROOT)


@pytest.fixture(scope="module")
def planner(dataset):
    return Planner(dataset, ExposureEngine(dataset))


def test_rotating_shows_share_annual_quota_without_single_site_breach(dataset, planner):
    """单站都不超瞬时/年度上限，但全年多处展出会累积。"""
    plan = planner.plan(AFTER)
    assert plan.protection_breaches == []
    assert plan.conflicts == []
    # B 馆承诺的 W017 在 A 馆已消耗大量额度，更正后不得不替换。
    b = plan.decision("B-next")
    assert b.decision == "keep"
    assert "W017" not in b.works
    assert b.substituted and b.substituted[0][0] == "W017"
    # 替代必须命中 B/城市/standard 的 eligibility。
    eligible = set(planner.eligible_works(dataset.shows["B-next"]))
    assert set(b.works) <= eligible


def test_opened_show_exposure_cannot_be_erased_by_replanning(dataset, planner):
    plan = planner.plan(AFTER)
    opened = plan.decision("A-past")
    assert opened.decision == "opened"
    assert "W017" in opened.works
    # 已开幕场次的暴露仍出现在账户里。
    account = ExposureEngine(dataset).exposure("W017", AFTER)
    assert account.lower_lux_hours > 6000


def test_candidate_show_has_no_commitment_and_zero_abandon_cost(dataset, planner):
    plan = planner.plan(AFTER)
    candidate = plan.decision("C-next")
    assert candidate.status == "candidate"
    if candidate.decision == "cancel":
        assert candidate.cancel_cost == 0


def test_same_physical_work_is_not_planned_in_two_venues(planner):
    plan = planner.plan(AFTER)
    assert plan.conflicts == []
    by_work: dict[str, list[str]] = {}
    for decision in plan.decisions:
        if decision.decision != "keep":
            continue
        for work_id in decision.works:
            by_work.setdefault(work_id, []).append(decision.show_id)
    for work_id, shows in by_work.items():
        assert len(shows) <= 1, f"{work_id} 被同时安排到 {shows}"


def test_equivalent_solutions_are_reported_not_arbitrarily_picked(dataset, planner):
    plan = planner.plan(BEFORE)
    assert plan.equivalent_count > 1
    assert len(plan.alternatives) > 1
    assert all(item["score"] == list(plan.score) for item in plan.alternatives)


def test_correction_forces_different_plan_and_reports_conflict(dataset, planner):
    before = planner.plan(BEFORE)
    after = planner.plan(AFTER)
    assert before.decision("B-next").works != after.decision("B-next").works
    # 最优解在更正后改变；原有承诺失去保护依据。
    assert before.decision("B-next").works == ("W017", "W020")


def test_confirmation_is_the_starting_point_of_next_round(dataset, planner):
    confirmation = make_confirmation("B-next", "keep", ("W017", "W020"), BEFORE, "馆方已确认")
    plan = planner.plan(AFTER, confirmations=[confirmation])
    assert plan.fixed_by_confirmation == ["B-next"]
    b = plan.decision("B-next")
    assert b.fixed_by_confirmation is True
    assert b.works == ("W017", "W020")
    # 不能为了避开冲突而悄悄把已确认场次换成别的藏品。
    assert plan.protection_breaches


def test_correction_separates_stopped_consumed_and_retained(dataset, planner):
    confirmation = make_confirmation("B-next", "keep", ("W017", "W020"), BEFORE)
    plan = planner.plan(AFTER, confirmations=[confirmation])
    review = review_confirmations(planner, plan, [confirmation])
    assert [item["show_id"] for item in review.stopped_unopened] == ["B-next"]
    assert [item["show_id"] for item in review.irreversible_consumed] == ["A-past"]
    assert review.lost_basis
    assert any("W017" in reason for reason in review.lost_basis)


def test_retained_confirmation_when_still_protected(dataset, planner):
    confirmation = make_confirmation("C-next", "keep", ("W014", "W008"), AFTER)
    plan = planner.plan(AFTER, confirmations=[confirmation])
    review = review_confirmations(planner, plan, [confirmation])
    assert [item["show_id"] for item in review.retained] == ["C-next"]
    assert not review.stopped_unopened
    assert review.irreversible_consumed


def test_cancel_cost_zero_before_cancel_before_and_full_after(dataset, planner):
    show = dataset.shows["B-next"]
    assert planner.cancel_cost(show, parse_dt("2026-05-31T23:00:00+08:00")) == 0
    assert planner.cancel_cost(show, parse_dt("2026-06-02T00:00:00+08:00")) == show.cancel_cost


def test_confirmation_signature_detects_content(dataset):
    first = make_confirmation("B-next", "keep", ("W017", "W020"), BEFORE)
    same = make_confirmation("B-next", "keep", ("W020", "W017"), BEFORE)
    different = make_confirmation("B-next", "keep", ("W017",), BEFORE)
    assert first.signature == same.signature
    assert first.signature != different.signature


def test_confirmations_round_trip(tmp_path):
    path = tmp_path / "confirmations.jsonl"
    first = make_confirmation("B-next", "keep", ("W017", "W020"), BEFORE, "第一次")
    second = make_confirmation("B-next", "cancel", (), AFTER, "馆方反悔")
    save_confirmations(path, [first, second])
    loaded = load_confirmations(path)
    assert len(loaded) == 2
    assert latest_confirmations(loaded)[0].decision == "cancel"
    assert loaded[0].works == ("W017", "W020")


def test_show_days_does_not_add_end_date(dataset):
    show = dataset.shows["A-past"]
    assert show_days(show.start, show.end) == 3
    assert show_days(dataset.shows["B-next"].start, dataset.shows["B-next"].end) == 19


def test_candidate_assumptions_are_explicit(dataset):
    planner = Planner(
        dataset,
        ExposureEngine(dataset),
        PlanAssumptions(candidate_lux=40.0, candidate_daily_hours=6.0),
    )
    plan = planner.plan(AFTER)
    payload = plan.as_dict()
    assert payload["assumptions"] == {"candidate_lux": 40.0, "candidate_daily_hours": 6.0}


def test_planning_uses_upper_bound_not_lower_bound(dataset):
    engine = ExposureEngine(dataset)
    account = engine.exposure("W020", AFTER)
    # W020 缺测 1 小时，上界比下界多 100 lx·h；规划必须按上界算剩余。
    assert account.upper_lux_hours - account.lower_lux_hours == pytest.approx(100.0)
    assert account.remaining(2026)["safe_remaining_lux_hours"] < account.remaining(2026)["optimistic_remaining_lux_hours"]
