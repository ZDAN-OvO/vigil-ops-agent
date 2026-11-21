.PHONY: init format lint test web-build web-test check-all
init:
	uv sync --group dev
format:
	uv run --frozen ruff check --select I --fix src tests hatch_build.py
	uv run --frozen ruff format src tests hatch_build.py
lint:
	uv run --frozen ruff check src tests hatch_build.py
	uv run --frozen ruff format --check src tests hatch_build.py
web-build:
	npm --prefix web ci --no-audit --no-fund
	npm --prefix web run build
web-test: web-build
	npm --prefix web test
	uv run --frozen python tests/web/check_styles.py
	uv run --frozen python tests/web/browser_cases.py
test: web-build
	uv run --frozen pytest
check-all: web-test
	$(MAKE) format
	$(MAKE) lint
	uv run --frozen pytest
