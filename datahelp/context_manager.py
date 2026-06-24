"""分层 prompt 组装与预算控制。"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass, field

DEFAULT_TOTAL_BUDGET = 50000

DEFAULT_SECTION_BUDGETS = {
    "prefix": 32000,
    "memory": 3000,
    "relevant_memory": 2000,
    "history": 12000,
    "current_request": 0,
}

DEFAULT_REDUCTION_ORDER = ("relevant_memory", "history", "memory", "prefix")


def _compute_section_floors(budgets: dict[str, int]) -> dict[str, int]:
    return {s: max(20, b // 4) if b > 0 else 0 for s, b in budgets.items()}


@dataclass
class PromptMetadata:
    total_chars: int = 0
    section_chars: dict[str, int] = field(default_factory=dict)
    budget_reductions: dict[str, int] = field(default_factory=dict)


class ContextManager:

    def __init__(
        self,
        prefix_text: str,
        tool_descriptions: str = "",
        workspace_text: str = "",
        memory=None,
        total_budget: int = DEFAULT_TOTAL_BUDGET,
    ):
        self._prefix_text = prefix_text
        self._tool_descriptions = tool_descriptions
        self._workspace_text = workspace_text
        self._memory = memory
        self._total_budget = total_budget

    def build(self, user_message: str, history: list[dict] | None = None) -> tuple[str, PromptMetadata]:
        metadata = PromptMetadata()
        sections = self._build_all_sections(user_message, history or [])
        total = sum(len(t) for t in sections.values())
        metadata.section_chars = {name: len(text) for name, text in sections.items()}
        metadata.total_chars = total
        if total > self._total_budget:
            sections = self._reduce_sections(sections, metadata)
        parts = [
            sections.get("prefix", ""),
            sections.get("memory", ""),
            sections.get("relevant_memory", ""),
            sections.get("history", ""),
            sections.get("current_request", ""),
        ]
        prompt = "\n\n".join(p for p in parts if p.strip())
        return prompt, metadata

    def _build_all_sections(self, user_message: str, history: list[dict]) -> dict[str, str]:
        return {
            "prefix": self._build_prefix_section(),
            "memory": self._build_memory_section(),
            "relevant_memory": self._build_relevant_memory_section(user_message),
            "history": self._build_history_section(history),
            "current_request": self._build_current_request_section(user_message),
        }

    def _build_prefix_section(self) -> str:
        parts = ["# 系统规则", self._prefix_text.strip()]
        if self._tool_descriptions:
            parts.extend(["", "# 可用工具", self._tool_descriptions.strip()])
        if self._workspace_text:
            parts.extend(["", self._workspace_text.strip()])
        return "\n".join(parts)

    def _build_memory_section(self) -> str:
        if self._memory is None:
            return ""
        text = self._memory.render_memory_text()
        if not text.strip():
            return ""
        return f"# 记忆\n{text.strip()}"

    def _build_relevant_memory_section(self, user_message: str) -> str:
        if self._memory is None:
            return ""
        notes = self._memory.retrieval_candidates(user_message, limit=3)
        if not notes:
            return ""
        lines = ["# 相关记忆"]
        for note in notes:
            text = note.get("text", "")
            if len(text) > 300:
                text = text[:300] + "..."
            lines.append(f"  - {text}")
        return "\n".join(lines)

    def _build_history_section(self, history: list[dict]) -> str:
        if not history:
            return ""
        cleaned = self._compress_history(history)
        recent_window = 6
        recent_start = max(0, len(cleaned) - recent_window)
        lines = ["# 对话历史"]
        for i, msg in enumerate(cleaned):
            role = msg.get("role", "unknown")
            content = str(msg.get("content", ""))
            is_recent = i >= recent_start
            line_limit = 900 if is_recent else 400
            if len(content) > line_limit:
                content = content[:line_limit] + "..."
            lines.append(f"[{role}] {content.strip()}")
        return "\n".join(lines)

    def _compress_history(self, history: list[dict]) -> list[dict]:
        """压缩历史：用文件摘要替代较早的 read_file 结果，缩减体积。"""
        if not self._memory:
            return history
        recent_window = 6
        recent_start = max(0, len(history) - recent_window)
        result = list(history)
        for i, msg in enumerate(result):
            if i >= recent_start:
                break
            if msg.get("role") == "tool" and msg.get("tool_name") == "read_file":
                content = str(msg.get("content", ""))
                if len(content) < 300:
                    continue
                tool_name = msg.get("tool_name", "")
                summary = self._find_file_summary(content)
                if summary:
                    result[i] = dict(msg, content=f"[文件摘要] {summary}")
                else:
                    result[i] = dict(msg, content=content[:300] + "...[截断]")
        return result

    def _find_file_summary(self, content: str) -> str | None:
        """从 read_file 结果中提取路径，在 memory 中查找摘要。"""
        if not self._memory:
            return None
        try:
            file_summaries = self._memory.state.get("file_summaries", {})
            if not file_summaries:
                return None
            for path, info in file_summaries.items():
                if path in content[:200]:
                    return info.get("summary", "")[:200]
            first_line = content.split("\n")[0] if content else ""
            for path, info in file_summaries.items():
                if path.endswith(first_line.split()[-1]) if first_line.split() else False:
                    return info.get("summary", "")[:200]
        except Exception:
            pass
        return None

    def _build_current_request_section(self, user_message: str) -> str:
        return f"# 当前请求\n{user_message.strip()}"

    def _reduce_sections(self, sections: dict[str, str], metadata: PromptMetadata) -> dict[str, str]:
        floors = _compute_section_floors(DEFAULT_SECTION_BUDGETS)
        result = dict(sections)
        total = sum(len(t) for t in result.values())
        over = total - self._total_budget
        for section_name in DEFAULT_REDUCTION_ORDER:
            if over <= 0:
                break
            text = result.get(section_name, "")
            if not text:
                continue
            current_len = len(text)
            floor = floors.get(section_name, 20)
            target = max(floor, current_len - over)
            if target < current_len:
                result[section_name] = text[:target] + "\n...[裁剪]"
                cut = current_len - len(result[section_name])
                over -= cut
                metadata.budget_reductions[section_name] = cut
        return result


if __name__ == "__main__":
    cm = ContextManager(prefix_text="你是 DataHelp，一个数据分析 agent。", tool_descriptions="  - read_file(path): 读取文件")
    history = [{"role": "user", "content": "看看这个项目"}, {"role": "assistant", "content": "好的。"}]
    prompt, meta = cm.build("检查测试文件", history)
    print(prompt)
    print(f"\n总长度: {meta.total_chars}")
