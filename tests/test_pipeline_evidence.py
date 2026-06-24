"""无网络单元测试：pipeline 集成报告编排与证据引擎。

测试目标：
  - 证据文件（analysis_evidence.json / .md）在 work_csv 就绪后被写入 task_dir
  - result 中新增 evidence_status、evidence_files、evidence_error 字段
  - generated_files 中追加证据文件
  - 引擎成功 → ReportOrchestrator 路径，agent.ask 不被调用
  - 引擎成功 → report_quality={status,attempts,warnings} 写入 result
  - 引擎成功 → 编排文本被用于输出报告
  - 引擎异常 → 传统 agent 路径，agent.ask 仍被调用
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


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


def _make_mock_agent():
    """构造 mock agent + run_store，模拟 build_agent 正常返回。"""
    model = MagicMock()
    model.model_name = "mock-model"

    task_state = MagicMock()
    task_state.to_dict.return_value = {
        "status": "completed", "tool_steps": 0, "stop_reason": "finished",
    }
    task_state.status = "completed"
    task_state.stop_reason = "finished"
    task_state.tool_steps = 0
    task_state.final_answer = "分析完成。"

    agent = MagicMock()
    agent.model = model
    agent.task_state = task_state
    agent.history = [{"role": "user", "content": "test"}]
    agent.ask.return_value = "分析完成。"

    run_store = MagicMock()
    return agent, run_store


class TestPipelineEvidenceSuccess(unittest.TestCase):
    """引擎成功路径 —— 证据文件正常生成。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.csv_path = os.path.join(self.tmpdir, "test_data.csv")
        with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
            f.write(SAMPLE_CSV)
        self.output_dir = os.path.join(self.tmpdir, "output")
        os.makedirs(self.output_dir, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ── 辅助 ────────────────────────────────────────────

    def _run(self):
        """mock ReportOrchestrator.run 后执行 run_data_help_analysis。"""
        from datahelp.report_orchestrator import ReportOutcome
        fake_outcome = ReportOutcome(
            text="分析完成。编排报告内容。",
            quality_status="standard",
            attempts=2,
            warnings=[],
        )
        with patch("datahelp.report_orchestrator.ReportOrchestrator.run", return_value=fake_outcome):
            from datahelp.pipeline import run_data_help_analysis
            return run_data_help_analysis(self.csv_path, self.output_dir, provider="mock")

    # ── 证据文件存在性 ──────────────────────────────────

    def test_evidence_json_exists(self):
        """成功时生成 analysis_evidence.json。"""
        result = self._run()
        self.assertTrue(
            os.path.exists(os.path.join(result["output_dir"], "analysis_evidence.json")),
        )

    def test_evidence_md_exists(self):
        """成功时生成 analysis_evidence.md。"""
        result = self._run()
        self.assertTrue(
            os.path.exists(os.path.join(result["output_dir"], "analysis_evidence.md")),
        )

    # ── result 字段 ─────────────────────────────────────

    def test_evidence_status_success(self):
        """evidence_status 为 'success'。"""
        result = self._run()
        self.assertEqual(result.get("evidence_status"), "success")

    def test_evidence_files_list(self):
        """evidence_files 包含两个证据文件名。"""
        result = self._run()
        self.assertIn("evidence_files", result)
        self.assertIn("analysis_evidence.json", result["evidence_files"])
        self.assertIn("analysis_evidence.md", result["evidence_files"])

    def test_evidence_files_in_generated_files(self):
        """证据文件追加到 generated_files。"""
        result = self._run()
        self.assertIn("analysis_evidence.json", result["generated_files"])
        self.assertIn("analysis_evidence.md", result["generated_files"])

    def test_evidence_error_empty_on_success(self):
        """成功时 evidence_error 为空字符串。"""
        result = self._run()
        self.assertEqual(result.get("evidence_error"), "")

    # ── agent 不被调用（ReportOrchestrator 路径） ────────

    def test_agent_ask_not_called(self):
        """证据引擎成功时 agent.ask 不被调用。"""
        from datahelp.report_orchestrator import ReportOutcome
        agent, run_store = _make_mock_agent()
        fake_outcome = ReportOutcome(
            text="分析完成。编排报告内容。",
            quality_status="standard",
            attempts=2,
            warnings=[],
        )
        with patch("datahelp.cli.build_agent", return_value=(agent, run_store)):
            with patch("datahelp.report_orchestrator.ReportOrchestrator.run", return_value=fake_outcome):
                from datahelp.pipeline import run_data_help_analysis
                run_data_help_analysis(self.csv_path, self.output_dir, provider="mock")
        agent.ask.assert_not_called()

    def test_pipeline_status_completed(self):
        """pipeline 最终状态为 completed。"""
        result = self._run()
        self.assertEqual(result.get("status"), "completed")

    def test_final_answer_present(self):
        """result 包含 final_answer。"""
        result = self._run()
        self.assertIn("final_answer", result)
        self.assertTrue(len(result["final_answer"]) > 0)

    # ── 报告质量字段 ────────────────────────────────────

    def test_report_quality_present(self):
        """report_quality 写入 result。"""
        result = self._run()
        self.assertIn("report_quality", result)
        self.assertEqual(result["report_quality"]["status"], "standard")
        self.assertEqual(result["report_quality"]["attempts"], 2)
        self.assertEqual(result["report_quality"]["warnings"], [])

    # ── 编排文本用于输出报告 ────────────────────────────

    def test_final_answer_used_in_report(self):
        """analysis_report.md 包含编排器输出的文本。"""
        result = self._run()
        report_path = os.path.join(result["output_dir"], "analysis_report.md")
        with open(report_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("编排报告内容", content)

    # ── 证据内容合法性 ──────────────────────────────────

    def test_json_evidence_contains_row_count(self):
        """analysis_evidence.json 可解析且包含 row_count。"""
        result = self._run()
        json_path = os.path.join(result["output_dir"], "analysis_evidence.json")
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["row_count"], 12)


class TestPipelineEvidenceFailure(unittest.TestCase):
    """引擎异常路径 —— pipeline 不崩溃，agent 继续执行。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.csv_path = os.path.join(self.tmpdir, "test_data.csv")
        with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
            f.write(SAMPLE_CSV)
        self.output_dir = os.path.join(self.tmpdir, "output")
        os.makedirs(self.output_dir, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_with_engine_error(self):
        """让引擎抛出异常后执行 pipeline。"""
        agent, run_store = _make_mock_agent()
        with patch("datahelp.cli.build_agent", return_value=(agent, run_store)):
            with patch(
                "datahelp.analysis_engine.DeterministicAnalysisEngine.run",
                side_effect=RuntimeError("模拟引擎崩溃"),
            ):
                from datahelp.pipeline import run_data_help_analysis
                return run_data_help_analysis(self.csv_path, self.output_dir)

    # ── 引擎异常字段 ────────────────────────────────────

    def test_evidence_status_failed(self):
        """引擎异常时 evidence_status 为 'failed'。"""
        result = self._run_with_engine_error()
        self.assertEqual(result.get("evidence_status"), "failed")

    def test_evidence_files_empty_on_failure(self):
        """引擎异常时 evidence_files 为空列表。"""
        result = self._run_with_engine_error()
        self.assertEqual(result.get("evidence_files"), [])

    def test_evidence_error_contains_message(self):
        """引擎异常时 evidence_error 包含异常信息。"""
        result = self._run_with_engine_error()
        self.assertIn("evidence_error", result)
        self.assertIn("模拟引擎崩溃", result["evidence_error"])

    # ── pipeline 不崩溃 ─────────────────────────────────

    def test_pipeline_still_completed(self):
        """引擎异常后 pipeline 状态仍为 completed。"""
        result = self._run_with_engine_error()
        self.assertEqual(result.get("status"), "completed")

    def test_agent_ask_still_called(self):
        """引擎异常后 agent.ask 仍被调用。"""
        agent, run_store = _make_mock_agent()
        with patch("datahelp.cli.build_agent", return_value=(agent, run_store)):
            with patch(
                "datahelp.analysis_engine.DeterministicAnalysisEngine.run",
                side_effect=RuntimeError("模拟引擎崩溃"),
            ):
                from datahelp.pipeline import run_data_help_analysis
                run_data_help_analysis(self.csv_path, self.output_dir)
        agent.ask.assert_called_once()

    def test_final_answer_still_present(self):
        """引擎异常后 final_answer 仍存在。"""
        result = self._run_with_engine_error()
        self.assertIn("final_answer", result)
        self.assertTrue(len(result["final_answer"]) > 0)


# ══════════════════════════════════════════════════════════════════════
# 确定性引擎成功 + create_model_client 抛出 RuntimeError
# ══════════════════════════════════════════════════════════════════════

class TestPipelineCreateModelClientError(unittest.TestCase):
    """确定性引擎成功，但 datahelp.pipeline.create_model_client 抛出 RuntimeError。

    预期：
      - status=completed
      - report_quality.status=degraded
      - warnings 包含异常信息
      - analysis_report.md 含降级报告标记
      - datahelp.cli.build_agent 不被调用
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.csv_path = os.path.join(self.tmpdir, "test_data.csv")
        with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
            f.write(SAMPLE_CSV)
        self.output_dir = os.path.join(self.tmpdir, "output")
        os.makedirs(self.output_dir, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self):
        """patch create_model_client → RuntimeError 后执行 pipeline。"""
        with patch("datahelp.pipeline.create_model_client") as mock_create:
            mock_create.side_effect = RuntimeError("模拟创建客户端失败")
            from datahelp.pipeline import run_data_help_analysis
            return run_data_help_analysis(self.csv_path, self.output_dir, provider="mock")

    def test_pipeline_status_completed(self):
        """pipeline 状态为 completed。"""
        result = self._run()
        self.assertEqual(result.get("status"), "completed")

    def test_report_quality_degraded(self):
        """report_quality.status 为 degraded。"""
        result = self._run()
        self.assertEqual(result["report_quality"]["status"], "degraded")

    def test_warnings_contain_exception(self):
        """warnings 包含异常信息。"""
        result = self._run()
        self.assertTrue(
            any("模拟创建客户端失败" in w for w in result["report_quality"]["warnings"]),
            f"warnings 应包含异常信息, got: {result['report_quality']['warnings']}",
        )

    def test_report_contains_degraded_marker(self):
        """analysis_report.md 包含降级报告标记。"""
        result = self._run()
        report_path = os.path.join(result["output_dir"], "analysis_report.md")
        with open(report_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("数据分析报告（降级版）", content)

    def test_build_agent_not_called(self):
        """datahelp.cli.build_agent 不被调用。"""
        with patch("datahelp.pipeline.create_model_client") as mock_create:
            mock_create.side_effect = RuntimeError("模拟创建客户端失败")
            with patch("datahelp.cli.build_agent") as mock_build:
                from datahelp.pipeline import run_data_help_analysis
                run_data_help_analysis(self.csv_path, self.output_dir, provider="mock")
        mock_build.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
