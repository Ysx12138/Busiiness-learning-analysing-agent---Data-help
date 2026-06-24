"""分层的会话记忆 —— task_summary, recent_files, file_summaries, episodic_notes, durable。"""
from __future__ import annotations

import re
import time
from pathlib import Path

WORKING_FILE_LIMIT = 10
NOTE_RECALL_LIMIT = 3


DURABLE_TOPICS = {
    "project-conventions": {"title": "Project Conventions", "tags": ["convention"]},
    "key-decisions": {"title": "Key Decisions", "tags": ["decision"]},
    "dependency-facts": {"title": "Dependency Facts", "tags": ["dependency"]},
    "user-preferences": {"title": "User Preferences", "tags": ["preference"]},
}


def _file_freshness(path: str) -> str:
    try:
        stat = Path(path).stat()
        return f"{stat.st_size}-{stat.st_mtime_ns}"
    except OSError:
        return ""


def _summarize(text: str, max_chars: int = 200) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0] + "..."


def summarize_read_result(result: str) -> str:
    lines = result.split("\n")
    summary_parts = []
    if lines:
        summary_parts.append(lines[0])
    content_lines = [l for l in lines[1:] if l.strip()][:5]
    if content_lines:
        summary_parts.extend(content_lines)
    return _summarize("\n".join(summary_parts), max_chars=300)


def default_memory_state() -> dict:
    return {
        "working": {"task_summary": "", "recent_files": []},
        "episodic_notes": [],
        "file_summaries": {},
        "task": "", "files": [], "notes": [], "next_note_index": 0,
    }


class LayeredMemory:

    def __init__(self, state: dict | None = None):
        self._state = state or default_memory_state()

    @property
    def state(self) -> dict:
        return self._state

    def set_task_summary(self, summary: str):
        self._state["working"]["task_summary"] = _summarize(summary, 120)

    def remember_file(self, path: str):
        files = self._state["working"]["recent_files"]
        files = [f for f in files if f != path]
        files.append(path)
        self._state["working"]["recent_files"] = files[-WORKING_FILE_LIMIT:]

    def set_file_summary(self, path: str, summary: str):
        self._state["file_summaries"][path] = {
            "summary": summary,
            "created_at": time.time(),
            "freshness": _file_freshness(path),
        }

    def invalidate_file_summary(self, path: str):
        if path in self._state["file_summaries"]:
            del self._state["file_summaries"][path]

    def append_note(self, text: str, tags: tuple[str, ...] = (), source: str = ""):
        self._state["episodic_notes"].append({
            "text": text,
            "tags": tags,
            "source": source,
            "created_at": time.time(),
        })

    def retrieval_candidates(self, query: str, limit: int = NOTE_RECALL_LIMIT):
        query_lower = query.lower()
        query_tokens = set(query_lower.split())
        scored = []
        for note in self._state["episodic_notes"]:
            text = note.get("text", "")
            tags = note.get("tags", ())
            score = 0
            if any(t.lower() in query_lower for t in tags):
                score += 10
            note_tokens = set(text.lower().split())
            score += len(query_tokens & note_tokens)
            score += 0.001 * note.get("created_at", 0)
            scored.append((score, note))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [note for _, note in scored[:limit]]

    def render_memory_text(self, workspace_root: str = "") -> str:
        lines = []
        w = self._state["working"]
        if w.get("task_summary"):
            lines.append(f"## 当前任务\n{w['task_summary']}\n")
        if w.get("recent_files"):
            lines.append("## 最近文件")
            for f in w["recent_files"]:
                lines.append(f"  - {f}")
            lines.append("")
        fresh_summaries = []
        for path, info in self._state["file_summaries"].items():
            freshness = _file_freshness(path) if workspace_root else info.get("freshness", "")
            if freshness and freshness == info.get("freshness"):
                fresh_summaries.append((path, info["summary"]))
        if fresh_summaries:
            lines.append("## 文件摘要")
            for path, summary in fresh_summaries:
                lines.append(f"  - {path}: {_summarize(summary, 100)}")
            lines.append("")
        note_count = len(self._state["episodic_notes"])
        if note_count:
            lines.append(f"## 笔记 ({note_count} 条)")
        return "\n".join(lines)


# ── DurableMemoryStore ──────────────────────────────

class DurableMemoryStore:
    """跨会话持久化记忆，存放在 .datahelp/memory/ 目录下。

    接受 promotion gate 筛选 —— 不是所有信息都值得长期记住。
    只有项目约定、关键决策、依赖事实、用户偏好这类稳定信息才进入。
    """

    def __init__(self, root: str):
        self.root = Path(root)
        self.index_path = self.root / "MEMORY.md"
        self.topics_dir = self.root / "topics"

    def _load_index(self) -> list[dict]:
        if not self.index_path.exists():
            return []
        topics = []
        current = None
        for line in self.index_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            m = re.match(r"- \[([^\]]+)\]\([^)]+\):\s*(.+)", line)
            if m:
                current = {"topic": m.group(1).strip(), "title": m.group(2).strip(), "tags": []}
                topics.append(current)
            elif current and line.startswith("- tags:"):
                current["tags"] = [t.strip() for t in line.split(":", 1)[1].split(",") if t.strip()]
        return topics

    def _write_index(self, topics: list[dict]):
        self.root.mkdir(parents=True, exist_ok=True)
        self.topics_dir.mkdir(parents=True, exist_ok=True)
        lines = ["# Durable Memory Index", ""]
        for t in topics:
            lines.append(f"- [{t['topic']}](topics/{t['topic']}.md): {t.get('title', t['topic'])}")
            lines.append(f"  - tags: {', '.join(t['tags'])}")
        self.index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _add_topic(self, slug: str, title: str, tags: list[str]):
        topics = self._load_index()
        if not any(t["topic"] == slug for t in topics):
            topics.append({"topic": slug, "title": title, "tags": tags})
            self._write_index(topics)
        # 创建空白 topic 文件
        topic_path = self.topics_dir / f"{slug}.md"
        if not topic_path.exists():
            meta = {"title": title, "tags": tags}
            lines = [
                f"# {title}",
                "",
                f"- topic: {slug}",
                f"- tags: {', '.join(tags)}",
                "",
                "## Notes",
                "",
            ]
            topic_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _ensure_default_topics(self):
        for slug, meta in DURABLE_TOPICS.items():
            self._add_topic(slug, meta["title"], meta["tags"])

    def load_topic_notes(self, topic: str) -> list[dict]:
        path = self.topics_dir / f"{topic}.md"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        notes = []
        capture = False
        for line in lines:
            if line.strip() == "## Notes":
                capture = True
                continue
            if capture and line.strip().startswith("- "):
                notes.append({
                    "text": line.strip()[2:],
                    "source": topic,
                    "kind": "durable",
                })
        return notes

    def append_note(self, topic: str, text: str):
        """在指定 topic 下追加一条笔记。"""
        self._ensure_default_topics()
        path = self.topics_dir / f"{topic}.md"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"- {text}\n")

    def promote(self, text: str, tags: tuple[str, ...] | None = None, source: str = ""):
        """推广门 —— 决定一条信息是否值得写入长期记忆。

        当前规则：只要信息看起来像"事实陈述"就暂存到 key-decisions。
        后续可以加更严格的筛选逻辑。
        """
        self._ensure_default_topics()
        topic = "key-decisions"
        if source:
            text = f"[{source}] {text}"
        self.append_note(topic, text)

    def retrieval_candidates(self, query: str, limit: int = 2) -> list[dict]:
        """跨所有 topic 搜索相关笔记。"""
        query_lower = query.lower()
        query_tokens = set(query_lower.split())
        scored = []
        for topic in self._load_index():
            for note in self.load_topic_notes(topic["topic"]):
                text = note.get("text", "")
                note_tokens = set(text.lower().split())
                topic_tags = {t.lower() for t in topic.get("tags", [])}
                tag_match = int(bool(query_tokens & topic_tags))
                overlap = len(query_tokens & note_tokens)
                score = tag_match * 10 + overlap
                if score > 0:
                    scored.append((score, note))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [note for _, note in scored[:limit]]

    def render_text(self) -> str:
        """渲染为模型可见的文本。"""
        lines = ["## 长期记忆"]
        for topic in self._load_index():
            notes = self.load_topic_notes(topic["topic"])
            if notes:
                lines.append(f"\n### {topic.get('title', topic['topic'])}")
                for note in notes:
                    lines.append(f"  - {note['text']}")
        return "\n".join(lines)


if __name__ == "__main__":
    mem = LayeredMemory()
    mem.set_task_summary("检查测试失败原因")
    print("task_summary:", mem.state["working"]["task_summary"])
    mem.remember_file("src/main.py")
    mem.remember_file("tests/test_main.py")
    print("recent_files:", mem.state["working"]["recent_files"])
    mem.set_file_summary("/tmp/test.py", "Test file summary")
    mem.append_note("Config 类需要添加超时参数", tags=("config.py",), source="config.py")
    results = mem.retrieval_candidates("config")
    for r in results:
        print(f"  recall: {r['text']}")
