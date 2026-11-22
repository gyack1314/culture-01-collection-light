"""策展安排：在已有承诺、主题最低覆盖与藏品保护之间取舍。

优先级（见 docs/业务约定.md）：

1. 优先避免保护风险；
2. 其后最小化确认场次的撤约费用；
3. 最后最大化候选场次主题覆盖。

同一优先级允许多个等价解；本模块报告等价解数量与前若干个代表解，不替
策展人在等价编号之间做任意选择。

硬约束：

* 已开幕场次不可取消，实际暴露固定计入，不能被后续计划抹掉；
* 候选场次不构成对外承诺，放弃不产生撤约费用；
* 已确认但未开幕的场次撤约费用按 ``cancel_before`` 与 ``cancel_cost`` 计算；
* 同一件实物不能在同一时段出现在两馆；
* 替代藏品必须命中场次主题、馆方等级和借展期间的 eligibility。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations, product
from typing import Iterable

from .data import Dataset, Show
from .exposure import ExposureEngine, WorkExposure
from .timeutil import interval_hours, show_days, split_by_year


@dataclass(frozen=True)
class PlanAssumptions:
    """候选场次没有承诺时采用的照度假设；必须显式记录在输出中。"""

    candidate_lux: float = 50.0
    candidate_daily_hours: float = 8.0


@dataclass
class Confirmation:
    """馆方已经确认的调整；下一轮计划从这里继续。"""

    show_id: str
    decision: str  # "keep" | "cancel"
    works: tuple[str, ...] = ()
    confirmed_at: object | None = None
    note: str = ""
    signature: str = ""

    def as_dict(self) -> dict:
        return {
            "show_id": self.show_id,
            "decision": self.decision,
            "works": list(self.works),
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
            "note": self.note,
            "signature": self.signature,
        }


@dataclass
class ShowPlan:
    show_id: str
    venue_id: str
    theme: str
    status: str
    decision: str  # "keep" | "cancel" | "opened"
    works: tuple[str, ...] = ()
    coverage: int = 0
    coverage_required: int = 0
    coverage_ok: bool = False
    cancel_cost: float = 0.0
    committed_works: tuple[str, ...] = ()
    honoured_works: tuple[str, ...] = ()
    substituted: tuple[tuple[str, str], ...] = ()  # (承诺藏品, 替代藏品)
    dropped_works: tuple[str, ...] = ()
    extra_works: tuple[str, ...] = ()
    fixed_by_confirmation: bool = False
    protection_notes: list[str] = field(default_factory=list)
    unfulfilled_commitments: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def protected(self) -> bool:
        return not self.protection_notes

    def as_dict(self) -> dict:
        return {
            "show_id": self.show_id,
            "venue_id": self.venue_id,
            "theme": self.theme,
            "status": self.status,
            "decision": self.decision,
            "works": list(self.works),
            "coverage": self.coverage,
            "coverage_required": self.coverage_required,
            "coverage_ok": self.coverage_ok,
            "cancel_cost": self.cancel_cost,
            "committed_works": list(self.committed_works),
            "honoured_works": list(self.honoured_works),
            "substituted": [{"from": a, "to": b} for a, b in self.substituted],
            "dropped_works": list(self.dropped_works),
            "extra_works": list(self.extra_works),
            "fixed_by_confirmation": self.fixed_by_confirmation,
            "protected": self.protected,
            "protection_notes": self.protection_notes,
            "unfulfilled_commitments": self.unfulfilled_commitments,
            "reasons": self.reasons,
        }


@dataclass
class Plan:
    as_of: object
    decisions: list[ShowPlan]
    assumptions: PlanAssumptions
    total_cancel_cost: float = 0.0
    protection_breaches: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    unfulfilled_commitments: list[str] = field(default_factory=list)
    candidate_coverage: int = 0
    equivalent_count: int = 1
    alternatives: list[dict] = field(default_factory=list)
    fixed_by_confirmation: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def score(self) -> tuple:
        return (len(self.protection_breaches) + len(self.conflicts), self.total_cancel_cost, -self.candidate_coverage)

    def decision(self, show_id: str) -> ShowPlan:
        for decision in self.decisions:
            if decision.show_id == show_id:
                return decision
        raise KeyError(show_id)

    def as_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "assumptions": {
                "candidate_lux": self.assumptions.candidate_lux,
                "candidate_daily_hours": self.assumptions.candidate_daily_hours,
            },
            "score": {
                "protection_and_schedule_conflicts": len(self.protection_breaches) + len(self.conflicts),
                "total_cancel_cost": self.total_cancel_cost,
                "candidate_coverage": self.candidate_coverage,
            },
            "fixed_by_confirmation": self.fixed_by_confirmation,
            "decisions": [d.as_dict() for d in self.decisions],
            "breaches": self.protection_breaches,
            "conflicts": self.conflicts,
            "unfulfilled_commitments": self.unfulfilled_commitments,
            "equivalent_count": self.equivalent_count,
            "alternatives": self.alternatives,
            "notes": self.notes,
        }


class Planner:
    def __init__(
        self,
        dataset: Dataset,
        engine: ExposureEngine | None = None,
        assumptions: PlanAssumptions | None = None,
    ):
        self.dataset = dataset
        self.engine = engine or ExposureEngine(dataset)
        self.assumptions = assumptions or PlanAssumptions()
        self._eligibility: dict[str, list] = {}
        for entry in dataset.eligibility:
            self._eligibility.setdefault(entry.work_id, []).append(entry)
        self._commitments_by_show: dict[str, list] = {}
        for commitment in dataset.commitments:
            self._commitments_by_show.setdefault(commitment.show_id, []).append(commitment)

    # -- 基础查询 -----------------------------------------------------
    def show_status(self, show: Show, as_of) -> str:
        if show.status == "opened" or show.start <= as_of:
            return "opened"
        return show.status

    def eligible_works(self, show: Show) -> list[str]:
        result = []
        for work_id, entries in self._eligibility.items():
            for entry in entries:
                if (
                    entry.venue_id == show.venue_id
                    and entry.theme == show.theme
                    and entry.venue_grade == show.venue_grade
                    and entry.start <= show.start.date()
                    and entry.end >= show.end.date()
                ):
                    result.append(work_id)
                    break
        return sorted(set(result))

    def commitment_hours(self, commitment, show: Show) -> float:
        return commitment.planned_lux * commitment.daily_hours * show_days(show.start, show.end)

    def commitment_lux_hours(self, show: Show, work_id: str) -> float:
        for commitment in self._commitments_by_show.get(show.show_id, []):
            if commitment.work_id == work_id:
                return self.commitment_hours(commitment, show)
        return self._candidate_lux_hours(show)

    def show_hours(self, show: Show) -> float:
        return interval_hours((show.start, show.end))

    def _candidate_lux_hours(self, show: Show) -> float:
        return self.assumptions.candidate_lux * self.assumptions.candidate_daily_hours * show_days(show.start, show.end)

    def _planned_lux(self, show: Show, work_id: str) -> float:
        for commitment in self._commitments_by_show.get(show.show_id, []):
            if commitment.work_id == work_id:
                return commitment.planned_lux
        return self.assumptions.candidate_lux

    def _addition_by_year(self, show: Show, lux_hours: float) -> dict[int, float]:
        total = self.show_hours(show)
        if total <= 0:
            return {}
        result: dict[int, float] = {}
        for year, piece in split_by_year((show.start, show.end)):
            result[year] = result.get(year, 0.0) + lux_hours * interval_hours(piece) / total
        return result

    def cancel_cost(self, show: Show, as_of) -> float:
        """已确认场次的撤约费用；cancel_before 之前为 0；候选与已开幕不适用。"""
        if show.status != "confirmed":
            return 0.0
        if show.cancel_before is not None and as_of.date() < show.cancel_before:
            return 0.0
        return show.cancel_cost

    # -- 规划 ---------------------------------------------------------
    def plan(
        self,
        as_of,
        confirmations: Iterable[Confirmation] = (),
        max_equivalents: int = 8,
        horizon=None,
    ) -> Plan:
        shows = sorted(self.dataset.shows.values(), key=lambda s: (s.start, s.show_id))
        fixed = {c.show_id: c for c in confirmations if c.confirmed_at is None or c.confirmed_at <= as_of}
        opened = [s for s in shows if self.show_status(s, as_of) == "opened"]
        future = [s for s in shows if s not in opened]
        base = self._base_exposure(as_of, horizon)

        options: dict[str, list[ShowPlan]] = {}
        for show in future:
            if show.show_id in fixed:
                options[show.show_id] = [self._fixed_option(show, fixed[show.show_id], as_of, base)]
            else:
                options[show.show_id] = self._show_options(show, as_of, base)

        opened_plans = [self._opened_plan(show) for show in opened]
        keys = [s.show_id for s in future]
        solutions: list[tuple[tuple, list[ShowPlan]]] = []
        for choice in product(*(options[key] for key in keys)):
            decisions = [_clone(plan) for plan in choice]
            score, breaches, conflicts, unfulfilled = self._evaluate(decisions, base)
            solutions.append((score, decisions))

        if not solutions:  # pragma: no cover - 至少存在空方案
            solutions = [((0, 0.0, 0), [])]
        best_score = min(score for score, _ in solutions)
        best = [(score, decisions) for score, decisions in solutions if score == best_score]
        best.sort(key=lambda item: _signature(item[1]))
        plan = self._assemble(best[0][1], opened_plans, as_of, base)
        plan.equivalent_count = len(best)
        plan.alternatives = [
            {
                "score": list(score),
                "decisions": [
                    {"show_id": d.show_id, "decision": d.decision, "works": list(d.works), "cancel_cost": d.cancel_cost}
                    for d in decisions
                ],
            }
            for score, decisions in best[:max_equivalents]
        ]
        plan.fixed_by_confirmation = sorted(fixed)
        return plan

    def _base_exposure(self, as_of, horizon=None) -> dict[str, WorkExposure]:
        """已实现暴露：只使用截至时点已知的事实，不用未来读数补写历史。

        已开幕但尚未结束的场次只计入截至时点已经发生的部分；其剩余天数在
        后续分析中以新的读数出现，而不是提前按计划计入。
        """
        horizon = horizon or as_of
        return {work_id: self.engine.exposure(work_id, horizon) for work_id in self.dataset.works}

    def _base_remaining(self, base: dict[str, WorkExposure], work_id: str, year: int) -> float:
        return max(0.0, base[work_id].remaining(year)["safe_remaining_lux_hours"])

    def commitment_report(self, as_of) -> list[ShowPlan]:
        """不经优化，按现有承诺与候选状态如实报告各馆兑现情况。"""
        base = self._base_exposure(as_of)
        result: list[ShowPlan] = []
        for show in sorted(self.dataset.shows.values(), key=lambda s: (s.start, s.show_id)):
            if self.show_status(show, as_of) == "opened":
                result.append(self._opened_plan(show))
                continue
            commitments = sorted(self._commitments_by_show.get(show.show_id, []), key=lambda c: c.commitment_id)
            if not commitments:
                plan = ShowPlan(
                    show_id=show.show_id,
                    venue_id=show.venue_id,
                    theme=show.theme,
                    status=show.status,
                    decision="candidate",
                    coverage_required=show.min_items,
                    reasons=["候选场次：尚无对外承诺，藏品组合在 plan 中给出"],
                )
                result.append(plan)
                continue
            works = tuple(c.work_id for c in commitments)
            option = self._make_option(show, works, base, source="承诺原样")
            if option is None:
                option = ShowPlan(
                    show_id=show.show_id,
                    venue_id=show.venue_id,
                    theme=show.theme,
                    status=show.status,
                    decision="keep",
                    works=works,
                    coverage=len(works),
                    coverage_required=show.min_items,
                    coverage_ok=len(works) >= show.min_items,
                    committed_works=tuple(sorted(works)),
                    honoured_works=tuple(sorted(works)),
                    unfulfilled_commitments=[],
                    reasons=["承诺藏品不满足 current eligibility 约束，需与馆方重议"],
                )
            result.append(option)
        return result

    def _work_problems(self, show: Show, work_id: str, base: dict[str, WorkExposure]) -> list[str]:
        work = self.dataset.works[work_id]
        problems = []
        account = base[work_id]
        planned = self._planned_lux(show, work_id)
        if planned > work.max_lux:
            problems.append(f"计划 {planned:g} lx > 瞬时上限 {work.max_lux:g} lx")
        for year, amount in self._addition_by_year(show, self.commitment_lux_hours(show, work_id)).items():
            remaining = self._base_remaining(base, work_id, year)
            if remaining < amount - 1e-9:
                problems.append(
                    f"{year} 年需 {amount:.1f} lx·h，证据允许的可用上界仅 {remaining:.1f} lx·h"
                )
        return _uniq(problems)

    def _work_fits(self, show: Show, work_id: str, base: dict[str, WorkExposure]) -> bool:
        return not self._work_problems(show, work_id, base)

    def _make_option(
        self,
        show: Show,
        works: Iterable[str],
        base: dict[str, WorkExposure],
        source: str,
        substitutions: tuple[tuple[str, str], ...] = (),
        fixed: bool = False,
    ) -> ShowPlan | None:
        works = tuple(dict.fromkeys(works))
        if len(works) < show.min_items or len(works) != len(set(works)):
            return None
        eligible = set(self.eligible_works(show))
        if any(work_id not in eligible for work_id in works):
            return None
        commitments = {c.work_id: c for c in self._commitments_by_show.get(show.show_id, [])}
        plan = ShowPlan(
            show_id=show.show_id,
            venue_id=show.venue_id,
            theme=show.theme,
            status=show.status,
            decision="keep",
            works=works,
            coverage=len(works),
            coverage_required=show.min_items,
            coverage_ok=len(works) >= show.min_items,
            committed_works=tuple(sorted(commitments)),
            honoured_works=tuple(sorted(w for w in works if w in commitments)),
            substituted=substitutions,
            dropped_works=tuple(sorted(w for w in commitments if w not in works)),
            extra_works=tuple(sorted(w for w in works if w not in commitments)),
            fixed_by_confirmation=fixed,
            reasons=[f"来源：{source}"],
        )
        for work_id in works:
            problem = self._work_problems(show, work_id, base)
            if problem:
                plan.protection_notes.append(f"{work_id}：" + "；".join(problem))
        # 承诺藏品被替换时，替代关系需要记录；若某承诺既未被兑现也未被替代，单独列出。
        replaced = {original for original, _replacement in substitutions}
        plan.unfulfilled_commitments = [
            f"{show.show_id} 承诺 {work_id} 未兑现或未落实替代（影响馆方 {show.venue_id}）"
            for work_id in plan.dropped_works
            if work_id not in replaced
        ]
        for original, replacement in substitutions:
            plan.reasons.append(
                f"承诺 {original} 因保护限制改由 {replacement} 替代（馆方 {show.venue_id} 需确认）"
            )
        if plan.unfulfilled_commitments:
            plan.reasons.append("主题最低覆盖不能把未兑现的承诺算作完成")
        return plan

    def _candidate_options_for_show(self, show: Show, base: dict[str, WorkExposure], limit: int = 40) -> list[ShowPlan]:
        pool = self.eligible_works(show)
        required = show.min_items
        if len(pool) < required:
            return []
        scored = []
        for work_id in pool:
            feasible = self._work_fits(show, work_id, base)
            years = self._addition_by_year(show, self._candidate_lux_hours(show))
            headroom = min(
                (self._base_remaining(base, work_id, year) - amount for year, amount in years.items()),
                default=0.0,
            )
            scored.append((0 if feasible else 1, -headroom, work_id))
        scored.sort()
        feasible = [work_id for flag, _h, work_id in scored if flag == 0]
        candidates = feasible if len(feasible) >= required else [w for _f, _h, w in scored]
        options = []
        for works in combinations(candidates, required):
            option = self._make_option(show, works, base, source="candidate")
            if option is not None:
                options.append(option)
            if len(options) >= limit:
                break
        return options

    def _commitment_options(self, show: Show, commitments, base) -> list[ShowPlan]:
        """承诺方案：原样保留，或对不安全的承诺给出命中 eligibility 的替代。"""
        if not commitments:
            return self._candidate_options_for_show(show, base)
        pool = self.eligible_works(show)
        committed = {c.work_id for c in commitments}
        options: list[ShowPlan] = []

        original = self._make_option(show, [c.work_id for c in commitments], base, source="承诺原样")
        if original is not None:
            options.append(original)

        # 每个承诺位置可保留原藏品或换成一个合格替代；按笛卡尔积生成，最多 24 个。
        choices = []
        for commitment in commitments:
            alternatives = [commitment.work_id]
            if not self._work_fits(show, commitment.work_id, base):
                alternatives.extend(w for w in pool if w not in committed)
            choices.append(alternatives)
        seen = set()
        for works in product(*choices):
            if len(set(works)) != len(works):
                continue
            substitutions = tuple(
                (commitment.work_id, work)
                for commitment, work in zip(commitments, works)
                if work != commitment.work_id
            )
            key = tuple(works)
            if key in seen:
                continue
            seen.add(key)
            option = self._make_option(
                show,
                works,
                base,
                source="替代" if substitutions else "承诺原样",
                substitutions=substitutions,
            )
            if option is not None:
                options.append(option)
            if len(options) >= 24:
                break

        # 承诺数量少于主题最低覆盖时，补充合格藏品。
        if len(commitments) < show.min_items:
            for candidate in pool:
                if candidate in committed:
                    continue
                option = self._make_option(
                    show, [c.work_id for c in commitments] + [candidate], base, source="补充主题覆盖"
                )
                if option is not None:
                    options.append(option)
        return _dedupe_options(options)

    def _show_options(self, show: Show, as_of, base: dict[str, WorkExposure]) -> list[ShowPlan]:
        cancel = self._cancel_plan(show, as_of)
        if show.status == "candidate":
            options = self._candidate_options_for_show(show, base)
            options.append(cancel)  # 候选不构成对外承诺，放弃无费用
            return _dedupe_options(options)
        commitments = sorted(self._commitments_by_show.get(show.show_id, []), key=lambda c: c.commitment_id)
        options = self._commitment_options(show, commitments, base)
        options.append(cancel)
        return _dedupe_options(options)

    def _cancel_plan(self, show: Show, as_of) -> ShowPlan:
        cost = self.cancel_cost(show, as_of)
        reasons = [f"撤约费用 {cost:g} 元" + ("（cancel_before 之前）" if cost == 0 else "")]
        unfulfilled = [f"{show.show_id} 承诺 {w} 未兑现（馆方 {show.venue_id}）" for w in sorted(
            c.work_id for c in self._commitments_by_show.get(show.show_id, [])
        )]
        if unfulfilled:
            reasons.append("撤约导致承诺内容无法兑现")
        return ShowPlan(
            show_id=show.show_id,
            venue_id=show.venue_id,
            theme=show.theme,
            status=show.status,
            decision="cancel",
            coverage=0,
            coverage_required=show.min_items,
            coverage_ok=False,
            cancel_cost=cost,
            committed_works=tuple(sorted(c.work_id for c in self._commitments_by_show.get(show.show_id, []))),
            unfulfilled_commitments=unfulfilled,
            reasons=reasons,
        )

    def _fixed_option(self, show: Show, confirmation: Confirmation, as_of, base) -> ShowPlan:
        if confirmation.decision == "cancel":
            plan = self._cancel_plan(show, as_of)
            plan.fixed_by_confirmation = True
            plan.reasons = ["馆方已确认撤约；本轮不再重新选择"] + plan.reasons
            return plan
        works = tuple(confirmation.works) or tuple(
            sorted(c.work_id for c in self._commitments_by_show.get(show.show_id, []))
        )
        plan = self._make_option(show, works, base, source="馆方已确认", fixed=True)
        if plan is None:
            plan = ShowPlan(
                show_id=show.show_id,
                venue_id=show.venue_id,
                theme=show.theme,
                status=show.status,
                decision="keep",
                works=works,
                coverage=len(works),
                coverage_required=show.min_items,
                coverage_ok=False,
                fixed_by_confirmation=True,
                unfulfilled_commitments=[
                    f"{show.show_id} 承诺 {w} 未兑现（馆方 {show.venue_id}）"
                    for w in sorted(c.work_id for c in self._commitments_by_show.get(show.show_id, []))
                    if w not in works
                ],
                reasons=["已确认藏品不满足场次 eligibility 或数量要求"],
            )
        plan.reasons.append("馆方已确认；本轮从该确认继续，不重新视为自由选择")
        return plan

    def _opened_plan(self, show: Show) -> ShowPlan:
        works = []
        for placement in self.dataset.placements:
            if placement.end <= show.start or placement.start >= show.end:
                continue
            cabinet = self.dataset.cabinets[placement.cabinet_id]
            if cabinet.venue_id == show.venue_id:
                works.append(placement.work_id)
        works = tuple(sorted(set(works)))
        committed = tuple(sorted(c.work_id for c in self._commitments_by_show.get(show.show_id, [])))
        covered = sum(1 for work_id in works if self.dataset.works[work_id].theme == show.theme)
        reasons = ["已开幕：实际暴露不可撤销，计划只记录"]
        if not works:
            reasons.append("资料中没有该场次的布展记录；不能据此推定实际展出藏品")
        return ShowPlan(
            show_id=show.show_id,
            venue_id=show.venue_id,
            theme=show.theme,
            status=show.status,
            decision="opened",
            works=works,
            coverage=covered,
            coverage_required=show.min_items,
            coverage_ok=covered >= show.min_items,
            committed_works=committed,
            reasons=reasons,
        )

    # -- 评估 ---------------------------------------------------------
    def _evaluate(
        self, decisions: list[ShowPlan], base: dict[str, WorkExposure]
    ) -> tuple[tuple, list[str], list[str], list[str]]:
        breaches: list[str] = []
        conflicts: list[str] = []
        unfulfilled: list[str] = []
        additions: dict[str, dict[int, float]] = {}
        booking: list[tuple[Show, str]] = []

        for plan in decisions:
            show = self.dataset.shows[plan.show_id]
            unfulfilled.extend(plan.unfulfilled_commitments)
            if plan.decision != "keep":
                continue
            for work_id in plan.works:
                lux_hours = self.commitment_lux_hours(show, work_id)
                for year, amount in self._addition_by_year(show, lux_hours).items():
                    additions.setdefault(work_id, {}).setdefault(year, 0.0)
                    additions[work_id][year] += amount
                booking.append((show, work_id))
                if self._planned_lux(show, work_id) > self.dataset.works[work_id].max_lux:
                    conflicts.append(
                        f"{work_id} 在 {show.show_id} 的计划照度 {self._planned_lux(show, work_id):g} lx "
                        f"超过瞬时上限 {self.dataset.works[work_id].max_lux:g} lx"
                    )

        for work_id, per_year in additions.items():
            for year, amount in per_year.items():
                remaining = self._base_remaining(base, work_id, year)
                if amount > remaining + 1e-9:
                    breaches.append(
                        f"{work_id} {year} 年计划新增 {amount:.1f} lx·h 超过证据允许的可用上界 "
                        f"{remaining:.1f} lx·h"
                    )

        # 同一件实物不能同时出现在两馆（含与已开幕场次冲突）。
        opened_booking = self._opened_bookings()
        all_booking = booking + opened_booking
        for i in range(len(all_booking)):
            show_a, work_a = all_booking[i]
            for j in range(i + 1, len(all_booking)):
                show_b, work_b = all_booking[j]
                if work_a != work_b or show_a.show_id == show_b.show_id:
                    continue
                if show_a.start < show_b.end and show_b.start < show_a.end:
                    conflicts.append(
                        f"{work_a} 同时安排在两馆：{show_a.show_id}（馆方 {show_a.venue_id}）与 "
                        f"{show_b.show_id}（馆方 {show_b.venue_id}）"
                    )

        candidate_coverage = sum(
            1
            for plan in decisions
            if plan.status == "candidate" and plan.decision == "keep" and plan.coverage_ok
        )
        cost = sum(plan.cancel_cost for plan in decisions if plan.decision == "cancel")
        score = (len(breaches) + len(conflicts), cost, -candidate_coverage)
        return score, _uniq(breaches), _uniq(conflicts), _uniq(unfulfilled)

    def _opened_bookings(self) -> list[tuple[Show, str]]:
        result = []
        for show in self.dataset.shows.values():
            if show.status != "opened":
                continue
            for placement in self.dataset.placements:
                if placement.end <= show.start or placement.start >= show.end:
                    continue
                if self.dataset.cabinets[placement.cabinet_id].venue_id == show.venue_id:
                    result.append((show, placement.work_id))
        return result

    def _assemble(self, decisions, opened_plans, as_of, base) -> Plan:
        all_decisions = sorted(
            opened_plans + decisions,
            key=lambda plan: (self.dataset.shows[plan.show_id].start, plan.show_id),
        )
        plan = Plan(as_of=as_of, decisions=all_decisions, assumptions=self.assumptions)
        _, breaches, conflicts, unfulfilled = self._evaluate(decisions, base)
        plan.protection_breaches = breaches
        plan.conflicts = _uniq(conflicts)
        plan.unfulfilled_commitments = _uniq(unfulfilled)
        plan.total_cancel_cost = sum(p.cancel_cost for p in decisions if p.decision == "cancel")
        plan.candidate_coverage = sum(
            1 for p in decisions if p.status == "candidate" and p.decision == "keep" and p.coverage_ok
        )
        if any(p.status == "opened" and not p.coverage_ok for p in opened_plans):
            plan.notes.append("已开幕场次主题覆盖按实际布展统计；不足部分不能靠后续计划补记")
        return plan


# -- 馆方确认后的持续复核 ---------------------------------------------
@dataclass
class ContinuityReview:
    as_of: object
    stopped_unopened: list[dict] = field(default_factory=list)
    irreversible_consumed: list[dict] = field(default_factory=list)
    retained: list[dict] = field(default_factory=list)
    lost_basis: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "stopped_unopened": self.stopped_unopened,
            "irreversible_consumed": self.irreversible_consumed,
            "retained": self.retained,
            "lost_basis": self.lost_basis,
        }


def review_confirmations(
    planner: Planner, plan: Plan, confirmations: Iterable[Confirmation]
) -> ContinuityReview:
    """按更正后的计划，把已确认场次分成三类。

    * 需要停止的未开展场次：确认保留，但保护依据已不成立；
    * 无法撤销的既成消耗：已开幕场次，暴露已经发生；
    * 仍可保持：确认保留且保护仍成立。
    """
    review = ContinuityReview(as_of=plan.as_of)
    confirmed = {c.show_id: c for c in confirmations}
    for decision in plan.decisions:
        if decision.decision == "opened":
            review.irreversible_consumed.append(
                {
                    "show_id": decision.show_id,
                    "venue_id": decision.venue_id,
                    "works": list(decision.works),
                    "reason": "已开幕，实际暴露不可撤销",
                }
            )
            continue
        if decision.show_id not in confirmed:
            continue
        entry = decision.as_dict()
        if decision.decision == "keep" and decision.protected and decision.coverage_ok:
            entry["reason"] = "确认仍与当前证据相容，可保持"
            review.retained.append(entry)
        else:
            entry["reason"] = "确认已失去保护依据，需停止或重新协商"
            review.stopped_unopened.append(entry)
            review.lost_basis.extend(decision.protection_notes)
            if decision.decision == "cancel":
                review.lost_basis.append(f"{decision.show_id} 已按当前证据撤约")
    review.lost_basis = _uniq(review.lost_basis)
    return review


# -- 小工具 -----------------------------------------------------------
def _clone(plan: ShowPlan) -> ShowPlan:
    return ShowPlan(
        show_id=plan.show_id,
        venue_id=plan.venue_id,
        theme=plan.theme,
        status=plan.status,
        decision=plan.decision,
        works=plan.works,
        coverage=plan.coverage,
        coverage_required=plan.coverage_required,
        coverage_ok=plan.coverage_ok,
        cancel_cost=plan.cancel_cost,
        committed_works=plan.committed_works,
        honoured_works=plan.honoured_works,
        substituted=plan.substituted,
        dropped_works=plan.dropped_works,
        extra_works=plan.extra_works,
        fixed_by_confirmation=plan.fixed_by_confirmation,
        protection_notes=list(plan.protection_notes),
        unfulfilled_commitments=list(plan.unfulfilled_commitments),
        reasons=list(plan.reasons),
    )


def _uniq(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _dedupe_options(options: list[ShowPlan]) -> list[ShowPlan]:
    seen = set()
    result = []
    for option in options:
        key = (option.decision, option.works, option.cancel_cost)
        if key in seen:
            continue
        seen.add(key)
        result.append(option)
    return result


def _signature(decisions: list[ShowPlan]) -> tuple:
    return tuple((p.show_id, p.decision, p.works) for p in decisions)


