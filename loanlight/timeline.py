"""按 actions 时间线重放分析，展示更正如何传播。

actions 是独立的业务操作输入，不是分析结论；本模块只按时间顺序重放，
不把未来的更正提前用于过去的截至时点。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .data import Dataset
from .exposure import ExposureEngine
from .plan import Confirmation, Plan, Planner
from .timeutil import parse_dt


@dataclass
class TimelineStep:
    action_id: str
    at: object
    action: str
    object_id: str
    known_calibrations: list[str]
    changed_works: list[dict] = field(default_factory=list)
    newly_known_calibrations: list[str] = field(default_factory=list)
    plan_score: dict = field(default_factory=dict)
    plan_changes: list[dict] = field(default_factory=list)
    focus: dict | None = None

    def as_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "at": self.at.isoformat(),
            "action": self.action,
            "object_id": self.object_id,
            "known_calibrations": self.known_calibrations,
            "newly_known_calibrations": self.newly_known_calibrations,
            "changed_works": self.changed_works,
            "plan_score": self.plan_score,
            "plan_changes": self.plan_changes,
            "focus": self.focus,
        }


def replay(
    dataset: Dataset,
    confirmations: Iterable[Confirmation] = (),
    engine: ExposureEngine | None = None,
    planner: Planner | None = None,
) -> list[TimelineStep]:
    engine = engine or ExposureEngine(dataset)
    planner = planner or Planner(dataset, engine)
    actions = sorted(dataset.actions, key=lambda a: (a.at, a.action_id))

    previous: dict[str, dict] = {}
    previous_plan: Plan | None = None
    steps: list[TimelineStep] = []
    for action in actions:
        accounts = {work_id: engine.exposure(work_id, action.at) for work_id in dataset.works}
        changed = []
        for work_id, account in accounts.items():
            current = {
                "lower": account.lower_lux_hours,
                "upper": account.upper_lux_hours,
                "peak": account.peak_upper,
            }
            old = previous.get(work_id)
            if old is None:
                previous[work_id] = current
                continue
            if (
                abs(current["lower"] - old["lower"]) > 1e-6
                or abs(current["upper"] - old["upper"]) > 1e-6
                or abs(current["peak"] - old["peak"]) > 1e-6
            ):
                changed.append(
                    {
                        "work_id": work_id,
                        "delta_lower_lux_hours": round(current["lower"] - old["lower"], 6),
                        "delta_upper_lux_hours": round(current["upper"] - old["upper"], 6),
                        "delta_peak_lux": round(current["peak"] - old["peak"], 6),
                        "before": old,
                        "after": current,
                    }
                )
            previous[work_id] = current

        plan = planner.plan(action.at, confirmations=confirmations)
        plan_changes = []
        if previous_plan is not None:
            old_map = {d.show_id: d for d in previous_plan.decisions}
            for decision in plan.decisions:
                old = old_map.get(decision.show_id)
                if old is None or (old.decision, old.works) != (decision.decision, decision.works):
                    plan_changes.append(
                        {
                            "show_id": decision.show_id,
                            "before": {"decision": old.decision, "works": list(old.works)} if old else None,
                            "after": {"decision": decision.decision, "works": list(decision.works)},
                        }
                    )
        previous_plan = plan

        known: set[str] = set()
        for work_id in dataset.works:
            known.update(accounts[work_id].known_calibrations)

        focus = None
        # 只列出真正被该更正改写读数的藏品；同在 D6 的柜体不等于受影响。
        affected: list[str] = []
        calibration = next(
            (c for c in dataset.calibrations if c.calibration_id == action.object_id), None
        )
        if calibration is not None:
            for work_id, account in accounts.items():
                if calibration.calibration_id not in account.known_calibrations:
                    continue
                for segment in account.segments:
                    if calibration.calibration_id in segment.calibration_ids:
                        affected.append(work_id)
                        break
            focus = {
                "calibration_id": calibration.calibration_id,
                "device_id": calibration.device_id,
                "applicable": [calibration.start.isoformat(), calibration.end.isoformat()],
                "issued_at": calibration.issued_at.isoformat() if calibration.issued_at else None,
                "gain": calibration.gain,
                "offset": calibration.offset,
                "supersedes": calibration.supersedes,
                "affects_works": sorted(affected),
            }
        elif action.object_id in dataset.shows:
            focus = plan.decision(action.object_id).as_dict()

        steps.append(
            TimelineStep(
                action_id=action.action_id,
                at=action.at,
                action=action.action,
                object_id=action.object_id,
                known_calibrations=sorted(known),
                newly_known_calibrations=sorted(
                    set(known) - set(steps[-1].known_calibrations) if steps else set(known)
                ),
                changed_works=sorted(changed, key=lambda item: item["work_id"]),
                plan_score=plan.as_dict()["score"],
                plan_changes=plan_changes,
                focus=focus,
            )
        )
    return steps


def parse_action_time(value: str):
    return parse_dt(value)
