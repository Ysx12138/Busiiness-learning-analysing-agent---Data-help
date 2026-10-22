"""报告渲染与校验。

两条路径共用同一份章节定义：
- 有模型时：模型按这份结构写，写完用 validate_sections 检查；
- 没模型或模型失败时：render_from_evidence 直接把证据渲染成同一份结构，
  而不是编一段话。

章节定义只有这一处。旧代码里有四份互斥的定义，改一处不传导。
"""
from __future__ import annotations

from dataclasses import dataclass

from datahelp.plan import AnalysisPlan, Evidence
from datahelp.profile import DatasetProfile, profile_to_text

# 章节顺序即报告顺序；required=True 表示缺了这一节就不算合格报告
SECTIONS: list[tuple[str, bool]] = [
    ("数据概览", True),
    ("字段与业务含义", True),
    ("数据质量", True),
    ("主要指标", True),
    ("分组与趋势", True),
    ("核心发现", True),
    ("分析边界", True),
]


@dataclass
class ReportCheck:
    ok: bool
    missing: list[str]
    present: list[str]

    def summary(self) -> str:
        if self.ok:
            return f"结构完整（{len(self.present)} 节）"
        return "缺少章节：" + "、".join(self.missing)


def validate_sections(text: str) -> ReportCheck:
    """按章节标题检查，不要求顺序，但要求每节都有实际内容。"""
    present: list[str] = []
    missing: list[str] = []
    for title, required in SECTIONS:
        marker = f"## {title}"
        idx = text.find(marker)
        if idx < 0:
            if required:
                missing.append(title)
            continue
        body = text[idx + len(marker):]
        next_idx = body.find("\n## ")
        body = body[:next_idx] if next_idx >= 0 else body
        if len(body.strip()) < 10:
            if required:
                missing.append(title)
            continue
        present.append(title)
    return ReportCheck(ok=not missing, missing=missing, present=present)


_MAX_ITEMS = 12


def _fmt(value) -> str:
    if isinstance(value, dict):
        items = list(value.items())
        shown = "、".join(f"{k} {_fmt(v)}" for k, v in items[:_MAX_ITEMS])
        return shown + ("…" if len(items) > _MAX_ITEMS else "")
    if isinstance(value, float):
        # 大数用千分位，小数保留有效位，避免出现 2.032e+06 这种
        if abs(value) >= 1000:
            return f"{value:,.0f}"
        return f"{value:,.4g}"
    return str(value)


def render_from_evidence(
    profile: DatasetProfile,
    plan: AnalysisPlan,
    evidence: list[Evidence],
    model_note: str = "",
) -> str:
    """不调模型，直接把画像和证据渲染成报告。

    这是"降级"但诚实的版本：它不会扩散出推理，只说算出来的东西。
    """
    titles = {m.id: m.title for m in plan.metrics}
    lines: list[str] = []
    lines.append("# 数据分析报告")
    lines.append("")
    if model_note:
        lines.append(f"> {model_note}")
        lines.append("")
    lines.append(f"**分析问题**：{plan.question}")
    lines.append("")

    # 1 数据概览
    lines.append("## 数据概览")
    lines.append("")
    lines.append(f"- 规模：{profile.rows} 行 × {profile.cols} 列")
    kinds: dict[str, int] = {}
    for c in profile.columns:
        kinds[c.kind] = kinds.get(c.kind, 0) + 1
    lines.append("- 字段构成：" + "、".join(f"{k} {v} 列" for k, v in kinds.items()))
    lines.append(f"- 完全重复行：{profile.duplicate_rows} 行")
    lines.append("")

    # 2 字段与业务含义
    lines.append("## 字段与业务含义")
    lines.append("")
    lines.append("以下是字段的客观描述；业务含义需要结合具体场景判断。")
    lines.append("")
    for c in profile.columns:
        bits = [f"**{c.name}**（{c.kind}）"]
        if c.kind == "numeric":
            bits.append(f"范围 {c.min}–{c.max}，均值 {c.mean}")
        if c.kind == "datetime":
            bits.append("可解析为日期")
        if c.kind == "id":
            bits.append("疑似主键，不宜作为分组维度")
        if c.kind in ("categorical", "boolean"):
            bits.append(f"{c.unique} 个取值")
        if c.missing:
            bits.append(f"缺失 {c.missing}（{c.missing_rate:.1%}）")
        if c.samples:
            bits.append("示例：" + " / ".join(c.samples[:3]))
        lines.append("- " + "；".join(bits))
    lines.append("")

    # 3 数据质量
    lines.append("## 数据质量")
    lines.append("")
    missing_ev = next((e for e in evidence if e.metric.startswith("missing")), None)
    dup_ev = next((e for e in evidence if e.metric.startswith("duplicates")), None)
    if missing_ev:
        lines.append(f"- 缺失：{_fmt(missing_ev.value)}（口径：{missing_ev.formula}）")
    for c in profile.columns:
        if c.missing:
            lines.append(f"- {c.name} 缺失 {c.missing} 个（{c.missing_rate:.1%}）")
    if dup_ev:
        lines.append(f"- 重复：{_fmt(dup_ev.value)}（口径：{dup_ev.formula}）")
    for w in profile.warnings:
        lines.append(f"- 注意：{w}")
    lines.append("")

    # 4 主要指标
    lines.append("## 主要指标")
    lines.append("")
    numeric_ev = [e for e in evidence if e.metric.startswith("summary") or e.metric.startswith("derived")]
    if not numeric_ev:
        lines.append("这份数据里没有可用于定量分析的数值列。")
    for e in numeric_ev:
        lines.append(f"- **{titles.get(e.metric, e.metric)}**：{_fmt(e.value)}")
        lines.append(f"  - 口径：{e.formula}；来源列：{'、'.join(e.source_columns)}")
        if e.caveat:
            lines.append(f"  - 注意：{e.caveat}")
    lines.append("")

    # 5 分组与趋势
    lines.append("## 分组与趋势")
    lines.append("")
    group_ev = [e for e in evidence if e.metric.startswith("group") or e.metric.startswith("trend")]
    if not group_ev:
        lines.append("没有可分组列或可解析的日期列，这一节跳过。")
    for e in group_ev:
        lines.append(f"- **{titles.get(e.metric, e.metric)}**：{_fmt(e.value)}")
        lines.append(f"  - 口径：{e.formula}")
        if e.caveat:
            lines.append(f"  - 注意：{e.caveat}")
    lines.append("")

    # 6 核心发现
    lines.append("## 核心发现")
    lines.append("")
    findings = _derived_findings(profile, evidence, titles)
    if not findings:
        lines.append("这一轮没有算出足以称为发现的东西。")
    for item in findings:
        lines.append(f"- {item}")
    lines.append("")

    # 7 分析边界
    lines.append("## 分析边界")
    lines.append("")
    lines.append("- 报告里的数字全部来自上面的确定性计算，没有一处由模型推算。")
    skipped = [e for e in evidence if e.caveat]
    if skipped:
        lines.append(f"- 有 {len(skipped)} 项计算被跳过或打了折扣，原因逐条写在对应条目下。")
    lines.append("- 只能说明数据里有什么，不能说明为什么，也不能替代业务判断。")
    lines.append("")

    return "\n".join(lines)


def _derived_findings(profile: DatasetProfile, evidence: list[Evidence], titles: dict[str, str] | None = None) -> list[str]:
    titles = titles or {}
    """从证据里挑出可以说的话。只做事实陈述，不做因果推断。"""
    findings: list[str] = []
    for e in evidence:
        if e.metric.startswith("derived") and isinstance(e.value, (int, float)):
            findings.append(f"{titles.get(e.metric, e.metric)} 合计约 {_fmt(e.value)}（{e.formula}）")
        elif e.metric.startswith("group") and isinstance(e.value, dict) and e.value:
            top = max(e.value.items(), key=lambda kv: kv[1])
            findings.append(f"{e.formula}，最高的一组是「{top[0]}」，为 {_fmt(top[1])}")
        elif e.metric.startswith("trend") and isinstance(e.value, dict) and e.value:
            # 只比首尾容易被季节性骗了（这类数据年底冲高很常见），改报峰谷
            items = sorted(e.value.items())
            peak_month, peak = max(items, key=lambda kv: kv[1])
            low_month, low = min(items, key=lambda kv: kv[1])
            ratio = f"，峰谷相差 {peak / low:.1f} 倍" if low else ""
            findings.append(
                f"{titles.get(e.metric, e.metric)}：最高 {peak_month}（{_fmt(peak)}），"
                f"最低 {low_month}（{_fmt(low)}）{ratio}"
            )
    return findings
