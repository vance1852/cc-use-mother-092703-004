from __future__ import annotations

import unittest
from pathlib import Path

from manufacturing_delivery.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class AcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["rejected_order"], "order-c")
        self.assertIn("component_shortage", result["rejected_conflict_codes"])
        self.assertIn("substitute_blocked_customer", result["rejected_conflict_codes"])
        self.assertEqual(result["substitute_core"]["rule_id"], "rule-core-alt")
        self.assertEqual(result["change"]["rescheduled"], 1)
        self.assertEqual(result["change"]["locked"][0]["unit_id"], "order-a-U001")
        self.assertTrue(all(item["kind"] == "rolled_back" for item in result["rollback_diff"]))
        self.assertEqual(result["shipment"]["state"], "shipped")
        self.assertTrue(result["audit"]["valid"])
        self.assertEqual(len(result["audit"]["head_hash"]), 64)


if __name__ == "__main__":
    unittest.main()
