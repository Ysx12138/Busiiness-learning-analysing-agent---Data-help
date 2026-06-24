"""ReportOrchestrator 单元测试 —— 使用 FakeModelClient 验证编排核心流程。

测试场景:
  1. 一次合格：模型首轮返回包含全部必需章节的报告
  2. 缺章节后修订：首轮缺章节，修订后补全
  3. 第二次仍不合格：修订后仍缺章节，返回降级报告
  4. 异常降级：模型调用抛出异常，返回降级报告
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datahelp.analysis_contract import (
    AnalysisEvidence,
    AnalysisPlan,
    AnalysisResult,
)
from datahelp.models import ModelClient
from datahelp.report_orchestrator import (
    ReportOrchestrator,
    ReportOutcome,
    _REQUIRED_SECTIONS,
    _check_sections,
    _check_core_findings_structure,
    _build_degraded_report,
    _build_evidence_prompt,
)


# ══════════════════════════════════════════════════════════════════════
# FakeModelClient —— 可控的测试桩
# ══════════════════════════════════════════════════════════════════════

class FakeModelClient(ModelClient):
    """测试用模型客户端，可预置多轮回复或抛出异常。"""

    def __init__(
        self,
        responses: list[str] | None = None,
        raise_on_call: int | None = None,
        raise_exception: Exception | None = None,
    ):
        """
        参数:
            responses:      按调用顺序返回的字符串列表。
                            如果调用次数超过列表长度，重复返回最后一项。
            raise_on_call:  指定第几次调用时抛出异常（从 1 开始计数）。
            raise_exception: 要抛出的异常实例，默认 RuntimeError。
        """
        self._responses = responses or []
        self._raise_on_call = raise_on_call
        self._raise_exception = raise_exception or RuntimeError("Simulated model failure")
        self.call_count = 0
        self.prompts: list[str] = []   # 记录每次调用的完整 prompt

    def complete(self, prompt: str, max_tokens: int = 4096) -> str:
        self.call_count += 1
        self.prompts.append(prompt)

        if self._raise_on_call is not None and self.call_count >= self._raise_on_call:
            raise self._raise_exception

        if not self._responses:
            return "Mock reply。"
        idx = min(self.call_count - 1, len(self._responses) - 1)
        return self._responses[idx]

    @property
    def model_name(self) -> str:
        return "fake-model"


# ══════════════════════════════════════════════════════════════════════
# 测试用样本数据
# ══════════════════════════════════════════════════════════════════════

SAMPLE_EVIDENCE = AnalysisEvidence(
    metric_name="row_count",
    value=100,
    formula="len(df)",
    source_columns=[],
    calculation_method="count",
)

SAMPLE_EVIDENCE_CAVEAT = AnalysisEvidence(
    metric_name="missing_rate::age",
    value=0.05,
    formula="df['age'].isna().sum() / row_count",
    source_columns=["age"],
    calculation_method="missing_rate",
    caveat="age 列存在 5% 缺失。",
)


def make_sample_result() -> AnalysisResult:
    """构建一个包含全部 6 类 plan 的样本 AnalysisResult。"""
    return AnalysisResult(
        input_file="test_data.csv",
        row_count=100,
        column_count=5,
        column_names=["name", "age", "city", "score", "date"],
        plans=[
            AnalysisPlan(
                plan_id="profile",
                description="数据集概览",
                evidence=[
                    SAMPLE_EVIDENCE,
                    AnalysisEvidence(
                        metric_name="column_count",
                        value=5,
                        formula="len(df.columns)",
                        source_columns=[],
                        calculation_method="count",
                    ),
                ],
                created_at="",
            ),
            AnalysisPlan(
                plan_id="missing",
                description="缺失值分析",
                evidence=[
                    AnalysisEvidence(
                        metric_name="total_missing_cells",
                        value=10,
                        formula="df.isna().sum().sum()",
                        source_columns=[],
                        calculation_method="count",
                    ),
                    SAMPLE_EVIDENCE_CAVEAT,
                ],
                created_at="",
            ),
            AnalysisPlan(
                plan_id="duplicates",
                description="重复行分析",
                evidence=[
                    AnalysisEvidence(
                        metric_name="duplicate_row_count",
                        value=2,
                        formula="df.duplicated().sum()",
                        source_columns=[],
                        calculation_method="duplicate_count",
                    ),
                ],
                created_at="",
            ),
            AnalysisPlan(
                plan_id="numeric_stats",
                description="数值列描述性统计",
                evidence=[
                    AnalysisEvidence(
                        metric_name="numeric_columns",
                        value=["age", "score"],
                        formula="df.select_dtypes(include=[np.number]).columns.tolist()",
                        source_columns=[],
                        calculation_method="column_profile",
                    ),
                    AnalysisEvidence(
                        metric_name="stat::age",
                        value={"mean": 35.2, "min": 18, "max": 65},
                        formula="mean / min / max",
                        source_columns=["age"],
                        calculation_method="descriptive_stats",
                    ),
                ],
                created_at="",
            ),
            AnalysisPlan(
                plan_id="categorical",
                description="分类列聚合",
                evidence=[
                    AnalysisEvidence(
                        metric_name="categorical_columns",
                        value=["city", "name"],
                        formula="heuristic",
                        source_columns=[],
                        calculation_method="column_profile",
                    ),
                    AnalysisEvidence(
                        metric_name="value_counts::city",
                        value={"北京": 40, "上海": 35, "广州": 25},
                        formula="df['city'].value_counts()",
                        source_columns=["city"],
                        calculation_method="value_counts",
                    ),
                ],
                created_at="",
            ),
            AnalysisPlan(
                plan_id="date_trend",
                description="可解析日期按月趋势",
                evidence=[
                    AnalysisEvidence(
                        metric_name="date_columns",
                        value=["date"],
                        formula="pd.to_datetime(col, errors='coerce')",
                        source_columns=[],
                        calculation_method="column_profile",
                    ),
                ],
                created_at="",
            ),
            AnalysisPlan(
                plan_id="derived_metrics",
                description="基础派生指标",
                evidence=[
                    AnalysisEvidence(
                        metric_name="derived_profit",
                        value=50000,
                        formula="SUM(revenue) - SUM(cost)",
                        source_columns=["revenue", "cost"],
                        calculation_method="derived_profit",
                        caveat="profit = revenue - cost",
                    ),
                ],
                created_at="",
            ),
        ],
        generated_at="",
    )


# 包含全部 9 个必需章节 + 核心发现含 5 子要素的合格报告
VALID_REPORT = """# 数据分析报告

## 数据概览
数据集包含 100 行、5 列，包括 name、age、city、score、date。

## 数据质量检查
总计 10 个缺失值，缺失率 0.5%。age 列缺失率为 5%。发现 2 行重复数据。

## 基础指标分析
数值列包括 age 和 score。age 均值 35.2，范围 18~65；score 均值 78.5。

## 分组与排名分析
城市分布：北京 40、上海 35、广州 25，北京占比最高。

## 趋势分析
date 列可解析为日期，支持按月趋势观察。

## 核心发现

### 发现 1：数据规模适中
**数据证据**: 数据集包含 100 行 5 列，基于 row_count=100、column_count=5 的证据。
**方法解释**: 使用 count 方法对数据集进行基础概览统计。
**业务含义**: 数据规模适中，覆盖 5 个维度，适合初步探索性分析。
**风险边界**: 样本量仅 100，结论可能不具备统计显著性，需更多数据验证。
**初学者复用**: 使用 `len(df)` 和 `df.columns.tolist()` 快速了解数据规模。

### 发现 2：年龄分布以中年为主
**数据证据**: age 均值 35.2，最小 18，最大 65，基于 stat::age 的描述性统计。
**方法解释**: 使用 describe() 对数值列进行描述性统计，计算均值、最值。
**业务含义**: 用户年龄覆盖面较广，以 30-40 岁中年用户为主，可作为运营重点。
**风险边界**: age 列存在 5% 缺失，均值和范围可能略有偏差。
**初学者复用**: 使用 `df['age'].describe()` 快速查看数值分布。

## 业务建议
1. 建议处理 age 列 5% 缺失值（如中位数填充）。
2. 北京用户最多，可优先在北京开展运营活动。
3. 结合 score 指标进行用户分层，制定差异化策略。

## 分析边界与风险警告
- age 列存在 5% 缺失，可能影响统计结果。
- 样本量 100 较小，结论外推需谨慎。
- profit = revenue - cost，需确保源数据准确性。

## 初学者教学总结
本报告展示了从数据概览到业务建议的完整分析流程。关键步骤包括：检查数据质量、计算基础指标、分组对比、提炼核心发现、提出行动建议。初学者可重点掌握 `df.describe()`、`df['col'].value_counts()`、`df.isna().sum()` 三大基础分析方法。"""

# 仅包含部分章节的不完整报告（缺：分组与排名分析、趋势分析、核心发现、初学者教学总结）
INCOMPLETE_REPORT = """# 数据分析报告

## 数据概览
数据集包含 100 行、5 列。

## 数据质量检查
总计 10 个缺失值。

## 基础指标分析
数值列包括 age 和 score。

## 业务建议
建议处理缺失值。

## 分析边界与风险警告
部分列存在缺失值。
"""

# 修订版（补全了章节，包含全部 9 个 + 核心发现 5 子要素）
REVISED_REPORT = VALID_REPORT


# ══════════════════════════════════════════════════════════════════════
# 单元测试 —— 核心工具函数
# ══════════════════════════════════════════════════════════════════════

class TestCheckSections(unittest.TestCase):
    """验证 _check_sections 工具函数。"""

    def test_all_sections_present(self):
        """包含全部章节时返回空 missing。"""
        present, missing = _check_sections(VALID_REPORT)
        self.assertEqual(len(missing), 0)
        self.assertIn("数据概览", present)

    def test_incomplete_report_has_missing(self):
        """缺章节时正确报告缺失项。"""
        present, missing = _check_sections(INCOMPLETE_REPORT)
        self.assertTrue(
            "分组与排名分析" in missing or "趋势分析" in missing,
            "分组与排名分析或趋势分析应在缺失列表中",
        )

    def test_empty_text_returns_all_missing(self):
        """空文本返回全部章节缺失。"""
        present, missing = _check_sections("")
        self.assertEqual(len(missing), len(_REQUIRED_SECTIONS))


class TestBuildDegradedReport(unittest.TestCase):
    """验证降级报告构建。"""

    def test_contains_required_sections(self):
        """降级报告包含全部 9 个必需章节框架。"""
        result = make_sample_result()
        report = _build_degraded_report(result)
        for section in _REQUIRED_SECTIONS:
            with self.subTest(section=section):
                self.assertIn(section, report)

    def test_contains_caveats_in_limitations(self):
        """分析边界与风险警告章节包含证据中的 caveat。"""
        result = make_sample_result()
        report = _build_degraded_report(result)
        self.assertIn("age 列存在 5% 缺失", report)

    def test_passes_section_check(self):
        """降级报告自身应通过章节检查（使用章节别名映射）。"""
        result = make_sample_result()
        report = _build_degraded_report(result)
        present, missing = _check_sections(report)
        self.assertEqual(len(missing), 0, f"降级报告缺章节: {missing}")

    def test_passes_core_findings_check(self):
        """降级报告通过核心发现结构检查（每个发现含 5 个子要素）。"""
        result = make_sample_result()
        report = _build_degraded_report(result)
        passed, warnings = _check_core_findings_structure(report)
        self.assertTrue(passed, f"核心发现结构检查失败: {warnings}")

    def test_no_empty_data_fallback_phrases(self):
        """降级报告不含"未包含业务建议""自行获取洞察"等空数据回退文本。"""
        result = make_sample_result()
        report = _build_degraded_report(result)
        self.assertNotIn("未包含业务建议", report)
        self.assertNotIn("自行获取洞察", report)


class TestBuildEvidencePrompt(unittest.TestCase):
    """验证证据 prompt 构建。"""

    def test_contains_evidence_values(self):
        """prompt 包含证据关键数值。"""
        result = make_sample_result()
        prompt = _build_evidence_prompt(result)
        self.assertIn("100", prompt)   # row_count
        self.assertIn("10", prompt)    # total_missing_cells
        self.assertIn("北京", prompt)  # value_counts

    def test_contains_caveats(self):
        """prompt 包含 caveat 信息。"""
        result = make_sample_result()
        prompt = _build_evidence_prompt(result)
        self.assertIn("age", prompt)
        self.assertIn("⚠️", prompt)


# ══════════════════════════════════════════════════════════════════════
# 单元测试 —— ReportOrchestrator 核心流程
# ══════════════════════════════════════════════════════════════════════

class TestReportOrchestratorOnePass(unittest.TestCase):
    """一次合格：模型首轮返回完整报告。"""

    def setUp(self):
        self.result = make_sample_result()
        self.client = FakeModelClient(responses=[VALID_REPORT])

    def test_quality_status_standard(self):
        """一次合格时 quality_status='standard'。"""
        outcome = ReportOrchestrator(self.client, self.result).run()
        self.assertEqual(outcome.quality_status, "standard")

    def test_attempts_is_one(self):
        """一次合格时 attempts=1。"""
        outcome = ReportOrchestrator(self.client, self.result).run()
        self.assertEqual(outcome.attempts, 1)

    def test_text_matches_model_output(self):
        """返回文本即模型输出。"""
        outcome = ReportOrchestrator(self.client, self.result).run()
        self.assertEqual(outcome.text, VALID_REPORT)

    def test_no_warnings(self):
        """一次合格时无警告。"""
        outcome = ReportOrchestrator(self.client, self.result).run()
        self.assertEqual(len(outcome.warnings), 0)


class TestReportOrchestratorRevisionSucceeds(unittest.TestCase):
    """缺章节后修订成功：首轮缺章节，修订后补全。"""

    def setUp(self):
        self.result = make_sample_result()
        self.client = FakeModelClient(
            responses=[INCOMPLETE_REPORT, REVISED_REPORT]
        )

    def test_quality_status_standard(self):
        """修订成功后 quality_status='standard'。"""
        outcome = ReportOrchestrator(self.client, self.result).run()
        self.assertEqual(outcome.quality_status, "standard")

    def test_attempts_is_two(self):
        """修订成功时 attempts=2。"""
        outcome = ReportOrchestrator(self.client, self.result).run()
        self.assertEqual(outcome.attempts, 2)

    def test_text_is_revised_report(self):
        """返回文本是修订后的报告。"""
        outcome = ReportOrchestrator(self.client, self.result).run()
        self.assertEqual(outcome.text, REVISED_REPORT)

    def test_warning_about_missing_sections(self):
        """警告中包含首轮缺失章节信息。"""
        outcome = ReportOrchestrator(self.client, self.result).run()
        self.assertTrue(
            any("缺少章节" in w for w in outcome.warnings),
            f"警告应包含缺失章节信息, got: {outcome.warnings}",
        )

    def test_called_model_twice(self):
        """模型被调用两次。"""
        ReportOrchestrator(self.client, self.result).run()
        self.assertEqual(self.client.call_count, 2)


class TestReportOrchestratorBothFail(unittest.TestCase):
    """第二次仍不合格：修订后仍缺章节，返回降级报告。"""

    def setUp(self):
        self.result = make_sample_result()
        # 首轮和第二轮都返回不完整的报告
        self.client = FakeModelClient(
            responses=[INCOMPLETE_REPORT, INCOMPLETE_REPORT]
        )

    def test_quality_status_degraded(self):
        """修订仍不合格时 quality_status='degraded'。"""
        outcome = ReportOrchestrator(self.client, self.result).run()
        self.assertEqual(outcome.quality_status, "degraded")

    def test_attempts_is_two(self):
        """尝试了两次。"""
        outcome = ReportOrchestrator(self.client, self.result).run()
        self.assertEqual(outcome.attempts, 2)

    def test_text_is_degraded_report(self):
        """返回文本是降级报告。"""
        outcome = ReportOrchestrator(self.client, self.result).run()
        expected = _build_degraded_report(self.result)
        self.assertEqual(outcome.text, expected)

    def test_warning_about_revision_still_missing(self):
        """警告包含修订后仍缺失的信息。"""
        outcome = ReportOrchestrator(self.client, self.result).run()
        self.assertTrue(
            any("修订后仍有问题" in w or "修订后仍缺少" in w for w in outcome.warnings),
            f"警告应包含\"修订后仍有问题\"或\"修订后仍缺少\", got: {outcome.warnings}",
        )


class TestReportOrchestratorExceptionDegraded(unittest.TestCase):
    """异常降级：模型调用抛出异常，返回降级报告。"""

    def setUp(self):
        self.result = make_sample_result()
        self.client = FakeModelClient(
            responses=[VALID_REPORT],
            raise_on_call=1,
            raise_exception=RuntimeError("API connection failed"),
        )

    def test_quality_status_degraded(self):
        """异常时 quality_status='degraded'。"""
        outcome = ReportOrchestrator(self.client, self.result).run()
        self.assertEqual(outcome.quality_status, "degraded")

    def test_attempts_is_one(self):
        """只尝试了一次。"""
        outcome = ReportOrchestrator(self.client, self.result).run()
        self.assertEqual(outcome.attempts, 1)

    def test_text_is_degraded_report(self):
        """返回文本是降级报告。"""
        outcome = ReportOrchestrator(self.client, self.result).run()
        expected = _build_degraded_report(self.result)
        self.assertEqual(outcome.text, expected)

    def test_warning_about_exception(self):
        """警告包含异常信息。"""
        outcome = ReportOrchestrator(self.client, self.result).run()
        self.assertTrue(
            any("模型调用异常" in w for w in outcome.warnings),
            f"警告应包含\"模型调用异常\", got: {outcome.warnings}",
        )


class TestReportOrchestratorRevisionExceptionDegraded(unittest.TestCase):
    """修订时异常：首轮缺章节，修订调用抛出异常，返回降级报告。"""

    def setUp(self):
        self.result = make_sample_result()
        self.client = FakeModelClient(
            responses=[INCOMPLETE_REPORT, VALID_REPORT],
            raise_on_call=2,
            raise_exception=RuntimeError("Revision timed out"),
        )

    def test_quality_status_degraded(self):
        """修订异常时 quality_status='degraded'。"""
        outcome = ReportOrchestrator(self.client, self.result).run()
        self.assertEqual(outcome.quality_status, "degraded")

    def test_attempts_is_two(self):
        """尝试了两次。"""
        outcome = ReportOrchestrator(self.client, self.result).run()
        self.assertEqual(outcome.attempts, 2)

    def test_warning_about_revision_exception(self):
        """警告包含修订异常信息。"""
        outcome = ReportOrchestrator(self.client, self.result).run()
        self.assertTrue(
            any("修订模型调用异常" in w for w in outcome.warnings),
            f"警告应包含\"修订模型调用异常\", got: {outcome.warnings}",
        )


class TestReportOrchestratorEdgeCases(unittest.TestCase):
    """边缘场景测试。"""

    def test_empty_result_does_not_crash(self):
        """空 AnalysisResult 不崩溃，返回降级报告。"""
        empty_result = AnalysisResult()
        client = FakeModelClient(responses=[""])
        # 空文本会导致全部章节缺失
        outcome = ReportOrchestrator(client, empty_result).run()
        self.assertEqual(outcome.quality_status, "degraded")

    def test_audit_mode_prompt_different(self):
        """audit_report 模式生成不同的 system prompt。"""
        result = make_sample_result()
        client = FakeModelClient(responses=[VALID_REPORT])

        orchestrator = ReportOrchestrator(client, result, mode="audit_report")
        outcome = orchestrator.run()

        # audit 模式应在 prompt 中包含"审计"
        self.assertIn("审计", client.prompts[0])
        self.assertEqual(outcome.quality_status, "standard")


# ══════════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
