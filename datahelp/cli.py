"""命令行入口 —— 支持 one-shot 和 REPL 模式。"""

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
            from datahelp.tools_data import generate_excel, generate_html, generate_pdf
            for csv_file in csv_files:
                try:
                    print(generate_excel(str(csv_file), agent.repo_root, str(output_path), analysis_text=analysis_text))
                    print(generate_html(str(csv_file), agent.repo_root, str(output_path), analysis_text=analysis_text))
                    print(generate_pdf(str(csv_file), agent.repo_root, str(output_path), analysis_text=analysis_text))
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
        result = run_one_shot(agent, run_store, args.task, output_dir=args.output_dir)
        print(result)
    else:
        run_repl(agent, run_store)


if __name__ == "__main__":
    main()
