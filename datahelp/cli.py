"""命令行入口 —— 支持 one-shot 和 REPL 模式。"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from datahelp.config import load_project_env, provider_env
from datahelp.models import create_model_client
from datahelp.runtime import DataHelp
from datahelp.run_store import RunStore
from datahelp.tools import build_tool_registry
from datahelp.memory import DurableMemoryStore
from datahelp.evaluator import Evaluator


def build_agent(args) -> tuple[DataHelp, RunStore]:
    # 启动时加载 .env 文件
    load_project_env(args.cwd or ".")
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    repo_root = str(cwd)
    provider = args.provider or provider_env("DATAHELP_PROVIDER", default="mock")
    model = create_model_client(provider=provider, model=args.model, temperature=args.temperature)
    tools = build_tool_registry(repo_root)
    datahelp_dir = cwd / ".datahelp"
    datahelp_dir.mkdir(parents=True, exist_ok=True)
    run_store = RunStore(str(datahelp_dir))
    datahelp_memory_dir = datahelp_dir / "memory"
    durable_memory = DurableMemoryStore(str(datahelp_memory_dir))
    checkpoint_dir = str(datahelp_dir / "checkpoints")
    agent = DataHelp(model_client=model, repo_root=repo_root, tools=tools, max_steps=args.max_steps or 20, max_new_tokens=args.max_new_tokens or 4096, approval_policy=args.approval or "auto", durable_memory=durable_memory, checkpoint_dir=checkpoint_dir, skill_name="business_analysis" if args.mode else None, mode=args.mode, output_dir=args.output_dir)
    return agent, run_store


def _skill_step0_confirm(args, agent: DataHelp) -> tuple[str, str, str]:
    """Step 0: 交互确认数据集、输出模式、输出文件夹。

    仅在 --mode 指定时执行。
    返回 (task, mode, output_dir)，其中 task 可能被修改。
    """
    mode = agent._mode or "beginner_summary"
    output_dir = agent._output_dir or ""
    task = args.task or ""

    print("\n  ╭─ Step 0: 分析前确认 ───────────────────")
    print("  │ 请确认以下三项信息：")

    # 0a: 确认数据集
    default_dataset = ""
    if task:
        import re
        path_match = re.search(r"data/[^\s,，。]+\.csv", task)
        if path_match:
            default_dataset = path_match.group()
    dataset_prompt = f"  数据集路径 [{default_dataset or '未指定'}]: "
    dataset_input = input(dataset_prompt).strip()
    if dataset_input:
        task = f"分析 {dataset_input} 的数据"

    # 0b: 确认输出模式
    mode_prompt = f"  输出模式 [{mode}] (beginner_summary / standard_report / audit_report): "
    mode_input = input(mode_prompt).strip()
    if mode_input and mode_input in ("beginner_summary", "standard_report", "audit_report"):
        mode = mode_input

    # 0c: 确认输出文件夹
    out_prompt = f"  输出文件夹 [{output_dir or '默认（不生成交付物）'}]: "
    out_input = input(out_prompt).strip()
    if out_input:
        output_dir = out_input

    # 更新 agent
    agent._mode = mode
    agent._output_dir = output_dir if output_dir else None
    if agent.skill_engine:
        from datahelp.skill_engine import create_skill_engine
        agent.skill_engine = create_skill_engine(mode)

    print(f"  │ 模式: {mode}  |  输出: {output_dir or '无'}")
    print("  ╰────────────────────────────────────────\n")
    return task, mode, output_dir


def run_one_shot(agent: DataHelp, run_store: RunStore, task: str, output_dir: str | None = None):
    print(f"\n  datahelp: {task}")
    print(f"  模型: {agent.model.model_name}")
    print(f"  仓库: {agent.repo_root}\n")
    if agent.task_state:
        run_store.create_run_dir(agent.task_state.run_id)
    result = agent.ask(task)
    if agent.task_state:
        run_store.write_task_state(agent.task_state)
        run_store.append_trace(agent.task_state.run_id, {"event": "run_completed", "status": agent.task_state.status})
        run_store.write_report(agent.task_state.run_id, agent.task_state)
    print(f"\n  ✅ {agent.task_state.status} | 步骤: {agent.task_state.tool_steps} | {agent.task_state.stop_reason}\n")

    # 获取 agent 分析结论
    analysis_text = agent.task_state.final_answer if agent.task_state else ""

    # 生成交付物（当指定了 output-dir 时）
    if output_dir:
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        # 扫描工作目录中的 CSV 文件
        repo_path = Path(agent.repo_root)
        csv_files = list(repo_path.glob("*.csv"))
        if csv_files:
            mode = agent.skill_engine.config.mode if agent.skill_engine else ""
            from datahelp.tools_data import generate_excel, generate_html, generate_pdf
            for csv_file in csv_files:
                try:
                    print(generate_excel(str(csv_file), agent.repo_root, str(output_path), analysis_text=analysis_text, mode=mode))
                    print(generate_html(str(csv_file), agent.repo_root, str(output_path), analysis_text=analysis_text))
                    print(generate_pdf(str(csv_file), agent.repo_root, str(output_path), analysis_text=analysis_text, mode=mode))
                except Exception as e:
                    print(f"  ⚠️  {csv_file.name} 交付物生成失败: {e}")
        else:
            print("  ℹ️  未在工作目录找到 CSV 文件，跳过交付物生成")
            print(f"  💡 提示: 将 CSV 数据文件放在 {agent.repo_root} 下并加上 --output-dir")

    return result


def run_repl(agent: DataHelp, run_store: RunStore):
    print(f"\n  ╭─ DataHelp ───────────────────────────")
    print(f"  │ 模型: {agent.model.model_name}")
    print(f"  │ 仓库: {agent.repo_root}")
    print(f"  │ 输入 /exit 退出")
    print(f"  ╰────────────────────────────────────\n")
    while True:
        try:
            task = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); break
        if not task:
            continue
        if task in ("/exit", "/quit"):
            break
        result = agent.ask(task)
        if agent.task_state:
            run_store.write_task_state(agent.task_state)
            run_store.write_report(agent.task_state.run_id, agent.task_state)
        print(f"\n  {result}\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="datahelp", description="DataHelp —— 商科生的数据分析学习 Agent")
    parser.add_argument("--provider", "-p", default=provider_env("DATAHELP_PROVIDER", default="mock"), choices=["mock", "deepseek", "openai", "ollama", "anthropic"], help="模型提供商")
    parser.add_argument("--model", "-m", default=None, help="模型名称")
    parser.add_argument("--cwd", default=None, help="工作目录")
    parser.add_argument("--max-steps", type=int, default=40, help="最大工具调用步数")
    parser.add_argument("--approval", default="auto", choices=["auto", "ask", "never"], help="高风险工具审批策略（auto: 自动, ask: 询问, never: 禁止）")
    parser.add_argument("--temperature", type=float, default=None, help="模型采样温度 (0.0-2.0)")
    parser.add_argument("--max-new-tokens", type=int, default=None, help="每次模型调用的最大输出 token 数")
    parser.add_argument("--mode", default=None, choices=["beginner_summary", "standard_report", "audit_report"], help="输出模式（beginner_summary: 初学者友好, standard_report: 标准报告, audit_report: 审计报告）")
    parser.add_argument("--output-dir", "-o", default=None, help="交付产物输出文件夹（Excel/HTML/PDF）")
    parser.add_argument("--eval", default=None, help="评测模式：指定测试集 JSONL 文件路径")
    parser.add_argument("task", nargs="?", default=None, help="一次性任务文本。不传则进入交互模式。")
    return parser.parse_args(argv)


def main():
    # ── Data Help Agent 子命令 ──────────────────────────
    if len(sys.argv) > 1 and sys.argv[1] in ("setup", "watch", "config"):
        subcommand = sys.argv[1]

        if subcommand == "setup":
            from datahelp.config_manager import setup_interactive
            setup_interactive()
            return

        if subcommand == "config":
            from datahelp.config_manager import show_config
            show_config()
            return

        if subcommand == "watch":
            from datahelp.config_manager import load_config
            from datahelp.watcher import start_watching
            single_run = "--once" in sys.argv
            config = load_config()
            start_watching(config, single_run=single_run)
            return

    # ── 原有流程（手动模式、eval、REPL）────────────────
    args = parse_args()

    if args.eval:
        from datahelp.evaluator import Evaluator

        def _factory():
            a, _ = build_agent(args)
            return a

        print(f"\n  ╭─ DataHelp eval ─────────────────────")
        print(f"  │ 模型: {args.provider or provider_env('DATAHELP_PROVIDER', default='mock')}")
        print(f"  │ 测试集: {args.eval}")
        print(f"  ╰────────────────────────────────────\n")
        evaluator = Evaluator(_factory, args.eval)
        evaluator.run_all()
        evaluator.report()
        return

    agent, run_store = build_agent(args)
    if args.task:
        # Step 0: mode 指定时执行输入确认
        task = args.task
        output_dir = args.output_dir
        if args.mode and not args.eval:
            task, mode, output_dir = _skill_step0_confirm(args, agent)
        result = run_one_shot(agent, run_store, task, output_dir=output_dir)
        print(result)
    else:
        run_repl(agent, run_store)


if __name__ == "__main__":
    main()
