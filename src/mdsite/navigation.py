"""把 Markdown 与源码页合并到原目录导航中。"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .source_pages import source_markdown_paths

NavValue: TypeAlias = str | list["NavValue"] | dict[str, "NavValue"]
_INDEX_NAMES = {"index.md", "readme.md"}
_INLINE_LINK = re.compile(r"(?P<prefix>\]\()(?P<target><[^>\r\n]*>|[^)\s]+)")
_REFERENCE_LINK = re.compile(r"(?m)(?P<prefix>^\s*\[[^\]]+\]:\s*)(?P<target><[^>\r\n]*>|[^\s]+)")
_HTML_LINK = re.compile(
    r"(?P<prefix>\b(?:href|src)\s*=\s*)(?P<quote>['\"])(?P<target>.*?)(?P=quote)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Page:
    """保存导航页面的来源、排序与显示值。"""

    relative: Path
    kind: str
    priority: int
    value: NavValue


@dataclass
class _Directory:
    """保存一个目录下的页面和子目录。"""

    pages: list[_Page] = field(default_factory=list)
    children: dict[str, _Directory] = field(default_factory=dict)


def _insert(
    root: _Directory,
    relative: Path,
    entry: NavValue,
    priority: int,
    kind: str,
) -> None:
    """按源文件的父目录插入页面。"""
    directory = root
    for part in relative.parts[:-1]:
        directory = directory.children.setdefault(part, _Directory())
    directory.pages.append(_Page(relative, kind, priority, entry))


def _directory_title(name: str) -> str:
    """让编号目录在导航中以空格分隔编号和标题。"""
    if name.casefold() == "examples":
        return "Examples"
    match = re.fullmatch(r"(\d+)-(.+)", name)
    return f"{match.group(1)} {match.group(2)}" if match else name


def _index_page(directory: _Directory) -> _Page | None:
    """优先选择 README，其次选择 index 作为目录入口。"""
    return next(
        (
            page
            for name in ("readme.md", "index.md")
            for page in directory.pages
            if page.kind == "markdown" and page.relative.name.casefold() == name
        ),
        None,
    )


def _source_link_target(target: str) -> str:
    """把首页中的源目录相对链接映射到临时 source 路径。"""
    enclosed = target.startswith("<") and target.endswith(">")
    raw = target[1:-1] if enclosed else target
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc or not parts.path or parts.path.startswith("/"):
        return target

    decoded_path = unquote(parts.path)
    normalized = posixpath.normpath(decoded_path)
    if normalized == ".." or normalized.startswith("../"):
        return target

    relative_path = parts.path
    while relative_path.startswith("./"):
        relative_path = relative_path[2:]
    mapped = urlunsplit(
        ("", "", posixpath.join("source", relative_path), parts.query, parts.fragment)
    )
    return f"<{mapped}>" if enclosed else mapped


def _rewrite_line_links(line: str) -> str:
    """改写首页的 Markdown 与 HTML 本地链接，保留行内代码。"""
    backtick = chr(96)
    inline_code = re.compile("(" + backtick + r"+).*?\1")
    saved: list[str] = []

    def hide_code(match: re.Match[str]) -> str:
        marker = f"\0MDSITE_CODE_{len(saved)}\0"
        saved.append(match.group(0))
        return marker

    protected = inline_code.sub(hide_code, line)
    for pattern in (_INLINE_LINK, _REFERENCE_LINK, _HTML_LINK):

        def rewrite(match: re.Match[str]) -> str:
            prefix = match.group("prefix")
            target = _source_link_target(match.group("target"))
            quote_char = match.groupdict().get("quote")
            return (
                f"{prefix}{quote_char}{target}{quote_char}" if quote_char else f"{prefix}{target}"
            )

        protected = pattern.sub(rewrite, protected)
    for index, code in enumerate(saved):
        protected = protected.replace(f"\0MDSITE_CODE_{index}\0", code)
    return protected


def _rewrite_homepage_links(markdown: str) -> str:
    """只改写普通 Markdown 行，避免触碰代码围栏中的示例。"""
    backtick = chr(96)
    fence: tuple[str, int] | None = None
    rewritten: list[str] = []
    for line in markdown.splitlines(keepends=True):
        stripped = line.lstrip()
        marker_char = stripped[:1]
        marker_length = len(stripped) - len(stripped.lstrip(marker_char)) if marker_char else 0
        is_fence = marker_char in {backtick, "~"} and marker_length >= 3
        if is_fence:
            if fence is None:
                fence = (marker_char, marker_length)
            elif marker_char == fence[0] and marker_length >= fence[1]:
                fence = None
            rewritten.append(line)
        elif fence is None:
            rewritten.append(_rewrite_line_links(line))
        else:
            rewritten.append(line)
    return "".join(rewritten)


def _page_route(page: _Page) -> str:
    """返回页面在临时文档树中的路径。"""
    if page.kind == "source":
        return (Path("files") / page.relative.parent / f"{page.relative.name}.md").as_posix()
    if page.kind == "generated":
        return page.relative.as_posix()
    return (Path("source") / page.relative).as_posix()


def _relative_link(page: Path, target: Path) -> str:
    """生成适用于 GitHub Pages 子路径部署的相对链接。"""
    relative = posixpath.relpath(target.as_posix(), start=page.parent.as_posix())
    return quote(relative, safe="/-._~")


def _directory_index_route(relative: Path) -> Path:
    """返回不修改源目录的临时目录索引路径。"""
    return Path("directory-index", *relative.parts, "index.md")


def _write_listing(
    directory: _Directory,
    relative: Path,
    page: Path,
    source_root_name: str,
    docs: Path,
) -> None:
    """为缺少 README 的目录生成临时内容清单。"""
    title = source_root_name if not relative.parts else _directory_title(relative.name)
    rows: list[str] = [f"# {title}", ""]
    entries: list[tuple[str, str, Path]] = []

    for name, child in sorted(directory.children.items(), key=lambda item: item[0].casefold()):
        child_relative = relative / name
        index = _index_page(child)
        target = (
            Path("source") / index.relative
            if index is not None
            else _directory_index_route(child_relative)
        )
        entries.append(("folder", _directory_title(name), target))

    for item in directory.pages:
        label = item.relative.stem if item.kind == "markdown" else item.relative.name
        entries.append(("file", label, Path(_page_route(item))))

    entries.sort(key=lambda entry: (0 if entry[0] == "folder" else 1, entry[1].casefold()))
    if not entries:
        rows.append("此目录中没有可浏览的 Markdown 或文本源码文件。")
    else:
        rows.extend(f"- [{label}]({_relative_link(page, target)})" for _, label, target in entries)

    destination = docs / page
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _add_directory_indexes(
    directory: _Directory,
    relative: Path,
    source_root_name: str,
    docs: Path,
) -> None:
    """为没有 README/index 的子目录递归生成入口页。"""
    for name, child in sorted(directory.children.items(), key=lambda item: item[0].casefold()):
        child_relative = relative / name
        _add_directory_indexes(child, child_relative, source_root_name, docs)
        if _index_page(child) is None:
            route = _directory_index_route(child_relative)
            _write_listing(child, child_relative, route, source_root_name, docs)
            child.pages.append(_Page(route, "generated", -1, route.as_posix()))


def _render(directory: _Directory) -> list[NavValue]:
    """递归生成 Zensical 的嵌套导航项。"""
    entries: list[NavValue] = [
        page.value
        for page in sorted(
            directory.pages,
            key=lambda page: (page.priority, page.relative.name.casefold()),
        )
    ]
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


def build_navigation(
    source: Path,
    source_pages: list[Path],
    docs: Path,
) -> list[NavValue]:
    """创建首页与目录入口，并生成源码目录的嵌套导航。"""
    root = _Directory()
    markdown_paths = source_markdown_paths(source)
    for relative in markdown_paths:
        route = (Path("source") / relative).as_posix()
        filename = relative.name.casefold()
        is_index = filename in _INDEX_NAMES
        entry = route if is_index else {relative.stem: route}
        priority = -1 if filename == "readme.md" else 0 if filename == "index.md" else 1
        _insert(root, relative, entry, priority, "markdown")

    for relative in source_pages:
        route = (Path("files") / relative.parent / f"{relative.name}.md").as_posix()
        _insert(root, relative, {relative.name: route}, 2, "source")

    home = _index_page(root)
    home_path = docs / "index.md"
    if home is not None:
        content = (source / home.relative).read_text(encoding="utf-8-sig")
        home_path.write_text(_rewrite_homepage_links(content), encoding="utf-8")
        root.pages.remove(home)
    else:
        _write_listing(root, Path(), Path("index.md"), source.name or "Markdown", docs)

    _add_directory_indexes(root, Path(), source.name or "Markdown", docs)
    return ["index.md", *_render(root)]


def toml_navigation(entries: list[NavValue]) -> str:
    """生成可读的 TOML 导航配置。"""
    lines = ["nav = ["]
    lines.extend(f"  {_toml_value(entry)}," for entry in entries)
    lines.append("]")
    return "\n".join(lines)
