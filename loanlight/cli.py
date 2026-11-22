"""命令行入口：查询账户、承诺、调整方案、更正影响与持续复核。

示例（在项目根目录运行）：

    python -m loanlight account --work W017 --as-of 2026-06-05T10:00:00+08:00
    python -m loanlight segments --work W017 --as-of 2026-06-05T10:00:00+08:00
    python -m loanlight commitments --as-of 2026-06-05T10:00:00+08:00
    python -m loanlight plan --as-of 2026-06-04T09:00:00+08:00
    python -m loanlight reconcile --work W017 --before 2026-06-04T09:00:00+08:00 --after 2026-06-05T10:00:00+08:00
    python -m loanlight confirm --show B-next --decision keep --works W017,W020 --at ...
    python -m loanlight review --as-of 2026-06-05T10:00:00+08:00
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .calibration import reading_correction_details
from .data import load_dataset
from .exposure import ExposureEngine
from .plan import Confirmation, Planner, PlanAssumptions, review_confirmations
from .render import (
    render_account,
    render_committee,
    render_plan,
    render_reconcile,
    render_segments,
    to_json,
)
from .store import latest_confirmations, load_confirmations, make_confirmation, save_confirmations
from .timeutil import parse_dt

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIRMATIONS = "output/confirmations.jsonl"


def _load(args):
    return load_dataset(args.root)


def _as_of(args):
    return parse_dt(args.as_of) if args.as_of else None


def cmd_account(args) -> int:
    dataset = _load(args)
    engine = ExposureEngine(dataset)
    as_of = _as_of(args)
    work_ids = [args.work] if args.work else sorted(dataset.works)
    for work_id in work_ids:
        account = engine.exposure(work_id, as_of)
        if args.json:
            print(to_json(account.as_dict()))
        else:
            print(render_account(account))
            print()
    return 0


def cmd_segments(args) -> int:
    dataset = _load(args)
    engine = ExposureEngine(dataset)
    as_of = _as_of(args)
    account = engine.exposure(args.work, as_of)
    if args.json:
        print(to_json(account.as_dict()))
    else:
        print(render_segments(account))
    return 0


def cmd_readings(args) -> int:
    dataset = _load(args)
    as_of = _as_of(args)
    devices = {args.device} if args.device else None
    rows = [
        reading_correction_details(dataset, reading, as_of)
        for reading in dataset.readings
        if (devices is None or reading.device_id in devices)
        and (args.cabinet is None or reading.cabinet_id == args.cabinet)
    ]
    if args.json:
        print(to_json(rows))
    else:
        for row in rows:
            calibration = row["calibration"]["calibration_id"] or "unit"
            print(
                f"{row['reading_id']} {row['at']} {row['device_id']}/{row['cabinet_id']} "
                f"原始 {row['raw_lux']:.2f} → 校正 {row['corrected_lux']:.3f} lx（{calibration}）"
            )
    return 0


def _planner(args, dataset):
    engine = ExposureEngine(dataset)
    assumptions = PlanAssumptions(candidate_lux=args.candidate_lux, candidate_daily_hours=args.candidate_daily_hours)
    return Planner(dataset, engine, assumptions)


def _confirmations(args) -> list[Confirmation]:
    path = Path(args.confirmations)
    if not path.exists():
        return []
    return latest_confirmations(load_confirmations(path))


def cmd_commitments(args) -> int:
    dataset = _load(args)
    planner = _planner(args, dataset)
    as_of = _as_of(args)
    if args.plan:
        plan = planner.plan(as_of, confirmations=_confirmations(args))
        decisions = plan.decisions
    else:
        decisions = planner.commitment_report(as_of)
    if args.json:
        print(to_json([decision.as_dict() for decision in decisions]))
    else:
        print(render_committee(decisions))
    return 0


def cmd_plan(args) -> int:
    dataset = _load(args)
    planner = _planner(args, dataset)
    plan = planner.plan(_as_of(args), confirmations=_confirmations(args), max_equivalents=args.max_equivalents)
    if args.json:
        print(to_json(plan.as_dict()))
    else:
        print(render_plan(plan))
    return 0


def cmd_reconcile(args) -> int:
    dataset = _load(args)
    engine = ExposureEngine(dataset)
    delta = engine.reconcile(args.work, parse_dt(args.before), parse_dt(args.after))
    if args.json:
        print(to_json(delta))
    else:
        print(render_reconcile(delta))
    return 0


def cmd_confirm(args) -> int:
    at = parse_dt(args.at) if args.at else None
    if at is None:
        raise SystemExit("确认必须带 --at（馆方确认时间）")
    works = tuple(w for w in (args.works or "").split(",") if w)
    confirmation = make_confirmation(args.show, args.decision, works, at, args.note or "")
    path = save_confirmations(args.confirmations, [confirmation], append=True)
    print(f"已记录确认：{confirmation.show_id} {confirmation.decision} {list(confirmation.works)} → {path}")
    return 0


def cmd_review(args) -> int:
    dataset = _load(args)
    planner = _planner(args, dataset)
    confirmations = _confirmations(args)
    as_of = _as_of(args)
    plan = planner.plan(as_of, confirmations=confirmations)
    review = review_confirmations(planner, plan, confirmations)
    if args.json:
        print(to_json(review.as_dict()))
    else:
        print(f"馆方确认后的持续复核（截至 {review.as_of.isoformat()}）")
        print(f"  需要停止的未开展场次：{len(review.stopped_unopened)}")
        for item in review.stopped_unopened:
            print(f"    - {item['show_id']}（{item['venue_id']}）{item['reason']}")
            for note in item.get("protection_notes", []):
                print(f"        ! {note}")
        print(f"  无法撤销的既成消耗：{len(review.irreversible_consumed)}")
        for item in review.irreversible_consumed:
            print(f"    - {item['show_id']}（{item['venue_id']}）{', '.join(item['works'])}")
        print(f"  仍可保持的确认：{len(review.retained)}")
        for item in review.retained:
            print(f"    - {item['show_id']}（{item['venue_id']}）{', '.join(item['works'])}")
        if review.lost_basis:
            print("  失去保护依据的原因：")
            for reason in review.lost_basis:
                print(f"    - {reason}")
    return 0


def cmd_timeline(args) -> int:
    from .timeline import replay

    dataset = _load(args)
    steps = replay(dataset, confirmations=_confirmations(args))
    if args.json:
        print(to_json([step.as_dict() for step in steps]))
        return 0
    for step in steps:
        print(f"{step.at.isoformat()} {step.action} {step.object_id}（{step.action_id}）")
        if step.newly_known_calibrations:
            print(f"  新增已知校正：{', '.join(step.newly_known_calibrations)}")
        if step.changed_works:
            print(f"  账户变化：{len(step.changed_works)} 件藏品")
            for item in step.changed_works:
                print(
                    f"    {item['work_id']} 下界 {item['delta_lower_lux_hours']:+.1f}，"
                    f"上界 {item['delta_upper_lux_hours']:+.1f} lx·h，"
                    f"峰值 {item['delta_peak_lux']:+.1f} lx"
                )
        else:
            print("  账户变化：无（该时点之前没有新的已知事实）")
        score = step.plan_score
        print(
            f"  方案：冲突 {score['protection_and_schedule_conflicts']}，"
            f"撤约费用 {score['total_cancel_cost']:g} 元，候选覆盖 {score['candidate_coverage']} 场"
        )
        for change in step.plan_changes:
            before = change["before"]
            before_text = f"{before['decision']} {before['works']}" if before else "无"
            after = change["after"]
            print(f"    {change['show_id']}：{before_text} → {after['decision']} {after['works']}")
        if step.focus and "calibration_id" in step.focus:
            focus = step.focus
            print(
                f"  更正 {focus['calibration_id']}：{focus['device_id']} "
                f"{focus['applicable'][0]}~{focus['applicable'][1]}，"
                f"gain={focus['gain']:g} offset={focus['offset']:g}，影响 {len(focus['affects_works'])} 件藏品"
            )
        print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loanlight", description="巡展累积光暴露分析与借展承诺安排")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="项目根目录（含 data/）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--as-of", required=True, help="分析截至时点（带时区 ISO 8601）")
        p.add_argument("--confirmations", default=DEFAULT_CONFIRMATIONS, help="确认记录 JSONL 路径")
        p.add_argument("--candidate-lux", type=float, default=50.0, help="候选场次假设照度 lx")
        p.add_argument("--candidate-daily-hours", type=float, default=8.0, help="候选场次假设每日小时")
        p.add_argument("--json", action="store_true", help="输出 JSON")

    account = sub.add_parser("account", help="查询藏品年度账户")
    account.add_argument("--work", help="藏品编号；省略则列出全部")
    account.add_argument("--as-of", required=True)
    account.add_argument("--json", action="store_true", help="输出 JSON")
    account.set_defaults(func=cmd_account)

    segments = sub.add_parser("segments", help="列出贡献额度的已测区间与仍不确定的缺测区间")
    segments.add_argument("--work", required=True)
    segments.add_argument("--as-of", required=True)
    segments.add_argument("--json", action="store_true", help="输出 JSON")
    segments.set_defaults(func=cmd_segments)

    readings = sub.add_parser("readings", help="查看原始读数的校正明细")
    readings.add_argument("--device")
    readings.add_argument("--cabinet")
    readings.add_argument("--as-of", required=True)
    readings.add_argument("--json", action="store_true", help="输出 JSON")
    readings.set_defaults(func=cmd_readings)

    commitments = sub.add_parser("commitments", help="各馆承诺与兑现情况")
    common(commitments)
    commitments.add_argument("--plan", action="store_true", help="按当前最优方案报告兑现情况")
    commitments.set_defaults(func=cmd_commitments)

    plan = sub.add_parser("plan", help="给出可行调整方案、代价与等价解")
    common(plan)
    plan.add_argument("--max-equivalents", type=int, default=8)
    plan.set_defaults(func=cmd_plan)

    reconcile = sub.add_parser("reconcile", help="比较计量更正前后账户变化")
    reconcile.add_argument("--work", required=True)
    reconcile.add_argument("--before", required=True)
    reconcile.add_argument("--after", required=True)
    reconcile.add_argument("--json", action="store_true", help="输出 JSON")
    reconcile.set_defaults(func=cmd_reconcile)

    confirm = sub.add_parser("confirm", help="记录馆方确认的调整")
    confirm.add_argument("--show", required=True)
    confirm.add_argument("--decision", choices=("keep", "cancel"), required=True)
    confirm.add_argument("--works", default="")
    confirm.add_argument("--at", required=True)
    confirm.add_argument("--note", default="")
    confirm.add_argument("--confirmations", default=DEFAULT_CONFIRMATIONS)
    confirm.set_defaults(func=cmd_confirm)

    review = sub.add_parser("review", help="更正后复核已确认场次：停止/既成消耗/保持")
    common(review)
    review.set_defaults(func=cmd_review)

    timeline = sub.add_parser("timeline", help="按 actions 时间线重放账户与方案变化")
    common(timeline)
    timeline.set_defaults(func=cmd_timeline)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
