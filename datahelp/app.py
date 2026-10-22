"""统一编排层 —— 一次分析只从这里进。

流程固定为五步，每一步的产物都落盘，出问题能倒查：

    profile（确定性） → plan（模型或默认） → compute（确定性）
    → narrate（模型或直出） → deliver（确定性）

约束：
- 数值只能来自 compute，narrate 拿不到原始数据，只能看证据。
- 任何一步失败都不许伪装成成功：状态、原因、产物三者一致。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from datahelp.narrative import render_from_evidence, validate_sections
from datahelp.plan import AnalysisPlan, Evidence, PlanError, default_plan, execute_plan
from datahelp.profile import DatasetProfile, build_profile, profile_to_text

PLAN_PROMPT = """你是数据分析师。下面是一份数据集的画像。请制定分析计划。

{profile}

要求：
1. 只输出 JSON，不要解释。结构：
{{"question": "这次要回答的业务问题",
  "rationale": "为什么这么分析",
  "metrics": [
    {{"id": "唯一短名", "kind": "操作类型", "title": "人话标题", "params": {{...}}}}
  ]}}
2. kind 只能是：summary、group、trend、derived、missing、duplicates
3. 各操作的 params：
   - summary: {{"column": 列名}}
   - group: {{"by": 分类列, "measure": 数值列, "agg": "sum|mean|count"}}
   - trend: {{"date": 日期列, "measure": 数值列, "agg": "sum|mean"}}
   - derived: {{"name": 指标名, "factors": [{{"col": 列}}, {{"one_minus": 列}}, {{"const": 数}}]}}
   - missing: {{}} 或 {{"column": 列名}}
   - duplicates: {{}}
4. 不要把 id 类型的列当分组维度。不要把日期列当分类列。
5. 如果某列需要先用别的列算出来（例如营收 = 单价 × 数量 × (1-折扣)），用 derived。
6. 最多 12 条。
"""

REPORT_PROMPT = """你是数据分析师，要给一位商科初学者讲清楚这份数据。

下面是已经算好的证据。你只能解释这些数字，不能自己推算新的数字，也不能编造数据里没有的列。

{evidence}

分析计划想回答的问题是：{question}

请按以下章节写一份中文报告，章节标题必须完全一致：

## 数据概览
## 字段与业务含义
## 数据质量
## 主要指标
## 分组与趋势
## 核心发现
## 分析边界

要求：
- 每个数字都要能对应到上面的证据，不要出现证据里没有的数。
- 对每个核心发现，说清：结果是什么、用的什么口径、业务上意味着什么、这个结论不能说明什么。
- 面向初学者，解释为什么这么分析，而不只是给结论。
"""


@dataclass
class AnalysisOutcome:
    status: str
    output_dir: str
    report: str = ""
    error: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    report_ok: bool = False
    report_issues: list[str] = field(default_factory=list)
    duration: float = 0.0

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "output_dir": self.output_dir,
            "error": self.error,
            "artifacts": self.artifacts,
            "report_ok": self.report_ok,
            "report_issues": self.report_issues,
            "duration_seconds": round(self.duration, 2),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def _extract_json(text: str) -> dict:
    """模型输出可能带 ```json 包裹或前后寒暄，取第一个完整 JSON 对象。"""
    start = text.find("{")
    if start < 0:
        raise PlanError("模型输出里没有 JSON")
    depth = 0
    for idx in range(start, len(text)):
        if text[idx] == "{":
            depth += 1
        elif text[idx] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start: idx + 1])
    raise PlanError("模型输出的 JSON 不完整")


def plan_with_model(client, profile: DatasetProfile) -> AnalysisPlan:
    """让模型提计划。失败一律抛 PlanError，由调用方回退。"""
    prompt = PLAN_PROMPT.format(profile=profile_to_text(profile))
    raw = client.complete(prompt, max_tokens=2048)
    data = _extract_json(raw)
    data.setdefault("origin", "model")
    plan = AnalysisPlan.from_dict(data)
    # 计划里的列名必须真实存在，否则整份计划作废，避免算出一堆空
    known = {c.name for c in profile.columns}
    # 派生列（如 revenue）在计算时才会生成，允许被后续操作引用
    known |= {
        str(m.params.get("name"))
        for m in plan.metrics
        if m.kind == "derived" and m.params.get("name")
    }
    for metric in plan.metrics:
        for key in ("column", "by", "measure", "date"):
            value = metric.params.get(key)
            if value and value not in known:
                raise PlanError(f"计划引用了不存在的列: {value}")
        for factor in metric.params.get("factors") or []:
            col = factor.get("col") or factor.get("one_minus")
            if col and col not in known:
                raise PlanError(f"计划引用了不存在的列: {col}")
    return plan


def _evidence_to_text(evidence: list[Evidence]) -> str:
    lines = []
    for e in evidence:
        lines.append(f"- {e.metric}: {e.display_value}")
        lines.append(f"  口径: {e.formula} | 来源列: {', '.join(e.source_columns) or '无'}")
        if e.caveat:
            lines.append(f"  注意: {e.caveat}")
    return "\n".join(lines)


def analyze(
    csv_path: str,
    output_dir: str,
    provider: str = "mock",
    model: str | None = None,
    question: str = "",
    allow_model: bool = True,
) -> AnalysisOutcome:
    """跑一次完整分析。调用方只需要关心返回的 outcome。"""
    started = time.time()
    source = Path(csv_path).resolve()
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    def finish(**kwargs) -> AnalysisOutcome:
        outcome = AnalysisOutcome(duration=time.time() - started, **kwargs)
        log = outcome.to_dict()
        log.update({"input": str(source), "started_at": _now(), "finished_at": _now()})
        _write_json(out / "run_log.json", log)
        return outcome

    # ① profile
    try:
        profile = build_profile(str(source))
    except Exception as exc:  # 读不了就是读不了，不往后走
        return finish(status="failed", output_dir=str(out), error=f"无法读取数据集: {exc}")

    _write_json(out / "profile.json", profile.to_dict())

    # ② plan
    plan: AnalysisPlan
    plan_note = ""
    if allow_model and provider != "mock":
        try:
            from datahelp.models import create_model_client

            client = create_model_client(provider=provider, model=model)
            plan = plan_with_model(client, profile)
        except Exception as exc:
            plan = default_plan(profile)
            plan_note = f"模型制定计划失败，改用默认计划：{exc}"
    else:
        plan = default_plan(profile)
        if not allow_model:
            plan_note = "未启用模型，使用默认计划"
    if question:
        plan.question = question
    _write_json(out / "plan.json", plan.to_dict())

    # ③ compute
    evidence = execute_plan(str(source), plan)
    _write_json(out / "evidence.json", {"plan": plan.to_dict(), "evidence": [e.to_dict() for e in evidence]})

    computed = [e for e in evidence if e.value not in (None, {}, [])]
    if not computed:
        return finish(
            status="failed",
            output_dir=str(out),
            error="所有计算项都失败了，没有可用证据",
            artifacts={"profile": str(out / "profile.json"), "plan": str(out / "plan.json")},
        )

    # ④ narrate
    report = ""
    issues: list[str] = []
    if allow_model and provider != "mock":
        try:
            from datahelp.models import create_model_client

            client = create_model_client(provider=provider, model=model)
            prompt = REPORT_PROMPT.format(
                evidence=_evidence_to_text(evidence), question=plan.question
            )
            draft = client.complete(prompt, max_tokens=8192)
            check = validate_sections(draft)
            if check.ok:
                report = draft
            else:
                issues = check.missing
                report = draft
        except Exception as exc:
            issues = [f"模型写报告失败: {exc}"]

    if not report:
        note = plan_note or "未使用模型，报告由证据直接渲染"
        report = render_from_evidence(profile, plan, evidence, model_note=note)

    (out / "report.md").write_text(report, encoding="utf-8")
    check = validate_sections(report)

    # ⑤ deliver
    artifacts = {
        "profile": str(out / "profile.json"),
        "plan": str(out / "plan.json"),
        "evidence": str(out / "evidence.json"),
        "report": str(out / "report.md"),
    }
    try:
        from datahelp.tools_data import generate_excel, generate_html, generate_pdf

        # 工具层的路径囚笼要求输入在输出根目录内，先把数据文件放进去
        deliver_csv = source
        if source.parent != out:
            import shutil

            deliver_csv = out / source.name
            shutil.copy2(source, deliver_csv)

        generate_excel(str(deliver_csv), str(out), str(out), analysis_text=report)
        generate_html(str(deliver_csv), str(out), str(out), analysis_text=report)
        generate_pdf(str(deliver_csv), str(out), str(out), analysis_text=report)
        for pattern, key in (("_analysis.xlsx", "excel"), ("_report.html", "html"), ("_report.pdf", "pdf")):
            matches = list(out.glob(f"*{pattern}"))
            if matches:
                artifacts[key] = str(matches[0])
    except Exception as exc:
        issues.append(f"交付物生成失败: {exc}")

    return finish(
        status="completed",
        output_dir=str(out),
        report=report,
        report_ok=check.ok,
        report_issues=check.missing or issues,
        artifacts=artifacts,
    )
