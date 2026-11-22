"""命令行输出的稳定契约与跨馆轮换示例。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from loanlight.cli import main
from loanlight.data import load_dataset
from loanlight.plan import PlanAssumptions, Planner
from loanlight.timeutil import parse_dt

ROOT = Path(__file__).resolve().parents[1]
BEFORE = parse_dt("2026-06-04T09:00:00+08:00")
AFTER = parse_dt("2026-06-05T10:00:00+08:00")


def test_account_cli_json(capsys):
    code = main(["--root", str(ROOT), "account", "--work", "W017", "--as-of", AFTER.isoformat(), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["work_id"] == "W017"
    assert payload["years"][0]["upper_lux_hours"] > payload["years"][0]["lower_lux_hours"]
    assert payload["measured_segments"]
    assert payload["pending_calibrations"] == []


def test_segments_cli_shows_evidence(capsys):
    code = main(["--root", str(ROOT), "segments", "--work", "W017", "--as-of", AFTER.isoformat()])
    assert code == 0
    text = capsys.readouterr().out
    assert "已测区间" in text and "缺测区间" in text
    assert "校正 C02" in text


def test_reconcile_cli_json(capsys):
    code = main(
        [
            "--root",
            str(ROOT),
            "reconcile",
            "--work",
            "W017",
            "--before",
            BEFORE.isoformat(),
            "--after",
            AFTER.isoformat(),
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["new_calibrations"] == ["C02"]
    assert payload["changed_groups"]


def test_plan_cli_reports_equivalents(capsys):
    code = main(["--root", str(ROOT), "plan", "--as-of", BEFORE.isoformat()])
    assert code == 0
    text = capsys.readouterr().out
    assert "等价解" in text
    assert "B-next" in text and "C-next" in text


def test_confirm_and_review_cli(tmp_path, capsys):
    path = tmp_path / "confirmations.jsonl"
    code = main(
        [
            "confirm",
            "--show",
            "B-next",
            "--decision",
            "keep",
            "--works",
            "W017,W020",
            "--at",
            BEFORE.isoformat(),
            "--confirmations",
            str(path),
        ]
    )
    assert code == 0
    assert path.exists()
    capsys.readouterr()  # 丢弃 confirm 的文本输出
    code = main(
        [
            "--root",
            str(ROOT),
            "review",
            "--as-of",
            AFTER.isoformat(),
            "--confirmations",
            str(path),
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stopped_unopened"][0]["show_id"] == "B-next"
    assert payload["irreversible_consumed"][0]["show_id"] == "A-past"


def test_candidate_assumptions_affect_coverage(dataset_module):
    dataset = dataset_module
    from loanlight.exposure import ExposureEngine

    planner = Planner(
        dataset, ExposureEngine(dataset), PlanAssumptions(candidate_lux=95.0, candidate_daily_hours=12.0)
    )
    plan = planner.plan(AFTER)
    assert plan.as_dict()["assumptions"]["candidate_lux"] == 95.0
    # 高照度假设下候选场次更难安排，但不会为了覆盖而超用额度。
    assert plan.protection_breaches == []


def test_plan_without_confirmation_does_not_mutate_inputs(dataset_module):
    dataset = dataset_module
    from loanlight.exposure import ExposureEngine

    before_state = [(p.placement_id, p.work_id) for p in dataset.placements]
    planner = Planner(dataset, ExposureEngine(dataset))
    planner.plan(BEFORE)
    planner.plan(AFTER)
    after_state = [(p.placement_id, p.work_id) for p in dataset.placements]
    assert before_state == after_state


def test_inspect_still_passes():
    from tools.inspect_inputs import inspect

    counts = inspect(ROOT)
    assert counts["works"] == 24


@pytest.fixture(scope="module")
def dataset_module():
    return load_dataset(ROOT)


def test_timeline_replay_attributes_correction(capsys):
    code = main(["--root", str(ROOT), "timeline", "--as-of", AFTER.isoformat(), "--json"])
    assert code == 0
    steps = json.loads(capsys.readouterr().out)
    assert [step["action"] for step in steps] == ["analyse", "calibration_arrival"]
    second = steps[1]
    assert second["newly_known_calibrations"] == ["C02"]
    assert {item["work_id"] for item in second["changed_works"]} == {"W005", "W011", "W017", "W023"}
    assert second["focus"]["affects_works"] == ["W005", "W011", "W017", "W023"]
    assert any(change["show_id"] == "B-next" for change in second["plan_changes"])
    # 更正发出前的时点不能被追溯改写。
    assert steps[0]["newly_known_calibrations"] == ["C01"]
    assert all("C02" not in step["known_calibrations"] for step in steps[:1])
