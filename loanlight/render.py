"""把分析结果渲染为稳定的文本与 JSON。"""
from __future__ import annotations

import json
from typing import Iterable

from .exposure import WorkExposure
from .plan import Plan, ShowPlan


def _lux(value: float) -> str:
    return f"{value:.1f}"


def render_account(account: WorkExposure) -> str:
    lines = [
        f"藏品 {account.work_id} 截至 {account.as_of.isoformat()}",
        f"  年度上限 {account.cap_lux_hours:g} lx·h；瞬时上限 {account.max_lux:g} lx",
        f"  已证明暴露下界 {_lux(account.lower_lux_hours)} lx·h；"
        f"含缺测的上界 {_lux(account.upper_lux_hours)} lx·h",
        f"  已测 {account.hours_measured:.1f} h；缺测 {account.hours_missing:.1f} h；"
        f"遮光 {account.hours_blackout:.1f} h（不消耗额度）",
        f"  峰值读数上界 {_lux(account.peak_upper)} lx",
    ]
    if account.known_calibrations:
        lines.append(f"  截至已知校正：{', '.join(account.known_calibrations)}")
    if account.pending_calibrations:
        lines.append(f"  截至尚未发出的更正：{', '.join(account.pending_calibrations)}")
    for year in sorted(account.years):
        remaining = account.remaining(year)
        status = "可证明安全" if remaining["proven"] else "上界已超限，不能按剩余额度规划"
        lines.append(
            f"  {year}：下界 {_lux(remaining['lower_lux_hours'])}，上界 {_lux(remaining['upper_lux_hours'])}，"
            f"可用剩余 {_lux(remaining['safe_remaining_lux_hours'])} lx·h（{status}）"
        )
    for warning in account.warnings:
        lines.append(f"  ! {warning}")
    measured = [s for s in account.segments if s.kind == "measured"]
    missing = [s for s in account.segments if s.kind == "missing"]
    lines.append(f"  已测区间 {len(measured)} 段（贡献额度），缺测区间 {len(missing)} 段（仍不确定）")
    return "\n".join(lines)


def render_segments(account: WorkExposure) -> str:
    lines = [f"藏品 {account.work_id} 区间明细（截至 {account.as_of.isoformat()}）"]
    for title, kind in (("已测区间", "measured"), ("缺测区间", "missing")):
        lines.append(f"[{title}]")
        rows = [s for s in account.segments if s.kind == kind]
        if not rows:
            lines.append("  （无）")
        for segment in rows:
            evidence = ",".join(segment.reading_ids) if segment.reading_ids else "无端点读数"
            cal = ",".join(segment.calibration_ids) or "unit"
            lines.append(
                f"  {segment.start.isoformat()} ~ {segment.end.isoformat()} "
                f"{segment.cabinet_id} {segment.placement_id or '-'} "
                f"{_lux(segment.lux_low)}~{_lux(segment.lux_high)} lx × {segment.hours:.2f} h "
                f"= {_lux(segment.lower_lux_hours)}~{_lux(segment.upper_lux_hours)} lx·h "
                f"[{evidence}; 校正 {cal}]"
            )
    return "\n".join(lines)


def render_reconcile(delta: dict) -> str:
    lines = [
        f"藏品 {delta['work_id']} 计量更正影响：{delta['before_as_of']} → {delta['after_as_of']}",
        f"  下界变化 {delta['delta_lower_lux_hours']:+.1f} lx·h；"
        f"上界变化 {delta['delta_upper_lux_hours']:+.1f} lx·h；"
        f"峰值变化 {delta['delta_peak_lux']:+.1f} lx",
        f"  新增已知校正：{', '.join(delta['new_calibrations']) or '无'}",
        f"  直接受影响的已测区间 {len(delta['changed_segments'])} 段",
    ]
    for item in delta["changed_segments"]:
        lines.append(
            f"    {item['start']} ~ {item['end']} {item['cabinet_id']} "
            f"{_lux(item['before_lux'][0])}→{_lux(item['after_lux'][0])} lx "
            f"（{','.join(item['before_calibrations']) or 'unit'} → "
            f"{','.join(item['after_calibrations']) or 'unit'}）"
        )
    for group in delta.get("changed_groups", []):
        lines.append(
            f"  汇总：{group['cabinet_id']} 由 {'/'.join(group['before_calibrations'])} 改为 "
            f"{'/'.join(group['after_calibrations'])}，{group['segment_count']} 段，"
            f"{group['first_start']} ~ {group['last_end']}，"
            f"额度变化 {group['delta_upper_lux_hours']:+.1f} lx·h"
        )
    if delta["new_segments"]:
        lines.append(f"  新增区间 {len(delta['new_segments'])} 段")
    if delta["retired_segments"]:
        lines.append(f"  不再成立的区间 {len(delta['retired_segments'])} 段")
    return "\n".join(lines)


def render_plan(plan: Plan) -> str:
    score = plan.as_dict()["score"]
    lines = [
        f"调整方案（截至 {plan.as_of.isoformat()}）",
        f"  保护与排期冲突 {score['protection_and_schedule_conflicts']} 项；确认场次撤约费用 {score['total_cancel_cost']:g} 元；"
        f"候选场次覆盖 {score['candidate_coverage']} 场；等价解 {plan.equivalent_count} 个",
        f"  候选假设：照度 {plan.assumptions.candidate_lux:g} lx、每日 {plan.assumptions.candidate_daily_hours:g} h"
        "（承诺之外的候选场次资料未给出照度，必须显式记录）",
    ]
    if plan.fixed_by_confirmation:
        lines.append(f"  从已确认继续：{', '.join(plan.fixed_by_confirmation)}")
    for decision in plan.decisions:
        lines.append("  " + _decision_line(decision))
        for reason in decision.reasons:
            lines.append(f"      · {reason}")
        for note in decision.protection_notes:
            lines.append(f"      ! {note}")
    if plan.protection_breaches:
        lines.append("  保护冲突：")
        for breach in plan.protection_breaches:
            lines.append(f"    - {breach}")
    if plan.conflicts:
        lines.append("  排期冲突（受影响馆方）：")
        for conflict in plan.conflicts:
            lines.append(f"    - {conflict}")
    for note in plan.notes:
        lines.append(f"  说明：{note}")
    return "\n".join(lines)


def _decision_line(decision: ShowPlan) -> str:
    if decision.decision == "opened":
        label = "已开幕（既成暴露）"
    elif decision.decision == "cancel":
        label = "取消"
    elif decision.decision == "keep":
        label = "保留"
    else:  # pragma: no cover
        label = decision.decision
    works = ",".join(decision.works) or "-"
    coverage = "达" if decision.coverage_ok else "未达"
    line = (
        f"{decision.show_id}（{decision.venue_id}/{decision.theme}/{decision.status}）"
        f"{label} 藏品 {works} 覆盖 {decision.coverage}/{decision.coverage_required}{coverage}"
    )
    if decision.cancel_cost:
        line += f" 撤约费用 {decision.cancel_cost:g} 元"
    return line


def render_committee(plans: Iterable[ShowPlan]) -> str:
    lines = ["各馆承诺与兑现情况"]
    for plan in plans:
        lines.append("  " + _decision_line(plan))
        if plan.dropped_works:
            dropped = [w for w in plan.dropped_works if w not in {a for a, _ in plan.substituted}]
            if dropped:
                lines.append(f"      未兑现承诺：{', '.join(dropped)}")
        if plan.substituted:
            lines.append(
                "      替代：" + "；".join(f"{a}→{b}" for a, b in plan.substituted)
                + "（需馆方确认，原承诺内容发生变化）"
            )
        if plan.extra_works:
            lines.append(f"      额外藏品：{', '.join(plan.extra_works)}")
        for note in plan.protection_notes:
            lines.append(f"      ! {note}")
        for reason in plan.unfulfilled_commitments:
            lines.append(f"      ! {reason}")
    return "\n".join(lines)


def to_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False, default=str)
