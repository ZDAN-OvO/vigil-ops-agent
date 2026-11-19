.PHONY: init format lint test check-all
init:
	uv sync --group dev
format:
	uv run --frozen ruff check --select I --fix src tests
	uv run --frozen ruff format src tests
lint:
	uv run --frozen ruff check src tests
	uv run --frozen ruff format --check src tests
test:
	uv run --frozen pytest
check-all: format lint test
