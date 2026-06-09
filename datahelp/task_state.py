"""单次 agent 运行的状态快照。"""

import uuid
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskState:

    def __init__(self, data: dict):
        self._data = data

    @classmethod
    def create(cls, run_id: str | None = None, task_id: str | None = None, user_request: str = "") -> "TaskState":
        return cls({
            "run_id": run_id or uuid.uuid4().hex[:12],
            "task_id": task_id or uuid.uuid4().hex[:12],
            "status": "running",
            "tool_steps": 0,
            "attempts": 0,
            "last_tool": None,
            "stop_reason": None,
            "final_answer": None,
            "user_request": user_request,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        })

    @property
    def run_id(self) -> str:
        return self._data["run_id"]
    @property
    def task_id(self) -> str:
        return self._data["task_id"]
    @property
    def status(self) -> str:
        return self._data["status"]
    @property
    def tool_steps(self) -> int:
        return self._data["tool_steps"]
    @property
    def attempts(self) -> int:
        return self._data["attempts"]
    @property
    def last_tool(self) -> tuple | None:
        return self._data["last_tool"]
    @property
    def stop_reason(self) -> str | None:
        return self._data["stop_reason"]
    @property
    def final_answer(self) -> str | None:
        return self._data["final_answer"]
    @property
    def is_finished(self) -> bool:
        return self._data["status"] in ("completed", "stopped", "failed")
    @property
    def data(self) -> dict:
        return self._data

    def record_attempt(self):
        self._data["attempts"] += 1
        self._data["updated_at"] = _now_iso()

    def record_tool(self, name: str, args: dict | None = None):
        self._data["tool_steps"] += 1
        self._data["last_tool"] = (name, args or {})
        self._data["updated_at"] = _now_iso()

    def finish_success(self, final_answer: str):
        self._data["status"] = "completed"
        self._data["stop_reason"] = "final_answer_returned"
        self._data["final_answer"] = final_answer
        self._data["updated_at"] = _now_iso()

    def finish_stopped(self, reason: str = "stopped"):
        self._data["status"] = "stopped"
        self._data["stop_reason"] = reason
        self._data["updated_at"] = _now_iso()

    def finish_failed(self, reason: str = "error"):
        self._data["status"] = "failed"
        self._data["stop_reason"] = reason
        self._data["updated_at"] = _now_iso()

    def to_dict(self) -> dict:
        return dict(self._data)

    def __repr__(self) -> str:
        return f"TaskState(run={self.run_id}, status={self.status}, steps={self.tool_steps})"


if __name__ == "__main__":
    ts = TaskState.create(user_request="检查测试失败原因")
    ts.record_attempt()
    ts.record_tool("read_file", {"path": "src/main.py"})
    ts.finish_success("找到了")
    print(ts)
