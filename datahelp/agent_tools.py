"""Agent-facing data tools for product-grade analysis workflows.

These tools keep the Agent in charge of deciding the next step while moving
deterministic data work into safe, structured actions. They are intentionally
higher level than ``write_file`` / ``run_shell`` so a consumer-facing Agent can
analyze data without improvising Python scripts for every run.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from datahelp.analysis_contract import AnalysisResult
from datahelp.analysis_engine import DeterministicAnalysisEngine
from datahelp.report_orchestrator import build_evidence_report
from datahelp.tools import resolve_path


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _load_result(path: Path) -> AnalysisResult:
    return AnalysisResult.from_json(path.read_text(encoding="utf-8"))


def _default_evidence_path(repo_root: str, args: dict) -> Path:
    raw = args.get("evidence_path") or "analysis_evidence.json"
    return Path(resolve_path(raw, repo_root))


def inspect_dataset(args: dict, repo_root: str) -> str:
    """Inspect a CSV/XLSX file and return product-friendly dataset context."""
    path = Path(resolve_path(args["path"], repo_root))
    if not path.exists():
        return _json({"status": "failed", "reason": f"文件不存在: {path}"})
    if not path.is_file():
        return _json({"status": "failed", "reason": f"不是文件: {path}"})

    try:
        if path.suffix.lower() == ".csv":
            df = pd.read_csv(path)
        elif path.suffix.lower() in (".xlsx", ".xls"):
            df = pd.read_excel(path)
        else:
            return _json({"status": "failed", "reason": f"不支持的文件格式: {path.suffix}"})
    except Exception as exc:
        return _json({"status": "failed", "reason": f"读取失败: {exc}"})

    columns = []
    for col in df.columns:
        series = df[col]
        non_null = int(series.notna().sum())
        columns.append({
            "name": str(col),
            "dtype": str(series.dtype),
            "non_null": non_null,
            "missing": int(series.isna().sum()),
            "unique": int(series.nunique(dropna=True)),
            "sample_values": [str(v) for v in series.dropna().head(3).tolist()],
        })

    numeric_columns = [str(c) for c in df.select_dtypes(include="number").columns]
    likely_date_columns = []
    for col in df.columns:
        name = str(col).lower()
        if any(k in name for k in ("date", "time", "day", "month", "日期", "时间")):
            likely_date_columns.append(str(col))

    return _json({
        "status": "ok",
        "path": str(path),
        "row_count": int(len(df)),
        "column_count": int(len(df.columns)),
        "columns": columns,
        "numeric_columns": numeric_columns,
        "likely_date_columns": likely_date_columns,
        "next_recommended_tool": "suggest_analysis_plan",
    })


def suggest_analysis_plan(args: dict, repo_root: str) -> str:
    """Suggest a small, explicit plan from fields and user goal."""
    inspection = json.loads(inspect_dataset({"path": args["path"]}, repo_root))
    if inspection.get("status") != "ok":
        return _json(inspection)

    goal = args.get("goal") or "生成适合商科初学者的数据分析学习报告"
    numeric = inspection.get("numeric_columns", [])
    dates = inspection.get("likely_date_columns", [])
    categorical = [
        c["name"] for c in inspection.get("columns", [])
        if c["name"] not in numeric and 1 < int(c.get("unique", 0)) <= 50
    ][:5]

    steps = [
        {"id": "quality", "tool": "run_analysis_evidence", "why": "先确认缺失、重复和字段风险，避免报告建立在脏数据上。"},
        {"id": "numeric", "tool": "run_analysis_evidence", "why": "用确定性统计获得均值、中位数、分布等可信证据。"},
    ]
    if categorical:
        steps.append({"id": "grouping", "tool": "run_analysis_evidence", "why": "按低基数字段做分组和排名，帮助学生理解差异。"})
    if dates:
        steps.append({"id": "trend", "tool": "run_analysis_evidence", "why": "存在疑似日期字段，可尝试趋势分析并说明边界。"})
    steps.extend([
        {"id": "validate", "tool": "validate_evidence", "why": "检查证据是否真实可用，失败时不要伪装成功。"},
        {"id": "report", "tool": "compose_report_from_evidence", "why": "只基于证据生成教学报告，降低大模型编造风险。"},
    ])

    return _json({
        "status": "ok",
        "goal": goal,
        "dataset": {
            "rows": inspection["row_count"],
            "columns": inspection["column_count"],
            "numeric_columns": numeric,
            "categorical_candidates": categorical,
            "likely_date_columns": dates,
        },
        "plan": steps,
        "next_recommended_tool": "run_analysis_evidence",
    })


def run_analysis_evidence(args: dict, repo_root: str) -> str:
    """Run deterministic evidence generation as an Agent-selected tool."""
    path = Path(resolve_path(args["path"], repo_root))
    output_dir = Path(resolve_path(args.get("output_dir") or ".", repo_root))
    output_dir.mkdir(parents=True, exist_ok=True)

    result = DeterministicAnalysisEngine.run(str(path), str(output_dir))
    evidence_path = output_dir / "analysis_evidence.json"
    evidence_md_path = output_dir / "analysis_evidence.md"

    has_read_failure = any(
        ev.metric_name == "read_error" and ev.value
        for ev in result.all_evidence()
    )
    has_empty = result.row_count == 0

    status = "failed" if has_read_failure else "empty" if has_empty else "ok"
    return _json({
        "status": status,
        "input_file": str(path),
        "row_count": result.row_count,
        "column_count": result.column_count,
        "evidence_path": str(evidence_path),
        "evidence_md_path": str(evidence_md_path),
        "plans": [p.plan_id for p in result.plans],
        "caveat_count": sum(1 for ev in result.all_evidence() if ev.caveat),
        "next_recommended_tool": "validate_evidence" if status == "ok" else "inspect_dataset",
    })


def validate_evidence(args: dict, repo_root: str) -> str:
    """Validate evidence quality before any report is presented to users."""
    evidence_path = _default_evidence_path(repo_root, args)
    if not evidence_path.exists():
        return _json({
            "status": "failed",
            "reason": f"证据文件不存在: {evidence_path}",
            "can_report": False,
        })

    try:
        result = _load_result(evidence_path)
    except Exception as exc:
        return _json({
            "status": "failed",
            "reason": f"证据文件无法解析: {exc}",
            "can_report": False,
        })

    issues = []
    if result.row_count <= 0:
        issues.append("数据为空或读取失败，不能生成正常分析报告。")
    if result.column_count <= 0:
        issues.append("未识别到有效字段。")
    if not result.plans:
        issues.append("没有生成任何分析计划证据。")

    all_evidence = result.all_evidence()
    if not all_evidence:
        issues.append("没有可引用的证据项。")
    if any(ev.metric_name == "read_error" for ev in all_evidence):
        issues.append("原始文件读取失败。")

    return _json({
        "status": "ok" if not issues else "degraded",
        "can_report": bool(all_evidence) and not any("读取失败" in x for x in issues),
        "issues": issues,
        "evidence_count": len(all_evidence),
        "caveat_count": sum(1 for ev in all_evidence if ev.caveat),
        "required_next_step": "compose_report_from_evidence" if not issues else "ask_user_or_fix_input",
    })


def compose_report_from_evidence(args: dict, repo_root: str) -> str:
    """Compose a deterministic fallback report from validated evidence."""
    evidence_path = _default_evidence_path(repo_root, args)
    if not evidence_path.exists():
        return "错误: 缺少 analysis_evidence.json，无法生成报告。"
    try:
        result = _load_result(evidence_path)
    except Exception as exc:
        return f"错误: 证据文件无法解析 - {exc}"
    return build_evidence_report(result)
