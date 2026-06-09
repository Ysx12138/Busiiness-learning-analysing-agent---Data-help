"""agent 主循环 —— 感知、决策、行动、记录。"""

import json
import re
import textwrap
from pathlib import Path

from datahelp.context_manager import ContextManager
from datahelp.memory import LayeredMemory, DurableMemoryStore, summarize_read_result
from datahelp.skill_loader import load_skill
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
MAX_ATTEMPTS = 30
MAX_TOOL_OUTPUT = 12000


def clip(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[截断 {len(text) - limit} 字符]"


def parse_model_output(raw: str) -> tuple[str, dict | str | None]:
    text = raw.strip()
    if not text:
        return "retry", {"message": "模型返回空内容，请重试。"}

    tool_match = re.search(r"<tool>(.*?)</tool>", text, re.DOTALL)
    if tool_match:
        try:
            payload = json.loads(tool_match.group(1).strip())
            return "tool", payload
        except json.JSONDecodeError:
            pass

    xml_match = re.search(r'<tool\s+name=["\'](\w+)["\']>(.*?)</tool>', text, re.DOTALL)
    if xml_match:
        name = xml_match.group(1)
        body = xml_match.group(2).strip()
        return "tool", {"name": name, "args": _parse_xml_args(body)}

    final_match = re.search(r"<final>(.*?)</final>", text, re.DOTALL)
    if final_match:
        return "final", final_match.group(1).strip()

    if len(text) > 10:
        # 安全检查：如果文本中包含工具调用关键词但未正确使用 <tool> 标签，拒绝当作最终答案
        if '"name"' in text and '"args"' in text:
            return "retry", {"message": "输出中包含 JSON 格式的工具调用标记，但没有被 <tool> 标签包裹。请使用 <tool>{\"name\": \"xxx\", \"args\": {...}}</tool> 格式。"}
        # 没有 <final> 标签的长文本不是有效最终答案，要求使用 <final> 或 <tool> 格式
        return "retry", {"message": "请使用 <tool> 调用工具，或用 <final> 输出最终报告。不要输出纯文字段落。"}

    return "retry", {"message": "输出太短或格式不正确，请使用 <tool> 或 <final> 格式。"}


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

    **Step 2 — 迭代深挖**
    查看脚本输出后，如果发现值得深挖的方向，写第二个脚本继续分析。
    每次写一个新文件（analysis_02.py、analysis_03.py...）。

    **Step 3 — 编译报告**
    汇总所有分析脚本的输出，输出完整的 12 节 <final> 报告。
    教学规则和输出结构见下方 Business Analysis Skill（全文）。

    **Step 4 — 生成交付物**（如果配置了 output-dir）
    调用 generate_excel / generate_html / generate_pdf 生成文件。

    ## 数据真实性规则（必须遵守）

    本系统严禁任何形式的数据编造。

    1. **第一步必须调用 csv_summary**：获取实际行数、列数、列名后才能进行后续分析。系统会阻止你在未执行 csv_summary 的情况下使用 run_shell。
    2. **脚本必须读取真实数据**：必须使用 pd.read_csv("文件名.csv") 读取原始 CSV 文件。绝对不得在代码中构造模拟数据（如 data = [{"col1": ...}]）或使用 random 生成数据。
    3. **所有数值必须可追溯**：报告中的每个均值、合计、百分比、排名，都必须来自工具实际返回的结果。任何数值不是来自工具执行结果的，不得写入报告。
    4. **如果 csv_summary 返回错误**：说明文件不存在或路径错误，先修正路径，不要编造数据代替。

    ## 工具使用规则

    - 分析脚本尽量写在一个 .py 文件里一次性执行，不要拆成多个小工具调用
    - 每次执行之间输出简短的中间分析，帮助用户理解
    - 至少迭代 2-3 轮分析脚本，覆盖 5+ 个分析维度后再输出最终报告
    - 不要重复调用参数完全相同的工具

    ## 格式规范

    - 工具调用：<tool>{"name": "xxx", "args": {...}}</tool>
    - 中间分析：在工具调用之间输出简短的分析说明
    - 最终报告：<final>完整的分析报告</final>

    ## 最终报告结构（必须遵守）

    最终报告必须包含以下 12 个部分，缺一不可：

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

    在跑完充分的迭代分析（至少 2-3 轮脚本、覆盖 5+ 个分析维度）后，用 <final> 输出完整的 12 节报告。不要提前输出 <final>。
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
        self._explored_csvs: set[str] = set()

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
            attempts = checkpoint["attempts"]
            tool_steps = checkpoint["tool_steps"]
        else:
            self.memory.set_task_summary(user_message)
            self.history.append({"role": "user", "content": user_message})
            self.task_state = TaskState.create(user_request=user_message)
            self._explored_csvs = set()
            attempts = 0
            tool_steps = 0

        while tool_steps < self.max_steps and attempts < MAX_ATTEMPTS:
            attempts += 1
            self.task_state.record_attempt()

            prompt = self._build_prompt(user_message)
            raw = self.model.complete(prompt, max_tokens=self.max_new_tokens)
            kind, payload = parse_model_output(raw)

            if kind == "tool":
                name = payload.get("name", "") if isinstance(payload, dict) else ""
                args = payload.get("args", {}) if isinstance(payload, dict) else {}
                result = self.run_tool(name, args)
                self.task_state.record_tool(name, args)
                tool_steps = self.task_state.tool_steps
                self.history.append({"role": "assistant", "content": f"调用工具: {name}"})
                self.history.append({"role": "tool", "content": clip(result), "tool_name": name})
                self._save_checkpoint()
                continue

            if kind == "retry":
                retry_msg = payload.get("message", "请重试。") if isinstance(payload, dict) else str(payload)
                self.history.append({"role": "assistant", "content": f"[retry] {retry_msg}"})
                continue

            final_answer = str(payload or raw)
            self.task_state.finish_success(final_answer)
            self.history.append({"role": "assistant", "content": final_answer})
            self._remove_checkpoint()
            return final_answer

        self.task_state.finish_stopped("step_limit_reached")
        self._remove_checkpoint()
        return f"<final>已达到最大步骤限制 ({self.max_steps})，任务可能未完成。</final>"

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
        if self.skill_instructions:
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

        self._cached_prefix = combined
        self._cached_fingerprint = fp
        return combined

    def run_tool(self, name: str, args: dict) -> str:
        tool = self.tools.get(name)
        if tool is None:
            return f"错误: 未知工具 '{name}'"
        try:
            validate_tool_args(name, args, tool["schema"])
        except (ValueError, TypeError) as e:
            return f"错误: 参数校验失败 - {e}"
        if is_repeated_call(name, args, self.last_tool_call):
            return f"错误: 重复调用 '{name}'，请换一个工具或输出最终答案。"
        self.last_tool_call = (name, args)

        # 数据真实性门控：run_shell / 生成类工具必须先执行 csv_summary
        if name in ("run_shell", "generate_excel", "generate_html", "generate_pdf") and not self._explored_csvs:
            return f"错误: 必须先调用 csv_summary 获取实际数据，然后才能使用 {name}。请先对 CSV 文件执行 csv_summary。"

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
