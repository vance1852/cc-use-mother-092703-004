"""确定性的制造交付排产与占用计算。

所有函数都是纯函数：输入仓储层读出的普通字典，输出可直接持久化的
计划快照 / 问题清单 / 版本差异。日期统一使用 ``YYYY-MM-DD`` 文本，
字典序与时间序一致，保证结果可复算、可回放。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Mapping, Sequence


# 质量等级序：A 高于 B 高于 C，替代规则给出最低可接受等级。
GRADE_RANK = {"A": 3, "B": 2, "C": 1}


def plus_days(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def grade_rank(grade: str) -> int | None:
    return GRADE_RANK.get(grade.upper())


def _issue(kind: str, blocking: bool, message: str, **extra: Any) -> dict[str, Any]:
    return {"type": kind, "blocking": blocking, "message": message, **extra}


def _sorted_lines(lines: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(lines, key=lambda item: str(item["line_id"]))


def _gates_for_family(gates: Sequence[Mapping[str, Any]], family: str) -> list[Mapping[str, Any]]:
    return sorted(
        (gate for gate in gates if gate["family"] == family),
        key=lambda item: (int(item["sequence"]), str(item["gate_id"])),
    )


def _gate_schedule(production_date: str, gates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    prefix = 0
    for gate in gates:
        duration = max(0, int(gate["duration_days"]))
        start_on = plus_days(production_date, 1 + prefix)
        end_on = start_on if duration == 0 else plus_days(start_on, duration - 1)
        rows.append({
            "gate_id": gate["gate_id"],
            "sequence": int(gate["sequence"]),
            "start_on": start_on,
            "end_on": end_on,
            "state": "pending",
        })
        prefix += duration
    return rows


def _ready_on(production_date: str, gates: Sequence[Mapping[str, Any]]) -> str:
    total_days = sum(max(0, int(gate["duration_days"])) for gate in gates)
    return plus_days(production_date, max(1, total_days))


def _evaluate_primary(batch: Mapping[str, Any], available: int, approved_grades: frozenset[str]) -> list[str]:
    reasons: list[str] = []
    if not bool(batch["certified"]):
        reasons.append("批次未通过质量认证")
    if str(batch["grade"]).upper() not in approved_grades:
        reasons.append(f"客户未批准质量等级 {batch['grade']}")
    if available <= 0:
        reasons.append("批次库存已被其他合同全部占用")
    return reasons


def _evaluate_substitute(
    *,
    batch: Mapping[str, Any],
    substitute_part: Mapping[str, Any] | None,
    original_family: str,
    minimum_grade: str,
    available: int,
    approved_grades: frozenset[str],
    allowed_substitutes: frozenset[str],
    substitute_part_id: str,
) -> list[str]:
    reasons: list[str] = []
    if substitute_part is None:
        reasons.append("替代部件不存在于部件目录")
        return reasons
    if substitute_part["family"] != original_family:
        reasons.append("替代部件与原件不属于同一产品族")
    batch_grade = str(batch["grade"]).upper()
    batch_rank = grade_rank(batch_grade)
    required_rank = grade_rank(minimum_grade)
    if batch_rank is None or required_rank is None:
        reasons.append(f"质量等级 {batch_grade} 无法按规则等级 {minimum_grade} 核验")
    elif batch_rank < required_rank:
        reasons.append(f"批次等级 {batch_grade} 低于替代规则要求的 {minimum_grade}")
    if batch_grade not in approved_grades:
        reasons.append(f"客户未批准质量等级 {batch_grade}")
    if substitute_part_id not in allowed_substitutes:
        reasons.append("客户约束未接受该替代部件")
    if not bool(batch["certified"]):
        reasons.append("批次未通过质量认证")
    if available <= 0:
        reasons.append("批次库存已被其他合同全部占用")
    return reasons


def _source_requirement(  # noqa: PLR0913
    *,
    requirement_part_id: str,
    need: int,
    family: str,
    parts: Mapping[str, Mapping[str, Any]],
    rules: Sequence[Mapping[str, Any]],
    constraint: Mapping[str, Any],
    batches: Sequence[Mapping[str, Any]],
    availability: Mapping[str, int],
) -> dict[str, Any]:
    """为单个 BOM 需求挑选批次：原件优先，缺口再由已核验替代件补齐。"""
    approved: frozenset[str] = constraint["approved_grades"]
    allowed: frozenset[str] = constraint["allowed_substitutes"]
    original = parts.get(requirement_part_id)

    picked: list[dict[str, Any]] = []
    batch_report: list[dict[str, Any]] = []
    substitute_report: list[dict[str, Any]] = []
    remaining = need

    primary_batches = sorted(
        (batch for batch in batches if batch["part_id"] == requirement_part_id),
        key=lambda item: (str(item["received_on"]), str(item["batch_id"])),
    )
    for batch in primary_batches:
        usable_qty = availability.get(batch["batch_id"], 0)
        reasons = _evaluate_primary(batch, usable_qty, approved)
        batch_report.append({
            "batch_id": batch["batch_id"],
            "part_id": batch["part_id"],
            "grade": batch["grade"],
            "certified": bool(batch["certified"]),
            "available_quantity": usable_qty,
            "usable": not reasons,
            "reasons": reasons,
        })
        if not reasons and remaining > 0:
            take = min(remaining, usable_qty)
            picked.append({
                "batch_id": batch["batch_id"],
                "part_id": batch["part_id"],
                "requirement_part_id": requirement_part_id,
                "rule_id": None,
                "quantity": take,
            })
            remaining -= take

    if remaining > 0:
        candidate_rules = sorted(
            (rule for rule in rules if rule["original_part_id"] == requirement_part_id and rule.get("active", 1)),
            key=lambda item: str(item["rule_id"]),
        )
        for rule in candidate_rules:
            substitute_part_id = rule["substitute_part_id"]
            substitute_part = parts.get(substitute_part_id)
            candidate_batches = sorted(
                (batch for batch in batches if batch["part_id"] == substitute_part_id),
                key=lambda item: str(item["batch_id"]),
            )
            for batch in candidate_batches:
                usable_qty = availability.get(batch["batch_id"], 0)
                reasons = _evaluate_substitute(
                    batch=batch,
                    substitute_part=substitute_part,
                    original_family=family if original is None else original["family"],
                    minimum_grade=rule["minimum_grade"],
                    available=usable_qty,
                    approved_grades=approved,
                    allowed_substitutes=allowed,
                    substitute_part_id=substitute_part_id,
                )
                entry = {
                    "rule_id": rule["rule_id"],
                    "batch_id": batch["batch_id"],
                    "part_id": substitute_part_id,
                    "grade": batch["grade"],
                    "certified": bool(batch["certified"]),
                    "available_quantity": usable_qty,
                    "eligible": not reasons,
                    "reasons": reasons,
                }
                substitute_report.append(entry)
                if not reasons and remaining > 0:
                    take = min(remaining, usable_qty)
                    picked.append({
                        "batch_id": batch["batch_id"],
                        "part_id": substitute_part_id,
                        "requirement_part_id": requirement_part_id,
                        "rule_id": rule["rule_id"],
                        "quantity": take,
                    })
                    remaining -= take

    return {
        "requirement_part_id": requirement_part_id,
        "required": need,
        "sources": picked,
        "shortfall": remaining,
        "primary_batches": batch_report,
        "substitute_candidates": substitute_report,
    }


def _split_sources_by_unit(
    sourced: Sequence[Mapping[str, Any]],
    unit_numbers: Sequence[int],
    bom_quantity: Mapping[str, int],
) -> list[dict[str, Any]]:
    reservations: list[dict[str, Any]] = []
    for row in sourced:
        requirement = row["requirement_part_id"]
        per_unit = bom_quantity[requirement]
        source_index = 0
        sources = list(row["sources"])
        source_left = [int(item["quantity"]) for item in sources]
        for unit_no in unit_numbers:
            need = per_unit
            while need > 0:
                if source_index >= len(sources):
                    raise ValueError("占用拆分时出现缺口")
                take = min(need, source_left[source_index])
                reservations.append({
                    "unit_no": unit_no,
                    "requirement_part_id": requirement,
                    "batch_id": sources[source_index]["batch_id"],
                    "part_id": sources[source_index]["part_id"],
                    "rule_id": sources[source_index]["rule_id"],
                    "quantity": take,
                })
                source_left[source_index] -= take
                need -= take
                if source_left[source_index] == 0:
                    source_index += 1
    return reservations


def _schedule_units(
    *,
    unit_numbers: Sequence[int],
    family: str,
    as_of: str,
    lines: Sequence[Mapping[str, Any]],
    busy: Mapping[tuple[str, str], int],
    window: Mapping[str, Any],
) -> tuple[dict[int, dict[str, str]], list[dict[str, Any]]]:
    """按日期、产线编号贪心放置，返回每台设备的产线/生产日期与拥塞证据。"""
    placements: dict[int, dict[str, str]] = {}
    issues: list[dict[str, Any]] = []
    horizon = str(window["closes_on"])
    day = as_of
    remaining = sorted(unit_numbers)
    family_lines = _sorted_lines([line for line in lines if line["family"] == family and line.get("active", 1)])
    while remaining and day <= horizon:
        for line in family_lines:
            free = int(line["daily_capacity"]) - busy.get((line["line_id"], day), 0)
            while free > 0 and remaining:
                unit_no = remaining.pop(0)
                placements[unit_no] = {"line_id": line["line_id"], "production_date": day}
                free -= 1
        day = plus_days(day, 1)
    if remaining:
        issues.append(_issue(
            "production_overload",
            True,
            f"产线能力在运输窗口 {window['window_id']} 关闭前无法完成 {len(remaining)} 台设备",
            unscheduled_units=remaining,
            window_id=window["window_id"],
            closes_on=horizon,
        ))
    return placements, issues


def _busy_evidence(
    *,
    lines: Sequence[Mapping[str, Any]],
    family: str,
    as_of: str,
    closes_on: str,
    busy_sources: Mapping[tuple[str, str], list[dict[str, str]]],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    line_ids = [line["line_id"] for line in _sorted_lines(lines) if line["family"] == family]
    day = as_of
    while day <= closes_on:
        for line_id in line_ids:
            sources = busy_sources.get((line_id, day))
            if sources:
                evidence.append({"line_id": line_id, "date": day, "occupied_by": sources})
        day = plus_days(day, 1)
    return evidence


def build_order_plan(  # noqa: PLR0913
    *,
    order: Mapping[str, Any],
    product_model: str,
    bom: Sequence[Mapping[str, Any]],
    as_of: str,
    parts: Mapping[str, Mapping[str, Any]],
    rules: Sequence[Mapping[str, Any]],
    constraint: Mapping[str, Any],
    batches: Sequence[Mapping[str, Any]],
    availability: Mapping[str, int],
    busy: Mapping[tuple[str, str], int],
    busy_sources: Mapping[tuple[str, str], list[dict[str, str]]],
    lines: Sequence[Mapping[str, Any]],
    gates: Sequence[Mapping[str, Any]],
    window: Mapping[str, Any],
    window_competitors: Sequence[str],
    fixed: Mapping[int, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """为整单（或工程变更后未开工部分）生成候选计划与问题清单。

    ``fixed`` 中的单元是已经开工、不得换型的设备，其排产与占用原样保留。
    """
    fixed = fixed or {}
    quantity = int(order["quantity"])
    family = order["family"]
    all_units = list(range(1, quantity + 1))
    new_units = [unit_no for unit_no in all_units if unit_no not in fixed]
    family_gates = _gates_for_family(gates, family)
    bom_quantity = {row["part_id"]: int(row["quantity"]) for row in bom}

    issues: list[dict[str, Any]] = []

    # 1. 部件占用：仅为未开工单元重新核验与占用。
    sourced_rows = []
    for part_id in sorted(bom_quantity):
        sourced = _source_requirement(
            requirement_part_id=part_id,
            need=bom_quantity[part_id] * len(new_units),
            family=family,
            parts=parts,
            rules=rules,
            constraint=constraint,
            batches=batches,
            availability=availability,
        )
        sourced_rows.append(sourced)
        if sourced["shortfall"] > 0:
            issue = _issue(
                "component_shortage",
                True,
                f"部件 {part_id} 缺口 {sourced['shortfall']} 件，候选批次与替代件均无法满足",
                requirement_part_id=part_id,
                shortfall_quantity=sourced["shortfall"],
            )
            issue["primary_batches"] = sourced["primary_batches"]
            issue["substitute_candidates"] = sourced["substitute_candidates"]
            issues.append(issue)

    reservations: list[dict[str, Any]] = []
    if new_units and all(row["shortfall"] == 0 for row in sourced_rows):
        reservations = _split_sources_by_unit(sourced_rows, new_units, bom_quantity)

    # 2. 产线排产。
    placements, schedule_issues = _schedule_units(
        unit_numbers=new_units,
        family=family,
        as_of=as_of,
        lines=lines,
        busy=busy,
        window=window,
    )
    issues.extend(schedule_issues)

    # 3. 检验关口与就绪/发运日期。
    units: list[dict[str, Any]] = []
    for unit_no in all_units:
        if unit_no in fixed:
            fixed_unit = fixed[unit_no]["unit"]
            unit_row = {
                "unit_no": unit_no,
                "product_model": fixed_unit["product_model"],
                "line_id": fixed_unit["line_id"],
                "production_date": fixed_unit["production_date"],
                "gates": fixed[unit_no]["gates"],
                "ready_on": fixed_unit["ready_on"],
                "ship_on": fixed_unit["ship_on"],
                "window_id": fixed_unit.get("window_id", order["window_id"]),
                "locked": True,
            }
        else:
            placement = placements.get(unit_no)
            production_date = placement["production_date"] if placement else None
            gate_rows = _gate_schedule(production_date, family_gates) if production_date else []
            ready_on = _ready_on(production_date, family_gates) if production_date else None
            ship_on = None
            if ready_on is not None:
                if ready_on < str(window["opens_on"]):
                    ship_on = str(window["opens_on"])
                else:
                    ship_on = ready_on
            unit_row = {
                "unit_no": unit_no,
                "product_model": product_model,
                "line_id": placement["line_id"] if placement else None,
                "production_date": production_date,
                "gates": gate_rows,
                "ready_on": ready_on,
                "ship_on": ship_on,
                "window_id": window["window_id"],
                "locked": False,
            }
        units.append(unit_row)

    ready_dates = [row["ready_on"] for row in units if row["ready_on"]]
    ship_dates = [row["ship_on"] for row in units if row["ship_on"]]
    ready_on = max(ready_dates) if ready_dates else None
    ship_on = max(ship_dates) if ship_dates else None

    for row in units:
        if row["ready_on"] is not None and row["ready_on"] > str(window["closes_on"]):
            issues.append(_issue(
                "ready_after_window_close",
                True,
                f"单元 {row['unit_no']} 最早检验完成日 {row['ready_on']} 晚于窗口关闭日 {window['closes_on']}",
                unit_no=row["unit_no"],
                ready_on=row["ready_on"],
                closes_on=window["closes_on"],
            ))
    requested_date = str(order["requested_date"])
    if ship_on is not None and ship_on > requested_date:
        issues.append(_issue(
            "delivery_after_requested",
            True,
            f"预计发运日 {ship_on} 晚于客户要求的 {requested_date}",
            promised_ship_on=ship_on,
            requested_date=requested_date,
        ))

    # 4. 运输窗口舱位（含已开工单元保留的舱位）。
    fixed_using_window = sum(
        1 for unit_no in fixed if fixed[unit_no]["unit"].get("window_id") == window["window_id"]
    )
    window_used_other = sum(int(item["quantity"]) for item in window_competitors)
    competitor_labels = [f"{item['order_id']}({item['quantity']}台)" for item in window_competitors]
    if window_used_other + fixed_using_window + len(new_units) > int(window["slots"]):
        issues.append(_issue(
            "shipping_window_full",
            True,
            f"运输窗口 {window['window_id']} 舱位不足，已占用订单：{'、'.join(competitor_labels) or '无'}",
            window_id=window["window_id"],
            slots=int(window["slots"]),
            used_by_other_orders=window_used_other,
            competing_orders=list(window_competitors),
        ))

    if schedule_issues:
        evidence = _busy_evidence(
            lines=lines,
            family=family,
            as_of=as_of,
            closes_on=str(window["closes_on"]),
            busy_sources=busy_sources,
        )
        if evidence:
            issues.append(_issue(
                "line_capacity_sources",
                False,
                "以下产线日期已被其他合同占用，是排产冲突的来源",
                occupied=evidence,
            ))

    # 已开工单元的既有占用并入快照。
    for unit_no, fixed_payload in fixed.items():
        reservations.extend(fixed_payload["reservations"])
    reservations.sort(key=lambda item: (int(item["unit_no"]), str(item["requirement_part_id"]), str(item["batch_id"])))

    plan = {
        "order_id": order["order_id"],
        "product_model": product_model,
        "family": family,
        "customer_id": order["customer_id"],
        "quantity": quantity,
        "window_id": window["window_id"],
        "requested_date": requested_date,
        "bom": [{"part_id": row["part_id"], "quantity": int(row["quantity"])} for row in sorted(bom, key=lambda item: item["part_id"])],
        "units": sorted(units, key=lambda item: int(item["unit_no"])),
        "reservations": reservations,
        "ready_on": ready_on,
        "promised_ship_on": ship_on,
    }
    return {"plan": plan, "issues": issues, "components": sourced_rows, "feasible": not any(item["blocking"] for item in issues)}


def restore_order_plan(  # noqa: PLR0913
    *,
    target: Mapping[str, Any],
    planned_unit_numbers: Sequence[int],
    fixed: Mapping[int, Mapping[str, Any]],
    as_of: str,
    parts: Mapping[str, Mapping[str, Any]],
    batches: Sequence[Mapping[str, Any]],
    availability: Mapping[str, int],
    busy: Mapping[tuple[str, str], int],
    busy_sources: Mapping[tuple[str, str], list[dict[str, str]]],
    lines: Sequence[Mapping[str, Any]],
    gates: Sequence[Mapping[str, Any]],
    window: Mapping[str, Any],
    window_competitors: Sequence[str],
) -> dict[str, Any]:
    """按历史修订原样恢复未开工单元的占用与排程，并在当前资源上重新核验。"""
    issues: list[dict[str, Any]] = []
    batches_by_id = {str(batch["batch_id"]): batch for batch in batches}
    gates_by_id = {str(gate["gate_id"]): gate for gate in gates}
    planned_set = set(planned_unit_numbers)

    restored_reservations: list[dict[str, Any]] = []
    needed_by_batch: dict[str, int] = {}
    for row in target["reservations"]:
        if int(row["unit_no"]) not in planned_set:
            continue
        restored_reservations.append(dict(row))
        needed_by_batch[row["batch_id"]] = needed_by_batch.get(row["batch_id"], 0) + int(row["quantity"])
    for batch_id, need in sorted(needed_by_batch.items()):
        batch = batches_by_id.get(batch_id)
        if batch is None:
            issues.append(_issue("component_shortage", True, f"历史批次 {batch_id} 已不存在", batch_id=batch_id))
            continue
        usable = availability.get(batch_id, 0)
        if usable < need:
            issues.append(_issue(
                "component_shortage",
                True,
                f"回退批次 {batch_id} 当前仅有 {usable} 件可用，恢复修订需要 {need} 件",
                batch_id=batch_id,
                available_quantity=usable,
                required_quantity=need,
            ))

    target_units = {int(row["unit_no"]): row for row in target["units"]}
    units: list[dict[str, Any]] = []
    family = target["family"]
    family_gates = _gates_for_family(gates, family)
    for unit_no in range(1, int(target["quantity"]) + 1):
        if unit_no in fixed:
            payload = fixed[unit_no]
            unit_row = {
                "unit_no": unit_no,
                "product_model": payload["unit"]["product_model"],
                "line_id": payload["unit"]["line_id"],
                "production_date": payload["unit"]["production_date"],
                "gates": payload["gates"],
                "ready_on": payload["unit"]["ready_on"],
                "ship_on": payload["unit"]["ship_on"],
                "window_id": payload["unit"].get("window_id", target["window_id"]),
                "locked": True,
            }
            units.append(unit_row)
            continue
        historical = target_units.get(unit_no)
        if historical is None:
            issues.append(_issue("restore_missing_unit", True, f"历史修订缺少单元 {unit_no}", unit_no=unit_no))
            continue
        line_id = historical["line_id"]
        production_date = historical["production_date"]
        line = next((item for item in lines if item["line_id"] == line_id), None)
        if line is None:
            issues.append(_issue(
                "production_line_missing",
                True,
                f"历史产线 {line_id} 已不存在，无法恢复单元 {unit_no} 的排程",
                unit_no=unit_no,
                line_id=line_id,
            ))
        else:
            used = busy.get((line_id, production_date), 0)
            if used >= int(line["daily_capacity"]):
                issues.append(_issue(
                    "production_overload",
                    True,
                    f"回退后单元 {unit_no} 在 {line_id} / {production_date} 的产线能力已被占用",
                    unit_no=unit_no,
                    line_id=line_id,
                    production_date=production_date,
                    occupied_by=busy_sources.get((line_id, production_date), []),
                ))
        gate_rows: list[dict[str, Any]] = []
        for gate in historical.get("gates", []):
            if gate["gate_id"] not in gates_by_id:
                issues.append(_issue(
                    "inspection_gate_missing",
                    True,
                    f"历史检验关口 {gate['gate_id']} 已不存在，无法恢复",
                    gate_id=gate["gate_id"],
                ))
                continue
            gate_rows.append({
                "gate_id": gate["gate_id"],
                "sequence": int(gate["sequence"]),
                "start_on": gate["start_on"],
                "end_on": gate["end_on"],
                "state": "pending",
            })
        if not gate_rows and family_gates:
            gate_rows = _gate_schedule(production_date, family_gates)
        ready_on = historical["ready_on"]
        ship_on = historical["ship_on"]
        if ship_on is not None and ship_on < str(window["opens_on"]):
            ship_on = str(window["opens_on"])
        if ready_on > str(window["closes_on"]):
            issues.append(_issue(
                "ready_after_window_close",
                True,
                f"单元 {unit_no} 历史就绪日 {ready_on} 晚于窗口关闭日 {window['closes_on']}",
                unit_no=unit_no,
            ))
        units.append({
            "unit_no": unit_no,
            "product_model": historical["product_model"],
            "line_id": line_id,
            "production_date": production_date,
            "gates": gate_rows,
            "ready_on": ready_on,
            "ship_on": ship_on,
            "window_id": window["window_id"],
            "locked": False,
        })

    for unit_no, payload in fixed.items():
        restored_reservations.extend(payload["reservations"])
    restored_reservations.sort(key=lambda item: (int(item["unit_no"]), str(item["requirement_part_id"]), str(item["batch_id"])))

    ready_dates = [row["ready_on"] for row in units if row["ready_on"]]
    ship_dates = [row["ship_on"] for row in units if row["ship_on"]]
    ready_on = max(ready_dates) if ready_dates else None
    ship_on = max(ship_dates) if ship_dates else None
    requested_date = str(target["requested_date"])
    if ship_on is not None and ship_on > requested_date:
        issues.append(_issue(
            "delivery_after_requested",
            True,
            f"回退后预计发运日 {ship_on} 晚于客户要求的 {requested_date}",
            promised_ship_on=ship_on,
            requested_date=requested_date,
        ))

    fixed_using_window = sum(
        1 for payload in fixed.values() if payload["unit"].get("window_id") == window["window_id"]
    )
    window_used_other = sum(int(item["quantity"]) for item in window_competitors)
    if window_used_other + fixed_using_window + len(planned_set) > int(window["slots"]):
        issues.append(_issue(
            "shipping_window_full",
            True,
            f"运输窗口 {window['window_id']} 舱位不足以恢复修订",
            used_by_other_orders=window_used_other,
            competing_orders=list(window_competitors),
        ))

    plan = {
        "order_id": target["order_id"],
        "product_model": target["product_model"],
        "family": family,
        "customer_id": target["customer_id"],
        "quantity": int(target["quantity"]),
        "window_id": window["window_id"],
        "requested_date": requested_date,
        "bom": [dict(row) for row in target["bom"]],
        "units": sorted(units, key=lambda item: int(item["unit_no"])),
        "reservations": restored_reservations,
        "ready_on": ready_on,
        "promised_ship_on": ship_on,
    }
    return {"plan": plan, "issues": issues, "feasible": not any(item["blocking"] for item in issues)}


def _reservation_multiset(plan: Mapping[str, Any]) -> dict[tuple[Any, ...], int]:
    counts: dict[tuple[Any, ...], int] = {}
    for row in plan["reservations"]:
        key = (
            int(row["unit_no"]),
            str(row["requirement_part_id"]),
            str(row["batch_id"]),
            str(row["part_id"]),
            "" if row.get("rule_id") is None else str(row["rule_id"]),
        )
        counts[key] = counts.get(key, 0) + int(row["quantity"])
    return counts


def diff_plans(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> dict[str, Any]:
    """比较两个计划修订，输出单元排程、部件占用与交期的结构化差异。"""
    if before is None:
        return {
            "from_revision": None,
            "to_revision": after.get("revision"),
            "created": True,
            "summary": {},
            "units_changed": [],
            "reservations_added": [
                {"unit_no": key[0], "requirement_part_id": key[1], "batch_id": key[2], "part_id": key[3], "rule_id": key[4] or None, "quantity": qty}
                for key, qty in sorted(_reservation_multiset(after).items())
            ],
            "reservations_removed": [],
        }

    before_units = {int(row["unit_no"]): row for row in before["units"]}
    after_units = {int(row["unit_no"]): row for row in after["units"]}
    tracked_fields = ("product_model", "line_id", "production_date", "ready_on", "ship_on", "window_id")
    units_changed: list[dict[str, Any]] = []
    for unit_no in sorted(set(before_units) | set(after_units)):
        old = before_units.get(unit_no)
        new = after_units.get(unit_no)
        fields: dict[str, dict[str, Any]] = {}
        for field_name in tracked_fields:
            old_value = None if old is None else old.get(field_name)
            new_value = None if new is None else new.get(field_name)
            if old_value != new_value:
                fields[field_name] = {"from": old_value, "to": new_value}
        old_locked = bool(old and old.get("locked"))
        new_locked = bool(new and new.get("locked"))
        if fields or old_locked != new_locked:
            fields["locked"] = {"from": old_locked, "to": new_locked}
            units_changed.append({"unit_no": unit_no, "changes": fields})

    before_counts = _reservation_multiset(before)
    after_counts = _reservation_multiset(after)
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for key in sorted(set(before_counts) | set(after_counts)):
        delta = after_counts.get(key, 0) - before_counts.get(key, 0)
        entry = {
            "unit_no": key[0],
            "requirement_part_id": key[1],
            "batch_id": key[2],
            "part_id": key[3],
            "rule_id": key[4] or None,
            "quantity": abs(delta),
        }
        if delta > 0:
            added.append(entry)
        elif delta < 0:
            removed.append(entry)

    summary_fields = ("product_model", "window_id", "ready_on", "promised_ship_on", "requested_date")
    summary = {}
    for field_name in summary_fields:
        old_value = before.get(field_name)
        new_value = after.get(field_name)
        if old_value != new_value:
            summary[field_name] = {"from": old_value, "to": new_value}

    return {
        "from_revision": before.get("revision"),
        "to_revision": after.get("revision"),
        "created": False,
        "summary": summary,
        "units_changed": units_changed,
        "reservations_added": added,
        "reservations_removed": removed,
    }
