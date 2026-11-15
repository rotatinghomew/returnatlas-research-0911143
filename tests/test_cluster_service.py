import json
import tempfile
import unittest
from pathlib import Path

from returnatlas.service import Application


class ClusterEndpointTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.application = Application("data", Path(self.workspace.name) / "state")

    def test_scan_endpoint_returns_report(self):
        status, payload = self.application.dispatch(
            "GET", "/clusters/scan?mc=39&realizations=5")
        self.assertEqual(status, 200)
        report = payload["report"]
        self.assertEqual(report["counting_units"]["exposure_unit"], "已完成配送订单")
        self.assertEqual(report["counting_units"]["return_orders"], 99)
        self.assertEqual(report["counting_units"]["contact_records"], 116)
        self.assertIn("search_space", report)
        self.assertIn("coverage", report)
        self.assertIn("method", report)
        self.assertEqual(payload["source_fingerprint"],
                         self.application.catalog.fingerprint)

    def test_scan_endpoint_validates_parameters(self):
        status, payload = self.application.dispatch("GET", "/clusters/scan?scenario=bogus")
        self.assertEqual(status, 400)
        status, _ = self.application.dispatch("GET", "/clusters/scan?mc=-1")
        self.assertEqual(status, 400)
        status, _ = self.application.dispatch("GET", "/clusters/scan?week_from=2026-09")
        self.assertEqual(status, 400)

    def test_scan_endpoint_applies_species_filter(self):
        status, payload = self.application.dispatch(
            "GET", "/clusters/scan?species=鱼&mc=19&realizations=0")
        self.assertEqual(status, 200)
        strata = payload["report"]["strata"]
        self.assertTrue(all(row["species"] == "鱼" for row in strata))

    def test_review_round_trip_and_integrity(self):
        _, scanned = self.application.dispatch(
            "GET", "/clusters/scan?mc=19&realizations=5")
        status, created = self.application.dispatch("POST", "/cluster-reviews", {
            "label": "东南部复核线索", "report": scanned["report"],
            "notes": "等待主管复核"})
        self.assertEqual(status, 201)
        self.assertEqual(created["kind"], "spatial_cluster_review")
        self.assertEqual(created["source_fingerprint"],
                         self.application.catalog.fingerprint)
        # 证据回链包含报告引用的订单与位置
        evidence = created["evidence"]
        referenced = set()
        for cluster in scanned["report"]["clusters"]:
            referenced.update(cluster["exposure_order_ids"])
        if referenced:
            self.assertEqual({row["id"] for row in evidence["deliveries"]}, referenced)
        status, listing = self.application.dispatch("GET", "/cluster-reviews")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in listing["reviews"]], [created["id"]])
        status, restored = self.application.dispatch(
            "GET", "/cluster-reviews/" + created["id"])
        self.assertEqual(status, 200)
        self.assertEqual(restored, created)

    def test_review_rejects_bad_payload(self):
        status, _ = self.application.dispatch("POST", "/cluster-reviews",
                                              {"label": "缺报告"})
        self.assertEqual(status, 400)
        status, _ = self.application.dispatch("POST", "/cluster-reviews",
                                              {"label": "x", "report": {}, "extra": 1})
        self.assertEqual(status, 400)
        status, _ = self.application.dispatch("GET", "/cluster-reviews/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
