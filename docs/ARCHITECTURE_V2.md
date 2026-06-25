# DataHelp V2 架构文档

> 本文档记录 DataHelp V2 确定性分析架构的设计决策、组件边界和降级策略。
> 最后更新: 2026-06-25

---

## 一、V1 的主要风险

V1（当前生产版本，agent 自由分析路径）存在以下三类系统性风险：

| 风险 | 描述 | 后果 |
|------|------|------|
| **模型自由计算** | Agent 调用 LLM 时，模型可自由决定分析哪些指标、使用什么公式，无法保证每次运行结果一致且可审计。 | 分析结论不可复现，审计方无法验证数值来源。 |
| **终端证据截断** | Agent 的运行日志（task_state / history）包含大量中间步骤，但缺少结构化的、可独立分发的证据文件。交付物（Excel/PDF）中的数值直接从 agent 对话文本中"摘取"，易截断或丢失。 | 交付物中的关键指标缺少到原始数据行的追溯路径。 |
| **交付物与事实脱节** | Excel/HTML/PDF 交付物中的统计值（如均值、分组排名）来自 agent 生成的文本，而非对原始数据的确定性计算。模型可能编造或错误解读数值。 | 业务决策依据的数据不可信，违反审计合规要求。 |

---

## 二、V2 主链路图

```
┌─────────────┐     ┌─────────────────────────────┐     ┌───────────────────────┐
│             │     │                             │     │                       │
│  输入 CSV   │────▶│  确定性分析引擎              │────▶│  证据 JSON + MD       │
│  (或 Excel) │     │  DeterministicAnalysisEngine │     │  analysis_evidence.*  │
│             │     │  (pandas 计算，100% 确定性)   │     │                       │
└─────────────┘     └─────────────────────────────┘     └───────────┬───────────┘
                                                                    │
                                                                    ▼
                                        ┌─────────────────────────────────────────┐
                                        │  报告编排器 ReportOrchestrator         │
                                        │                                        │
                                        │  ┌─────────────────────────────────┐  │
                                        │  │  模型仅解释证据，不可计算新事实    │  │
                                        │  │  → 构建 system + user prompt     │  │
                                        │  │  → LLM 生成结构化中文报告        │  │
                                        │  │  → 验证 9 章节 + 5 子要素       │  │
                                        │  │  → 不合格则修订（最多 1 次）     │  │
                                        │  └─────────────────────────────────┘  │
                                        │                                        │
                                        │  ┌─────────────────────────────────┐  │
                                        │  │  降级路径（免 LLM）              │  │
                                        │  │  → build_evidence_report()       │  │
                                        │  │  → 基于证据直接生成 9 章节框架   │  │
                                        │  └─────────────────────────────────┘  │
                                        └───────────┬───────────────────────────┘
                                                    │
                                                    ▼
                                        ┌─────────────────────┐
                                        │  交付物生成          │
                                        │                     │
                                        │  Excel (原始数据+   │
                                        │   统计+思维模型)    │
                                        │  HTML (结构化报告)   │
                                        │  PDF (CJK 中文报告)  │
                                        └─────────────────────┘
```

### 数据流概要

1. **输入预处理**: Excel 自动转为 CSV（pandas → 纯 Python 兜底）
2. **确定性计算**: `DeterministicAnalysisEngine.run()` 读取 CSV，产出 `AnalysisResult`（含 7 类分析 plan）
3. **证据序列化**: 写入 `analysis_evidence.json` + `analysis_evidence.md`
4. **报告编排**: `ReportOrchestrator.run()` 将证据转为 LLM prompt → 结构化中文报告
5. **交付物生成**: 基于证据 + 报告生成 Excel/HTML/PDF

---

## 三、各层责任边界

### 3.1 `analysis_contract.py` — 数据合约层

```
AnalysisEvidence    一条证据：metric_name, value, formula, source_columns,
                    calculation_method, caveat

AnalysisPlan        一组相关证据：plan_id, description, evidence[]

AnalysisResult      一次运行的顶层容器：input_file, row_count, column_count,
                    column_names, plans[], generated_at
```

**职责**:
- 纯 dataclass，零业务逻辑
- 所有字段均可 JSON 序列化/反序列化（`to_dict() / from_dict()`）
- `make_evidence()` 便利构造器

### 3.2 `analysis_engine.py` — 确定性分析引擎层

```
DeterministicAnalysisEngine.run(csv_path, output_dir) → AnalysisResult
```

**职责**:
- 使用 pandas 执行**确定性**计算，相同输入 → 相同输出
- 产出 7 个 AnalysisPlan:
  - `profile` — 行数、列数、列类型
  - `missing` — 缺失值统计（总数、逐列、缺失率）
  - `duplicates` — 重复行统计
  - `numeric_stats` — 数值列描述性统计（count/mean/std/min/q25/median/q75/max）
  - `categorical` — 分类列 value_counts（前 N 个）
  - `date_trend` — 日期列按月聚合
  - `derived_metrics` — 派生指标（profit = revenue - cost）

**边界**:
- ⚠️ **模型只解释证据，不可计算新事实** — 引擎层不调用任何 LLM
- 所有异常均被捕获并以 `caveat` 形式记录，绝不抛出
- 空文件 / 不存在文件 → 兜底 `_make_failed_result` / `_make_empty_result`

### 3.3 `report_orchestrator.py` — 报告编排层

```
ReportOrchestrator(client, result, mode).run() → ReportOutcome
  .text           — 报告文本
  .quality_status — "standard" | "degraded"
  .attempts       — 调用模型次数（1 或 2）
  .warnings       — 质量问题列表
```

**职责**:
- 将 `AnalysisResult` 转为中文 prompt
- 调用 `ModelClient.complete()` 生成结构化报告
- 验证 9 个必需章节 + 核心发现的 5 个子要素
- 不合格时执行一次修订（第二次模型调用）
- 仍不合格或异常 → 返回降级报告

**模型约束（写入 system prompt）**:

> 模型只可解释 evidence，不能进行新的计算。

**章节验证**:
| 必需章节 | 别名 |
|----------|------|
| 数据概览 | 数据集概览 |
| 数据质量检查 | 数据质量, 数据质量分析 |
| 基础指标分析 | 基础指标, 描述性统计, 基本指标 |
| 分组与排名分析 | 分组分析, 排名分析, 分组与排名, 类别分析 |
| 趋势分析 | 时间趋势, 趋势, 月度趋势 |
| 核心发现 | 关键发现, 主要发现 |
| 业务建议 | 行动建议, 建议, 业务建议与行动 |
| 分析边界与风险警告 | 分析边界, 风险警告, 局限性, 局限性分析, 风险提示 |
| 初学者教学总结 | 教学总结, 总结, 初学者总结, 学习要点 |

**核心发现子要素**: 数据证据 / 方法解释 / 业务含义 / 风险边界 / 初学者复用

### 3.4 `pipeline.py` — 流水线编排层

```
run_data_help_analysis(input_file, output_dir, provider, model, mode) → dict
```

**职责**:
- 文件预处理（Excel→CSV）
- 调用确定性分析引擎
- 根据引擎是否成功分支：
  - 成功 → `ReportOrchestrator` 路径（无 agent）
  - 失败 → 传统 agent 路径（V1 兼容）
- 生成交付物（Excel / HTML / PDF）
- 收集 `generated_files`、写入 `run_log.json`

**返回 dict 结构**:

```python
{
    "status": "completed" | "failed",
    "input_file": str,
    "output_dir": str,
    "generated_files": [str, ...],
    "evidence_status": "success" | "failed",
    "evidence_files": [str, ...],
    "evidence_error": str,
    "report_quality": {
        "status": "standard" | "degraded",
        "attempts": int,
        "warnings": [str, ...]
    },
    "final_answer": str,
    "duration_seconds": float,
    # ...
}
```

### 3.5 `tools_data.py` — 交付物生成层

| 函数 | 输出 | 关键内容 |
|------|------|----------|
| `generate_excel` | `.xlsx` | 原始数据、整体对比、维度分析、交叉分析、思维模型、自检问答、分析看板、分析报告 |
| `generate_html` | `.html` | KPI 卡片、字段信息、描述性统计、执行摘要、核心发现、完整报告 |
| `generate_pdf` | `.pdf` | 封面、关键指标、核心发现、行动建议、思维模型、数据预览、完整报告（CJK 字体） |

---

## 四、降级策略

### 4.1 降级矩阵

| 异常点 | 降级行为 | 最终效果 |
|--------|----------|----------|
| **create_model_client() 异常** | 跳过 ReportOrchestrator，直接调用 `build_evidence_report()` | report_quality.status=degraded，交付物正常生成 |
| **ReportOrchestrator.run() 异常** | 异常捕获，调用 `build_evidence_report()` | 同上 |
| **模型生成不合格（缺章节/缺子要素）** | 最多一次修订，仍不合格 → `_build_degraded_report()` | report_quality.status=degraded，报告含 "(降级版)" 标记 |
| **确定性分析引擎异常** | 回退到传统 agent 路径（V1 兼容） | evidence_status=failed，agent 继续分析 |
| **所有交付物生成异常** | 异常被捕获并打印警告，不影响 pipeline 主状态 | generated_files 中缺失对应文件 |

### 4.2 降级报告的能力

`build_evidence_report(result)` 免 LLM 调用，基于已有 `AnalysisResult` 直接生成包含全部 9 个标准章节的 Markdown：

- 数据概览 → 从 result 元数据填充
- 数据质量检查 → 从 missing + duplicates plan 填充
- 基础指标分析 → 从 numeric_stats plan 填充
- 分组与排名分析 → 从 categorical plan 填充
- 趋势分析 → 从 date_trend plan 填充
- 核心发现 → 启发式选取最佳 evidence 构建 5 子要素
- 业务建议 → 基于缺失/重复率 + 数值/分类/趋势 evidence 生成
- 分析边界与风险警告 → 汇总全部 caveat
- 初学者教学总结 → 基于使用的方法列表生成

---

## 五、可验证性

### 5.1 generated_files

`run_data_help_analysis()` 返回的 `result["generated_files"]` 包含本次运行生成的所有文件名。典型列表：

```
["analysis_evidence.json",
 "analysis_evidence.md",
 "analysis_report.md",
 "xxx_analysis.xlsx",
 "xxx_report.html",
 "xxx_report.pdf"]
```

### 5.2 run_log.json

每次运行在 `task_dir/run_log.json` 写入完整运行记录，包含：

- 输入文件、起止时间、耗时
- evidence_status / evidence_error
- report_quality (status, attempts, warnings)
- generated_files 全列表
- 最终状态 (status) 和 final_answer

### 5.3 evidence_status & report_quality

| 字段 | 取值 | 含义 |
|------|------|------|
| `evidence_status` | `"success"` | 确定性分析引擎正常完成 |
| `evidence_status` | `"failed"` | 引擎异常，回退到 agent 路径 |
| `report_quality.status` | `"standard"` | LLM 生成的报告通过章节+结构验证 |
| `report_quality.status` | `"degraded"` | LLM 失败或不合格，使用降级报告 |
| `report_quality.attempts` | 1 或 2 | 实际调用模型的次数 |
| `report_quality.warnings` | `[str, ...]` | 质量问题详述 |

### 5.4 测试命令

```bash
# 运行所有 V2 相关测试
python -m pytest tests/test_analysis_engine.py -v
python -m pytest tests/test_report_orchestrator.py -v
python -m pytest tests/test_pipeline_evidence.py -v

# 指定单个测试
python -m pytest tests/test_analysis_engine.py::TestDeterministicAnalysisEngine::test_run_returns_analysis_result -v

# 全部测试一次运行
python -m pytest tests/test_analysis_engine.py tests/test_report_orchestrator.py tests/test_pipeline_evidence.py -v
```

测试覆盖的场景：

| 测试文件 | 覆盖目标 |
|----------|----------|
| `test_analysis_engine.py` | 7 类 plan 的正确性、边界场景（空/不存在/全文本文件）、evidence 合约完整性、Markdown 输出 |
| `test_report_orchestrator.py` | 一次合格、缺章节修订成功、二次仍不合格、异常降级、修订异常、边缘场景、audit 模式 |
| `test_pipeline_evidence.py` | 证据文件生成、evidence_status 字段、agent 不被调用、报告质量字段、引擎异常降级、create_model_client 异常降级 |

---

## 六、当前已知边界

### 6.1 通用自动分析性质

V2 是**通用自动分析工具**，非定制化业务看板。分析维度由引擎基于列名启发式推断（如 revenue/cost 匹配派生利润），不感知特定行业的业务语义。

### 6.2 业务指标语义依赖输入列

- 派生指标（profit / margin）依赖列名包含 `revenue` / `cost`（支持中英文）
- 分类列检测基于 dtype + 唯一值数量阈值（`_MAX_CATEGORICAL_UNIQUE=50`）
- 日期列检测基于格式匹配 + 样本解析成功率 ≥ 50%
- 若业务列名不符合预期模式，引擎仍会执行基础统计（profile / missing / numeric_stats），但无法自动计算业务级派生指标

### 6.3 模型可用性依赖

- `ReportOrchestrator` 依赖外部 LLM API（deepseek / openai / anthropic / ollama）
- 模型调用超时或返回格式异常时，自动降级为 evidence-based 报告
- 降级报告的"核心发现"和"业务建议"使用启发式模板，质量低于 LLM 生成版本

### 6.4 交付物生成依赖

- Excel 依赖 `openpyxl`
- HTML 无额外依赖（纯字符串拼接）
- PDF 依赖 `fpdf2` + CJK 字体文件（通过 `DATAHELP_CJK_FONT_PATH` 环境变量或预定义路径配置）
