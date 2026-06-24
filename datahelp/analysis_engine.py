"""DataHelp V2 确定性分析引擎。

DeterministicAnalysisEngine.run(csv_path, output_dir) 读取 CSV，
使用标准库或 pandas 计算可完全回溯的审计分析证据，输出：

  - analysis_evidence.json   —— AnalysisResult 序列化
  - analysis_evidence.md     —— 人类可读的 Markdown 报告

所有异常都被捕获并以 caveat 形式记录，绝不抛出。
"""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from datahelp.analysis_contract import (
    AnalysisEvidence,
    AnalysisPlan,
    AnalysisResult,
    make_evidence,
)

# ── 常量 ──────────────────────────────────────────────────────────────

_TOP_N_CATEGORIES = 10         # 分类列最多展示前 N 个取值
_MAX_CATEGORICAL_UNIQUE = 50   # 超过此唯一值数不计为分类列
_TS_OUTPUT = "analysis_evidence.json"
_MD_OUTPUT = "analysis_evidence.md"

# 常见日期格式（优先尝试显式匹配，避免 pd.to_datetime 自动推断产生 UserWarning）
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%d-%m-%Y",
    "%d/%m/%Y",
)


# ══════════════════════════════════════════════════════════════════════
# 公开引擎
# ══════════════════════════════════════════════════════════════════════

class DeterministicAnalysisEngine:
    """确定性分析引擎，产生可完全回溯的审计分析证据。

    用法::

        result = DeterministicAnalysisEngine.run("data.csv", "output/")
        print(result.to_json())
    """

    @staticmethod
    def run(csv_path: str, output_dir: str) -> AnalysisResult:
        """读取 CSV，计算各类分析证据，写入 JSON 和 Markdown。"""
        output_dir = _ensure_dir(output_dir)

        # ── 读取 CSV ──────────────────────────────────────────────
        try:
            df = pd.read_csv(csv_path)
        except Exception as exc:
            result = _make_failed_result(csv_path, exc)
            _write_outputs(result, output_dir)
            return result

        if df.empty:
            result = _make_empty_result(csv_path, df)
            _write_outputs(result, output_dir)
            return result

        # ── 执行分析 ──────────────────────────────────────────────
        plans: list[AnalysisPlan] = [
            _profile_plan(df),
            _missing_plan(df),
            _duplicates_plan(df),
            _numeric_stats_plan(df),
            _categorical_plan(df),
            _date_trend_plan(df),
            _derived_metrics_plan(df),
        ]

        result = AnalysisResult(
            input_file=os.path.abspath(csv_path),
            row_count=len(df),
            column_count=len(df.columns),
            column_names=list(df.columns),
            plans=plans,
            generated_at="",
        )

        _write_outputs(result, output_dir)
        return result


# ══════════════════════════════════════════════════════════════════════
# 内部构建函数 —— 每个返回一个 AnalysisPlan
# ══════════════════════════════════════════════════════════════════════

def _profile_plan(df: pd.DataFrame) -> AnalysisPlan:
    """数据集概览：行列数、列名、类型。"""
    evidence: list[AnalysisEvidence] = [
        make_evidence(
            metric_name="row_count",
            value=int(len(df)),
            formula="len(df)",
            source_columns=[],
            calculation_method="count",
        ),
        make_evidence(
            metric_name="column_count",
            value=int(len(df.columns)),
            formula="len(df.columns)",
            source_columns=[],
            calculation_method="count",
        ),
        make_evidence(
            metric_name="total_cells",
            value=int(len(df) * len(df.columns)),
            formula="row_count * column_count",
            source_columns=[],
            calculation_method="count",
        ),
    ]

    # 每列名 + dtype
    for col in df.columns:
        evidence.append(
            make_evidence(
                metric_name=f"column_profile::{col}",
                value=str(df[col].dtype),
                formula="df[col].dtype",
                source_columns=[col],
                calculation_method="column_profile",
                caveat=_dtype_caveat(df[col]),
            )
        )

    return AnalysisPlan(
        plan_id="profile",
        description="数据集概览",
        evidence=evidence,
        created_at="",
    )


def _missing_plan(df: pd.DataFrame) -> AnalysisPlan:
    """缺失值分析。"""
    evidence: list[AnalysisEvidence] = []
    total_cells = len(df) * len(df.columns)
    total_missing = int(df.isna().sum().sum())

    evidence.append(
        make_evidence(
            metric_name="total_missing_cells",
            value=total_missing,
            formula="df.isna().sum().sum()",
            source_columns=[],
            calculation_method="count",
        )
    )
    evidence.append(
        make_evidence(
            metric_name="total_missing_rate",
            value=round(total_missing / total_cells, 6) if total_cells else 0.0,
            formula="total_missing / (row_count * column_count)",
            source_columns=[],
            calculation_method="missing_rate",
        )
    )

    # 逐列缺失
    missing_cols = []
    for col in df.columns:
        miss_count = int(df[col].isna().sum())
        if miss_count > 0:
            missing_cols.append(col)
        evidence.append(
            make_evidence(
                metric_name=f"missing_count::{col}",
                value=miss_count,
                formula=f"df['{col}'].isna().sum()",
                source_columns=[col],
                calculation_method="count",
            )
        )
        evidence.append(
            make_evidence(
                metric_name=f"missing_rate::{col}",
                value=round(miss_count / len(df), 6) if len(df) else 0.0,
                formula=f"df['{col}'].isna().sum() / row_count",
                source_columns=[col],
                calculation_method="missing_rate",
            )
        )

    evidence.append(
        make_evidence(
            metric_name="columns_with_missing",
            value=missing_cols,
            formula="[col for col in df.columns if df[col].isna().sum() > 0]",
            source_columns=[],
            calculation_method="column_profile",
        )
    )

    return AnalysisPlan(
        plan_id="missing",
        description="缺失值分析",
        evidence=evidence,
        created_at="",
    )


def _duplicates_plan(df: pd.DataFrame) -> AnalysisPlan:
    """重复行分析。"""
    evidence: list[AnalysisEvidence] = []
    try:
        dup_count = int(df.duplicated().sum())
        dup_rate = round(dup_count / len(df), 6) if len(df) else 0.0
    except Exception as exc:
        dup_count = 0
        dup_rate = 0.0
        caveat = f"无法计算重复行: {exc}"
    else:
        caveat = ""

    evidence.append(
        make_evidence(
            metric_name="duplicate_row_count",
            value=dup_count,
            formula="df.duplicated().sum()",
            source_columns=[],
            calculation_method="duplicate_count",
            caveat=caveat,
        )
    )
    evidence.append(
        make_evidence(
            metric_name="duplicate_rate",
            value=dup_rate,
            formula="duplicate_row_count / row_count",
            source_columns=[],
            calculation_method="missing_rate",
            caveat=caveat,
        )
    )

    return AnalysisPlan(
        plan_id="duplicates",
        description="重复行分析",
        evidence=evidence,
        created_at="",
    )


def _numeric_stats_plan(df: pd.DataFrame) -> AnalysisPlan:
    """数值列描述性统计。"""
    evidence: list[AnalysisEvidence] = []
    numeric_cols = list(df.select_dtypes(include=[np.number]).columns)

    if not numeric_cols:
        evidence.append(
            make_evidence(
                metric_name="numeric_columns",
                value=[],
                formula="df.select_dtypes(include=[np.number]).columns.tolist()",
                source_columns=[],
                calculation_method="column_profile",
                caveat="数据集中无数值列，无法计算数值描述统计。",
            )
        )
        return AnalysisPlan(
            plan_id="numeric_stats",
            description="数值列描述性统计",
            evidence=evidence,
            created_at="",
        )

    evidence.append(
        make_evidence(
            metric_name="numeric_columns",
            value=numeric_cols,
            formula="df.select_dtypes(include=[np.number]).columns.tolist()",
            source_columns=[],
            calculation_method="column_profile",
        )
    )

    for col in numeric_cols:
        series = df[col].dropna()
        if len(series) == 0:
            evidence.append(
                make_evidence(
                    metric_name=f"stat::{col}",
                    value=None,
                    formula="",
                    source_columns=[col],
                    calculation_method="mean",
                    caveat=f"列 '{col}' 全部为空，无法计算统计量。",
                )
            )
            continue

        try:
            stats = {
                "count": int(len(series)),
                "mean": _safe_float(series.mean()),
                "std": _safe_float(series.std()),
                "min": _safe_float(series.min()),
                "q25": _safe_float(series.quantile(0.25)),
                "median": _safe_float(series.median()),
                "q75": _safe_float(series.quantile(0.75)),
                "max": _safe_float(series.max()),
            }
        except Exception as exc:
            evidence.append(
                make_evidence(
                    metric_name=f"stat::{col}",
                    value=None,
                    formula="",
                    source_columns=[col],
                    calculation_method="descriptive_stats",
                    caveat=f"列 '{col}' 统计计算失败: {exc}",
                )
            )
            continue

        evidence.append(
            make_evidence(
                metric_name=f"stat::{col}",
                value=stats,
                formula=(
                    "count / mean / std / min / q25 / median / q75 / max"
                ),
                source_columns=[col],
                calculation_method="descriptive_stats",
            )
        )

    return AnalysisPlan(
        plan_id="numeric_stats",
        description="数值列描述性统计",
        evidence=evidence,
        created_at="",
    )


def _categorical_plan(df: pd.DataFrame) -> AnalysisPlan:
    """分类列聚合（value counts）。"""
    evidence: list[AnalysisEvidence] = []

    # 识别可能的分类列：object / category dtype，或唯一值较少的数值列
    cat_candidates = []
    for col in df.columns:
        dtype = df[col].dtype
        if pd.api.types.is_object_dtype(dtype) or pd.api.types.is_string_dtype(dtype) or isinstance(dtype, pd.CategoricalDtype):
            cat_candidates.append(col)
        elif pd.api.types.is_integer_dtype(dtype) or pd.api.types.is_bool_dtype(dtype):
            # 整数／布尔列，唯一值较少也视为分类
            try:
                unique_count = df[col].nunique(dropna=False)
                if 0 < unique_count <= _MAX_CATEGORICAL_UNIQUE:
                    cat_candidates.append(col)
            except Exception:
                pass

    if not cat_candidates:
        evidence.append(
            make_evidence(
                metric_name="categorical_columns",
                value=[],
                formula="",
                source_columns=[],
                calculation_method="column_profile",
                caveat="数据集中没有可识别的分类列。",
            )
        )
        return AnalysisPlan(
            plan_id="categorical",
            description="分类列聚合",
            evidence=evidence,
            created_at="",
        )

    evidence.append(
        make_evidence(
            metric_name="categorical_columns",
            value=cat_candidates,
            formula="heuristic: object/category dtype or low-cardinality numeric",
            source_columns=[],
            calculation_method="column_profile",
        )
    )

    for col in cat_candidates:
        try:
            vc = df[col].value_counts().head(_TOP_N_CATEGORIES)
            vc_dict = {str(k): int(v) for k, v in vc.items()}
            unique_count = int(df[col].nunique())
        except Exception as exc:
            evidence.append(
                make_evidence(
                    metric_name=f"value_counts::{col}",
                    value=None,
                    formula="",
                    source_columns=[col],
                    calculation_method="value_counts",
                    caveat=f"列 '{col}' 值计数失败: {exc}",
                )
            )
            continue

        evidence.append(
            make_evidence(
                metric_name=f"unique_count::{col}",
                value=unique_count,
                formula=f"df['{col}'].nunique()",
                source_columns=[col],
                calculation_method="count",
            )
        )
        evidence.append(
            make_evidence(
                metric_name=f"value_counts::{col}",
                value=vc_dict,
                formula=f"df['{col}'].value_counts().head({_TOP_N_CATEGORIES})",
                source_columns=[col],
                calculation_method="value_counts",
            )
        )

    return AnalysisPlan(
        plan_id="categorical",
        description="分类列聚合",
        evidence=evidence,
        created_at="",
    )


def _coerce_datetime(series: pd.Series) -> pd.Series:
    """安全解析日期：优先常见显式格式，失败时抑制 UserWarning 后自动推断。"""
    for date_format in _DATE_FORMATS:
        try:
            parsed = pd.to_datetime(series, format=date_format, errors="coerce")
            if parsed.notna().any():
                return parsed
        except Exception:
            continue
    # 兜底：让 pandas 自动推断，但局部抑制 UserWarning
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning, message=".*Could not infer format.*")
        parsed = pd.to_datetime(series, errors="coerce")
    return parsed


def _date_trend_plan(df: pd.DataFrame) -> AnalysisPlan:
    """可解析日期列按月趋势。"""
    evidence: list[AnalysisEvidence] = []

    # 尝试将每列解析为日期
    date_columns = []
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            date_columns.append(col)
            continue
        # 仅对文本列尝试解析日期（跳过数值列，避免 epoch ns 误判）
        if not (pd.api.types.is_object_dtype(df[col]) or pd.api.types.is_string_dtype(df[col])):
            continue
        # 尝试常见日期格式
        try:
            sample = df[col].dropna().head(20)
            if sample.empty:
                continue
            # 至少成功解析 50%
            parsed = _coerce_datetime(sample)
            success_rate = parsed.notna().sum() / len(sample)
            if success_rate >= 0.5:
                date_columns.append(col)
        except Exception:
            continue

    if not date_columns:
        evidence.append(
            make_evidence(
                metric_name="date_columns",
                value=[],
                formula="",
                source_columns=[],
                calculation_method="column_profile",
                caveat="未检测到可解析的日期列，无法计算按月趋势。",
            )
        )
        return AnalysisPlan(
            plan_id="date_trend",
            description="可解析日期按月趋势",
            evidence=evidence,
            created_at="",
        )

    evidence.append(
        make_evidence(
            metric_name="date_columns",
            value=date_columns,
            formula="pd.to_datetime(col, errors='coerce', infer_datetime_format=True)",
            source_columns=[],
            calculation_method="column_profile",
        )
    )

    for col in date_columns:
        try:
            # 完整解析并提取 year-month
            parsed = _coerce_datetime(df[col])
            ym = parsed.dt.to_period("M").value_counts().sort_index()
            ym_dict = {str(k): int(v) for k, v in ym.items()}
            total_parsed = int(parsed.notna().sum())
            parse_rate = round(total_parsed / len(df), 6) if len(df) else 0.0
        except Exception as exc:
            evidence.append(
                make_evidence(
                    metric_name=f"monthly_trend::{col}",
                    value=None,
                    formula="",
                    source_columns=[col],
                    calculation_method="monthly_count",
                    caveat=f"列 '{col}' 按月聚合失败: {exc}",
                )
            )
            continue

        evidence.append(
            make_evidence(
                metric_name=f"monthly_trend::{col}",
                value=ym_dict,
                formula=(
                    f"pd.to_datetime(df['{col}'], errors='coerce')"
                    f".dt.to_period('M').value_counts()"
                ),
                source_columns=[col],
                calculation_method="monthly_count",
            )
        )
        evidence.append(
            make_evidence(
                metric_name=f"date_parse_rate::{col}",
                value=parse_rate,
                formula=f"parsed.notna().sum() / row_count",
                source_columns=[col],
                calculation_method="missing_rate",
                caveat=(
                    f"成功解析 {total_parsed}/{len(df)} 行"
                    if total_parsed < len(df)
                    else ""
                ),
            )
        )

    return AnalysisPlan(
        plan_id="date_trend",
        description="可解析日期按月趋势",
        evidence=evidence,
        created_at="",
    )


def _derived_metrics_plan(df: pd.DataFrame) -> AnalysisPlan:
    """基础派生指标（如 profit = revenue - cost）。"""
    evidence: list[AnalysisEvidence] = []
    headers_lower = {col.lower(): col for col in df.columns}

    # ── 模式: revenue + cost → profit / profit_margin ──
    revenue_col = None
    cost_col = None
    for key, orig in headers_lower.items():
        if "revenue" in key or "收入" in key or "销售额" in key:
            revenue_col = orig
        if "cost" in key or "成本" in key or "cogs" in key:
            cost_col = orig

    if revenue_col and cost_col:
        rev_vals = pd.to_numeric(df[revenue_col], errors="coerce")
        cost_vals = pd.to_numeric(df[cost_col], errors="coerce")
        valid = rev_vals.notna() & cost_vals.notna()
        if valid.any():
            total_profit = round((rev_vals[valid] - cost_vals[valid]).sum(), 4)
            total_rev = round(rev_vals[valid].sum(), 4)
            margin = round(total_profit / total_rev, 4) if total_rev else 0.0
            evidence.append(make_evidence(
                metric_name="derived_profit",
                value=total_profit,
                formula=f"SUM({revenue_col}) - SUM({cost_col})",
                source_columns=[revenue_col, cost_col],
                calculation_method="derived_profit",
                caveat="profit = revenue - cost",
            ))
            evidence.append(make_evidence(
                metric_name="derived_profit_margin",
                value=margin,
                formula=f"(SUM({revenue_col}) - SUM({cost_col})) / SUM({revenue_col})",
                source_columns=[revenue_col, cost_col],
                calculation_method="derived_margin",
                caveat="",
            ))
        else:
            evidence.append(make_evidence(
                metric_name="derived_profit",
                value="无成对非空数值",
                formula=f"SUM({revenue_col}) - SUM({cost_col})",
                source_columns=[revenue_col, cost_col],
                calculation_method="derived_profit",
                caveat=f"列 {revenue_col} 和 {cost_col} 无成对非空数值，无法计算利润。",
            ))
    elif revenue_col and not cost_col:
        evidence.append(make_evidence(
            metric_name="derived_profit",
            value="未找到 cost 列",
            formula="revenue - cost",
            source_columns=[revenue_col],
            calculation_method="derived_profit",
            caveat=f"找到收入列 {revenue_col} 但未找到对应的成本/费用列。",
        ))
    elif cost_col and not revenue_col:
        evidence.append(make_evidence(
            metric_name="derived_profit",
            value="未找到 revenue 列",
            formula="revenue - cost",
            source_columns=[cost_col],
            calculation_method="derived_profit",
            caveat=f"找到成本列 {cost_col} 但未找到对应的收入列。",
        ))
    else:
        evidence.append(make_evidence(
            metric_name="derived_profit",
            value="未匹配到 revenue/cost 列",
            formula="revenue - cost",
            source_columns=[],
            calculation_method="derived_profit",
            caveat="未检测到 revenue/cost 字段名匹配，跳过利润派生指标。",
        ))

    return AnalysisPlan(
        plan_id="derived_metrics",
        description="基础派生指标",
        evidence=evidence,
        created_at="",
    )


# ══════════════════════════════════════════════════════════════════════
# 输出辅助
# ══════════════════════════════════════════════════════════════════════

def _write_outputs(result: AnalysisResult, output_dir: str) -> None:
    """将 result 写为 JSON 和 Markdown。"""
    json_path = os.path.join(output_dir, _TS_OUTPUT)
    md_path = os.path.join(output_dir, _MD_OUTPUT)

    with open(json_path, "w", encoding="utf-8") as f:
        f.write(result.to_json(indent=2))

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(_to_markdown(result))


def _to_markdown(result: AnalysisResult) -> str:
    """将 AnalysisResult 渲染为可读 Markdown。"""
    lines: list[str] = []
    lines.append("# 数据分析审计证据\n")
    lines.append(f"- **输入文件**: `{result.input_file}`")
    lines.append(f"- **行数**: {result.row_count}")
    lines.append(f"- **列数**: {result.column_count}")
    lines.append(f"- **列名**: {', '.join(result.column_names)}")
    lines.append(f"- **生成时间**: {result.generated_at}")
    lines.append("")

    for plan in result.plans:
        lines.append(f"## {plan.description}")
        lines.append(f"*Plan ID: `{plan.plan_id}`*\n")

        if not plan.evidence:
            lines.append("_无证据。_\n")
            continue

        for ev in plan.evidence:
            lines.append(f"### {ev.metric_name}")
            lines.append(f"- **值**: `{_fmt_value(ev.value)}`")
            lines.append(f"- **计算公式**: `{ev.formula}`")
            lines.append(f"- **来源列**: {ev.source_columns}")
            lines.append(f"- **计算方法**: `{ev.calculation_method}`")
            if ev.caveat:
                lines.append(f"- ⚠️ **注意事项**: {ev.caveat}")
            lines.append("")

    return "\n".join(lines)


def _fmt_value(v: Any) -> str:
    """将值格式化为人类可读字符串。"""
    if v is None:
        return "None"
    if isinstance(v, float):
        return f"{v:.6g}"
    if isinstance(v, dict):
        if len(v) > 10:
            items = ", ".join(f"{k}: {_fmt_value(v)}" for k, v in list(v.items())[:10])
            return "{" + items + f", ... +{len(v)-10} more" + "}"
        items = ", ".join(f"{k}: {_fmt_value(v)}" for k, v in v.items())
        return "{" + items + "}"
    if isinstance(v, list):
        if len(v) > 10:
            return "[" + ", ".join(str(x) for x in v[:10]) + f", ... +{len(v)-10} more" + "]"
        return "[" + ", ".join(str(x) for x in v) + "]"
    return str(v)


# ══════════════════════════════════════════════════════════════════════
# 异常/空值兜底
# ══════════════════════════════════════════════════════════════════════

def _make_failed_result(csv_path: str, exc: Exception) -> AnalysisResult:
    """CSV 无法读取时的兜底 result。"""
    return AnalysisResult(
        input_file=os.path.abspath(csv_path),
        row_count=0,
        column_count=0,
        column_names=[],
        plans=[
            AnalysisPlan(
                plan_id="error",
                description="数据读取失败",
                evidence=[
                    make_evidence(
                        metric_name="read_error",
                        value=str(exc),
                        formula="pd.read_csv(csv_path)",
                        source_columns=[],
                        calculation_method="read_csv",
                        caveat=f"无法读取 CSV 文件: {exc}",
                    )
                ],
                created_at="",
            )
        ],
        generated_at="",
    )


def _make_empty_result(csv_path: str, df: pd.DataFrame) -> AnalysisResult:
    """CSV 无数据行时的兜底 result。"""
    return AnalysisResult(
        input_file=os.path.abspath(csv_path),
        row_count=0,
        column_count=len(df.columns),
        column_names=list(df.columns),
        plans=[
            AnalysisPlan(
                plan_id="empty",
                description="空数据集",
                evidence=[
                    make_evidence(
                        metric_name="empty_warning",
                        value="CSV 文件没有数据行，仅含表头。",
                        formula="len(df) == 0",
                        source_columns=[],
                        calculation_method="column_profile",
                        caveat="数据为空，无法执行任何实质性分析。",
                    )
                ],
                created_at="",
            )
        ],
        generated_at="",
    )


# ══════════════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════════════

def _ensure_dir(path: str) -> str:
    """确保目录存在。"""
    os.makedirs(path, exist_ok=True)
    return path


def _safe_float(val: Any) -> float | None:
    """安全转换为 float，NaN → None。"""
    if val is None:
        return None
    try:
        v = float(val)
        if pd.isna(v):
            return None
        return round(v, 6)
    except (ValueError, TypeError):
        return None


def _dtype_caveat(series: pd.Series) -> str:
    """对特定 dtype 给出注意事项。"""
    if pd.api.types.is_object_dtype(series):
        return "对象类型列，可能包含混合类型或文本。"
    return ""
