"""配置文件管理 —— Data Help 的 input_dir / output_dir 配置。

供 watcher 和 pipeline 使用，不依赖 agent 主循环。
"""

import json
import os
import sys
from pathlib import Path

CONFIG_DIR_NAME = ".datahelp"
CONFIG_FILE_NAME = "config.json"

DEFAULT_CONFIG = {
    "input_dir": "",
    "output_dir": "",
    "auto_watch": True,
    "supported_files": [".csv", ".xlsx", ".xls"],
    "provider": "deepseek",
    "model": "deepseek-v4-flash",
    "mode": "beginner_summary",
    "poll_interval": 5,
    "stability_wait": 3,
}


def _config_dir() -> Path:
    return Path.home() / CONFIG_DIR_NAME


def config_path() -> Path:
    return _config_dir() / CONFIG_FILE_NAME


def load_config() -> dict:
    """读取配置文件，文件不存在或损坏时返回默认配置。"""
    path = config_path()
    if not path.exists():
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        merged = dict(DEFAULT_CONFIG)
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_CONFIG)


def save_config(config: dict) -> Path:
    """保存配置文件，自动创建目录。"""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def setup_interactive() -> dict:
    """交互式引导用户设置 input_dir 和 output_dir。

    在终端中询问用户路径，校验目录是否存在，保存配置文件。
    """
    print("\n  ╭─ DataHelp Agent 首次配置 ──────────────────")
    print("  │ 需要设置两个文件夹：")
    print("  │ 1. input_dir：你放数据集的文件夹")
    print("  │ 2. output_dir：分析结果输出文件夹")
    print("  ╰──────────────────────────────────────────────\n")

    config = load_config()

    while True:
        raw = input("  输入文件夹路径 (input_dir) > ").strip()
        if not raw:
            print("  ❌ 路径不能为空，请重新输入。")
            continue
        p = Path(raw).expanduser()
        if not p.exists():
            create = input(f"  ⚠️ 路径 {p} 不存在，是否创建？[Y/n] ").strip().lower()
            if create in ("", "y", "yes"):
                p.mkdir(parents=True, exist_ok=True)
                print(f"  ✅ 已创建: {p}")
            else:
                continue
        config["input_dir"] = str(p.resolve())
        break

    while True:
        raw = input("  输出文件夹路径 (output_dir) > ").strip()
        if not raw:
            print("  ❌ 路径不能为空，请重新输入。")
            continue
        p = Path(raw).expanduser()
        if not p.exists():
            create = input(f"  ⚠️ 路径 {p} 不存在，是否创建？[Y/n] ").strip().lower()
            if create in ("", "y", "yes"):
                p.mkdir(parents=True, exist_ok=True)
                print(f"  ✅ 已创建: {p}")
            else:
                continue
        config["output_dir"] = str(p.resolve())
        break

    provider = input(f"  模型提供商 (默认: {config['provider']}) > ").strip()
    if provider:
        config["provider"] = provider

    model = input(f"  模型名称 (默认: {config['model']}) > ").strip()
    if model:
        config["model"] = model

    save_config(config)

    print(f"\n  ✅ 配置已保存: {config_path()}")
    print(f"  📂 输入文件夹: {config['input_dir']}")
    print(f"  📂 输出文件夹: {config['output_dir']}")
    print(f"  🤖 模型: {config['provider']}/{config['model']}")
    print(f"  📋 支持格式: {', '.join(config['supported_files'])}")

    return config


def show_config() -> None:
    """显示当前配置。"""
    config = load_config()
    path = config_path()
    if not path.exists():
        print("\n  ⚠️ 配置文件不存在。请先运行 setup：")
        print("     python -m datahelp setup")
        return

    print("\n  ╭─ DataHelp Agent 当前配置 ────────────────")
    print(f"  │ 配置文件: {path}")
    print(f"  │ 输入文件夹: {config.get('input_dir', '未设置')}")
    print(f"  │ 输出文件夹: {config.get('output_dir', '未设置')}")
    print(f"  │ 模型: {config.get('provider')}/{config.get('model')}")
    print(f"  │ 监听间隔: {config.get('poll_interval', 5)} 秒")
    print(f"  │ 稳定检测等待: {config.get('stability_wait', 3)} 秒")
    print(f"  │ 支持格式: {', '.join(config.get('supported_files', []))}")
    print(f"  │ 自动监听: {'开启' if config.get('auto_watch', True) else '关闭'}")
    print("  ╰──────────────────────────────────────────────\n")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "show":
        show_config()
    else:
        setup_interactive()
