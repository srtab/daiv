# Makefile

.PHONY: help test test-ci lint lint-check lint-format lint-fix lint-typing evals

help:
	@echo "Available commands:"
	@echo "  make test           - Run tests with coverage report"
	@echo "  make lint           - Run lint check and format check"
	@echo "  make lint-check     - Run lint check only (ruff)"
	@echo "  make lint-format    - Check code formatting"
	@echo "  make lint-fix       - Fix linting and formatting issues"
	@echo "  make lint-typing    - Run type checking with mypy"
	@echo "  make lock           - Update uv lock"
	@echo "  make evals          - Run evals"

test:
	uv run pytest tests

lint: lint-check lint-format

lint-check:
	uv run --only-group=dev ruff check .

lint-format:
	uv run --only-group=dev ruff format . --check
	uv run --only-group=dev pyproject-fmt pyproject.toml --check

lint-fix:
	uv run --only-group=dev ruff check . --fix
	uv run --only-group=dev ruff format .
	uv run --only-group=dev pyproject-fmt pyproject.toml

lint-typing:
	uv run --only-group=dev mypy daiv

lock:
	uv lock

makemessages:
	uv run django-admin makemessages --ignore=*/node_modules/* --ignore=.venv --no-location --no-wrap --all

compilemessages:
	uv run django-admin compilemessages

evals:
	LANGSMITH_TEST_SUITE="DAIV evals" uv run pytest --reuse-db evals --no-cov evals/test_codebase_search.py
