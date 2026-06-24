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

        # ── 确定性分析引擎：产生可审计证据文件 ──
        engine_result = None
        try:
            from datahelp.analysis_engine import DeterministicAnalysisEngine

            engine_result = DeterministicAnalysisEngine.run(work_csv, str(task_dir))
            result["evidence_status"] = "success"
            evidence_files = ["analysis_evidence.json", "analysis_evidence.md"]
            result["evidence_files"] = evidence_files
            result["evidence_error"] = ""

            for fname in evidence_files:
                fpath = task_dir / fname
                if fpath.exists() and fname not in result["generated_files"]:
                    result["generated_files"].append(fname)

            print(f"  ✅ 证据文件已生成: {', '.join(evidence_files)}")
        except Exception as e:
            result["evidence_status"] = "failed"
            result["evidence_files"] = []
            result["evidence_error"] = str(e)
            print(f"  ⚠️ 确定性分析引擎异常（不影响主流程）: {e}")

        # ── 加载环境变量 ──
        load_project_env(str(work_dir))

        # ── 分支：确定性分析路径 vs 传统 agent 路径 ──
        if result.get("evidence_status") == "success":
            # 确定性分析 + 报告编排路径（无 agent）
            from datahelp.report_orchestrator import ReportOrchestrator

            try:
                client = create_model_client(provider=provider, model=model)
                outcome = ReportOrchestrator(client, engine_result, mode).run()
                final_answer = outcome.text
                result["report_quality"] = {
                    "status": outcome.quality_status,
                    "attempts": outcome.attempts,
                    "warnings": outcome.warnings,
                }
                agent = None
                _model_name = client.model_name
            except Exception as e:
                from datahelp.report_orchestrator import build_evidence_report
                final_answer = build_evidence_report(engine_result)
                result["report_quality"] = {
                    "status": "degraded",
                    "attempts": 0,
                    "warnings": [f"LLM 报告编排异常: {e}"],
                }
                agent = None
                _model_name = "degraded"
        else:
            # ── 传统 agent 路径（完全原有流程） ──
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

            # ── 执行分析 ──
            user_message = f"分析数据集 {Path(work_csv).name}"
            print(f"\n  📊 开始分析: {Path(input_file).name}")
            print(f"  📁 工作目录: {work_dir}")
            print(f"  🤖 模型: {agent.model.model_name}\n")

            final_answer = agent.ask(user_message)
            _model_name = agent.model.model_name

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
        result["status"] = "completed"
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
