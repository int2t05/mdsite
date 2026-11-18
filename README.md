# mdsite-cli

[![CI](https://github.com/int2t05/mdsite-cli/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/int2t05/mdsite-cli/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mdsite-cli)](https://pypi.org/project/mdsite-cli/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Preview and build a browsable website directly from a Markdown directory. The source files are read through a temporary directory link and are never copied or rewritten.

## Quick start

Run this from the Markdown root:

~~~powershell
uvx --from mdsite-cli mdsite
~~~

To run the latest GitHub version directly:

~~~powershell
uvx --from git+https://github.com/int2t05/mdsite-cli mdsite
~~~

Preview opens at `http://127.0.0.1:8000`. Stop it with `Ctrl+C`.

Preview another directory or use a different port:

~~~powershell
uvx --from git+https://github.com/int2t05/mdsite-cli mdsite --path "D:\notes"
uvx --from git+https://github.com/int2t05/mdsite-cli mdsite serve --port 8080
~~~

Build a static site into `site/` under the Markdown root:

~~~powershell
uvx --from git+https://github.com/int2t05/mdsite-cli mdsite build
uvx --from git+https://github.com/int2t05/mdsite-cli mdsite build --path "D:\notes" --output "D:\published-notes"
~~~

`build` refuses to replace an existing directory unless its ownership marker matches both the source and output paths. Custom output directories must be outside the Markdown root.

## Publish a public GitHub Pages site

`publish` builds the Markdown directory and uploads the generated website to the `gh-pages` branch. It does not copy Markdown source files into the repository. GitHub Pages websites are public, so publish only material that is safe to share.

Sign in once with GitHub CLI:

~~~powershell
gh auth login
~~~

Publish to an existing public repository:

~~~powershell
uvx --from git+https://github.com/int2t05/mdsite-cli mdsite publish --path "D:\notes" --repository "owner/notes" --open
~~~

If the repository does not exist, add `--create-repository` to create it as a public repository:

~~~powershell
uvx --from git+https://github.com/int2t05/mdsite-cli mdsite publish --path "D:\notes" --repository "owner/notes" --create-repository --open
~~~

The command takes the repository on every run and stores no project state. Credentials are read from `GH_TOKEN`, then `GITHUB_TOKEN`, then an existing `gh` login. To use a differently named environment variable, pass `--token-env NAME`; the token itself is never accepted as a command-line argument or written to the project.

For a fine-grained token, grant repository Contents: write for an existing repository. Creating a repository also needs repository creation permission. Creating or changing the Pages source needs Pages: write and Administration: write. Classic tokens generally need the `repo` scope. If the repository already uses another Pages source, the command stops; add `--configure-pages` only when you intend to switch it to `gh-pages` at the repository root. The first publish creates an ownership marker on `gh-pages`; later runs update only a branch with the matching marker, remove stale generated files, and use a normal fast-forward push.

## Features and limits

- Reads the folder tree directly; the source directory is not rewritten.
- Supports Markdown, Mermaid diagrams, and MathJax formulas.
- Formula rendering loads MathJax from a CDN, so it needs an internet connection.
- Requires Python 3.11 or newer and uv.

## Development

~~~powershell
uv sync --locked --all-groups
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests -v
uv build
~~~

## License

MIT; see [LICENSE](LICENSE).
