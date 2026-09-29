"""贯通订单承诺、部件占用、产线排程、检验、运输、工程变更与回退的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import ManufacturingService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = ManufacturingService(
        connection, FrozenClock(datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc))
    )

    users = (
        ("sales", "销售", "sales"),
        ("coord", "生产协调员", "coordinator"),
        ("eng", "设计工程师", "engineer"),
        ("wh", "库房", "warehouse"),
        ("qa", "质量工程师", "quality"),
        ("audit", "审计", "auditor"),
    )
    for user_id, name, role in users:
        service.create_user(user_id, name, role)

    # 客户约束：只接受 A/B 等级批次，并明确批准铝绕组作为替代。
    service.create_customer_constraint("sales", {
        "customer_id": "cust-hn-grid",
        "approved_grades": ["A", "B"],
        "allowed_substitutes": ["winding-al"],
        "notes": "衡阳电网公司：替代件须事先书面认可",
    })

    # 部件目录（变压器 + 成套开关设备）。
    for part in (
        {"part_id": "core-110", "name": "110kV 铁芯", "family": "transformer", "unit": "件"},
        {"part_id": "winding-cu", "name": "铜绕组", "family": "transformer", "unit": "套"},
        {"part_id": "winding-al", "name": "铝绕组", "family": "transformer", "unit": "套"},
        {"part_id": "bushing-220", "name": "220kV 高压套管", "family": "transformer", "unit": "支"},
        {"part_id": "oltc-switch", "name": "有载分接开关", "family": "transformer", "unit": "台"},
        {"part_id": "breaker-vcb", "name": "真空断路器", "family": "switchgear", "unit": "台"},
        {"part_id": "cabinet-kyn", "name": "KYN28 柜体", "family": "switchgear", "unit": "面"},
        {"part_id": "busbar-cu", "name": "铜母线", "family": "switchgear", "unit": "组"},
    ):
        service.register_part("eng", part)

    service.register_substitution_rule("eng", {
        "rule_id": "sub-cu-al",
        "original_part_id": "winding-cu",
        "substitute_part_id": "winding-al",
        "minimum_grade": "B",
    })

    # 部件批次：铜绕组 b1 只有 2 套，刚好被第一个合同全部占用；
    # 铝绕组 b1 到货时尚未通过质量认证。
    service.register_batch("wh", {"batch_id": "core-b1", "part_id": "core-110", "quantity": 8, "grade": "A", "heat_number": "H-CORE-1", "certified": True, "received_on": "2026-09-15"})
    service.register_batch("wh", {"batch_id": "wcu-b1", "part_id": "winding-cu", "quantity": 2, "grade": "A", "heat_number": "H-CU-9", "certified": True, "received_on": "2026-09-16"})
    service.register_batch("wh", {"batch_id": "wal-b1", "part_id": "winding-al", "quantity": 4, "grade": "B", "heat_number": "H-AL-3", "certified": False, "received_on": "2026-09-17"})
    service.register_batch("wh", {"batch_id": "bush-b1", "part_id": "bushing-220", "quantity": 20, "grade": "A", "heat_number": "H-BSH-7", "certified": True, "received_on": "2026-09-15"})
    service.register_batch("wh", {"batch_id": "oltc-b1", "part_id": "oltc-switch", "quantity": 2, "grade": "A", "heat_number": "H-OLTC-2", "certified": True, "received_on": "2026-09-18"})
    for batch in (
        {"batch_id": "vcb-b1", "part_id": "breaker-vcb", "quantity": 4, "grade": "A", "heat_number": "H-VCB-1", "certified": True, "received_on": "2026-09-15"},
        {"batch_id": "vcb-b2", "part_id": "breaker-vcb", "quantity": 4, "grade": "B", "heat_number": "H-VCB-2", "certified": True, "received_on": "2026-09-16"},
        {"batch_id": "cab-b1", "part_id": "cabinet-kyn", "quantity": 6, "grade": "A", "heat_number": "H-CAB-1", "certified": True, "received_on": "2026-09-15"},
        {"batch_id": "bus-b1", "part_id": "busbar-cu", "quantity": 6, "grade": "A", "heat_number": "H-BUS-1", "certified": True, "received_on": "2026-09-15"},
    ):
        service.register_batch("wh", batch)

    # 产线能力。
    service.create_line("coord", {"line_id": "line-t1", "family": "transformer", "name": "变压器总装一线", "daily_capacity": 1})
    service.create_line("coord", {"line_id": "line-t2", "family": "transformer", "name": "变压器总装二线", "daily_capacity": 1})
    service.create_line("coord", {"line_id": "line-s1", "family": "switchgear", "name": "开关柜装配线", "daily_capacity": 2})

    # 检验关口。
    service.create_gate("qa", {"gate_id": "g-t-assembly", "family": "transformer", "name": "总装与绕组检验", "sequence": 1, "duration_days": 1})
    service.create_gate("qa", {"gate_id": "g-t-routine", "family": "transformer", "name": "出厂例行试验", "sequence": 2, "duration_days": 2})
    service.create_gate("qa", {"gate_id": "g-s-mechanical", "family": "switchgear", "name": "机械操作试验", "sequence": 1, "duration_days": 1})
    service.create_gate("qa", {"gate_id": "g-s-hipot", "family": "switchgear", "name": "绝缘耐压试验", "sequence": 2, "duration_days": 1})

    # 运输窗口。
    service.create_shipping_window("coord", {
        "window_id": "win-hn-1005",
        "destination": "衡阳装备基地成品堆场",
        "opens_on": "2026-10-05",
        "closes_on": "2026-10-12",
        "slots": 12,
    })

    transformer_bom = [
        {"part_id": "core-110", "quantity": 1},
        {"part_id": "winding-cu", "quantity": 1},
        {"part_id": "bushing-220", "quantity": 3},
    ]
    switchgear_bom = [
        {"part_id": "breaker-vcb", "quantity": 1},
        {"part_id": "cabinet-kyn", "quantity": 1},
        {"part_id": "busbar-cu", "quantity": 1},
    ]

    # 合同一：2 台变压器。
    service.submit_order("sales", {
        "order_id": "TRF-2601", "customer_id": "cust-hn-grid", "product_model": "SFZ-150000/110",
        "family": "transformer", "quantity": 2, "requested_date": "2026-10-09",
        "shipping_window_id": "win-hn-1005", "bom": transformer_bom, "idempotency_key": "order-trf-2601",
    })
    plan_a = service.plan_order("coord", "TRF-2601")
    service.confirm_order("coord", "TRF-2601", plan_a["revision"])

    # 成套开关设备合同同步交付。
    service.submit_order("sales", {
        "order_id": "SWG-3301", "customer_id": "cust-hn-grid", "product_model": "KYN28-12",
        "family": "switchgear", "quantity": 2, "requested_date": "2026-10-09",
        "shipping_window_id": "win-hn-1005", "bom": switchgear_bom, "idempotency_key": "order-swg-3301",
    })
    plan_s = service.plan_order("coord", "SWG-3301")
    service.confirm_order("coord", "SWG-3301", plan_s["revision"])

    # 合同二：同样需要铜绕组，关键批次已被合同一整体占用。
    service.submit_order("sales", {
        "order_id": "TRF-2602", "customer_id": "cust-hn-grid", "product_model": "SFZ-120000/110",
        "family": "transformer", "quantity": 2, "requested_date": "2026-10-09",
        "shipping_window_id": "win-hn-1005", "bom": transformer_bom, "idempotency_key": "order-trf-2602",
    })
    blocked = service.plan_order("coord", "TRF-2602")
    shortage = next(issue for issue in blocked["issues"] if issue["type"] == "component_shortage")
    # 质量认证前：替代件候选被规则拦下，计划不可承诺。
    candidate_before = shortage["substitute_candidates"][0]

    # 质量工程师完成铝绕组批次认证后重新排程，替代件通过三重核验进入候选。
    service.certify_batch("qa", "wal-b1")
    plan_b = service.plan_order("coord", "TRF-2602")
    service.confirm_order("coord", "TRF-2602", plan_b["revision"])
    substituted = [
        row for unit in plan_b["plan"]["units"]
        for row in plan_b["plan"]["reservations"]
        if row["unit_no"] == unit["unit_no"] and row["rule_id"] == "sub-cu-al"
    ]

    # 合同一第 1 台开工并完成第一道检验关口。
    service.start_unit("wh", "TRF-2601", 1)
    service.record_gate_result("qa", "TRF-2601", 1, "g-t-assembly", True, "绕组直流电阻合格")

    # 工程变更：第 1 台保持原型号继续生产，第 2 台改用带分接开关的新设计。
    service.propose_change("eng", {
        "change_id": "ECN-2026-018",
        "order_id": "TRF-2601",
        "new_product_model": "SFPZ-180000/220",
        "new_bom": [
            {"part_id": "core-110", "quantity": 1},
            {"part_id": "winding-al", "quantity": 1},
            {"part_id": "bushing-220", "quantity": 3},
            {"part_id": "oltc-switch", "quantity": 1},
        ],
        "reason": "客户新增调压要求，未开工部分切换设计",
    })
    evaluation = service.evaluate_change("coord", "ECN-2026-018")
    applied = service.apply_change("coord", "ECN-2026-018", evaluation["revision"])

    status = service.order_status("coord", "TRF-2601")
    rollback = service.rollback_plan("coord", "TRF-2601", 1)
    audit = service.audit_chain("audit")

    connection.close()
    return {
        "status": "ok",
        "workspace": workspace.name,
        "contract_a_revision": plan_a["revision"],
        "contract_b_blocked": {
            "feasible": blocked["feasible"],
            "issue_types": [issue["type"] for issue in blocked["issues"]],
            "substitute_before_certification": {
                "eligible": candidate_before["eligible"],
                "reasons": candidate_before["reasons"],
            },
        },
        "contract_b_confirmed_revision": plan_b["revision"],
        "substitute_sources": [
            {"unit_no": row["unit_no"], "batch_id": row["batch_id"], "rule_id": row["rule_id"], "quantity": row["quantity"]}
            for row in substituted
        ],
        "engineering_change": {
            "revision": applied["revision"],
            "locked_unit_numbers": applied["locked_unit_numbers"],
            "unit_1_model_after_change": status["units"][0]["product_model"],
            "unit_2_model_after_change": status["units"][1]["product_model"],
            "diff_summary": evaluation["diff"]["summary"],
            "units_changed": [
                {"unit_no": row["unit_no"], "fields": sorted(row["changes"])}
                for row in evaluation["diff"]["units_changed"]
            ],
        },
        "rollback": {
            "new_revision": rollback["new_revision"],
            "restored_from": rollback["restored_from"],
            "locked_unit_numbers": rollback["locked_unit_numbers"],
            "promised_ship_before": rollback["diff"]["summary"].get("promised_ship_on"),
            "product_model_before": rollback["diff"]["summary"].get("product_model"),
        },
        "audit": audit,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行输变电装备制造交付编排服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
