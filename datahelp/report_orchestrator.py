"""报告编排器 —— 将 AnalysisResult（确定性分析证据）通过 LLM 转为结构化中文报告。

核心流程:
  1. 把证据 JSON 转为中文提示词（system + user prompt）
  2. 调用 ModelClient.complete() 要求模型仅解释证据、输出固定章节
  3. 验证报告是否包含全部 9 个必需的标准章节
  4. 额外验证"核心发现"章节中每个发现项都包含 5 个必需子要素
  5. 缺章节/缺要素时最多执行一次修订（第二次调用模型）
  6. 第二次仍不合格或发生异常时，返回基于 evidence 直接生成的降级 Markdown
     （quality_status='degraded'），降级报告仍包含全部 9 个章节框架
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from datahelp.analysis_contract import AnalysisResult, AnalysisEvidence, AnalysisPlan
from datahelp.models import ModelClient


# ══════════════════════════════════════════════════════════════════════
# ReportOutcome —— 编排器返回结果
# ══════════════════════════════════════════════════════════════════════

@dataclass
class ReportOutcome:
    """报告编排结果。"""
    text: str = ""
    quality_status: str = "standard"  # "standard" | "degraded"
    attempts: int = 0
    warnings: list[str] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════
# 必需章节定义（9 大章节）
# ══════════════════════════════════════════════════════════════════════

_REQUIRED_SECTIONS = [
    "数据概览",
    "数据质量检查",
    "基础指标分析",
    "分组与排名分析",
    "趋势分析",
    "核心发现",
    "业务建议",
    "分析边界与风险警告",
    "初学者教学总结",
]

_SECTION_ALIASES: dict[str, list[str]] = {
    "数据概览":           ["数据概览", "数据集概览"],
    "数据质量检查":       ["数据质量检查", "数据质量", "数据质量分析"],
    "基础指标分析":       ["基础指标分析", "基础指标", "描述性统计", "基本指标"],
    "分组与排名分析":     ["分组与排名分析", "分组分析", "排名分析", "分组与排名", "类别分析"],
    "趋势分析":           ["趋势分析", "时间趋势", "趋势", "月度趋势"],
    "核心发现":           ["核心发现", "关键发现", "主要发现"],
    "业务建议":           ["业务建议", "行动建议", "建议", "业务建议与行动"],
    "分析边界与风险警告": ["分析边界与风险警告", "分析边界", "风险警告", "局限性", "局限性分析", "风险提示"],
    "初学者教学总结":     ["初学者教学总结", "教学总结", "总结", "初学者总结", "学习要点"],
}

# 每个核心发现必需的 5 个子要素
_CORE_FINDING_SUB_SECTIONS = [
    "数据证据",
    "方法解释",
    "业务含义",
    "风险边界",
    "初学者复用",
]


# ══════════════════════════════════════════════════════════════════════
# Prompt 构建
# ══════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """你是数据分析报告撰写助手。你的任务是仅基于用户提供的分析证据，生成一份结构清晰、适合商科初学者阅读的数据分析报告。

## 规则
1. **仅解释提供的证据**，不要添加外部知识或猜测。模型只可解释 evidence，不能进行新的计算。
2. 报告必须包含以下 9 个章节（使用 ## 标题，不得省略或合并）：
   - 数据概览
   - 数据质量检查
   - 基础指标分析
   - 分组与排名分析
   - 趋势分析
   - 核心发现
   - 业务建议
   - 分析边界与风险警告
   - 初学者教学总结
3. 在 **核心发现** 章节中，每个发现项必须严格包含以下 5 个部分（使用 **粗体** 子标题）：
   - **数据证据**: 引用具体的数值和指标，说明数据来源
   - **方法解释**: 说明使用了什么分析方法（如分组聚合、对比分析）
   - **业务含义**: 解释这个发现对业务决策的实际意义
   - **风险边界**: 指出该发现的适用条件、数据局限性和可能的误导
   - **初学者复用**: 给出初学者可以直接套用的操作步骤或代码思路
4. 使用中文撰写，语言简洁易懂，避免专业术语不做解释。
5. 如果证据中包含 caveat（注意事项），请在对应章节中说明这些限制。
6. 不要输出额外的章节，严格按照上述 9 个章节组织报告。
7. 直接输出报告内容，不要输出思考过程。"""


def _build_evidence_prompt(result: AnalysisResult) -> str:
    """将 AnalysisResult 转为包含全部证据的 user prompt。"""
    lines: list[str] = []
    lines.append("请根据以下分析证据生成数据分析报告。\n")
    lines.append("## 数据集信息")
    lines.append(f"- 输入文件: `{result.input_file}`")
    lines.append(f"- 行数: {result.row_count}")
    lines.append(f"- 列数: {result.column_count}")
    lines.append(f"- 列名: {', '.join(result.column_names)}")
    lines.append("")

    for plan in result.plans:
        lines.append(f"### {plan.description} (plan_id: {plan.plan_id})")
        if not plan.evidence:
            lines.append("  无证据。\n")
            continue

        for ev in plan.evidence:
            val_str = _fmt_prompt_value(ev.value)
            caveat_str = f" ⚠️ {ev.caveat}" if ev.caveat else ""
            lines.append(f"- **{ev.metric_name}**: {val_str}{caveat_str}")
            lines.append(f"  - 计算公式: `{ev.formula}`")
            lines.append(f"  - 来源列: {ev.source_columns}")

        lines.append("")

    return "\n".join(lines)


def _fmt_prompt_value(v: Any) -> str:
    """将证据值格式化为人类可读字符串（用于 prompt 中）。"""
    if v is None:
        return "无"
    if isinstance(v, float):
        return f"{v:.6g}"
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, default=str)
    if isinstance(v, list):
        if not v:
            return "[]"
        return ", ".join(str(x) for x in v)
    return str(v)


# ══════════════════════════════════════════════════════════════════════
# 章节验证
# ══════════════════════════════════════════════════════════════════════

def _check_sections(text: str) -> tuple[list[str], list[str]]:
    """检查报告文本中包含哪些必需章节。

    返回:
        (present, missing) 两个列表，分别表示已出现和缺失的章节名称。
    """
    present: list[str] = []
    missing: list[str] = []

    for section in _REQUIRED_SECTIONS:
        aliases = _SECTION_ALIASES[section]
        found = False
        for alias in aliases:
            # 匹配 Markdown 标题（## / ### / #）
            if f"# {alias}" in text or f"## {alias}" in text or f"### {alias}" in text:
                found = True
                break
            # 兜底：直接出现在文本中也认可
            if alias in text:
                found = True
                break
        if found:
            present.append(section)
        else:
            missing.append(section)

    return present, missing


# ══════════════════════════════════════════════════════════════════════
# 核心发现结构验证
# ══════════════════════════════════════════════════════════════════════

def _extract_sections(text: str) -> dict[str, str]:
    """将 Markdown 文本按 ## / # 标题拆分为 {章节名: 内容} 字典。"""
    sections: dict[str, str] = {}
    current_section = "_preamble"
    current_lines: list[str] = []

    for line in text.split("\n"):
        if line.startswith("## ") or line.startswith("# "):
            if current_lines:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = line.lstrip("# ").strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        sections[current_section] = "\n".join(current_lines).strip()

    return sections


def _split_findings(text: str) -> list[str]:
    """将核心发现章节的文本拆分为单个发现项。

    尝试按编号项（1. / 1、）、子章节（###）、或破折号列表（-）拆分。
    均无效时以整个章节作为一项返回。
    """
    lines = text.split("\n")

    # 策略 1：按编号项拆分（1. xxx / 1、xxx / 1) xxx）
    findings: list[str] = []
    current: list[str] = []
    for line in lines:
        stripped = line.strip()
        if re.match(r'^\d+[\.\、\）\)]\s', stripped):
            if current:
                findings.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        findings.append("\n".join(current))

    if len(findings) > 1:
        return [f.strip() for f in findings if f.strip()]

    # 策略 2：按子章节拆分（### xxx）
    findings = []
    current = []
    for line in lines:
        if line.startswith("### "):
            if current:
                findings.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        findings.append("\n".join(current))

    if len(findings) > 1:
        return [f.strip() for f in findings if f.strip()]

    # 策略 3：按破折号列表拆分（- xxx）
    findings = []
    current = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- ") or stripped.startswith("* "):
            if current:
                findings.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        findings.append("\n".join(current))

    if len(findings) > 1:
        return [f.strip() for f in findings if f.strip()]

    # 兜底：整个章节作为一项
    return [text.strip()] if text.strip() else []


def _check_core_findings_structure(text: str) -> tuple[bool, list[str]]:
    """检查核心发现章节中每个发现项是否包含全部 5 个子要素。

    返回:
        (pass, warnings)  — pass 表示全部通过，warnings 列出缺失项。
    """
    sections = _extract_sections(text)

    # 找到核心发现章节的内容
    core_content = ""
    for alias in _SECTION_ALIASES.get("核心发现", ["核心发现"]):
        if alias in sections:
            core_content = sections[alias]
            break

    if not core_content:
        return False, ["未找到核心发现章节"]

    findings = _split_findings(core_content)
    if not findings:
        return False, ["核心发现章节为空"]

    warnings: list[str] = []
    for i, finding in enumerate(findings):
        missing = [s for s in _CORE_FINDING_SUB_SECTIONS if s not in finding]
        if missing:
            warnings.append(f"核心发现 #{i+1} 缺少: {', '.join(missing)}")

    return len(warnings) == 0, warnings


# ══════════════════════════════════════════════════════════════════════
# 降级报告生成（不依赖模型，直接从 evidence 构建）
# ══════════════════════════════════════════════════════════════════════

def _find_plan(result: AnalysisResult, plan_id: str) -> AnalysisPlan | None:
    """按 plan_id 查找 AnalysisPlan。"""
    for plan in result.plans:
        if plan.plan_id == plan_id:
            return plan
    return None


# ── 证据驱动的内容生成（供降级/直接报告共用）────────────────────

def _build_one_finding(result: AnalysisResult) -> str:
    """从 evidence 中提炼一条核心发现（含5个子要素）。"""
    candidates = []
    for plan in result.plans:
        for ev in plan.evidence:
            if ev.value is not None:
                candidates.append((plan, ev))
    if not candidates:
        return (
            "**数据证据**: 当前数据不支持该判断。\n\n"
            "**方法解释**: 分析计划中未包含可量化的证据。\n\n"
            "**业务含义**: 暂无数据支撑业务洞察。\n\n"
            "**风险边界**: 当前数据不支持该判断。\n\n"
            "**初学者复用**: 下一步建议检查数据源完整性，确认分析计划已正确执行后重新运行。"
        )
    chosen = None
    for plan, ev in candidates:
        if ev.metric_name.startswith("stat::"):
            chosen = (plan, ev)
            break
    if chosen is None:
        for plan, ev in candidates:
            if ev.metric_name.startswith("value_counts::") and ev.source_columns and 'date' not in ev.source_columns[0].lower():
                chosen = (plan, ev)
                break
    if chosen is None:
        for plan, ev in candidates:
            if ev.metric_name.startswith("monthly_trend::"):
                chosen = (plan, ev)
                break
    if chosen is None:
        chosen = candidates[0]
    plan, ev = chosen
    val_str = _fmt_prompt_value(ev.value)
    cols = "、".join(ev.source_columns) if ev.source_columns else "未指定"
    method_label = {
        "numeric_stats": "描述性统计", "categorical": "分组聚合与排名",
        "date_trend": "时间趋势分析", "missing": "缺失值分析", "duplicates": "重复值检测",
    }.get(plan.plan_id, "数据分析")
    business_map = {
        "numeric_stats": "该指标反映了数据的集中趋势或离散程度，可作为业务决策的量化基准。",
        "categorical": "排名结果可直接用于资源分配决策或确定重点优化方向。",
        "date_trend": "趋势变化有助于预判业务走向，辅助制定分阶段业务目标。",
        "missing": "缺失比例直接影响分析可靠性，需优先修复数据采集环节。",
        "duplicates": "重复记录会扭曲聚合统计结果，建议在入库时建立去重规则。",
    }
    business = business_map.get(plan.plan_id, "该指标可用于辅助业务判断。")
    risk = ev.caveat if ev.caveat else "当前数据不支持该判断，建议结合更多维度数据交叉验证结论的稳健性。"
    reuse_map = {
        "numeric_stats": f"可使用 `df['{ev.source_columns[0] if ev.source_columns else '列名'}'].describe()` 快速获取描述性统计。",
        "categorical": "可使用 `df.groupby('分组列')['指标列'].agg(['mean','sum','count'])` 实现分组聚合。",
        "date_trend": "可使用 `df.resample('M', on='日期列')['指标列'].sum()` 绘制月度趋势。",
        "missing": "可使用 `df.isnull().sum()` / `df.isnull().mean()` 快速检查缺失情况。",
        "duplicates": "可使用 `df.duplicated().sum()` 检测重复行数。",
    }
    reuse = reuse_map.get(plan.plan_id, "建议按「数据清洗→描述性统计→分组分析→趋势分析」的流程实操练习。")
    return "\n".join([
        f"**数据证据**: 分析发现 **{ev.metric_name}** 的值为 {val_str}，来源列为 {cols}。",
        "",
        f"**方法解释**: 该指标通过 `{ev.formula}` 计算，属于 **{method_label}**。",
        "",
        f"**业务含义**: {business}",
        "",
        f"**风险边界**: {risk}",
        "",
        f"**初学者复用**: {reuse}",
    ])


def _build_business_advice(result: AnalysisResult) -> str:
    """基于证据生成业务建议段落（最多 4 条，质量建议固定为第 1 条）。"""
    lines = []

    # ── 1. 质量部分（固定第 1 条） ──
    total_missing_rate = None
    duplicate_rate = None
    for plan in result.plans:
        for ev in plan.evidence:
            if ev.metric_name == "total_missing_rate" and ev.value is not None:
                total_missing_rate = ev.value
            if ev.metric_name == "duplicate_rate" and ev.value is not None:
                duplicate_rate = ev.value

    quality_has_issue = False
    if (total_missing_rate is not None and total_missing_rate > 0) or (duplicate_rate is not None and duplicate_rate > 0):
        quality_has_issue = True
        parts = []
        if total_missing_rate is not None and total_missing_rate > 0:
            parts.append(f"缺失率 {total_missing_rate}")
        if duplicate_rate is not None and duplicate_rate > 0:
            parts.append(f"重复率 {duplicate_rate}")
        lines.append(f"数据质量方面，{'、'.join(parts)}，建议进行数据清洗后重新分析。")
    else:
        lines.append("当前质量检查未发现缺失或重复记录，可进入业务指标分析。")

    # ── 2. 收集数值/分类/趋势候选（按原顺序） ──
    candidates: list[str] = []

    # 数字部分
    stat_evidences = []
    for plan in result.plans:
        for ev in plan.evidence:
            if ev.metric_name.startswith("stat::") and ev.value is not None:
                stat_evidences.append(ev)
    for ev in stat_evidences[:2]:
        val_str = _fmt_prompt_value(ev.value)
        candidates.append(f"指标 {ev.metric_name} 的值为 {val_str}，建议关注该指标的波动情况。")

    # 分类部分
    cat_ev = None
    for plan in result.plans:
        for ev in plan.evidence:
            if ev.metric_name.startswith("value_counts::") and ev.value is not None:
                if ev.source_columns and 'date' in ev.source_columns[0].lower():
                    continue
                if isinstance(ev.value, dict) and len(ev.value) >= 2:
                    cat_ev = ev
                    break
        if cat_ev is not None:
            break
    if cat_ev is not None:
        val_str = _fmt_prompt_value(cat_ev.value)
        candidates.append(f"分类指标 {cat_ev.metric_name} 显示{val_str}，可基于类别差异制定策略。")

    # 趋势部分
    trend_ev = None
    for plan in result.plans:
        for ev in plan.evidence:
            if ev.metric_name.startswith("monthly_trend::") and ev.value is not None:
                trend_ev = ev
                break
        if trend_ev is not None:
            break
    if trend_ev is not None:
        val_str = _fmt_prompt_value(trend_ev.value)
        candidates.append(f"趋势指标 {trend_ev.metric_name} 显示{val_str}，可作为业务节奏调整的参考。")

    # ── 3. 填充候选，确保总数不超过 4（质量 1 条 + 最多 3 条） ──
    lines.extend(candidates[:3])

    # 若没有任何实际业务建议
    has_business_advice = bool(stat_evidences[:2]) or cat_ev is not None or trend_ev is not None or quality_has_issue
    if not has_business_advice:
        return "当前数据不支持生成业务建议。下一步动作：补充指标或检查数据源后重试。"

    return "\n".join(lines)


def _build_teaching_summary(result: AnalysisResult) -> str:
    """基于使用的方法生成教学总结段落。"""
    methods = []
    has_data = False
    for plan in result.plans:
        if plan.evidence and any(e.value is not None for e in plan.evidence):
            has_data = True
            label = {
                "numeric_stats": "描述性统计（均值/标准差等）",
                "categorical": "分组聚合与排名",
                "date_trend": "时间序列趋势",
                "missing": "缺失值检测",
                "duplicates": "重复值检测",
            }.get(plan.plan_id)
            if label:
                methods.append(label)
    if not has_data:
        return "本次分析未产生有效证据。**下一步动作**: 检查数据源是否为空或格式异常，修正后重新运行。"
    mstr = "、".join(methods)
    return (
        f"本次分析主要使用了 **{mstr}** 方法。\n\n"
        "- **学习要点**: 数据分析应从数据质量检查（缺失/重复）开始，再按指标类型选择描述性或分组分析方法。\n"
        "- **实操建议**: 初学者可先掌握 `df.describe()` 和 `df.groupby()` 两个核心操作，再拓展趋势分析。\n"
        "- **风险意识**: 任何分析结论都应结合业务背景解读，注意 caveat 中标注的数据局限性。"
    )


# ── 降级报告生成（不依赖模型，直接从 evidence 构建）─────────────

def _build_degraded_report(result: AnalysisResult) -> str:
    """基于 AnalysisResult 直接生成降级版 Markdown 报告。

    包含全部 9 个必需章节框架，数据来源于已有证据。
    """
    lines: list[str] = []
    lines.append("# 数据分析报告（降级版）\n")
    lines.append("> ⚠️ 由于模型生成报告未通过质量标准，以下为基于分析证据自动生成的降级报告。\n")

    # ── 数据概览 ──
    lines.append("## 数据概览\n")
    lines.append(f"- **输入文件**: `{result.input_file}`")
    lines.append(f"- **行数**: {result.row_count}")
    lines.append(f"- **列数**: {result.column_count}")
    lines.append(f"- **列名**: {', '.join(result.column_names)}")
    lines.append("")

    # ── 数据质量检查 ──
    lines.append("## 数据质量检查\n")
    missing_plan = _find_plan(result, "missing")
    dup_plan = _find_plan(result, "duplicates")
    if missing_plan:
        for ev in missing_plan.evidence:
            val_str = _fmt_prompt_value(ev.value)
            caveat_str = f" — ⚠️ {ev.caveat}" if ev.caveat else ""
            lines.append(f"- **{ev.metric_name}**: {val_str}{caveat_str}")
    if dup_plan:
        for ev in dup_plan.evidence:
            val_str = _fmt_prompt_value(ev.value)
            lines.append(f"- **{ev.metric_name}**: {val_str}")
    if not missing_plan and not dup_plan:
        lines.append("_无质量检查证据。_")
    lines.append("")

    # ── 基础指标分析 ──
    lines.append("## 基础指标分析\n")
    ns_plan = _find_plan(result, "numeric_stats")
    if ns_plan and ns_plan.evidence:
        for ev in ns_plan.evidence:
            val_str = _fmt_prompt_value(ev.value)
            caveat_str = f" — ⚠️ {ev.caveat}" if ev.caveat else ""
            lines.append(f"- **{ev.metric_name}**: {val_str}{caveat_str}")
    else:
        lines.append("_无数值指标证据。_")
    lines.append("")

    # ── 分组与排名分析 ──
    lines.append("## 分组与排名分析\n")
    cat_plan = _find_plan(result, "categorical")
    if cat_plan and cat_plan.evidence:
        for ev in cat_plan.evidence:
            val_str = _fmt_prompt_value(ev.value)
            lines.append(f"- **{ev.metric_name}**: {val_str}")
    else:
        lines.append("_无分类聚合证据。_")
    lines.append("")

    # ── 趋势分析 ──
    lines.append("## 趋势分析\n")
    dt_plan = _find_plan(result, "date_trend")
    if dt_plan and dt_plan.evidence:
        for ev in dt_plan.evidence:
            val_str = _fmt_prompt_value(ev.value)
            caveat_str = f" — ⚠️ {ev.caveat}" if ev.caveat else ""
            lines.append(f"- **{ev.metric_name}**: {val_str}{caveat_str}")
    else:
        lines.append("_无趋势分析证据。_")
    lines.append("")

    # ── 核心发现 ──
    lines.append("## 核心发现\n")
    lines.append(_build_one_finding(result))
    lines.append("")

    # ── 业务建议 ──
    lines.append("## 业务建议\n")
    lines.append(_build_business_advice(result))
    lines.append("")

    # ── 分析边界与风险警告 ──
    lines.append("## 分析边界与风险警告\n")
    all_caveats: list[str] = []
    for plan in result.plans:
        for ev in plan.evidence:
            if ev.caveat:
                all_caveats.append(f"- **{ev.metric_name}**: {ev.caveat}")
    if all_caveats:
        lines.extend(all_caveats)
    else:
        lines.append("_分析过程中未发现明显的边界或风险。_")
    lines.append("")

    # ── 初学者教学总结 ──
    lines.append("## 初学者教学总结\n")
    lines.append(_build_teaching_summary(result))
    lines.append("")

    return "\n".join(lines)


# ── 公开 wrapper：直接生成完整证据报告 ──────────────────────────

def build_evidence_report(result: AnalysisResult) -> str:
    """基于 AnalysisResult 直接生成完整的 Markdown 报告（免 LLM 调用）。

    包含全部 9 个标准章节，核心发现/业务建议/教学总结基于已有证据自动填充。
    适用于不需要 LLM 调用或 LLM 生成失败的降级替代场景。
    """
    return _build_degraded_report(result)


# ══════════════════════════════════════════════════════════════════════
# ReportOrchestrator
# ══════════════════════════════════════════════════════════════════════

class ReportOrchestrator:
    """报告编排器：将 AnalysisResult（分析证据）通过 LLM 转为结构化中文报告。

    用法::

        outcome = ReportOrchestrator(client, result, mode="standard_report").run()
        print(outcome.text)
        print(outcome.quality_status)

    流程:
        1. 将证据转为中文提示
        2. 调用模型生成报告（仅解释证据，输出固定章节）
        3. 验证必需章节
        4. 如缺章节，最多执行一次修订（第二次调用模型）
        5. 如第二次仍不合格或发生异常，返回降级 Markdown
           （quality_status='degraded'）
    """

    def __init__(
        self,
        client: ModelClient,
        result: AnalysisResult,
        mode: str = "standard_report",
    ):
        """初始化编排器。

        参数:
            client:  模型客户端（ModelClient 接口）
            result:  确定性分析结果（AnalysisResult）
            mode:    报告模式（"standard_report" / "audit_report" 等）
        """
        self._client = client
        self._result = result
        self._mode = mode

    # ── 公开接口 ───────────────────────────────────────────────────

    def run(self) -> ReportOutcome:
        """执行报告编排，返回 ReportOutcome。"""
        attempts = 0
        warnings: list[str] = []

        # 构建 prompt
        system_prompt = self._build_system_prompt()
        user_prompt = _build_evidence_prompt(self._result)

        # ── 第一次调用 ─────────────────────────────────────────────
        attempts += 1
        try:
            report = self._call_model(system_prompt, user_prompt)
        except Exception as e:
            warnings.append(f"模型调用异常: {e}")
            return ReportOutcome(
                text=_build_degraded_report(self._result),
                quality_status="degraded",
                attempts=attempts,
                warnings=warnings,
            )

        # ── 验证：章节 + 核心发现结构 ──────────────────────────────
        present, missing = _check_sections(report)
        pass_core, core_warnings = _check_core_findings_structure(report)

        if not missing and pass_core:
            return ReportOutcome(
                text=report,
                quality_status="standard",
                attempts=attempts,
                warnings=warnings,
            )

        # ── 修订（第二次调用） ─────────────────────────────────────
        all_issues: list[str] = []
        if missing:
            all_issues.append(f"缺少章节: {', '.join(missing)}")
        if core_warnings:
            all_issues.extend(core_warnings)

        warnings.extend(all_issues)
        revision_prompt = self._build_revision_prompt(all_issues, user_prompt)

        attempts += 1
        try:
            report = self._call_model(system_prompt, revision_prompt)
        except Exception as e:
            warnings.append(f"修订模型调用异常: {e}")
            return ReportOutcome(
                text=_build_degraded_report(self._result),
                quality_status="degraded",
                attempts=attempts,
                warnings=warnings,
            )

        # ── 再次验证 ───────────────────────────────────────────────
        present, missing = _check_sections(report)
        pass_core, core_warnings = _check_core_findings_structure(report)

        if missing or not pass_core:
            final_issues: list[str] = []
            if missing:
                final_issues.append(f"缺少章节: {', '.join(missing)}")
            if core_warnings:
                final_issues.extend(core_warnings)
            warnings.append(f"修订后仍有问题: {'; '.join(final_issues)}")
            return ReportOutcome(
                text=_build_degraded_report(self._result),
                quality_status="degraded",
                attempts=attempts,
                warnings=warnings,
            )

        return ReportOutcome(
            text=report,
            quality_status="standard",
            attempts=attempts,
            warnings=warnings,
        )

    # ── 内部方法 ───────────────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        """根据 mode 构建 system prompt。"""
        if self._mode == "audit_report":
            return (
                _SYSTEM_PROMPT
                + "\n\n## 附加要求\n"
                + "- 本报告为审计用途，请使用正式严谨的语言。\n"
                + "- 每个数据点必须引用其计算公式与来源列。"
            )
        return _SYSTEM_PROMPT

    def _build_revision_prompt(self, issues: list[str], original_evidence: str) -> str:
        """构建修订 prompt，指出问题清单。"""
        lines: list[str] = [
            "你之前生成的报告存在以下问题：",
            "",
        ]
        for issue in issues:
            lines.append(f"- {issue}")
        lines.append("")
        lines.append("请修正上述问题，仅基于已有证据解释，不要添加外部知识。")
        lines.append("")
        if any("缺少章节" in issue for issue in issues):
            lines.append("确保报告包含全部 9 个必需章节（使用 ## 标题）。")
        if any("核心发现" in issue for issue in issues):
            lines.append(
                "确保每个核心发现都包含 **数据证据**、**方法解释**、"
                "**业务含义**、**风险边界**、**初学者复用** 五个子部分。"
            )
        lines.append("")
        lines.append("原始证据如下：")
        lines.append(original_evidence)
        return "\n".join(lines)

    def _call_model(self, system_prompt: str, user_prompt: str) -> str:
        """组合 system + user prompt 并调用模型。"""
        full_prompt = f"{system_prompt}\n\n{user_prompt}"
        return self._client.complete(full_prompt, max_tokens=8192)
