from __future__ import annotations

import json
import sqlite3
import unittest

from manufacturing_delivery.api import JsonApplication
from manufacturing_delivery.service import ManufacturingService


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(ManufacturingService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, payload: dict, actor: str = "plan"):
        return self.app.handle(
            "POST", path,
            {"X-Actor-Id": actor, "Content-Type": "application/json"},
            json.dumps(payload).encode("utf-8"),
        )

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_missing_actor(self) -> None:
        response = self.app.handle(
            "POST", "/models", {}, b"{}"
        )
        self.assertEqual(response.status, 422)

    def test_json_error_shape(self) -> None:
        response = self.app.handle(
            "POST", "/models", {"X-Actor-Id": "plan"}, b"not-json"
        )
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_user_and_model_routes(self) -> None:
        user = self._post("/users", {"user_id": "plan", "display_name": "计划员", "role": "planner"}, actor="x")
        # 建用户不需要 actor 头之外的权限
        self.assertEqual(user.status, 201)
        model = self._post("/models", {
            "model_id": "SVT-110", "equipment_type": "transformer", "name": "变压器",
            "bom": {"core": 1}, "preferred_components": {"core": "CORE-X"},
            "routing_hours": {"assembly": 10}, "inspection_gates": ["g1"],
            "accepts_substitutes": False,
        })
        self.assertEqual(model.status, 201, model.body)
        self.assertEqual(model.body["model_id"], "SVT-110")

    def test_forbidden_reports_status(self) -> None:
        self._post("/users", {"user_id": "p1", "display_name": "p", "role": "planner"}, actor="x")
        self._post("/users", {"user_id": "c1", "display_name": "c", "role": "coordinator"}, actor="x")
        response = self._post("/models", {
            "model_id": "SVT-110", "equipment_type": "transformer", "name": "变压器",
            "bom": {"core": 1}, "preferred_components": {"core": "CORE-X"},
            "routing_hours": {"assembly": 10}, "inspection_gates": ["g1"],
        }, actor="c1")
        self.assertEqual(response.status, 403)

    def test_conflict_confirmation_returns_details(self) -> None:
        self._post("/users", {"user_id": "p1", "display_name": "p", "role": "planner"}, actor="x")
        self._post("/users", {"user_id": "q1", "display_name": "q", "role": "quality"}, actor="x")
        self._post("/users", {"user_id": "c1", "display_name": "c", "role": "coordinator"}, actor="x")
        self._post("/models", {
            "model_id": "SVT-110", "equipment_type": "transformer", "name": "变压器",
            "bom": {"core": 1}, "preferred_components": {"core": "CORE-X"},
            "routing_hours": {"assembly": 10}, "inspection_gates": ["g1"],
        }, actor="p1")
        self._post("/shipping-windows", {
            "window_id": "w1", "destination": "衡阳", "opens_on": "2026-10-10",
            "closes_on": "2026-10-30", "capacity_units": 1,
        }, actor="p1")
        order = {
            "order_id": "o1", "customer": "客户", "due_date": "2026-10-20",
            "window_id": "w1", "idempotency_key": "o1-key",
            "items": [{"model_id": "SVT-110", "quantity": 1, "allow_substitutes": False,
                       "required_grades": {}}],
        }
        submitted = self._post("/orders", order, actor="c1")
        self.assertEqual(submitted.status, 201)
        confirmed = self.app.handle(
            "POST", "/orders/o1/confirm", {"X-Actor-Id": "c1"},
            json.dumps({"expected_revision": 1}).encode("utf-8"),
        )
        # 没有部件批次和产线能力，整体确认必然失败并回传冲突明细。
        self.assertEqual(confirmed.status, 409)
        self.assertIn("details", confirmed.body["error"])


if __name__ == "__main__":
    unittest.main()
