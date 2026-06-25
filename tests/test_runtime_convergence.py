"""无网络单元测试：DataHelp 分析脚本收敛行为。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datahelp.runtime import DataHelp
from datahelp.models import MockModelClient


class TestRuntimeConvergence(unittest.TestCase):
    """验证 run_shell 成功/失败时的收敛行为与门控拦截。"""

    def setUp(self):
        self.model = MockModelClient()
        self.app = DataHelp(
            model_client=self.model,
            repo_root="/tmp",
        )
        # 允许 run_shell 通过数据真实性门控
        self.app._explored_csvs.add("test.csv")

    # ── 失败不记录 ────────────────────────────────

    @patch("datahelp.tools.subprocess.run")
    def test_failed_shell_does_not_record_script_run(self, mock_run):
        """Shell 返回 (exit code:) 时不记录脚本已运行，不收斂。"""
        self.app._analysis_scripts_written = ["analysis_01.py"]

        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "Error: module not found"

        self.app.run_tool("run_shell", {"command": "python analysis_01.py"})

        self.assertNotIn("analysis_01.py", self.app._analysis_scripts_run)
        self.assertFalse(self.app._convergence_triggered)
        self.assertNotEqual(self.app._phase, "report")

    @patch("datahelp.tools.subprocess.run")
    def test_failed_shell_with_exit_code_in_result(self, mock_run):
        """返回文本含 (exit code:) 时不应标记为成功。"""
        self.app._analysis_scripts_written = ["analysis_01.py"]

        mock_run.return_value.returncode = 2
        mock_run.return_value.stdout = "partial output"
        mock_run.return_value.stderr = ""

        result = self.app.run_tool("run_shell", {"command": "python analysis_01.py"})

        self.assertNotIn("analysis_01.py", self.app._analysis_scripts_run)
        self.assertFalse(self.app._convergence_triggered)
        self.assertNotEqual(self.app._phase, "report")

    @patch("datahelp.tools.subprocess.run")
    def test_error_prefix_does_not_record_script_run(self, mock_run):
        """返回以「错误」开头时不记录脚本已运行。"""
        self.app._analysis_scripts_written = ["analysis_01.py"]

        # 模拟 tool_run_shell 抛出异常 → 返回 "错误: ..."
        mock_run.side_effect = RuntimeError("timeout")

        result = self.app.run_tool("run_shell", {"command": "python analysis_01.py"})

        self.assertNotIn("analysis_01.py", self.app._analysis_scripts_run)
        self.assertFalse(self.app._convergence_triggered)

    # ── 成功收敛 ────────────────────────────────

    @patch("datahelp.tools.subprocess.run")
    def test_successful_script_sets_phase_report(self, mock_run):
        """Shell 成功执行后 phase=report、convergence_triggered=True。"""
        self.app._analysis_scripts_written = ["analysis_01.py"]

        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "Analysis complete\navg_price: 42.5"
        mock_run.return_value.stderr = ""

        self.app.run_tool("run_shell", {"command": "python analysis_01.py"})

        self.assertIn("analysis_01.py", self.app._analysis_scripts_run)
        self.assertTrue(self.app._convergence_triggered)
        self.assertEqual(self.app._phase, "report")

    # ── 门控拦截 ────────────────────────────────

    def test_re_run_script_blocked_after_convergence(self):
        """收敛后再次 run_shell 同一脚本被 _check_gates 拦截。"""
        self.app._analysis_scripts_written = ["analysis_01.py"]
        self.app._analysis_scripts_run = {"analysis_01.py"}
        self.app._phase = "report"
        self.app._convergence_triggered = True

        gate_result = self.app._check_gates(
            "run_shell", {"command": "python analysis_01.py"}
        )

        self.assertIsNotNone(gate_result)
        self.assertIn("证据已齐全", gate_result)
        self.assertIn("不得再次运行脚本", gate_result)

    def test_re_run_not_blocked_if_not_in_analysis_scripts_run(self):
        """收敛后运行尚未执行过的脚本不应被该门控拦截。"""
        self.app._analysis_scripts_written = ["analysis_01.py", "analysis_02.py"]
        self.app._analysis_scripts_run = {"analysis_01.py"}
        self.app._phase = "report"
        self.app._convergence_triggered = True

        gate_result = self.app._check_gates(
            "run_shell", {"command": "python analysis_02.py"}
        )

        # analysis_02.py 不在 _analysis_scripts_run 中，门控应放行
        self.assertIsNone(gate_result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
