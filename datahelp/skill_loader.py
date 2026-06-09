"""Skill 加载器 —— 从 skills/<name>/SKILL.md 读取规则文本。"""

from pathlib import Path


def load_skill(name: str = "business_analysis") -> str:
    """读取 skills/<name>/SKILL.md，返回纯文本规则。文件不存在时返回空字符串。"""
    skill_path = Path(__file__).resolve().parent.parent / "skills" / name / "SKILL.md"
    if not skill_path.exists():
        return ""
    return skill_path.read_text(encoding="utf-8").strip()
