"""DeterministicAnalysisEngine 单元测试 —— 使用临时 CSV 验证全部 6 类分析证据。"""
import csv
import json
import os
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datahelp.analysis_contract import AnalysisResult
from datahelp.analysis_engine import DeterministicAnalysisEngine


# ── 测试用 CSV 样本 ────────────────────────────────────────────

SAMPLE_CSV = """name,age,score,city,joined_date
Alice,30,95.5,New York,2024-01-15
Bob,25,87.0,New York,2024-02-20
Charlie,35,,London,2024-01-10
Diana,28,92.3,London,2024-03-05
Eve,30,88.8,Paris,2024-02-28
Frank,32,,Paris,2024-04-01
Grace,29,91.0,New York,2024-01-22
Henry,31,85.5,London,2024-03-15
Iris,27,94.2,Paris,2024-02-10
Jack,30,89.5,New York,2024-04-12
Kevin,,78.0,London,2024-05-01
Lucy,26,95.0,New York,2024-05-20"""


class TestDeterministicAnalysisEngine(unittest.TestCase):
    """引擎核心功能测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.csv_path = os.path.join(self.tmpdir, "test_data.csv")
        with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
            f.write(SAMPLE_CSV)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ── 基本功能 ────────────────────────────────────────────────

    def test_run_returns_analysis_result(self):
        """run() 返回 AnalysisResult 实例。"""
        result = DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        self.assertIsInstance(result, AnalysisResult)

    def test_run_basic_metadata(self):
        """行数、列数、列名与 CSV 一致。"""
        result = DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        self.assertEqual(result.row_count, 12)
        self.assertEqual(result.column_count, 5)
        self.assertIn("name", result.column_names)
        self.assertIn("score", result.column_names)

    def test_run_generated_at_is_set(self):
        """generated_at 为空字符串（确定性输出）。"""
        result = DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        self.assertEqual(result.generated_at, "")
    # ── 输出文件存在 ────────────────────────────────────────────

    def test_json_output_exists(self):
        """应生成 analysis_evidence.json。"""
        DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        json_path = os.path.join(self.tmpdir, "analysis_evidence.json")
        self.assertTrue(os.path.exists(json_path))

    def test_md_output_exists(self):
        """应生成 analysis_evidence.md。"""
        DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        md_path = os.path.join(self.tmpdir, "analysis_evidence.md")
        self.assertTrue(os.path.exists(md_path))

    def test_json_is_valid_analysis_result(self):
        """JSON 可反序列化为 AnalysisResult。"""
        DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        json_path = os.path.join(self.tmpdir, "analysis_evidence.json")
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        result = AnalysisResult.from_dict(data)
        self.assertIsInstance(result, AnalysisResult)
        self.assertEqual(result.row_count, 12)

    # ── 6 类 Plan ───────────────────────────────────────────────

    def test_has_profile_plan(self):
        """结果包含 profile plan。"""
        result = DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        plan_ids = [p.plan_id for p in result.plans]
        self.assertIn("profile", plan_ids)

    def test_profile_plan_has_row_count_evidence(self):
        """profile plan 包含 row_count 证据。"""
        result = DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        profile = [p for p in result.plans if p.plan_id == "profile"][0]
        metrics = [e.metric_name for e in profile.evidence]
        self.assertIn("row_count", metrics)
        row_ev = [e for e in profile.evidence if e.metric_name == "row_count"][0]
        self.assertEqual(row_ev.value, 12)

    def test_has_missing_plan(self):
        """结果包含 missing plan。"""
        result = DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        plan_ids = [p.plan_id for p in result.plans]
        self.assertIn("missing", plan_ids)

    def test_missing_plan_counts(self):
        """缺失值统计正确。"""
        result = DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        missing = [p for p in result.plans if p.plan_id == "missing"][0]
        ev = [e for e in missing.evidence if e.metric_name == "total_missing_cells"][0]
        # Kevin age (row 11), Charlie score (row 3), Frank score (row 6) = 3
        self.assertEqual(ev.value, 3)

    def test_has_duplicates_plan(self):
        """结果包含 duplicates plan。"""
        result = DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        plan_ids = [p.plan_id for p in result.plans]
        self.assertIn("duplicates", plan_ids)

    def test_duplicates_zero(self):
        """当前样本无重复行。"""
        result = DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        dup = [p for p in result.plans if p.plan_id == "duplicates"][0]
        ev = [e for e in dup.evidence if e.metric_name == "duplicate_row_count"][0]
        self.assertEqual(ev.value, 0)

    def test_duplicates_detected(self):
        """插入重复行后应检测到重复。"""
        dup_csv = os.path.join(self.tmpdir, "dup_data.csv")
        with open(dup_csv, "w", newline="", encoding="utf-8") as f:
            f.write(SAMPLE_CSV + "\nAlice,30,95.5,New York,2024-01-15")
        result = DeterministicAnalysisEngine.run(dup_csv, self.tmpdir)
        dup = [p for p in result.plans if p.plan_id == "duplicates"][0]
        ev = [e for e in dup.evidence if e.metric_name == "duplicate_row_count"][0]
        self.assertEqual(ev.value, 1)

    def test_has_numeric_stats_plan(self):
        """结果包含 numeric_stats plan。"""
        result = DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        plan_ids = [p.plan_id for p in result.plans]
        self.assertIn("numeric_stats", plan_ids)

    def test_numeric_stats_has_age_and_score(self):
        """numeric_stats 包含 age 和 score 两列。"""
        result = DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        ns = [p for p in result.plans if p.plan_id == "numeric_stats"][0]
        col_ev = [e for e in ns.evidence if e.metric_name == "numeric_columns"][0]
        self.assertIn("age", col_ev.value)
        self.assertIn("score", col_ev.value)

    def test_numeric_stats_values_reasonable(self):
        """数值统计量合理。"""
        result = DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        ns = [p for p in result.plans if p.plan_id == "numeric_stats"][0]
        # 找 score 的 stat
        stat_ev = [e for e in ns.evidence if e.metric_name == "stat::score"][0]
        stats = stat_ev.value
        self.assertIsInstance(stats, dict)
        self.assertIn("mean", stats)
        self.assertAlmostEqual(stats["mean"], 89.68, places=2)
        self.assertIn("min", stats)
        self.assertEqual(stats["min"], 78.0)
        self.assertIn("max", stats)
        self.assertEqual(stats["max"], 95.5)

    def test_has_categorical_plan(self):
        """结果包含 categorical plan。"""
        result = DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        plan_ids = [p.plan_id for p in result.plans]
        self.assertIn("categorical", plan_ids)

    def test_categorical_has_city(self):
        """categorical plan 应识别 city 列。"""
        result = DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        cat = [p for p in result.plans if p.plan_id == "categorical"][0]
        col_ev = [e for e in cat.evidence if e.metric_name == "categorical_columns"][0]
        self.assertIn("city", col_ev.value)

    def test_categorical_value_counts(self):
        """city 列 value_counts 正确。"""
        result = DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        cat = [p for p in result.plans if p.plan_id == "categorical"][0]
        vc_ev = [e for e in cat.evidence if e.metric_name == "value_counts::city"][0]
        counts = vc_ev.value
        self.assertEqual(counts["New York"], 5)  # Alice, Bob, Grace, Jack, Lucy
        self.assertEqual(counts["London"], 4)    # Charlie, Diana, Henry, Kevin
        self.assertEqual(counts["Paris"], 3)     # Eve, Frank, Iris

    def test_has_date_trend_plan(self):
        """结果包含 date_trend plan。"""
        result = DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        plan_ids = [p.plan_id for p in result.plans]
        self.assertIn("date_trend", plan_ids)

    def test_date_trend_monthly_counts(self):
        """joined_date 按月统计正确。"""
        result = DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        dt = [p for p in result.plans if p.plan_id == "date_trend"][0]
        trend_ev = [e for e in dt.evidence if e.metric_name == "monthly_trend::joined_date"]
        self.assertTrue(len(trend_ev) > 0)
        # 合并所有月度证据
        monthly = {}
        for ev in trend_ev:
            if isinstance(ev.value, dict):
                monthly.update(ev.value)
        # 应该有 2024-01, 2024-02, 2024-03, 2024-04, 2024-05
        self.assertIn("2024-01", monthly)
        self.assertEqual(monthly["2024-01"], 3)  # Alice, Charlie, Grace
        self.assertEqual(monthly["2024-05"], 2)  # Kevin, Lucy

    def test_non_date_column_not_misidentified(self):
        """字符串列不应误判为日期列。"""
        csv = os.path.join(self.tmpdir, "non_date.csv")
        with open(csv, "w", newline="", encoding="utf-8") as f:
            f.write("id,label\n1,hello\n2,world\n3,foo\n4,bar\n5,baz\n6,misc\n")
        result = DeterministicAnalysisEngine.run(csv, self.tmpdir)
        dt = [p for p in result.plans if p.plan_id == "date_trend"][0]
        col_ev = [e for e in dt.evidence if e.metric_name == "date_columns"]
        # 没有日期列被识别
        self.assertEqual(len(col_ev), 1)
        self.assertEqual(col_ev[0].value, [])

    def test_no_user_warning_from_date_parsing(self):
        """运行分析引擎时不产生 pandas 格式推断 UserWarning。"""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
            date_warnings = [
                x for x in w
                if issubclass(x.category, UserWarning)
                and "Could not infer format" in str(x.message)
            ]
            self.assertEqual(
                len(date_warnings), 0,
                f"发现日期推断 UserWarning: {[str(x.message) for x in date_warnings]}",
            )

    # ── Evidence 合约 ─────────────────────────────────────────────

    def test_every_evidence_has_required_fields(self):
        """每条证据都包含 metric_name, value, formula, source_columns,
        calculation_method。"""
        result = DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        for plan in result.plans:
            for ev in plan.evidence:
                self.assertTrue(len(ev.metric_name) > 0,
                                f"证据缺少 metric_name: {ev}")
                self.assertIsNotNone(ev.value,
                                     f"证据 {ev.metric_name} 的 value 为 None")
                self.assertIsInstance(ev.formula, str,
                                      f"证据 {ev.metric_name} 的 formula 应含 str")
                self.assertIsInstance(ev.source_columns, list,
                                      f"证据 {ev.metric_name} 的 source_columns 应是 list")
                self.assertIsInstance(ev.calculation_method, str,
                                      f"证据 {ev.metric_name} 缺 calculation_method")

    # ── 边界场景 ────────────────────────────────────────────────

    def test_empty_csv_no_rows(self):
        """仅有表头的 CSV 不抛异常，返回 caveat。"""
        empty_csv = os.path.join(self.tmpdir, "empty.csv")
        with open(empty_csv, "w", newline="", encoding="utf-8") as f:
            f.write("a,b,c\n")
        result = DeterministicAnalysisEngine.run(empty_csv, self.tmpdir)
        self.assertEqual(result.row_count, 0)
        self.assertEqual(result.column_count, 3)
        plan_ids = [p.plan_id for p in result.plans]
        self.assertIn("empty", plan_ids)

    def test_non_existent_file_does_not_crash(self):
        """不存在的文件不抛异常，返回 read_error caveat。"""
        fake_path = os.path.join(self.tmpdir, "nonexistent.csv")
        result = DeterministicAnalysisEngine.run(fake_path, self.tmpdir)
        self.assertEqual(result.row_count, 0)
        plan_ids = [p.plan_id for p in result.plans]
        self.assertIn("error", plan_ids)

    def test_non_numeric_csv_produces_caveat(self):
        """全文本无数值列的 CSV 产生 caveat 而非异常。"""
        txt_csv = os.path.join(self.tmpdir, "text_only.csv")
        with open(txt_csv, "w", newline="", encoding="utf-8") as f:
            f.write("label,desc\nfoo,some text\nbar,other text\n")
        result = DeterministicAnalysisEngine.run(txt_csv, self.tmpdir)
        ns = [p for p in result.plans if p.plan_id == "numeric_stats"][0]
        caveats = [e.caveat for e in ns.evidence if e.caveat]
        self.assertTrue(len(caveats) > 0)

    def test_no_date_column_produces_caveat(self):
        """无日期列的 CSV 产生 caveat。"""
        nodate_csv = os.path.join(self.tmpdir, "no_date.csv")
        with open(nodate_csv, "w", newline="", encoding="utf-8") as f:
            f.write("label,desc\nfoo,some text\nbar,other text\nbaz,misc\n")
        result = DeterministicAnalysisEngine.run(nodate_csv, self.tmpdir)
        dt = [p for p in result.plans if p.plan_id == "date_trend"][0]
        caveats = [e.caveat for e in dt.evidence if e.caveat]
        self.assertTrue(len(caveats) > 0)

    # ── Markdown 输出 ────────────────────────────────────────────

    def test_md_contains_evidence_sections(self):
        """Markdown 包含证据板块标题。"""
        DeterministicAnalysisEngine.run(self.csv_path, self.tmpdir)
        md_path = os.path.join(self.tmpdir, "analysis_evidence.md")
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("## 数据集概览", content)
        self.assertIn("## 缺失值分析", content)
        self.assertIn("## 重复行分析", content)
        self.assertIn("## 数值列描述性统计", content)
        self.assertIn("## 分类列聚合", content)
        self.assertIn("## 可解析日期按月趋势", content)
        self.assertIn("row_count", content)
        self.assertIn("missing_count", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
