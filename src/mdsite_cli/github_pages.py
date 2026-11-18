"""用临时 Git 工作区把静态网页发布到 GitHub Pages。"""

from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

_API_URL = "https://api.github.com"
_API_VERSION = "2026-03-10"
_PAGES_BRANCH = "gh-pages"
_PAGES_MARKER = ".mdsite-cli-pages"


class GitHubPagesError(RuntimeError):
    """发布或配置 GitHub Pages 时可直接展示的错误。"""


class _GitHubApiError(GitHubPagesError):
    """保留 HTTP 状态码，供缺少仓库或 Pages 配置判断。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def parse_repository(value: str) -> tuple[str, str]:
    """接受 owner/repo 或标准 GitHub HTTPS 仓库地址。"""
    raw = value.strip()
    if "://" in raw:
        try:
            parsed = urlparse(raw)
            port = parsed.port
        except ValueError as exc:
            raise GitHubPagesError("仓库地址不是有效的 GitHub HTTPS URL。") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"github.com", "www.github.com"}
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or parsed.query
            or parsed.fragment
        ):
            raise GitHubPagesError("仓库地址必须是 github.com 的 HTTPS URL。")
        segments = parsed.path.strip("/").split("/")
    else:
        segments = raw.strip("/").split("/")

    if len(segments) != 2:
        raise GitHubPagesError("仓库请写成 owner/repo 或 https://github.com/owner/repo。")
    owner, repository = segments
    if repository.endswith(".git"):
        repository = repository[:-4]
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
    if not owner or not repository or set(owner + repository) - allowed:
        raise GitHubPagesError("仓库名包含 GitHub 不支持的字符。")
    return owner, repository


def resolve_token(explicit: str | None, environment_name: str | None) -> str:
    """按显式值、指定环境变量、GH_TOKEN 和 gh 登录顺序取得凭据。"""
    if explicit and explicit.strip():
        return explicit.strip()

    if environment_name:
        token = os.environ.get(environment_name, "").strip()
        if not token:
            raise GitHubPagesError(f"环境变量 {environment_name} 未设置。")
        return token

    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(name, "").strip()
        if token:
            return token

    gh = shutil.which("gh")
    if gh is None:
        raise GitHubPagesError("未找到 GitHub 凭据。请先运行 gh auth login，或设置 GH_TOKEN。")
    try:
        result = subprocess.run(
            [gh, "auth", "token"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitHubPagesError(
            "读取 gh 登录凭据失败；请运行 gh auth status 检查登录状态。"
        ) from exc
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise GitHubPagesError("gh 尚未登录。请运行 gh auth login，或设置 GH_TOKEN 后重试。")
    return token


def _api_request(
    token: str,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    """发送 JSON 请求，错误消息不包含认证头或令牌。"""
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        _API_URL + path,
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "mdsite-cli",
            "X-GitHub-Api-Version": _API_VERSION,
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            response_body = response.read()
    except HTTPError as exc:
        try:
            error_body = json.loads(exc.read().decode("utf-8"))
            message = (
                error_body.get("message", "请求被拒绝")
                if isinstance(error_body, dict)
                else "请求被拒绝"
            )
        except (UnicodeError, json.JSONDecodeError):
            message = "请求被拒绝"
        raise _GitHubApiError(exc.code, f"GitHub API {exc.code}：{message}") from exc
    except URLError as exc:
        raise GitHubPagesError(f"无法连接 GitHub API：{exc.reason}") from exc

    if not response_body:
        return {}
    try:
        data = json.loads(response_body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GitHubPagesError("GitHub API 返回了无法解析的响应。") from exc
    if not isinstance(data, dict):
        raise GitHubPagesError("GitHub API 返回了意外的数据格式。")
    return data


def _repository_path(owner: str, repository: str) -> str:
    return f"/repos/{quote(owner, safe='')}/{quote(repository, safe='')}"


def _get_or_create_repository(
    owner: str,
    repository: str,
    token: str,
    create_repository: bool,
    description: str,
) -> dict[str, object]:
    """检查仓库公开性；需要时只创建公开仓库。"""
    path = _repository_path(owner, repository)
    try:
        repo = _api_request(token, "GET", path)
    except _GitHubApiError as exc:
        if exc.status != 404 or not create_repository:
            if exc.status == 404:
                raise GitHubPagesError(
                    "找不到仓库或当前凭据无权访问；确认仓库地址，或使用 --create-repository。"
                ) from exc
            raise

        account = _api_request(token, "GET", "/user")
        login = str(account.get("login", ""))
        if not login:
            raise GitHubPagesError("GitHub 未返回当前账号名，无法创建仓库。")
        create_path = (
            "/user/repos"
            if owner.casefold() == login.casefold()
            else f"/orgs/{quote(owner, safe='')}/repos"
        )
        create_payload: dict[str, object] = {
            "name": repository,
            "private": False,
            "auto_init": False,
        }
        if description:
            create_payload["description"] = description
        repo = _api_request(token, "POST", create_path, create_payload)

    if repo.get("private") is not False:
        raise GitHubPagesError("GitHub Pages 发布要求目标仓库为公开仓库。")
    clone_url = repo.get("clone_url")
    parsed_clone_url = urlparse(str(clone_url or ""))
    if parsed_clone_url.scheme != "https" or parsed_clone_url.hostname != "github.com":
        raise GitHubPagesError("GitHub API 未返回有效的 HTTPS clone 地址。")
    return repo


def _pages_match(pages: dict[str, object], branch: str) -> bool:
    source = pages.get("source")
    if not isinstance(source, dict):
        return False
    return (
        pages.get("build_type", "legacy") == "legacy"
        and source.get("branch") == branch
        and source.get("path") == "/"
    )


def _read_pages(
    token: str,
    owner: str,
    repository: str,
) -> dict[str, object] | None:
    try:
        return _api_request(token, "GET", f"{_repository_path(owner, repository)}/pages")
    except _GitHubApiError as exc:
        if exc.status == 404:
            return None
        raise


def _assert_pages_configuration(
    pages: dict[str, object] | None,
    branch: str,
    allow_reconfigure: bool,
) -> None:
    """避免悄悄覆盖仓库现有的 Pages 来源。"""
    if pages is None or _pages_match(pages, branch) or allow_reconfigure:
        return
    source = pages.get("source")
    if isinstance(source, dict):
        current = f"{source.get('branch', '?')}:{source.get('path', '?')}"
    else:
        current = str(pages.get("build_type", "workflow"))
    raise GitHubPagesError(
        f"仓库 Pages 已使用 {current}。确认切换到 gh-pages 根目录后，添加 --configure-pages。"
    )


def _ensure_pages_configuration(
    token: str,
    owner: str,
    repository: str,
    pages: dict[str, object] | None,
    branch: str,
    allow_reconfigure: bool,
) -> dict[str, object]:
    """创建或显式切换到 gh-pages 根目录发布。"""
    path = f"{_repository_path(owner, repository)}/pages"
    payload: dict[str, object] = {
        "build_type": "legacy",
        "source": {"branch": branch, "path": "/"},
    }
    if pages is None:
        try:
            return _api_request(token, "POST", path, payload)
        except _GitHubApiError as exc:
            if exc.status != 409:
                raise
            current = _read_pages(token, owner, repository)
            if current is not None and _pages_match(current, branch):
                return current
            raise GitHubPagesError("GitHub Pages 配置发生冲突，请检查仓库设置后重试。") from exc

    if _pages_match(pages, branch):
        return pages
    if not allow_reconfigure:
        _assert_pages_configuration(pages, branch, False)
    _api_request(token, "PUT", path, payload)
    updated = _read_pages(token, owner, repository)
    if updated is None:
        raise GitHubPagesError("GitHub 未保存 Pages 设置，请检查仓库权限后重试。")
    return updated


def _git_environment(
    token: str,
    token_environment: str | None = None,
) -> dict[str, str]:
    """把 Git 凭据放进子进程配置，不写入 remote 或仓库配置。"""
    environment = os.environ.copy()
    for name in ("GH_TOKEN", "GITHUB_TOKEN", token_environment):
        if name:
            environment.pop(name, None)
    try:
        index = int(environment.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        index = 0
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    environment[f"GIT_CONFIG_KEY_{index}"] = "http.https://github.com/.extraheader"
    environment[f"GIT_CONFIG_VALUE_{index}"] = f"AUTHORIZATION: basic {encoded}"
    environment["GIT_CONFIG_COUNT"] = str(index + 1)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "never"
    return environment


def _git(*arguments: str, environment: dict[str, str]) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise GitHubPagesError("发布到 GitHub Pages 需要安装 Git。")
    try:
        result = subprocess.run(
            [executable, *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=environment,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitHubPagesError("执行 Git 失败或超时，请检查 Git 安装和网络连接。") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        for key, value in environment.items():
            if key.startswith("GIT_CONFIG_VALUE_") and "AUTHORIZATION: basic " in value:
                detail = detail.replace(value, "AUTHORIZATION: basic [redacted]")
                detail = detail.replace(value.rsplit(" ", 1)[-1], "[redacted]")
        raise GitHubPagesError(f"GitHub Pages 分支更新失败：{detail}")
    return result.stdout.strip()


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        attributes = os.lstat(path).st_file_attributes
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _pages_marker(repository: str) -> str:
    return f"mdsite-cli-github-pages\nrepository={repository.casefold()}\nbranch={_PAGES_BRANCH}\n"


def _validate_built_site(site: Path) -> None:
    if not (site / "index.html").is_file():
        raise GitHubPagesError("静态网页没有 index.html，取消发布。")
    for path in site.rglob("*"):
        if path.name == ".git":
            raise GitHubPagesError("网页产物包含 .git 路径，不能安全发布。")
        if path.name == _PAGES_MARKER:
            raise GitHubPagesError(
                f"网页产物占用了保留文件 {_PAGES_MARKER}，请重命名源文件后重试。"
            )
        if _is_link(path):
            raise GitHubPagesError(f"网页产物包含链接，不能安全发布：{path.relative_to(site)}")


def _publish_branch(
    site: Path,
    owner: str,
    repository: str,
    clone_url: str,
    token: str,
    branch_exists: bool,
    token_environment: str | None = None,
) -> bool:
    """通过临时 Git 仓库替换受 mdsite 标记保护的 gh-pages 内容。"""
    _validate_built_site(site)
    full_name = f"{owner}/{repository}"
    marker_value = _pages_marker(full_name)
    environment = _git_environment(token, token_environment)

    with tempfile.TemporaryDirectory(prefix="mdsite-pages-") as directory:
        working_copy = Path(directory) / "pages"
        if branch_exists:
            _git(
                "clone",
                "--depth",
                "1",
                "--branch",
                _PAGES_BRANCH,
                "--single-branch",
                clone_url,
                str(working_copy),
                environment=environment,
            )
            marker = working_copy / _PAGES_MARKER
            if _is_link(marker) or not marker.is_file():
                raise GitHubPagesError(
                    "gh-pages 分支没有 mdsite 所有权标记；为保护现有内容，拒绝覆盖。"
                )
            try:
                existing_marker = marker.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise GitHubPagesError("无法读取 gh-pages 所有权标记，拒绝覆盖。") from exc
            if existing_marker != marker_value:
                raise GitHubPagesError(
                    "gh-pages 所有权标记与目标仓库不匹配；为保护现有内容，拒绝覆盖。"
                )
            if _git("-C", str(working_copy), "status", "--porcelain", environment=environment):
                raise GitHubPagesError("gh-pages 工作区不干净，拒绝覆盖。")
            _git(
                "-C",
                str(working_copy),
                "rm",
                "-r",
                "--ignore-unmatch",
                "--",
                ".",
                environment=environment,
            )
        else:
            working_copy.mkdir()
            _git("init", str(working_copy), environment=environment)
            _git(
                "-C",
                str(working_copy),
                "checkout",
                "--orphan",
                _PAGES_BRANCH,
                environment=environment,
            )
            _git(
                "-C",
                str(working_copy),
                "remote",
                "add",
                "origin",
                clone_url,
                environment=environment,
            )

        shutil.copytree(site, working_copy, dirs_exist_ok=True)
        (working_copy / ".nojekyll").touch()
        (working_copy / _PAGES_MARKER).write_text(marker_value, encoding="utf-8")
        _git("-C", str(working_copy), "config", "user.name", "mdsite-cli", environment=environment)
        _git(
            "-C",
            str(working_copy),
            "config",
            "user.email",
            "mdsite-cli@users.noreply.github.com",
            environment=environment,
        )
        _git("-C", str(working_copy), "add", "--all", "--force", environment=environment)
        executable = shutil.which("git")
        if executable is None:
            raise GitHubPagesError("发布到 GitHub Pages 需要安装 Git。")
        try:
            diff = subprocess.run(
                [executable, "-C", str(working_copy), "diff", "--cached", "--quiet"],
                capture_output=True,
                check=False,
                env=environment,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitHubPagesError("无法检查 gh-pages 更新内容。") from exc
        if diff.returncode == 0:
            return False
        if diff.returncode != 1:
            raise GitHubPagesError("无法检查 gh-pages 更新内容。")
        _git(
            "-C",
            str(working_copy),
            "commit",
            "-m",
            "Publish site with mdsite-cli",
            environment=environment,
        )
        _git(
            "-C",
            str(working_copy),
            "push",
            "origin",
            _PAGES_BRANCH,
            environment=environment,
        )
        return True


def _pages_url(pages: dict[str, object], owner: str, repository: str) -> str:
    html_url = pages.get("html_url")
    if isinstance(html_url, str) and html_url:
        return html_url.rstrip("/") + "/"
    domain = owner.casefold()
    if repository.casefold() == f"{domain}.github.io":
        return f"https://{domain}.github.io/"
    return f"https://{domain}.github.io/{repository}/"


def publish_site(
    site: Path,
    repository_value: str,
    *,
    explicit_token: str | None = None,
    token_environment: str | None = None,
    create_repository: bool = False,
    description: str = "",
    configure_pages: bool = False,
) -> str:
    """发布静态产物到公开仓库，并确保 Pages 使用 gh-pages 根目录。"""
    owner, repository = parse_repository(repository_value)
    token = resolve_token(explicit_token, token_environment)
    repo = _get_or_create_repository(owner, repository, token, create_repository, description)
    clone_url = str(repo["clone_url"])
    pages = _read_pages(token, owner, repository)
    _assert_pages_configuration(pages, _PAGES_BRANCH, configure_pages)

    try:
        _api_request(
            token,
            "GET",
            f"{_repository_path(owner, repository)}/branches/{quote(_PAGES_BRANCH, safe='')}",
        )
        branch_exists = True
    except _GitHubApiError as exc:
        if exc.status != 404:
            raise
        branch_exists = False

    changed = _publish_branch(
        site,
        owner,
        repository,
        clone_url,
        token,
        branch_exists,
        token_environment,
    )
    pages = _ensure_pages_configuration(
        token, owner, repository, pages, _PAGES_BRANCH, configure_pages
    )
    url = _pages_url(pages, owner, repository)
    state = "已更新" if changed else "内容无变化"
    print(f"GitHub Pages {state}：{url}")
    return url
