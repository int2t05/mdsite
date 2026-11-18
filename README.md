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
