"""馆方确认的持久化与签名。

确认文件使用 JSON Lines，默认放在 ``output/confirmations.jsonl``（已被
.gitignore 忽略）。签名只覆盖确认内容，用于发现确认后计划被改写。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable

from .plan import Confirmation
from .timeutil import parse_dt


def confirmation_signature(show_id: str, decision: str, works: Iterable[str]) -> str:
    payload = json.dumps(
        {"show_id": show_id, "decision": decision, "works": sorted(works)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def make_confirmation(
    show_id: str,
    decision: str,
    works: Iterable[str],
    at,
    note: str = "",
) -> Confirmation:
    works = tuple(sorted(works))
    if decision not in {"keep", "cancel"}:
        raise ValueError("确认只能是 keep 或 cancel")
    if decision == "cancel":
        works = ()
    return Confirmation(
        show_id=show_id,
        decision=decision,
        works=works,
        confirmed_at=at,
        note=note,
        signature=confirmation_signature(show_id, decision, works),
    )


def save_confirmations(path: str | Path, confirmations: Iterable[Confirmation], append: bool = True) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict] = []
    if append and path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                existing.append(json.loads(line))
    existing.extend(c.as_dict() for c in confirmations)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in existing) + "\n",
        encoding="utf-8",
    )
    return path


def load_confirmations(path: str | Path) -> list[Confirmation]:
    path = Path(path)
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        result.append(
            Confirmation(
                show_id=row["show_id"],
                decision=row["decision"],
                works=tuple(row.get("works") or ()),
                confirmed_at=parse_dt(row["confirmed_at"]) if row.get("confirmed_at") else None,
                note=row.get("note") or "",
                signature=row.get("signature") or "",
            )
        )
    return result


def latest_confirmations(confirmations: Iterable[Confirmation]) -> list[Confirmation]:
    """同一场次以最后一条确认为准，避免重复追加造成歧义。"""
    latest: dict[str, Confirmation] = {}
    for confirmation in confirmations:
        latest[confirmation.show_id] = confirmation
    return [latest[key] for key in sorted(latest)]
