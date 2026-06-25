"""Skill Engine —— SKILL.md 结构化规则引擎。

在 SKILL.md（纯文本 prompt）和 Agent Runtime（工具循环）之间增加一层
可编程规则层，让 Agent 行为真正按 skill 定义执行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ── 模块开关定义 ───────────────────────────────────────

MODULE_NAMES = [
    "dataset_risk_check",
    "cleaning_impact",
    "field_mapping",
    "metric_formulas",
    "recommendation_table",
    "audit_detail",
    "next_analysis",
]


@dataclass
class SkillConfig:
    """Skill 配置：输出模式 + 模块覆写开关。"""
    mode: str = "beginner_summary"  # beginner_summary | standard_report | audit_report
    toggles: dict[str, bool] = field(default_factory=dict)

    def __post_init__(self):
        if self.mode not in ("beginner_summary", "standard_report", "audit_report"):
            self.mode = "beginner_summary"

    def is_on(self, module: str) -> bool:
        default = _MODULE_DEFAULTS[self.mode].get(module, False)
        return self.toggles.get(module, default)

    def to_toggle_string(self) -> str:
        parts = [f"输出模式: {self.mode}"]
        for m in MODULE_NAMES:
            if m in self.toggles:
                parts.append(f"{'+' if self.toggles[m] else '-'}{m}")
        return ", ".join(parts)


# ── 模块默认值（来自 SKILL §7） ─────────────────────

_MODULE_DEFAULTS: dict[str, dict[str, bool]] = {
    "beginner_summary": {
        "dataset_risk_check": True,
        "cleaning_impact": True,
        "field_mapping": True,
        "metric_formulas": True,
        "recommendation_table": False,
        "audit_detail": False,
        "next_analysis": True,
    },
    "standard_report": {
        "dataset_risk_check": True,
        "cleaning_impact": True,
        "field_mapping": True,
        "metric_formulas": True,
        "recommendation_table": True,
        "audit_detail": False,
        "next_analysis": True,
    },
    "audit_report": {
        "dataset_risk_check": True,
        "cleaning_impact": True,
        "field_mapping": True,
        "metric_formulas": True,
        "recommendation_table": True,
        "audit_detail": True,
        "next_analysis": True,
    },
}


# ── 报告章节定义（来自 SKILL §9 + runtime.py SYSTEM_PROMPT） ──

_REQUIRED_SECTIONS: dict[str, list[str]] = {
    "beginner_summary": [
        "数据概览",
        "字段识别与业务含义",
        "数据质量检查",
        "基础指标分析",
        "核心发现",
        "业务建议",
        "初学者教学总结",
    ],
    "standard_report": [
        "数据概览",
        "字段识别与业务含义",
        "数据质量检查",
        "基础指标分析",
        "分组与排名分析",
        "趋势分析",
        "核心发现",
        "业务建议",
        "进阶分析推荐",
        "跳过的高级方法及原因",
        "分析边界与风险警告",
        "初学者教学总结",
    ],
    "audit_report": [
        "数据概览",
        "字段识别与业务含义",
        "数据质量检查",
        "基础指标分析",
        "分组与排名分析",
        "趋势分析",
        "核心发现",
        "业务建议",
        "进阶分析推荐",
        "跳过的高级方法及原因",
        "分析边界与风险警告",
        "初学者教学总结",
        "质量检查与改进建议",
    ],
}

# 章节标题的可用变体（模型可能使用近义词）
_SECTION_ALIASES: dict[str, list[str]] = {
    "数据概览": ["数据概览", "数据集概览", "数据概述", "Data Overview"],
    "字段识别与业务含义": ["字段识别", "字段说明", "字段业务含义", "字段识别与业务含义", "字段映射"],
    "数据质量检查": ["数据质量检查", "数据质量", "数据质量分析", "数据质量问题"],
    "基础指标分析": ["基础指标分析", "描述性统计", "指标分析", "基础指标", "Basic Metrics"],
    "分组与排名分析": ["分组与排名分析", "分组分析", "排名分析", "分组聚合", "Grouping and Ranking"],
    "趋势分析": ["趋势分析", "时间趋势", "月度趋势", "Trend Analysis"],
    "核心发现": ["核心发现", "关键发现", "主要发现", "Key Findings"],
    "业务建议": ["业务建议", "业务建议与行动", "建议", "Recommendations"],
    "进阶分析推荐": ["进阶分析推荐", "进阶分析", "进一步分析", "Further Analysis"],
    "跳过的高级方法及原因": ["跳过的高级方法", "未执行的方法", "Skipped Methods"],
    "分析边界与风险警告": ["分析边界", "风险警告", "局限性", "Limitations"],
    "初学者教学总结": ["初学者教学总结", "教学总结", "Beginner Summary", "为什么这样分析"],
    "质量检查与改进建议": ["质量检查", "质量评估", "改进建议", "Quality Check"],
}


# ── 8 要素教学检测关键词 ──────────────────────────────

_TEACHING_ELEMENTS: dict[str, list[str]] = {
    "分析结果": ["结果", "数据表明", "数据显示", "发现", "数据显示出"],
    "方法解释": ["方法", "为什么使用", "为什么选择", "用[了]?.*分析"],
    "指标解释": ["指标", "含义是", "表示", "反映"],
    "公式说明": ["公式", "计算方式", "计算为", "/", "÷", "×"],
    "字段来源": ["字段", "列", "数据列", "来源"],
    "业务含义": ["业务", "说明", "意义", "意味着", "意味着", "代表"],
    "风险边界": ["风险", "局限", "不能", "无法证明", "注意", "谨慎", "不代表"],
    "初学者复用": ["下次", "可以用", "可以用于", "复用", "类似数据", "下次遇到"],
}

# Tier 2 方法关键词
_TIER2_METHODS = [
    "correlation", "相关", "rfm", "t-test", "t检验", "chi-square", "卡方",
    "linear regression", "线性回归", "logistic regression", "逻辑回归",
    "clustering", "聚类", "cohort", "同期群", "seasonality", "季节性",
    "time series", "时序预测", "forecasting", "预测", "sentiment", "情感",
    "causal", "因果",
]

# 思维模型要求
_THINKING_MODEL_REQUIREMENTS: dict[str, int] = {
    "beginner_summary": 2,
    "standard_report": 3,
    "audit_report": 5,
}

# 5 种思维模型的自检问题关键词
_THINKING_MODEL_KEYWORDS = [
    # 1. 分解
    ["分解", "结构拆解", "拆解", "组成部分", "按.*分"],
    # 2. 分层差异
    ["分层", "分组差异", "subgroup", "子群", "倍数", "比率"],
    # 3. 代理推断
    ["代理", "间接推断", "推断", "proxy", "替代"],
    # 4. 约束 vs 偏好
    ["约束", "偏好", "不能选择", "主动选择", "限制"],
    # 5. 杠杆点
    ["杠杆", "高份额", "改进空间", "leverage", "重点"],
]

# Excel 工作表要求
_EXCEL_SHEETS: dict[str, list[str]] = {
    "beginner_summary": [
        "数据概览", "整体对比", "分析看板", "分析报告",
    ],
    "standard_report": [
        "数据概览", "整体对比", "维度分析", "交叉分析", "分析看板", "分析报告",
    ],
    "audit_report": [
        "数据概览", "整体对比", "维度分析", "交叉分析",
        "思维模型", "自检问答", "分析看板", "分析报告",
    ],
}


class ValidationResult:
    """报告结构校验结果。"""
    def __init__(self, passed: bool, missing: list[str], found: list[str]):
        self.passed = passed
        self.missing = missing
        self.found = found

    def __repr__(self) -> str:
        if self.passed:
            return "✅ 报告结构完整"
        return f"⚠️ 缺少以下章节: {', '.join(self.missing)}"


class SkillEngine:
    """SKILL.md 规则引擎。"""

    def __init__(self, mode: str = "beginner_summary"):
        self.config = SkillConfig(mode=mode)

    # ── 模块控制 ───────────────────────────────────────

    def get_module_defaults(self, mode: str | None = None) -> dict[str, bool]:
        """获取指定 mode 的默认模块开关表。"""
        return dict(_MODULE_DEFAULTS.get(mode or self.config.mode, _MODULE_DEFAULTS["beginner_summary"]))

    def parse_toggles(self, text: str) -> dict[str, bool]:
        """解析用户覆写开关语法，如 '+metric_formulas', '-audit_detail'。

        返回 {模块名: True/False} 字典。
        """
        toggles: dict[str, bool] = {}
        for token in re.findall(r'[+-]\w+', text):
            enable = token[0] == "+"
            name = token[1:]
            if name in MODULE_NAMES:
                toggles[name] = enable
        return toggles

    def apply_toggles(self, toggle_text: str):
        """解析并应用用户覆写开关。"""
        toggles = self.parse_toggles(toggle_text)
        self.config.toggles.update(toggles)

    # ── 报告结构 ───────────────────────────────────────

    def get_required_sections(self, mode: str | None = None) -> list[str]:
        """获取当前 mode 需要的报告章节标题列表。"""
        return list(_REQUIRED_SECTIONS.get(mode or self.config.mode, _REQUIRED_SECTIONS["beginner_summary"]))

    def validate_report_structure(self, report_text: str, mode: str | None = None) -> ValidationResult:
        """检查报告文本是否包含所有必需的章节。"""
        required = self.get_required_sections(mode)
        missing: list[str] = []
        found: list[str] = []

        for section in required:
            aliases = _SECTION_ALIASES.get(section, [section])
            if any(a in report_text for a in aliases):
                found.append(section)
            else:
                missing.append(section)

        return ValidationResult(passed=len(missing) == 0, missing=missing, found=found)

    # ── 教学 8 要素 ────────────────────────────────────

    def check_teaching_elements(self, text: str, required_count: int = 8) -> list[str]:
        """检查文本中包含了哪些教学要素，返回缺失项列表。"""
        missing: list[str] = []
        for element, keywords in _TEACHING_ELEMENTS.items():
            found = any(re.search(kw, text) for kw in keywords)
            if not found:
                missing.append(element)
        return missing

    # ── 思维模型 ───────────────────────────────────────

    def get_thinking_model_requirements(self, mode: str | None = None) -> int:
        """获取当前 mode 需要的最少思维模型数量。"""
        return _THINKING_MODEL_REQUIREMENTS.get(mode or self.config.mode, 2)

    def count_thinking_models(self, text: str) -> int:
        """统计文本中出现的思维模型种类数量。"""
        count = 0
        for group in _THINKING_MODEL_KEYWORDS:
            if any(re.search(kw, text) for kw in group):
                count += 1
        return count

    # ── Excel 交付物 ───────────────────────────────────

    def get_required_excel_sheets(self, mode: str | None = None) -> list[str]:
        """获取当前 mode 需要的 Excel 工作表列表。"""
        return list(_EXCEL_SHEETS.get(mode or self.config.mode, _EXCEL_SHEETS["beginner_summary"]))

    # ── Tier 2 方法检测 ────────────────────────────────

    def is_tier2_method(self, tool_name: str, args: dict) -> bool:
        """检测工具调用是否试图执行 Tier 2 方法。"""
        command = ""
        if tool_name == "run_shell":
            command = str(args.get("command", ""))
        elif tool_name == "write_file":
            command = str(args.get("content", ""))
        return any(m in command.lower() for m in _TIER2_METHODS)

    # ── Phase 指令生成 ────────────────────────────────

    def get_phase_instruction(self, phase: str, tier: int | None = None) -> str:
        """返回 mode 适配的阶段指令文字。"""
        if phase == "explore":
            return self._explore_instruction()
        if phase == "analyze":
            return self._analyze_instruction(tier)
        if phase == "report":
            return self._report_instruction()
        return ""

    def _explore_instruction(self) -> str:
        return (
            "\n\n## 📋 探索阶段指令\n"
            "先调用 csv_summary 了解数据规模、列名、行列数。\n"
            "观察外键字段（product_id, customer_id, order_id）来确定是否需要关联其他 CSV。\n"
            "探索结束后立即进入分析阶段。"
        )

    def _analyze_instruction(self, tier: int | None = None) -> str:
        instructions = (
            "\n\n## 📋 分析阶段指令（优先级高于通用规则）\n"
            "你已完成数据探索，现在必须执行深度分析。\n\n"
        )

        # Tier 提示
        if self.config.mode == "beginner_summary":
            instructions += (
                "### 分析方法分层\n"
                "• Tier 1（必须执行）：描述性统计、分组聚合、排名分析、数据质量检查\n"
                "• Tier 2（仅推荐，不自动执行）：相关分析、RFM、回归、聚类等——仅建议但不执行\n\n"
            )
        elif self.config.mode == "audit_report":
            instructions += (
                "### 分析方法分层\n"
                "• Tier 1（必须执行）：描述性统计、分组聚合、排名分析、数据质量检查\n"
                "• Tier 2（可自动执行）：相关分析、RFM、回归、聚类等——audit_report 模式允许执行\n"
                "• 执行 Tier 2 方法时需包含：方法说明、字段要求、业务问题、风险警告\n\n"
            )
        else:
            instructions += (
                "### 分析方法分层\n"
                "• Tier 1（必须执行）：描述性统计、分组聚合、排名分析、数据质量检查\n"
                "• Tier 2（可执行但需说明）：相关分析、RFM、回归、聚类等——执行时需附带方法和风险说明\n\n"
            )

        instructions += (
            "下一步操作顺序（严格遵守）：\n"
            "1. 用 write_file 写 analysis_01.py，涵盖 5+ 个分析维度\n"
            "2. 用 run_shell 执行 analysis_01.py\n"
            "3. 分析结果，决定是否需要写 analysis_02.py 做补充\n"
            "4. 用 <final> 输出完整报告\n\n"
            "禁止：重新读 CSV 文件、反复 csv_summary、list_files、search\n"
            "每个发现必须包含：结果 → 方法 → 指标 → 业务解释 → 风险边界"
        )
        return instructions

    def _report_instruction(self) -> str:
        required = self.get_required_sections()
        sections_str = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(required))
        return (
            "\n\n## 🚨 报告阶段指令\n"
            "你必须用 <final> 输出包含以下所有章节的完整报告：\n\n"
            f"{sections_str}\n\n"
            "每个核心发现需包含 8 要素：分析结果 → 方法解释 → 指标解释 → 公式说明 "
            "→ 字段来源 → 业务含义 → 风险边界 → 初学者复用\n\n"
            f"{'包含至少 2 种思维模型的自检问答。' if self.config.mode == 'beginner_summary' else '包含全部 5 种思维模型的自检问答与数据证据。' if self.config.mode == 'audit_report' else '包含至少 3 种思维模型的自检问答。'}"
        )

    def get_report_quality_reminder(self) -> str:
        """返回质量检查清单的文本提示（SKILL §13 精简版）。"""
        return (
            "\n\n### 质量检查清单（最终输出前请逐一确认）\n"
            "- 是否包含 clear business question？\n"
            "- 是否包含 dataset field explanation？\n"
            "- 是否包含 dataset risk check？\n"
            "- 是否包含 analysis method explanation（含 tier justification）？\n"
            "- 是否包含 metric formulas and current calculations？\n"
            "- 是否包含 business interpretation？\n"
            "- 是否包含 actionable recommendations？\n"
            "- 是否包含 beginner learning notes？\n"
            "- 是否包含 thinking model teaching？\n"
            "- 是否包含 limitations？\n"
            "- 是否包含 possible further analysis directions？\n"
            "- 是否包含 skipped advanced methods and reasons？\n"
            "- 每个核心发现是否包含 8 要素教学结构？"
        )


# ── 便捷工厂 ──────────────────────────────────────────

def create_skill_engine(mode: str = "beginner_summary", toggle_text: str = "") -> SkillEngine:
    """创建 SkillEngine 实例，可选的用户覆写开关。"""
    engine = SkillEngine(mode=mode)
    if toggle_text:
        engine.apply_toggles(toggle_text)
    return engine
