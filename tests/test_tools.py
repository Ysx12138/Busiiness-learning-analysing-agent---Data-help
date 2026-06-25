"""无网络测试：tool_run_shell 解释器重写逻辑。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# 确保项目根在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datahelp.tools import tool_run_shell


class TestToolRunShellInterpreterRewrite(unittest.TestCase):
    """测试 tool_run_shell 将开头的 python/python3 替换为 sys.executable。"""

    def setUp(self):
        self.repo_root = "/tmp"

    # -- 重写断言 --------------------------------

    @patch("datahelp.tools.subprocess.run")
    def test_python_rewritten_to_sys_executable(self, mock_run):
        """python xxx 应被重写为 sys.executable xxx。"""
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "ok\n"
        mock_run.return_value.stderr = ""

        tool_run_shell({"command": "python analysis_01.py"}, self.repo_root)

        args, _ = mock_run.call_args
        cmd = args[0]
        self.assertFalse(cmd.startswith("python "),
                         f"不应以裸 python 开头: {cmd!r}")
        self.assertIn(sys.executable, cmd,
                      f"应包含 sys.executable: {cmd!r}")

    @patch("datahelp.tools.subprocess.run")
    def test_python3_rewritten_to_sys_executable(self, mock_run):
        """python3 xxx 应被重写为 sys.executable xxx。"""
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "ok\n"
        mock_run.return_value.stderr = ""

        tool_run_shell({"command": "python3 analysis_01.py"}, self.repo_root)

        args, _ = mock_run.call_args
        cmd = args[0]
        self.assertFalse(cmd.startswith("python3 "),
                         f"不应以裸 python3 开头: {cmd!r}")
        self.assertIn(sys.executable, cmd,
                      f"应包含 sys.executable: {cmd!r}")

    # -- && / ; 前缀测试（新增） --------------------------------

    @patch("datahelp.tools.subprocess.run")
    def test_python_after_and_and_rewritten(self, mock_run):
        """cd x && python xxx 中的 python 应被替换为 sys.executable。"""
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "ok\n"
        mock_run.return_value.stderr = ""

        tool_run_shell({"command": "cd x && python analysis_01.py"}, self.repo_root)

        args, _ = mock_run.call_args
        cmd = args[0]
        self.assertIn(sys.executable, cmd,
                      f"&& 后的 python 应替换: {cmd!r}")

    @patch("datahelp.tools.subprocess.run")
    def test_python3_after_semicolon_rewritten(self, mock_run):
        """cd y; python3 a.py 中的 python3 应被替换为 sys.executable。"""
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "ok\n"
        mock_run.return_value.stderr = ""

        tool_run_shell({"command": "cd y; python3 a.py"}, self.repo_root)

        args, _ = mock_run.call_args
        cmd = args[0]
        self.assertIn(sys.executable, cmd,
                      f"; 后的 python3 应替换: {cmd!r}")

    # -- 不被重写断言 --------------------------------

    @patch("datahelp.tools.subprocess.run")
    def test_echo_not_rewritten(self, mock_run):
        """echo hello 应保持原样。"""
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "hello\n"
        mock_run.return_value.stderr = ""

        tool_run_shell({"command": "echo hello"}, self.repo_root)

        args, _ = mock_run.call_args
        self.assertEqual(args[0], "echo hello")

    @patch("datahelp.tools.subprocess.run")
    def test_leading_whitespace_rewrite_still_works(self, mock_run):
        """前置空白时 python 仍被重写为 sys.executable。"""
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "ok\n"
        mock_run.return_value.stderr = ""

        tool_run_shell({"command": "  python analysis_01.py"}, self.repo_root)

        args, _ = mock_run.call_args
        cmd = args[0]
        self.assertIn(sys.executable, cmd,
                      f"前置空白时仍应包含 sys.executable: {cmd!r}")

    @patch("datahelp.tools.subprocess.run")
    def test_python_alone_no_space_not_rewritten(self, mock_run):
        """python 后无参数（无空格）不应被重写。"""
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""

        tool_run_shell({"command": "python3"}, self.repo_root)

        args, _ = mock_run.call_args
        self.assertEqual(args[0], "python3")

    # -- 返回文本断言 --------------------------------

    @patch("datahelp.tools.subprocess.run")
    def test_return_text_includes_interpreter_when_rewritten(self, mock_run):
        """重写发生时返回文本应包含解释器路径。"""
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "result\n"
        mock_run.return_value.stderr = ""

        result = tool_run_shell({"command": "python script.py"}, self.repo_root)

        self.assertIn(sys.executable, result,
                      f"返回文本应包含解释器路径，得到: {result}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
