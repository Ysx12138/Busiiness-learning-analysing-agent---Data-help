"""指标聚合 —— 从评测结果计算统计量并生成报告。"""

import statistics


class MetricsReport:

    def __init__(self, results: list[dict]):
        self.total = len(results)
        self.results = results
        self._compute()

    def _compute(self):
        completed = [r for r in self.results if r["status"] == "completed"]
        self.success_count = len(completed)
        self.success_rate = self.success_count / self.total if self.total > 0 else 0.0

        if completed:
            self.avg_steps = statistics.mean(r["tool_steps"] for r in completed)
            self.avg_duration = statistics.mean(r["duration"] for r in completed)
            all_steps = [r["tool_steps"] for r in completed]
            self.min_steps = min(all_steps)
            self.max_steps = max(all_steps)
            self.total_duration = sum(r["duration"] for r in completed)
        else:
            self.avg_steps = 0.0
            self.avg_duration = 0.0
            self.min_steps = 0
            self.max_steps = 0
            self.total_duration = 0.0

        self.failures = [r for r in self.results if r["status"] != "completed"]

    def format(self) -> str:
        lines = []
        lines.append("=" * 50)
        lines.append("  Evaluator Report")
        lines.append("=" * 50)
        lines.append(f"  Total tasks  : {self.total}")
        lines.append(f"  Success      : {self.success_count}/{self.total} ({self.success_rate:.0%})")
        lines.append(f"  Avg steps    : {self.avg_steps:.1f}")
        lines.append(f"  Min steps    : {self.min_steps}")
        lines.append(f"  Max steps    : {self.max_steps}")
        lines.append(f"  Avg duration : {self.avg_duration:.1f}s")
        lines.append(f"  Total time   : {self.total_duration:.1f}s")
        if self.failures:
            lines.append("")
            lines.append("  Failures:")
            for f in self.failures:
                lines.append(f"    - {f['id']}: {f.get('stop_reason', f['status'])}")
        lines.append("=" * 50)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "success_count": self.success_count,
            "success_rate": self.success_rate,
            "avg_steps": self.avg_steps,
            "avg_duration": self.avg_duration,
            "total_duration": self.total_duration,
            "min_steps": self.min_steps,
            "max_steps": self.max_steps,
            "failures": [{"id": f["id"], "reason": f.get("stop_reason", f["status"])} for f in self.failures],
            "results": self.results,
        }


def compute_metrics(results: list[dict]) -> MetricsReport:
    return MetricsReport(results)
