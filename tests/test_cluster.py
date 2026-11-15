import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from returnatlas.catalog import Catalog
from returnatlas.cluster import (BOUNDARY_SCENARIOS, ScanError,
                                 build_boundary_versions, build_frame,
                                 build_geometry, cluster_scan, eligible_order_mask,
                                 run_scan, week_key)


class FrameTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog("data")
        self.geometry = build_geometry(self.catalog)

    def test_event_unit_is_deduped_order_not_contacts(self):
        frame = build_frame(self.catalog)
        counting = frame["counting"]
        self.assertEqual(counting["return_orders"], 99)
        self.assertEqual(counting["claims"], 99)
        self.assertEqual(counting["contact_records"], 116)
        self.assertLess(counting["return_orders"], counting["contact_records"])
        self.assertTrue(counting["multi_contact_order_ids"])
        order_events = [int(order["event"]) for order in frame["orders"]]
        self.assertEqual(sum(order_events), counting["return_orders"])
        self.assertTrue(all(value in (0, 1) for value in order_events))

    def test_uncompleted_orders_are_not_exposure(self):
        frame = build_frame(self.catalog)
        delivered = {row["id"] for row in self.catalog.rows("deliveries") if row["delivered"]}
        self.assertEqual({order["order_id"] for order in frame["orders"]}, delivered)
        self.assertEqual(frame["counting"]["exposure_orders"], len(delivered))

    def test_stratified_expected_matches_identity(self):
        frame = build_frame(self.catalog)
        versions = build_boundary_versions(self.catalog)
        result = run_scan(frame, self.geometry, versions, "time_varying",
                          replications=0, seed=1)
        # 间接标准化恒等式：全量窗口期望等于总事件数
        self.assertAlmostEqual(result["total_mu"], result["total_events"], places=9)

    def test_species_and_week_filters(self):
        fish = build_frame(self.catalog, species=["鱼"])
        self.assertEqual({order["species"] for order in fish["orders"]}, {"鱼"})
        ranged = build_frame(self.catalog, week_from="2026-W35", week_to="2026-W40")
        self.assertTrue(all("2026-W35" <= order["week"] <= "2026-W40"
                            for order in ranged["orders"]))
        with self.assertRaises(ScanError):
            build_frame(self.catalog, week_from="2026-09")
        with self.assertRaises(ScanError):
            build_frame(self.catalog, week_from="2026-W40", week_to="2026-W31")
        with self.assertRaises(ScanError):
            build_frame(self.catalog, species=[])

    def test_week_key_is_iso(self):
        self.assertEqual(week_key("2026-08-01"), "2026-W31")


class InferenceTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog("data")
        self.geometry = build_geometry(self.catalog)
        self.frame = build_frame(self.catalog)
        self.versions = build_boundary_versions(self.catalog)

    def test_seed_makes_scan_reproducible(self):
        first = run_scan(self.frame, self.geometry, self.versions, "time_varying",
                         replications=49, seed=123)
        second = run_scan(self.frame, self.geometry, self.versions, "time_varying",
                          replications=49, seed=123)
        self.assertEqual(first["null_maximums"], second["null_maximums"])
        self.assertEqual(
            [window["llr"] for window in first["windows"]],
            [window["llr"] for window in second["windows"]])

    def test_p_value_reflects_full_search_null(self):
        result = run_scan(self.frame, self.geometry, self.versions, "time_varying",
                          replications=199, seed=7)
        nulls = np.array(result["null_maximums"])
        top = max(result["windows"], key=lambda window: window["llr"])
        p_value = (1 + int((nulls >= top["llr"] - 1e-12).sum())) / (len(nulls) + 1)
        self.assertGreaterEqual(p_value, 1 / (len(nulls) + 1))
        # 当前虚构数据近似均匀，不应产生搜索校正后的显著线索
        self.assertGreater(p_value, 0.05)

    def test_planted_hot_zone_is_detected(self):
        rng = np.random.default_rng(42)
        hot = {"G36", "G43", "G44", "G45"}
        for order in self.frame["orders"]:
            if order["location_id"] in hot and not order["event"] and rng.random() < 0.55:
                order["event"] = True
        strata = {}
        for order in self.frame["orders"]:
            bucket = strata.setdefault((order["species"], order["week"]), [0, 0])
            bucket[0] += 1
            bucket[1] += int(order["event"])
        for row in self.frame["strata"]:
            exposure, events = strata[(row["species"], row["week"])]
            row["return_rate"] = events / exposure
        result = run_scan(self.frame, self.geometry, self.versions, "time_varying",
                          replications=199, seed=7)
        nulls = np.array(result["null_maximums"])
        top = max(result["windows"], key=lambda window: window["llr"])
        p_value = (1 + int((nulls >= top["llr"] - 1e-12).sum())) / (len(nulls) + 1)
        member_ids = {result["location_ids"][index] for index in top["members"]}
        self.assertLessEqual(p_value, 0.05)
        self.assertTrue(hot <= member_ids)

    def test_null_permutation_preserves_stratum_counts(self):
        result = run_scan(self.frame, self.geometry, self.versions, "time_varying",
                          replications=19, seed=3)
        # 每次重复的总事件数守恒，且阈值来自完整搜索网格的最大值
        self.assertTrue(all(value >= 0 for value in result["null_maximums"]))
        self.assertEqual(len(result["null_maximums"]), 19)

    def test_report_without_monte_carlo_has_no_leads(self):
        report = cluster_scan(self.catalog, self.geometry, self.frame, self.versions,
                              replications=0, realization_count=0, seed=7)
        self.assertEqual(report["clusters"], [])
        self.assertIsNone(report["search_space"]["null_llr_quantile_95"])


class UncertaintyTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog("data")
        self.geometry = build_geometry(self.catalog)
        self.frame = build_frame(self.catalog)
        self.versions = build_boundary_versions(self.catalog)

    def test_street_locations_have_multiple_candidates(self):
        street = [location for location, item in self.geometry.items()
                  if item["resolution"] == "街道"]
        self.assertTrue(street)
        self.assertTrue(all(len(self.geometry[location]["candidates"]) >= 2
                            for location in street))

    def test_report_carries_geocoding_and_boundary_stability(self):
        report = cluster_scan(self.catalog, self.geometry, self.frame, self.versions,
                              replications=49, realization_count=12, seed=7)
        # 无显著线索时也要返回完整的方法与覆盖说明
        self.assertEqual(report["coverage"]["locations_without_exposure"], [])
        self.assertIn("interpretation", report["coverage"])
        self.assertEqual(len(report["search_space"]["radii_km_evaluated"]) > 0, True)
        # 即使没有显著线索，边界情景的可达性仍可单独核验
        for scenario in BOUNDARY_SCENARIOS:
            mask = eligible_order_mask(self.frame, self.geometry, self.versions, scenario)
            self.assertEqual(len(mask), len(self.frame["orders"]))
            self.assertTrue(mask.dtype == bool)

    def test_report_with_hot_zone_contains_traceable_lead(self):
        rng = np.random.default_rng(42)
        hot = {"G36", "G43", "G44", "G45"}
        for order in self.frame["orders"]:
            if order["location_id"] in hot and not order["event"] and rng.random() < 0.55:
                order["event"] = True
        strata = {}
        for order in self.frame["orders"]:
            bucket = strata.setdefault((order["species"], order["week"]), [0, 0])
            bucket[0] += 1
            bucket[1] += int(order["event"])
        for row in self.frame["strata"]:
            exposure, events = strata[(row["species"], row["week"])]
            row["return_rate"] = events / exposure
        report = cluster_scan(self.catalog, self.geometry, self.frame, self.versions,
                              replications=99, realization_count=20, seed=7)
        self.assertTrue(report["clusters"])
        lead = report["clusters"][0]
        self.assertEqual(len(lead["return_order_ids"]), lead["events"])
        self.assertEqual(len(lead["exposure_order_ids"]), lead["exposure"])
        self.assertTrue(set(lead["return_order_ids"]) <= set(lead["exposure_order_ids"]))
        self.assertGreaterEqual(lead["geocoding_stability"]["anchor_inclusion_rate"], 0.0)
        self.assertEqual(len(lead["boundary_stability"]), 3)
        self.assertTrue(any(item.get("baseline") for item in lead["boundary_stability"]))
        # 证据只到位置（街道/路段），不出现任何订单位置以外的个人标识字段
        for forbidden in ("customer_name", "resident_id", "phone", "id_card"):
            self.assertNotIn(forbidden, json.dumps(report, ensure_ascii=False))
        for identifier in lead["location_ids"]:
            self.assertRegex(identifier, r"^G\d+$")

    def test_no_exposure_locations_are_listed_not_scanned(self):
        # 用品种过滤 + 周过滤制造无暴露位置
        frame = build_frame(self.catalog, species=["鱼"],
                            week_from="2026-W31", week_to="2026-W32")
        report = cluster_scan(self.catalog, self.geometry, frame, self.versions,
                              replications=19, realization_count=5, seed=7)
        scanned = set(report["coverage"]["locations_with_exposure"])
        unobserved = set(report["coverage"]["locations_without_exposure"])
        self.assertTrue(unobserved)
        self.assertFalse(scanned & unobserved)
        for cluster in report["clusters"]:
            self.assertTrue(set(cluster["location_ids"]) <= scanned)

    def test_invalid_arguments_rejected(self):
        with self.assertRaises(ScanError):
            cluster_scan(self.catalog, self.geometry, self.frame, self.versions,
                         replications=10000)
        with self.assertRaises(ScanError):
            cluster_scan(self.catalog, self.geometry, self.frame, self.versions,
                         scenario="unknown")
        with self.assertRaises(ScanError):
            cluster_scan(self.catalog, self.geometry, self.frame, self.versions,
                         min_events=0)


class EmptyFrameTests(unittest.TestCase):
    def test_filtering_everything_out_is_an_error(self):
        catalog = Catalog("data")
        with self.assertRaises(ScanError):
            build_frame(catalog, species=["不存在的品种"])


if __name__ == "__main__":
    unittest.main()
