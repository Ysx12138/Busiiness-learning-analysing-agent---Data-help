"""配置管理 —— 加载 .env 文件，统一管理环境变量。"""

import os
import re
from pathlib import Path

ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_env_line(line: str):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].strip()
    if "=" not in line:
        return None
    name, value = line.split("=", 1)
    name = name.strip()
    if not ENV_KEY_PATTERN.match(name):
        return None
    return name, _strip_quotes(value)


def find_project_env(start: str | Path) -> Path | None:
    """从 start 目录向上查找 .env 文件。"""
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for path in (current, *current.parents):
        env_path = path / ".env"
        if env_path.exists():
            return env_path
    return None


def load_project_env(start: str | Path, override: bool = True) -> dict[str, str]:
    """加载 .env 文件并写入 os.environ。"""
    env_path = find_project_env(start)
    if env_path is None:
        return {}
    loaded = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        name, value = parsed
        loaded[name] = value
        if override or name not in os.environ:
            os.environ[name] = value
    return loaded


def provider_env(name: str, *legacy_names: str, default: str = "") -> str:
    """按优先级读取环境变量，支持多个旧名称回退。"""
    for env_name in (name, *legacy_names):
        value = os.environ.get(env_name)
        if value:
            return value
    return default
