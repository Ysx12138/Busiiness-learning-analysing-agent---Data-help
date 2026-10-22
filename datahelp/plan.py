"""分析计划与执行 —— 模型说"算什么"，这里负责"算出来"。

计划是一份结构化 JSON，不是自由代码。可执行的操作是一组封闭模板，
每个模板都对应一段固定的 pandas 计算，所以：

- 数值只可能来自这里，模型编不出数；
- 每一步都能写成"公式 + 来源列"，可以追溯；
- 模型想算新东西，只能组合这些模板，不能塞任意 Python 进来。

计划可以由模型生成（plan_from_json），也可以由画像自动推导（default_plan，
在没有模型或模型失败时兜底）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from datahelp.profile import DatasetProfile

# 分组结果最多保留多少个组
_TOP_N = 10
# 分类列取值超过这个数就不做趋势/分组（省得输出爆炸）
_MAX_SERIES_POINTS = 24

_AGG_LABEL = {"sum": "合计", "mean": "平均", "count": "计数"}


class PlanError(ValueError):
    """计划不合法（未知操作、参数缺失、列不存在）。"""


@dataclass
class MetricSpec:
    """一条要计算的东西。"""

    id: str
    kind: str  # summary / group / trend / derived / missing / duplicates
    title: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AnalysisPlan:
    """一次分析的完整计划。"""

    question: str  # 这次要回答的业务问题
    metrics: list[MetricSpec] = field(default_factory=list)
    rationale: str = ""  # 为什么这么分析
    origin: str = "model"  # model / default

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "rationale": self.rationale,
            "origin": self.origin,
            "metrics": [m.to_dict() for m in self.metrics],
        }

    @classmethod
    def from_dict(cls, data: dict) -> AnalysisPlan:
        metrics = []
        for raw in data.get("metrics", []):
            kind = raw.get("kind", "")
            if kind not in _EXECUTORS:
                raise PlanError(f"未知的操作类型: {kind}")
            metrics.append(
                MetricSpec(
                    id=str(raw.get("id") or kind),
                    kind=kind,
                    title=str(raw.get("title") or kind),
                    params=dict(raw.get("params") or {}),
                )
            )
        if not metrics:
            raise PlanError("计划里没有任何计算项")
        return AnalysisPlan(
            question=str(data.get("question", "")),
            metrics=metrics,
            rationale=str(data.get("rationale", "")),
            origin=str(data.get("origin", "model")),
        )


@dataclass
class Evidence:
    """一条计算结果。"""

    metric: str
    value: Any
    formula: str
    source_columns: list[str]
    method: str
    caveat: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def display_value(self) -> str:
        if isinstance(self.value, dict):
            return "; ".join(f"{k}={_fmt(v)}" for k, v in list(self.value.items())[:8])
        if isinstance(self.value, list):
            return ", ".join(_fmt(v) for v in self.value[:8])
        return _fmt(self.value)


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:,.4g}"
    return str(value)


# ── 执行器：每一种操作对应一段固定计算 ────────────────────────────


def _require(params: dict, key: str) -> str:
    value = params.get(key)
    if not value:
        raise PlanError(f"缺少参数 {key}")
    return str(value)


def _get_series(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        raise PlanError(f"列不存在: {column}")
    return df[column]


def _exec_summary(df: pd.DataFrame, params: dict) -> Evidence:
    column = _require(params, "column")
    series = pd.to_numeric(_get_series(df, column), errors="coerce").dropna()
    if series.empty:
        return Evidence(
            metric=f"summary::{column}", value={}, formula="描述性统计",
            source_columns=[column], method="pandas describe", caveat="列全为空或无法转成数值",
        )
    stats = {
        "count": int(series.count()),
        "mean": round(float(series.mean()), 4),
        "median": round(float(series.median()), 4),
        "std": round(float(series.std()), 4) if len(series) > 1 else 0.0,
        "min": round(float(series.min()), 4),
        "max": round(float(series.max()), 4),
    }
    return Evidence(
        metric=f"summary::{column}",
        value=stats,
        formula="均值/中位数/标准差/极值",
        source_columns=[column],
        method="pandas 描述性统计（剔除空值）",
    )


def _exec_group(df: pd.DataFrame, params: dict) -> Evidence:
    by = _require(params, "by")
    measure = _require(params, "measure")
    agg = str(params.get("agg", "sum"))
    if agg not in ("sum", "mean", "count"):
        raise PlanError(f"不支持的聚合方式: {agg}")

    key = _get_series(df, by)
    value = pd.to_numeric(_get_series(df, measure), errors="coerce")

    if key.nunique(dropna=True) > _MAX_SERIES_POINTS and key.nunique(dropna=True) > _MAX_SERIES_POINTS * 4:
        return Evidence(
            metric=f"group::{by}::{measure}",
            value={},
            formula=f"按 {by} 分组对 {measure} 求{_AGG_LABEL.get(agg, agg)}",
            source_columns=[by, measure],
            method="pandas groupby",
            caveat=f"{by} 取值过多（{key.nunique()} 个），不适合分组，已跳过",
        )

    grouped = value.groupby(key)
    result = getattr(grouped, agg)().dropna().sort_values(ascending=False).head(_TOP_N)
    total = float(value.sum())
    rows = {str(k): round(float(v), 4) for k, v in result.items()}
    caveat = ""
    if total:
        caveat = f"最高一组占总体的 {result.iloc[0] / total:.1%}" if agg == "sum" and result.iloc[0] else ""
    return Evidence(
        metric=f"group::{by}::{measure}::{agg}",
        value=rows,
        formula=f"按 {by} 分组，对 {measure} 求{_AGG_LABEL.get(agg, agg)}（取前 {_TOP_N}）",
        source_columns=[by, measure],
        method="pandas groupby",
        caveat=caveat,
    )


def _exec_trend(df: pd.DataFrame, params: dict) -> Evidence:
    date_col = _require(params, "date")
    measure = _require(params, "measure")
    agg = str(params.get("agg", "sum"))

    raw = _get_series(df, date_col)
    parsed = pd.to_datetime(raw, errors="coerce", format="mixed")
    valid_ratio = float(parsed.notna().mean()) if len(parsed) else 0.0
    if valid_ratio < 0.8:
        return Evidence(
            metric=f"trend::{date_col}::{measure}",
            value={},
            formula=f"按 {date_col} 月度聚合 {measure}",
            source_columns=[date_col, measure],
            method="pandas to_datetime + resample",
            caveat=f"只有 {valid_ratio:.0%} 的值能解析成日期，趋势不可靠",
        )

    value = pd.to_numeric(_get_series(df, measure), errors="coerce")
    frame = pd.DataFrame({"d": parsed.dt.to_period("M"), "v": value}).dropna()
    grouped = frame.groupby("d")["v"]
    series = getattr(grouped, agg)().sort_index()
    rows = {str(k): round(float(v), 4) for k, v in series.items()}
    return Evidence(
        metric=f"trend::{date_col}::{measure}::{agg}",
        value=rows,
        formula=f"{date_col} 按月分组，对 {measure} 求{_AGG_LABEL.get(agg, agg)}",
        source_columns=[date_col, measure],
        method="pandas period 聚合",
    )


def _derive_series(df: pd.DataFrame, params: dict) -> tuple[pd.Series, str, list[str]]:
    """把 factors 乘成一列。只支持乘法，够覆盖营收/利润这类指标。"""
    factors = params.get("factors") or []
    if not factors:
        raise PlanError("派生指标缺少 factors")

    product = pd.Series(1.0, index=df.index)
    used: list[str] = []
    parts: list[str] = []
    for factor in factors:
        if "col" in factor:
            col = factor["col"]
            used.append(col)
            product = product * pd.to_numeric(_get_series(df, col), errors="coerce")
            parts.append(col)
        elif "one_minus" in factor:
            col = factor["one_minus"]
            used.append(col)
            product = product * (1 - pd.to_numeric(_get_series(df, col), errors="coerce"))
            parts.append(f"(1-{col})")
        elif "const" in factor:
            product = product * float(factor["const"])
            parts.append(str(factor["const"]))
        else:
            raise PlanError(f"无法识别的 factor: {factor}")
    return product, " × ".join(parts), used


def _materialize_derived(df: pd.DataFrame, plan: AnalysisPlan) -> pd.DataFrame:
    """先把派生列算出来加进表，后面的 group / trend 才能按它分组。

    公式和来源列写进 df.attrs，不用模块级全局量——否则连续跑两次会串。
    """
    formulas: dict[str, str] = {}
    sources: dict[str, list[str]] = {}
    for metric in plan.metrics:
        if metric.kind != "derived":
            continue
        name = metric.params.get("name")
        if not name:
            continue
        try:
            series, formula, used = _derive_series(df, metric.params)
        except PlanError:
            continue
        if name not in df.columns:
            df = df.copy()
        df[name] = series
        formulas[name] = f"{name} = {formula}"
        sources[name] = used
    df.attrs["derived_formulas"] = formulas
    df.attrs["derived_sources"] = sources
    df.attrs["derived_names"] = list(formulas)
    return df


def _exec_derived(df: pd.DataFrame, params: dict) -> Evidence:
    """派生指标的合计值。列已在 execute_plan 里物化好，这里只负责汇总。"""
    name = _require(params, "name")
    if name not in df.columns:
        raise PlanError(f"派生列未生成: {name}")
    series = pd.to_numeric(df[name], errors="coerce")
    agg = str(params.get("agg", "sum"))
    value = float(series.mean(skipna=True)) if agg == "mean" else float(series.sum(skipna=True))
    formulas = df.attrs.get("derived_formulas", {})
    sources = df.attrs.get("derived_sources", {})
    return Evidence(
        metric=f"derived::{name}",
        value=round(value, 4),
        formula=formulas.get(name, name),
        source_columns=list(sources.get(name, [])),
        method="逐行相乘后汇总",
        caveat="若某行存在缺失值，该行不计入",
    )


def _exec_missing(df: pd.DataFrame, params: dict) -> Evidence:
    column = params.get("column")
    if column:
        series = _get_series(df, column)
        rate = float(series.isna().mean())
        return Evidence(
            metric=f"missing::{column}",
            value={"missing": int(series.isna().sum()), "rate": round(rate, 4)},
            formula="缺失数 / 总行数",
            source_columns=[column],
            method="pandas isna",
        )
    # 只统计原始列：派生列的空值是我们自己算出来的，不该算进"数据质量"
    derived = set(df.attrs.get("derived_names", []))
    source_df = df[[c for c in df.columns if c not in derived]]
    total = int(source_df.isna().sum().sum())
    cells = int(source_df.shape[0] * source_df.shape[1])
    return Evidence(
        metric="missing::all",
        value={"missing_cells": total, "rate": round(total / cells, 4) if cells else 0.0},
        formula="原始列的空单元格 / 原始单元格总数",
        source_columns=list(source_df.columns),
        method="pandas isna",
    )


def _exec_duplicates(df: pd.DataFrame, params: dict) -> Evidence:
    dup = int(df.duplicated().sum())
    return Evidence(
        metric="duplicates::all",
        value={"duplicate_rows": dup, "rate": round(dup / len(df), 4) if len(df) else 0.0},
        formula="完全重复的行数 / 总行数",
        source_columns=[],
        method="pandas duplicated",
    )


_EXECUTORS = {
    "summary": _exec_summary,
    "group": _exec_group,
    "trend": _exec_trend,
    "derived": _exec_derived,
    "missing": _exec_missing,
    "duplicates": _exec_duplicates,
}


def execute_plan(csv_path: str, plan: AnalysisPlan) -> list[Evidence]:
    """按计划逐条计算。单条失败只影响它自己，返回的每条都带 caveat。"""
    df = pd.read_csv(csv_path)
    df = _materialize_derived(df, plan)
    results: list[Evidence] = []
    for metric in plan.metrics:
        executor = _EXECUTORS.get(metric.kind)
        if executor is None:
            results.append(
                Evidence(
                    metric=metric.id, value=None, formula="",
                    source_columns=[], method="", caveat=f"未知操作: {metric.kind}",
                )
            )
            continue
        try:
            evidence = executor(df, metric.params)
        except PlanError as exc:
            evidence = Evidence(
                metric=metric.id, value=None, formula="", source_columns=[],
                method="", caveat=f"跳过：{exc}",
            )
        evidence.metric = metric.id or evidence.metric
        results.append(evidence)
    return results


# ── 默认计划：没有模型时的兜底，也用来做回归基线 ────────────────


def default_plan(profile: DatasetProfile) -> AnalysisPlan:
    """按画像推一份稳妥的计划。

    与旧引擎的关键区别：
    - 不把 id 列当分组维度（画像已经排除了）；
    - 日期列不再同时当分类列；
    - 有单价×数量×折扣这类组合时，自动加一条收入派生指标。
    """
    metrics: list[MetricSpec] = [
        MetricSpec(id="missing_all", kind="missing", title="整体缺失情况"),
        MetricSpec(id="duplicates", kind="duplicates", title="重复行"),
    ]

    for col in profile.numeric_columns:
        metrics.append(
            MetricSpec(id=f"summary_{col.name}", kind="summary", title=f"{col.name} 的分布",
                       params={"column": col.name})
        )

    cats = profile.categorical_columns
    nums = profile.numeric_columns

    # 能推出营收就先推：直接用单价求和是没有业务含义的，这是旧引擎的隐性错误。
    revenue = _guess_revenue(profile)
    if revenue is not None:
        metrics.append(
            MetricSpec(id="revenue_total", kind="derived", title="总营收",
                       params={"name": "revenue", "factors": revenue})
        )
        measure = "revenue"
    else:
        currency = next((c for c in nums if c.unit_guess == "currency"), None)
        measure = currency.name if currency else (nums[0].name if nums else None)

    if measure is not None:
        for cat in cats[:3]:
            metrics.append(
                MetricSpec(id=f"group_{cat.name}_{measure}", kind="group",
                           title=f"按 {cat.name} 看 {measure}",
                           params={"by": cat.name, "measure": measure, "agg": "sum"})
            )
        for date_col in profile.date_columns[:1]:
            metrics.append(
                MetricSpec(id=f"trend_{date_col.name}_{measure}", kind="trend",
                           title=f"{measure} 的月度趋势",
                           params={"date": date_col.name, "measure": measure, "agg": "sum"})
            )

    return AnalysisPlan(
        question="这份数据集整体情况如何，有哪些值得注意的现象",
        metrics=metrics,
        rationale="默认计划：先看数据质量，再看主要数值分布、分组与趋势，能推出营收则一并计算",
        origin="default",
    )


def _guess_revenue(profile: DatasetProfile) -> list[dict] | None:
    """找"单价 × 数量 × (1-折扣)"这类组合。找不到返回 None。"""
    by_name = {c.name.lower(): c for c in profile.numeric_columns}
    price = next((c for c in profile.numeric_columns if c.unit_guess == "currency"), None)
    quantity = next((c for c in profile.numeric_columns if c.unit_guess == "quantity"), None)
    if price is None or quantity is None:
        return None

    factors: list[dict] = [{"col": price.name}, {"col": quantity.name}]
    discount = next(
        (c for c in profile.numeric_columns if "discount" in c.name.lower() or "折扣" in c.name),
        None,
    )
    if discount is not None:
        factors.append({"one_minus": discount.name})
    return factors
