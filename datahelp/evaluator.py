"""评测框架 —— 批量加载测试任务、运行 agent、收集结果、生成报告。"""

import json
import time
from pathlib import Path


def load_test_set(path: str) -> list[dict]:
    tasks = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                tasks.append(json.loads(line))
    return tasks


class Evaluator:

    def __init__(self, agent_factory, test_set: list[dict] | str):
        """
        agent_factory: 无参 callable，每次调用返回一个全新的 DataHelp 实例。
        test_set: JSONL 文件路径，或已解析的任务列表。
        """
        self.agent_factory = agent_factory
        if isinstance(test_set, str):
            self.test_set = load_test_set(test_set)
        else:
            self.test_set = test_set
        self.results: list[dict] = []

    def run_all(self):
        for task in self.test_set:
            result = self._run_single(task)
            self.results.append(result)
            self._log_progress(result)

    def _run_single(self, task: dict) -> dict:
        agent = self.agent_factory()
        prompt = task.get("prompt", "")
        task_id = task.get("id", "unknown")

        start = time.time()
        try:
            answer = agent.ask(prompt)
            duration = time.time() - start
            ts = agent.task_state
            return {
                "id": task_id,
                "status": ts.status if ts else "unknown",
                "tool_steps": ts.tool_steps if ts else 0,
                "attempts": ts.attempts if ts else 0,
                "stop_reason": ts.stop_reason if ts else "",
                "duration": round(duration, 2),
                "final_answer": answer,
            }
        except Exception as e:
            duration = time.time() - start
            return {
                "id": task_id,
                "status": "error",
                "tool_steps": 0,
                "attempts": 0,
                "stop_reason": str(e),
                "duration": round(duration, 2),
                "final_answer": "",
            }

    def _log_progress(self, result: dict):
        status = "✅" if result["status"] == "completed" else "❌"
        print(f"  {status} {result['id']}  ({result['tool_steps']} steps, {result['duration']}s)")

    def report(self):
        from datahelp.metrics import compute_metrics
        metrics = compute_metrics(self.results)
        print()
        print(metrics.format())
        return metrics


def run_eval(agent_factory, test_set_path: str) -> dict:
    """便捷入口：创建 evaluator → 运行 → 返回指标。"""
    evaluator = Evaluator(agent_factory, test_set_path)
    print(f"\n  Running {len(evaluator.test_set)} tasks...\n")
    evaluator.run_all()
    metrics = evaluator.report()
    return metrics.to_dict()
