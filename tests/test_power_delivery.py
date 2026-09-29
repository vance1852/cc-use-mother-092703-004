from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from power_delivery.api import JsonApplication
from power_delivery.clock import FrozenClock
from power_delivery.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from power_delivery.planning import build_order_plan, diff_plans, grade_rank, plus_days
from power_delivery.service import ManufacturingService


CUSTOMER = {
    "customer_id": "cust-1",
    "approved_grades": ["A", "B"],
    "allowed_substitutes": ["part-al"],
    "notes": "",
}

TRANSFORMER_PARTS = [
    {"part_id": "part-cu", "name": "铜绕组", "family": "transformer", "unit": "套"},
    {"part_id": "part-al", "name": "铝绕组", "family": "transformer", "unit": "套"},
    {"part_id": "part-core", "name": "铁芯", "family": "transformer", "unit": "件"},
]


def _order_payload(order_id: str, *, quantity: int = 1, requested: str = "2026-10-20", key: str | None = None) -> dict:
    return {
        "order_id": order_id,
        "customer_id": "cust-1",
        "product_model": "SFZ-110",
        "family": "transformer",
        "quantity": quantity,
        "requested_date": requested,
        "shipping_window_id": "win-1",
        "bom": [{"part_id": "part-cu", "quantity": 1}, {"part_id": "part-core", "quantity": 1}],
        "idempotency_key": key or f"key-{order_id}",
    }


class PlanningFunctionTests(unittest.TestCase):
    def test_grade_rank_ordering_and_date_arithmetic(self) -> None:
        self.assertGreater(grade_rank("A"), grade_rank("B"))
        self.assertEqual(plus_days("2026-09-30", 5), "2026-10-05")

    def _base_kwargs(self) -> dict:
        return dict(
            order={
                "order_id": "o1", "customer_id": "cust-1", "family": "transformer",
                "quantity": 1, "requested_date": "2026-10-20", "window_id": "win-1",
            },
            product_model="SFZ-110",
            bom=[{"part_id": "part-cu", "quantity": 1}],
            as_of="2026-09-20",
            parts={
                "part-cu": {"part_id": "part-cu", "family": "transformer"},
                "part-al": {"part_id": "part-al", "family": "transformer"},
            },
            rules=[{"rule_id": "r1", "original_part_id": "part-cu", "substitute_part_id": "part-al",
                    "minimum_grade": "B", "active": 1}],
            constraint={"approved_grades": frozenset({"A", "B"}), "allowed_substitutes": frozenset({"part-al"})},
            busy={},
            busy_sources={},
            lines=[{"line_id": "l1", "family": "transformer", "daily_capacity": 1, "active": 1}],
            gates=[{"gate_id": "g1", "family": "transformer", "sequence": 1, "duration_days": 2, "active": 1}],
            window={"window_id": "win-1", "opens_on": "2026-10-01", "closes_on": "2026-10-30", "slots": 5},
            window_competitors=[],
        )

    def test_substitute_requires_customer_rule_and_quality(self) -> None:
        kwargs = self._base_kwargs()
        kwargs["batches"] = [
            {"batch_id": "al-b1", "part_id": "part-al", "grade": "B", "certified": True, "received_on": "2026-09-01"},
        ]
        kwargs["availability"] = {"al-b1": 1}
        result = build_order_plan(**kwargs)
        self.assertTrue(result["feasible"])
        self.assertEqual(result["plan"]["reservations"][0]["rule_id"], "r1")

    def test_substitute_rejected_when_customer_has_not_approved_part(self) -> None:
        kwargs = self._base_kwargs()
        kwargs["constraint"] = {"approved_grades": frozenset({"A", "B"}), "allowed_substitutes": frozenset()}
        kwargs["batches"] = [
            {"batch_id": "al-b1", "part_id": "part-al", "grade": "B", "certified": True, "received_on": "2026-09-01"},
        ]
        kwargs["availability"] = {"al-b1": 1}
        result = build_order_plan(**kwargs)
        self.assertFalse(result["feasible"])
        issue = next(item for item in result["issues"] if item["type"] == "component_shortage")
        self.assertIn("客户约束未接受", issue["substitute_candidates"][0]["reasons"][0])

    def test_substitute_rejected_for_low_grade_and_uncertified_batch(self) -> None:
        kwargs = self._base_kwargs()
        kwargs["batches"] = [
            {"batch_id": "al-c", "part_id": "part-al", "grade": "C", "certified": True, "received_on": "2026-09-01"},
            {"batch_id": "al-b2", "part_id": "part-al", "grade": "B", "certified": False, "received_on": "2026-09-02"},
        ]
        kwargs["availability"] = {"al-c": 1, "al-b2": 1}
        result = build_order_plan(**kwargs)
        self.assertFalse(result["feasible"])
        candidates = result["components"][0]["substitute_candidates"]
        reasons = {item["batch_id"]: item["reasons"] for item in candidates}
        self.assertTrue(any("低于替代规则要求" in text for text in reasons["al-c"]))
        self.assertTrue(any("未通过质量认证" in text for text in reasons["al-b2"]))

    def test_diff_detects_model_and_ship_date_changes(self) -> None:
        before = {
            "revision": 1, "product_model": "M1", "window_id": "w", "ready_on": "2026-10-10",
            "promised_ship_on": "2026-10-11", "requested_date": "2026-10-20",
            "units": [{"unit_no": 1, "product_model": "M1", "line_id": "l1", "production_date": "2026-09-20",
                       "ready_on": "2026-10-10", "ship_on": "2026-10-11", "window_id": "w", "locked": False}],
            "reservations": [],
        }
        after = dict(before, revision=2, product_model="M2", promised_ship_on="2026-10-15")
        after["units"] = [dict(before["units"][0], product_model="M2", ship_on="2026-10-15")]
        diff = diff_plans(before, after)
        self.assertEqual(diff["summary"]["product_model"], {"from": "M1", "to": "M2"})
        self.assertEqual(diff["summary"]["promised_ship_on"]["to"], "2026-10-15")


class ServiceFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc))
        self.service = ManufacturingService(self.connection, self.clock)
        for user_id, role in (
            ("sales", "sales"),
            ("coord", "coordinator"),
            ("eng", "engineer"),
            ("wh", "warehouse"),
            ("qa", "quality"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.create_customer_constraint("sales", CUSTOMER)
        for part in TRANSFORMER_PARTS:
            self.service.register_part("eng", part)
        self.service.register_substitution_rule("eng", {
            "rule_id": "r-cu-al", "original_part_id": "part-cu",
            "substitute_part_id": "part-al", "minimum_grade": "B",
        })
        self.service.create_line("coord", {"line_id": "l1", "family": "transformer", "name": "总装一线", "daily_capacity": 1})
        self.service.create_line("coord", {"line_id": "l2", "family": "transformer", "name": "总装二线", "daily_capacity": 1})
        self.service.create_gate("qa", {"gate_id": "g1", "family": "transformer", "name": "例行试验", "sequence": 1, "duration_days": 1})
        self.service.create_shipping_window("coord", {
            "window_id": "win-1", "destination": "衡阳", "opens_on": "2026-10-01",
            "closes_on": "2026-10-31", "slots": 10,
        })
        self.service.register_batch("wh", {"batch_id": "cu-b1", "part_id": "part-cu", "quantity": 2,
                                           "grade": "A", "heat_number": "H1", "certified": True, "received_on": "2026-09-01"})
        self.service.register_batch("wh", {"batch_id": "al-b1", "part_id": "part-al", "quantity": 2,
                                           "grade": "B", "heat_number": "H2", "certified": False, "received_on": "2026-09-02"})
        self.service.register_batch("wh", {"batch_id": "core-b1", "part_id": "part-core", "quantity": 5,
                                           "grade": "A", "heat_number": "H3", "certified": True, "received_on": "2026-09-01"})

    def tearDown(self) -> None:
        self.connection.close()

    def _plan_and_confirm(self, order_id: str, **kwargs) -> dict:
        self.service.submit_order("sales", _order_payload(order_id, **kwargs))
        plan = self.service.plan_order("coord", order_id)
        if plan["feasible"]:
            self.service.confirm_order("coord", order_id, plan["revision"])
        return plan

    def test_duplicate_component_promise_blocks_second_contract(self) -> None:
        first = self._plan_and_confirm("O-1", quantity=2)
        self.assertTrue(first["feasible"])
        second = self._plan_and_confirm("O-2")
        self.assertFalse(second["feasible"])
        shortage = next(item for item in second["issues"] if item["type"] == "component_shortage")
        self.assertEqual(shortage["requirement_part_id"], "part-cu")
        self.assertEqual(shortage["shortfall_quantity"], 1)
        self.assertEqual(self.service.batch("cu-b1")["available_quantity"], 0)

    def test_second_confirmation_rejected_after_concurrent_promise(self) -> None:
        # 两个合同在确认前各自排程都可行（计划阶段不预占）；
        # O-10 需要 2 台，将唯一铜绕组批次占尽，O-11 确认时事务内复核必须失败。
        self.service.submit_order("sales", _order_payload("O-10", quantity=2, key="k10"))
        self.service.submit_order("sales", _order_payload("O-11", key="k11"))
        plan_10 = self.service.plan_order("coord", "O-10")
        plan_11 = self.service.plan_order("coord", "O-11")
        self.assertTrue(plan_10["feasible"])
        self.assertTrue(plan_11["feasible"])
        self.service.confirm_order("coord", "O-10", plan_10["revision"])
        with self.assertRaises(Conflict):
            self.service.confirm_order("coord", "O-11", plan_11["revision"])
        # 被拒合同重新排程后应看到真实冲突。
        replanned = self.service.plan_order("coord", "O-11")
        self.assertFalse(replanned["feasible"])

    def test_infeasible_plan_cannot_be_confirmed(self) -> None:
        self._plan_and_confirm("O-1", quantity=2)
        self.service.submit_order("sales", _order_payload("O-2"))
        plan = self.service.plan_order("coord", "O-2")
        with self.assertRaises(InvalidState):
            self.service.confirm_order("coord", "O-2", plan["revision"])

    def test_certification_unlocks_substitute_and_confirmation_holds_it(self) -> None:
        self._plan_and_confirm("O-1", quantity=2)
        self.service.submit_order("sales", _order_payload("O-2"))
        blocked = self.service.plan_order("coord", "O-2")
        shortage = next(item for item in blocked["issues"] if item["type"] == "component_shortage")
        candidate = shortage["substitute_candidates"][0]
        self.assertFalse(candidate["eligible"])
        with self.assertRaises(Forbidden):
            self.service.certify_batch("wh", "al-b1")
        self.service.certify_batch("qa", "al-b1")
        plan = self.service.plan_order("coord", "O-2")
        self.assertTrue(plan["feasible"])
        reservation = next(row for row in plan["plan"]["reservations"] if row["part_id"] == "part-al")
        self.assertEqual(reservation["rule_id"], "r-cu-al")
        self.service.confirm_order("coord", "O-2", plan["revision"])
        self.assertEqual(self.service.batch("al-b1")["held_quantity"], 1)

    def test_engineering_change_locks_started_unit_and_reschedules_other(self) -> None:
        plan = self._plan_and_confirm("O-9", quantity=2)
        self.service.start_unit("wh", "O-9", 1)
        self.service.record_gate_result("qa", "O-9", 1, "g1", True, "合格")
        self.service.propose_change("eng", {
            "change_id": "ECN-1", "order_id": "O-9", "new_product_model": "SFZ-220",
            "new_bom": [{"part_id": "part-al", "quantity": 1}, {"part_id": "part-core", "quantity": 1}],
            "reason": "升级",
        })
        # 未认证的替代件使变更评估先失败，协调员不能强行应用。
        evaluation = self.service.evaluate_change("coord", "ECN-1")
        self.assertFalse(evaluation["feasible"])
        with self.assertRaises(InvalidState):
            self.service.apply_change("coord", "ECN-1", evaluation["revision"])
        self.service.certify_batch("qa", "al-b1")
        evaluation = self.service.evaluate_change("coord", "ECN-1")
        self.assertTrue(evaluation["feasible"])
        self.assertEqual(evaluation["locked_unit_numbers"], [1])
        applied = self.service.apply_change("coord", "ECN-1", evaluation["revision"])
        self.assertEqual(applied["locked_unit_numbers"], [1])
        status = self.service.order_status("coord", "O-9")
        self.assertEqual(status["units"][0]["state"], "awaiting_shipment")
        self.assertEqual(status["units"][0]["product_model"], "SFZ-110")
        self.assertEqual(status["units"][1]["state"], "planned")
        self.assertEqual(status["units"][1]["product_model"], "SFZ-220")
        # 已开工单元继续占用原铜绕组批次，未开工单元改用铝绕组。
        held: dict[str, int] = {}
        for row in self.connection.execute(
            "SELECT part_id,quantity FROM component_reservations WHERE order_id='O-9' AND state='held'"
        ).fetchall():
            held[row["part_id"]] = held.get(row["part_id"], 0) + row["quantity"]
        self.assertEqual(held, {"part-cu": 1, "part-al": 1, "part-core": 2})

    def test_normal_replan_refused_after_production_started(self) -> None:
        self._plan_and_confirm("O-3")
        self.service.start_unit("wh", "O-3", 1)
        with self.assertRaises(InvalidState):
            self.service.plan_order("coord", "O-3")

    def test_rollback_restores_prior_revision_and_reports_diff(self) -> None:
        plan = self._plan_and_confirm("O-4", quantity=2)
        self.service.start_unit("wh", "O-4", 1)
        self.service.certify_batch("qa", "al-b1")
        self.service.propose_change("eng", {
            "change_id": "ECN-2", "order_id": "O-4", "new_product_model": "SFZ-220",
            "new_bom": [{"part_id": "part-al", "quantity": 1}, {"part_id": "part-core", "quantity": 1}],
            "reason": "升级",
        })
        evaluation = self.service.evaluate_change("coord", "ECN-2")
        self.service.apply_change("coord", "ECN-2", evaluation["revision"])
        result = self.service.rollback_plan("coord", "O-4", 1)
        self.assertEqual(result["restored_from"], 1)
        self.assertEqual(result["locked_unit_numbers"], [1])
        self.assertEqual(result["diff"]["summary"]["product_model"], {"from": "SFZ-220", "to": "SFZ-110"})
        status = self.service.order_status("coord", "O-4")
        self.assertEqual(status["units"][0]["product_model"], "SFZ-110")
        self.assertEqual(status["units"][1]["product_model"], "SFZ-110")
        with self.assertRaises(ValidationFailed):
            self.service.rollback_plan("coord", "O-4", result["new_revision"])

    def test_window_slot_shortage_names_competing_orders(self) -> None:
        self.service.create_shipping_window("coord", {
            "window_id": "win-small", "destination": "衡阳", "opens_on": "2026-10-01",
            "closes_on": "2026-10-31", "slots": 1,
        })
        payload = _order_payload("O-5")
        payload["shipping_window_id"] = "win-small"
        self.service.submit_order("sales", payload)
        plan = self.service.plan_order("coord", "O-5")
        self.service.confirm_order("coord", "O-5", plan["revision"])
        payload2 = _order_payload("O-6")
        payload2["shipping_window_id"] = "win-small"
        self.service.submit_order("sales", payload2)
        plan2 = self.service.plan_order("coord", "O-6")
        issue = next(item for item in plan2["issues"] if item["type"] == "shipping_window_full")
        self.assertIn("O-5", issue["message"])

    def test_idempotent_order_submission(self) -> None:
        payload = _order_payload("O-7")
        first = self.service.submit_order("sales", payload)
        self.assertEqual(first, self.service.submit_order("sales", payload))
        changed = dict(payload, product_model="OTHER")
        with self.assertRaises(Conflict):
            self.service.submit_order("sales", changed)

    def test_audit_chain_detects_tampering(self) -> None:
        self._plan_and_confirm("O-8")
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE pd_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_api_boundary_health_and_errors(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        response = app.handle("GET", "/orders/UNKNOWN/status", {"X-Actor-Id": "coord"})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "not_found")
        forbidden = app.handle("POST", "/orders/O/units/start", {"X-Actor-Id": "audit"}, b'{"unit_no":1}')
        self.assertEqual(forbidden.status, 403)


if __name__ == "__main__":
    unittest.main()
