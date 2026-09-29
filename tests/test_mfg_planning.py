from __future__ import annotations

import unittest
from decimal import Decimal

from manufacturing_delivery.planning import (
    Ledger,
    commitment_view,
    diff_unit_commitments,
    grade_rank,
    plan_unit,
)


MODEL = {
    "model_id": "SVT-110",
    "equipment_type": "transformer",
    "name": "变压器",
    "bom": {"core": 1, "bushing": 2},
    "preferred_components": {"core": "CORE-S90", "bushing": "BUSH-110"},
    "routing_hours": {"winding_shop": Decimal("40"), "assembly": Decimal("30")},
    "inspection_gates": ["routine_test"],
    "accepts_substitutes": True,
}


def lot(lot_id: str, model: str, category: str, grade_rank_: int, qty: int, state: str = "released") -> dict:
    return {
        "lot_id": lot_id,
        "component_model": model,
        "category": category,
        "quality_grade": chr(ord("A") + grade_rank_ - 1),
        "grade_rank": grade_rank_,
        "quantity": qty,
        "held_qty": 0,
        "consumed_qty": 0,
        "received_at": f"2026-09-{20 + len(lot_id):02d}T00:00:00Z",
        "quality_state": state,
    }


RULE = {
    "rule_id": "rule-core-alt",
    "equipment_type": "transformer",
    "category": "core",
    "preferred_model": "CORE-S90",
    "substitute_model": "CORE-S90B",
    "allow_customer_override": 1,
    "minimum_grade_rank": 2,
    "active": 1,
}


def cap(station: str, day: str, hours: str, booked: str = "0") -> dict:
    return {
        "line_id": "L1",
        "station": station,
        "service_date": day,
        "available_hours": hours,
        "booked_hours": booked,
    }


CAPACITIES = {
    "winding_shop": [cap("winding_shop", "2026-10-01", "80")],
    "assembly": [cap("assembly", "2026-10-03", "80")],
}


class PlanningTests(unittest.TestCase):
    def test_grade_rank(self) -> None:
        self.assertEqual(grade_rank("a"), 1)
        self.assertEqual(grade_rank("C"), 3)
        with self.assertRaises(ValueError):
            grade_rank("AA")

    def test_preferred_lot_selected_without_rule(self) -> None:
        lots = [
            lot("lot-core-1", "CORE-S90", "core", 1, 1),
            lot("lot-bush-1", "BUSH-110", "bushing", 1, 2),
        ]
        result = plan_unit(
            unit_seq=1, model=MODEL, preferred_components=MODEL["preferred_components"],
            allow_substitutes=False, required_grades={}, lots=lots, rules=[],
            capacities=CAPACITIES, ledger=Ledger.empty(), earliest_on="2026-09-29",
            due_date="2026-10-20", design_revision="BASE",
        )
        core = next(c for c in result["components"] if c["category"] == "core")
        self.assertFalse(core["is_substitute"])
        self.assertEqual(result["conflicts"], [])

    def test_quarantined_lot_is_not_candidate(self) -> None:
        lots = [lot("lot-core-1", "CORE-S90", "core", 1, 1, state="quarantined")]
        result = plan_unit(
            unit_seq=1, model=MODEL, preferred_components=MODEL["preferred_components"],
            allow_substitutes=False, required_grades={}, lots=lots, rules=[RULE],
            capacities=CAPACITIES, ledger=Ledger.empty(), earliest_on="2026-09-29",
            due_date="2026-10-20", design_revision="BASE",
        )
        codes = {c["code"] for c in result["conflicts"]}
        self.assertIn("component_shortage", codes)

    def test_substitute_requires_customer_permission(self) -> None:
        lots = [
            lot("lot-core-alt", "CORE-S90B", "core", 2, 1),
            lot("lot-bush-1", "BUSH-110", "bushing", 1, 2),
        ]
        result = plan_unit(
            unit_seq=1, model=MODEL, preferred_components=MODEL["preferred_components"],
            allow_substitutes=False, required_grades={}, lots=lots, rules=[RULE],
            capacities=CAPACITIES, ledger=Ledger.empty(), earliest_on="2026-09-29",
            due_date="2026-10-20", design_revision="BASE",
        )
        self.assertNotIn("components", [c["category"] for c in result["components"]])
        self.assertIn("substitute_blocked_customer", {c["code"] for c in result["conflicts"]})
        self.assertFalse(any(c["category"] == "core" for c in result["components"]))

    def test_substitute_accepted_when_customer_and_quality_pass(self) -> None:
        lots = [
            lot("lot-core-alt", "CORE-S90B", "core", 2, 1),
            lot("lot-bush-1", "BUSH-110", "bushing", 1, 2),
        ]
        result = plan_unit(
            unit_seq=1, model=MODEL, preferred_components=MODEL["preferred_components"],
            allow_substitutes=True, required_grades={"core": "B"}, lots=lots, rules=[RULE],
            capacities=CAPACITIES, ledger=Ledger.empty(), earliest_on="2026-09-29",
            due_date="2026-10-20", design_revision="BASE",
        )
        core = next(c for c in result["components"] if c["category"] == "core")
        self.assertTrue(core["is_substitute"])
        self.assertEqual(core["rule_id"], "rule-core-alt")

    def test_substitute_rejected_when_grade_below_rule_floor(self) -> None:
        rule = dict(RULE, minimum_grade_rank=1)  # 规则要求 A 级
        lots = [
            lot("lot-core-alt", "CORE-S90B", "core", 2, 1),
            lot("lot-bush-1", "BUSH-110", "bushing", 1, 2),
        ]
        result = plan_unit(
            unit_seq=1, model=MODEL, preferred_components=MODEL["preferred_components"],
            allow_substitutes=True, required_grades={}, lots=lots, rules=[rule],
            capacities=CAPACITIES, ledger=Ledger.empty(), earliest_on="2026-09-29",
            due_date="2026-10-20", design_revision="BASE",
        )
        self.assertIn("substitute_grade_insufficient", {c["code"] for c in result["conflicts"]})

    def test_customer_grade_floor_blocks_higher_grade_substitute(self) -> None:
        lots = [
            lot("lot-core-alt", "CORE-S90B", "core", 2, 1),
            lot("lot-bush-1", "BUSH-110", "bushing", 1, 2),
        ]
        result = plan_unit(
            unit_seq=1, model=MODEL, preferred_components=MODEL["preferred_components"],
            allow_substitutes=True, required_grades={"core": "A"}, lots=lots, rules=[RULE],
            capacities=CAPACITIES, ledger=Ledger.empty(), earliest_on="2026-09-29",
            due_date="2026-10-20", design_revision="BASE",
        )
        self.assertFalse(any(c["category"] == "core" for c in result["components"]))
        self.assertTrue(any(c["code"] == "component_shortage" for c in result["conflicts"]))

    def test_overpromised_lot_blocks_second_unit_in_shared_ledger(self) -> None:
        lots = [
            lot("lot-core-1", "CORE-S90", "core", 1, 1),
            lot("lot-bush-1", "BUSH-110", "bushing", 1, 2),
        ]
        ledger = Ledger.empty()
        kwargs = dict(
            model=MODEL, preferred_components=MODEL["preferred_components"],
            allow_substitutes=False, required_grades={}, lots=lots, rules=[],
            capacities=CAPACITIES, ledger=ledger, earliest_on="2026-09-29",
            due_date="2026-10-20", design_revision="BASE",
        )
        first = plan_unit(unit_seq=1, **kwargs)
        second = plan_unit(unit_seq=2, **kwargs)
        self.assertEqual(len(first["components"]), 2)
        self.assertIn("component_shortage", {c["code"] for c in second["conflicts"]})

    def test_late_completion_raises_delivery_conflict(self) -> None:
        capacities = {
            "winding_shop": [cap("winding_shop", "2026-10-01", "80")],
            "assembly": [cap("assembly", "2026-11-02", "80")],
        }
        lots = [
            lot("lot-core-1", "CORE-S90", "core", 1, 1),
            lot("lot-bush-1", "BUSH-110", "bushing", 1, 2),
        ]
        result = plan_unit(
            unit_seq=1, model=MODEL, preferred_components=MODEL["preferred_components"],
            allow_substitutes=False, required_grades={}, lots=lots, rules=[],
            capacities=capacities, ledger=Ledger.empty(), earliest_on="2026-09-29",
            due_date="2026-10-20", design_revision="BASE",
        )
        self.assertIn("delivery_late", {c["code"] for c in result["conflicts"]})

    def test_diff_detects_component_and_revision_changes(self) -> None:
        before = commitment_view({
            "unit_id": "u1", "unit_seq": 1, "model_id": "SVT-110", "state": "scheduled",
            "design_revision": "BASE", "planned_complete_on": "2026-10-05",
            "components": [
                {"category": "bushing", "component_model": "BUSH-110", "lot_id": "L1", "is_substitute": 0},
            ],
            "schedule": [{"station": "assembly", "line_id": "L1", "service_date": "2026-10-03"}],
        })
        after = dict(before)
        after["design_revision"] = "R2"
        after["components"] = {
            "bushing": {"component_model": "BUSH-126", "lot_id": "L2", "is_substitute": False},
        }
        diff = diff_unit_commitments(before, after)
        fields = {change["field"] for change in diff["changed"]}
        self.assertIn("design_revision", fields)
        self.assertIn("component.bushing", fields)


if __name__ == "__main__":
    unittest.main()
