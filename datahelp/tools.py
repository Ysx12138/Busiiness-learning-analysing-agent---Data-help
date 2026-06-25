"""工具注册表与安全校验 —— 定义 agent 可执行的所有动作。"""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from datahelp.workspace import IGNORED_PATH_NAMES
from datahelp.tools_data import (
    read_csv_summary,
    generate_excel,
    generate_html,
    generate_pdf,
)


def _is_ignored(name: str) -> bool:
    if name in IGNORED_PATH_NAMES:
        return True
    for pat in IGNORED_PATH_NAMES:
        if pat.startswith("*") and name.endswith(pat[1:]):
            return True
    return False


def _filter_entries(entries: list[Path]) -> list[Path]:
    return [e for e in entries if not _is_ignored(e.name)]


def _search_with_rg(pattern: str, path: Path, max_results: int) -> list[str] | None:
    try:
        result = subprocess.run(
            ["rg", "--no-heading", "--line-number", "--smart-case",
             "--max-count", str(max_results), pattern, str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.strip().split("\n")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _make_tool_spec(schema: dict, risky: bool, description: str, run_fn):
    return {
        "schema": schema,
        "risky": risky,
        "description": description,
        "run": run_fn,
    }


def resolve_path(raw_path: str, repo_root: str) -> str:
    target = (Path(repo_root) / raw_path).resolve()
    root = Path(repo_root).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(f"路径逃逸: {raw_path} -> {target} 不在仓库 {root} 内")
    return str(target)


def validate_tool_args(name: str, args: dict, schema: dict) -> None:
    for param, spec in schema.items():
        if "=" in spec:
            param_type, default = spec.split("=", 1)
        else:
            param_type = spec
            default = None
        value = args.get(param)
        if value is None and default is not None:
            args[param] = int(default) if param_type == "int" else default
            continue
        if value is None:
            raise ValueError(f"工具 '{name}' 缺少必填参数 '{param}'")
        if param_type == "int":
            args[param] = int(value)
        elif param_type == "str" and not isinstance(value, str):
            raise ValueError(f"参数 '{param}' 应为字符串，收到 {type(value).__name__}")
    if name in ("read_file", "write_file", "patch_file") and "path" in args:
        path = args["path"]
        if not path or not isinstance(path, str):
            raise ValueError("路径不能为空")


def is_repeated_call(name: str, args: dict, last_tool: tuple | None) -> bool:
    if last_tool is None:
        return False
    last_name, last_args = last_tool
    return name == last_name and args == last_args


def tool_list_files(args: dict, repo_root: str) -> str:
    path_str = resolve_path(args["path"], repo_root)
    path = Path(path_str)
    if not path.is_dir():
        return f"错误: {path} 不是目录"
    try:
        entries = list(path.iterdir())
    except PermissionError:
        return f"错误: 没有权限读取目录 {path}"
    entries = _filter_entries(entries)
    dirs = sorted(e for e in entries if e.is_dir())
    files = sorted(e for e in entries if e.is_file())
    others = sorted(e for e in entries if e not in dirs and e not in files)

    MAX_ENTRIES = 200
    ordered = (dirs + files + others)[:MAX_ENTRIES]
    lines = [f"目录 {path_str} ({len(entries)} 项)"]
    if len(entries) > MAX_ENTRIES:
        lines[0] += f"，显示前 {MAX_ENTRIES}"
    for entry in ordered:
        if entry.is_dir():
            lines.append(f"  [D] {entry.name}/")
        elif entry.is_file():
            lines.append(f"  [F] {entry.name}")
        else:
            lines.append(f"  [?] {entry.name}")
    return "\n".join(lines) if ordered else f"(空目录: {path})"


def tool_read_file(args: dict, repo_root: str) -> str:
    path_str = resolve_path(args["path"], repo_root)
    path = Path(path_str)
    if not path.exists():
        return f"错误: 文件不存在 {path}"
    if not path.is_file():
        return f"错误: {path} 不是文件"
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    start_line = int(args.get("start_line", 1))
    end_line = int(args.get("end_line", 0)) or len(lines)
    start_line = max(1, start_line)
    end_line = min(len(lines), end_line)
    selected = lines[start_line - 1 : end_line]
    result_lines = []
    for i, line in enumerate(selected, start=start_line):
        result_lines.append(f"{i:6d}  {line}")
    summary = f"文件 {path_str} ({len(lines)} 行，显示 {start_line}-{end_line})"
    return summary + "\n" + "\n".join(result_lines)


def tool_search(args: dict, repo_root: str) -> str:
    pattern = args["pattern"]
    path_str = args.get("path", repo_root)
    path = Path(resolve_path(path_str, repo_root))
    if not path.exists():
        return f"错误: 路径不存在 {path}"
    include = args.get("include", "*")
    max_results = int(args.get("max_results", 30))

    matches = None
    if path.is_dir():
        matches = _search_with_rg(pattern, path, max_results)

    if matches is not None:
        return "\n".join(matches) if matches else f"未找到包含 '{pattern}' 的内容"

    matches = []
    try:
        if path.is_file():
            targets = [path]
        else:
            targets = []
            for root_dir, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if not _is_ignored(d)]
                for f in files:
                    if _is_ignored(f):
                        continue
                    if fnmatch.fnmatch(f, include):
                        targets.append(Path(root_dir) / f)
    except PermissionError:
        return "错误: 没有权限搜索"
    for target in targets:
        if len(matches) >= max_results:
            break
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(text.split("\n"), start=1):
                if pattern in line and len(matches) < max_results:
                    rel_path = target.relative_to(Path(repo_root))
                    matches.append(f"{rel_path}:{i}: {line.strip()[:120]}")
        except (OSError, ValueError):
            continue
    if not matches:
        return f"未找到包含 '{pattern}' 的内容"
    return "\n".join(matches)


def tool_run_shell(args: dict, repo_root: str) -> str:
    command = args["command"]
    timeout = min(int(args.get("timeout", 20)), 120)
    safe_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "USER": os.environ.get("USER", ""),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
    }
    # 将命令开头的 python/python3 替换为 sys.executable（允许前置空白）
    command = re.sub(r"(^\s*|(?:&&|;)\s*)python(?:3)?\s+", lambda m: f"{m.group(1)}{shlex.quote(sys.executable)} ", command, count=1)
    try:
        result = subprocess.run(
            command,
            cwd=repo_root,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=safe_env,
        )
    except subprocess.TimeoutExpired:
        return f"错误: 命令执行超时 ({timeout} 秒)"
    except Exception as e:
        return f"错误: 命令执行失败 - {e}"
    output_parts = [f"执行命令: {command}"]
    if result.stdout:
        output_parts.append(result.stdout.strip()[:3000])
    if result.stderr:
        output_parts.append(f"(stderr) {result.stderr.strip()[:1000]}")
    if result.returncode != 0:
        output_parts.append(f"(exit code: {result.returncode})")
    return "\n".join(output_parts) if output_parts else "(命令无输出)"


def tool_write_file(args: dict, repo_root: str) -> str:
    path_str = resolve_path(args["path"], repo_root)
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = args["content"]
    path.write_text(content, encoding="utf-8")
    return f"已写入 {len(content)} 字符到 {path_str}"


def tool_patch_file(args: dict, repo_root: str) -> str:
    path_str = resolve_path(args["path"], repo_root)
    path = Path(path_str)
    if not path.exists():
        return f"错误: 文件不存在 {path}"
    old_text = args["old_text"]
    new_text = args.get("new_text", "")
    content = path.read_text(encoding="utf-8")
    count = content.count(old_text)
    if count == 0:
        return "错误: 未找到要替换的文本（区分大小写）"
    if count > 1:
        return f"错误: 找到 {count} 处匹配，需要精确匹配唯一位置。请提供更多上下文。"
    new_content = content.replace(old_text, new_text, 1)
    path.write_text(new_content, encoding="utf-8")
    return f"已替换 1 处，文件 {path_str}"


def build_tool_registry(repo_root: str, depth: int = 0, max_depth: int = 1) -> dict:
    def _make(fn):
        def wrapped(args):
            return fn(args, repo_root)
        return wrapped

    tools = {
        "list_files": _make_tool_spec(
            schema={"path": "str"},
            risky=False,
            description="列出目录中的文件和子目录",
            run_fn=_make(tool_list_files),
        ),
        "read_file": _make_tool_spec(
            schema={"path": "str", "start_line": "int=1", "end_line": "int=0"},
            risky=False,
            description="读取文件内容，可指定行号范围",
            run_fn=_make(tool_read_file),
        ),
        "search": _make_tool_spec(
            schema={"pattern": "str", "path": "str=", "include": "str=*", "max_results": "int=30"},
            risky=False,
            description="在仓库中搜索文本（类似 grep）",
            run_fn=_make(tool_search),
        ),
        "run_shell": _make_tool_spec(
            schema={"command": "str", "timeout": "int=20"},
            risky=True,
            description="在仓库根目录执行 shell 命令",
            run_fn=_make(tool_run_shell),
        ),
        "write_file": _make_tool_spec(
            schema={"path": "str", "content": "str"},
            risky=True,
            description="创建或覆盖文件",
            run_fn=_make(tool_write_file),
        ),
        "patch_file": _make_tool_spec(
            schema={"path": "str", "old_text": "str", "new_text": "str="},
            risky=True,
            description="精确替换文件中的文本",
            run_fn=_make(tool_patch_file),
        ),
        "csv_summary": _make_tool_spec(
            schema={"path": "str"},
            risky=False,
            description="读取 CSV 文件并返回基础概览：行数、列数、列名",
            run_fn=lambda args: read_csv_summary(args["path"], repo_root),
        ),
        "generate_excel": _make_tool_spec(
            schema={"path": "str", "output_dir": "str=", "analysis_text": "str=", "mode": "str="},
            risky=False,
            description="生成数据分析 Excel 交付物（含数据表、统计表、图表、看板）",
            run_fn=lambda args: generate_excel(args["path"], repo_root, args.get("output_dir", ""), args.get("analysis_text", ""), args.get("mode", "")),
        ),
        "generate_html": _make_tool_spec(
            schema={"path": "str", "output_dir": "str=", "analysis_text": "str=", "mode": "str="},
            risky=False,
            description="生成数据分析 HTML 报告",
            run_fn=lambda args: generate_html(args["path"], repo_root, args.get("output_dir", ""), args.get("analysis_text", "")),
        ),
        "generate_pdf": _make_tool_spec(
            schema={"path": "str", "output_dir": "str=", "analysis_text": "str=", "mode": "str="},
            risky=False,
            description="生成数据分析 PDF 报告",
            run_fn=lambda args: generate_pdf(args["path"], repo_root, args.get("output_dir", ""), args.get("analysis_text", "")),
        ),
    }
    if depth < max_depth:
        tools["delegate"] = _make_tool_spec(
            schema={"task": "str", "max_steps": "int=3"},
            risky=True,
            description="将子任务委派给子 agent",
            run_fn=_make(lambda args: f"(delegate: {args['task']})"),
        )
    return tools


def describe_tools(tools: dict) -> str:
    lines = []
    for name, spec in tools.items():
        params = ", ".join(f"{k}: {v}" for k, v in spec["schema"].items())
        risk = " [高风险]" if spec["risky"] else ""
        lines.append(f"  - {name}({params}){risk}")
        lines.append(f"    {spec['description']}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    test_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    repo_root = str(Path(test_dir).resolve())
    tools = build_tool_registry(repo_root)
    print("=== 可用工具 ===")
    print(describe_tools(tools))
    print()
    print("=== list_files('.') ===")
    print(tools["list_files"]["run"]({"path": "."}))
