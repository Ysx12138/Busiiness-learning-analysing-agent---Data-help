"""运行工件持久化 —— task_state.json / trace.jsonl / report.json。"""

import json
import os
import time
from pathlib import Path


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


class RunStore:

    def __init__(self, base_dir: str):
        self._base_dir = Path(base_dir)

    def create_run_dir(self, run_id: str) -> str:
        run_dir = self._base_dir / "runs" / run_id
        ensure_dir(str(run_dir))
        return str(run_dir)

    def write_task_state(self, task_state) -> str:
        run_dir = self._find_run_dir(task_state.run_id)
        path = Path(run_dir) / "task_state.json"
        data = task_state.to_dict() if hasattr(task_state, "to_dict") else task_state
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def append_trace(self, run_id: str, event: dict):
        run_dir = self._find_run_dir(run_id)
        path = Path(run_dir) / "trace.jsonl"
        event["timestamp"] = time.time()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def write_report(self, run_id: str, task_state) -> str:
        run_dir = self._find_run_dir(run_id)
        path = Path(run_dir) / "report.json"
        data = task_state.to_dict() if hasattr(task_state, "to_dict") else dict(task_state)
        report = {
            "run_id": run_id,
            "status": data.get("status"),
            "stop_reason": data.get("stop_reason"),
            "tool_steps": data.get("tool_steps", 0),
            "attempts": data.get("attempts", 0),
            "final_answer": data.get("final_answer"),
            "user_request": data.get("user_request", ""),
        }
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def _find_run_dir(self, run_id: str) -> str:
        run_dir = self._base_dir / "runs" / run_id
        ensure_dir(str(run_dir))
        return str(run_dir)


if __name__ == "__main__":
    import tempfile
    from datahelp.task_state import TaskState
    with tempfile.TemporaryDirectory() as tmpdir:
        store = RunStore(tmpdir)
        ts = TaskState.create(user_request="测试")
        store.create_run_dir(ts.run_id)
        store.write_task_state(ts)
        store.append_trace(ts.run_id, {"event": "run_started"})
        ts.finish_success("完成")
        store.write_task_state(ts)
        store.write_report(ts.run_id, ts)
        print(f"Run {ts.run_id} saved to {tmpdir}")
