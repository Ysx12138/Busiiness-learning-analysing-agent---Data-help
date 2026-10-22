"""V3 核心的回归测试：画像、计划、计算、报告。

这些测试不依赖模型，全部跑确定性路径。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from datahelp.app import analyze, plan_with_model
from datahelp.narrative import SECTIONS, render_from_evidence, validate_sections
from datahelp.plan import AnalysisPlan, PlanError, default_plan, execute_plan
from datahelp.profile import build_profile

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample_sales.csv"


def _write_csv(directory: str, body: str) -> str:
    path = Path(directory) / "toy.csv"
    path.write_text(body, encoding="utf-8")
    return str(path)


class TestProfile(unittest.TestCase):
    def setUp(self):
        self.profile = build_profile(str(SAMPLE))

    def test_id_column_is_not_categorical(self):
        order_id = next(c for c in self.profile.columns if c.name == "order_id")
        self.assertEqual(order_id.kind, "id")
        self.assertNotIn("order_id", [c.name for c in self.profile.categorical_columns])

    def test_date_column_is_not_categorical(self):
        date_col = next(c for c in self.profile.columns if c.name == "order_date")
        self.assertEqual(date_col.kind, "datetime")
        self.assertNotIn("order_date", [c.name for c in self.profile.categorical_columns])

    def test_discount_is_ratio_not_quantity(self):
        """discount 里含 count，不能被当成数量。"""
        discount = next(c for c in self.profile.columns if c.name == "discount")
        self.assertEqual(discount.unit_guess, "ratio")

    def test_missing_counts_match_data(self):
        satisfaction = next(c for c in self.profile.columns if c.name == "satisfaction")
        self.assertGreater(satisfaction.missing, 0)
        self.assertAlmostEqual(
            satisfaction.missing_rate, satisfaction.missing / self.profile.rows, places=4
        )


class TestPlan(unittest.TestCase):
    def setUp(self):
        self.profile = build_profile(str(SAMPLE))
        self.plan = default_plan(self.profile)

    def test_default_plan_derives_revenue(self):
        derived = [m for m in self.plan.metrics if m.kind == "derived"]
        self.assertTrue(derived, "应当从单价×数量×折扣推出营收")
        self.assertEqual(derived[0].params["name"], "revenue")

    def test_groups_use_revenue_not_raw_price(self):
        groups = [m for m in self.plan.metrics if m.kind == "group"]
        self.assertTrue(groups)
        for metric in groups:
            self.assertEqual(metric.params["measure"], "revenue")

    def test_plan_rejects_unknown_operation(self):
        with self.assertRaises(PlanError):
            AnalysisPlan.from_dict({"metrics": [{"id": "x", "kind": "run_anything"}]})

    def test_plan_rejects_empty_metrics(self):
        with self.assertRaises(PlanError):
            AnalysisPlan.from_dict({"metrics": []})


class TestCompute(unittest.TestCase):
    def setUp(self):
        self.profile = build_profile(str(SAMPLE))
        self.plan = default_plan(self.profile)

    def test_every_evidence_has_traceable_formula(self):
        for ev in execute_plan(str(SAMPLE), self.plan):
            if ev.value in (None, {}, []):
                continue
            self.assertTrue(ev.formula, f"{ev.metric} 缺口径")
            self.assertTrue(ev.method, f"{ev.metric} 缺计算方法")

    def test_revenue_group_sums_close_to_total(self):
        evidence = {e.metric: e for e in execute_plan(str(SAMPLE), self.plan)}
        total = evidence["revenue_total"].value
        by_region = evidence["group_region_revenue"].value
        self.assertAlmostEqual(sum(by_region.values()), total, delta=abs(total) * 1e-6)

    def test_compute_is_deterministic(self):
        first = [e.to_dict() for e in execute_plan(str(SAMPLE), self.plan)]
        second = [e.to_dict() for e in execute_plan(str(SAMPLE), self.plan)]
        self.assertEqual(first, second)

    def test_unknown_column_degrades_to_caveat(self):
        plan = AnalysisPlan(
            question="t",
            metrics=[__import__("datahelp.plan", fromlist=["MetricSpec"]).MetricSpec(
                id="bad", kind="summary", title="bad", params={"column": "不存在"})],
        )
        evidence = execute_plan(str(SAMPLE), plan)
        self.assertIn("列不存在", evidence[0].caveat)


class TestNarrative(unittest.TestCase):
    def setUp(self):
        self.profile = build_profile(str(SAMPLE))
        self.plan = default_plan(self.profile)
        self.evidence = execute_plan(str(SAMPLE), self.plan)
        self.report = render_from_evidence(self.profile, self.plan, self.evidence)

    def test_report_contains_all_sections(self):
        check = validate_sections(self.report)
        self.assertTrue(check.ok, check.summary())

    def test_validator_catches_missing_section(self):
        broken = self.report.replace("## 核心发现", "## 别的")
        check = validate_sections(broken)
        self.assertFalse(check.ok)
        self.assertIn("核心发现", check.missing)

    def test_every_section_has_content(self):
        for title, required in SECTIONS:
            if not required:
                continue
            self.assertIn(f"## {title}", self.report)

    def test_report_cites_formulas(self):
        self.assertIn("口径", self.report)


class TestModelPlanGuard(unittest.TestCase):
    """模型给错列名时必须整份作废，不能算出一堆空。"""

    class FakeClient:
        def __init__(self, payload: str):
            self.payload = payload
            self.model_name = "fake"

        def complete(self, prompt, max_tokens=1024):
            return self.payload

    def test_unknown_column_rejected(self):
        profile = build_profile(str(SAMPLE))
        bad = json.dumps({"metrics": [
            {"id": "a", "kind": "summary", "title": "t", "params": {"column": "sparkles"}}
        ]})
        with self.assertRaises(PlanError):
            plan_with_model(self.FakeClient(bad), profile)

    def test_derived_column_may_be_referenced(self):
        profile = build_profile(str(SAMPLE))
        payload = json.dumps({"metrics": [
            {"id": "rev", "kind": "derived", "title": "营收",
             "params": {"name": "revenue", "factors": [
                 {"col": "unit_price"}, {"col": "quantity"}, {"one_minus": "discount"}]}},
            {"id": "g", "kind": "group", "title": "按地区",
             "params": {"by": "region", "measure": "revenue", "agg": "sum"}},
        ]})
        plan = plan_with_model(self.FakeClient(payload), profile)
        self.assertEqual(len(plan.metrics), 2)

    def test_fenced_json_is_parsed(self):
        profile = build_profile(str(SAMPLE))
        payload = '好的，这是计划：\n```json\n{"metrics": [{"id": "a", "kind": "duplicates", "title": "重复"}]}\n```'
        plan = plan_with_model(self.FakeClient(payload), profile)
        self.assertEqual(plan.metrics[0].kind, "duplicates")


class TestAnalyzeEndToEnd(unittest.TestCase):
    def test_mock_run_produces_all_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome = analyze(str(SAMPLE), tmp, provider="mock")
            self.assertEqual(outcome.status, "completed")
            self.assertTrue(outcome.report_ok)
            for key in ("profile", "plan", "evidence", "report"):
                self.assertTrue(Path(outcome.artifacts[key]).exists(), key)
            self.assertTrue((Path(tmp) / "run_log.json").exists())

    def test_unreadable_file_fails_honestly(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.csv"
            bad.write_bytes(b"\x00\xff\xfe not a table")
            outcome = analyze(str(bad), str(Path(tmp) / "out"), provider="mock")
            self.assertEqual(outcome.status, "failed")
            self.assertIn("无法读取", outcome.error)

    def test_empty_but_valid_csv_reports_what_it_can(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv = _write_csv(tmp, "a,b\n1,2\n3,4\n")
            outcome = analyze(csv, str(Path(tmp) / "out"), provider="mock")
            self.assertEqual(outcome.status, "completed")
            evidence = json.loads(Path(outcome.artifacts["evidence"]).read_text())
            metrics = [e["metric"] for e in evidence["evidence"]]
            self.assertIn("duplicates", metrics)


if __name__ == "__main__":
    unittest.main()
