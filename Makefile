# Makefile

.PHONY: help test test-ci lint lint-check lint-format lint-fix lint-typing evals

help:
	@echo "Available commands:"
	@echo "  make test           - Run tests with coverage report"
	@echo "  make lint           - Run lint check and format check"
	@echo "  make lint-check     - Run lint check only (ruff)"
	@echo "  make lint-format    - Check code formatting"
	@echo "  make lint-fix       - Fix linting and formatting issues"
	@echo "  make lint-typing    - Run type checking with ty"
	@echo "  make lock           - Update uv lock"
	@echo "  make integration-tests          - Run integration tests"

test:
	LANGCHAIN_TRACING_V2=false uv run pytest -s tests/unit_tests

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
	uv run --only-group=dev ty check daiv

lock:
	uv lock

makemessages:
	uv run django-admin makemessages --ignore=*/node_modules/* --ignore=.venv --no-location --no-wrap --all

compilemessages:
	uv run django-admin compilemessages

integration-tests:
	LANGSMITH_TEST_SUITE="DAIV: Diff to Metadata" uv run pytest --reuse-db tests/integration_tests --no-cov --log-level=INFO -k test_diff_to_metadata -n 2

swebench:
	uv run evals/swebench.py --dataset-path "SWE-bench/SWE-bench_Lite" --dataset-split "dev" --output-path predictions.json --num-samples 1

swebench-evaluate: swebench-clean
	mkdir -p /tmp/swebench
	git clone https://github.com/SWE-bench/SWE-bench /tmp/swebench
	cd /tmp/swebench; uv venv --python 3.11; uv pip install -e .; uv run -m swebench.harness.run_evaluation \
		--dataset_name SWE-bench/SWE-bench_Lite \
		--split dev \
		--max_workers 4 \
		--predictions_path /tmp/predictions.json \
		--run_id 1

swebench-clean:
	rm -rf /tmp/swebench

docs-serve:
	uv run --only-group=docs mkdocs serve -o -a localhost:4000 -w docs/

langsmith-fetch:
	uv run langsmith-fetch traces --project-uuid 00d1a04e-0087-4813-9a18-5995cd5bee5c --limit 1 ./my-traces
