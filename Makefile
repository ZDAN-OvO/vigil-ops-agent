.PHONY: init help format lint test check-all web-build web-check
init:
	uv sync --group dev
help:
	uv run --frozen aiops --help
	uv run --frozen aiops dev --help
format:
	uv run --frozen ruff check --select I --fix src tests
	uv run --frozen ruff format src tests
lint:
	uv run --frozen ruff check src tests
	uv run --frozen ruff format --check src tests
test:
	uv run --frozen pytest
web-build:
	pnpm --dir web install --frozen-lockfile
	pnpm --dir web build
web-check:
	uv run --frozen python scripts/export_web_openapi.py --check
	pnpm --dir web check
check-all: format lint test web-check
