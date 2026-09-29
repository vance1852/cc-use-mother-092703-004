"""制造交付编排事务用例。

把订单承诺、部件批次、产线能力、检验关口和运输窗口放进同一个计划：
订单确认时整体占用部件批次与运输舱位；工程变更时已开工单元锁定、
未开工单元重新排程；所有计划修订都可比较差异并回退。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    ComponentBatch,
    ComponentPart,
    CustomerConstraint,
    EngineeringChange,
    InspectionGate,
    OrderRequest,
    ProductionLine,
    ShippingWindow,
    SubstitutionRule,
)
from .planning import build_order_plan, diff_plans, restore_order_plan
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "sales": {"customer.write", "order.write", "order.cancel", "plan.read"},
    "coordinator": {
        "resource.write",
        "plan.run",
        "plan.read",
        "order.confirm",
        "change.apply",
        "rollback.run",
    },
    "engineer": {"part.write", "change.write", "plan.read"},
    "warehouse": {"batch.write", "production.start", "ship"},
    "quality": {"gate.write", "batch.certify", "plan.read"},
    "auditor": {"audit.read", "plan.read"},
}

ACTIVE_ORDER_STATES = ("confirmed", "in_production", "change_pending")
STARTED_UNIT_STATES = ("production", "inspection", "awaiting_shipment", "shipped", "completed")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class ManufacturingService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _today(self) -> str:
        return self.clock.now().date().isoformat()

    # ------------------------------------------------------------------ 用户与审计

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM pd_users WHERE user_id=?", (user_id,)
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
            "SELECT event_hash FROM pd_audit_events ORDER BY event_id DESC LIMIT 1"
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
            "INSERT INTO pd_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
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
                    "INSERT INTO pd_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM pd_audit_events ORDER BY event_id").fetchall()
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

    # ------------------------------------------------------------------ 主数据

    def create_customer_constraint(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "customer.write")
        constraint = CustomerConstraint.from_dict(raw)
        now = self._now()
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO customer_constraints(customer_id,approved_grades_json,allowed_substitutes_json,"
                    "notes,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        constraint.customer_id,
                        canonical_json(sorted(constraint.approved_grades)),
                        canonical_json(sorted(constraint.allowed_substitutes)),
                        constraint.notes,
                        actor_id,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("客户约束已经存在") from exc
            self._audit("customer", constraint.customer_id, "customer.constraint_created", actor_id, dict(raw))
        return self.customer_constraint(constraint.customer_id)

    def customer_constraint(self, customer_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM customer_constraints WHERE customer_id=?", (customer_id,)
        ).fetchone()
        if row is None:
            raise NotFound("客户约束不存在")
        return {
            "customer_id": row["customer_id"],
            "approved_grades": json.loads(row["approved_grades_json"]),
            "allowed_substitutes": json.loads(row["allowed_substitutes_json"]),
            "notes": row["notes"],
            "revision": row["revision"],
        }

    def register_part(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "part.write")
        part = ComponentPart.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO component_parts(part_id,name,family,unit,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (part.part_id, part.name, part.family, part.unit, actor_id, self._now()),
                )
                self._audit("part", part.part_id, "part.registered", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("部件编号已经存在") from exc
        return {"part_id": part.part_id, "name": part.name, "family": part.family, "unit": part.unit}

    def register_substitution_rule(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "part.write")
        rule = SubstitutionRule.from_dict(raw)
        original = self.connection.execute(
            "SELECT * FROM component_parts WHERE part_id=?", (rule.original_part_id,)
        ).fetchone()
        substitute = self.connection.execute(
            "SELECT * FROM component_parts WHERE part_id=?", (rule.substitute_part_id,)
        ).fetchone()
        if original is None or substitute is None:
            raise ValidationFailed("替代规则引用的部件不存在")
        if original["family"] != substitute["family"]:
            raise ValidationFailed("替代部件必须与原件属于同一产品族")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO substitution_rules(rule_id,original_part_id,substitute_part_id,minimum_grade,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        rule.rule_id,
                        rule.original_part_id,
                        rule.substitute_part_id,
                        rule.minimum_grade,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("substitution_rule", rule.rule_id, "rule.registered", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("替代规则编号已经存在") from exc
        return {
            "rule_id": rule.rule_id,
            "original_part_id": rule.original_part_id,
            "substitute_part_id": rule.substitute_part_id,
            "minimum_grade": rule.minimum_grade,
        }

    def register_batch(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "batch.write")
        batch = ComponentBatch.from_dict(raw)
        part = self.connection.execute(
            "SELECT * FROM component_parts WHERE part_id=?", (batch.part_id,)
        ).fetchone()
        if part is None:
            raise ValidationFailed("部件不存在，不能登记批次")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO component_batches(batch_id,part_id,quantity,grade,heat_number,certified,"
                    "received_on,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        batch.batch_id,
                        batch.part_id,
                        batch.quantity,
                        batch.grade,
                        batch.heat_number,
                        1 if batch.certified else 0,
                        batch.received_on,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("batch", batch.batch_id, "batch.registered", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("部件批次编号已经存在") from exc
        return self.batch(batch.batch_id)

    def certify_batch(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        self._require(actor_id, "batch.certify")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE component_batches SET certified=1,revision=revision+1 WHERE batch_id=? AND certified=0",
                (batch_id,),
            )
            if cursor.rowcount != 1:
                row = self.connection.execute(
                    "SELECT certified FROM component_batches WHERE batch_id=?", (batch_id,)
                ).fetchone()
                if row is None:
                    raise NotFound("部件批次不存在")
                raise InvalidState("批次已经通过质量认证")
            self._audit("batch", batch_id, "batch.certified", actor_id, {})
        return self.batch(batch_id)

    def batch(self, batch_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM component_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFound("部件批次不存在")
        held = self.connection.execute(
            "SELECT COALESCE(SUM(quantity),0) held FROM component_reservations WHERE batch_id=? AND state='held'",
            (batch_id,),
        ).fetchone()["held"]
        return {
            "batch_id": row["batch_id"],
            "part_id": row["part_id"],
            "quantity": row["quantity"],
            "held_quantity": held,
            "available_quantity": row["quantity"] - held,
            "grade": row["grade"],
            "heat_number": row["heat_number"],
            "certified": bool(row["certified"]),
            "received_on": row["received_on"],
            "revision": row["revision"],
        }

    def create_line(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "resource.write")
        line = ProductionLine.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO production_lines(line_id,family,name,daily_capacity,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (line.line_id, line.family, line.name, line.daily_capacity, actor_id, self._now()),
                )
                self._audit("line", line.line_id, "line.created", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("产线编号已经存在") from exc
        return {"line_id": line.line_id, "family": line.family, "name": line.name, "daily_capacity": line.daily_capacity}

    def create_gate(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "gate.write")
        gate = InspectionGate.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO inspection_gates(gate_id,family,name,sequence,duration_days,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (gate.gate_id, gate.family, gate.name, gate.sequence, gate.duration_days, actor_id, self._now()),
                )
                self._audit("gate", gate.gate_id, "gate.created", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("检验关口编号已经存在") from exc
        return {
            "gate_id": gate.gate_id,
            "family": gate.family,
            "name": gate.name,
            "sequence": gate.sequence,
            "duration_days": gate.duration_days,
        }

    def create_shipping_window(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "resource.write")
        window = ShippingWindow.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO shipping_windows(window_id,destination,opens_on,closes_on,slots,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        window.window_id,
                        window.destination,
                        window.opens_on,
                        window.closes_on,
                        window.slots,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("window", window.window_id, "window.created", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("运输窗口编号已经存在") from exc
        return {
            "window_id": window.window_id,
            "destination": window.destination,
            "opens_on": window.opens_on,
            "closes_on": window.closes_on,
            "slots": window.slots,
        }

    # ------------------------------------------------------------------ 订单

    def submit_order(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "order.write")
        order = OrderRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM pd_idempotency WHERE scope='order' AND idempotency_key=?",
            (order.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同订单内容")
            return json.loads(stored["response_json"])
        if self.connection.execute(
            "SELECT 1 FROM customer_constraints WHERE customer_id=?", (order.customer_id,)
        ).fetchone() is None:
            raise ValidationFailed("客户约束不存在，无法核验客户准入")
        if self.connection.execute(
            "SELECT 1 FROM shipping_windows WHERE window_id=?", (order.shipping_window_id,)
        ).fetchone() is None:
            raise ValidationFailed("运输窗口不存在")
        for line in order.bom:
            part = self.connection.execute(
                "SELECT family FROM component_parts WHERE part_id=?", (line.part_id,)
            ).fetchone()
            if part is None:
                raise ValidationFailed(f"BOM 部件 {line.part_id} 不存在")
            if part["family"] != order.family:
                raise ValidationFailed(f"BOM 部件 {line.part_id} 与订单产品族不一致")
        response = {"order_id": order.order_id, "state": "draft", "revision": 0}
        try:
            with transaction(self.connection, immediate=True):
                now = self._now()
                self.connection.execute(
                    "INSERT INTO orders(order_id,customer_id,product_model,family,quantity,requested_date,"
                    "window_id,state,revision,idempotency_key,submitted_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,'draft',0,?,?,?,?)",
                    (
                        order.order_id,
                        order.customer_id,
                        order.product_model,
                        order.family,
                        order.quantity,
                        order.requested_date,
                        order.shipping_window_id,
                        order.idempotency_key,
                        actor_id,
                        now,
                        now,
                    ),
                )
                self.connection.executemany(
                    "INSERT INTO order_bom(order_id,part_id,quantity) VALUES(?,?,?)",
                    [(order.order_id, line.part_id, line.quantity) for line in order.bom],
                )
                self.connection.execute(
                    "INSERT INTO pd_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('order',?,?,?,?)",
                    (order.idempotency_key, request_digest, canonical_json(response), now),
                )
                self._audit("order", order.order_id, "order.submitted", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("订单编号或幂等键冲突") from exc
        return response

    def _load_order(self, order_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if row is None:
            raise NotFound("订单不存在")
        return row

    def order(self, actor_id: str, order_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.read")
        row = self._load_order(order_id)
        result = dict(row)
        result["bom"] = [
            {"part_id": item["part_id"], "quantity": item["quantity"]}
            for item in self.connection.execute(
                "SELECT part_id,quantity FROM order_bom WHERE order_id=? ORDER BY part_id", (order_id,)
            ).fetchall()
        ]
        return result

    def cancel_order(self, actor_id: str, order_id: str) -> dict[str, Any]:
        self._require(actor_id, "order.cancel")
        order_row = self._load_order(order_id)
        if order_row["state"] in ("completed", "cancelled"):
            raise InvalidState("订单已结束，不能取消")
        if order_row["state"] == "change_pending":
            raise InvalidState("订单存在待决工程变更，请先应用或驳回变更")
        started = self.connection.execute(
            "SELECT COUNT(1) count FROM unit_states WHERE order_id=? AND state IN ({})".format(
                ",".join("?" for _ in STARTED_UNIT_STATES)
            ),
            [order_id, *STARTED_UNIT_STATES],
        ).fetchone()["count"]
        if started:
            raise InvalidState("已有设备开工，不能整体取消；请使用工程变更调整未开工部分")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE component_reservations SET state='released' WHERE order_id=? AND state='held'",
                (order_id,),
            )
            cursor = self.connection.execute(
                "UPDATE orders SET state='cancelled',updated_at=? WHERE order_id=?",
                (self._now(), order_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("订单状态已变化")
            self._audit("order", order_id, "order.cancelled", actor_id, {})
        return {"order_id": order_id, "state": "cancelled"}

    # ------------------------------------------------------ 计划上下文读取

    def _catalog_context(self, order_row: sqlite3.Row) -> dict[str, Any]:
        parts = {
            row["part_id"]: dict(row)
            for row in self.connection.execute(
                "SELECT part_id,name,family,unit FROM component_parts WHERE active=1"
            ).fetchall()
        }
        rules = [
            dict(row)
            for row in self.connection.execute(
                "SELECT rule_id,original_part_id,substitute_part_id,minimum_grade,active "
                "FROM substitution_rules WHERE active=1"
            ).fetchall()
        ]
        batches = [
            {
                "batch_id": row["batch_id"],
                "part_id": row["part_id"],
                "grade": row["grade"],
                "certified": bool(row["certified"]),
                "received_on": row["received_on"],
            }
            for row in self.connection.execute(
                "SELECT batch_id,part_id,grade,certified,received_on FROM component_batches"
            ).fetchall()
        ]
        constraint_row = self.connection.execute(
            "SELECT * FROM customer_constraints WHERE customer_id=?",
            (order_row["customer_id"],),
        ).fetchone()
        constraint = {
            "approved_grades": frozenset(json.loads(constraint_row["approved_grades_json"])),
            "allowed_substitutes": frozenset(json.loads(constraint_row["allowed_substitutes_json"])),
        }
        window = dict(
            self.connection.execute(
                "SELECT window_id,destination,opens_on,closes_on,slots FROM shipping_windows WHERE window_id=?",
                (order_row["window_id"],),
            ).fetchone()
        )
        lines = [dict(row) for row in self.connection.execute(
            "SELECT line_id,family,name,daily_capacity,active FROM production_lines WHERE active=1"
        ).fetchall()]
        gates = [dict(row) for row in self.connection.execute(
            "SELECT gate_id,family,name,sequence,duration_days,active FROM inspection_gates WHERE active=1"
        ).fetchall()]
        return {
            "parts": parts,
            "rules": rules,
            "batches": batches,
            "constraint": constraint,
            "window": window,
            "lines": lines,
            "gates": gates,
        }

    def _availability(self, order_id: str, replannable_units: set[int] | None = None) -> dict[str, int]:
        """各批次对当前订单可见的可用量。

        其它订单的有效占用、本订单已开工单元的占用都要扣除；本订单待重排
        单元（未开工）的旧占用视为可释放，需要加回。
        """
        replannable_units = replannable_units or set()
        rows = self.connection.execute(
            "SELECT b.batch_id, b.quantity, COALESCE(r.held, 0) held "
            "FROM component_batches b LEFT JOIN ("
            "SELECT batch_id, SUM(quantity) held FROM component_reservations WHERE state='held' "
            "GROUP BY batch_id"
            ") r ON r.batch_id=b.batch_id",
        ).fetchall()
        result = {row["batch_id"]: row["quantity"] - row["held"] for row in rows}
        if replannable_units:
            placeholders = ",".join("?" for _ in replannable_units)
            released = self.connection.execute(
                f"SELECT batch_id, SUM(quantity) freed FROM component_reservations "
                f"WHERE order_id=? AND state='held' AND unit_no IN ({placeholders}) GROUP BY batch_id",
                [order_id, *sorted(replannable_units)],
            ).fetchall()
            for row in released:
                result[row["batch_id"]] = result.get(row["batch_id"], 0) + row["freed"]
        return result

    def _line_busy(
        self, order_id: str, fixed_units: set[int]
    ) -> tuple[dict[tuple[str, str], int], dict[tuple[str, str], list[dict[str, str]]]]:
        """从其它有效订单的当前计划中汇总产线占用，并附上冲突来源。"""
        busy: dict[tuple[str, str], int] = {}
        sources: dict[tuple[str, str], list[dict[str, str]]] = {}
        rows = self.connection.execute(
            "SELECT order_id, current_plan_revision FROM orders WHERE state IN ({}) AND order_id<>?".format(
                ",".join("?" for _ in ACTIVE_ORDER_STATES)
            ),
            [*ACTIVE_ORDER_STATES, order_id],
        ).fetchall()
        for other in rows:
            revision = self.connection.execute(
                "SELECT plan_json FROM order_plan_revisions WHERE order_id=? AND revision=?",
                (other["order_id"], other["current_plan_revision"]),
            ).fetchone()
            if revision is None:
                continue
            plan = json.loads(revision["plan_json"])
            for unit in plan["units"]:
                if not unit.get("production_date") or not unit.get("line_id"):
                    continue
                key = (unit["line_id"], unit["production_date"])
                busy[key] = busy.get(key, 0) + 1
                sources.setdefault(key, []).append({
                    "order_id": other["order_id"],
                    "unit_no": unit["unit_no"],
                    "product_model": unit["product_model"],
                })
        # 本订单已开工单元对自己也是不可移动的既成占用。
        for unit_no in sorted(fixed_units):
            unit = self.connection.execute(
                "SELECT product_model,line_id,production_date FROM unit_states WHERE order_id=? AND unit_no=?",
                (order_id, unit_no),
            ).fetchone()
            if unit is None or not unit["production_date"]:
                continue
            key = (unit["line_id"], unit["production_date"])
            busy[key] = busy.get(key, 0) + 1
            sources.setdefault(key, []).append({
                "order_id": order_id,
                "unit_no": unit_no,
                "product_model": unit["product_model"],
                "locked": True,
            })
        return busy, sources

    def _window_competitors(self, order_id: str, window_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT order_id,quantity FROM orders WHERE window_id=? AND state IN ({}) AND order_id<>? "
            "ORDER BY order_id".format(",".join("?" for _ in ACTIVE_ORDER_STATES)),
            [window_id, *ACTIVE_ORDER_STATES, order_id],
        ).fetchall()
        return [{"order_id": row["order_id"], "quantity": row["quantity"]} for row in rows]

    def _assemble_plan(
        self,
        order_row: sqlite3.Row,
        *,
        product_model: str,
        bom: Sequence[Mapping[str, Any]],
        fixed: Mapping[int, dict[str, Any]],
        replannable_units: set[int],
    ) -> dict[str, Any]:
        context = self._catalog_context(order_row)
        busy, busy_sources = self._line_busy(order_row["order_id"], set(fixed))
        return build_order_plan(
            order={
                "order_id": order_row["order_id"],
                "customer_id": order_row["customer_id"],
                "family": order_row["family"],
                "quantity": order_row["quantity"],
                "requested_date": order_row["requested_date"],
                "window_id": order_row["window_id"],
            },
            product_model=product_model,
            bom=bom,
            as_of=self._today(),
            parts=context["parts"],
            rules=context["rules"],
            constraint=context["constraint"],
            batches=context["batches"],
            availability=self._availability(order_row["order_id"], replannable_units),
            busy=busy,
            busy_sources=busy_sources,
            lines=context["lines"],
            gates=context["gates"],
            window=context["window"],
            window_competitors=self._window_competitors(order_row["order_id"], order_row["window_id"]),
            fixed=fixed,
        )

    def _fixed_units(self, order_id: str) -> dict[int, dict[str, Any]]:
        """已开工单元的冻结视图：产品型号、产线、检验排期与部件占用保持不变。"""
        started_rows = self.connection.execute(
            "SELECT * FROM unit_states WHERE order_id=? ORDER BY unit_no", (order_id,)
        ).fetchall()
        if not started_rows:
            return {}
        order_row = self._load_order(order_id)
        revision_row = self.connection.execute(
            "SELECT plan_json FROM order_plan_revisions WHERE order_id=? AND revision=?",
            (order_id, order_row["current_plan_revision"]),
        ).fetchone()
        planned_units = {
            int(unit["unit_no"]): unit
            for unit in json.loads(revision_row["plan_json"])["units"]
        } if revision_row else {}
        gate_results = self.connection.execute(
            "SELECT unit_no,gate_id,state FROM gate_results WHERE order_id=?", (order_id,)
        ).fetchall()
        result_map: dict[tuple[int, str], str] = {
            (row["unit_no"], row["gate_id"]): row["state"] for row in gate_results
        }
        reservations = self.connection.execute(
            "SELECT unit_no,requirement_part_id,batch_id,part_id,rule_id,quantity "
            "FROM component_reservations WHERE order_id=? AND state='held' ORDER BY unit_no,requirement_part_id",
            (order_id,),
        ).fetchall()
        fixed: dict[int, dict[str, Any]] = {}
        for row in started_rows:
            if row["state"] not in STARTED_UNIT_STATES:
                continue
            unit_no = int(row["unit_no"])
            planned = planned_units.get(unit_no, {})
            gates = []
            for gate in planned.get("gates", []):
                gate = dict(gate)
                gate["state"] = result_map.get((unit_no, gate["gate_id"]), "pending")
                gates.append(gate)
            fixed[unit_no] = {
                "unit": {
                    "product_model": row["product_model"],
                    "line_id": row["line_id"],
                    "production_date": row["production_date"],
                    "ready_on": planned.get("ready_on"),
                    "ship_on": planned.get("ship_on"),
                    "window_id": planned.get("window_id", order_row["window_id"]),
                },
                "gates": gates,
                "reservations": [
                    dict(item)
                    for item in reservations
                    if int(item["unit_no"]) == unit_no
                ],
            }
        return fixed

    # ------------------------------------------------------ 计划、确认

    def plan_order(self, actor_id: str, order_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.run")
        order_row = self._load_order(order_id)
        if order_row["state"] in ("completed", "cancelled"):
            raise InvalidState("订单已结束，不能重新排程")
        if order_row["state"] == "change_pending":
            raise InvalidState("订单存在待决工程变更，请先应用或驳回")
        started = self._fixed_units(order_id)
        if started and order_row["state"] != "draft":
            raise InvalidState("已有设备开工，普通重排会改动在制设备；请发起工程变更")

        bom = [
            {"part_id": row["part_id"], "quantity": row["quantity"]}
            for row in self.connection.execute(
                "SELECT part_id,quantity FROM order_bom WHERE order_id=? ORDER BY part_id",
                (order_id,),
            ).fetchall()
        ]
        replannable = {number for number in range(1, int(order_row["quantity"]) + 1) if number not in started}
        with transaction(self.connection, immediate=True):
            result = self._assemble_plan(
                order_row,
                product_model=order_row["product_model"],
                bom=bom,
                fixed=started,
                replannable_units=replannable,
            )
            revision = self._persist_revision(
                order_row,
                result,
                actor_id,
                change_id=None,
                restored_from=None,
            )
            self._audit("order", order_id, "order.planned", actor_id, {
                "revision": revision,
                "feasible": result["feasible"],
                "issues": [issue["type"] for issue in result["issues"]],
            })
        return self._revision_view(order_id, revision)

    def _persist_revision(
        self,
        order_row: sqlite3.Row,
        result: Mapping[str, Any],
        actor_id: str,
        *,
        change_id: str | None,
        restored_from: int | None,
    ) -> int:
        next_revision = int(order_row["revision"]) + 1
        self.connection.execute(
            "INSERT INTO order_plan_revisions(order_id,revision,plan_json,issues_json,feasible,change_id,"
            "restored_from,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                order_row["order_id"],
                next_revision,
                canonical_json(result["plan"]),
                canonical_json(result["issues"]),
                1 if result["feasible"] else 0,
                change_id,
                restored_from,
                actor_id,
                self._now(),
            ),
        )
        self.connection.execute(
            "UPDATE orders SET revision=?, updated_at=? WHERE order_id=?",
            (next_revision, self._now(), order_row["order_id"]),
        )
        return next_revision

    @staticmethod
    def _commitment_signature(result: Mapping[str, Any]) -> str:
        """影响资源占用的承诺要素：占用、产线/生产日期与发运窗口。"""
        plan = result["plan"]
        reservations = sorted(
            (row["batch_id"], row["part_id"], row["requirement_part_id"], row["rule_id"], row["unit_no"], row["quantity"])
            for row in plan["reservations"]
        )
        placements = sorted(
            (row["unit_no"], row["line_id"], row["production_date"], row["window_id"])
            for row in plan["units"]
        )
        return digest({
            "reservations": reservations,
            "placements": placements,
            "promised_ship_on": plan["promised_ship_on"],
            "ready_on": plan["ready_on"],
        })

    def _revalidate_commitment(
        self,
        order_row: sqlite3.Row,
        revision_row: sqlite3.Row,
        *,
        product_model: str,
        bom: Sequence[Mapping[str, Any]],
        fixed: Mapping[int, dict[str, Any]],
        replannable_units: set[int],
    ) -> None:
        """在写入事务内按当前资源重算，确认待承诺修订没有被并发提交抢空。"""
        fresh = self._assemble_plan(
            order_row,
            product_model=product_model,
            bom=bom,
            fixed=fixed,
            replannable_units=replannable_units,
        )
        if not fresh["feasible"]:
            raise Conflict(
                "资源已被其他合同占用，计划修订失效，请重新排程后再确认",
                {"issues": fresh["issues"]},
            )
        stored_signature = self._commitment_signature({
            "plan": json.loads(revision_row["plan_json"])
        })
        fresh_signature = self._commitment_signature(fresh)
        if stored_signature != fresh_signature:
            raise Conflict(
                "资源状态与生成修订时不一致（部件批次、产线日期或运输舱位已变化），请重新排程",
                {"issues": fresh["issues"]},
            )

    def _current_bom(self, order_id: str) -> list[dict[str, Any]]:
        return [
            {"part_id": row["part_id"], "quantity": row["quantity"]}
            for row in self.connection.execute(
                "SELECT part_id,quantity FROM order_bom WHERE order_id=? ORDER BY part_id",
                (order_id,),
            ).fetchall()
        ]

    def _revision_row(self, order_id: str, revision: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM order_plan_revisions WHERE order_id=? AND revision=?",
            (order_id, revision),
        ).fetchone()
        if row is None:
            raise NotFound("计划修订不存在")
        return row

    def _revision_view(self, order_id: str, revision: int) -> dict[str, Any]:
        row = self._revision_row(order_id, revision)
        return {
            "order_id": order_id,
            "revision": revision,
            "feasible": bool(row["feasible"]),
            "change_id": row["change_id"],
            "restored_from": row["restored_from"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "plan": json.loads(row["plan_json"]),
            "issues": json.loads(row["issues_json"]),
        }

    def latest_plan(self, actor_id: str, order_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.read")
        order_row = self._load_order(order_id)
        if not order_row["revision"]:
            raise InvalidState("订单尚未生成计划修订")
        return self._revision_view(order_id, int(order_row["revision"]))

    def plan_revision(self, actor_id: str, order_id: str, revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.read")
        return self._revision_view(order_id, revision)

    def list_revisions(self, actor_id: str, order_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.read")
        self._load_order(order_id)
        rows = self.connection.execute(
            "SELECT revision,feasible,change_id,restored_from,created_at FROM order_plan_revisions "
            "WHERE order_id=? ORDER BY revision",
            (order_id,),
        ).fetchall()
        return {"order_id": order_id, "revisions": [dict(row) for row in rows]}

    def confirm_order(self, actor_id: str, order_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "order.confirm")
        with transaction(self.connection, immediate=True):
            order_row = self._load_order(order_id)
            if order_row["state"] not in ("draft", "confirmed"):
                raise InvalidState(f"订单状态 {order_row['state']} 不能确认；工程变更需走应用流程")
            if int(order_row["revision"]) != expected_revision:
                raise Conflict("计划修订已过期，请基于最新修订确认")
            revision_row = self._revision_row(order_id, expected_revision)
            if not revision_row["feasible"]:
                raise InvalidState("计划修订存在阻断性冲突，不能整体占用资源")
            if revision_row["change_id"]:
                raise InvalidState("该修订来自工程变更评估，必须使用变更应用接口")
            started = self._fixed_units(order_id)
            if started:
                raise InvalidState("已有设备开工，请通过工程变更应用新计划")
            replannable = {number for number in range(1, int(order_row["quantity"]) + 1)}
            self._revalidate_commitment(
                order_row,
                revision_row,
                product_model=order_row["product_model"],
                bom=self._current_bom(order_id),
                fixed={},
                replannable_units=replannable,
            )
            plan = json.loads(revision_row["plan_json"])
            # 整体占用：部件批次 + 单元排产状态在同一事务落库。
            self.connection.execute(
                "UPDATE component_reservations SET state='released' WHERE order_id=? AND state='held'",
                (order_id,),
            )
            self.connection.executemany(
                "INSERT INTO component_reservations(order_id,plan_revision,unit_no,requirement_part_id,"
                "batch_id,part_id,rule_id,quantity,state,created_at) VALUES(?,?,?,?,?,?,?,?, 'held', ?)",
                [
                    (
                        order_id,
                        expected_revision,
                        row["unit_no"],
                        row["requirement_part_id"],
                        row["batch_id"],
                        row["part_id"],
                        row["rule_id"],
                        row["quantity"],
                        self._now(),
                    )
                    for row in plan["reservations"]
                ],
            )
            existing = {
                row["unit_no"]
                for row in self.connection.execute(
                    "SELECT unit_no FROM unit_states WHERE order_id=?", (order_id,)
                ).fetchall()
            }
            for unit in plan["units"]:
                if unit["unit_no"] in existing:
                    self.connection.execute(
                        "UPDATE unit_states SET revision=?, product_model=?, line_id=?, production_date=?, "
                        "state='planned', started_at=NULL, updated_at=? WHERE order_id=? AND unit_no=?",
                        (
                            expected_revision,
                            unit["product_model"],
                            unit["line_id"],
                            unit["production_date"],
                            self._now(),
                            order_id,
                            unit["unit_no"],
                        ),
                    )
                else:
                    self.connection.execute(
                        "INSERT INTO unit_states(order_id,unit_no,revision,state,product_model,line_id,"
                        "production_date,updated_at) VALUES(?,?,?, 'planned', ?,?,?,?)",
                        (
                            order_id,
                            unit["unit_no"],
                            expected_revision,
                            unit["product_model"],
                            unit["line_id"],
                            unit["production_date"],
                            self._now(),
                        ),
                    )
            self.connection.execute(
                "UPDATE orders SET state='confirmed', current_plan_revision=?, updated_at=? WHERE order_id=?",
                (expected_revision, self._now(), order_id),
            )
            self._audit("order", order_id, "order.confirmed", actor_id, {
                "revision": expected_revision,
                "promised_ship_on": plan["promised_ship_on"],
                "reservations": len(plan["reservations"]),
            })
        return {
            "order_id": order_id,
            "state": "confirmed",
            "revision": expected_revision,
            "promised_ship_on": plan["promised_ship_on"],
        }

    # ------------------------------------------------------ 生产与检验

    def start_unit(self, actor_id: str, order_id: str, unit_no: int) -> dict[str, Any]:
        self._require(actor_id, "production.start")
        with transaction(self.connection, immediate=True):
            order_row = self._load_order(order_id)
            if order_row["state"] == "change_pending":
                raise InvalidState("订单存在待决工程变更，变更应用或驳回前不能开工新单元")
            unit = self._load_unit(order_id, unit_no)
            if unit["state"] != "planned":
                raise InvalidState(f"单元当前状态 {unit['state']}，不能开工")
            now = self._now()
            self.connection.execute(
                "UPDATE unit_states SET state='production', started_at=COALESCE(started_at,?), updated_at=? "
                "WHERE order_id=? AND unit_no=?",
                (now, now, order_id, unit_no),
            )
            if order_row["state"] == "confirmed":
                self.connection.execute(
                    "UPDATE orders SET state='in_production', updated_at=? WHERE order_id=?",
                    (now, order_id),
                )
            self._audit("unit", f"{order_id}#{unit_no}", "unit.started", actor_id, {
                "line_id": unit["line_id"],
                "production_date": unit["production_date"],
                "product_model": unit["product_model"],
            })
        return self._unit_view(order_id, unit_no)

    def _load_unit(self, order_id: str, unit_no: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM unit_states WHERE order_id=? AND unit_no=?", (order_id, unit_no)
        ).fetchone()
        if row is None:
            raise NotFound("设备单元不存在")
        return row

    def record_gate_result(
        self,
        actor_id: str,
        order_id: str,
        unit_no: int,
        gate_id: str,
        passed: bool,
        note: str = "",
    ) -> dict[str, Any]:
        self._require(actor_id, "gate.write")
        order_row = self._load_order(order_id)
        unit = self._load_unit(order_id, unit_no)
        if unit["state"] not in ("production", "inspection"):
            raise InvalidState(f"单元当前状态 {unit['state']}，不能登记检验结果")
        gate = self.connection.execute(
            "SELECT * FROM inspection_gates WHERE gate_id=? AND family=? AND active=1",
            (gate_id, order_row["family"]),
        ).fetchone()
        if gate is None:
            raise ValidationFailed("检验关口不存在或不属于该产品族")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO gate_results(order_id,unit_no,gate_id,state,note,recorded_by,recorded_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(order_id,unit_no,gate_id) DO UPDATE SET "
                "state=excluded.state,note=excluded.note,recorded_by=excluded.recorded_by,recorded_at=excluded.recorded_at",
                (
                    order_id,
                    unit_no,
                    gate_id,
                    "passed" if passed else "failed",
                    note[:512],
                    actor_id,
                    self._now(),
                ),
            )
            self.connection.execute(
                "UPDATE unit_states SET state='inspection', updated_at=? WHERE order_id=? AND unit_no=? "
                "AND state='production'",
                (self._now(), order_id, unit_no),
            )
            required = self.connection.execute(
                "SELECT gate_id FROM inspection_gates WHERE family=? AND active=1 ORDER BY sequence",
                (order_row["family"],),
            ).fetchall()
            results = {
                row["gate_id"]: row["state"]
                for row in self.connection.execute(
                    "SELECT gate_id,state FROM gate_results WHERE order_id=? AND unit_no=?",
                    (order_id, unit_no),
                ).fetchall()
            }
            if all(results.get(item["gate_id"]) == "passed" for item in required):
                self.connection.execute(
                    "UPDATE unit_states SET state='awaiting_shipment', updated_at=? "
                    "WHERE order_id=? AND unit_no=?",
                    (self._now(), order_id, unit_no),
                )
            self._audit("unit", f"{order_id}#{unit_no}", "gate.recorded", actor_id, {
                "gate_id": gate_id,
                "result": "passed" if passed else "failed",
            })
        return self._unit_view(order_id, unit_no)

    def ship_unit(self, actor_id: str, order_id: str, unit_no: int) -> dict[str, Any]:
        self._require(actor_id, "ship")
        with transaction(self.connection, immediate=True):
            unit = self._load_unit(order_id, unit_no)
            if unit["state"] != "awaiting_shipment":
                raise InvalidState(f"单元当前状态 {unit['state']}，尚未通过全部检验关口")
            self.connection.execute(
                "UPDATE component_reservations SET state='released' "
                "WHERE order_id=? AND unit_no=? AND state='held'",
                (order_id, unit_no),
            )
            self.connection.execute(
                "UPDATE unit_states SET state='shipped', updated_at=? WHERE order_id=? AND unit_no=?",
                (self._now(), order_id, unit_no),
            )
            remaining = self.connection.execute(
                "SELECT COUNT(1) count FROM unit_states WHERE order_id=? AND state NOT IN ('shipped','completed','scrapped')",
                (order_id,),
            ).fetchone()["count"]
            if remaining == 0:
                self.connection.execute(
                    "UPDATE orders SET state='completed', updated_at=? WHERE order_id=?",
                    (self._now(), order_id),
                )
            self._audit("unit", f"{order_id}#{unit_no}", "unit.shipped", actor_id, {})
        return self._unit_view(order_id, unit_no)

    def _unit_view(self, order_id: str, unit_no: int) -> dict[str, Any]:
        unit = self._load_unit(order_id, unit_no)
        gates = [
            {"gate_id": row["gate_id"], "state": row["state"], "recorded_at": row["recorded_at"]}
            for row in self.connection.execute(
                "SELECT gate_id,state,recorded_at FROM gate_results WHERE order_id=? AND unit_no=? ORDER BY gate_id",
                (order_id, unit_no),
            ).fetchall()
        ]
        return {
            "order_id": order_id,
            "unit_no": unit_no,
            "state": unit["state"],
            "product_model": unit["product_model"],
            "line_id": unit["line_id"],
            "production_date": unit["production_date"],
            "started_at": unit["started_at"],
            "gates": gates,
        }

    def order_status(self, actor_id: str, order_id: str) -> dict[str, Any]:
        """协调员视图：当前承诺、单元进度、检验结果与受影响交期。"""
        self._require(actor_id, "plan.read")
        order_row = self._load_order(order_id)
        units = [
            self._unit_view(order_id, number)
            for number in range(1, int(order_row["quantity"]) + 1)
        ]
        response: dict[str, Any] = {
            "order_id": order_id,
            "state": order_row["state"],
            "revision": order_row["revision"],
            "current_plan_revision": order_row["current_plan_revision"],
            "product_model": order_row["product_model"],
            "customer_id": order_row["customer_id"],
            "requested_date": order_row["requested_date"],
            "window_id": order_row["window_id"],
            "units": units,
        }
        if order_row["current_plan_revision"]:
            revision = self._revision_row(order_id, int(order_row["current_plan_revision"]))
            plan = json.loads(revision["plan_json"])
            response["promised_ship_on"] = plan["promised_ship_on"]
            response["ready_on"] = plan["ready_on"]
            response["on_time"] = (
                plan["promised_ship_on"] is not None
                and plan["promised_ship_on"] <= order_row["requested_date"]
            )
        return response

    # ------------------------------------------------------ 工程变更

    def propose_change(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "change.write")
        change = EngineeringChange.from_dict(raw)
        order_row = self._load_order(change.order_id)
        if order_row["state"] not in ("confirmed", "in_production", "change_pending"):
            raise InvalidState(f"订单状态 {order_row['state']} 不能发起工程变更")
        pending = self.connection.execute(
            "SELECT change_id FROM engineering_changes WHERE order_id=? AND state='proposed'",
            (change.order_id,),
        ).fetchone()
        if pending is not None:
            raise InvalidState(f"订单已有待决工程变更 {pending['change_id']}，请先应用或驳回")
        for line in change.new_bom:
            part = self.connection.execute(
                "SELECT family FROM component_parts WHERE part_id=?", (line.part_id,)
            ).fetchone()
            if part is None:
                raise ValidationFailed(f"新 BOM 部件 {line.part_id} 不存在")
            if part["family"] != order_row["family"]:
                raise ValidationFailed(f"新 BOM 部件 {line.part_id} 与订单产品族不一致")
        content_sha256 = digest(raw)
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO engineering_changes(change_id,order_id,definition_json,content_sha256,"
                    "state,revision_from,created_by,created_at) VALUES(?,?,?,?,'proposed',?,?,?)",
                    (
                        change.change_id,
                        change.order_id,
                        canonical_json(raw),
                        content_sha256,
                        int(order_row["current_plan_revision"]),
                        actor_id,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("工程变更编号已经存在") from exc
            self.connection.execute(
                "UPDATE orders SET state='change_pending', updated_at=? WHERE order_id=? AND state<>'change_pending'",
                (self._now(), change.order_id),
            )
            self._audit("change", change.change_id, "change.proposed", actor_id, {
                "order_id": change.order_id,
                "revision_from": int(order_row["current_plan_revision"]),
            })
        return {"change_id": change.change_id, "state": "proposed", "revision_from": int(order_row["current_plan_revision"])}

    def _load_pending_change(self, change_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM engineering_changes WHERE change_id=?", (change_id,)
        ).fetchone()
        if row is None:
            raise NotFound("工程变更不存在")
        if row["state"] != "proposed":
            raise InvalidState(f"工程变更状态 {row['state']}，不能再评估或应用")
        return row

    def evaluate_change(self, actor_id: str, change_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.run")
        change_row = self._load_pending_change(change_id)
        order_id = change_row["order_id"]
        with transaction(self.connection, immediate=True):
            order_row = self._load_order(order_id)
            fixed = self._fixed_units(order_id)
            definition = json.loads(change_row["definition_json"])
            new_bom = [
                {"part_id": line["part_id"], "quantity": line["quantity"]}
                for line in definition["new_bom"]
            ]
            replannable = {number for number in range(1, int(order_row["quantity"]) + 1) if number not in fixed}
            result = self._assemble_plan(
                order_row,
                product_model=definition["new_product_model"],
                bom=new_bom,
                fixed=fixed,
                replannable_units=replannable,
            )
            before = json.loads(
                self._revision_row(order_id, int(change_row["revision_from"]))["plan_json"]
            )
            result["plan"]["revision"] = None
            comparison = diff_plans(before, result["plan"])
            revision = self._persist_revision(
                order_row,
                result,
                actor_id,
                change_id=change_id,
                restored_from=None,
            )
            self.connection.execute(
                "UPDATE orders SET state='change_pending', updated_at=? WHERE order_id=?",
                (self._now(), order_id),
            )
            self._audit("change", change_id, "change.evaluated", actor_id, {
                "revision": revision,
                "feasible": result["feasible"],
                "locked_units": sorted(fixed),
            })
        view = self._revision_view(order_id, revision)
        view["diff"] = comparison
        view["locked_unit_numbers"] = sorted(fixed)
        return view

    def apply_change(self, actor_id: str, change_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "change.apply")
        with transaction(self.connection, immediate=True):
            change_row = self._load_pending_change(change_id)
            order_id = change_row["order_id"]
            order_row = self._load_order(order_id)
            if int(order_row["revision"]) != expected_revision:
                raise Conflict("变更评估修订已过期，请重新评估")
            revision_row = self._revision_row(order_id, expected_revision)
            if revision_row["change_id"] != change_id or not revision_row["feasible"]:
                raise InvalidState("变更修订不可应用：不存在或仍有阻断性冲突")
            definition = json.loads(change_row["definition_json"])
            fixed = self._fixed_units(order_id)
            fixed_numbers = set(fixed)
            replannable = {number for number in range(1, int(order_row["quantity"]) + 1) if number not in fixed_numbers}
            self._revalidate_commitment(
                order_row,
                revision_row,
                product_model=definition["new_product_model"],
                bom=[
                    {"part_id": line["part_id"], "quantity": line["quantity"]}
                    for line in definition["new_bom"]
                ],
                fixed=fixed,
                replannable_units=replannable,
            )
            plan = json.loads(revision_row["plan_json"])
            self._swap_reservations(order_id, expected_revision, plan, fixed_numbers)
            self.connection.execute("DELETE FROM order_bom WHERE order_id=?", (order_id,))
            self.connection.executemany(
                "INSERT INTO order_bom(order_id,part_id,quantity) VALUES(?,?,?)",
                [(order_id, line["part_id"], line["quantity"]) for line in definition["new_bom"]],
            )
            self.connection.execute(
                "UPDATE orders SET product_model=?, current_plan_revision=?, state=?, updated_at=? WHERE order_id=?",
                (
                    definition["new_product_model"],
                    expected_revision,
                    "in_production" if fixed_numbers else "confirmed",
                    self._now(),
                    order_id,
                ),
            )
            for unit in plan["units"]:
                unit_no = int(unit["unit_no"])
                if unit_no in fixed_numbers:
                    continue
                self.connection.execute(
                    "UPDATE unit_states SET revision=?, product_model=?, line_id=?, production_date=?, "
                    "state='planned', started_at=NULL, updated_at=? WHERE order_id=? AND unit_no=?",
                    (
                        expected_revision,
                        unit["product_model"],
                        unit["line_id"],
                        unit["production_date"],
                        self._now(),
                        order_id,
                        unit_no,
                    ),
                )
            self.connection.execute(
                "UPDATE engineering_changes SET state='applied', revision_to=?, applied_at=? WHERE change_id=?",
                (expected_revision, self._now(), change_id),
            )
            self._audit("change", change_id, "change.applied", actor_id, {
                "order_id": order_id,
                "revision": expected_revision,
                "locked_units": sorted(fixed_numbers),
                "rescheduled_units": [n for n in range(1, int(order_row["quantity"]) + 1) if n not in fixed_numbers],
            })
        return {
            "change_id": change_id,
            "state": "applied",
            "revision": expected_revision,
            "locked_unit_numbers": sorted(fixed_numbers),
            "plan": plan,
        }

    def reject_change(self, actor_id: str, change_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "change.write")
        with transaction(self.connection, immediate=True):
            change_row = self._load_pending_change(change_id)
            order_id = change_row["order_id"]
            self.connection.execute(
                "UPDATE engineering_changes SET state='rejected' WHERE change_id=?",
                (change_id,),
            )
            # 评估产生的修订保留在历史中，但订单回到当前承诺修订。
            started = self.connection.execute(
                "SELECT COUNT(1) count FROM unit_states WHERE order_id=? AND state IN ({})".format(
                    ",".join("?" for _ in STARTED_UNIT_STATES)
                ),
                [order_id, *STARTED_UNIT_STATES],
            ).fetchone()["count"]
            self.connection.execute(
                "UPDATE orders SET state=?, updated_at=? WHERE order_id=?",
                ("in_production" if started else "confirmed", self._now(), order_id),
            )
            self._audit("change", change_id, "change.rejected", actor_id, {"reason": reason[:512]})
        return {"change_id": change_id, "state": "rejected"}

    def _swap_reservations(
        self,
        order_id: str,
        new_revision: int,
        plan: Mapping[str, Any],
        fixed_numbers: set[int],
    ) -> None:
        self.connection.execute(
            "UPDATE component_reservations SET state='released' "
            "WHERE order_id=? AND state='held' AND unit_no NOT IN ({})".format(
                ",".join(str(n) for n in fixed_numbers) if fixed_numbers else "SELECT 0 WHERE 0"
            ),
            (order_id,),
        )
        self.connection.executemany(
            "INSERT INTO component_reservations(order_id,plan_revision,unit_no,requirement_part_id,"
            "batch_id,part_id,rule_id,quantity,state,created_at) VALUES(?,?,?,?,?,?,?,?,'held',?)",
            [
                (
                    order_id,
                    new_revision,
                    row["unit_no"],
                    row["requirement_part_id"],
                    row["batch_id"],
                    row["part_id"],
                    row["rule_id"],
                    row["quantity"],
                    self._now(),
                )
                for row in plan["reservations"]
                if int(row["unit_no"]) not in fixed_numbers
            ],
        )

    # ------------------------------------------------------ 回退与差异

    def compare_revisions(self, actor_id: str, order_id: str, from_revision: int, to_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.read")
        before = json.loads(self._revision_row(order_id, from_revision)["plan_json"])
        after = json.loads(self._revision_row(order_id, to_revision)["plan_json"])
        before["revision"] = from_revision
        after["revision"] = to_revision
        return {"order_id": order_id, "diff": diff_plans(before, after)}

    def rollback_plan(self, actor_id: str, order_id: str, target_revision: int) -> dict[str, Any]:
        self._require(actor_id, "rollback.run")
        with transaction(self.connection, immediate=True):
            order_row = self._load_order(order_id)
            if order_row["state"] not in ("confirmed", "in_production"):
                raise InvalidState(f"订单状态 {order_row['state']} 不能回退；待决变更请先应用或驳回")
            target_row = self._revision_row(order_id, target_revision)
            target_plan = json.loads(target_row["plan_json"])
            current = int(order_row["current_plan_revision"])
            if target_revision >= current:
                raise ValidationFailed("只能回退到早于当前承诺的修订")
            fixed = self._fixed_units(order_id)
            planned_numbers = [n for n in range(1, int(order_row["quantity"]) + 1) if n not in fixed]
            context = self._catalog_context(order_row)
            busy, busy_sources = self._line_busy(order_id, set(fixed))
            restored = restore_order_plan(
                target=target_plan,
                planned_unit_numbers=planned_numbers,
                fixed=fixed,
                as_of=self._today(),
                parts=context["parts"],
                batches=context["batches"],
                availability=self._availability(order_id, set(planned_numbers)),
                busy=busy,
                busy_sources=busy_sources,
                lines=context["lines"],
                gates=context["gates"],
                window=context["window"],
                window_competitors=self._window_competitors(order_id, order_row["window_id"]),
            )
            if not restored["feasible"]:
                raise InvalidState("目标修订在当前资源条件下无法恢复")
            new_revision = self._persist_revision(
                order_row,
                restored,
                actor_id,
                change_id=None,
                restored_from=target_revision,
            )
            self._swap_reservations(order_id, new_revision, restored["plan"], set(fixed))
            self.connection.execute("DELETE FROM order_bom WHERE order_id=?", (order_id,))
            self.connection.executemany(
                "INSERT INTO order_bom(order_id,part_id,quantity) VALUES(?,?,?)",
                [(order_id, row["part_id"], row["quantity"]) for row in restored["plan"]["bom"]],
            )
            for unit in restored["plan"]["units"]:
                unit_no = int(unit["unit_no"])
                if unit_no in fixed:
                    continue
                self.connection.execute(
                    "UPDATE unit_states SET revision=?, product_model=?, line_id=?, production_date=?, "
                    "state='planned', started_at=NULL, updated_at=? WHERE order_id=? AND unit_no=?",
                    (
                        new_revision,
                        unit["product_model"],
                        unit["line_id"],
                        unit["production_date"],
                        self._now(),
                        order_id,
                        unit_no,
                    ),
                )
            self.connection.execute(
                "UPDATE orders SET product_model=?, current_plan_revision=?, state=?, updated_at=? WHERE order_id=?",
                (
                    restored["plan"]["product_model"],
                    new_revision,
                    "in_production" if fixed else "confirmed",
                    self._now(),
                    order_id,
                ),
            )
            before_plan = json.loads(self._revision_row(order_id, current)["plan_json"])
            after_plan = json.loads(self._revision_row(order_id, new_revision)["plan_json"])
            before_plan["revision"] = current
            after_plan["revision"] = new_revision
            comparison = diff_plans(before_plan, after_plan)
            self._audit("order", order_id, "order.rolled_back", actor_id, {
                "from_revision": current,
                "target_revision": target_revision,
                "new_revision": new_revision,
                "locked_units": sorted(fixed),
            })
        return {
            "order_id": order_id,
            "state": "in_production" if fixed else "confirmed",
            "new_revision": new_revision,
            "restored_from": target_revision,
            "locked_unit_numbers": sorted(fixed),
            "diff": comparison,
            "plan": restored["plan"],
            "issues": restored["issues"],
        }
