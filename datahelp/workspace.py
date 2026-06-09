"""WorkspaceContext —— agent 进入仓库后的工作区快照。"""

import hashlib
import os
import subprocess
import textwrap
from pathlib import Path

DOC_NAMES = ("AGENTS.md", "README.md", "pyproject.toml", "package.json")
IGNORED_PATH_NAMES = {
    ".git", "__pycache__", ".venv", "node_modules", ".pytest_cache",
    ".mypy_cache", ".DS_Store", "*.pyc", ".gitignore", ".env",
}
MAX_DOC_CHARS = 1200
RECENT_COMMITS_COUNT = 5


def _run_git(cwd: Path, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _file_fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}-{stat.st_mtime_ns}"


class WorkspaceContext:

    def __init__(self, cwd: Path):
        self.cwd = cwd.resolve()
        self.repo_root: Path | None = None
        self.branch: str | None = None
        self.default_branch: str | None = None
        self.status: str | None = None
        self.recent_commits: list[str] = []
        self.project_docs: dict[str, str] = {}

    @classmethod
    def build(cls, cwd: str | Path) -> "WorkspaceContext":
        ctx = cls(Path(cwd))
        ctx._collect_git_facts()
        ctx._collect_project_docs()
        return ctx

    def _collect_git_facts(self):
        root = _run_git(self.cwd, ["rev-parse", "--show-toplevel"])
        if root is None:
            return
        self.repo_root = Path(root).resolve()
        self.branch = _run_git(self.cwd, ["branch", "--show-current"])
        self.default_branch = _run_git(
            self.cwd, ["symbolic-ref", "refs/remotes/origin/HEAD", "--short"]
        )
        self.status = _run_git(self.cwd, ["status", "--short"])
        log = _run_git(
            self.cwd, ["log", "--oneline", f"-{RECENT_COMMITS_COUNT}"]
        )
        if log:
            self.recent_commits = log.split("\n")

    def _collect_project_docs(self):
        bases = [self.repo_root] if self.repo_root else []
        if self.cwd not in bases:
            bases.append(self.cwd)
        seen: set[str] = set()
        for base in bases:
            if base is None:
                continue
            for name in DOC_NAMES:
                path = base / name
                if not path.exists():
                    continue
                if self.repo_root:
                    try:
                        key = str(path.relative_to(self.repo_root))
                    except ValueError:
                        key = str(path)
                else:
                    key = str(path)
                if key in seen:
                    continue
                seen.add(key)
                text_content = path.read_text(encoding="utf-8", errors="replace")
                self.project_docs[key] = text_content[:MAX_DOC_CHARS]

    def fingerprint(self) -> str:
        parts = [
            str(self.repo_root or ""),
            str(self.branch or ""),
            str(self.status or ""),
            "|".join(self.recent_commits),
        ]
        for key, text in sorted(self.project_docs.items()):
            path = (self.repo_root or self.cwd) / key
            fprint = _file_fingerprint(path) if path.exists() else ""
            parts.append(f"{key}:{fprint}:{len(text)}")
        raw = "||".join(parts)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def text(self) -> str:
        lines = ["## Workspace"]
        lines.append(f"- cwd: {self.cwd}")
        if self.repo_root:
            lines.append(f"- repo_root: {self.repo_root}")
        if self.repo_root:
            branch_str = self.branch or "(detached HEAD)"
            lines.append(f"- branch: {branch_str}")
            if self.default_branch:
                lines.append(f"- default_branch: {self.default_branch}")
            lines.append(f"- dirty: {'yes' if self.status else 'no'}")
        if self.recent_commits:
            lines.append("- recent commits:")
            for commit in self.recent_commits:
                lines.append(f"  - {commit}")
        if self.project_docs:
            lines.append("- project docs:")
            for key in self.project_docs:
                lines.append(f"  - {key}")
        return textwrap.dedent("\n".join(lines))

    def __repr__(self) -> str:
        return (
            f"WorkspaceContext(cwd={self.cwd}, "
            f"repo_root={self.repo_root}, "
            f"branch={self.branch})"
        )


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    ctx = WorkspaceContext.build(target)
    print(ctx.text())
    print(f"\n--- fingerprint: {ctx.fingerprint()} ---")
