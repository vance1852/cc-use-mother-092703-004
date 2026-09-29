from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from manufacturing_delivery.clock import FrozenClock
from manufacturing_delivery.errors import Conflict, Forbidden, InvalidState
from manufacturing_delivery.service import ManufacturingService


MODEL = {
    "model_id": "SVT-110",
    "equipment_type": "transformer",
    "name": "110kV 变压器",
    "bom": {"core": 1, "bushing": 1},
    "preferred_components": {"core": "CORE-S90", "bushing": "BUSH-110"},
    "routing_hours": {"winding_shop": 40, "assembly": 30},
    "inspection_gates": ["routine_test", "type_test"],
    "accepts_substitutes": True,
}


def lot_payload(lot_id: str, model: str, category: str, grade: str = "A", qty: int = 4) -> dict:
    return {
        "lot_id": lot_id, "component_model": model, "category": category,
        "quality_grade": grade, "quantity": qty, "received_at": "2026-09-20T06:00:00Z",
    }


def capacity(station: str, day: str, hours: str = "80", line: str = "L1") -> dict:
    return {"line_id": line, "station": station, "service_date": day, "available_hours": hours}


def order_payload(order_id: str, *, substitutes: bool = False, key: str | None = None, due: str = "2026-10-20") -> dict:
    return {
        "order_id": order_id, "customer": "国网衡阳", "due_date": due, "window_id": "w-oct",
        "idempotency_key": key or f"{order_id}-key",
        "items": [{"model_id": "SVT-110", "quantity": 1, "allow_substitutes": substitutes,
                   "required_grades": {}}],
    }


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc))
        self.service = ManufacturingService(self.connection, self.clock)
        for user_id, role in (
            ("plan", "planner"), ("qa", "quality"),
            ("coord", "coordinator"), ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.create_model("plan", MODEL)
        self.service.add_lot("plan", lot_payload("lot-core", "CORE-S90", "core", qty=2))
        self.service.add_lot("plan", lot_payload("lot-bush", "BUSH-110", "bushing", qty=4))
        self.service.release_lot("qa", "lot-core", "合格")
        self.service.release_lot("qa", "lot-bush", "合格")
        for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
            self.service.add_capacity("plan", capacity("winding_shop", day))
            self.service.add_capacity("plan", capacity("assembly", day))
        self.service.add_window("plan", {
            "window_id": "w-oct", "destination": "衡阳枢纽",
            "opens_on": "2026-10-10", "closes_on": "2026-10-30", "capacity_units": 5,
        })

    def tearDown(self) -> None:
        self.connection.close()

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.add_lot("coord", lot_payload("x", "CORE-S90", "core"))
        with self.assertRaises(Forbidden):
            self.service.release_lot("coord", "lot-core")
        with self.assertRaises(Forbidden):
            self.service.confirm_order("plan", "whatever", 1)

    def test_quarantined_lot_cannot_confirm(self) -> None:
        self.service.add_lot("plan", lot_payload("lot-core-2", "CORE-S90", "core"))
        self.service.submit_order("coord", order_payload("o-x", key="o-x-key"))
        result = self.service.evaluate_order("coord", "o-x")
        # lot-core 有 2 件已放行，单台订单仍可行；改为占用隔离批次需耗尽已放行量。
        self.assertTrue(result["feasible"])
        self.service.submit_order("coord", order_payload("o-y", key="o-y-key"))
        self.service.confirm_order("coord", "o-x", 1)
        second = self.service.evaluate_order("coord", "o-y")
        self.assertTrue(second["feasible"])
        self.service.confirm_order("coord", "o-y", 1)
        self.service.submit_order("coord", order_payload("o-z", key="o-z-key"))
        third = self.service.evaluate_order("coord", "o-z")
        self.assertFalse(third["feasible"])
        self.assertIn("component_shortage", {c["code"] for c in third["conflicts"]})

    def test_confirm_holds_lots_capacity_and_window_atomically(self) -> None:
        self.service.submit_order("coord", order_payload("o-1"))
        self.service.confirm_order("coord", "o-1", 1)
        core = self.service.lot("lot-core")
        self.assertEqual(core["held_qty"], 1)
        window = self.service.window("w-oct")
        self.assertEqual(window["booked_units"], 1)
        booked = self.connection.execute(
            "SELECT COALESCE(sum(CAST(booked_hours AS REAL)),0) FROM line_capacities"
        ).fetchone()[0]
        self.assertEqual(booked, 70.0)

    def test_failed_confirmation_leaves_nothing_behind(self) -> None:
        # 第一张订单占用两台铁芯（批次总量仅 2）。
        first_payload = order_payload("o-a", key="o-a-key")
        first_payload["items"][0]["quantity"] = 2
        self.service.submit_order("coord", first_payload)
        self.service.confirm_order("coord", "o-a", 1)
        self.service.submit_order("coord", order_payload("o-b", key="o-b-key"))
        with self.assertRaises(InvalidState):
            self.service.confirm_order("coord", "o-b", 1)
        # 第二单没有留下任何半成品记录或窗口占用。
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM production_units WHERE order_id='o-b'").fetchone()[0],
            0,
        )
        self.assertEqual(self.service.window("w-oct")["booked_units"], 2)

    def test_idempotent_order_replay(self) -> None:
        payload = order_payload("o-1")
        first = self.service.submit_order("coord", payload)
        second = self.service.submit_order("coord", payload)
        self.assertEqual(first, second)
        changed = dict(payload, customer="其他客户")
        with self.assertRaises(Conflict):
            self.service.submit_order("coord", changed)

    def test_inspection_gates_must_be_passed_in_order(self) -> None:
        self.service.submit_order("coord", order_payload("o-1"))
        self.service.confirm_order("coord", "o-1", 1)
        unit_id = "o-1-U001"
        self.service.start_unit("coord", unit_id)
        self.service.complete_production("coord", unit_id)
        with self.assertRaises(InvalidState):
            self.service.record_inspection("qa", unit_id, "type_test", "passed")
        self.service.record_inspection("qa", unit_id, "routine_test", "passed")
        # 例行试验未全过时不能发运。
        with self.assertRaises(InvalidState):
            self.service.ship_unit("coord", "s-1", unit_id)
        self.service.record_inspection("qa", unit_id, "type_test", "passed")
        # 今天 09-29 不在窗口开放期内。
        with self.assertRaises(InvalidState):
            self.service.ship_unit("coord", "s-1", unit_id)
        self.clock.advance(days=15)
        shipped = self.service.ship_unit("coord", "s-1", unit_id)
        self.assertEqual(shipped["state"], "shipped")
        core = self.service.lot("lot-core")
        self.assertEqual(core["consumed_qty"], 1)
        self.assertEqual(core["held_qty"], 0)

    def test_failed_inspection_blocks_shipment(self) -> None:
        self.service.submit_order("coord", order_payload("o-1"))
        self.service.confirm_order("coord", "o-1", 1)
        unit_id = "o-1-U001"
        self.service.start_unit("coord", unit_id)
        self.service.complete_production("coord", unit_id)
        result = self.service.record_inspection("qa", unit_id, "routine_test", "failed", "局放超标")
        self.assertEqual(result["state"], "blocked")
        with self.assertRaises(InvalidState):
            self.clock.advance(days=15) or self.service.ship_unit("coord", "s-1", unit_id)

    def test_started_unit_is_locked_by_change(self) -> None:
        self.service.add_lot("plan", lot_payload("lot-bush-2", "BUSH-126", "bushing"))
        self.service.release_lot("qa", "lot-bush-2", "合格")
        self.service.add_capacity("plan", capacity("winding_shop", "2026-10-08"))
        self.service.add_capacity("plan", capacity("assembly", "2026-10-08"))
        self.service.submit_order("coord", order_payload("o-1"))
        self.service.confirm_order("coord", "o-1", 1)
        unit_id = "o-1-U001"
        self.service.start_unit("coord", unit_id)
        self.service.create_change("plan", {
            "change_id": "eco-1", "model_id": "SVT-110", "revision_label": "R2",
            "effective_on": "2026-10-01", "scope": "unstarted_only",
            "overrides": {"bushing": "BUSH-126"}, "reason": "升级",
        })
        applied = self.service.apply_change("coord", "eco-1", "o-1")
        self.assertFalse(applied["applied"])
        self.assertTrue(any(c["code"] == "started_unit_locked" for c in applied["conflicts"]))
        plan = self.service.order_plan("audit", "o-1")
        self.assertEqual(plan["units"][0]["design_revision"], "BASE")

    def test_change_reschedules_unstarted_unit_and_rollback_restores(self) -> None:
        self.service.add_lot("plan", lot_payload("lot-bush-2", "BUSH-126", "bushing"))
        self.service.release_lot("qa", "lot-bush-2", "合格")
        for day in ("2026-10-08", "2026-10-09"):
            self.service.add_capacity("plan", capacity("winding_shop", day))
            self.service.add_capacity("plan", capacity("assembly", day))
        # 两台：开工一台，另一台保持未开工。
        payload = order_payload("o-1")
        payload["items"][0]["quantity"] = 2
        self.service.submit_order("coord", payload)
        self.service.confirm_order("coord", "o-1", 1)
        self.service.start_unit("coord", "o-1-U001")
        self.service.create_change("plan", {
            "change_id": "eco-1", "model_id": "SVT-110", "revision_label": "R2",
            "effective_on": "2026-10-08", "scope": "unstarted_only",
            "overrides": {"bushing": "BUSH-126"}, "reason": "升级",
        })
        applied = self.service.apply_change("coord", "eco-1", "o-1")
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["rescheduled_units"], 1)
        self.assertEqual(applied["locked_units"][0]["unit_id"], "o-1-U001")
        plan = self.service.order_plan("audit", "o-1")
        u002 = next(u for u in plan["units"] if u["unit_id"] == "o-1-U002")
        self.assertEqual(u002["design_revision"], "R2")
        self.assertIn("BUSH-126", {c["component_model"] for c in u002["components"]})
        # 旧批次占用被释放，新批次被占用。
        self.assertEqual(self.service.lot("lot-bush")["held_qty"], 1)
        self.assertEqual(self.service.lot("lot-bush-2")["held_qty"], 1)
        rolled = self.service.rollback_change("coord", "eco-1", "o-1")
        self.assertTrue(rolled["rolled_back"])
        restored = self.service.order_plan("audit", "o-1")
        u002 = next(u for u in restored["units"] if u["unit_id"] == "o-1-U002")
        self.assertEqual(u002["design_revision"], "BASE")
        self.assertEqual(self.service.lot("lot-bush")["held_qty"], 2)
        self.assertEqual(self.service.lot("lot-bush-2")["held_qty"], 0)

    def test_rollback_refused_when_unit_started_after_change(self) -> None:
        self.service.add_lot("plan", lot_payload("lot-bush-2", "BUSH-126", "bushing"))
        self.service.release_lot("qa", "lot-bush-2", "合格")
        for day in ("2026-10-08", "2026-10-09"):
            self.service.add_capacity("plan", capacity("winding_shop", day))
            self.service.add_capacity("plan", capacity("assembly", day))
        self.service.submit_order("coord", order_payload("o-1"))
        self.service.confirm_order("coord", "o-1", 1)
        self.service.create_change("plan", {
            "change_id": "eco-1", "model_id": "SVT-110", "revision_label": "R2",
            "effective_on": "2026-10-08", "scope": "unstarted_only",
            "overrides": {"bushing": "BUSH-126"}, "reason": "升级",
        })
        self.service.apply_change("coord", "eco-1", "o-1")
        # 变更后设备按新版设计开工，回退必须被拒绝。
        self.service.start_unit("coord", "o-1-U001")
        with self.assertRaises(InvalidState):
            self.service.rollback_change("coord", "eco-1", "o-1")

    def test_rollback_refused_when_original_resources_are_repromised(self) -> None:
        # 新套管批次支持变更；旧批次 lot-bush 总量 4。
        self.service.add_lot("plan", lot_payload("lot-bush-2", "BUSH-126", "bushing"))
        self.service.release_lot("qa", "lot-bush-2", "合格")
        # 追加铁芯与容量，供抢占旧套管的第二张订单使用。
        self.service.add_lot("plan", lot_payload("lot-core-big", "CORE-S90", "core", qty=4))
        self.service.release_lot("qa", "lot-core-big", "合格")
        for day in ("2026-10-08", "2026-10-09"):
            self.service.add_capacity("plan", capacity("winding_shop", day))
            self.service.add_capacity("plan", capacity("assembly", day))
        self.service.submit_order("coord", order_payload("o-1"))
        self.service.confirm_order("coord", "o-1", 1)
        self.service.create_change("plan", {
            "change_id": "eco-1", "model_id": "SVT-110", "revision_label": "R2",
            "effective_on": "2026-10-08", "scope": "unstarted_only",
            "overrides": {"bushing": "BUSH-126"}, "reason": "升级",
        })
        self.service.apply_change("coord", "eco-1", "o-1")
        # 变更释放了 o-1 占用的 1 件 BUSH-110；另一张订单把 4 件旧批次全部重新承诺。
        grab = order_payload("o-grab", key="o-grab-key")
        grab["items"][0]["quantity"] = 4
        self.service.submit_order("coord", grab)
        self.service.confirm_order("coord", "o-grab", 1)
        self.assertEqual(self.service.lot("lot-bush")["held_qty"], 4)
        # 旧承诺资源已被重复承诺，回退必须在同一事务内被拒绝且不留半成品。
        with self.assertRaises(InvalidState):
            self.service.rollback_change("coord", "eco-1", "o-1")
        # 回退失败后 o-1 仍保持变更后的 R2 承诺与新套管占用。
        plan = self.service.order_plan("audit", "o-1")
        self.assertEqual(plan["units"][0]["design_revision"], "R2")
        self.assertEqual(self.service.lot("lot-bush-2")["held_qty"], 1)
        self.assertEqual(self.service.lot("lot-bush")["held_qty"], 4)

    def test_audit_chain(self) -> None:
        self.service.submit_order("coord", order_payload("o-1"))
        chain = self.service.audit_chain("audit")
        self.assertTrue(chain["valid"])
        self.assertGreater(chain["events"], 0)


if __name__ == "__main__":
    unittest.main()
