"""Sandboxed file access for the agents.

The CDK project is exposed read-only; the Terraform output directory is the
only place the agents can write. Every path the model supplies is resolved and
checked against its root so a crafted path ("../../etc/passwd", an absolute
path, a symlink out of the tree) can never escape.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Directories that never contain hand-written CDK source.
IGNORED_DIRS = {
    ".git",
    "node_modules",
    "cdk.out",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    "target",
    "obj",
    ".idea",
    ".vscode",
    ".terraform",
}

# Source extensions CDK apps are written in, plus the config files that
# describe how the app is wired (cdk.json context, package deps, etc.).
SOURCE_EXTENSIONS = {
    ".ts", ".js", ".mjs", ".py", ".java", ".cs", ".go",
    ".json", ".yaml", ".yml", ".md", ".txt", ".toml", ".cfg",
    ".sh", ".sql", ".graphql", ".html", ".conf",
}

WRITABLE_EXTENSIONS = {".tf", ".tfvars", ".md", ".json", ".tftpl", ".txt", ".py", ".js", ".sh", ".yaml", ".yml"}

MAX_READ_BYTES = 200_000


class WorkspaceError(Exception):
    """Raised for any disallowed or failed file operation; surfaced to the model."""


def _resolve_inside(root: Path, rel: str) -> Path:
    if not rel or rel.strip() == "":
        raise WorkspaceError("path must not be empty")
    candidate = Path(rel)
    if candidate.is_absolute():
        raise WorkspaceError(f"absolute paths are not allowed: {rel}")
    resolved = (root / candidate).resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise WorkspaceError(f"path escapes the workspace: {rel}")
    return resolved


@dataclass
class Workspace:
    source_root: Path
    output_root: Path
    notes: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.source_root = Path(self.source_root).resolve()
        self.output_root = Path(self.output_root).resolve()
        if not self.source_root.is_dir():
            raise WorkspaceError(f"CDK source directory not found: {self.source_root}")
        self.output_root.mkdir(parents=True, exist_ok=True)

    # ---- CDK source (read-only) -------------------------------------------

    def _walk(self, root: Path):
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = Path(dirpath).relative_to(root)
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                yield (rel_dir / name).as_posix()

    def list_source_files(self, pattern: str | None = None) -> list[str]:
        files = []
        for rel in self._walk(self.source_root):
            suffix = Path(rel).suffix
            if suffix not in SOURCE_EXTENSIONS:
                continue
            if rel.endswith(".d.ts") or rel.endswith("package-lock.json"):
                continue
            if pattern and not fnmatch.fnmatch(rel, pattern):
                continue
            files.append(rel)
        return files

    def read_source_file(self, rel: str, start_line: int | None = None, end_line: int | None = None) -> str:
        return self._read(self.source_root, rel, start_line, end_line)

    def search_source(self, pattern: str, glob: str | None = None, max_results: int = 200) -> list[str]:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise WorkspaceError(f"invalid regex: {exc}") from exc
        hits: list[str] = []
        for rel in self.list_source_files(glob):
            path = self.source_root / rel
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{rel}:{lineno}: {line.strip()[:300]}")
                    if len(hits) >= max_results:
                        hits.append(f"... truncated at {max_results} matches; narrow the pattern or glob")
                        return hits
        return hits

    # ---- Terraform output (read-write) -------------------------------------

    def list_output_files(self) -> list[str]:
        return [rel for rel in self._walk(self.output_root)]

    def read_output_file(self, rel: str) -> str:
        return self._read(self.output_root, rel, None, None)

    def write_output_file(self, rel: str, content: str) -> str:
        path = _resolve_inside(self.output_root, rel)
        if path.suffix not in WRITABLE_EXTENSIONS:
            raise WorkspaceError(
                f"refusing to write {rel}: extension must be one of {sorted(WRITABLE_EXTENSIONS)}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
        verb = "overwrote" if existed else "created"
        return f"{verb} {path.relative_to(self.output_root).as_posix()} ({len(content)} chars)"

    def delete_output_file(self, rel: str) -> str:
        path = _resolve_inside(self.output_root, rel)
        if not path.is_file():
            raise WorkspaceError(f"no such output file: {rel}")
        path.unlink()
        return f"deleted {rel}"

    # ---- shared helpers ----------------------------------------------------

    @staticmethod
    def _read(root: Path, rel: str, start_line: int | None, end_line: int | None) -> str:
        path = _resolve_inside(root, rel)
        if not path.is_file():
            raise WorkspaceError(f"no such file: {rel}")
        if path.stat().st_size > MAX_READ_BYTES and start_line is None:
            raise WorkspaceError(
                f"{rel} is {path.stat().st_size} bytes; read it in slices with start_line/end_line"
            )
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, start_line or 1)
        end = min(len(lines), end_line or len(lines))
        width = len(str(end))
        return "\n".join(f"{i:>{width}}| {lines[i - 1]}" for i in range(start, end + 1))
