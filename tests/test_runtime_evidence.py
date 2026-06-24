"""无网络单元测试：runtime 自动读取 analysis_evidence.md。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datahelp.runtime import DataHelp
from datahelp.models import MockModelClient
from datahelp.tools_data import read_evidence_file


class TestReadEvidenceFile(unittest.TestCase):
    """测试 read_evidence_file 工具函数。"""

    def test_read_existing_file(self):
        """存在文件时返回内容。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "analysis_evidence.md"
            p.write_text("data quality: ok\navg_price: 42.5", encoding="utf-8")
            content = read_evidence_file(str(p))
            self.assertIn("data quality", content)
            self.assertIn("42.5", content)

    def test_nonexistent_file(self):
        """文件不存在返回空字符串。"""
        content = read_evidence_file("/tmp/__nonexistent_evidence.md")
        self.assertEqual(content, "")

    def test_truncates_long_content(self):
        """超过 30000 字符时截断。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "analysis_evidence.md"
            p.write_text("x" * 40000, encoding="utf-8")
            content = read_evidence_file(str(p))
            self.assertEqual(len(content), 30000)

    def test_utf8_content(self):
        """正确读取中文 UTF-8 内容。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "analysis_evidence.md"
            p.write_text("数据质量检查\n- 缺失值：10\n- 重复值：2", encoding="utf-8")
            content = read_evidence_file(str(p))
            self.assertIn("缺失值", content)
            self.assertIn("10", content)


class TestRuntimeEvidenceAutoRead(unittest.TestCase):
    """验证 run_shell 成功运行分析脚本后自动读取 evidence 文件。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.evidence_path = Path(self.tmpdir) / "analysis_evidence.md"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- 有证据文件 --------------------------------

    @patch("datahelp.tools.subprocess.run")
    def test_evidence_appended_when_file_exists(self, mock_run):
        """证据文件存在时，结果应包含'权威分析证据'和文件内容。"""
        self.evidence_path.write_text(
            "## 数据质量\n- 缺失值: 0\n- 重复值: 0\n\n## 核心指标\n- 均价: 42.5\n",
            encoding="utf-8",
        )

        model = MockModelClient()
        app = DataHelp(model_client=model, repo_root=self.tmpdir)
        app._explored_csvs.add("test.csv")
        app._analysis_scripts_written = ["analysis_01.py"]

        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "Analysis done\navg_price: 42.5"
        mock_run.return_value.stderr = ""

        result = app.run_tool("run_shell", {"command": "python analysis_01.py"})

        self.assertIn("权威分析证据", result)
        self.assertIn("缺失值: 0", result)
        self.assertIn("均价: 42.5", result)

    @patch("datahelp.tools.subprocess.run")
    def test_evidence_with_long_content(self, mock_run):
        """长证据文件内容被包含在结果中。"""
        # 2000 字符内容，仍远小于 MAX_TOOL_OUTPUT，不会被裁剪
        content = ("# 数据质量\n- 缺失值: 0\n- 重复值: 0\n" * 50)
        self.evidence_path.write_text(content, encoding="utf-8")

        model = MockModelClient()
        app = DataHelp(model_client=model, repo_root=self.tmpdir)
        app._explored_csvs.add("test.csv")
        app._analysis_scripts_written = ["analysis_01.py"]

        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "ok"
        mock_run.return_value.stderr = ""

        result = app.run_tool("run_shell", {"command": "python analysis_01.py"})

        self.assertIn("权威分析证据", result)
        self.assertIn("数据质量", result)
        self.assertIn("缺失值: 0", result)

    # -- 无证据文件 --------------------------------

    @patch("datahelp.tools.subprocess.run")
    def test_no_evidence_file_normal_behavior(self, mock_run):
        """无 analysis_evidence.md 时，结果不受影响。"""
        model = MockModelClient()
        app = DataHelp(model_client=model, repo_root=self.tmpdir)
        app._explored_csvs.add("test.csv")
        app._analysis_scripts_written = ["analysis_01.py"]

        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "All good"
        mock_run.return_value.stderr = ""

        result = app.run_tool("run_shell", {"command": "python analysis_01.py"})

        self.assertIn("证据已齐全", result)
        self.assertNotIn("权威分析证据", result)

    @patch("datahelp.tools.subprocess.run")
    def test_empty_evidence_file_no_label(self, mock_run):
        """空证据文件不追加'权威分析证据'标签。"""
        self.evidence_path.write_text("", encoding="utf-8")

        model = MockModelClient()
        app = DataHelp(model_client=model, repo_root=self.tmpdir)
        app._explored_csvs.add("test.csv")
        app._analysis_scripts_written = ["analysis_01.py"]

        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "All good"
        mock_run.return_value.stderr = ""

        result = app.run_tool("run_shell", {"command": "python analysis_01.py"})

        self.assertIn("证据已齐全", result)
        self.assertNotIn("权威分析证据", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
