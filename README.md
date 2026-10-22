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

# 5. 开始分析
python -m datahelp --provider deepseek "分析数据集 sales.csv"
```

## 使用方式

### One-shot 模式（单次分析）

```bash
python -m datahelp --provider deepseek "列出项目根目录的文件"
```

### REPL 交互模式

```bash
python -m datahelp
# 进入交互界面后直接输入问题
```

### 数据分析

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

## 教学 Skill 模式

在 `--mode beginner_summary` 模式下，数据分析报告按 12 节结构输出，缺一不可：

1. **数据概览** — 数据集规模、字段说明、外键识别
2. **字段识别与业务含义** — 每个字段的语义、字段-业务问题映射
3. **数据质量检查** — 缺失值、重复值、异常值及教学说明
4. **基础指标分析** — 均值/中位数/标准差/分位数
5. **分组与排名分析** — Top/Bottom 排名及头部集中度
6. **趋势分析** — 按月聚合、环比变化
7. **核心发现** — 每个发现含结果/方法/指标/公式/字段/业务/风险/复用 8 要素
8. **业务建议** — 数据驱动的具体行动建议
9. **进阶分析推荐** — 为什么适合、需要什么字段、能回答什么
10. **跳过的高级方法及原因** — 透明说明不执行的方法
11. **分析边界与风险警告** — 局限性和风险提示
12. **初学者教学总结** — 思维模型回顾、可复用分析思路

## 项目结构

```
datahelp/
├── datahelp/                    # 核心 Python 包
│   ├── app.py                   # 统一编排：profile → plan → compute → narrate → deliver
│   ├── profile.py               # 数据集画像（只描述，不下结论）
│   ├── plan.py                  # 分析计划与确定性计算
│   ├── narrative.py             # 报告渲染与章节校验
│   ├── cli.py                   # 命令行入口
│   ├── runtime.py               # 旧的 Agent 主循环（--agent 时使用）
│   ├── models.py                # LLM 模型客户端
│   ├── tools.py                 # 工具注册表
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

## 运行原理（V3）

一次分析固定走五步，每一步的产物都落盘，出问题能倒查：

```
CSV
 ├─① profile   只描述不判断：列类型、取值、缺失、疑似主键/日期/金额
 │             → profile.json
 ├─② plan      模型基于画像提出"要回答什么问题、算什么、为什么"
 │             没有模型时用默认计划兜底
 │             → plan.json
 ├─③ compute   把计划编译成固定模板的 pandas 计算
 │             数值只可能从这里产生，每条都带公式和来源列
 │             → evidence.json
 ├─④ narrate   模型只解释证据，不许自己算；结构按固定章节校验
 │             没有模型时直接由证据渲染
 │             → report.md
 └─⑤ deliver   从证据和报告渲染 Excel / HTML / PDF
```

核心约定：**模型决定算什么，代码负责算出来，模型只负责解释。**

### 和之前两版的区别

| | V1（agent 循环） | V2（确定性引擎） | V3 |
|---|---|---|---|
| 分析什么 | 模型自由决定 | 写死 7 个计划 | 模型提计划，可审计 |
| 数值来源 | 模型写的脚本 | pandas | pandas，公式随计划落档 |
| 报告结构 | prompt 要求 | 标题正则校验 | 章节校验，单一来源 |
| 入口 | one-shot / REPL | 仅 watch | 全部走同一条 |

`--agent` 参数仍可回退到 V1 的自由循环，用于探索性追问；`python -m datahelp` 的主路径已经切到 V3。

### 为什么这样设计

V1 让模型自己写脚本算数，灵活但数字不可复现；V2 把"算什么"写死，可信但只会分析事先想到的那种表。
V3 把两件事分开：**分析什么**交给模型（它擅长，且计划可审计），**算出什么**交给代码（可复现，可追溯）。

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
