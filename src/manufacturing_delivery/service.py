"""订单承诺、部件批次、产线能力、检验关口与运输窗口的统一事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    ComponentLot,
    CustomerOrder,
    EngineeringChange,
    EquipmentModel,
    LineCapacity,
    ShippingWindow,
    SubstituteRule,
)
from .planning import (
    Ledger,
    canonical_json,
    commitment_view,
    digest,
    diff_unit_commitments,
    plan_order,
    plan_unit,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {
        "model.write",
        "rule.write",
        "capacity.write",
        "window.write",
        "lot.write",
        "change.write",
        "report.read",
    },
    "quality": {"lot.release", "inspection.write", "rule.write", "report.read"},
    "coordinator": {
        "order.write",
        "plan.confirm",
        "production.write",
        "shipment.write",
        "change.apply",
        "report.read",
    },
    "auditor": {"audit.read", "report.read"},
}


class ManufacturingService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _today(self) -> str:
        return self.clock.now().date().isoformat()

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM mfg_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM mfg_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO mfg_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO mfg_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------- 主数据登记

    def create_model(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "model.write")
        model = EquipmentModel.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO equipment_models(model_id,equipment_type,name,preferred_components_json,"
                    "bom_json,routing_hours_json,gates_json,accepts_substitutes,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        model.model_id,
                        model.equipment_type,
                        model.name,
                        canonical_json(dict(model.preferred_components)),
                        canonical_json({k: v for k, v in sorted(model.bom.items())}),
                        canonical_json([[station, str(hours)] for station, hours in model.routing_hours.items()]),
                        canonical_json(list(model.inspection_gates)),
                        1 if model.accepts_substitutes else 0,
                        self._now(),
                    ),
                )
                self._audit("model", model.model_id, "model.created", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("装备型号已经存在") from exc
        return self.model(model.model_id)

    def model(self, model_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM equipment_models WHERE model_id=?", (model_id,)
        ).fetchone()
        if row is None:
            raise NotFound("装备型号不存在")
        return self._model_view(row)

    @staticmethod
    def _model_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "model_id": row["model_id"],
            "equipment_type": row["equipment_type"],
            "name": row["name"],
            "preferred_components": json.loads(row["preferred_components_json"]),
            "bom": {k: int(v) for k, v in json.loads(row["bom_json"]).items()},
            "routing_hours": {entry[0]: entry[1] for entry in json.loads(row["routing_hours_json"])},
            "inspection_gates": json.loads(row["gates_json"]),
            "accepts_substitutes": bool(row["accepts_substitutes"]),
            "revision": row["revision"],
        }

    def add_capacity(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "capacity.write")
        capacity = LineCapacity.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO line_capacities(line_id,station,service_date,available_hours,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        capacity.line_id,
                        capacity.station,
                        capacity.service_date,
                        str(capacity.available_hours),
                        self._now(),
                    ),
                )
                cap_id = int(cursor.lastrowid)
                self._audit("line_capacity", str(cap_id), "capacity.added", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("同一产线工位日期的能力记录已经存在") from exc
        return {
            "cap_id": cap_id,
            "line_id": capacity.line_id,
            "station": capacity.station,
            "service_date": capacity.service_date,
            "available_hours": str(capacity.available_hours),
        }

    def add_window(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "window.write")
        window = ShippingWindow.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO shipping_windows(window_id,destination,opens_on,closes_on,capacity_units,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        window.window_id,
                        window.destination,
                        window.opens_on,
                        window.closes_on,
                        window.capacity_units,
                        self._now(),
                    ),
                )
                self._audit("shipping_window", window.window_id, "window.opened", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("运输窗口编号已经存在") from exc
        return self.window(window.window_id)

    def window(self, window_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM shipping_windows WHERE window_id=?", (window_id,)
        ).fetchone()
        if row is None:
            raise NotFound("运输窗口不存在")
        return dict(row)

    # ------------------------------------------------------------- 部件与质量

    def add_lot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "lot.write")
        lot = ComponentLot.from_dict(raw)
        from .planning import grade_rank

        rank = grade_rank(lot.quality_grade)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO component_lots(lot_id,component_model,category,quality_grade,grade_rank,"
                    "quantity,received_at,quality_state,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        lot.lot_id,
                        lot.component_model,
                        lot.category,
                        lot.quality_grade,
                        rank,
                        lot.quantity,
                        lot.received_at,
                        "quarantined",
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("component_lot", lot.lot_id, "lot.received", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("部件批次编号已经存在") from exc
        return self.lot(lot.lot_id)

    def lot(self, lot_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM component_lots WHERE lot_id=?", (lot_id,)
        ).fetchone()
        if row is None:
            raise NotFound("部件批次不存在")
        return dict(row)

    def _set_lot_state(self, actor_id: str, lot_id: str, target: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "lot.release")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM component_lots WHERE lot_id=?", (lot_id,)
            ).fetchone()
            if row is None:
                raise NotFound("部件批次不存在")
            if row["quality_state"] != "quarantined":
                raise InvalidState(f"批次当前为 {row['quality_state']}，不能改为 {target}")
            self.connection.execute(
                "UPDATE component_lots SET quality_state=?,released_at=?,revision=revision+1 WHERE lot_id=?",
                (target, self._now() if target == "released" else None, lot_id),
            )
            self._audit(
                "component_lot", lot_id, f"lot.{target}", actor_id, {"note": note}
            )
        return self.lot(lot_id)

    def release_lot(self, actor_id: str, lot_id: str, note: str = "") -> dict[str, Any]:
        return self._set_lot_state(actor_id, lot_id, "released", note)

    def reject_lot(self, actor_id: str, lot_id: str, note: str = "") -> dict[str, Any]:
        return self._set_lot_state(actor_id, lot_id, "rejected", note)

    def add_substitute_rule(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "rule.write")
        rule = SubstituteRule.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO substitute_rules(rule_id,equipment_type,category,preferred_model,"
                    "substitute_model,allow_customer_override,minimum_grade_rank,note,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        rule.rule_id,
                        rule.equipment_type,
                        rule.category,
                        rule.preferred_model,
                        rule.substitute_model,
                        1 if rule.allow_customer_override else 0,
                        rule.minimum_grade_rank,
                        rule.note,
                        self._now(),
                    ),
                )
                self._audit("substitute_rule", rule.rule_id, "rule.created", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("替代准入规则已经存在") from exc
        return self.substitute_rule(rule.rule_id)

    def substitute_rule(self, rule_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM substitute_rules WHERE rule_id=?", (rule_id,)
        ).fetchone()
        if row is None:
            raise NotFound("替代准入规则不存在")
        return self._rule_view(row)

    @staticmethod
    def _rule_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "rule_id": row["rule_id"],
            "equipment_type": row["equipment_type"],
            "category": row["category"],
            "preferred_model": row["preferred_model"],
            "substitute_model": row["substitute_model"],
            "allow_customer_override": bool(row["allow_customer_override"]),
            "minimum_grade_rank": int(row["minimum_grade_rank"]),
            "note": row["note"],
            "active": bool(row["active"]),
        }

    # ------------------------------------------------------------------ 订单

    def submit_order(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "order.write")
        order = CustomerOrder.from_dict(raw)
        request_sha = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM mfg_idempotency WHERE scope='order' AND idempotency_key=?",
            (order.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_sha:
                raise Conflict("幂等键对应不同订单内容")
            return json.loads(stored["response_json"])
        window = self.window(order.window_id)
        if order.due_date > window["closes_on"]:
            raise ValidationFailed("订单交期晚于运输窗口关闭日")
        model_ids = {item.model_id for item in order.items}
        placeholders = ",".join("?" for _ in model_ids)
        found = {
            row["model_id"]
            for row in self.connection.execute(
                f"SELECT model_id FROM equipment_models WHERE model_id IN ({placeholders})",
                tuple(sorted(model_ids)),
            )
        }
        missing = model_ids - found
        if missing:
            raise ValidationFailed(f"装备型号不存在：{sorted(missing)}")
        response = {"order_id": order.order_id, "state": "submitted", "revision": 1}
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO orders(order_id,customer,due_date,window_id,idempotency_key,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        order.order_id,
                        order.customer,
                        order.due_date,
                        order.window_id,
                        order.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                for item in order.items:
                    self.connection.execute(
                        "INSERT INTO order_items(order_id,model_id,quantity,allow_substitutes,required_grades_json) "
                        "VALUES(?,?,?,?,?)",
                        (
                            order.order_id,
                            item.model_id,
                            item.quantity,
                            1 if item.allow_substitutes else 0,
                            canonical_json(dict(item.required_grades)),
                        ),
                    )
                self.connection.execute(
                    "INSERT INTO mfg_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('order',?,?,?,?)",
                    (order.idempotency_key, request_sha, canonical_json(response), self._now()),
                )
                self._audit("order", order.order_id, "order.submitted", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("订单编号或幂等键冲突") from exc
        return response

    def _order_row(self, order_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if row is None:
            raise NotFound("订单不存在")
        return row

    def _order_view(self, row: sqlite3.Row) -> dict[str, Any]:
        items = [
            {
                "model_id": item["model_id"],
                "quantity": int(item["quantity"]),
                "allow_substitutes": bool(item["allow_substitutes"]),
                "required_grades": json.loads(item["required_grades_json"]),
            }
            for item in self.connection.execute(
                "SELECT * FROM order_items WHERE order_id=? ORDER BY model_id", (row["order_id"],)
            )
        ]
        return {
            "order_id": row["order_id"],
            "customer": row["customer"],
            "due_date": row["due_date"],
            "window_id": row["window_id"],
            "items": items,
        }

    def _all_models(self) -> dict[str, dict[str, Any]]:
        return {
            row["model_id"]: self._model_view(row)
            for row in self.connection.execute("SELECT * FROM equipment_models")
        }

    def _all_lots(self, exclude_units: Sequence[str] = ()) -> list[dict[str, Any]]:
        lots = [dict(row) for row in self.connection.execute("SELECT * FROM component_lots")]
        if exclude_units:
            placeholders = ",".join("?" for _ in exclude_units)
            released_back = {
                row["lot_id"]: int(row["held"])
                for row in self.connection.execute(
                    f"SELECT lot_id,sum(quantity) held FROM unit_components "
                    f"WHERE state='held' AND unit_id IN ({placeholders}) GROUP BY lot_id",
                    tuple(exclude_units),
                )
            }
            for lot in lots:
                if lot["lot_id"] in released_back:
                    lot["held_qty"] = int(lot["held_qty"]) - released_back[lot["lot_id"]]
        return lots

    def _all_rules(self) -> list[dict[str, Any]]:
        return [self._rule_view(row) for row in self.connection.execute("SELECT * FROM substitute_rules")]

    def _capacities(
        self, exclude_units: Sequence[str] = ()
    ) -> dict[str, list[dict[str, Any]]]:
        rows = [dict(row) for row in self.connection.execute("SELECT * FROM line_capacities")]
        if exclude_units:
            placeholders = ",".join("?" for _ in exclude_units)
            released_back = {
                (row["line_id"], row["station"], row["service_date"]): Decimal(str(row["hours"]))
                for row in self.connection.execute(
                    f"SELECT line_id,station,service_date,sum(CAST(hours AS REAL)) hours "
                    f"FROM unit_schedule WHERE state='scheduled' AND unit_id IN ({placeholders}) "
                    f"GROUP BY line_id,station,service_date",
                    tuple(exclude_units),
                )
            }
            for row in rows:
                key = (row["line_id"], row["station"], row["service_date"])
                if key in released_back:
                    row["booked_hours"] = str(Decimal(str(row["booked_hours"])) - released_back[key])
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["station"], []).append(row)
        return grouped

    def _windows(self) -> dict[str, dict[str, Any]]:
        return {row["window_id"]: dict(row) for row in self.connection.execute("SELECT * FROM shipping_windows")}

    def _build_plan(self, order_row: sqlite3.Row, *, earliest_on: str | None = None) -> dict[str, Any]:
        models = self._all_models()
        order = self._order_view(order_row)
        preferred = {model_id: view["preferred_components"] for model_id, view in models.items()}
        return plan_order(
            order=order,
            models=models,
            preferred_by_model=preferred,
            lots=self._all_lots(),
            rules=self._all_rules(),
            capacities=self._capacities(),
            windows=self._windows(),
            earliest_on=earliest_on or self._today(),
        )

    def evaluate_order(self, actor_id: str, order_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        order_row = self._order_row(order_id)
        plan = self._build_plan(order_row)
        return {
            "order_id": order_id,
            "state": order_row["state"],
            "feasible": plan["feasible"],
            "meets_due_date": plan["meets_due_date"],
            "window_ok": plan["window_ok"],
            "earliest_complete_on": plan["earliest_complete_on"],
            "conflicts": plan["conflicts"],
            "units": [
                {
                    "unit_seq": unit["unit_seq"],
                    "model_id": unit["model_id"],
                    "planned_complete_on": unit["planned_complete_on"],
                    "components": unit["components"],
                    "schedule": unit["schedule"],
                }
                for unit in plan["units"]
            ],
        }

    def confirm_order(self, actor_id: str, order_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.confirm")
        order_row = self._order_row(order_id)
        if order_row["state"] != "submitted" or order_row["revision"] != expected_revision:
            raise InvalidState("订单不是可确认的当前版本")
        model_rows = {
            row["model_id"]: row
            for row in self.connection.execute("SELECT * FROM equipment_models")
        }
        with transaction(self.connection, immediate=True):
            # IMMEDIATE 事务内重算，保证整体占用不会与并发确认相互覆盖。
            plan = self._build_plan(order_row)
            if not plan["feasible"]:
                raise InvalidState("存在资源冲突，无法整体确认订单", plan["conflicts"])
            self.connection.execute(
                "UPDATE orders SET state='confirmed',revision=revision+1,confirmed_at=? "
                "WHERE order_id=? AND revision=?",
                (self._now(), order_id, expected_revision),
            )
            self.connection.execute(
                "INSERT INTO plan_snapshots(order_id,order_revision,plan_json,conflict_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    order_id,
                    expected_revision + 1,
                    canonical_json(plan),
                    canonical_json(plan["conflicts"]),
                    actor_id,
                    self._now(),
                ),
            )
            for unit in plan["units"]:
                unit_id = f"{order_id}-U{unit['unit_seq']:03d}"
                model_row = model_rows[unit["model_id"]]
                gates = json.loads(model_row["gates_json"])
                design_json = canonical_json({
                    "design_revision": unit["design_revision"],
                    "preferred_components": json.loads(model_row["preferred_components_json"]),
                })
                self.connection.execute(
                    "INSERT INTO production_units(unit_id,order_id,model_id,unit_seq,state,design_revision,"
                    "design_json,planned_complete_on,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        unit_id,
                        order_id,
                        unit["model_id"],
                        unit["unit_seq"],
                        "scheduled",
                        unit["design_revision"],
                        design_json,
                        unit["planned_complete_on"],
                        self._now(),
                    ),
                )
                for entry in unit["components"]:
                    self.connection.execute(
                        "INSERT INTO unit_components(unit_id,lot_id,category,component_model,quantity,"
                        "is_substitute,rule_id,state,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            unit_id,
                            entry["lot_id"],
                            entry["category"],
                            entry["component_model"],
                            entry["quantity"],
                            1 if entry["is_substitute"] else 0,
                            entry["rule_id"],
                            "held",
                            self._now(),
                        ),
                    )
                    self.connection.execute(
                        "UPDATE component_lots SET held_qty=held_qty+?,revision=revision+1 WHERE lot_id=?",
                        (entry["quantity"], entry["lot_id"]),
                    )
                for slot in unit["schedule"]:
                    self.connection.execute(
                        "INSERT INTO unit_schedule(unit_id,line_id,station,seq_no,service_date,hours,state) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (
                            unit_id,
                            slot["line_id"],
                            slot["station"],
                            slot["seq_no"],
                            slot["service_date"],
                            slot["hours"],
                            "scheduled",
                        ),
                    )
                    self.connection.execute(
                        "UPDATE line_capacities SET booked_hours=CAST(booked_hours AS REAL)+?,"
                        "revision=revision+1 WHERE line_id=? AND station=? AND service_date=?",
                        (float(slot["hours"]), slot["line_id"], slot["station"], slot["service_date"]),
                    )
                for seq_no, gate in enumerate(gates, start=1):
                    self.connection.execute(
                        "INSERT INTO inspection_records(unit_id,gate,seq_no,result,recorded_at) "
                        "VALUES(?,?,?,?,?)",
                        (unit_id, gate, seq_no, "pending", self._now()),
                    )
            total_units = len(plan["units"])
            self.connection.execute(
                "UPDATE shipping_windows SET booked_units=booked_units+?,revision=revision+1 WHERE window_id=?",
                (total_units, order_row["window_id"]),
            )
            self._audit("order", order_id, "order.confirmed", actor_id, {
                "units": total_units,
                "earliest_complete_on": plan["earliest_complete_on"],
                "substitutes": sum(1 for u in plan["units"] for c in u["components"] if c["is_substitute"]),
            })
        return {
            "order_id": order_id,
            "state": "confirmed",
            "revision": expected_revision + 1,
            "units": len(plan["units"]),
            "earliest_complete_on": plan["earliest_complete_on"],
        }

    def order_plan(self, actor_id: str, order_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        order_row = self._order_row(order_id)
        units = []
        for unit_row in self.connection.execute(
            "SELECT * FROM production_units WHERE order_id=? ORDER BY unit_seq", (order_id,)
        ):
            components = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM unit_components WHERE unit_id=? ORDER BY category", (unit_row["unit_id"],)
                )
            ]
            schedule = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM unit_schedule WHERE unit_id=? ORDER BY seq_no", (unit_row["unit_id"],)
                )
            ]
            inspections = [
                {"gate": row["gate"], "result": row["result"], "note": row["note"]}
                for row in self.connection.execute(
                    "SELECT * FROM inspection_records WHERE unit_id=? ORDER BY seq_no", (unit_row["unit_id"],)
                )
            ]
            units.append({
                "unit_id": unit_row["unit_id"],
                "unit_seq": unit_row["unit_seq"],
                "model_id": unit_row["model_id"],
                "state": unit_row["state"],
                "design_revision": unit_row["design_revision"],
                "planned_complete_on": unit_row["planned_complete_on"],
                "started_at": unit_row["started_at"],
                "components": components,
                "schedule": schedule,
                "inspections": inspections,
            })
        latest_snapshot = self.connection.execute(
            "SELECT * FROM plan_snapshots WHERE order_id=? ORDER BY confirmation_id DESC LIMIT 1",
            (order_id,),
        ).fetchone()
        return {
            "order": self._order_view(order_row),
            "state": order_row["state"],
            "revision": order_row["revision"],
            "units": units,
            "snapshot_conflicts": []
            if latest_snapshot is None
            else json.loads(latest_snapshot["conflict_json"]),
        }

    # ------------------------------------------------------------- 生产与检验

    def _unit_row(self, unit_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM production_units WHERE unit_id=?", (unit_id,)
        ).fetchone()
        if row is None:
            raise NotFound("生产单元不存在")
        return row

    def start_unit(self, actor_id: str, unit_id: str) -> dict[str, Any]:
        self._require(actor_id, "production.write")
        with transaction(self.connection, immediate=True):
            row = self._unit_row(unit_id)
            if row["state"] != "scheduled":
                raise InvalidState(f"设备当前状态为 {row['state']}，不能开工")
            held = self.connection.execute(
                "SELECT count(*) FROM unit_components WHERE unit_id=? AND state='held'",
                (unit_id,),
            ).fetchone()[0]
            required = len(self.model(row["model_id"])["bom"])
            if held < required:
                raise InvalidState("占用部件不完整，不能开工")
            self.connection.execute(
                "UPDATE production_units SET state='in_production',started_at=? WHERE unit_id=?",
                (self._now(), unit_id),
            )
            self._audit("unit", unit_id, "unit.started", actor_id, {
                "design_revision": row["design_revision"],
            })
        return {"unit_id": unit_id, "state": "in_production"}

    def complete_production(self, actor_id: str, unit_id: str) -> dict[str, Any]:
        self._require(actor_id, "production.write")
        with transaction(self.connection, immediate=True):
            row = self._unit_row(unit_id)
            if row["state"] != "in_production":
                raise InvalidState(f"设备当前状态为 {row['state']}，尚未在制")
            self.connection.execute(
                "UPDATE production_units SET state='awaiting_inspection',production_completed_at=? WHERE unit_id=?",
                (self._now(), unit_id),
            )
            self._audit("unit", unit_id, "unit.production_completed", actor_id, {})
        return {"unit_id": unit_id, "state": "awaiting_inspection"}

    def record_inspection(
        self, actor_id: str, unit_id: str, gate: str, result: str, note: str = ""
    ) -> dict[str, Any]:
        self._require(actor_id, "inspection.write")
        if result not in {"passed", "failed", "waived"}:
            raise ValidationFailed("检验结论必须是 passed、failed 或 waived")
        if result == "waived" and not note.strip():
            raise ValidationFailed("免检放行必须填写原因")
        with transaction(self.connection, immediate=True):
            unit = self._unit_row(unit_id)
            if unit["state"] not in {"awaiting_inspection", "blocked"}:
                raise InvalidState("设备尚未完成总装，不能登记检验结论")
            gates = self.model(unit["model_id"])["inspection_gates"]
            records = {
                row["gate"]: row
                for row in self.connection.execute(
                    "SELECT * FROM inspection_records WHERE unit_id=? ORDER BY seq_no", (unit_id,)
                )
            }
            if gate not in records:
                raise ValidationFailed(f"关口 {gate} 不在该型号检验序列中")
            next_gate = next(
                (name for name in gates if records[name]["result"] in {"pending", "failed"}),
                None,
            )
            if next_gate != gate:
                raise InvalidState(f"检验关口必须按顺序登记，下一个待检关口是 {next_gate}")
            self.connection.execute(
                "UPDATE inspection_records SET result=?,inspector_id=?,note=?,recorded_at=? "
                "WHERE unit_id=? AND gate=?",
                (result, actor_id, note.strip(), self._now(), unit_id, gate),
            )
            if result == "failed":
                self.connection.execute(
                    "UPDATE production_units SET state='blocked' WHERE unit_id=?", (unit_id,)
                )
                new_state = "blocked"
            else:
                remaining = [
                    name
                    for name in gates
                    if self.connection.execute(
                        "SELECT result FROM inspection_records WHERE unit_id=? AND gate=?",
                        (unit_id, name),
                    ).fetchone()["result"]
                    in {"pending", "failed"}
                ]
                if remaining:
                    new_state = "awaiting_inspection"
                    self.connection.execute(
                        "UPDATE production_units SET state='awaiting_inspection' WHERE unit_id=?",
                        (unit_id,),
                    )
                else:
                    new_state = "inspected"
                    self.connection.execute(
                        "UPDATE production_units SET state='inspected',inspected_at=? WHERE unit_id=?",
                        (self._now(), unit_id),
                    )
                    # 全部关口放行后，占用部件正式转为消耗。
                    self.connection.execute(
                        "UPDATE unit_components SET state='consumed' WHERE unit_id=? AND state='held'",
                        (unit_id,),
                    )
                    rows = self.connection.execute(
                        "SELECT lot_id,sum(quantity) total FROM unit_components "
                        "WHERE unit_id=? GROUP BY lot_id",
                        (unit_id,),
                    ).fetchall()
                    for item in rows:
                        self.connection.execute(
                            "UPDATE component_lots SET held_qty=held_qty-?,consumed_qty=consumed_qty+?,"
                            "revision=revision+1 WHERE lot_id=?",
                            (item["total"], item["total"], item["lot_id"]),
                        )
            self._audit("unit", unit_id, "inspection.recorded", actor_id, {
                "gate": gate, "result": result, "note": note.strip(),
            })
        return {"unit_id": unit_id, "gate": gate, "result": result, "state": new_state}

    def ship_unit(self, actor_id: str, shipment_id: str, unit_id: str) -> dict[str, Any]:
        self._require(actor_id, "shipment.write")
        with transaction(self.connection, immediate=True):
            unit = self._unit_row(unit_id)
            if unit["state"] != "inspected":
                raise InvalidState("设备未通过全部检验关口，不能发运")
            window = self.window(self._order_row(unit["order_id"])["window_id"])
            today = self._today()
            if window["state"] != "open":
                raise InvalidState(f"运输窗口当前状态为 {window['state']}")
            if not window["opens_on"] <= today <= window["closes_on"]:
                raise InvalidState("今天不在运输窗口开放期内")
            try:
                self.connection.execute(
                    "INSERT INTO shipments(shipment_id,unit_id,window_id,shipped_at,created_by) "
                    "VALUES(?,?,?,?,?)",
                    (shipment_id, unit_id, window["window_id"], self._now(), actor_id),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("发运单编号冲突或设备已发运") from exc
            self.connection.execute(
                "UPDATE production_units SET state='shipped' WHERE unit_id=?", (unit_id,)
            )
            self._audit("unit", unit_id, "unit.shipped", actor_id, {
                "shipment_id": shipment_id, "window_id": window["window_id"],
            })
        return {"shipment_id": shipment_id, "unit_id": unit_id, "state": "shipped"}

    # ------------------------------------------------------------- 工程变更

    def create_change(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "change.write")
        change = EngineeringChange.from_dict(raw)
        self.model(change.model_id)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO engineering_changes(change_id,model_id,revision_label,effective_on,scope,"
                    "overrides_json,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        change.change_id,
                        change.model_id,
                        change.revision_label,
                        change.effective_on,
                        change.scope,
                        canonical_json(dict(change.overrides)),
                        change.reason,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("change", change.change_id, "change.created", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("工程变更编号已经存在") from exc
        return {"change_id": change.change_id, "state": "active"}

    def _unit_commitment(self, unit_row: sqlite3.Row) -> dict[str, Any]:
        components = [
            {
                "category": row["category"],
                "component_model": row["component_model"],
                "lot_id": row["lot_id"],
                "quantity": int(row["quantity"]),
                "is_substitute": bool(row["is_substitute"]),
                "rule_id": row["rule_id"],
            }
            for row in self.connection.execute(
                "SELECT * FROM unit_components WHERE unit_id=? AND state='held' ORDER BY category",
                (unit_row["unit_id"],),
            )
        ]
        schedule = [
            {
                "station": row["station"],
                "seq_no": row["seq_no"],
                "line_id": row["line_id"],
                "service_date": row["service_date"],
                "hours": row["hours"],
            }
            for row in self.connection.execute(
                "SELECT * FROM unit_schedule WHERE unit_id=? AND state='scheduled' ORDER BY seq_no",
                (unit_row["unit_id"],)
            )
        ]
        return {
            "unit_id": unit_row["unit_id"],
            "unit_seq": unit_row["unit_seq"],
            "model_id": unit_row["model_id"],
            "state": unit_row["state"],
            "design_revision": unit_row["design_revision"],
            "planned_complete_on": unit_row["planned_complete_on"],
            "components": components,
            "schedule": schedule,
        }

    @staticmethod
    def _revised_preferred(model_view: dict[str, Any], overrides: Mapping[str, str]) -> dict[str, str]:
        preferred = dict(model_view["preferred_components"])
        preferred.update({k: v for k, v in overrides.items() if k in preferred})
        return preferred

    def apply_change(self, actor_id: str, change_id: str, order_id: str) -> dict[str, Any]:
        self._require(actor_id, "change.apply")
        change_row = self._change_row(change_id)
        order_row = self._order_row(order_id)
        overrides = json.loads(change_row["overrides_json"])
        prior = self.connection.execute(
            "SELECT * FROM change_evaluations WHERE change_id=? AND order_id=?",
            (change_id, order_id),
        ).fetchone()
        if prior is not None and prior["applied"] and not prior["undone"]:
            raise InvalidState("变更已应用，请先回退或创建新变更")
        unit_rows = list(self.connection.execute(
            "SELECT * FROM production_units WHERE order_id=? AND model_id=? ORDER BY unit_seq",
            (order_id, change_row["model_id"]),
        ))
        if not unit_rows:
            raise NotFound("订单中没有该型号的设备")
        unstarted = [row for row in unit_rows if row["state"] == "scheduled" and row["started_at"] is None]
        unstarted_ids = {row["unit_id"] for row in unstarted}
        started = [row for row in unit_rows if row["unit_id"] not in unstarted_ids]

        locked_units: list[dict[str, Any]] = []
        if change_row["scope"] != "none" and overrides:
            for row in started:
                locked_units.append({
                    "code": "started_unit_locked",
                    "message": f"设备 {row['unit_id']} 已开工，维持设计 {row['design_revision']}，不得静默换型",
                    "unit_id": row["unit_id"],
                    "state": row["state"],
                })

        before_units = [self._unit_commitment(row) for row in unstarted]
        conflicts: list[dict[str, Any]] = []
        after_units: list[dict[str, Any]] = []
        if change_row["scope"] == "none":
            conflicts.append({
                "code": "change_out_of_scope",
                "message": "变更声明不影响在制设备（scope=none）",
            })
        elif unstarted:
            model_view = self.model(change_row["model_id"])
            revised_model = dict(model_view)
            revised_model["preferred_components"] = self._revised_preferred(model_view, overrides)
            order_view = self._order_view(order_row)
            grade_by_item = {
                item["model_id"]: item["required_grades"] for item in order_view["items"]
            }
            allow_by_item = {
                item["model_id"]: item["allow_substitutes"] for item in order_view["items"]
            }
            unit_ids = [row["unit_id"] for row in unstarted]
            lots = self._all_lots(exclude_units=unit_ids)
            capacities = self._capacities(exclude_units=unit_ids)
            ledger = Ledger.empty()
            for row in unstarted:
                planned = plan_unit(
                    unit_seq=row["unit_seq"],
                    model=revised_model,
                    preferred_components=revised_model["preferred_components"],
                    allow_substitutes=bool(
                        revised_model["accepts_substitutes"] and allow_by_item.get(row["model_id"], False)
                    ),
                    required_grades=grade_by_item.get(row["model_id"], {}),
                    lots=lots,
                    rules=self._all_rules(),
                    capacities=capacities,
                    ledger=ledger,
                    earliest_on=max(self._today(), change_row["effective_on"]),
                    due_date=order_row["due_date"],
                    design_revision=change_row["revision_label"],
                )
                conflicts.extend(planned["conflicts"])
                after_units.append({
                    "unit_id": row["unit_id"],
                    "unit_seq": row["unit_seq"],
                    "model_id": row["model_id"],
                    "state": "scheduled",
                    "design_revision": change_row["revision_label"],
                    "planned_complete_on": planned["planned_complete_on"],
                    "components": planned["components"],
                    "schedule": planned["schedule"],
                })

        # scope=all 时任何已开工设备的锁定都是阻断项；
        # unstarted_only 下若所有设备均已开工，变更没有可作用对象，同样阻断。
        blocking = list(conflicts)
        if change_row["scope"] == "all":
            blocking.extend(locked_units)
        elif not unstarted and started:
            blocking.extend(locked_units)
        applied = not blocking

        diffs = []
        if unstarted:
            after_by_seq = {item["unit_seq"]: item for item in after_units}
            for before in before_units:
                after = after_by_seq[before["unit_seq"]]
                diffs.append(diff_unit_commitments(
                    commitment_view(before),
                    {**commitment_view(after), "kind": "rescheduled"},
                ))

        affected = [
            {
                "unit_id": item["unit_id"],
                "before_complete_on": before_units[i]["planned_complete_on"],
                "after_complete_on": item["planned_complete_on"],
            }
            for i, item in enumerate(after_units)
        ]

        with transaction(self.connection, immediate=True):
            if prior is not None:
                self.connection.execute(
                    "DELETE FROM change_evaluations WHERE evaluation_id=?", (prior["evaluation_id"],)
                )
            self.connection.execute(
                "INSERT INTO change_evaluations(change_id,order_id,before_json,after_json,diff_json,"
                "conflict_json,applied,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    change_id,
                    order_id,
                    canonical_json(before_units),
                    canonical_json(after_units),
                    canonical_json(diffs),
                    canonical_json(blocking + locked_units),
                    1 if applied else 0,
                    actor_id,
                    self._now(),
                ),
            )
            if applied:
                self._materialize_change(before_units, after_units, change_row["revision_label"])
                self._audit("change", change_id, "change.applied", actor_id, {
                    "order_id": order_id,
                    "rescheduled": len(after_units),
                    "locked": len(locked_units),
                    "revision_label": change_row["revision_label"],
                })
            else:
                self._audit("change", change_id, "change.blocked", actor_id, {
                    "order_id": order_id,
                    "conflicts": blocking,
                    "locked": locked_units,
                })
        return {
            "change_id": change_id,
            "order_id": order_id,
            "applied": applied,
            "rescheduled_units": len(after_units) if applied else 0,
            "locked_units": locked_units,
            "conflicts": blocking,
            "affected_delivery": affected,
            "diff": diffs,
        }

    def _materialize_change(
        self, before_units: Sequence[Mapping[str, Any]], after_units: Sequence[Mapping[str, Any]], revision_label: str
    ) -> None:
        """用变更后的承诺替换未开工设备的占用（旧承诺已留存在评估记录中）。"""
        for before, after in zip(before_units, after_units):
            unit_id = before["unit_id"]
            self.connection.execute(
                "DELETE FROM unit_components WHERE unit_id=? AND state='held'", (unit_id,)
            )
            for entry in before["components"]:
                self.connection.execute(
                    "UPDATE component_lots SET held_qty=held_qty-?,revision=revision+1 WHERE lot_id=?",
                    (entry["quantity"], entry["lot_id"]),
                )
            self.connection.execute(
                "DELETE FROM unit_schedule WHERE unit_id=? AND state='scheduled'", (unit_id,)
            )
            for slot in before["schedule"]:
                self.connection.execute(
                    "UPDATE line_capacities SET booked_hours=CAST(booked_hours AS REAL)-?,"
                    "revision=revision+1 WHERE line_id=? AND station=? AND service_date=?",
                    (float(slot["hours"]), slot["line_id"], slot["station"], slot["service_date"]),
                )
            self.connection.execute(
                "UPDATE production_units SET design_revision=?,design_json=?,planned_complete_on=? WHERE unit_id=?",
                (
                    revision_label,
                    canonical_json({"design_revision": revision_label, "components": after["components"]}),
                    after["planned_complete_on"],
                    unit_id,
                ),
            )
            for entry in after["components"]:
                self.connection.execute(
                    "INSERT INTO unit_components(unit_id,lot_id,category,component_model,quantity,"
                    "is_substitute,rule_id,state,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        unit_id,
                        entry["lot_id"],
                        entry["category"],
                        entry["component_model"],
                        entry["quantity"],
                        1 if entry["is_substitute"] else 0,
                        entry["rule_id"],
                        "held",
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "UPDATE component_lots SET held_qty=held_qty+?,revision=revision+1 WHERE lot_id=?",
                    (entry["quantity"], entry["lot_id"]),
                )
            for slot in after["schedule"]:
                self.connection.execute(
                    "INSERT INTO unit_schedule(unit_id,line_id,station,seq_no,service_date,hours,state) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (unit_id, slot["line_id"], slot["station"], slot["seq_no"], slot["service_date"], slot["hours"], "scheduled"),
                )
                self.connection.execute(
                    "UPDATE line_capacities SET booked_hours=CAST(booked_hours AS REAL)+?,"
                    "revision=revision+1 WHERE line_id=? AND station=? AND service_date=?",
                    (float(slot["hours"]), slot["line_id"], slot["station"], slot["service_date"]),
                )

    def rollback_change(self, actor_id: str, change_id: str, order_id: str) -> dict[str, Any]:
        self._require(actor_id, "change.apply")
        evaluation = self.connection.execute(
            "SELECT * FROM change_evaluations WHERE change_id=? AND order_id=?",
            (change_id, order_id),
        ).fetchone()
        if evaluation is None:
            raise NotFound("变更评估不存在")
        if not evaluation["applied"]:
            raise InvalidState("变更当时未成功应用，无需回退")
        if evaluation["undone"]:
            raise InvalidState("变更已经回退")
        before_units = json.loads(evaluation["before_json"])
        after_units = json.loads(evaluation["after_json"])
        with transaction(self.connection, immediate=True):
            # 回退只允许发生在相关设备仍未开工时。
            for item in before_units:
                row = self._unit_row(item["unit_id"])
                if row["state"] != "scheduled" or row["started_at"] is not None:
                    raise InvalidState(f"设备 {item['unit_id']} 已开工，不能回退承诺")
            # 先撤销当前（变更后）占用，随后在同一事务内核验旧承诺资源仍然可得；
            # 核验失败会整体回滚，台账恢复到进入事务前的状态。
            for after in after_units:
                unit_id = after["unit_id"]
                for entry in after["components"]:
                    self.connection.execute(
                        "UPDATE component_lots SET held_qty=held_qty-?,revision=revision+1 WHERE lot_id=?",
                        (entry["quantity"], entry["lot_id"]),
                    )
                self.connection.execute(
                    "DELETE FROM unit_components WHERE unit_id=? AND state='held'", (unit_id,)
                )
                for slot in after["schedule"]:
                    self.connection.execute(
                        "UPDATE line_capacities SET booked_hours=CAST(booked_hours AS REAL)-?,"
                        "revision=revision+1 WHERE line_id=? AND station=? AND service_date=?",
                        (float(slot["hours"]), slot["line_id"], slot["station"], slot["service_date"]),
                    )
                self.connection.execute(
                    "DELETE FROM unit_schedule WHERE unit_id=? AND state='scheduled'", (unit_id,)
                )
            shortages: list[dict[str, Any]] = []
            for before in before_units:
                for entry in before["components"]:
                    lot = self.lot(entry["lot_id"])
                    available = int(lot["quantity"]) - int(lot["held_qty"]) - int(lot["consumed_qty"])
                    if available < entry["quantity"]:
                        shortages.append({
                            "code": "rollback_component_unavailable",
                            "lot_id": entry["lot_id"],
                            "available": available,
                            "required": entry["quantity"],
                        })
                for slot in before["schedule"]:
                    cap = self.connection.execute(
                        "SELECT * FROM line_capacities WHERE line_id=? AND station=? AND service_date=?",
                        (slot["line_id"], slot["station"], slot["service_date"]),
                    ).fetchone()
                    remaining = Decimal(str(cap["available_hours"])) - Decimal(str(cap["booked_hours"]))
                    if remaining < Decimal(str(slot["hours"])):
                        shortages.append({
                            "code": "rollback_capacity_unavailable",
                            "line_id": slot["line_id"],
                            "station": slot["station"],
                            "service_date": slot["service_date"],
                        })
            if shortages:
                raise InvalidState("回退目标资源已被其他承诺占用，回退被拒绝", shortages)
            for before in before_units:
                unit_id = before["unit_id"]
                for entry in before["components"]:
                    self.connection.execute(
                        "INSERT INTO unit_components(unit_id,lot_id,category,component_model,quantity,"
                        "is_substitute,rule_id,state,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            unit_id,
                            entry["lot_id"],
                            entry["category"],
                            entry["component_model"],
                            entry["quantity"],
                            1 if entry["is_substitute"] else 0,
                            entry["rule_id"],
                            "held",
                            self._now(),
                        ),
                    )
                    self.connection.execute(
                        "UPDATE component_lots SET held_qty=held_qty+?,revision=revision+1 WHERE lot_id=?",
                        (entry["quantity"], entry["lot_id"]),
                    )
                for slot in before["schedule"]:
                    self.connection.execute(
                        "INSERT INTO unit_schedule(unit_id,line_id,station,seq_no,service_date,hours,state) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (unit_id, slot["line_id"], slot["station"], slot["seq_no"], slot["service_date"], slot["hours"], "scheduled"),
                    )
                    self.connection.execute(
                        "UPDATE line_capacities SET booked_hours=CAST(booked_hours AS REAL)+?,"
                        "revision=revision+1 WHERE line_id=? AND station=? AND service_date=?",
                        (float(slot["hours"]), slot["line_id"], slot["station"], slot["service_date"]),
                    )
                self.connection.execute(
                    "UPDATE production_units SET design_revision=?,design_json=?,planned_complete_on=? WHERE unit_id=?",
                    (
                        before["design_revision"],
                        canonical_json({"design_revision": before["design_revision"]}),
                        before["planned_complete_on"],
                        unit_id,
                    ),
                )
            diffs = json.loads(evaluation["diff_json"])
            reversed_diffs = [
                {
                    **item,
                    "kind": "rolled_back",
                    "changed": [
                        {**change, "before": change["after"], "after": change["before"]}
                        for change in item["changed"]
                    ],
                }
                for item in diffs
            ]
            self.connection.execute(
                "UPDATE change_evaluations SET undone=1,undone_at=?,diff_json=? WHERE evaluation_id=?",
                (self._now(), canonical_json(reversed_diffs), evaluation["evaluation_id"]),
            )
            self._audit("change", change_id, "change.rolled_back", actor_id, {
                "order_id": order_id, "restored_units": len(before_units),
            })
        return {
            "change_id": change_id,
            "order_id": order_id,
            "rolled_back": True,
            "restored_units": len(before_units),
            "diff": reversed_diffs,
        }

    def _change_row(self, change_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM engineering_changes WHERE change_id=?", (change_id,)
        ).fetchone()
        if row is None:
            raise NotFound("工程变更不存在")
        return row

    def evaluation(self, actor_id: str, change_id: str, order_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self.connection.execute(
            "SELECT * FROM change_evaluations WHERE change_id=? AND order_id=?",
            (change_id, order_id),
        ).fetchone()
        if row is None:
            raise NotFound("变更评估不存在")
        return {
            "change_id": change_id,
            "order_id": order_id,
            "applied": bool(row["applied"]),
            "undone": bool(row["undone"]),
            "before": json.loads(row["before_json"]),
            "after": json.loads(row["after_json"]),
            "diff": json.loads(row["diff_json"]),
            "conflicts": json.loads(row["conflict_json"]),
            "created_at": row["created_at"],
            "undone_at": row["undone_at"],
        }

    # ------------------------------------------------------------------ 报告

    def conflicts_report(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        results: list[dict[str, Any]] = []
        for order_row in self.connection.execute("SELECT * FROM orders ORDER BY due_date,order_id"):
            if order_row["state"] == "submitted":
                plan = self._build_plan(order_row)
                results.append({
                    "order_id": order_row["order_id"],
                    "state": "submitted",
                    "feasible": plan["feasible"],
                    "earliest_complete_on": plan["earliest_complete_on"],
                    "conflicts": plan["conflicts"],
                })
        blocked = [
            {
                "unit_id": row["unit_id"],
                "order_id": row["order_id"],
                "gate": self.connection.execute(
                    "SELECT gate FROM inspection_records WHERE unit_id=? AND result='failed' "
                    "ORDER BY seq_no LIMIT 1",
                    (row["unit_id"],),
                ).fetchone()[0],
            }
            for row in self.connection.execute(
                "SELECT * FROM production_units WHERE state='blocked' ORDER BY unit_id"
            )
        ]
        return {"orders": results, "blocked_units": blocked}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM mfg_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
