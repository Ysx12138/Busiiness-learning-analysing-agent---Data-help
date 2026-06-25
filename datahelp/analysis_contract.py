"""DataHelp V2 分析合约 —— 确定性的可审计数据分析数据模型。

定义 AnalysisPlan、AnalysisEvidence、AnalysisResult 三个纯标准库
dataclass，每个都支持 to_dict / from_dict 序列化。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any


def _default_json(obj: Any) -> Any:
    """处理 dataclass asdict 中可能出现的非 JSON 原生类型。"""
    if isinstance(obj, (AnalysisPlan, AnalysisEvidence, AnalysisResult)):
        return obj.to_dict()
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    return str(obj)


@dataclass
class AnalysisEvidence:
    """一条分析证据，可完全回溯到原始字段与计算公式。

    每个证据都包含：
      - metric_name: 指标名称（如 "total_revenue"）
      - value:       计算得到的值（int/float/str/dict/list 等 JSON 原生类型）
      - formula:     计算公式（如 "SUM(revenue)"）
      - source_columns: 该指标来源的数据列名列表
      - calculation_method: 计算方法（如 "sum", "count", "mean", "min", "max",
                            "std", "missing_rate", "duplicate_count",
                            "derived_profit", "derived_margin", "monthly_count",
                            "value_counts", "column_profile"）
      - caveat:      注意事项 / 限制说明（无则置空字符串）
    """

    metric_name: str = ""
    value: Any = None
    formula: str = ""
    source_columns: list[str] = field(default_factory=list)
    calculation_method: str = ""
    caveat: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # 确保 value 可 JSON 序列化
        if not isinstance(d["value"], (str, int, float, bool, list, dict, type(None))):
            d["value"] = _default_json(d["value"])
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AnalysisEvidence:
        return cls(
            metric_name=d.get("metric_name", ""),
            value=d.get("value"),
            formula=d.get("formula", ""),
            source_columns=d.get("source_columns", []),
            calculation_method=d.get("calculation_method", ""),
            caveat=d.get("caveat", ""),
        )


@dataclass
class AnalysisPlan:
    """一组相关的分析计划，包含多个证据。

    每个 plan 描述一个分析维度的结果。
    """

    plan_id: str = ""
    description: str = ""
    evidence: list[AnalysisEvidence] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "description": self.description,
            "evidence": [e.to_dict() for e in self.evidence],
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AnalysisPlan:
        evidence_list = [AnalysisEvidence.from_dict(e) for e in d.get("evidence", [])]
        return cls(
            plan_id=d.get("plan_id", ""),
            description=d.get("description", ""),
            evidence=evidence_list,
            created_at=d.get("created_at", ""),
        )


@dataclass
class AnalysisResult:
    """一次完整分析运行的结果容器。

    包含输入文件信息、总体概况和多个 AnalysisPlan。
    """

    input_file: str = ""
    row_count: int = 0
    column_count: int = 0
    column_names: list[str] = field(default_factory=list)
    plans: list[AnalysisPlan] = field(default_factory=list)
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_file": self.input_file,
            "row_count": self.row_count,
            "column_count": self.column_count,
            "column_names": self.column_names,
            "plans": [p.to_dict() for p in self.plans],
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AnalysisResult:
        plans = [AnalysisPlan.from_dict(p) for p in d.get("plans", [])]
        return cls(
            input_file=d.get("input_file", ""),
            row_count=d.get("row_count", 0),
            column_count=d.get("column_count", 0),
            column_names=d.get("column_names", []),
            plans=plans,
            generated_at=d.get("generated_at", ""),
        )

    def all_evidence(self) -> list[AnalysisEvidence]:
        """展平返回所有 plan 下的全部证据。"""
        result: list[AnalysisEvidence] = []
        for p in self.plans:
            result.extend(p.evidence)
        return result

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False,
                          default=_default_json)

    @classmethod
    def from_json(cls, json_str: str) -> AnalysisResult:
        return cls.from_dict(json.loads(json_str))


# ── 便利函数 ──────────────────────────────────────────

def make_evidence(
    metric_name: str,
    value: Any,
    formula: str,
    source_columns: list[str],
    calculation_method: str,
    caveat: str = "",
) -> AnalysisEvidence:
    """快速构造一条证据。"""
    return AnalysisEvidence(
        metric_name=metric_name,
        value=value,
        formula=formula,
        source_columns=source_columns,
        calculation_method=calculation_method,
        caveat=caveat,
    )
