.PHONY: init help format lint test check-all
init:
	uv sync --group dev
help:
	uv run --frozen aiops --help
format:
	uv run --frozen ruff check --select I --fix src tests
	uv run --frozen ruff format src tests
lint:
	uv run --frozen ruff check src tests
	uv run --frozen ruff format --check src tests
test:
	uv run --frozen pytest
check-all: format lint test
