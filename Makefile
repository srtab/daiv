# Makefile

.PHONY: help test test-ci lint lint-check lint-format lint-fix lint-typing

help:
	@echo "Available commands:"
	@echo "  make test           - Run tests with coverage report"
	@echo "  make test-ci        - Run tests and generate XML coverage report (for CI)"
	@echo "  make lint           - Run lint check and format check"
	@echo "  make lint-check     - Run lint check only (ruff)"
	@echo "  make lint-format    - Check code formatting"
	@echo "  make lint-fix       - Fix linting and formatting issues"
	@echo "  make lint-typing    - Run type checking with mypy"
	@echo "  make lock           - Update uv lock"

test:
	uv run coverage run runtests.py
	uv run coverage report
	uv run coverage erase

test-ci:
	uv run coverage run runtests.py
	uv run coverage report
	uv run coverage xml

lint: lint-check lint-format

lint-check:
	uv run ruff check .

lint-format:
	uv run ruff format . --check
	uv run pyproject-fmt pyproject.toml --check

lint-fix:
	uv run ruff check . --fix
	uv run ruff format .
	uv run pyproject-fmt pyproject.toml

lint-typing:
	uv run mypy daiv

lock:
	uv lock