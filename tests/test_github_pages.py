"""验证 GitHub Pages 发布凭据、输入和分支安全。"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mdsite.cli import main
from mdsite.github_pages import (
    GitHubPagesError,
    _assert_pages_configuration,
    _get_or_create_repository,
    _git_environment,
    _GitHubApiError,
    _legacy_pages_marker,
    _pages_marker,
    _pages_url,
    _publish_branch,
    _validate_built_site,
    parse_repository,
    resolve_token,
)


class RepositoryInputTests(unittest.TestCase):
    """仓库参数仅接受 GitHub 的两段式地址。"""

    def test_accepts_owner_repository_and_https_url(self) -> None:
        self.assertEqual(parse_repository("owner/repo"), ("owner", "repo"))
        self.assertEqual(
            parse_repository("https://github.com/owner/repo.git/"),
            ("owner", "repo"),
        )

    def test_rejects_non_github_urls_and_extra_components(self) -> None:
        invalid = (
            "git@github.com:owner/repo.git",
            "https://example.com/owner/repo",
            "https://token@github.com/owner/repo",
            "https://github.com:444/owner/repo",
            "https://github.com:invalid/owner/repo",
            "https://github.com/owner/repo/tree/main",
            "owner/repo?tab=readme",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(GitHubPagesError):
                parse_repository(value)


class RepositoryApiTests(unittest.TestCase):
    """仓库创建只创建公开仓库，并保留用户给出的描述。"""

    def test_creates_public_repository_when_requested(self) -> None:
        repository = {
            "private": False,
            "clone_url": "https://github.com/owner/notes.git",
        }
        with patch(
            "mdsite.github_pages._api_request",
            side_effect=[
                _GitHubApiError(404, "missing"),
                {"login": "owner"},
                repository,
            ],
        ) as request:
            result = _get_or_create_repository(
                "owner",
                "notes",
                "in-memory-token",
                True,
                "Research notes",
            )

        self.assertEqual(result, repository)
        self.assertEqual(request.call_args_list[2].args[1], "POST")
        self.assertEqual(request.call_args_list[2].args[2], "/user/repos")
        self.assertEqual(
            request.call_args_list[2].args[3],
            {
                "name": "notes",
                "private": False,
                "auto_init": False,
                "description": "Research notes",
            },
        )

    def test_rejects_private_repository(self) -> None:
        with (
            patch(
                "mdsite.github_pages._api_request",
                return_value={"private": True, "clone_url": "https://github.com/owner/notes.git"},
            ),
            self.assertRaisesRegex(GitHubPagesError, "公开"),
        ):
            _get_or_create_repository("owner", "notes", "token", False, "")


class TokenTests(unittest.TestCase):
    """凭据优先使用显式来源，不输出或持久化令牌。"""

    def test_environment_token_precedence(self) -> None:
        with patch.dict(
            os.environ,
            {"GH_TOKEN": "standard", "GITHUB_TOKEN": "compat", "MD_TOKEN": "named"},
            clear=True,
        ):
            self.assertEqual(resolve_token(None, "MD_TOKEN"), "named")
            self.assertEqual(resolve_token(None, None), "standard")
            self.assertEqual(resolve_token("explicit", None), "explicit")

    def test_gh_token_is_used_when_environment_is_empty(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("mdsite.github_pages.shutil.which", return_value="gh"),
            patch(
                "mdsite.github_pages.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="from-gh\n", stderr=""),
            ) as run,
        ):
            self.assertEqual(resolve_token(None, None), "from-gh")
            self.assertEqual(run.call_args.args[0], ["gh", "auth", "token"])

    def test_named_environment_variable_must_exist(self) -> None:
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(GitHubPagesError):
            resolve_token(None, "MISSING_TOKEN")

    def test_git_credentials_stay_in_process_environment(self) -> None:
        token = "never-write-this-token"
        with patch.dict(
            os.environ,
            {"GIT_CONFIG_COUNT": "2", "GH_TOKEN": "inherited", "MD_TOKEN": "custom"},
            clear=True,
        ):
            environment = _git_environment(token, "MD_TOKEN")
        self.assertEqual(environment["GIT_CONFIG_COUNT"], "3")
        self.assertNotIn("GH_TOKEN", environment)
        self.assertNotIn("MD_TOKEN", environment)
        self.assertNotIn(token, environment["GIT_CONFIG_VALUE_2"])
        self.assertEqual(
            base64.b64decode(environment["GIT_CONFIG_VALUE_2"].rsplit(" ", 1)[1]),
            f"x-access-token:{token}".encode(),
        )
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")


class PagesConfigurationTests(unittest.TestCase):
    """配置冲突必须显式确认，URL 优先采用 GitHub 返回值。"""

    def test_refuses_to_switch_existing_pages_source_without_flag(self) -> None:
        pages = {"build_type": "legacy", "source": {"branch": "main", "path": "/docs"}}
        with self.assertRaises(GitHubPagesError):
            _assert_pages_configuration(pages, "gh-pages", False)
        _assert_pages_configuration(pages, "gh-pages", True)

    def test_pages_url_prefers_api_value(self) -> None:
        self.assertEqual(
            _pages_url({"html_url": "https://docs.example.test"}, "owner", "repo"),
            "https://docs.example.test/",
        )
        self.assertEqual(_pages_url({}, "owner", "repo"), "https://owner.github.io/repo/")


class BuiltSiteSafetyTests(unittest.TestCase):
    """阻止生成产物覆盖 Git 元数据或发布标记。"""

    def test_rejects_reserved_paths_in_site(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory)
            (site / "index.html").write_text("page", encoding="utf-8")
            for name in (".git", ".mdsite-pages", ".mdsite-cli-pages"):
                with self.subTest(name=name):
                    target = site / name
                    if name == ".git":
                        target.mkdir()
                    else:
                        target.write_text("user file", encoding="utf-8")
                    with self.assertRaises(GitHubPagesError):
                        _validate_built_site(site)
                    if target.is_dir():
                        target.rmdir()
                    else:
                        target.unlink()


@unittest.skipUnless(shutil.which("git"), "需要 Git 执行本地 bare repository 集成测试")
class PagesBranchTests(unittest.TestCase):
    """使用本地 bare Git 仓库覆盖部署分支生命周期，不访问网络。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.remote = root / "remote.git"
        self.site = root / "site"
        self.site.mkdir()
        (self.site / "index.html").write_text("first", encoding="utf-8")
        self._git("init", "--bare", str(self.remote))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *arguments: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            [shutil.which("git") or "git", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            self.fail(f"git 命令失败：{result.stderr}")
        return result.stdout.strip()

    def _deploy(
        self,
        owner: str = "owner",
        repository: str = "repo",
        exists: bool = False,
    ) -> bool:
        return _publish_branch(
            self.site,
            owner,
            repository,
            str(self.remote),
            "test-token",
            exists,
        )

    def _remote_file(self, path: str) -> str:
        return self._git("--git-dir", str(self.remote), "show", f"gh-pages:{path}")

    def test_publish_repeat_and_replace_without_stale_files(self) -> None:
        (self.site / "old.css").write_text("old", encoding="utf-8")
        (self.site / ".gitignore").write_text("*\n", encoding="utf-8")
        self.assertTrue(self._deploy())
        self.assertEqual(self._remote_file("index.html"), "first")
        self.assertEqual(self._remote_file("old.css"), "old")
        self.assertEqual(self._remote_file(".gitignore"), "*")

        self.assertFalse(self._deploy(exists=True))
        (self.site / "index.html").write_text("second", encoding="utf-8")
        (self.site / "old.css").unlink()
        self.assertTrue(self._deploy(exists=True))
        self.assertEqual(self._remote_file("index.html"), "second")
        result = subprocess.run(
            [
                shutil.which("git") or "git",
                "--git-dir",
                str(self.remote),
                "cat-file",
                "-e",
                "gh-pages:old.css",
            ],
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_refuses_existing_branch_without_matching_marker(self) -> None:
        repository = Path(self.temporary.name) / "initial"
        repository.mkdir()
        self._git("init", str(repository))
        self._git("checkout", "-b", "gh-pages", cwd=repository)
        self._git("config", "user.name", "test", cwd=repository)
        self._git("config", "user.email", "test@example.test", cwd=repository)
        (repository / "README.md").write_text("keep me", encoding="utf-8")
        self._git("add", "README.md", cwd=repository)
        self._git("commit", "-m", "existing page", cwd=repository)
        self._git("remote", "add", "origin", str(self.remote), cwd=repository)
        self._git("push", "origin", "gh-pages", cwd=repository)

        with self.assertRaisesRegex(GitHubPagesError, "拒绝覆盖"):
            self._deploy(exists=True)
        self.assertEqual(self._remote_file("README.md"), "keep me")

    def test_refuses_a_marker_owned_by_another_repository(self) -> None:
        self.assertTrue(self._deploy(repository="first"))
        with self.assertRaisesRegex(GitHubPagesError, "不匹配"):
            self._deploy(repository="second", exists=True)
        self.assertEqual(self._remote_file("index.html"), "first")

    def test_upgrades_legacy_marker_without_losing_site(self) -> None:
        repository = Path(self.temporary.name) / "legacy"
        repository.mkdir()
        self._git("init", str(repository))
        self._git("checkout", "-b", "gh-pages", cwd=repository)
        self._git("config", "user.name", "test", cwd=repository)
        self._git("config", "user.email", "test@example.test", cwd=repository)
        (repository / ".mdsite-cli-pages").write_text(
            _legacy_pages_marker("owner/repo"), encoding="utf-8"
        )
        (repository / "index.html").write_text("old site", encoding="utf-8")
        self._git("add", "--all", cwd=repository)
        self._git("commit", "-m", "old mdsite deployment", cwd=repository)
        self._git("remote", "add", "origin", str(self.remote), cwd=repository)
        self._git("push", "origin", "gh-pages", cwd=repository)

        self.assertTrue(self._deploy(exists=True))

        self.assertEqual(self._remote_file("index.html"), "first")
        self.assertEqual(self._remote_file(".mdsite-pages"), _pages_marker("owner/repo").strip())
        result = subprocess.run(
            [
                shutil.which("git") or "git",
                "--git-dir",
                str(self.remote),
                "cat-file",
                "-e",
                "gh-pages:.mdsite-cli-pages",
            ],
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)


@unittest.skipUnless(shutil.which("git"), "需要 Git 执行本地发布集成测试")
class PublishCommandIntegrationTests(unittest.TestCase):
    """真实构建并发布到本地 bare 仓库，模拟 GitHub API，不访问网络。"""

    def test_builds_markdown_and_publishes_static_site(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "notes"
            source.mkdir()
            (source / "README.md").write_text(
                "# Local Pages\n\nA published note.\n", encoding="utf-8"
            )
            remote = root / "remote.git"
            git = shutil.which("git") or "git"
            subprocess.run([git, "init", "--bare", str(remote)], check=True, capture_output=True)
            api_calls: list[tuple[str, str]] = []

            def fake_api(
                _token: str,
                method: str,
                path: str,
                _payload: dict[str, object] | None = None,
            ) -> dict[str, object]:
                api_calls.append((method, path))
                if method == "GET" and path.endswith("/branches/gh-pages"):
                    raise _GitHubApiError(404, "branch not found")
                if method == "POST" and path.endswith("/pages"):
                    return {"html_url": "https://owner.github.io/notes/"}
                self.fail(f"未预期的 GitHub API 请求：{method} {path}")

            with (
                patch("mdsite.cli.resolve_token", return_value="in-memory-token"),
                patch(
                    "mdsite.github_pages._get_or_create_repository",
                    return_value={"private": False, "clone_url": str(remote)},
                ),
                patch("mdsite.github_pages._read_pages", return_value=None),
                patch("mdsite.github_pages._api_request", side_effect=fake_api),
            ):
                result = main(["publish", "--path", str(source), "--repository", "owner/notes"])

            self.assertEqual(result, 0)
            self.assertIn(("POST", "/repos/owner/notes/pages"), api_calls)
            published_files = subprocess.run(
                [git, "--git-dir", str(remote), "ls-tree", "--name-only", "-r", "gh-pages"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            self.assertIn("index.html", published_files)
            self.assertTrue(
                any(
                    path.startswith("source/") and path.endswith(".html")
                    for path in published_files
                )
            )
            self.assertEqual(
                (source / "README.md").read_text(encoding="utf-8"),
                "# Local Pages\n\nA published note.\n",
            )


if __name__ == "__main__":
    unittest.main()
