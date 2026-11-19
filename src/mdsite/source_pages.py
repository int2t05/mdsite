"""为常见源码和文本文件生成临时浏览页。"""

from __future__ import annotations

import json
import os
import stat
from html import escape
from pathlib import Path

_MAX_FILE_BYTES = 1024 * 1024
_SKIPPED_DIRECTORIES = {
    "build",
    "dist",
    "node_modules",
    "site",
    "target",
    "venv",
    "__pycache__",
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
}
_LANGUAGES_BY_EXTENSION = {
    ".py": "python",
    ".pyi": "python",
    ".txt": "text",
    ".json": "json",
    ".jsonc": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "ini",
    ".properties": "properties",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".ps1": "powershell",
    ".bat": "batch",
    ".cmd": "batch",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".sql": "sql",
    ".r": "r",
    ".jl": "julia",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".xml": "xml",
    ".tex": "latex",
    ".bib": "bibtex",
    ".csv": "text",
    ".tsv": "text",
    ".log": "text",
    ".diff": "diff",
    ".patch": "diff",
}
_LANGUAGES_BY_FILENAME = {
    "dockerfile": "docker",
    "makefile": "make",
    "justfile": "make",
}


def _is_link(path: Path) -> bool:
    """跳过链接，避免跟随源目录里指向外部的内容。"""
    if path.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        attributes = os.lstat(path).st_file_attributes
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _is_hidden(path: Path) -> bool:
    """跳过点文件和 Windows 隐藏属性文件。"""
    if path.name.startswith("."):
        return True
    if os.name != "nt":
        return False
    try:
        attributes = os.lstat(path).st_file_attributes
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2))


def _language(path: Path) -> str | None:
    """按文件名和扩展名选择代码块语言。"""
    return _LANGUAGES_BY_FILENAME.get(path.name.casefold()) or _LANGUAGES_BY_EXTENSION.get(
        path.suffix.casefold()
    )


def _source_files(source: Path) -> list[tuple[Path, str]]:
    """只收集可安全显示的 UTF-8 文本文件。"""
    discovered: list[tuple[Path, str]] = []
    for directory, subdirectories, filenames in os.walk(source, topdown=True, followlinks=False):
        parent = Path(directory)
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if not _is_hidden(parent / name)
            and name.casefold() not in _SKIPPED_DIRECTORIES
            and not _is_link(parent / name)
        )
        for name in sorted(filenames):
            path = parent / name
            language = _language(path)
            if _is_hidden(path) or language is None or _is_link(path):
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                content = path.read_bytes()
                if len(content) > _MAX_FILE_BYTES or b"\0" in content:
                    continue
                content.decode("utf-8-sig")
                relative = path.relative_to(source)
            except (OSError, UnicodeError, ValueError):
                continue
            if "\n" in relative.as_posix() or "\r" in relative.as_posix():
                continue
            discovered.append((relative, language))
    return discovered


def write_source_pages(source: Path, docs: Path) -> int:
    """在临时文档目录写入源码页；正文由 snippets 从源目录读取。"""
    files = _source_files(source)
    if not files:
        return 0

    pages = docs / "files"
    for relative, language in files:
        target = pages.joinpath(*relative.parts[:-1], f"{relative.name}.md")
        target.parent.mkdir(parents=True, exist_ok=True)
        snippet_path = json.dumps(f"source/{relative.as_posix()}", ensure_ascii=False)
        target.write_text(
            "\n".join(
                [
                    f"# <code>{escape(relative.as_posix())}</code>",
                    "",
                    "原文件只通过临时目录链接读取；此页面不会改写源文件。",
                    "",
                    f'```{language} linenums="1"',
                    f"--8<-- {snippet_path}",
                    "```",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    (pages / "index.md").write_text(
        "\n".join(
            [
                "# 源码与文本文件",
                "",
                f"此目录提供 {len(files)} 个 UTF-8 源码或文本文件的只读浏览页。",
                "支持 Python、常见配置、Shell、PowerShell、JavaScript/TypeScript、"
                "HTML/CSS、SQL、R、Julia、Rust、Go、Java、C/C++、XML、LaTeX、CSV 和 diff。",
                "源码按原目录结构列在左侧导航中；隐藏文件、链接目录、常见构建目录、"
                "二进制文件和超过 1 MiB 的文件不会生成页面。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return len(files)
