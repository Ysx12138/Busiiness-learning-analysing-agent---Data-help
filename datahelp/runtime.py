"""agent 主循环 —— 感知、决策、行动、记录。"""
from __future__ import annotations

import json
import re
import textwrap
import time as _time
from pathlib import Path

from datahelp.context_manager import ContextManager
from datahelp.memory import LayeredMemory, DurableMemoryStore, summarize_read_result
from datahelp.skill_loader import load_skill
from datahelp.skill_engine import SkillEngine
from datahelp.task_state import TaskState
from datahelp.workspace import WorkspaceContext
from datahelp.tools import (
    build_tool_registry,
    describe_tools,
    validate_tool_args,
    is_repeated_call,
    resolve_path,
)

MAX_STEPS = 40
MAX_ATTEMPTS = 50
MAX_TOOL_OUTPUT = 12000


def _brief_args(args: dict, max_len: int = 60) -> str:
    """缩短参数显示，避免刷屏。"""
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 40:
            s = s[:37] + "..."
        parts.append(f"{k}={s}")
    joined = ", ".join(parts)
    if len(joined) > max_len:
        joined = joined[:max_len - 3] + "..."
    return joined


def clip(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[截断 {len(text) - limit} 字符]"


_TOOL_EXAMPLE = '正确格式示例：\n<tool>{"name": "csv_summary", "args": {"path": "data.csv"}}</tool>\n或：\n<final>完整分析报告</final>'

_FINAL_HINT = '如果分析完成，请直接使用 <final> 输出报告。如有任何工具调用，必须用 <tool> 标签包裹。'


def parse_model_output(raw: str, retry_count: int = 0, analysis_scripts_written: list[str] | None = None) -> tuple[str, dict | str | None]:
    text = raw.strip()
    if not text:
        return "retry", {"message": f"返回内容为空，请输出工具调用或最终报告。\n{_TOOL_EXAMPLE}"}

    # 1. <tool>JSON</tool> —— 优先精确解析
    tool_match = re.search(r"<tool>(.*?)</tool>", text, re.DOTALL)
    if tool_match:
        inner = tool_match.group(1).strip()
        try:
            payload = json.loads(inner)
            return "tool", payload
        except json.JSONDecodeError as e:
            msg = f"<tool> 标签内 JSON 解析失败：{e}。\n修复提示：检查引号、逗号、花括号是否完整。\n{_TOOL_EXAMPLE}"
            return "retry", {"message": msg}

    # 2. <tool name="xxx">XML风格</tool>
    xml_match = re.search(r'<tool\s+name=["\'](\w+)["\']>(.*?)</tool>', text, re.DOTALL)
    if xml_match:
        name = xml_match.group(1)
        body = xml_match.group(2).strip()
        return "tool", {"name": name, "args": _parse_xml_args(body)}

    # 3. <final>
    final_match = re.search(r"<final>(.*?)</final>", text, re.DOTALL)
    if final_match:
        return "final", final_match.group(1).strip()

    if len(text) > 10:
        # 4. 宽松解析：整段文本是否为 JSON tool call
        try:
            payload = json.loads(text)
            if isinstance(payload, dict) and "name" in payload and "args" in payload:
                return "tool", payload
        except json.JSONDecodeError:
            pass

        # 5. 宽松解析：找第一个 JSON 对象
        json_match = re.search(r'\{(?:[^{}]|"(?:\\.|[^"\\])*")*\}', text, re.DOTALL)
        if json_match:
            candidate = json_match.group(0)
            try:
                payload = json.loads(candidate)
                if isinstance(payload, dict) and "name" in payload and "args" in payload:
                    return "tool", payload
            except json.JSONDecodeError:
                pass

        # 6. 包含 JSON 但没用 <tool> 包裹
        if '"name"' in text and '"args"' in text:
            msg = ("输出中包含 JSON 格式的工具调用，但没有被 <tool> 标签包裹。\n"
                   "请将 JSON 放到 <tool> 标签内：\n"
                   '<tool>{"name": "xxx", "args": {...}}</tool>')
            return "retry", {"message": msg}

        # 7. 智能恢复：连续格式错误后自动识别意图
        if retry_count >= 2 and len(text) > 200:
            has_pandas = "import pandas" in text or "pd." in text
            has_read_csv = "read_csv" in text
            has_analysis = any(x in text for x in ("df.describe()", "df.groupby", "df.isnull()", "df.corr()"))
            pending = [s for s in (analysis_scripts_written or []) if True]  # copy
            if has_pandas or (has_read_csv and has_analysis):
                if pending and not any(s in text for s in pending):
                    # 已有待执行脚本且当前输出不是运行命令 → 转为 run_shell
                    return "tool", {"name": "run_shell", "args": {"command": f"python {pending[0]}", "timeout": 60}}
                # 自动选择文件名：如果 analysis_01.py 已存在则用 02
                auto_name = "analysis_01.py"
                if analysis_scripts_written and "analysis_01.py" in analysis_scripts_written:
                    auto_name = "analysis_02.py"
                return "tool", {"name": "write_file", "args": {"path": auto_name, "content": text}}
            has_report_structure = any(x in text for x in ("## 1.", "## 数据概览", "## 核心发现", "数据质量检查"))
            if has_report_structure:
                return "final", text
            # 文本中包含运行 Python 脚本的意图
            run_match = re.search(r'(?:python|python3)\s+(analysis_\d+\.py)', text)
            if run_match:
                return "tool", {"name": "run_shell", "args": {"command": f"python {run_match.group(1)}", "timeout": 60}}

        # 8. 长文本 → 标准 retry
        msg = f"请使用 <tool> 调用工具，或用 <final> 输出最终报告。\n{_TOOL_EXAMPLE}"
        if retry_count >= 2:
            msg = f"连续多次格式错误。请严格按以下格式之一输出：\n\n{_TOOL_EXAMPLE}\n\n{_FINAL_HINT}"
        return "retry", {"message": msg}

    return "retry", {"message": f"输出太短或格式不正确。\n{_TOOL_EXAMPLE}"}


def _parse_xml_args(body: str) -> dict:
    args = {}
    for match in re.finditer(r"<(\w+)>(.*?)</\1>", body, re.DOTALL):
        args[match.group(1)] = match.group(2).strip()
    return args


SYSTEM_PROMPT = textwrap.dedent("""\
    你是 DataHelp，一个面向商科初学者的数据分析教学 Agent。

    ## 核心工作流程

    每次数据分析按以下步骤执行：

    **Step 0 — 数据探索**
    先调用 csv_summary 了解数据规模、列名、行列数。
    观察字段名中是否包含外键（product_id、customer_id、order_id），
    如果有，用 list_files 查看数据目录找到关联的 CSV 文件。

    **Step 1 — 深度分析（写 Python 脚本）**
    用 write_file 把分析脚本写入 .py 文件（如 analysis_01.py），
    然后用 run_shell 执行。

    每个分析脚本都必须使用 pd.read_csv("文件名.csv") 读取原始 CSV 文件。
    一个脚本中可同时完成多个分析维度：

    ① 数据质量检查 — 缺失值、重复值、异常值
    ② 描述性统计 — 均值、中位数、标准差、分位数、总和
    ③ 分组聚合 — 按品类/卖家/区域等分组求和、均值、占比
    ④ 排名分析 — Top/Bottom 排名 + 头部集中度
    ⑤ 交叉分析 — 两个字段的关系（如价格区间 × 运费比）
    ⑥ 衍生指标 — AOV（总收入/总订单数）、利润率、运费占比
    ⑦ 分布分析 — 订单商品数分布、价格分桶
    ⑧ 趋势分析 — 有日期字段时按月聚合，计算环比
    ⑨ 跨表关联 — 发现外键时用 pd.merge 关联其他 CSV 文件
    ⑩ 分析脚本必须写 analysis_evidence.md，包含实际计算数值；最终报告不得将"应执行"当作结论。

    **证据文件要求（必须遵守）**
    analysis_01.py（及 analysis_02.py）除了少量终端 print 摘要外，必须在工作目录写出 analysis_evidence.md。
    该文件必须包含以下由真实数据计算得出的结果（禁止写"应执行"或任何占位符文本）：

    - 数据质量概要：缺失值数、重复值数、异常值数
    - 核心指标：各数值列的均值、中位数、合计
    - 分组统计：按区域/品类/卖家等维度的分组聚合结果（含具体数值）
    - Top 排名：Top 5 / Bottom 5 排名数据
    - 趋势分析（如有日期字段）：按月聚合的数值和环比变化

    最终报告（&lt;final&gt;）只能引用工具输出或 analysis_evidence.md 内实际存在的数值。不得编造任何数字。

    **Step 2 — 书写分析脚本（一次性完成多维度）**
    把 5+ 个分析维度写进**一个**脚本 analysis_01.py，一次性执行。
    **禁止**：不要拆成多个小脚本、不要写 analysis_02.py 之前就反复改 analysis_01.py。

    **Step 2b — 迭代（最多 1 轮，封顶 2 轮总计）**
    如果 analysis_01.py 执行完确实遗漏了重要维度，可写 analysis_02.py 做补充。
    **最多写 2 个分析脚本。analysis_02.py 执行完毕后必须立即输出 <final>，不得再写 analysis_03.py。**

    **Step 3 — 编译报告**
    汇总所有分析脚本的输出，输出完整的 13 节 <final> 报告。
    教学规则和输出结构见下方 Business Analysis Skill（全文）。

    **Step 4 — 生成交付物**（如果配置了 output-dir）
    调用 generate_excel / generate_html / generate_pdf 生成文件。

    ## 数据真实性规则（必须遵守）

    本系统严禁任何形式的数据编造。

    1. **第一步必须调用 csv_summary**：获取实际行数、列数、列名后才能进行后续分析。系统会阻止你在未执行 csv_summary 的情况下使用 run_shell。
    2. **脚本必须读取真实数据**：必须使用 pd.read_csv("文件名.csv") 读取原始 CSV 文件。绝对不得在代码中构造模拟数据（如 data = [{"col1": ...}]）或使用 random 生成数据。
    3. **所有数值必须可追溯**：报告中的每个均值、合计、百分比、排名，都必须来自工具实际返回的结果。任何数值不是来自工具执行结果的，不得写入报告。
    4. **如果 csv_summary 返回错误**：说明文件不存在或路径错误，先修正路径，不要编造数据代替。

    ## 数据质量与异常处理规则（必须遵守）

    当分析过程中遇到以下情况，必须按规则处理，不得忽略或编造：

    **字段含义不清**
    - 如果字段名无法直接推断业务含义（如 f1、col_a、tmp_001），在报告中标记为"⛔ 字段含义待确认"
    - 不得编造业务解释，应列出字段名、统计范围（最小/最大/均值）、可能的业务归属（订单/用户/商品等）
    - 要求用户补充字段口径说明

    **数据缺失严重**
    - 缺失比例 > 70%：标记"⛔ 严重缺失"，建议删除该字段
    - 缺失比例 30-70%：标记"⚠️ 缺失偏高"，说明风险、标注可能偏差方向，降低该字段权重
    - 缺失比例 < 30%：标记"ℹ️ 轻度缺失"，在分析中标注但不影响主要结论
    - 始终输出缺失字段名、缺失比例、对分析的潜在影响

    **分析结果不稳定**
    - 样本量 < 30 时：输出"⚠️ 小样本警告"，优先展示原始数据而非统计推断
    - 单一指标波动超过 50%：输出分析依据、限制条件、下一步验证方式
    - 不得把单次结果包装成确定结论；每次结论末尾附带"验证建议"

    **报告不可读**
    - 报告中每个分析方法至少包含一句"为什么要这样做"的业务解释
    - 如果某段报告只有指标没有解释，自动补充"指标含义 → 业务解释 → 学习复盘"三段
    - 确保初学者能理解每个数字代表什么、为什么重要

    ## 工具使用规则

    - 一个分析脚本尽量覆盖 5+ 个分析维度，一次性执行，不要拆成多次小工具调用
    - 每次执行之间输出简短的中间分析，帮助用户理解
    - 执行 run_shell 后，直接从输出文件读取结果，**不要重新读取 .py 脚本内容**
    - **最多 2 轮分析脚本。analysis_02.py 执行后必须立即输出 <final>**，不得继续深挖

    ## 格式规范

    - 工具调用：<tool>{"name": "xxx", "args": {...}}</tool>
    - 中间分析：在工具调用之间输出简短的分析说明
    - 最终报告：<final>完整的分析报告</final>

    ## 最终报告结构（必须遵守）

    最终报告必须包含以下 13 个部分，缺一不可：

    1. **数据概览** — 数据集规模、字段说明、外键识别
    2. **字段识别与业务含义** — 每个字段的语义、字段-业务问题映射表
    3. **数据质量检查** — 缺失值、重复值、异常值检查结论及教学说明
    4. **基础指标分析** — 均值/中位数/标准差/分位数，含教学要点
    5. **分组与排名分析** — 按类别/区域/卖家分组聚合、Top/Bottom 排名及集中度
    6. **趋势分析** — 有日期字段时按月聚合，计算环比变化
    7. **核心发现** — 每个发现含 8 要素教学（结果/方法/指标/公式/字段/业务/风险/复用）
    8. **业务建议** — 数据驱动的具体行动建议
    9. **进阶分析推荐** — 建议但不自动执行，说明为什么适合、需要什么字段、能回答什么
    10. **跳过的高级方法及原因** — 透明说明哪些方法不执行、为什么
    11. **分析边界与风险警告** — 当前分析的局限性和所有风险提示
    12. **初学者教学总结** — 为什么这样分析、思维模型回顾、可复用的分析思路
    13. **质量检查与改进建议** — 对本次分析的自行评估，包含：

        **通过项**
        - 字段解释完整度：关键字段是否说明含义、类型、可分析方向、风险
        - 分析问题质量：问题是否被当前数据回答，是否避免空泛商业口号
        - 报告可读性：初学者是否能看懂结论来源、指标含义、下一步验证
        - 复盘可迁移性：是否留下方法、公式、适用条件、下次操作建议

        **风险项**
        - 列出所有因数据限制、样本量、缺失字段导致的结论风险

        **建议修正**
        - 针对风险项列出可操作改进建议

    在完成 1-2 轮分析脚本后，立即用 <final> 输出完整的 13 节报告。注意：最多 2 轮，analysis_02.py 执行后不得再写新脚本。
""")


class DataHelp:

    def __init__(self, model_client, repo_root: str, tools: dict | None = None, memory: LayeredMemory | None = None, durable_memory: DurableMemoryStore | None = None, max_steps: int = MAX_STEPS, max_new_tokens: int = 4096, approval_policy: str = "auto", checkpoint_dir: str | None = None, skill_name: str | None = None, mode: str | None = None, output_dir: str | None = None):
        self.model = model_client
        self.repo_root = repo_root
        self.tools = tools or build_tool_registry(repo_root)
        self.memory = memory or LayeredMemory()
        self.durable_memory = durable_memory
        self.max_steps = max_steps
        self.max_new_tokens = max_new_tokens
        self.approval_policy = approval_policy
        self.checkpoint_dir = checkpoint_dir
        self.history: list[dict] = []
        self.task_state: TaskState | None = None
        self.last_tool_call: tuple | None = None
        self._mode = mode
        self._output_dir = output_dir
        self.skill_instructions = load_skill(skill_name) if skill_name else ""
        self.skill_engine = SkillEngine(mode or "beginner_summary") if skill_name else None
        self._explored_csvs: set[str] = set()
        # 分析轮次追踪（收敛用）
        self._analysis_scripts_written: list[str] = []
        self._analysis_scripts_run: set[str] = set()
        self._convergence_triggered: bool = False
        self._phase: str = "explore"  # "explore" | "analyze" | "report"
        self._gate_block_count: int = 0  # 连续门控拦截计数（用于逐步放宽限制）
        self._report_validated: bool = False  # 是否已完成 1 轮报告校验修正

        # 前缀缓存
        self._cached_prefix: str | None = None
        self._cached_fingerprint: str | None = None

    # ── checkpoint ──────────────────────────────────

    def _checkpoint_path(self) -> str | None:
        if not self.checkpoint_dir:
            return None
        p = Path(self.checkpoint_dir) / "checkpoint.json"
        return str(p)

    def _save_checkpoint(self):
        ckpt_path = self._checkpoint_path()
        if not ckpt_path or not self.task_state:
            return
        data = {
            "version": 1,
            "history": self.history,
            "last_tool_call": list(self.last_tool_call) if self.last_tool_call else None,
            "task_state": self.task_state.to_dict(),
            "memory_state": self.memory.state,
            "attempts": self.task_state.attempts,
            "tool_steps": self.task_state.tool_steps,
            "explored_csvs": list(self._explored_csvs),
            "analysis_scripts_written": list(self._analysis_scripts_written),
            "analysis_scripts_run": list(self._analysis_scripts_run),
            "convergence_triggered": self._convergence_triggered,
            "phase": self._phase,
            "gate_block_count": self._gate_block_count,
        }
        Path(ckpt_path).parent.mkdir(parents=True, exist_ok=True)
        Path(ckpt_path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _load_checkpoint(self) -> dict | None:
        ckpt_path = self._checkpoint_path()
        if not ckpt_path or not Path(ckpt_path).exists():
            return None
        data = json.loads(Path(ckpt_path).read_text(encoding="utf-8"))
        return data

    def _remove_checkpoint(self):
        ckpt_path = self._checkpoint_path()
        if ckpt_path and Path(ckpt_path).exists():
            Path(ckpt_path).unlink()

    def ask(self, user_message: str) -> str:
        checkpoint = self._load_checkpoint()

        if checkpoint:
            self.history = checkpoint["history"]
            self.last_tool_call = tuple(checkpoint["last_tool_call"]) if checkpoint.get("last_tool_call") else None
            self.task_state = TaskState(checkpoint["task_state"])
            self.memory = LayeredMemory(checkpoint.get("memory_state"))
            self._explored_csvs = set(checkpoint.get("explored_csvs", []))
            self._analysis_scripts_written = list(checkpoint.get("analysis_scripts_written", []))
            self._analysis_scripts_run = set(checkpoint.get("analysis_scripts_run", []))
            self._convergence_triggered = checkpoint.get("convergence_triggered", False)
            self._phase = checkpoint.get("phase", "explore")
            self._gate_block_count = checkpoint.get("gate_block_count", 0)
            attempts = checkpoint["attempts"]
            tool_steps = checkpoint["tool_steps"]
        else:
            self.memory.set_task_summary(user_message)
            self.history.append({"role": "user", "content": user_message})
            self.task_state = TaskState.create(user_request=user_message)
            self._explored_csvs = set()
            self._phase = "explore"
            self._gate_block_count = 0
            attempts = 0
            tool_steps = 0

        consecutive_retries = 0
        last_raw_text = None  # 上次模型原始输出，用于连续格式错误时的智能恢复

        while tool_steps < self.max_steps and attempts < MAX_ATTEMPTS:
            attempts += 1
            self.task_state.record_attempt()

            # 阶段推进：探索了至少 1 个 CSV 且达到 4+ 步 → 进入分析
            if self._phase == "explore" and (self._analysis_scripts_written or (self._explored_csvs and tool_steps >= 4)):
                self._phase = "analyze"
                self._cached_prefix = None
            elif self._phase == "analyze" and self._convergence_triggered:
                self._phase = "report"
                self._cached_prefix = None

            _tick_start = _time.time()
            print(f"  ⚙️ [{tool_steps}/{self.max_steps}] 思考中…", end="", flush=True)

            # 连续格式错误 ≥2 次 → 跳过模型调用，用上次输出执行智能恢复
            used_auto_recover = False
            if consecutive_retries >= 2 and last_raw_text is not None:
                raw = last_raw_text
                last_raw_text = None
                kind, payload = parse_model_output(raw, 2, self._analysis_scripts_written)
                if kind == "retry":
                    consecutive_retries = 0  # 恢复失败，回退到正常模型调用
                else:
                    used_auto_recover = True

            if not used_auto_recover:
                prompt = self._build_prompt(user_message)
                try:
                    raw = self.model.complete(prompt, max_tokens=self.max_new_tokens)
                    _elapsed = _time.time() - _tick_start
                    print(f" ({_elapsed:.0f}s)", end="", flush=True)
                except (RuntimeError, TimeoutError, OSError) as e:
                    print(f" → ⚠️ API 错误，重试…")
                    self.history.append({"role": "assistant", "content": f"[api_error] 模型调用失败：{e}。请继续。"})
                    consecutive_retries += 1
                    continue
                kind, payload = parse_model_output(raw, consecutive_retries, self._analysis_scripts_written)
            else:
                print(f" → 🔧 智能恢复…", end="", flush=True)
                consecutive_retries = 0

            if kind == "tool":
                name = payload.get("name", "") if isinstance(payload, dict) else ""
                args = payload.get("args", {}) if isinstance(payload, dict) else {}
                consecutive_retries = 0
                last_raw_text = None
                # 门控检查（不消耗 tool_steps，类 retry）
                gate_error = self._check_gates(name, args)
                if gate_error:
                    self._gate_block_count += 1
                    consecutive_retries += 1  # 递增 retry 计数，使 auto-recover 能在多次门控后触发
                    last_raw_text = raw  # 保存原始输出用于 auto-recover
                    print(f" → ⚠️ {gate_error[:60]}")
                    self.history.append({"role": "assistant", "content": f"[gate] {gate_error}"})
                    continue

                self._gate_block_count = 0
                print(f" → 调用 {name}({_brief_args(args)})")
                result = self.run_tool(name, args)
                self.task_state.record_tool(name, args)
                tool_steps = self.task_state.tool_steps
                self.history.append({"role": "assistant", "content": f"调用工具: {name}"})
                self.history.append({"role": "tool", "content": clip(result), "tool_name": name})
                self._save_checkpoint()
                continue

            if kind == "retry":
                retry_msg = payload.get("message", "请重试。") if isinstance(payload, dict) else str(payload)
                print(f" → ⚠️ 格式有误，重试中…")
                self.history.append({"role": "assistant", "content": f"[retry] {retry_msg}"})
                consecutive_retries += 1
                last_raw_text = raw  # 保存原始输出，下次连续错误时用于智能恢复
                continue

            final_answer = str(payload or raw)
            # Phase 5: 报告结构校验（1 轮修正）
            if self.skill_engine and not getattr(self, '_report_validated', False):
                validation = self.skill_engine.validate_report_structure(final_answer)
                teaching_missing = self.skill_engine.check_teaching_elements(final_answer)
                corrections = []
                if not validation.passed:
                    corrections.append(f"报告缺少以下章节: {', '.join(validation.missing)}。请补充完整。")
                if teaching_missing:
                    corrections.append(f"核心发现缺少以下教学要素: {', '.join(teaching_missing)}。请为每个核心发现补充完整。")
                if corrections:
                    self._report_validated = True  # 只允许 1 轮修正
                    msg = "请修正报告后重新用 <final> 输出。\n" + "\n".join(corrections)
                    self.history.append({"role": "user", "content": msg})
                    print(f" → ⚠️ 报告结构不完整，要求修正...")
                    continue  # 给模型一次修正机会
            self.task_state.finish_success(final_answer)
            self.history.append({"role": "assistant", "content": final_answer})
            self._remove_checkpoint()
            print(f"  ✅ 完成！共 {tool_steps} 步")
            return final_answer

        # 退出循环：超限
        if tool_steps >= self.max_steps:
            self.task_state.finish_stopped("step_limit_reached")
            stop_msg = f"已达到最大步骤限制 ({self.max_steps})。任务已保存 checkpoint，输入相同任务可续跑。"
        else:
            self.task_state.finish_stopped("attempt_limit_reached")
            stop_msg = f"已达到最大尝试次数 ({MAX_ATTEMPTS})，其中 {tool_steps} 次成功。任务已保存 checkpoint，输入相同任务可续跑。"
        print(f"  ⛔ {stop_msg}")
        # ⚠️ 不删 checkpoint，用户可以续跑
        return f"<final>{stop_msg}</final>"

    def _build_prompt(self, user_message: str) -> str:
        prefix_text = self._build_prefix()
        cm = ContextManager(
            prefix_text=prefix_text,
            memory=self.memory,
        )
        prompt, _ = cm.build(user_message, self.history)
        return prompt

    def _build_prefix(self) -> str:
        workspace = WorkspaceContext.build(self.repo_root)
        fp = workspace.fingerprint()

        if self._cached_prefix is not None and fp == self._cached_fingerprint:
            return self._cached_prefix

        parts = [SYSTEM_PROMPT]

        # ── 结构化 Skill 指令 ───────────────────────
        if self.skill_engine:
            mode = self.skill_engine.config.mode
            skill_parts = [
                f"# Business Analysis Skill 指令\n",
                f"输出模式: {mode}",
                f"交付物目录: {self._output_dir or '无'}",
                f"模块开关: {self.skill_engine.config.to_toggle_string()}",
                "",
                "## 报告结构要求",
                "最终报告必须包含以下章节（按顺序）：",
            ]
            for i, s in enumerate(self.skill_engine.get_required_sections(), 1):
                skill_parts.append(f"  {i}. {s}")
            skill_parts.extend([
                "",
                "## 教学方法（所有模式通用）",
                "每个核心发现必须包含 8 要素：",
                "  1. 分析结果 — 数据展示了什么",
                "  2. 方法解释 — 为什么选择这个分析方法",
                "  3. 指标解释 — 指标含义和计算方式",
                "  4. 公式说明 — 具体计算公式和字段来源",
                "  5. 字段来源 — 使用了哪些数据列",
                "  6. 业务含义 — 结果对业务决策的意义",
                "  7. 风险边界 — 这个结果不能证明什么、可能的问题",
                "  8. 初学者复用 — 下次遇到类似数据如何应用",
                "",
                "## 分析方法分层",
            ])
            if mode == "beginner_summary":
                skill_parts.append("Tier 1（必须执行）：描述性统计、分组聚合、排名分析、数据质量检查")
                skill_parts.append("Tier 2（仅推荐，不自动执行）：相关分析、RFM、回归、聚类等——仅建议但不在本模式执行")
            elif mode == "audit_report":
                skill_parts.append("Tier 1（必须执行）：描述性统计、分组聚合、排名分析、数据质量检查")
                skill_parts.append("Tier 2（可自动执行）：相关分析、RFM、回归、聚类等——执行时需包含方法说明和风险警告")
            else:
                skill_parts.append("Tier 1（必须执行）：描述性统计、分组聚合、排名分析、数据质量检查")
                skill_parts.append("Tier 2（可执行但需说明）：相关分析、RFM、回归、聚类等——需附带为何选择此方法和风险说明")
            skill_parts.append("Tier 3（仅当用户确认）：因果推断等需要明确业务问题的方法")
            skill_parts.extend([
                "",
                "## 通用教学规则",
                "- 用通俗语言解释专业术语",
                "- 解释指标含义和计算方式（含公式和数据）",
                "- 区分数据事实、业务解读和假设推测",
                "- 数据驱动的结论需附带验证限制",
                "- 每个业务建议需包含：数据证据 → 业务含义 → 建议行动 → 追踪指标 → 所需数据",
                "- 禁止把相关性解释为因果",
                "- 禁止把预测结果呈现为确定事实",
                "",
                "## 思维模型要求",
                f"最少使用 {self.skill_engine.get_thinking_model_requirements()} 种思维模型进行教学：",
                "  1. 分解 — 把总量指标拆解为有意义的组成部分",
                "  2. 分层差异 — 不盲目相信平均值，比较子群差异",
                "  3. 代理推断 — 当关键概念没有直接记录时，用可观测代理信号推断",
                "  4. 约束 vs 偏好 — 差异来自用户不能选还是不愿选",
                "  5. 杠杆点 — 高份额 + 高改进空间的优先发力点",
                "",
                "## 质量检查清单",
                "输出前确认包含：业务问题、字段解释、数据风险检查、分析方法说明（含分层依据）、",
                "指标公式与当前计算值、业务解读、可操作建议、初学者学习笔记、思维模型教学、局限性、",
                "进阶分析推荐、跳过高阶方法及原因。",
            ])
            parts.append("\n".join(skill_parts))

            # ── 注入完整教学规范作为补充参考（紧随结构化摘要之后） ──
            if self.skill_instructions:
                parts.append(
                    "# 完整教学规范（结构化执行指令优先）\n\n"
                    f"{self.skill_instructions}"
                )
        elif self.skill_instructions:
            # fallback：没有 skill_engine 但有原始 skill 文本
            mode_note = f"输出模式: {self._mode or 'beginner_summary'}"
            output_note = f"交付物目录: {self._output_dir}" if self._output_dir else "交付物目录: 无"
            parts.append(f"# Business Analysis Skill（全文）\n\n[配置] {mode_note} | {output_note}\n\n{self.skill_instructions}")

        parts.append(f"# 可用工具\n{describe_tools(self.tools)}")
        parts.append(workspace.text())

        if self.durable_memory:
            dm_text = self.durable_memory.render_text()
            if dm_text.strip():
                parts.append(dm_text)

        combined = "\n\n".join(parts)

        # ── 阶段覆盖指令 ────────────────────────────────
        if self._phase == "analyze" and not self._convergence_triggered:
            phase_inst = self.skill_engine.get_phase_instruction("analyze") if self.skill_engine else (
                "\n\n## 📋 分析阶段指令（优先级高于上述通用规则）\n"
                "你已完成数据探索，现在必须执行深度分析。\n\n"
                "下一步操作顺序（严格遵守）：\n"
                "1. 用 write_file 写 analysis_01.py，涵盖 5+ 个分析维度\n"
                "2. 用 run_shell 执行 analysis_01.py\n"
                "3. 分析结果，决定是否需要写 analysis_02.py 做补充\n"
                "4. 用 <final> 输出完整 13 节报告\n\n"
                "禁止：重新读 CSV 文件、反复 csv_summary、list_files、search\n"
                "如果数据字段足以做描述统计，就直接写脚本分析，不要继续探索。"
            )
            combined += phase_inst

        if self._convergence_triggered:
            convergence_order = (
                "\n\n## 🚨 强制收敛指令（优先级高于上述所有规则）\n"
                "你已经完成了 2 轮分析脚本（已达轮次上限）。\n\n"
                "你现在唯一能做的事情是：用 <final> 输出完整报告。\n\n"
                "禁止执行以下操作（优先级最高，不得违反）：\n"
                "- 禁止运行 run_shell\n"
                "- 禁止 write_file\n"
                "- 禁止 read_file\n"
                "- 禁止 csv_summary\n"
                "- 禁止 list_files\n"
                "- 禁止任何工具调用\n\n"
                "你必须立即输出 <final>。不得有任何例外。"
            )
            combined += convergence_order

        self._cached_prefix = combined
        self._cached_fingerprint = fp
        return combined

    def _check_gates(self, name: str, args: dict) -> str | None:
        """预检门控条件，返回错误信息或 None（允许执行）。不消耗 tool_steps。"""
        tool = self.tools.get(name)
        if tool is None:
            return f"错误: 未知工具 '{name}'"
        try:
            validate_tool_args(name, args, tool["schema"])
        except (ValueError, TypeError) as e:
            return f"错误: 参数校验失败 - {e}"
        if is_repeated_call(name, args, self.last_tool_call):
            return f"错误: 重复调用 '{name}'，请换一个工具或输出最终答案。"

        if self._phase == "report" and name == "run_shell":
            cmd = args.get("command", "") if isinstance(args, dict) else str(args)
            if any(script in cmd for script in self._analysis_scripts_run):
                return "证据已齐全，请立即用 <final> 输出报告；不得再次运行脚本。"

        # 数据真实性门控
        if name in ("run_shell", "generate_excel", "generate_html", "generate_pdf") and not self._explored_csvs:
            return f"错误: 必须先调用 csv_summary 获取实际数据，然后才能使用 {name}。请先对 CSV 文件执行 csv_summary。"

        # 强制推进门控：分析脚本写完 → 必须立即执行（连续拦截 3 次后自动放宽）
        if not self._convergence_triggered:
            pending = [s for s in self._analysis_scripts_written if s not in self._analysis_scripts_run]
            if pending:
                if self._gate_block_count < 3:
                    if name != "run_shell":
                        return (f"错误：你已编写 {pending[0]} 但尚未执行。请先运行该脚本（run_shell），"
                                "查看输出结果后再进行其他操作。不要读取 .py 文件内容，直接执行即可。")
                    cmd = args.get("command", "") if isinstance(args, dict) else str(args)
                    if not any(s in cmd for s in pending):
                        return (f"错误：你有待执行的脚本 {pending[0]}，但当前 run_shell 命令中未包含它。"
                                f"请直接运行该脚本，如：python {pending[0]}")
                # 连续拦截 ≥3 次：放宽门控，允许 read_file / write_file 等，避免死锁
                # （但仍禁止 list_files / csv_summary 等无关工具）

        # 强制推进门控：首轮脚本已运行 → 限制 read/write 范围，防止无效循环
        if not self._convergence_triggered and len(self._analysis_scripts_run) >= 1:
            if name == "read_file":
                path = args.get("path", "")
                # 允许读 .txt 输出文件，禁止重新读 .py / .csv
                if path.endswith(".py") or ".csv" in path:
                    return "错误：首轮分析已完成，不要重新读取 .py 脚本或原始 CSV。如需补充分析，请写 analysis_02.py。"
            elif name == "write_file":
                path = args.get("path", "")
                if not re.search(r'analysis_\d+\.py$', path):
                    return "错误：此阶段只允许写 analysis_02.py 补充分析，或输出 <final> 结束。"
            elif name not in ("run_shell", "generate_excel", "generate_html", "generate_pdf"):
                # list_files / search / csv_summary 等禁止
                return ("错误：你已完成首轮分析脚本，应专注于整理报告。请：\n"
                        "1) 写 analysis_02.py 做补充分析，然后输出 <final>\n"
                        "2) 或直接输出 <final> 完整报告")

        # 分析阶段门控：禁止继续探索，必须开始写分析脚本
        if self._phase == "analyze" and not self._analysis_scripts_written:
            # 连续拦截 >= 3 次 → 放宽门控，避免死锁
            relaxed = self._gate_block_count >= 3
            if not relaxed:
                if name in ("csv_summary", "list_files", "search"):
                    return "错误：你已完成数据探索，请立即用 write_file 写 analysis_01.py 开始分析，不要继续探索数据。"
                if name == "read_file":
                    path = args.get("path", "")
                    if ".csv" in path:
                        return "错误：数据探索已完成，请用 write_file 写分析脚本分析数据，不要读了。"
                if name == "run_shell":
                    # 允许运行简单命令，但如果目的是探索数据则阻止
                    cmd = args.get("command", "") if isinstance(args, dict) else str(args)
                    if any(x in cmd for x in ("head", "cat", "wc", "less", "more")):
                        return "错误：请用 write_file 写分析脚本进行系统分析，不要运行临时查看命令。"

        # Phase 4: Tier 2 门控（analyze 阶段阻止自动执行高级方法，除非 audit_report 模式）
        if self._phase == "analyze" and self.skill_engine and not self._convergence_triggered:
            is_tier2 = self.skill_engine.is_tier2_method(name, args)
            if is_tier2:
                is_audit = self.skill_engine.config.mode == "audit_report"
                if is_audit:
                    # audit_report 模式允许执行，但需附带说明
                    pass
                elif self._gate_block_count >= 3:
                    # 连续门控后放宽限制
                    pass
                else:
                    return ("错误：检测到 Tier 2 高级分析方法（相关分析、RFM、回归、聚类等）。"
                            "在当前模式下不会自动执行高级方法。\n\n"
                            "请先完成 Tier 1 基础分析（描述性统计、分组聚合、排名分析），"
                            "或切换为 audit_report 模式以允许执行高级方法。"
                            "如需推荐这些方法，请在报告中以「进阶分析推荐」章节列出。")

        return None  # 允许执行

    def run_tool(self, name: str, args: dict) -> str:
        tool = self.tools.get(name)
        self.last_tool_call = (name, args)

        # Phase 6: 自动注入 analysis_text + mode 到交付物生成工具
        if name in ("generate_excel", "generate_html", "generate_pdf"):
            if "analysis_text" not in args and self.task_state and self.task_state.final_answer:
                args["analysis_text"] = self.task_state.final_answer
            if "mode" not in args and self.skill_engine:
                args["mode"] = self.skill_engine.config.mode

        # 审批策略
        if tool["risky"]:
            if self.approval_policy == "never":
                return f"错误: 高风险工具 '{name}' 已被审批策略禁止。"
            if self.approval_policy == "ask":
                print(f"\n  ⚠️ 高风险工具: {name}({args})")
                yn = input("  确认执行？[Y/n] ").strip().lower()
                if yn not in ("", "y", "yes"):
                    return f"错误: 用户拒绝了 '{name}' 的执行。"

        try:
            result = tool["run"](args)
        except ValueError as e:
            return f"错误: {e}"
        except Exception as e:
            return f"错误: 工具执行异常 - {e}"
        result = clip(result)
        self._update_memory_after_tool(name, args, result)

        # csv_summary 成功后注册已探索路径
        if name == "csv_summary" and "错误" not in result:
            self._explored_csvs.add(args.get("path", ""))

        # ── 分析轮次追踪 & 收敛触发 ──
        if name == "write_file" and "错误" not in result:
            path = args.get("path", "")
            if re.search(r'analysis_\d+\.py$', path) and path not in self._analysis_scripts_written:
                self._analysis_scripts_written.append(path)
                # 已写 2 个脚本仍未运行 → 直接触发收敛
                if len(self._analysis_scripts_written) >= 2 and not self._convergence_triggered:
                    self._convergence_triggered = True
                    self._cached_prefix = None
                    result += ("\n\n⚠️【收敛通知】已完成 2 轮分析脚本（已达轮次上限）。"
                               "请根据已有结果立即用 <final> 输出最终报告，不要继续执行任何工具。")
        elif name == "run_shell" and "错误" not in result and "(exit code:" not in result:
            cmd = args.get("command", "") if isinstance(args, dict) else str(args)
            for script in self._analysis_scripts_written:
                if script in cmd and script not in self._analysis_scripts_run:
                    self._analysis_scripts_run.add(script)
                    out_match = re.search(r'>\s*(/\S+|\S+\.\w+)', cmd)
                    if out_match:
                        out_path = out_match.group(1)
                        resolved_out = resolve_path(out_path, self.repo_root)
                        try:
                            out_content = Path(resolved_out).read_text(encoding="utf-8", errors="replace")
                            if len(out_content) > 0:
                                result += f"\n\n📄 {out_path} 完整内容:\n{out_content[:30000]}"
                        except (OSError, ValueError):
                            pass
                    self._convergence_triggered = True
                    self._phase = "report"
                    self._cached_prefix = None
                    candidate = Path(self.repo_root) / "analysis_evidence.md"
                    if candidate.exists():
                        try:
                            ev = candidate.read_text(encoding="utf-8", errors="replace")
                            if ev.strip():
                                result += f"\n\n📋 权威分析证据:\n{ev[:30000]}"
                        except (OSError, ValueError):
                            pass
                    result += "\n\n证据已齐全，请立即用 <final> 输出报告；不得再次运行脚本。"
                    break

        # 末尾比开头重要：优先保留自动追加的分析输出（而非原始 CLI 输出）
        if len(result) > MAX_TOOL_OUTPUT:
            result = result[-(MAX_TOOL_OUTPUT - 200):]

        return result

    def _update_memory_after_tool(self, name: str, args: dict, result: str):
        path = args.get("path", "")
        if name in ("read_file", "write_file", "patch_file"):
            try:
                resolved = resolve_path(path, self.repo_root)
            except ValueError:
                resolved = path
            self.memory.remember_file(resolved)
        if name == "read_file":
            summary = summarize_read_result(result)
            self.memory.set_file_summary(resolved, summary)
            self.memory.append_note(summary, tags=(path,), source=path)
        elif name in ("write_file", "patch_file"):
            try:
                resolved = resolve_path(path, self.repo_root)
            except ValueError:
                resolved = path
            self.memory.invalidate_file_summary(resolved)


if __name__ == "__main__":
    from datahelp.models import MockModelClient, create_model_client
    import sys
    provider = sys.argv[1] if len(sys.argv) > 1 else "mock"
    model = MockModelClient(reply='<tool>{"name": "list_files", "args": {"path": "."}}</tool>') if provider == "mock" else create_model_client(provider)
    repo = sys.argv[2] if len(sys.argv) > 2 else "."
    agent = DataHelp(model_client=model, repo_root=str(Path(repo).resolve()))
    result = agent.ask("检查一下这个项目的结构")
    print("=== 最终结果 ===")
    print(result)
    print(f"状态: {agent.task_state.status}, 步骤: {agent.task_state.tool_steps}")
