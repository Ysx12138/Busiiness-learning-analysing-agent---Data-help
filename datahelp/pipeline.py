"""数据分析流水线 —— 统一分析入口，复用现有 agent 流程。"""
from __future__ import annotations

# 提供 `run_data_help_analysis()` 函数，供：
# - 手动模式：用户选择文件后调用
# - watch 模式：监听器检测到新文件后调用
#
# 不修改任何现有的 skill 输出逻辑。
#
# V2 确定性分析引擎：pipeline 默认优先运行，生成可审计的结构化证据；
# 仅在引擎抛出异常时回退到 legacy agent 流程。

import json
import os
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from datahelp.config import load_project_env
from datahelp.models import create_model_client
from datahelp.analysis_contract import AnalysisResult


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def task_output_dir_name(input_filename: str) -> str:
    """生成任务输出目录名：原文件名_YYYYMMDD_HHMMSS"""
    stem = Path(input_filename).stem
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{stem}_{ts}"


def ensure_dir(path: str) -> Path:
    p = Path(path).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _result_has_read_error(result: AnalysisResult) -> bool:
    return any(ev.metric_name == "read_error" for ev in result.all_evidence())


def _result_is_reportable(result: AnalysisResult) -> bool:
    return result.row_count > 0 and result.column_count > 0 and not _result_has_read_error(result)


def _build_result_manifest(result: dict, task_dir: Path) -> dict:
    """Create a reader-facing artifact manifest for one analysis run."""
    generated = []
    for name in result.get("generated_files", []):
        path = task_dir / name
        generated.append({
            "name": name,
            "path": str(path),
            "exists": path.exists(),
            "size_bytes": path.stat().st_size if path.exists() else 0,
            "role": _file_role(name),
        })

    recommended = []
    for role in ("report", "html", "excel", "pdf", "evidence_md"):
        match = next((f for f in generated if f["role"] == role and f["exists"]), None)
        if match:
            recommended.append(match)

    warnings = _public_warnings_for_result(result)
    if not warnings and result.get("evidence_error"):
        warnings.append(result["evidence_error"])

    return {
        "title": "DataHelp analysis result",
        "status": result.get("status", "unknown"),
        "input_file": result.get("input_file", ""),
        "output_dir": str(task_dir),
        "agent_path": result.get("agent_path", ""),
        "evidence_status": result.get("evidence_status", ""),
        "report_quality": result.get("report_quality", {}),
        "warnings": list(dict.fromkeys(warnings)),
        "recommended_files": recommended,
        "files": generated,
        "next_steps": _next_steps_for_result(result, recommended),
    }


def _file_role(name: str) -> str:
    if name == "analysis_report.md":
        return "report"
    if name == "analysis_evidence.md":
        return "evidence_md"
    if name == "analysis_evidence.json":
        return "evidence_json"
    if name.endswith("_report.html"):
        return "html"
    if name.endswith("_analysis.xlsx"):
        return "excel"
    if name.endswith("_report.pdf"):
        return "pdf"
    if name == "run_log.json":
        return "run_log"
    if name == "task_state.json":
        return "agent_state"
    if name == "history.json":
        return "agent_history"
    return "source_or_support"


def _public_warnings_for_result(result: dict) -> list[str]:
    """Convert internal report diagnostics into user-facing risk notes."""
    quality = result.get("report_quality", {}) or {}
    status = quality.get("status", "")
    raw_warnings = quality.get("warnings", []) or []

    public: list[str] = []
    if result.get("evidence_status") == "fallback_success":
        public.append("Agent 未完整产出证据链，本次已使用确定性证据工具兜底。")
    if status == "degraded":
        public.append("报告生成经过降级兜底，建议优先核对 analysis_evidence.md 中的数字来源。")
    if status == "failed":
        public.append("本次未能生成可信分析报告，请先修正输入数据后重试。")
    for warning in raw_warnings:
        if "LLM 报告编排异常" in warning:
            public.append("模型报告编排异常，系统已保留证据文件供人工检查。")
        elif "数据读取失败" in warning or "数据读取失败或为空" in warning:
            public.append("数据读取失败或为空，不能生成正常交付物。")
    return list(dict.fromkeys(public))


def _next_steps_for_result(result: dict, recommended: list[dict]) -> list[str]:
    if result.get("status") != "completed":
        return [
            "检查输入文件是否为空、编码是否正常、格式是否为 CSV/XLS/XLSX。",
            "修正数据文件后重新运行 datahelp analyze。",
            "如仍失败，查看 analysis_evidence.md 和 run_log.json 中的错误原因。",
        ]
    steps = ["先打开 analysis_report.md 阅读完整结论。"]
    if any(f["role"] == "html" for f in recommended):
        steps.append("需要展示时打开 HTML 报告。")
    if any(f["role"] == "excel" for f in recommended):
        steps.append("需要检查数据和统计过程时打开 Excel 文件。")
    if result.get("evidence_status") == "fallback_success":
        steps.append("本次使用了确定性证据兜底，建议重点查看证据文件确认数字来源。")
    return steps


def _write_result_summary(manifest: dict, task_dir: Path) -> Path:
    """Write a beginner-facing Markdown summary next to run artifacts."""
    lines = [
        "# DataHelp 结果摘要",
        "",
        f"- 状态: {manifest['status']}",
        f"- 输入文件: {manifest['input_file']}",
        f"- 输出目录: {manifest['output_dir']}",
        f"- Agent 路径: {manifest.get('agent_path') or '未记录'}",
        f"- 证据状态: {manifest.get('evidence_status') or '未记录'}",
        "",
        "## 推荐先看",
    ]
    if manifest["recommended_files"]:
        for f in manifest["recommended_files"]:
            lines.append(f"- {f['name']} ({f['role']})")
    else:
        lines.append("- 暂无可推荐文件，请查看 run_log.json。")

    if manifest["warnings"]:
        lines.extend(["", "## 风险提示"])
        for warning in manifest["warnings"]:
            lines.append(f"- {warning}")

    lines.extend(["", "## 下一步"])
    for step in manifest["next_steps"]:
        lines.append(f"- {step}")

    lines.extend(["", "## 全部文件"])
    for f in manifest["files"]:
        status = "存在" if f["exists"] else "缺失"
        lines.append(f"- {f['name']} | {f['role']} | {status} | {f['size_bytes']} bytes")

    path = task_dir / "RESULT_SUMMARY.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def try_convert_excel_to_csv(excel_path: Path, output_dir: Path) -> Path | None:
    """尝试将 Excel 文件转换为 CSV，使用可用的 Python 库。

    按优先级尝试：pandas → openpyxl → xlrd → 返回 None
    这是 pipeline 预处理步骤，不修改 skill 逻辑。
    """
    # 尝试用 pandas（大概率已安装）
    try:
        import subprocess
        script = f"""import pandas as pd
df = pd.read_excel("{excel_path}")
csv_path = "{output_dir / (excel_path.stem + '.csv')}"
df.to_csv(csv_path, index=False)
print(csv_path)
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            csv_line = result.stdout.strip().split("\n")[-1]
            csv_path = Path(csv_line)
            if csv_path.exists():
                return csv_path
    except Exception:
        pass

    # 尝试用纯 Python 实现（只支持 .xlsx）
    try:
        import zipfile
        import xml.etree.ElementTree as ET

        if excel_path.suffix.lower() != ".xlsx":
            return None

        csv_path = output_dir / (excel_path.stem + ".csv")
        rows = []

        with zipfile.ZipFile(excel_path) as z:
            # 找到共享字符串表
            shared_strings = {}
            if "xl/sharedStrings.xml" in z.namelist():
                ss_tree = ET.parse(z.open("xl/sharedStrings.xml"))
                ss_root = ss_tree.getroot()
                ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                for i, si in enumerate(ss_root.findall(".//s:si", ns)):
                    texts = si.findall(".//s:t", ns)
                    shared_strings[i] = "".join(t.text or "" for t in texts)

            # 找到第一个工作表
            if "xl/workbook.xml" in z.namelist():
                wb_tree = ET.parse(z.open("xl/workbook.xml"))
                wb_root = wb_tree.getroot()
                ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                sheets = wb_root.findall(".//s:sheet", ns)
                if not sheets:
                    return None
                first_sheet_id = sheets[0].get("sheetId")

            # 找到对应的 sheet XML
            for name in z.namelist():
                if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
                    sheet_tree = ET.parse(z.open(name))
                    sheet_root = sheet_tree.getroot()
                    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

                    for row in sheet_root.findall(".//s:row", ns):
                        row_data = []
                        for c in row.findall(".s:c", ns):
                            cell_type = c.get("t", "")
                            cell_value = c.find("s:v", ns)
                            val = cell_value.text if cell_value is not None else ""
                            if cell_type == "s" and val:
                                val = shared_strings.get(int(val), val)
                            row_data.append(val)
                        rows.append(row_data)
                    break  # 只处理第一个 sheet

        if rows:
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                import csv
                writer = csv.writer(f)
                writer.writerows(rows)
            return csv_path
    except Exception:
        pass

    return None


def run_data_help_analysis(
    input_file: str,
    output_dir: str,
    provider: str = "deepseek",
    model: str | None = None,
    mode: str = "beginner_summary",
    max_steps: int = 20,
    max_new_tokens: int = 4096,
) -> dict:
    """统一分析入口 —— 对输入文件执行完整分析流程。

    这个函数会被两种模式调用：
    - 手动模式：用户在 CLI 中选择文件后直接调用
    - watch 模式：监听器检测到新文件后调用

    参数：
        input_file: 输入数据集路径（CSV 或 Excel）
        output_dir: 交付产物输出根目录
        provider: 模型提供商（deepseek / openai / anthropic / ollama）
        model: 模型名称，None 则使用默认
        mode: 输出模式（beginner_summary / standard_report / audit_report）
        max_steps: 最大工具步数
        max_new_tokens: 最大输出 token 数

    返回：
        dict: {"status": str, "output_dir": str, "report": str, ...}
    """
    started_at = time.time()
    input_path = Path(input_file).resolve()
    output_root = ensure_dir(output_dir)

    if not input_path.exists():
        return {"status": "failed", "error": f"文件不存在: {input_path}"}

    # 检查文件格式
    supported = {".csv", ".xlsx", ".xls"}
    if input_path.suffix.lower() not in supported:
        return {
            "status": "failed",
            "error": f"不支持的文件格式: {input_path.suffix}，支持: {', '.join(supported)}",
        }

    # 创建任务输出目录
    task_dir_name = task_output_dir_name(input_path.name)
    task_dir = output_root / task_dir_name
    task_dir.mkdir(parents=True, exist_ok=True)

    result = {
        "status": "running",
        "input_file": str(input_path),
        "output_dir": str(task_dir),
        "start_time": now_iso(),
        "end_time": "",
        "generated_files": [],
        "error_message": "",
        "final_answer": "",
    }

    try:
        # ── 预处理：处理数据文件 ──
        work_dir = task_dir  # 以任务目录为工作目录
        if input_path.suffix.lower() == ".csv":
            work_csv = shutil.copy2(str(input_path), str(work_dir / input_path.name))
        else:
            # Excel → CSV 转换（预处理，不修改 skill）
            csv_path = try_convert_excel_to_csv(input_path, work_dir)
            if csv_path and csv_path.exists():
                work_csv = str(csv_path)
            else:
                # 转换失败，把原文件复制过去，让 agent 尝试处理
                work_csv = str(shutil.copy2(str(input_path), str(work_dir / input_path.name)))

        result["working_file"] = work_csv

        # ── 加载环境变量 ──
        load_project_env(str(work_dir))

        # ── Agent-first 路径：模型负责决策，高层工具负责稳定执行 ──
        from datahelp.cli import build_agent

        class _Args:
            def __init__(self):
                self.provider = provider
                self.model = model
                self.cwd = str(work_dir)
                self.output_dir = str(task_dir)
                self.max_steps = max_steps
                self.max_new_tokens = max_new_tokens
                self.approval = "auto"
                self.temperature = None
                self.mode = mode
                self.eval = None  # eval 模式不需要在 pipeline 支持

        args = _Args()
        agent, run_store = build_agent(args)

        user_message = (
            f"请以产品级 Agent 工作流分析数据集 {Path(work_csv).name}。"
            f"目标：为商科初学者生成可信、可学习、可交付的数据分析报告。"
            f"请优先调用 inspect_dataset、suggest_analysis_plan、run_analysis_evidence、"
            f"validate_evidence、compose_report_from_evidence。证据输出目录请使用当前工作目录 '.'。"
        )
        print(f"\n  📊 开始 Agent 分析: {Path(input_file).name}")
        print(f"  📁 工作目录: {work_dir}")
        print(f"  🤖 模型: {agent.model.model_name}\n")

        final_answer = agent.ask(user_message)
        _model_name = agent.model.model_name
        result["agent_path"] = "agent_first"

        evidence_json = task_dir / "analysis_evidence.json"
        evidence_md = task_dir / "analysis_evidence.md"
        if evidence_json.exists() and evidence_md.exists():
            result["evidence_status"] = "success"
            result["evidence_files"] = ["analysis_evidence.json", "analysis_evidence.md"]
            result["evidence_error"] = ""
            result["report_quality"] = {
                "status": "agent_generated",
                "attempts": getattr(agent.task_state, "attempts", 0) if agent.task_state else 0,
                "warnings": [],
            }
            for fname in result["evidence_files"]:
                if fname not in result["generated_files"]:
                    result["generated_files"].append(fname)
        else:
            # Product fallback: preserve user value but mark that the Agent failed
            # to complete the evidence loop by itself.
            try:
                from datahelp.analysis_engine import DeterministicAnalysisEngine
                from datahelp.report_orchestrator import ReportOrchestrator, build_evidence_report

                engine_result = DeterministicAnalysisEngine.run(work_csv, str(task_dir))
                warnings = ["Agent 未产出证据文件，已使用确定性证据工具兜底。"]
                if not _result_is_reportable(engine_result):
                    final_answer = build_evidence_report(engine_result)
                    quality_status = "failed"
                    attempts = 0
                    warnings.append("数据读取失败或为空，已停止正常报告生成。")
                    _model_name = "failed"
                    result["agent_path"] = "failed_evidence"
                    result["evidence_status"] = "failed"
                    result["evidence_error"] = "数据读取失败或为空，不能生成可信分析报告。"
                else:
                    try:
                        client = create_model_client(provider=provider, model=model)
                        outcome = ReportOrchestrator(client, engine_result, mode).run()
                        final_answer = outcome.text
                        quality_status = outcome.quality_status
                        attempts = outcome.attempts
                        warnings.extend(outcome.warnings)
                        _model_name = client.model_name
                    except Exception as e:
                        final_answer = build_evidence_report(engine_result)
                        quality_status = "degraded"
                        attempts = 0
                        warnings.append(f"LLM 报告编排异常: {e}")
                        _model_name = "degraded"
                    result["agent_path"] = "fallback_deterministic"
                    result["evidence_status"] = "fallback_success"
                    result["evidence_error"] = "Agent 未产出证据文件，已使用确定性证据工具兜底。"
                result["evidence_files"] = ["analysis_evidence.json", "analysis_evidence.md"]
                result["report_quality"] = {
                    "status": quality_status,
                    "attempts": attempts,
                    "warnings": warnings,
                }
                for fname in result["evidence_files"]:
                    if (task_dir / fname).exists() and fname not in result["generated_files"]:
                        result["generated_files"].append(fname)
            except Exception as e:
                result["evidence_status"] = "failed"
                result["evidence_files"] = []
                result["evidence_error"] = str(e)
                result["report_quality"] = {
                    "status": "failed",
                    "attempts": 0,
                    "warnings": [str(e)],
                }

        # ── 保存报告（共用 final_answer） ──
        report_path = task_dir / "analysis_report.md"
        report_content = f"""# 数据分析报告

## 基本信息

- **数据集**: {input_path.name}
- **分析时间**: {now_iso()}
- **模型**: {_model_name}
- **模式**: {mode}

---

## 分析结果

{final_answer}
"""
        report_path.write_text(report_content, encoding="utf-8")
        result["generated_files"].append("analysis_report.md")

        # ── 保存 task_state（仅 agent 路径） ──
        if agent is not None and agent.task_state:
            ts_path = task_dir / "task_state.json"
            ts_path.write_text(
                json.dumps(agent.task_state.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            result["generated_files"].append("task_state.json")

        # ── 保存 history（仅 agent 路径） ──
        if agent is not None:
            history_path = task_dir / "history.json"
            history_path.write_text(
                json.dumps(agent.history, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            result["generated_files"].append("history.json")

        # ── 生成交付物（Excel / HTML / PDF）──
        if result.get("evidence_status") != "failed":
            try:
                from datahelp.tools_data import generate_excel, generate_html, generate_pdf
                csv_for_deliverable = work_csv if input_path.suffix.lower() != ".csv" else work_csv
                if csv_for_deliverable and Path(csv_for_deliverable).exists():
                    excel_result = generate_excel(csv_for_deliverable, str(task_dir), str(task_dir), analysis_text=final_answer, mode=mode)
                    html_result = generate_html(csv_for_deliverable, str(task_dir), str(task_dir), analysis_text=final_answer, mode=mode)
                    pdf_result = generate_pdf(csv_for_deliverable, str(task_dir), str(task_dir), analysis_text=final_answer, mode=mode)
                    print(f"\n  {excel_result}")
                    print(f"  {html_result}")
                    print(f"  {pdf_result}")
                    result["generated_files"].extend([
                        f"{input_path.stem}_analysis.xlsx",
                        f"{input_path.stem}_report.html",
                        f"{input_path.stem}_report.pdf",
                    ])
            except Exception as e:
                print(f"  ⚠️ 交付物生成失败: {e}")

        # ── 收集 agent 在运行时生成的其他文件 ──
        for f in task_dir.iterdir():
            if f.is_file() and f.name not in ("analysis_report.md", "task_state.json", "history.json", "run_log.json", "error_log.txt"):
                if f.name not in result["generated_files"]:
                    result["generated_files"].append(f.name)

        # ── 清理临时工作文件 ──
        working_path = Path(work_csv)
        if working_path.exists() and working_path.parent == task_dir:
            pass  # 保留在工作目录中

        # ── 更新结果 ──
        result["status"] = "failed" if result.get("evidence_status") == "failed" else "completed"
        result["end_time"] = now_iso()
        result["final_answer"] = final_answer

    except Exception as e:
        result["status"] = "failed"
        result["end_time"] = now_iso()
        result["error_message"] = str(e)

        # 写入错误报告
        error_path = task_dir / "error_log.txt"
        error_path.write_text(
            f"# 错误报告\n\n时间: {now_iso()}\n文件: {input_path}\n错误: {e}\n",
            encoding="utf-8",
        )

    # ── 写入交付清单和用户摘要 ──
    manifest = _build_result_manifest(result, task_dir)
    manifest_path = task_dir / "result_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    summary_path = _write_result_summary(manifest, task_dir)
    for fname in (manifest_path.name, summary_path.name):
        if fname not in result["generated_files"]:
            result["generated_files"].append(fname)

    # ── 写入 run_log.json ──
    duration = time.time() - started_at
    result["duration_seconds"] = round(duration, 2)
    log_path = task_dir / "run_log.json"
    log_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # ── 输出结果 ──
    status_icon = "✅" if result["status"] == "completed" else "❌"
    print(f"\n  {status_icon} 分析完成: {result['status']}")
    print(f"  📁 输出目录: {task_dir}")
    print(f"  📄 生成文件: {', '.join(result['generated_files'])}")
    if manifest["recommended_files"]:
        rec_names = ", ".join(f["name"] for f in manifest["recommended_files"][:4])
        print(f"  👀 推荐先看: {rec_names}")
    if result["error_message"]:
        print(f"  ⚠️ 错误: {result['error_message']}")

    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python -m datahelp.pipeline <input_file> <output_dir> [--provider deepseek] [--mode beginner_summary]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_dir = sys.argv[2]
    provider = sys.argv[3] if len(sys.argv) > 3 else "deepseek"
    mode = sys.argv[4] if len(sys.argv) > 4 else "beginner_summary"

    result = run_data_help_analysis(input_file, output_dir, provider=provider, mode=mode)
    print(f"\n最终状态: {result['status']}")
