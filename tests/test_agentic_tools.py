"""No-network tests for the product-grade Agent tool path."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from datahelp.models import ModelClient
from datahelp.runtime import DataHelp
from datahelp.tools import build_tool_registry


SAMPLE_CSV = """product,category,revenue,date
A,Books,120,2026-01-01
B,Books,180,2026-01-02
C,Games,250,2026-02-01
D,Games,300,2026-02-15
"""


class SequenceModel(ModelClient):
    """Small deterministic model that walks the Agent through high-level tools."""

    def __init__(self):
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "sequence-model"

    def complete(self, prompt: str, max_tokens: int = 4096) -> str:
        self.calls += 1
        outputs = [
            '<tool>{"name": "inspect_dataset", "args": {"path": "sample.csv"}}</tool>',
            '<tool>{"name": "suggest_analysis_plan", "args": {"path": "sample.csv", "goal": "学习型销售分析"}}</tool>',
            '<tool>{"name": "run_analysis_evidence", "args": {"path": "sample.csv", "output_dir": "."}}</tool>',
            '<tool>{"name": "validate_evidence", "args": {"evidence_path": "analysis_evidence.json"}}</tool>',
            '<tool>{"name": "compose_report_from_evidence", "args": {"evidence_path": "analysis_evidence.json"}}</tool>',
            "<final>已基于证据生成教学型数据分析报告。</final>",
        ]
        return outputs[min(self.calls - 1, len(outputs) - 1)]


class TestAgenticToolPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "sample.csv").write_text(SAMPLE_CSV, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_agent_can_complete_high_level_tool_path(self):
        agent = DataHelp(
            model_client=SequenceModel(),
            repo_root=str(self.root),
            tools=build_tool_registry(str(self.root)),
            max_steps=8,
            skill_name="business_analysis",
            mode="beginner_summary",
        )

        result = agent.ask("分析 sample.csv，生成学习型报告")

        self.assertIn("已基于证据生成", result)
        self.assertEqual(agent.task_state.status, "completed")
        self.assertEqual(agent._agent_product_state, "reported")
        self.assertTrue((self.root / "analysis_evidence.json").exists())
        self.assertTrue((self.root / "analysis_evidence.md").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
