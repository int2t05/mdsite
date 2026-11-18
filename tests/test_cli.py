"""覆盖目录链接、输出归属和实际静态构建。"""

from __future__ import annotations

import io
import tempfile
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path
from unittest.mock import patch

from mdsite_cli.cli import (
    MdsiteError,
    _marker_value,
    _owned_output,
    _parser,
    _project_workspace,
    _publish_site,
    _resolve_output,
    main,
)


class WorkspaceTests(unittest.TestCase):
    """验证临时工作区只链接源目录。"""

    def test_workspace_links_source_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "notes ' $ &"
            source.mkdir()
            (source / "README.md").write_text("# Notes\n", encoding="utf-8")

            with _project_workspace(source) as (workspace, config):
                source_link = workspace / "docs" / "source"
                self.assertTrue(source_link.is_dir())
                self.assertEqual(source_link.resolve(), source.resolve())
                self.assertTrue((source_link / "README.md").is_file())
                self.assertTrue(config.is_file())
                self.assertFalse((source / "site").exists())

            self.assertFalse(workspace.exists())
            self.assertTrue((source / "README.md").is_file())


class OutputSafetyTests(unittest.TestCase):
    """验证构建结果只能覆盖同一路径的本工具产物。"""

    def test_existing_unowned_output_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "notes"
            output = source / "site"
            output.mkdir(parents=True)
            sentinel = output / "keep.txt"
            sentinel.write_text("user data", encoding="utf-8")

            with self.assertRaises(MdsiteError):
                _owned_output(output, source)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "user data")

    def test_malformed_marker_is_treated_as_unowned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "notes"
            source.mkdir()
            output = source / "site"
            output.mkdir()
            (output / ".mdsite-generated").write_bytes(b"\xff")

            with self.assertRaises(MdsiteError):
                _owned_output(output, source)

    def test_marker_is_bound_to_source_and_output_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "notes"
            source.mkdir()
            output = source / "site"
            output.mkdir()
            marker = output / ".mdsite-generated"
            marker.write_text(_marker_value(output, source), encoding="utf-8")

            self.assertTrue(_owned_output(output, source))
            with self.assertRaises(MdsiteError):
                _owned_output(output, root / "other-notes")

            copied_output = root / "copied-site"
            copied_output.mkdir()
            (copied_output / ".mdsite-generated").write_text(
                marker.read_text(encoding="utf-8"), encoding="utf-8"
            )
            with self.assertRaises(MdsiteError):
                _owned_output(copied_output, source)

    def test_output_rejects_source_and_nested_custom_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "notes"
            source.mkdir()

            with self.assertRaises(MdsiteError):
                _resolve_output(source, source)
            with self.assertRaises(MdsiteError):
                _resolve_output(source, source / "generated")

            output, excluded = _resolve_output(source, None)
            self.assertEqual(output, source.resolve() / "site")
            self.assertEqual(excluded, output)

    def test_owned_site_is_replaced_without_stale_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "notes"
            source.mkdir()
            built = root / "built"
            built.mkdir()
            output = root / "published"

            (built / "index.html").write_text("first", encoding="utf-8")
            _publish_site(built, output, source)
            self.assertEqual((output / "index.html").read_text(encoding="utf-8"), "first")

            (output / "old.html").write_text("stale", encoding="utf-8")
            (built / "index.html").write_text("second", encoding="utf-8")
            _publish_site(built, output, source)

            self.assertEqual((output / "index.html").read_text(encoding="utf-8"), "second")
            self.assertFalse((output / "old.html").exists())


class CommandTests(unittest.TestCase):
    """验证入口参数与 Zensical 静态构建。"""

    def test_help_works_with_ascii_console_encoding(self) -> None:
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="ascii")
        try:
            with patch("sys.stdout", stream):
                with self.assertRaises(SystemExit) as result:
                    main(["--help"])

            self.assertEqual(result.exception.code, 0)
            stream.flush()
            self.assertIn(b"mdsite", buffer.getvalue())
            self.assertIn(b"\\u", buffer.getvalue())
        finally:
            stream.detach()

    def test_no_subcommand_defaults_to_preview(self) -> None:
        args = _parser().parse_args([])
        self.assertIsNone(args.command)
        self.assertEqual(args.path, None)

    def test_publish_command_accepts_stateless_repository_and_token_source(self) -> None:
        args = _parser().parse_args(
            [
                "publish",
                "--repository",
                "owner/notes",
                "--token-env",
                "NOTES_TOKEN",
                "--create-repository",
                "--configure-pages",
            ]
        )
        self.assertEqual(args.command, "publish")
        self.assertEqual(args.repository, "owner/notes")
        self.assertEqual(args.token_env, "NOTES_TOKEN")
        self.assertTrue(args.create_repository)
        self.assertTrue(args.configure_pages)

    def test_publish_builds_then_opens_returned_pages_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "notes"
            source.mkdir()
            built_site = root / "built-site"
            built_site.mkdir()

            def use_built_site(
                _source: Path,
                _output_to_exclude: Path | None,
                consume_site: Callable[[Path], None],
                *,
                environment: Mapping[str, str] | None = None,
            ) -> int:
                captured_environment.update(environment or {})
                consume_site(built_site)
                return 0

            captured_environment: dict[str, str] = {}
            with (
                patch.dict(
                    "os.environ",
                    {
                        "GH_TOKEN": "token-from-gh-env",
                        "GITHUB_TOKEN": "fallback-token",
                        "NOTES_TOKEN": "custom-token",
                    },
                ),
                patch("mdsite_cli.cli.resolve_token", return_value="resolved-token"),
                patch("mdsite_cli.cli._build_and_use", side_effect=use_built_site),
                patch(
                    "mdsite_cli.cli.publish_site",
                    return_value="https://owner.github.io/notes/",
                ) as publish,
                patch("mdsite_cli.cli.webbrowser.open", return_value=True) as open_browser,
            ):
                result = main(
                    [
                        "publish",
                        "--path",
                        str(source),
                        "--repository",
                        "owner/notes",
                        "--token-env",
                        "NOTES_TOKEN",
                        "--open",
                    ]
                )

            self.assertEqual(result, 0)
            self.assertEqual(publish.call_args.kwargs["explicit_token"], "resolved-token")
            self.assertEqual(publish.call_args.kwargs["token_environment"], "NOTES_TOKEN")
            open_browser.assert_called_once_with("https://owner.github.io/notes/")
            self.assertNotIn("GH_TOKEN", captured_environment)
            self.assertNotIn("GITHUB_TOKEN", captured_environment)
            self.assertNotIn("NOTES_TOKEN", captured_environment)

    def test_build_generates_site_and_can_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "notes"
            source.mkdir()
            (source / "README.md").write_text("# Notes\n\nA simple local page.\n", encoding="utf-8")

            self.assertEqual(main(["build", "--path", str(source)]), 0)
            output = source / "site"
            self.assertTrue((output / "index.html").is_file())
            self.assertTrue(list((output / "source").rglob("*.html")))

            self.assertEqual(main(["build", "--path", str(source)]), 0)
            self.assertTrue((output / "index.html").is_file())


if __name__ == "__main__":
    unittest.main()
