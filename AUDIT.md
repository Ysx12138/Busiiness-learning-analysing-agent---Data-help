# DataHelp 架构审计报告 V2

> 审计时间：2026-06-08
> 审计对象：DataHelp Agent 改造前后对比
> 测试数据集：retail_test.csv（12 行，6 列）
> 测试模型：deepseek-v4-flash

---

## 一、改造总览

| 指标 | 改造前（审计 V1） | 改造后（V2） | 变化 |
|------|------------------|-------------|------|
| SYSTEM_PROMPT 长度 | 176 字符 | 796 字符 | +353% |
| SKILL.md 注入方式 | 28,682 字符全文 dump | 提取 ~800 字符核心规则 | -97% |
| 最终 prefix 长度 | 30,276 字符 | ~2,700 字符 | -91% |
| ContextManager 利用率 | 60% 被截断 | 100% 有效利用 | ✅ |
| 工具调用步数 | 1 步 | 12 步 | +1,100% |
| 数据真实性 | 编造模拟数据 | 100% 真实数据 | ✅ |
| 教学输出 | 无（模型自由发挥） | 6 要素教学（每个发现） | ✅ |
| 交付物 | 无 | Excel + HTML + PDF | ✅ |

---

## 二、System Prompt 对比

### 改造前（176 字符）

```
你是 DataHelp，一个面向代码仓库的轻量本地 coding agent。
任务完成后，用 <final> 输出最终答案。
规则：
- 每次只调用一个工具
- 不要重复调用完全相同的工具
```

问题：
- 定位是"代码仓库 agent"，不是"数据分析教学 agent"
- `<final>` 约束鼓励模型"尽快结束"——调用一个 csv_summary 后就输出 `<final>`
- 没有定义分析工作流，模型不知道先做什么后做什么
- 没有反数据编造规则

### 改造后（796 字符）

核心章节：
1. **核心工作流程** — 5 步定义：数据探索 → 数据质量 → 深入分析 → 生成报告 → 交付物
2. **数据真实性规则** — 4 条强制约束：csv_summary 先行、run_shell 用 pd.read_csv、数值可追溯、报错不编造
3. **工具使用规则** — 每次只调用一个工具、至少 3-5 个不同工具再出报告
4. **格式规范** — `<tool>` 和 `<final>` 格式要求

关键差异：
- `<final>` 从"任务完成后就输出"改为"完成完整分析和教学输出后才用"
- 新增"至少调用 3-5 个不同的分析工具后再输出最终报告"
- 新增"数据真实性规则"强制反编造

---

## 三、Skill 集成方式对比

### 改造前

```python
self.skill_instructions = load_skill(skill_name)
# → 28,682 字符文本 dump
# → 在 _build_prefix 中追加到 prefix
# → ContextManager 截断至 ~11,876 字符
# → 60% 教学规则丢失（Section 8-16 全部被截）
```

### 改造后

```python
parts = [SYSTEM_PROMPT]  # 796 字符（核心行为定义）
if self.skill_instructions:
    teaching_rules = """提取的 6 要素规则 + 基础分析清单
    + 进阶分析说明 + 初学者摘要模板 + 严禁行为"""
    parts.append(teaching_rules)
parts.append(describe_tools(tools))
parts.append(workspace.text())
# → ~2,700 字符总 prefix
# → 100% 在预算内，无截断
```

**Skill 规则存活度对比：**

| 规则类型 | 改造前 | 改造后 |
|---------|--------|--------|
| 6 要素教学（分析结果/方法/指标/字段/业务/风险） | ⚠️ 部分存活 | ✅ 完整 |
| 基础分析清单 | ❌ 被截断 | ✅ 完整 |
| 进阶分析规则 | ❌ 被截断 | ✅ 完整 |
| beginner_summary 模板 | ❌ 被截断 | ✅ 完整 |
| 严禁行为 | ❌ 被截断 | ✅ 完整 |
| PPT 生成（无关内容） | ⚠️ 占用空间 | ❌ 已移除 |

---

## 四、运行时加固

### 4.1 数据真实性门控

```python
# run_tool() 中的新增检查
if name in ("run_shell", "generate_excel", "generate_html", "generate_pdf") \
        and not self._explored_csvs:
    return f"错误: 必须先调用 csv_summary 获取实际数据..."

# csv_summary 成功后注册
if name == "csv_summary" and "错误" not in result:
    self._explored_csvs.add(args.get("path", ""))
```

效果：模型必须先调用 `csv_summary` 并通过验证，才能使用 `run_shell` 进行深入分析。

### 4.2 解析器修复

```python
# 修复前：纯文本 > 10 字符 → 视为 "final"（导致提前终止）
if len(text) > 10:
    return "final", text[:2000]

# 修复后：纯文本 → 返回 retry，强制使用 <tool> 或 <final> 格式
if len(text) > 10:
    if '"name"' in text and '"args"' in text:
        return "retry", {"message": "...未被 <tool> 标签包裹..."}
    return "retry", {"message": "请使用 <tool> 或 <final> 格式。"}
```

效果：模型无法通过输出纯文本来"蒙混过关"终止任务，必须使用格式标签。

### 4.3 Checkpoint 持久化

`_explored_csvs` 现在随 checkpoint 保存和恢复，断点续跑时门控状态不丢失。

---

## 五、测试结果对比

### 数值准确性验证（最新 run）

| 数据点 | Agent 报告值 | CSV 实际值 | 匹配 |
|--------|-------------|-----------|------|
| Electronics 总收入 | 65,000 | 65,000 | ✅ |
| Electronics 总成本 | 46,700 | 46,700 | ✅ |
| Clothing 总收入 | 38,800 | 38,800 | ✅ |
| Food 总收入 | 22,200 | 22,200 | ✅ |
| 总收入 | 126,000 | 126,000 | ✅ |
| 1 月收入 | 50,700 | 50,700 | ✅ |
| 2 月收入 | 46,800 | 46,800 | ✅ |
| 3 月收入 | 28,500 | 28,500 | ✅ |
| South 利润率 | 44.4% | 44.4% | ✅ |
| Food 利润率 | 59.5% | 59.5% | ✅ |

**零编造、零偏差。**

### 执行流程对比

| 步骤 | 改造前（1 步） | 改造后（12 步） |
|------|---------------|----------------|
| csv_summary | ✅ | ✅ |
| field_types | ❌ 直接跳到 final | ✅ |
| missing_values | ❌ | ✅ |
| numeric_stats | ❌ | ✅ |
| read_file | ❌ | ✅ |
| run_shell（分组聚合） | ❌ | ✅（写入 analysis.py） |
| run_shell（趋势分析） | ❌ | ✅ |
| 教学报告 | ❌ 无 | ✅ beginner_summary |
| generate_excel | ❌ | ✅ |
| generate_html | ❌ | ✅ |
| generate_pdf | ❌ | ✅ |

---

## 六、当前剩余问题

| 问题 | 严重程度 | 说明 |
|------|---------|------|
| deepseek-v4-flash 格式不稳定性 | ⚠️ 中 | 有时跳过 `<tool>` 输出纯文本，被 retry 纠正后可恢复 |
| ContextManager 预算限制 | ⚠️ 低 | 当前 prefix ~2,700，远低于 12K 上限，但如果新增大量工具描述可能超限 |
| 交付物生成独立于 agent | ℹ️ 信息 | cli.py 的 `run_one_shot` 在 agent 完成后扫描 CSV 目录生成交付物，不是 agent 自行调用 |
| 没有集成测试 | ℹ️ 信息 | 功能测试依赖手动运行，没有自动化测试套件 |

---

## 七、改进建议

### 高优先级
1. **支持 Claude 模型** — deepseek-v4-flash 在工具调用格式稳定性上弱于 Claude，切换模型后效果会更稳定
2. **run_shell 结果自动校验** — 执行后自动交叉验证关键数值（均值、合计等）与工具返回结果是否一致

### 中优先级
3. **增加集成测试** — 对 `parse_model_output`、`run_tool` 门控、`_explored_csvs` 追踪等核心逻辑编写单元测试
4. **改进 retry 机制** — 连续 3 次 retry 后自动注入更明确的格式示例到历史记录

### 低优先级
5. **交付物质量提升** — 当前 Excel/HTML/PDF 是纯模板生成，未使用 agent 的实际分析结论
6. **Prefix 动态裁剪** — 当前 tools 列表已经固定，但如果未来注册更多工具，需要动态管理 prefix 以适应不同模型的 Context 限制
