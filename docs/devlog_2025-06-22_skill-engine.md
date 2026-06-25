# 开发日志：Skill Engine 架构改造

日期: 2025-06-22
作者: Claude Code (assisted)

## 背景

DataHelp Agent 加载了 `business_analysis` SKILL.md（770 行），但只是全文塞进 system prompt，模型"看到"了要求但没有代码层面的执行保障。这是一个架构层面的缺失：

- SKILL.md 定义的 Step 0/输出模式/Tier 分层/8 要素教学/13 节报告结构等全部靠模型自觉
- `--mode` 三个输出模式声明了但代码行为完全一样
- 交付物 Excel 只有 3 张通用表，不符合 SKILL 要求的多维分析结构

## 核心设计

新增 `skill_engine.py` —— 在 SKILL.md（纯文本 prompt）和 Agent Runtime（工具循环）之间加一层结构化规则引擎。

```
SKILL.md ─→ skill_loader.py ─→ skill_engine.py ◄── runtime.py / cli.py / tools_data.py
                                        │
                                        ├── SkillConfig (mode + 7 模块开关)
                                        ├── validate_report_structure()
                                        ├── check_teaching_elements()
                                        ├── is_tier2_method()
                                        ├── get_phase_instruction()
                                        └── get_required_excel_sheets()
```

## 改动清单

### 新增文件

| 文件 | 说明 |
|------|------|
| `datahelp/skill_engine.py` | ~470 行规则引擎核心。SkillConfig dataclass、SkillEngine class（报告结构校验、教学 8 要素检测、Tier 2 方法识别、思维模型计数、阶段指令生成、Excel 工作表要求）|

### 修改文件

| 文件 | 改动 |
|------|------|
| `datahelp/runtime.py` | 4 处：① `__init__` 集成 SkillEngine；② `_build_prefix()` 用结构化指令替换全文 SKILL.md（token 省 84%，33047→5436 chars）；③ `_check_gates()` 增加 Tier 2 门控；④ `<final>` 后执行报告/教学校验（1 轮修正）；⑤ gate block 分支保存 `last_raw_text` 使 auto-recover 能触发；⑥ `run_tool()` 自动注入 `analysis_text` + `mode` 到 generate 工具 |
| `datahelp/cli.py` | 增加 Step 0 交互确认（_skill_step0_confirm），one-shot 模式下先问数据集/模式/输出文件夹 |
| `datahelp/tools.py` | generate 工具 schema 增加 `analysis_text` 和 `mode` 可选参数 |
| `datahelp/tools_data.py` | Excel 从 4 张表扩展到最多 8 张表（自动检测分类列生成维度分析/交叉分析/思维模型/自检问答）；PDF 增加执行摘要/思维模型/自检问答/行动建议 |
| `datahelp/*.py` (7 个文件) | 加 `from __future__ import annotations` 兼容 Python 3.9 |

## 关键数字

- **Token 节省**: 33047 chars → 5436 chars（full SKILL.md dump → 结构化指令，省 84%）
- **Excel 表**: 4 张 → 最多 8 张（按 mode 切换）
- **报告章节**: 13 节全量校验（audit_report），自动检测缺失
- **教学要素**: 8 要素检测（结果/方法/指标/公式/字段/业务/风险/复用）
- **分析方法**: Tier 1/2/3 分层门控，阻止非 audit 模式自动执行高级方法
- **思维模型**: beginner 最少 2 种，audit 全部 5 种

## 遗留问题

- Tier 2 门控依赖 `is_tier2_method()` 的关键词匹配，可能漏判或误判
- 增强型 Excel 的"维度分析"和"交叉分析"工作表仅在数据集中存在分类列时生成（纯数值数据集跳过）
- 报告结构校验采用章节标题关键词匹配，模型使用近义词时可能误判
- `--mode` 默认为 None（不走 skill 路径，保持向后兼容），指定后才激活 Skill Engine
