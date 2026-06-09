"""文件监听器 —— 监听 input_dir，自动触发数据分析。

工作流程：
  1. 检查 input_dir 是否有新文件
  2. 等待文件复制完成（大小稳定检测）
  3. 检查任务历史，避免重复分析
  4. 调用 pipeline.run_data_help_analysis()
  5. 记录运行状态

不修改任何现有 skill 输出逻辑。
"""

import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from datahelp.config_manager import load_config
from datahelp.pipeline import run_data_help_analysis


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 任务历史管理 ─────────────────────────────────────


TASK_HISTORY_FILENAME = "task_history.json"


def _task_history_path(output_dir: str) -> Path:
    return Path(output_dir).resolve() / ".datahelp" / TASK_HISTORY_FILENAME


def load_task_history(output_dir: str) -> dict:
    """读取任务历史，避免重复分析。"""
    path = _task_history_path(output_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_task_history(output_dir: str, history: dict) -> None:
    """保存任务历史。"""
    path = _task_history_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


def file_fingerprint(file_path: Path) -> str:
    """计算文件指纹：大小 + 修改时间，用于快速判断文件是否变化。"""
    try:
        stat = file_path.stat()
        return f"{stat.st_size}-{stat.st_mtime_ns}"
    except OSError:
        return ""


def file_hash(file_path: Path) -> str:
    """计算 SHA256 文件哈希，用于精确判断文件是否相同。"""
    try:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


# ── 文件检测 ─────────────────────────────────────────


def detect_new_files(input_dir: str, history: dict, supported_extensions: list[str]) -> list[Path]:
    """检测 input_dir 中未处理过的新文件。

    规则：
    - 只返回普通文件（不是目录）
    - 只返回支持的文件格式
    - 跳过已在 history 中的文件（路径精确匹配）
    - 跳过文件名以 . 开头的隐藏文件
    - 跳过临时文件（以 ~ 结尾或 .tmp/.temp）
    """
    input_path = Path(input_dir).resolve()
    if not input_path.exists():
        return []

    skip_extensions = {".tmp", ".temp", ".part"}
    new_files = []

    for f in sorted(input_path.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0):
        if not f.is_file():
            continue

        # 跳过隐藏文件和临时文件
        if f.name.startswith("."):
            continue
        if f.name.endswith("~"):
            continue
        if f.suffix.lower() in skip_extensions:
            continue

        # 检查是否支持
        if f.suffix.lower() not in supported_extensions:
            continue

        # 检查是否已处理过
        fp = file_fingerprint(f)
        abs_path = str(f.resolve())

        existing = history.get(abs_path)
        if existing:
            # 如果文件内容没变，跳过
            if existing.get("fingerprint") == fp:
                continue
            # 指纹变了，文件被修改过，重新分析
            print(f"  📝 检测到文件已修改: {f.name}")

        new_files.append(f)

    return new_files


def wait_for_file_stable(file_path: Path, stability_wait: int = 3, max_checks: int = 5) -> bool:
    """等待文件复制完成（大小稳定检测）。

    策略：
    1. 记录文件当前大小
    2. 等待 stability_wait 秒
    3. 再次检查文件大小
    4. 如果大小不变 → 认为复制完成
    5. 如果大小变化 → 继续等待，最多 max_checks 次
    """
    for check in range(max_checks):
        try:
            prev_size = file_path.stat().st_size
        except OSError:
            return False

        time.sleep(stability_wait)

        try:
            current_size = file_path.stat().st_size
        except OSError:
            return False

        if current_size == prev_size and current_size > 0:
            return True

        # 文件还在变化中
        if check == 0:
            print(f"  ⏳ 文件还在复制中 (当前: {current_size} 字节)...")

    return False


# ── 主循环 ───────────────────────────────────────────


def render_status_line(
    task_dir: str | None,
    status: str,
    filename: str,
) -> str:
    """渲染单行状态信息。"""
    if status == "running":
        return f"  ▶️  {filename}"
    elif status == "completed":
        return f"  ✅ {filename} → {task_dir}" if task_dir else f"  ✅ {filename}"
    elif status == "failed":
        return f"  ❌ {filename} (失败)"
    return f"  ⏹️  {filename}"


def start_watching(config: dict, single_run: bool = False) -> None:
    """启动监听主循环。

    参数：
        config: 配置字典（来自 config_manager.load_config()）
        single_run: True = 检查一次后退出（用于测试）；False = 持续监听
    """
    input_dir = str(config.get("input_dir", ""))
    output_dir = str(config.get("output_dir", ""))
    supported = config.get("supported_files", [".csv", ".xlsx", ".xls"])
    poll_interval = int(config.get("poll_interval", 5))
    stability_wait = int(config.get("stability_wait", 3))
    provider = str(config.get("provider", "deepseek"))
    model = config.get("model")
    mode = str(config.get("mode", "beginner_summary"))

    # ── 验证配置 ──
    if not input_dir or not output_dir:
        print("\n  ⚠️ 配置不完整。请先运行 setup：")
        print("     python -m datahelp setup\n")
        return

    input_path = Path(input_dir)
    output_path = Path(output_dir)

    if not input_path.exists():
        print(f"\n  ❌ 输入文件夹不存在: {input_dir}")
        print("     请运行 python -m datahelp setup 重新设置\n")
        return

    output_path.mkdir(parents=True, exist_ok=True)

    # ── 显示状态 ──
    print(f"\n  ╭─ DataHelp Agent 监听中 ─────────────────────")
    print(f"  │ 输入文件夹: {input_dir}")
    print(f"  │ 输出文件夹: {output_dir}")
    print(f"  │ 支持格式: {', '.join(supported)}")
    print(f"  │ 模型: {provider}/{model or '默认'}")
    print(f"  │ 模式: {mode}")
    if single_run:
        print(f"  │ 模式: 单次检测")
    else:
        print(f"  │ 监听间隔: {poll_interval} 秒")
    print(f"  ╰──────────────────────────────────────────────\n")

    # ── 加载任务历史 ──
    history = load_task_history(output_dir)

    # ── 主循环 ──
    iteration = 0
    while True:
        iteration += 1

        try:
            new_files = detect_new_files(input_dir, history, supported)

            if new_files:
                print(f"\n  📋 第 {iteration} 次检测: 发现 {len(new_files)} 个新文件\n")
            else:
                if not single_run:
                    time.sleep(poll_interval)
                elif iteration > 1:
                    print("\n  ℹ️  没有新文件。\n")
                    break
                continue

            for file_path in new_files:
                filename = file_path.name
                abs_path = str(file_path.resolve())

                print(f"  📄 发现新文件: {filename}")

                # ── 等待文件复制完成 ──
                if not wait_for_file_stable(file_path, stability_wait):
                    print(f"  ⚠️  {filename}: 文件复制可能未完成，仍尝试分析...")
                    # 仍然尝试分析，让 pipeline 自行处理

                # ── 更新历史（标记为运行中） ──
                history[abs_path] = {
                    "file_path": abs_path,
                    "fingerprint": file_fingerprint(file_path),
                    "file_hash": file_hash(file_path),
                    "status": "running",
                    "output_dir": "",
                    "created_at": now_iso(),
                }
                save_task_history(output_dir, history)

                # ── 触发分析 ──
                try:
                    result = run_data_help_analysis(
                        input_file=abs_path,
                        output_dir=str(output_dir),
                        provider=provider,
                        model=model,
                        mode=mode,
                    )

                    # ── 更新历史 ──
                    history[abs_path] = {
                        "file_path": abs_path,
                        "fingerprint": file_fingerprint(file_path),
                        "file_hash": file_hash(file_path),
                        "status": result.get("status", "failed"),
                        "output_dir": result.get("output_dir", ""),
                        "created_at": now_iso(),
                        "generated_files": result.get("generated_files", []),
                    }
                    save_task_history(output_dir, history)

                    print(render_status_line(
                        result.get("output_dir", ""),
                        result.get("status", "failed"),
                        filename,
                    ))

                except Exception as e:
                    history[abs_path] = {
                        "file_path": abs_path,
                        "fingerprint": file_fingerprint(file_path),
                        "file_hash": file_hash(file_path),
                        "status": "failed",
                        "output_dir": "",
                        "created_at": now_iso(),
                        "error": str(e),
                    }
                    save_task_history(output_dir, history)
                    print(f"  ❌ {filename}: 分析失败 - {e}")

            if single_run:
                print(f"\n  ℹ️  单次检测完成。\n")
                break

            time.sleep(poll_interval)

        except KeyboardInterrupt:
            print("\n\n  ⏹️  监听已停止。\n")
            break
        except Exception as e:
            print(f"\n  ⚠️  监听异常: {e}")
            if single_run:
                break
            time.sleep(poll_interval)


if __name__ == "__main__":
    config = load_config()
    single = "--once" in sys.argv
    start_watching(config, single_run=single)
