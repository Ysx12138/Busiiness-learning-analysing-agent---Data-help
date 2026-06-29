"""No-network tests for the beginner-facing analyze command."""
from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from datahelp import cli


class TestAnalyzeCommand(unittest.TestCase):
    def test_analyze_command_calls_pipeline(self):
        argv = [
            "datahelp",
            "analyze",
            "sales.csv",
            "--output-dir",
            "out",
            "--provider",
            "mock",
            "--mode",
            "beginner_summary",
        ]
        with patch.object(sys, "argv", argv):
            with patch("datahelp.pipeline.run_data_help_analysis") as mock_run:
                mock_run.return_value = {"status": "completed", "output_dir": "out"}
                cli.main()

        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        self.assertEqual(args[0], "sales.csv")
        self.assertEqual(args[1], "out")
        self.assertEqual(kwargs["provider"], "mock")
        self.assertEqual(kwargs["mode"], "beginner_summary")

    def test_analyze_command_exits_nonzero_on_failure(self):
        argv = ["datahelp", "analyze", "bad.csv", "--provider", "mock"]
        with patch.object(sys, "argv", argv):
            with patch("datahelp.pipeline.run_data_help_analysis") as mock_run:
                mock_run.return_value = {"status": "failed", "evidence_error": "数据读取失败"}
                with self.assertRaises(SystemExit) as cm:
                    cli.main()

        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
