# DataHelp V3 重构方案

> 写于 2026-10-22。基于对整个仓库的逐文件审计（128 个单测可跑通），不是凭文档推断。

---

## 一、体检结论

### 0. 最刺眼的一条：当年修好的病，已经复发了

`AUDIT_V3.md` 的结论是"SKILL.md 被截断到只剩 3%，规则进不了模型"。当时的修法是把前缀预算提到 32,000 字符。

现在实测（`_build_prefix()` 直接调用）：**prefix = 34,704 字符，已经超出 32,000 的预算**。再叠上工具表、工作区、阶段指令，一次真实请求总长实测 146,320 字符，裁剪后仍有 111,031——是 50,000 总预算的 2.2 倍，而 `_reduce_sections` 没有二次检查（`context_manager.py:165`）。裁剪是保留头部、丢弃尾部（`context_manager.py:180`），所以最先被切掉的恰好是后加的工具表和分析阶段指令。

那次修复是"把数字调大"，不是"让结构不依赖数字"。数字调大之后，内容又长回来了。

### 1. 仓库里同时住着两代产品，而且主路径走的是旧的那一代

同一个 `python -m datahelp`，不同子命令走的是两套互不相关的实现：

| 入口 | 实际执行 | 报告结构 | 数值来源 |
|---|---|---|---|
| one-shot / REPL / eval | `runtime.py` 的 agent 循环 | 13 节，靠 prompt 要求 | 模型自己写 pandas 脚本算 |
| watch | `pipeline.py` → 确定性引擎 → `report_orchestrator.py` | 9 节，靠标题校验 | pandas 计算，模型只解释 |

`pipeline.py`、`analysis_engine.py`、`report_orchestrator.py` 这三个文件（约 1400 行，是 6 月 25 日那次 "Upgrade to V2" 的成果）**只能从 watch 子命令到达**。手动分析永远走不到它们。

这不是架构分层，是一次升级没有接线。

### 2. 报告结构有四份互斥的定义

| 位置 | 节数 | 谁在用 |
|---|---|---|
| `runtime.py:255` | 13 节 | agent 循环 |
| `report_orchestrator.py:41` | 9 节 | 确定性路径 |
| `skill_engine.py:84` | 7 / 12 / 13 节（按模式） | 终稿校验 |
| `tools_data.py:153` | 6 个别名，要求"执行摘要" | 交付物生成 |

改一处不会传导到其他三处。`tools_data` 要的"执行摘要"两条链路都不产出，所以交付物永远走兜底分支（`tools_data.py:239`）。

### 3. 规则也存了三份

`runtime.py:149` 的 SYSTEM_PROMPT、`skill_engine.py:84` 的模式规则、`skills/business_analysis/SKILL.md`（769 行）——同一套教学要求，三处各写一遍。改 SKILL.md 不影响另外两处。

这正是 AUDIT_V3 教训 1（"手写提取是最大的敌人"）的反面：当时删掉了一处手写提取，现在又长出来两处。

### 4. 模型层停留在 2023 年的接口

`models.py:40`：`complete(prompt: str) -> str`。整个对话被压成一个字符串，没有 messages、没有原生工具调用、没有流式。

于是只能造一层文本协议，再用七级正则兜底去猜模型的意思——`runtime.py:55-139`，包括"连续格式错误后根据文本里有没有 `import pandas` 来猜该写文件还是该运行"（`runtime.py:110-131`）。

这 85 行存在的唯一原因是：接口没法让模型规规矩矩地说"我要调这个工具、参数是这些"。

### 5. 几个会直接影响用户的缺陷

| 现象 | 位置 | 后果 |
|---|---|---|
| 引擎读表失败也标"成功" | `pipeline.py:213` | 失败被伪装成正常，继续出 9 节报告 |
| 交付物按目录下**所有** CSV 生成 | `cli.py:107` | 分析 A 文件，B 的 xlsx 也生成，结论还贴同一份 |
| 章节校验是子串匹配，别名含"建议""总结" | `skill_engine.py:256` | 几乎是中文报告就能过，校验形同虚设 |
| 同一份数据算两遍 | `analysis_engine.py` 与 `tools_data.py:793` | 引擎的证据和交付物里的统计各算各的，没有一致性校验 |
| `--mode` 时用 `input()` 交互 | `cli.py:57` | 管道/CI 下直接 EOF 崩 |
| 默认值三处不一样 | `cli.py:149` / `config_manager.py:19` / `models.py:78` | 同一件事三个答案 |
| `watcher.py:1` docstring 没闭合 | 全文第一行 | `from __future__ import annotations` 变成了字符串内容，现在没崩只是因为 3.10+ 语法兼容 |
| `pyproject.toml` 没声明 numpy | 依赖表 | 靠 pandas 传递依赖，pandas 一变就断 |

### 6. 测试的有效范围

128 个测试全过，但：
- 全部用 mock 模型，没有一次真实调用；
- `test_set.jsonl` 只有 4 条，内容是"列出项目根目录的文件"这种冒烟用例，不是数据分析用例；
- 仓库里**没有任何一份样本数据**；
- 两条链路各自测，没有测它们的契约冲突（比如交付物要的那一节谁都不产出）。

也就是说：测试证明的是"代码能按写的跑"，不是"产品能出好报告"。

### 7. 两处"看着有、其实没有"的功能

- **长期记忆**：`memory.promote()`（`memory.py:223`）全仓库没有调用方。`runtime.py:598` 每次往 prompt 里注入的"## 长期记忆"区块，永远是空的。用户以为它在积累，其实什么都没存。
- **委派工具**：`tools.py:324` 注册了一个叫 `delegate` 的工具，`run` 的实现就是返回字符串 `"(delegate: ...)"`。模型会以为自己能派活出去。

### 8. 会话状态会串味

checkpoint 是固定文件名（`runtime.py:324`），`ask()` 进来先无条件读它，不校验是不是同一个任务（`runtime.py:362`）。REPL 里上一个任务异常结束后，下一个任务会继承它的 history 和阶段标志。

更糟的是 `_convergence_triggered` / `_analysis_scripts_written` 在任务之间不重置（`runtime.py:381` 只重置了三个字段）。一旦触发过收敛，后续每个任务都带着"禁止任何工具调用"的阶段指令（`runtime.py:618`），而用户看不出来。


---

## 二、两份都只对了一半

**V1（agent）对在**：分析什么由模型决定，遇到怪列名、怪数据能现场想办法。
**V1 错在**：数值是模型写的脚本算出来的，脚本不留档、过程不复现、结论不可审计。

**V2（确定性引擎）对在**：数值只来自 pandas 计算，模型不许自己算。
**V2 错在**：分析什么被写死成 7 个 plan，模型退化成复读机；而且这条路根本接不到手动入口。

V1 的问题是「谁来算」，V2 的问题是「谁来定分析什么」。两个各自只解决了一半。

### 实测：引擎在一张普通零售表上就露馅了

用 `examples/sample_sales.csv`（2020 行，12 列，一张很正常的零售订单表）跑确定性引擎，证据文件里出现三处明显误判：

1. `categorical` 计划把 `order_id`（2000 个唯一值）识别成分类维度。主键被当成了可分组字段——这正是初学者最常犯的错，而产品号称在教初学者。
2. `order_date` 同时出现在 `categorical` 和 `date_trend` 两个计划里，因为它既是字符串又能解析成日期。
3. `derived_metrics` 的输出是「未找到 revenue 列」，所以没有算利润。可表里明明有 `unit_price`、`quantity`、`discount`、`cost_per_unit`，收入就是这三者的乘积。

这三处都不是 bug，是设计使然：引擎只会按列名和 dtype 做固定规则匹配。换个模型来做，第一眼就能看出订单号不该分组、收入该自己算。

也就是说，V2 用「7 个写死的 plan」换来的确定性，代价是**它只会分析它事先想到的那种表**。

---

## 三、V3：模型提计划，代码算数，模型写解释

```
输入 CSV
   │
   ├─① profile（确定性，不调模型）
   │    列类型 / 基数 / 缺失 / 时间跨度 / 疑似主键 / 疑似金额列 / 单位猜测
   │    → dataset_profile.json
   │
   ├─② plan（模型，结构化输出）
   │    基于 profile + 用户问题，产出分析计划：
   │    要回答哪几个业务问题、每个问题需要算什么、口径与公式、为什么值得算
   │    → analysis_plan.json
   │
   ├─③ compute（确定性，子进程 pandas）
   │    把 plan 编译成 pandas 代码执行，产出证据：
   │    值 / 公式 / 来源列 / 样本量 / 口径说明 / caveat
   │    → evidence.json + evidence.md
   │
   ├─④ narrate（模型，只解释不计算）
   │    拿证据写教学报告，禁止引入新数字
   │    用 schema 校验结构，不用正则匹配标题
   │    → report.md
   │
   └─⑤ deliver（确定性渲染）
        Excel / HTML / PDF 全部从 evidence + report 渲染，三处共用一份章节定义
```

一句话：**模型负责"该算什么"，代码负责"算出什么"，模型负责"这说明了什么"。**

### 和原来比，改了什么

| | V1 | V2 | V3 |
|---|---|---|---|
| 分析什么 | 模型自由决定 | 写死 7 个 plan | 模型提计划（可审计） |
| 数值来源 | 模型写的脚本 | pandas | pandas，且公式随计划落档 |
| 报告结构 | prompt 要求 | 标题正则校验 | schema 校验 |
| 模型接口 | 单字符串 + 7 层正则 | 单字符串 | messages + 原生工具调用 |
| 入口 | 一个 | 另一个（watch） | 一个 |
| 交付物数据 | 界面文字贴进去 | 从证据渲染 | 从证据渲染 |

### 为什么"现在"能做

不是因为模型变聪明了这种没法验证的说法。是因为这几件事现在是接口能力，而代码没用上：

1. **原生工具调用**：模型能直接给出结构化调用，`runtime.py:55-139` 那 85 行猜测逻辑不需要存在。
2. **结构化输出 / JSON schema 约束**：plan 和报告的结构可以由接口保证，不需要事后用正则从 markdown 里抠章节。
3. **长上下文**：SKILL.md 全文 28,790 字符可以直接放进 system prompt，不需要再搞一套字符预算去裁。
4. **prompt 缓存**：一段固定不变的大 system prompt，重复调用时可以缓存，成本问题不再逼人砍上下文。

前三条是设计约束，第四条是成本约束——它们共同让"注入全文 + 结构化契约"成为可行方案，而这正是 V2 的两条补丁想绕开的问题。

---

## 四、迁移步骤

分四步，每步都能独立跑通、可回滚。

**第 0 步：止血（不影响架构）**
- 删死代码：`skill_engine.count_thinking_models`、`get_report_quality_requirement`、`get_required_excel_sheets`、`tools_data.read_evidence_file`
- 修 `pipeline.py:213` 的失败伪装、`cli.py:107` 的全目录生成、`watcher.py:1` 的 docstring
- `pyproject.toml` 补 numpy 和版本上限
- 补一份样本数据（已加 `examples/sample_sales.csv`）和一条真实模型回归用例

**第 1 步：合流入口**
- 新建 `datahelp/app.py`，作为唯一编排层
- cli / REPL / watch 都调它，删掉 `pipeline.py:259-286` 回退到 agent 的分支
- 此时行为不变，只是路径统一

**第 2 步：换模型层**
- `ModelClient.complete(prompt)` → `chat(messages, tools=...)`
- 删 `runtime.py` 的 XML 协议与 7 级正则
- 工具 schema 从 `tools.py` 的字符串 DSL 换成 JSON Schema

**第 3 步：三段式落地**
- 新增 `profile.py`、`plan.py`、`compute.py`、`narrate.py`
- `evidence.json` 成为唯一数值来源；交付物只读它
- 章节定义收敛到一处 `report_schema.py`，四份合并成一份

**第 4 步：删旧**
- 删 `runtime.py` 的循环（保留其会话/checkpoint 能力，迁到 app.py）
- 删 `skill_engine.py` 的门控
- 保留 `analysis_engine.py` 里经过验证的 pandas 计算函数，作为 compute 的执行后端

---

## 五、验收标准

重构是否成功，不看"代码更短了"，看这四条：

1. **同一份数据、同一个问题，跑两次，evidence.json 的每个数值完全一致。**
2. **报告里出现的每个数字，都能在 evidence.json 里找到出处（值 + 公式 + 来源列）。**
3. **`python -m datahelp` 一条命令出全套交付物，且交付物里的数字与 evidence 一致。**
4. **换一份没见过的数据（列名不同、有脏值），不崩、不编造，缺什么说什么。**

第 2 条是这次重构的核心价值：它是"教学型分析"能被信任的前提。

---

## 六、尚未确认

- 当前可用的模型型号与其结构化输出能力，需要在有 API key 的环境里实测。
- `analysis_engine.py` 里哪些计算值得原样保留，取决于第 3 步选定的 compute 后端形态。
- 审计报告里"deepseek-v4-pro 弱于 Claude"是当时环境下的观察，本次重构不假设模型强弱，只依赖接口能力。


---

## 七、进度

### 已完成（2026-10-22）

- **第 0 步 止血**：修掉引擎失败伪装成功、失败后仍报 completed、返回契约不一致、watcher docstring 未闭合、依赖未声明 numpy。
- **第 1 步 合流**：新增 `app.py` 作为唯一编排层；`watch` 与 one-shot 都走它。
  `python -m datahelp "分析 examples/x.csv"` 现在直接进入确定性链路，`--agent` 才回退旧循环。
- **第 3 步 三段式核心**：`profile.py`（画像）、`plan.py`（计划 + 确定性计算）、
  `narrative.py`（报告渲染 + 章节校验）已落地，并在样本数据上跑通。
- **样本数据**：`examples/sample_sales.csv` + 生成脚本，含缺失、重复、极端值。

### 已经解决的旧问题

| 旧问题 | 现在 |
|---|---|
| 把 `order_id` 当分类维度 | 画像识别为主键，不进入分组 |
| 日期列同时当分类列 | 只作为 `datetime`，单独走趋势 |
| `discount` 被猜成数量 | 先判比率，再判金额/数量 |
| 认不出"单价 × 数量 × 折扣" | 派生指标支持乘法公式，自动推出营收 |
| 四份互斥的章节定义 | `narrative.SECTIONS` 一处定义 |
| 报告数字无法追溯 | 每条证据带 formula / source_columns / caveat |

### 未完成

- 第 2 步：模型层仍是 `complete(prompt) -> str`，还没换成 messages + 原生工具调用。
- 第 4 步：`runtime.py`、`pipeline.py`、`report_orchestrator.py`、`analysis_engine.py`
  仍在仓库里（前两个已不在主路径上），需要删除或吸收。
- 交付物仍由 `tools_data.py` 自己再算一遍统计，尚未改成"只从 evidence 渲染"。
- 没有 API key，模型路径（plan_with_model / 报告生成）只在 fake client 上验证过。
