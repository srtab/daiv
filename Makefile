# Makefile

.PHONY: help setup test test-ci lint lint-check lint-format lint-fix lint-imports lint-typing evals eval-prompts tailwind-build tailwind-watch

help:
	@echo "Available commands:"
	@echo "  make setup          - Set up local development environment"
	@echo "  make test           - Run tests with coverage report"
	@echo "  make lint           - Run lint, format and import-layering checks"
	@echo "  make lint-check     - Run lint check only (ruff)"
	@echo "  make lint-format    - Check code formatting"
	@echo "  make lint-imports   - Check app import layering (import-linter)"
	@echo "  make lint-fix       - Fix linting and formatting issues"
	@echo "  make lint-typing    - Run type checking with ty"
	@echo "  make lock           - Update uv lock"
	@echo "  make integration-tests          - Run integration tests"
	@echo "  make eval-prompts SUITES=... OUT=...  - Run eval suites DAIV_EVAL_REPEATS times, appending metrics to OUT"
	@echo "  make eval-prompts CASES=... OUT=...   - The same for single test ids, to confirm a flip compare_runs reports"

setup:
	@if [ ! -f docker/local/app/config.secrets.env ]; then \
		cp docker/local/app/config.secrets.env.example docker/local/app/config.secrets.env && \
		echo "Created docker/local/app/config.secrets.env from template."; \
	else \
		echo "docker/local/app/config.secrets.env already exists, skipping."; \
	fi
	@if [ ! -f docker/local/gitlab-runner/config.toml ]; then \
		cp docker/local/gitlab-runner/config.toml.example docker/local/gitlab-runner/config.toml && \
		echo "Created docker/local/gitlab-runner/config.toml from template."; \
	else \
		echo "docker/local/gitlab-runner/config.toml already exists, skipping."; \
	fi
	@echo ""
	@echo "Next steps:"
	@echo "  1. Edit docker/local/app/config.secrets.env and add your API keys"
	@echo "     (at minimum: one LLM provider key + CODEBASE_GITLAB_AUTH_TOKEN if using GitLab)"
	@echo "  2. Start core services:  docker compose up --build"
	@echo "  3. Optional services:"
	@echo "       docker compose --profile gitlab up    # local GitLab instance"
	@echo "       docker compose --profile sandbox up   # sandbox code executor"
	@echo "       docker compose --profile github up    # smee webhook forwarder for GitHub"
	@echo "       docker compose --profile full up      # gitlab + runner + sandbox (not smee)"

test:
	LANGCHAIN_TRACING_V2=false uv run pytest -s tests/unit_tests -n auto

lint: lint-check lint-format lint-imports

lint-check:
	uv run --only-group=dev ruff check .

lint-format:
	uv run --only-group=dev ruff format . --check
	uv run --only-group=dev pyproject-fmt pyproject.toml --check
	git ls-files -z -- '*templates/*.html' | xargs -0r uv run --only-group=dev djade --target-version 6.0 --check

lint-imports:
	PYTHONPATH=daiv uv run --only-group=dev lint-imports

lint-fix:
	uv run --only-group=dev ruff check . --fix
	uv run --only-group=dev ruff format .
	uv run --only-group=dev pyproject-fmt pyproject.toml
	git ls-files -z -- '*templates/*.html' | xargs -0r uv run --only-group=dev djade --target-version 6.0
	$(MAKE) --no-print-directory lint-imports

lint-typing:
	uv run --only-group=dev ty check daiv

makemessages:
	uv run django-admin makemessages --ignore=*/node_modules/* --ignore=.venv --no-location --no-wrap --all

compilemessages:
	uv run django-admin compilemessages

# See AGENTS.md "Integration tests need credentials" for why every flag here is load-bearing.
# CODEBASE_GITLAB_URL is read from the secrets file rather than config.env: this runs on the host,
# where config.env's compose-internal `gitlab` hostname does not resolve. Exporting it here at all
# would mask the secrets file, since env_files_skip_if_set treats an exported key as already set.
integration-tests:
	GITLAB_URL="$${CODEBASE_GITLAB_URL:-$$(sed -n 's/^CODEBASE_GITLAB_URL=//p' docker/local/app/config.secrets.env 2>/dev/null | tail -1)}"; \
	CODEBASE_GITLAB_URL="$${GITLAB_URL:-http://127.0.0.1:8929}" \
	LANGSMITH_TEST_TRACKING="$${LANGSMITH_TEST_TRACKING:-false}" \
	uv run pytest --envfile +docker/local/app/config.secrets.env --reuse-db tests/integration_tests --no-cov --log-level=INFO -m "diff_to_metadata or memory"

EVAL_MODEL ?= openrouter:z-ai/glm-5.2
EVAL_SUITE_MODEL_VARS := ASK_USER SKILLS TODOS WEB_SEARCH SUBAGENTS CODE_REVIEW
empty :=
space := $(empty) $(empty)

# pytest exit 1 (some tests failed) is a result, so the loop goes on; anything above 1 is a broken run.
eval-prompts:
	@test -n "$(SUITES)$(CASES)" || { echo 'SUITES or CASES is required, e.g. make eval-prompts SUITES="skills todos" OUT=eval-runs/before.jsonl'; exit 2; }
	@test -n "$(OUT)" || { echo 'OUT is required: the JSONL file every pass appends to'; exit 2; }
	@mkdir -p "$(dir $(abspath $(OUT)))"
	@GITLAB_URL="$${CODEBASE_GITLAB_URL:-$$(sed -n 's/^CODEBASE_GITLAB_URL=//p' docker/local/app/config.secrets.env 2>/dev/null | tail -1)}"; \
	SANDBOX_URL="$${DAIV_SANDBOX_URL:-$$(sed -n 's/^DAIV_SANDBOX_URL=//p' docker/local/app/config.secrets.env 2>/dev/null | tail -1)}"; \
	for run in $$(seq 1 $${DAIV_EVAL_REPEATS:-3}); do \
		echo "eval-prompts: pass $$run of $${DAIV_EVAL_REPEATS:-3}"; \
		CODEBASE_GITLAB_URL="$${GITLAB_URL:-http://127.0.0.1:8929}" \
		DAIV_SANDBOX_URL="$${SANDBOX_URL:-http://127.0.0.1:8888}" \
		LANGSMITH_TEST_TRACKING="$${LANGSMITH_TEST_TRACKING:-false}" \
		DAIV_EVAL_METRICS_OUT="$(abspath $(OUT))" DAIV_EVAL_RUN=$$run \
		$(foreach var,$(EVAL_SUITE_MODEL_VARS),DAIV_EVAL_$(var)_MODELS="$(EVAL_MODEL)") \
		uv run pytest --envfile +docker/local/app/config.secrets.env --reuse-db --no-cov --log-level=INFO \
			$(if $(CASES),$(foreach case,$(CASES),'$(case)'),tests/integration_tests -m "$(subst $(space), or ,$(strip $(SUITES)))"); \
		status=$$?; [ $$status -le 1 ] || exit $$status; \
	done

swebench:
	uv run evals/swebench.py --dataset-path "princeton-nlp/SWE-bench_Verified" --dataset-split "test" --output-path swebench-predictions.json --num-samples 10

swebench-evaluate: swebench-clean
	mkdir -p /tmp/swebench
	git clone https://github.com/SWE-bench/SWE-bench /tmp/swebench
	cd /tmp/swebench; uv venv --python 3.11; uv pip install -e .; uv run -m swebench.harness.run_evaluation \
		--dataset_name princeton-nlp/SWE-bench_Verified \
		--split dev \
		--max_workers 4 \
		--predictions_path /tmp/predictions.json \
		--run_id 1

swebench-clean:
	rm -rf /tmp/swebench

swerebench:
	uv run evals/swebench.py --dataset-path "nebius/SWE-rebench-leaderboard" --dataset-split "2026_03" --output-path swerebench-predictions.json --num-samples 10

swerebench-evaluate: swerebench-clean
	mkdir -p /tmp/swerebench
	git clone https://github.com/SWE-rebench/SWE-bench-fork /tmp/swerebench
	cd /tmp/swerebench; uv venv --python 3.11; uv pip install -e .; uv run -m swebench.harness.run_evaluation \
		--dataset_name nebius/SWE-rebench-leaderboard \
		--split 2026_03 \
		--max_workers 4 \
		--predictions_path /tmp/predictions.json \
		--namespace "swerebench" \
		--run_id 1

swerebench-clean:
	rm -rf /tmp/swerebench

docs-serve:
	uv run --only-group=docs mkdocs serve -o -a localhost:4000 -w docs/

tailwind-build:
	docker compose exec app tailwindcss -i daiv/static_src/css/input.css -o daiv/static/css/styles.css --minify

tailwind-watch:
	docker compose exec app tailwindcss -i daiv/static_src/css/input.css -o daiv/static/css/styles.css --watch

langsmith-fetch:
	uv run langsmith-fetch traces --project-uuid 00d1a04e-0087-4813-9a18-5995cd5bee5c --limit 7 --include-metadata ./daiv-traces
