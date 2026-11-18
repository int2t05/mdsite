"""把当前 Markdown 目录临时挂接到 Zensical，提供预览和静态构建。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import webbrowser
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from .github_pages import GitHubPagesError, publish_site, resolve_token

_MARKER_NAME = ".mdsite-generated"


class MdsiteError(RuntimeError):
    """命令执行时可直接展示给用户的错误。"""


def _toml_string(value: str) -> str:
    """用 TOML 基础字符串表示路径和标题。"""
    return json.dumps(value, ensure_ascii=False)


def _source_path(value: Path | None) -> Path:
    """解析并校验 Markdown 根目录。"""
    candidate = value if value is not None else Path.cwd()
    try:
        source = candidate.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MdsiteError(f"目录不存在或无法访问：{candidate}") from exc
    if not source.is_dir():
        raise MdsiteError(f"指定路径不是目录：{source}")
    return source


def _create_directory_link(link_path: Path, target_path: Path) -> None:
    """在临时项目中创建指向原目录的链接。"""
    if os.name != "nt":
        link_path.symlink_to(target_path, target_is_directory=True)
        return

    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if powershell is None:
        raise MdsiteError("未找到 PowerShell，无法创建 Windows 目录联接。")

    # 单引号字面量会把路径作为数据传入 PowerShell，避免创建临时脚本。
    link_literal = "'{}'".format(str(link_path).replace("'", "''"))
    target_literal = "'{}'".format(str(target_path).replace("'", "''"))
    command = (
        f"New-Item -ItemType Junction -Path {link_literal} -Target {target_literal} | Out-Null"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise MdsiteError(f"创建目录联接失败：{detail}")


def _write_workspace(
    source: Path,
    workspace: Path,
    output_to_exclude: Path | None,
) -> Path:
    """生成仅存在于临时目录中的 Zensical 项目。"""
    docs = workspace / "docs"
    docs.mkdir(parents=True)
    _create_directory_link(docs / "source", source)

    overrides = workspace / "overrides"
    overrides.mkdir()
    bundled_template = Path(__file__).parent / "assets" / "main.html"
    shutil.copyfile(bundled_template, overrides / "main.html")

    title = source.name or "Markdown"
    intro = [
        f"# {title}",
        "",
        "此站点直接读取原目录中的 Markdown 文件，不会复制或改写源文件。",
        "",
        "请从左侧导航的 **source** 分类浏览文档。",
    ]
    readme = source / "README.md"
    if readme.is_file():
        intro.extend(["", f"[打开 README](source/{quote(readme.name)})"])
    (docs / "index.md").write_text("\n".join(intro) + "\n", encoding="utf-8")

    excluded = ["/**", "!/source/**", "!/index.md"]
    if output_to_exclude is not None:
        relative_output = output_to_exclude.relative_to(source).as_posix()
        excluded.append(f"/source/{relative_output}/**")

    config = workspace / "zensical.toml"
    config.write_text(
        "\n".join(
            [
                "[project]",
                f"site_name = {_toml_string(title)}",
                'docs_dir = "docs"',
                'site_dir = "site"',
                f"watch = [{_toml_string(str(source))}]",
                "exclude_docs = {}".format(_toml_string("\n".join(excluded))),
                'extra_javascript = ["https://unpkg.com/mathjax@3/es5/tex-mml-chtml.js"]',
                "",
                "[project.theme]",
                'language = "zh"',
                'custom_dir = "overrides"',
                "",
                "[project.markdown_extensions]",
                "pymdownx.arithmatex.generic = true",
                "pymdownx.superfences.custom_fences = [",
                '  { name = "mermaid", class = "mermaid", '
                'format = "pymdownx.superfences.fence_code_format" },',
                "]",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config


@contextmanager
def _project_workspace(
    source: Path,
    output_to_exclude: Path | None = None,
) -> Iterator[tuple[Path, Path]]:
    """运行期间保留临时配置；先移除目录联接，再清理临时文件。"""
    workspace = Path(tempfile.mkdtemp(prefix="mdsite-"))
    link_path = workspace / "docs" / "source"
    try:
        config = _write_workspace(source, workspace, output_to_exclude)
        yield workspace, config
    finally:
        if os.path.lexists(link_path):
            try:
                if os.name == "nt":
                    # Windows 上 rmdir 删除联接本身，不触碰原目录内容。
                    os.rmdir(link_path)
                else:
                    link_path.unlink()
            except OSError as exc:
                raise MdsiteError(
                    f"无法安全移除临时目录链接；为保护源文件，保留临时目录：{workspace}"
                ) from exc
        shutil.rmtree(workspace)


def _zensical_command(*arguments: str) -> list[str]:
    """定位当前 uvx 环境中的 Zensical 命令。"""
    executable = shutil.which("zensical")
    if executable is None:
        raise MdsiteError("找不到 Zensical 命令，请检查 mdsite 的安装环境。")
    return [executable, *arguments]


def _run_preview(source: Path, port: int, open_browser: bool) -> int:
    default_output = source / "site"
    output_to_exclude = default_output if _has_output_marker(default_output, source) else None
    with _project_workspace(source, output_to_exclude) as (workspace, config):
        command = _zensical_command(
            "serve",
            "--config-file",
            str(config),
            "--dev-addr",
            f"127.0.0.1:{port}",
        )
        if open_browser:
            command.append("--open")
        return subprocess.run(command, cwd=workspace, check=False).returncode


def _is_reparse_point(path: Path) -> bool:
    """识别 Windows 联接点，兼容 Python 3.11。"""
    if os.name != "nt" or not os.path.lexists(path):
        return False
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None:
        return is_junction()
    try:
        attributes = os.lstat(path).st_file_attributes
    except OSError:
        return False
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _resolve_output(source: Path, requested: Path | None) -> tuple[Path, Path | None]:
    """限制输出位置，避免覆盖源目录或把构建结果再次打包。"""
    source = source.resolve(strict=True)
    output = (requested if requested is not None else source / "site").expanduser()
    if not output.is_absolute():
        output = source / output
    output = output.absolute()
    if os.path.lexists(output):
        if output.is_symlink() or _is_reparse_point(output):
            raise MdsiteError(f"输出目录不能是符号链接或目录联接：{output}")
    resolved = output.resolve(strict=False)

    if resolved == source or source.is_relative_to(resolved):
        raise MdsiteError("输出目录不能等于或包含 Markdown 根目录。")

    default_output = (source / "site").resolve(strict=False)
    if resolved.is_relative_to(source) and resolved != default_output:
        raise MdsiteError(
            "自定义输出目录请放在 Markdown 根目录之外；默认目录 site/ 可以放在根目录内。"
        )

    excluded = resolved if resolved == default_output else None
    return output, excluded


def _marker_value(output: Path, source: Path) -> str:
    """生成绑定源目录和输出位置的归属标记。"""
    output_key = os.path.normcase(str(output.resolve(strict=False)))
    source_key = os.path.normcase(str(source.resolve(strict=True)))
    digest = hashlib.sha256(f"{source_key}\0{output_key}".encode())
    return f"mdsite\n{digest.hexdigest()}\n"


def _legacy_marker_value(output: Path, source: Path) -> str:
    """识别旧版本写入的本地构建归属标记。"""
    output_key = os.path.normcase(str(output.resolve(strict=False)))
    source_key = os.path.normcase(str(source.resolve(strict=True)))
    digest = hashlib.sha256(f"{source_key}\0{output_key}".encode())
    return f"mdsite-cli\n{digest.hexdigest()}\n"


def _has_output_marker(output: Path, source: Path) -> bool:
    """判断目录是否带有本工具写入的归属标记。"""
    if not os.path.lexists(output):
        return False
    if output.is_symlink() or _is_reparse_point(output) or not output.is_dir():
        return False
    marker = output / _MARKER_NAME
    try:
        return marker.is_file() and marker.read_text(encoding="utf-8") in {
            _marker_value(output, source),
            _legacy_marker_value(output, source),
        }
    except (OSError, UnicodeError):
        return False


def _owned_output(output: Path, source: Path) -> bool:
    """只允许覆盖本工具之前生成的普通目录。"""
    if not os.path.lexists(output):
        return False
    if output.is_symlink() or _is_reparse_point(output) or not output.is_dir():
        raise MdsiteError(f"输出路径已存在且不是可安全替换的 mdsite 目录：{output}")
    if not _has_output_marker(output, source):
        raise MdsiteError(f"输出目录已存在且不是由 mdsite 生成；请更换 --output：{output}")
    return True


def _publish_site(built_site: Path, output: Path, source: Path) -> None:
    """复制构建结果到目标目录，并保留已有非工具目录。"""
    output.parent.mkdir(parents=True, exist_ok=True)
    had_output = _owned_output(output, source)
    suffix = uuid.uuid4().hex
    staging = output.parent / f".{output.name}.mdsite-stage-{suffix}"
    backup = output.parent / f".{output.name}.mdsite-backup-{suffix}"

    try:
        shutil.copytree(built_site, staging)
        (staging / _MARKER_NAME).write_text(_marker_value(output, source), encoding="utf-8")
        if had_output:
            output.rename(backup)
        try:
            staging.rename(output)
        except OSError:
            if had_output and backup.exists() and not output.exists():
                backup.rename(output)
            raise
        if had_output:
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _build_and_use(
    source: Path,
    output_to_exclude: Path | None,
    consume_site: Callable[[Path], None],
    environment: Mapping[str, str] | None = None,
) -> int:
    """构建到临时目录，并在目录清理前交给调用方使用。"""
    with _project_workspace(source, output_to_exclude) as (workspace, config):
        command = _zensical_command("build", "--config-file", str(config))
        result = subprocess.run(command, cwd=workspace, check=False, env=environment)
        if result.returncode != 0:
            return result.returncode
        built_site = workspace / "site"
        if not (built_site / "index.html").is_file():
            raise MdsiteError("Zensical 构建结束，但没有生成 index.html。")
        consume_site(built_site)
    return 0


def _run_build(source: Path, requested_output: Path | None) -> int:
    output, output_to_exclude = _resolve_output(source, requested_output)
    _owned_output(output, source)
    result = _build_and_use(
        source,
        output_to_exclude,
        lambda built_site: _publish_site(built_site, output, source),
    )
    if result != 0:
        return result
    print(f"静态网页已生成：{output}")
    return 0


def _run_publish(source: Path, args: argparse.Namespace) -> int:
    """构建当前 Markdown 目录，并发布到指定公开仓库。"""
    token = resolve_token(None, args.token_env)
    build_environment = os.environ.copy()
    for name in ("GH_TOKEN", "GITHUB_TOKEN", args.token_env):
        if name:
            build_environment.pop(name, None)
    default_output = source / "site"
    output_to_exclude = default_output if _has_output_marker(default_output, source) else None
    published_url: list[str] = []

    def publish(built_site: Path) -> None:
        url = publish_site(
            built_site,
            args.repository,
            explicit_token=token,
            token_environment=args.token_env,
            create_repository=args.create_repository,
            description=args.description,
            configure_pages=args.configure_pages,
        )
        published_url.append(url)

    result = _build_and_use(
        source,
        output_to_exclude,
        publish,
        environment=build_environment,
    )
    if result != 0:
        return result
    if args.open and published_url and not webbrowser.open(published_url[0]):
        print(f"无法自动打开浏览器，请手动访问：{published_url[0]}")
    return 0


def _port(value: str) -> int:
    """校验本地预览端口。"""
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("端口必须是整数") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口范围为 1–65535")
    return port


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mdsite",
        description="将当前目录的 Markdown 文件预览或构建成网站。",
    )
    parser.add_argument("--path", type=Path, default=None, help="Markdown 根目录，默认当前目录")
    commands = parser.add_subparsers(dest="command")

    serve = commands.add_parser("serve", help="启动本地预览（默认操作）")
    serve.add_argument(
        "--path",
        type=Path,
        default=argparse.SUPPRESS,
        help="Markdown 根目录，默认当前目录",
    )
    serve.add_argument("--port", type=_port, default=8000, help="本地端口，默认 8000")
    serve.add_argument("--no-open", action="store_true", help="启动后不打开浏览器")

    build = commands.add_parser("build", help="生成静态网页")
    build.add_argument(
        "--path",
        type=Path,
        default=argparse.SUPPRESS,
        help="Markdown 根目录，默认当前目录",
    )
    build.add_argument("--output", type=Path, help="输出目录，默认根目录下的 site/")

    publish = commands.add_parser(
        "publish",
        help="构建并发布到 GitHub Pages（公开网站）",
        description="将当前 Markdown 目录构建后发布到公开 GitHub 仓库的 gh-pages 分支。",
    )
    publish.add_argument(
        "--path",
        type=Path,
        default=argparse.SUPPRESS,
        help="Markdown 根目录，默认当前目录",
    )
    publish.add_argument(
        "--repository",
        "--repo",
        required=True,
        help="目标仓库，格式为 owner/repo 或 GitHub HTTPS URL",
    )
    publish.add_argument(
        "--token-env",
        metavar="NAME",
        help="从指定环境变量读取 GitHub token；默认读取 GH_TOKEN、GITHUB_TOKEN 或 gh 登录",
    )
    publish.add_argument(
        "--create-repository",
        action="store_true",
        help="仓库不存在时创建公开仓库",
    )
    publish.add_argument("--description", default="", help="新建仓库的描述")
    publish.add_argument(
        "--configure-pages",
        action="store_true",
        help="确认后将现有 Pages 来源切换到 gh-pages 根目录",
    )
    publish.add_argument("--open", action="store_true", help="发布后尝试打开网站")

    return parser


def _configure_console() -> None:
    """让非 UTF-8 控制台转义不可编码字符，避免命令意外崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (OSError, ValueError):
                pass


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    args = _parser().parse_args(argv)
    try:
        source = _source_path(args.path)
        if args.command == "build":
            return _run_build(source, args.output)
        if args.command == "publish":
            return _run_publish(source, args)
        return _run_preview(
            source,
            getattr(args, "port", 8000),
            not getattr(args, "no_open", False),
        )
    except MdsiteError as exc:
        print(f"mdsite: {exc}", file=sys.stderr)
        return 2
    except GitHubPagesError as exc:
        print(f"mdsite: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
