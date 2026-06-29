# DataHelp

面向商科生的数据分析学习 Agent — 不只给结果，还教你为什么这样分析。

DataHelp 是一个轻量级本地 AI Agent，接收 CSV/Excel 数据文件，自动进行分析并输出**带有教学注释的分析报告**。初学者既能拿到分析结果，也能理解每个分析方法背后的逻辑。

## 快速开始

```bash
# 1. 克隆项目
git clone https://github.com/Ysx12138/Busiiness-learning-analysing-agent---Data-help.git
cd datahelp

# 2. 创建虚拟环境（推荐）
python -m venv .venv
source .venv/bin/activate

# 3. 安装
pip install -e .

# 4. 配置 API key
cp .env.example .env
# 编辑 .env，填入你的 API key

# 5. 一键分析
python -m datahelp analyze sales.csv -o outputs --provider deepseek
```

## 使用方式

### 一键分析模式（推荐）

```bash
python -m datahelp analyze sales.csv -o outputs --provider deepseek
```

这个命令会自动创建任务输出目录，并生成：

- `RESULT_SUMMARY.md`（推荐先看）
- `result_manifest.json`
- `analysis_report.md`
- `analysis_evidence.json` / `analysis_evidence.md`
- Excel / HTML / PDF 交付物

### One-shot Agent 模式（高级）

```bash
python -m datahelp --provider deepseek "列出项目根目录的文件"
```

### REPL 交互模式

```bash
python -m datahelp
# 进入交互界面后直接输入问题
```

### 自定义工作目录分析

```bash
# 指定数据所在目录作为工作目录
python -m datahelp --provider deepseek --cwd ./data "分析销售数据"
```

### 文件监听模式（自动分析新数据）

```bash
python -m datahelp setup       # 首次配置输入/输出文件夹
python -m datahelp watch       # 监听输入文件夹，自动分析新文件
```

## 支持的模型提供商

| 提供商 | `--provider` 参数 | 需要配置 |
|--------|-------------------|----------|
| DeepSeek | `deepseek` | `DATAHELP_DEEPSEEK_API_KEY` |
| Anthropic Claude | `anthropic` | `DATAHELP_ANTHROPIC_API_KEY` |
| OpenAI | `openai` | `DATAHELP_OPENAI_API_KEY` |
| Ollama（本地） | `ollama` | 无（需本地运行 Ollama） |
| Mock（测试用） | `mock` | 无需配置 |

## 命令行参数

```
--provider, -p     模型提供商（mock / deepseek / openai / anthropic / ollama）
--model, -m        模型名称（覆盖默认值）
--cwd              工作目录
--mode             输出模式（beginner_summary / standard_report / audit_report）
--output-dir, -o   交付产物输出文件夹（Excel / HTML / PDF）
--max-steps        最大工具调用步数（默认 40）
--approval         高风险工具审批策略（auto / ask / never）
--temperature      模型采样温度
--max-new-tokens   每次调用的最大输出 token 数
```

更低门槛的一键入口：

```
python -m datahelp analyze <数据文件> -o <输出文件夹> --provider deepseek
```

## 教学 Skill 模式

在 `--mode beginner_summary` 模式下，数据分析报告按 9 个核心章节输出：

1. **数据概览** — 数据集规模、字段说明、外键识别
2. **数据质量检查** — 缺失值、重复值、异常值及教学说明
3. **基础指标分析** — 均值/中位数/标准差/分位数
4. **分组与排名分析** — Top/Bottom 排名及头部集中度
5. **趋势分析** — 按月聚合、环比变化；无法执行时说明原因
6. **核心发现** — 每个发现含证据、方法、业务含义、风险和复用说明
7. **业务建议** — 数据驱动的具体行动建议
8. **分析边界与风险警告** — 局限性、跳过方法及风险提示
9. **初学者教学总结** — 思维模型回顾、可复用分析思路

## 项目结构

```
datahelp/
├── datahelp/                    # 核心 Python 包
│   ├── cli.py                   # 命令行入口
│   ├── runtime.py               # Agent 主循环
│   ├── models.py                # LLM 模型客户端
│   ├── tools.py                 # 工具注册表
│   ├── agent_tools.py           # Agent 可调用的高层数据分析工具
│   ├── tools_data.py            # 数据分析工具
│   ├── context_manager.py       # Prompt 组装与预算控制
│   ├── memory.py                # 分层记忆系统
│   ├── task_state.py            # 运行状态跟踪
│   ├── run_store.py             # 运行持久化
│   ├── evaluator.py             # 评测框架
│   ├── metrics.py               # 评测指标聚合
│   ├── skill_loader.py          # Skill 加载
│   ├── config.py                # 环境变量管理
│   ├── config_manager.py        # 交互式配置
│   ├── pipeline.py              # 数据分析流水线
│   ├── watcher.py               # 文件监听器
│   └── workspace.py             # 工作区快照
├── skills/
│   └── business_analysis/       # 教学分析 Skill
│       └── SKILL.md
├── tests/
│   └── test_set.jsonl
├── pyproject.toml
├── .env.example
└── LICENSE
```

## 运行原理

DataHelp 现在采用 **Agent-first + 确定性证据工具** 的架构。Agent 负责理解目标、决定下一步；高层数据工具负责稳定计算，避免模型编造数字：

```
用户输入 → inspect_dataset（了解数据规模、字段类型、样例值）
         → suggest_analysis_plan（根据字段和目标制定小步计划）
         → run_analysis_evidence（生成可追溯分析证据）
         → validate_evidence（校验证据是否足以出报告）
         → compose_report_from_evidence / <final>（生成 9 节教学报告）
         → generate_excel / generate_html / generate_pdf（交付物含分析结论）
```

### 为什么这样设计？

传统 Coding Agent 的"一步调一个工具"适合线性调试，不适合面向初学者的稳定数据分析。DataHelp 不再把核心成功路径完全押在模型自由写脚本上，而是把确定性分析能力封装成 Agent 可主动调用的高层工具。这样保留 Agent 的决策感，同时让数值证据更稳定、更可追溯。

### 关键参数

- 最大工具调用步数：40（应对多轮迭代分析）
- 工具输出上限：12,000 字符（减少复杂分析脚本的输出截断）
- 内置 SKILL.md 全文注入：28,790 字符教学规则 100% 传递给模型
- ContextManager 预算：50,000 字符（前缀 32K / 记忆 5K / 历史 12K）

## 架构演进

DataHelp 最初沿用 Coding Agent 的 tool-calling loop 架构，存在 6 项根本性问题：SKILL.md 手写提取丢失 97% 规则、工具粒度太细、步数限制、输出截断、预算不足、交付物脱节。详细的排查过程记录在 [AUDIT_V3.md](AUDIT_V3.md)。

核心改造：

| 问题 | 改前 | 改后 |
|------|------|------|
| SKILL.md 传递 | 800 字符手写提取（存活率 3%） | 28,790 字符全文注入（存活率 100%） |
| 分析方式 | 一步调一个工具，看一个维度 | Agent 选择高层分析工具，生成结构化证据 |
| 工作流 | field_types → missing_values → numeric_stats → ... | inspect → plan → evidence → validate → report |
| 输出结构 | 模型自由发挥 | 9 个核心章节 + 可选扩展模块 |
| 交付物 | 纯模板，不含分析结论 | Excel/HTML/PDF 含 Agent 分析报告 |
| ContextManager | 总预算 12K，前缀 3.6K（被截断） | 总预算 50K，前缀 32K（零截断） |

## 开发

```bash
# 安装可编辑模式
pip install -e .

# 运行评测
python -m datahelp --eval tests/test_set.jsonl

# Mock 模式测试（不消耗 API 额度）
python -m datahelp --provider mock "测试任务"
```

## License

MIT
