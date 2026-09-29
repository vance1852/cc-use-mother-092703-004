"""贯通订单承诺、部件批次、产线能力、检验关口、运输窗口与工程变更的离线验收。

场景：衡阳输变电装备基地同时交付两台 110kV 变压器（合同 A）、两台成套
开关设备（合同 B）和一台追加变压器（合同 C）。关键铁芯批次被多个合同
重复承诺，协调员必须看到冲突来源；替代铁芯只有在客户约束与质量规则都
通过后才能进入候选；工程变更生效后已开工设备锁定原设计，未开工设备重新
排程，并可回退比对承诺差异。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import InvalidState
from .service import ManufacturingService


TRANSFORMER = {
    "model_id": "SVT-110",
    "equipment_type": "transformer",
    "name": "110kV 油浸式电力变压器",
    "bom": {"core": 1, "winding": 1, "bushing": 2, "tap_changer": 1},
    "preferred_components": {
        "core": "CORE-S90",
        "winding": "WIND-CU",
        "bushing": "BUSH-110",
        "tap_changer": "OLTC-A",
    },
    "routing_hours": {"winding_shop": 40, "assembly": 30, "hv_test": 10},
    "inspection_gates": ["routine_test", "type_test"],
    "accepts_substitutes": True,
}

SWITCHGEAR = {
    "model_id": "SWG-12K",
    "equipment_type": "switchgear",
    "name": "12kV 金属铠装成套开关设备",
    "bom": {"breaker": 1, "busbar": 2, "relay": 1, "enclosure": 1},
    "preferred_components": {
        "breaker": "VCB-12",
        "busbar": "BB-CU",
        "relay": "RELAY-12",
        "enclosure": "CAB-12",
    },
    "routing_hours": {"panel_shop": 20, "wiring": 24, "sw_test": 8},
    "inspection_gates": ["hipot", "protection_check"],
    "accepts_substitutes": True,
}


LOTS = [
    ("lot-core-1", "CORE-S90", "core", "A", 2),
    ("lot-wind-1", "WIND-CU", "winding", "A", 4),
    ("lot-bush-1", "BUSH-110", "bushing", "A", 8),
    ("lot-tap-1", "OLTC-A", "tap_changer", "A", 3),
    ("lot-vcb-1", "VCB-12", "breaker", "A", 2),
    ("lot-bb-1", "BB-CU", "busbar", "A", 6),
    ("lot-relay-1", "RELAY-12", "relay", "A", 2),
    ("lot-cab-1", "CAB-12", "enclosure", "A", 2),
    # 替代铁芯：质量等级 B，需规则放行。
    ("lot-core-alt", "CORE-S90B", "core", "B", 1),
    # 工程变更后使用的新型套管。
    ("lot-bush-2", "BUSH-126", "bushing", "A", 2),
]


def _lot(lot_id: str, model: str, category: str, grade: str, qty: int) -> dict[str, object]:
    return {
        "lot_id": lot_id,
        "component_model": model,
        "category": category,
        "quality_grade": grade,
        "quantity": qty,
        "received_at": "2026-09-20T06:00:00Z",
    }


def _capacity(line: str, station: str, day: str, hours: str) -> dict[str, object]:
    return {"line_id": line, "station": station, "service_date": day, "available_hours": hours}


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc))
    service = ManufacturingService(connection, clock)
    for user_id, role in (
        ("plan", "planner"),
        ("qa", "quality"),
        ("coord", "coordinator"),
        ("audit", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    # 主数据：型号、产线能力、运输窗口。
    service.create_model("plan", TRANSFORMER)
    service.create_model("plan", SWITCHGEAR)
    for day, hours in (
        ("2026-10-01", "80"), ("2026-10-02", "80"), ("2026-10-03", "80"),
        ("2026-10-08", "80"), ("2026-10-09", "80"), ("2026-10-10", "80"),
    ):
        service.add_capacity("plan", _capacity("L1", "winding_shop", day, hours))
    for day in ("2026-10-03", "2026-10-04", "2026-10-09", "2026-10-10", "2026-10-12"):
        service.add_capacity("plan", _capacity("L1", "assembly", day, "80"))
    for day in ("2026-10-05", "2026-10-10", "2026-10-12", "2026-10-15"):
        service.add_capacity("plan", _capacity("L1", "hv_test", day, "40"))
    for day in ("2026-10-01", "2026-10-08", "2026-10-15"):
        service.add_capacity("plan", _capacity("L2", "panel_shop", day, "80"))
        service.add_capacity("plan", _capacity("L2", "wiring", day, "80"))
        service.add_capacity("plan", _capacity("L2", "sw_test", day, "40"))
    service.add_window("plan", {
        "window_id": "w-oct",
        "destination": "衡阳东枢纽站",
        "opens_on": "2026-10-10",
        "closes_on": "2026-10-30",
        "capacity_units": 8,
    })

    # 部件批次：登记时隔离待检，质量放行后才能进入计划。
    for lot in LOTS:
        service.add_lot("plan", _lot(*lot))
    for lot_id, *_ in LOTS:
        service.release_lot("qa", lot_id, "入厂检验合格")
    service.add_substitute_rule("qa", {
        "rule_id": "rule-core-alt",
        "equipment_type": "transformer",
        "category": "core",
        "preferred_model": "CORE-S90",
        "substitute_model": "CORE-S90B",
        "allow_customer_override": True,
        "minimum_grade_rank": 2,
        "note": "S90B 铁芯仅在客户书面同意后代用",
    })

    # 合同 A：两台变压器；合同 B：两台成套开关设备。
    service.submit_order("coord", {
        "order_id": "order-a", "customer": "国网衡阳供电公司", "due_date": "2026-10-20",
        "window_id": "w-oct", "idempotency_key": "order-a-key",
        "items": [{"model_id": "SVT-110", "quantity": 2, "allow_substitutes": False,
                   "required_grades": {"core": "A"}}],
    })
    service.submit_order("coord", {
        "order_id": "order-b", "customer": "衡阳轨道交通集团", "due_date": "2026-10-28",
        "window_id": "w-oct", "idempotency_key": "order-b-key",
        "items": [{"model_id": "SWG-12K", "quantity": 2, "allow_substitutes": False,
                   "required_grades": {}}],
    })
    plan_a = service.evaluate_order("coord", "order-a")
    assert plan_a["feasible"], plan_a["conflicts"]
    confirmed_a = service.confirm_order("coord", "order-a", 1)
    service.confirm_order("coord", "order-b", 1)

    # 合同 C 追加一台变压器：铁芯批次已被合同 A 整体占用。
    service.submit_order("coord", {
        "order_id": "order-c", "customer": "衡阳工业园区", "due_date": "2026-10-20",
        "window_id": "w-oct", "idempotency_key": "order-c-key",
        "items": [{"model_id": "SVT-110", "quantity": 1, "allow_substitutes": False,
                   "required_grades": {"core": "A"}}],
    })
    blocked_evaluation = service.evaluate_order("coord", "order-c")
    assert not blocked_evaluation["feasible"]
    conflict_codes = {item["code"] for item in blocked_evaluation["conflicts"]}
    assert "component_shortage" in conflict_codes
    assert "substitute_blocked_customer" in conflict_codes
    # 整体占用原则：确认失败时不得留下任何部件或工时占用。
    try:
        service.confirm_order("coord", "order-c", 1)
    except InvalidState as exc:
        confirm_error = exc.details
    else:  # pragma: no cover - 验收必须走到冲突分支
        raise AssertionError("资源冲突的订单不应被确认")
    held_after_failure = connection.execute(
        "SELECT COALESCE(sum(held_qty),0) FROM component_lots WHERE lot_id='lot-core-alt'"
    ).fetchone()[0]
    assert held_after_failure == 0

    # 客户书面同意替代 + 质量规则满足后，以合同 C' 重新承诺，替代铁芯进入候选。
    service.submit_order("coord", {
        "order_id": "order-c2", "customer": "衡阳工业园区", "due_date": "2026-10-20",
        "window_id": "w-oct", "idempotency_key": "order-c2-key",
        "items": [{"model_id": "SVT-110", "quantity": 1, "allow_substitutes": True,
                   "required_grades": {"core": "B"}}],
    })
    eval_c2 = service.evaluate_order("coord", "order-c2")
    assert eval_c2["feasible"], eval_c2["conflicts"]
    core_choice = next(
        comp for unit in eval_c2["units"] for comp in unit["components"] if comp["category"] == "core"
    )
    assert core_choice["is_substitute"] and core_choice["component_model"] == "CORE-S90B"
    assert core_choice["rule_id"] == "rule-core-alt"
    service.confirm_order("coord", "order-c2", 1)

    # 工程变更：10-08 起未开工变压器改用 BUSH-126 套管（R2 版）。
    service.create_change("plan", {
        "change_id": "eco-bush-126",
        "model_id": "SVT-110",
        "revision_label": "R2",
        "effective_on": "2026-10-08",
        "scope": "unstarted_only",
        "overrides": {"bushing": "BUSH-126"},
        "reason": "防污闪升级，新工程统一采用加长型套管",
    })
    # A-U001 先开工；A-U002 尚未开工。
    service.start_unit("coord", "order-a-U001")
    change_a = service.apply_change("coord", "eco-bush-126", "order-a")
    assert change_a["applied"]
    assert len(change_a["locked_units"]) == 1
    assert change_a["locked_units"][0]["unit_id"] == "order-a-U001"
    assert change_a["rescheduled_units"] == 1
    changed_fields = {item["field"] for item in change_a["diff"][0]["changed"]}
    assert "component.bushing" in changed_fields
    assert any(item["field"] == "design_revision" for item in change_a["diff"][0]["changed"])
    started_unit = service.order_plan("audit", "order-a")
    u001 = next(u for u in started_unit["units"] if u["unit_id"] == "order-a-U001")
    u002 = next(u for u in started_unit["units"] if u["unit_id"] == "order-a-U002")
    assert u001["design_revision"] == "BASE"
    assert u002["design_revision"] == "R2"

    # 回退变更：恢复 A-U002 的原承诺，差异方向反转，交期同步还原。
    rollback = service.rollback_change("coord", "eco-bush-126", "order-a")
    assert rollback["rolled_back"]
    assert all(item["kind"] == "rolled_back" for item in rollback["diff"])
    restored = service.order_plan("audit", "order-a")
    u002_restored = next(u for u in restored["units"] if u["unit_id"] == "order-a-U002")
    assert u002_restored["design_revision"] == "BASE"
    assert {row["component_model"] for row in u002_restored["components"] if row["category"] == "bushing"} == {"BUSH-110"}

    # 检验关口：A-U001 完成总装后必须依次通过例行试验和型式试验才能发运。
    service.complete_production("coord", "order-a-U001")
    service.record_inspection("qa", "order-a-U001", "routine_test", "passed", "例行项目合格")
    service.record_inspection("qa", "order-a-U001", "type_test", "passed", "型式试验合格")
    clock.advance(days=13)  # 进入运输窗口开放期
    shipment = service.ship_unit("coord", "ship-001", "order-a-U001")

    audit = service.audit_chain("audit")
    assert audit["valid"]
    result = {
        "status": "ok",
        "confirmed_orders": [confirmed_a["order_id"], "order-b", "order-c2"],
        "rejected_order": "order-c",
        "rejected_conflict_codes": sorted(conflict_codes),
        "substitute_core": {"lot_id": core_choice["lot_id"], "rule_id": core_choice["rule_id"]},
        "change": {
            "locked": change_a["locked_units"],
            "rescheduled": change_a["rescheduled_units"],
            "affected_delivery": change_a["affected_delivery"],
            "diff": change_a["diff"],
        },
        "rollback_diff": rollback["diff"],
        "shipment": shipment,
        "conflicts_report": service.conflicts_report("audit"),
        "audit": audit,
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行制造交付编排服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
