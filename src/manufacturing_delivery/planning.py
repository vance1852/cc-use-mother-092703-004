"""确定性的承诺核验、排程与工程变更差异计算。

规划层不访问数据库、不产生副作用：所有资源台账以普通字典传入，
同一输入必然得到同一计划，便于在事务前预检和在测试中断言。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Sequence


HOUR_QUANTUM = Decimal("0.01")


def quantize_hours(value: Decimal) -> Decimal:
    return value.quantize(HOUR_QUANTUM, rounding=ROUND_HALF_UP)


def hours_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def grade_rank(grade: str) -> int:
    """质量等级字母转序值：A 最好（1），Z 最差（26）。"""
    text = str(grade).strip().upper()
    if len(text) != 1 or not ("A" <= text <= "Z"):
        raise ValueError("质量等级必须是单个字母 A 到 Z")
    return ord(text) - ord("A") + 1


def conflict(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **extra}


@dataclass(frozen=True, slots=True)
class Ledger:
    """规划过程中的资源占用台账（本订单/本次重排的增量占用）。"""

    lot_held: dict[str, int]
    capacity_booked: dict[tuple[str, str, str], Decimal]
    window_booked: dict[str, int]

    @classmethod
    def empty(cls) -> "Ledger":
        return cls({}, {}, {})

    def lot_available(self, lot: Mapping[str, Any]) -> int:
        committed = int(lot["held_qty"]) + int(lot["consumed_qty"]) + self.lot_held.get(lot["lot_id"], 0)
        return int(lot["quantity"]) - committed

    def capacity_remaining(self, row: Mapping[str, Any]) -> Decimal:
        key = (row["line_id"], row["station"], row["service_date"])
        booked = Decimal(str(row["booked_hours"])) + self.capacity_booked.get(key, Decimal("0"))
        return Decimal(str(row["available_hours"])) - booked

    def window_remaining(self, window: Mapping[str, Any]) -> int:
        return int(window["capacity_units"]) - int(window["booked_units"]) - self.window_booked.get(
            window["window_id"], 0
        )

    def hold_lot(self, lot_id: str, quantity: int) -> None:
        self.lot_held[lot_id] = self.lot_held.get(lot_id, 0) + quantity

    def book_capacity(self, line_id: str, station: str, service_date: str, hours: Decimal) -> None:
        key = (line_id, station, service_date)
        self.capacity_booked[key] = self.capacity_booked.get(key, Decimal("0")) + hours

    def book_window(self, window_id: str, units: int) -> None:
        self.window_booked[window_id] = self.window_booked.get(window_id, 0) + units


def _released_lots(lots: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(
        (lot for lot in lots if lot["quality_state"] == "released"),
        key=lambda lot: (lot["received_at"], lot["lot_id"]),
    )


def _lot_grade_ok(
    lot: Mapping[str, Any],
    required_rank: int | None,
    rule_min_rank: int | None = None,
) -> bool:
    rank = int(lot["grade_rank"])
    if required_rank is not None and rank > required_rank:
        return False
    if rule_min_rank is not None and rank > rule_min_rank:
        return False
    return True


def _required_rank(required_grades: Mapping[str, str], category: str) -> int | None:
    grade = required_grades.get(category)
    return None if grade is None else grade_rank(grade)


def _choose_component(
    *,
    equipment_type: str,
    category: str,
    preferred_model: str,
    quantity: int,
    allow_substitutes: bool,
    required_rank: int | None,
    lots: Sequence[Mapping[str, Any]],
    rules: Sequence[Mapping[str, Any]],
    ledger: Ledger,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """返回选中的部件方案与阻断原因列表。

    首选批次不可得时，替代部件必须同时通过：
    1. 质量规则核验（存在启用规则、等级不低于规则下限）；
    2. 客户约束核验（型号允许替代、客户合同允许、规则允许客户放行）。
    """
    blockers: list[dict[str, Any]] = []

    def usable(lot: Mapping[str, Any], rule_min_rank: int | None) -> bool:
        return (
            lot["category"] == category
            and ledger.lot_available(lot) >= quantity
            and _lot_grade_ok(lot, required_rank, rule_min_rank)
        )

    # 1) 首选部件
    for lot in _released_lots(lots):
        if lot["component_model"] == preferred_model and usable(lot, None):
            return (
                {
                    "category": category,
                    "component_model": preferred_model,
                    "lot_id": lot["lot_id"],
                    "quantity": quantity,
                    "is_substitute": False,
                    "rule_id": None,
                    "grade": lot["quality_grade"],
                },
                blockers,
            )

    # 2) 替代部件候选：先找规则，再找客户/质量两道闸口。
    candidate_rules = sorted(
        (
            rule
            for rule in rules
            if rule["active"]
            and rule["equipment_type"] == equipment_type
            and rule["category"] == category
            and rule["preferred_model"] == preferred_model
        ),
        key=lambda rule: (int(rule["minimum_grade_rank"]), rule["rule_id"]),
    )
    substitute_lots = [
        lot
        for lot in _released_lots(lots)
        if lot["category"] == category
        and lot["component_model"] != preferred_model
        and ledger.lot_available(lot) >= quantity
    ]
    if not candidate_rules:
        blockers.append(conflict(
            "substitute_without_rule",
            f"部件类别 {category} 没有已启用的替代准入规则",
            category=category,
            preferred_model=preferred_model,
        ))
    if not allow_substitutes:
        blockers.append(conflict(
            "substitute_blocked_customer",
            f"客户合同约束不允许类别 {category} 使用替代部件",
            category=category,
        ))
    candidates: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for lot in substitute_lots:
        for rule in candidate_rules:
            if rule["substitute_model"] != lot["component_model"]:
                continue
            if not rule["allow_customer_override"]:
                blockers.append(conflict(
                    "substitute_blocked_quality",
                    f"质量规则 {rule['rule_id']} 禁止以客户放行替代 {preferred_model}",
                    category=category,
                    rule_id=rule["rule_id"],
                ))
                continue
            if not _lot_grade_ok(lot, required_rank, int(rule["minimum_grade_rank"])):
                blockers.append(conflict(
                    "substitute_grade_insufficient",
                    f"替代批次 {lot['lot_id']} 等级 {lot['quality_grade']} 低于规则 {rule['rule_id']} 下限",
                    category=category,
                    lot_id=lot["lot_id"],
                    rule_id=rule["rule_id"],
                ))
                continue
            if not allow_substitutes:
                continue
            candidates.append((lot, rule))
    if candidates:
        lot, rule = sorted(candidates, key=lambda pair: (pair[0]["received_at"], pair[0]["lot_id"]))[0]
        return (
            {
                "category": category,
                "component_model": lot["component_model"],
                "lot_id": lot["lot_id"],
                "quantity": quantity,
                "is_substitute": True,
                "rule_id": rule["rule_id"],
                "grade": lot["quality_grade"],
            },
            blockers,
        )
    blockers.insert(0, conflict(
        "component_shortage",
        f"部件类别 {category}（首选 {preferred_model}）没有满足客户与质量约束的可用批次",
        category=category,
        preferred_model=preferred_model,
        required_quantity=quantity,
    ))
    return None, blockers


def _schedule_stations(
    *,
    line_id_hint: str | None,
    routing: Mapping[str, Decimal],
    capacities: Mapping[str, Sequence[Mapping[str, Any]]],
    ledger: Ledger,
    earliest_on: str,
) -> tuple[list[dict[str, Any]], str | None, list[dict[str, Any]]]:
    """按工位顺序做确定性正向排程，返回工位占用、完成日期与冲突。"""
    schedule: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    cursor = earliest_on
    complete_on: str | None = None
    for seq, (station, raw_hours) in enumerate(routing.items(), start=1):
        hours = Decimal(str(raw_hours))
        chosen: Mapping[str, Any] | None = None
        for row in sorted(
            capacities.get(station, ()),
            key=lambda item: (item["service_date"], item["line_id"]),
        ):
            if row["service_date"] < cursor:
                continue
            if ledger.capacity_remaining(row) >= hours:
                chosen = row
                break
        if chosen is None:
            conflicts.append(conflict(
                "capacity_shortage",
                f"工位 {station} 在交期前没有 {hours_text(hours)} 工时可用",
                station=station,
                required_hours=hours_text(hours),
                not_before=cursor,
            ))
            continue
        ledger.book_capacity(chosen["line_id"], station, chosen["service_date"], hours)
        schedule.append(
            {
                "station": station,
                "seq_no": seq,
                "line_id": chosen["line_id"],
                "service_date": chosen["service_date"],
                "hours": hours_text(hours),
            }
        )
        cursor = chosen["service_date"]
        complete_on = chosen["service_date"]
    return schedule, complete_on, conflicts


def plan_unit(
    *,
    unit_seq: int,
    model: Mapping[str, Any],
    preferred_components: Mapping[str, str],
    allow_substitutes: bool,
    required_grades: Mapping[str, str],
    lots: Sequence[Mapping[str, Any]],
    rules: Sequence[Mapping[str, Any]],
    capacities: Mapping[str, Sequence[Mapping[str, Any]]],
    ledger: Ledger,
    earliest_on: str,
    due_date: str,
    design_revision: str,
) -> dict[str, Any]:
    """为单台设备给出部件方案与工位排程，并收集全部冲突来源。"""
    conflicts: list[dict[str, Any]] = []
    components: list[dict[str, Any]] = []
    for category, quantity in sorted(model["bom"].items()):
        preferred_model = preferred_components.get(category)
        if not preferred_model:
            conflicts.append(conflict(
                "bom_without_preferred",
                f"型号 {model['model_id']} 的部件类别 {category} 未指定首选部件",
                category=category,
            ))
            continue
        choice_data, blockers = _choose_component(
            equipment_type=model["equipment_type"],
            category=category,
            preferred_model=preferred_model,
            quantity=int(quantity),
            allow_substitutes=bool(model["accepts_substitutes"] and allow_substitutes),
            required_rank=_required_rank(required_grades, category),
            lots=lots,
            rules=rules,
            ledger=ledger,
        )
        if choice_data is None:
            conflicts.extend(blockers)
        else:
            ledger.hold_lot(choice_data["lot_id"], choice_data["quantity"])
            components.append(choice_data)

    schedule, complete_on, capacity_conflicts = _schedule_stations(
        line_id_hint=None,
        routing=model["routing_hours"],
        capacities=capacities,
        ledger=ledger,
        earliest_on=earliest_on,
    )
    conflicts.extend(capacity_conflicts)
    if complete_on is not None and complete_on > due_date:
        conflicts.append(conflict(
            "delivery_late",
            f"设备 {unit_seq} 最早完成日 {complete_on} 晚于客户交期 {due_date}",
            unit_seq=unit_seq,
            planned_complete_on=complete_on,
            due_date=due_date,
        ))
    return {
        "unit_seq": unit_seq,
        "model_id": model["model_id"],
        "design_revision": design_revision,
        "components": components,
        "schedule": schedule,
        "planned_complete_on": complete_on,
        "conflicts": conflicts,
    }


def plan_order(
    *,
    order: Mapping[str, Any],
    models: Mapping[str, Mapping[str, Any]],
    preferred_by_model: Mapping[str, Mapping[str, str]],
    lots: Sequence[Mapping[str, Any]],
    rules: Sequence[Mapping[str, Any]],
    capacities: Mapping[str, Sequence[Mapping[str, Any]]],
    windows: Mapping[str, Mapping[str, Any]],
    ledger: Ledger | None = None,
    earliest_on: str,
    design_revision_by_model: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """对整单做整体占用预检：部件批次、产线工时、运输窗口一次性核验。"""
    ledger = ledger or Ledger.empty()
    revisions = design_revision_by_model or {}
    window = windows[order["window_id"]]
    units: list[dict[str, Any]] = []
    total_units = 0
    seq = 0
    for item in order["items"]:
        model = models[item["model_id"]]
        for _ in range(int(item["quantity"])):
            seq += 1
            total_units += 1
            unit = plan_unit(
                unit_seq=seq,
                model=model,
                preferred_components=preferred_by_model[item["model_id"]],
                allow_substitutes=bool(item["allow_substitutes"]),
                required_grades=item["required_grades"],
                lots=lots,
                rules=rules,
                capacities=capacities,
                ledger=ledger,
                earliest_on=earliest_on,
                due_date=order["due_date"],
                design_revision=revisions.get(item["model_id"], "BASE"),
            )
            units.append(unit)
    window_conflicts: list[dict[str, Any]] = []
    if ledger.window_remaining(window) < total_units:
        window_conflicts.append(conflict(
            "window_capacity_exceeded",
            f"运输窗口 {window['window_id']} 剩余 {ledger.window_remaining(window)} 台，本单需要 {total_units} 台",
            window_id=window["window_id"],
            remaining=ledger.window_remaining(window),
            requested=total_units,
        ))
    for unit in units:
        complete_on = unit["planned_complete_on"]
        if complete_on is not None and complete_on > window["closes_on"]:
            window_conflicts.append(conflict(
                "window_closed_before_complete",
                f"设备 {unit['unit_seq']} 完成日 {complete_on} 晚于窗口关闭日 {window['closes_on']}",
                unit_seq=unit["unit_seq"],
                window_id=window["window_id"],
                closes_on=window["closes_on"],
            ))
    ledger.book_window(window["window_id"], total_units)
    all_conflicts = window_conflicts + [item for unit in units for item in unit["conflicts"]]
    completes = [unit["planned_complete_on"] for unit in units if unit["planned_complete_on"]]
    return {
        "order_id": order["order_id"],
        "window_id": window["window_id"],
        "units": units,
        "earliest_complete_on": max(completes) if completes else None,
        "meets_due_date": not any(item["code"] == "delivery_late" for item in all_conflicts),
        "window_ok": not window_conflicts,
        "feasible": not all_conflicts,
        "conflicts": all_conflicts,
    }


def commitment_view(unit: Mapping[str, Any]) -> dict[str, Any]:
    """把单台设备的承诺压平成可前后对比的视图。"""
    return {
        "unit_id": unit.get("unit_id"),
        "unit_seq": unit.get("unit_seq"),
        "model_id": unit["model_id"],
        "state": unit.get("state", "scheduled"),
        "design_revision": unit.get("design_revision"),
        "planned_complete_on": unit.get("planned_complete_on"),
        "components": {
            entry["category"]: {
                "component_model": entry["component_model"],
                "lot_id": entry.get("lot_id"),
                "is_substitute": bool(entry["is_substitute"]),
            }
            for entry in unit.get("components", [])
        },
        "schedule": [
            {"station": row["station"], "line_id": row["line_id"], "service_date": row["service_date"]}
            for row in unit.get("schedule", [])
        ],
    }


def diff_unit_commitments(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """逐台比对回退/重排前后的承诺差异。"""
    changes: list[dict[str, Any]] = []
    if before.get("design_revision") != after.get("design_revision"):
        changes.append({
            "field": "design_revision",
            "before": before.get("design_revision"),
            "after": after.get("design_revision"),
        })
    if before.get("planned_complete_on") != after.get("planned_complete_on"):
        changes.append({
            "field": "planned_complete_on",
            "before": before.get("planned_complete_on"),
            "after": after.get("planned_complete_on"),
        })
    categories = sorted(set(before.get("components", {})) | set(after.get("components", {})))
    for category in categories:
        old = before.get("components", {}).get(category)
        new = after.get("components", {}).get(category)
        if old != new:
            changes.append({"field": f"component.{category}", "before": old, "after": new})
    old_schedule = {row["station"]: row for row in before.get("schedule", [])}
    new_schedule = {row["station"]: row for row in after.get("schedule", [])}
    for station in sorted(set(old_schedule) | set(new_schedule)):
        if old_schedule.get(station) != new_schedule.get(station):
            changes.append({
                "field": f"schedule.{station}",
                "before": None if station not in old_schedule else {
                    "line_id": old_schedule[station]["line_id"],
                    "service_date": old_schedule[station]["service_date"],
                },
                "after": None if station not in new_schedule else {
                    "line_id": new_schedule[station]["line_id"],
                    "service_date": new_schedule[station]["service_date"],
                },
            })
    return {
        "unit_id": before.get("unit_id") or after.get("unit_id"),
        "unit_seq": before.get("unit_seq") or after.get("unit_seq"),
        "kind": after.get("kind", "rescheduled" if changes else "unchanged"),
        "changed": changes,
    }
