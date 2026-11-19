"""把 Markdown 与源码页合并到原目录导航中。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias

from .source_pages import source_markdown_paths

NavValue: TypeAlias = str | list["NavValue"] | dict[str, "NavValue"]


@dataclass
class _Directory:
    """保存一个目录下的页面和子目录。"""

    pages: list[tuple[int, str, NavValue]] = field(default_factory=list)
    children: dict[str, _Directory] = field(default_factory=dict)


def _insert(root: _Directory, relative: Path, entry: NavValue, priority: int) -> None:
    """按源文件的父目录插入页面。"""
    directory = root
    for part in relative.parts[:-1]:
        directory = directory.children.setdefault(part, _Directory())
    directory.pages.append((priority, relative.name.casefold(), entry))


def _directory_title(name: str) -> str:
    """让编号目录在导航中以空格分隔编号和标题。"""
    if name.casefold() == "examples":
        return "Examples"
    match = re.fullmatch(r"(\d+)-(.+)", name)
    return f"{match.group(1)} {match.group(2)}" if match else name


def _render(directory: _Directory) -> list[NavValue]:
    """递归生成 Zensical 的嵌套导航项。"""
    entries = [entry for _, _, entry in sorted(directory.pages)]
    for name, child in sorted(directory.children.items(), key=lambda item: item[0].casefold()):
        entries.append({_directory_title(name): _render(child)})
    return entries


def _toml_value(value: NavValue) -> str:
    """序列化导航使用的 TOML 字符串、数组和内联表。"""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        fields = ", ".join(
            f"{json.dumps(key, ensure_ascii=False)} = {_toml_value(item)}"
            for key, item in value.items()
        )
        return "{ " + fields + " }"
    raise TypeError(f"不支持的导航值：{type(value).__name__}")


def build_navigation(source: Path, source_pages: list[Path]) -> list[NavValue]:
    """将源码浏览页插入其原目录对应的 Markdown 导航节。"""
    root = _Directory()
    for relative in source_markdown_paths(source):
        route = (Path("source") / relative).as_posix()
        priority = 0 if relative.name.casefold() in {"index.md", "readme.md"} else 1
        _insert(root, relative, route, priority)

    for relative in source_pages:
        route = (Path("files") / relative.parent / f"{relative.name}.md").as_posix()
        _insert(root, relative, {relative.name: route}, 2)

    return ["index.md", *_render(root)]


def toml_navigation(entries: list[NavValue]) -> str:
    """生成可读的 TOML 导航配置。"""
    lines = ["nav = ["]
    lines.extend(f"  {_toml_value(entry)}," for entry in entries)
    lines.append("]")
    return "\n".join(lines)
