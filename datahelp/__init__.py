"""DataHelp —— 商科生的数据分析学习 Agent。"""

from datahelp.runtime import DataHelp
from datahelp.models import create_model_client
from datahelp.tools import build_tool_registry
from datahelp.pipeline import run_data_help_analysis

# V2 分析合约与引擎（可选导入，不改变现有 CLI 行为）
from datahelp.analysis_contract import AnalysisPlan, AnalysisEvidence, AnalysisResult
from datahelp.analysis_engine import DeterministicAnalysisEngine
from datahelp.report_orchestrator import ReportOrchestrator, ReportOutcome

__all__ = [
    "DataHelp",
    "create_model_client",
    "build_tool_registry",
    "run_data_help_analysis",
    "AnalysisPlan",
    "AnalysisEvidence",
    "AnalysisResult",
    "DeterministicAnalysisEngine",
    "ReportOrchestrator",
    "ReportOutcome",
]
