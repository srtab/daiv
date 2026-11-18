# Makefile

.PHONY: help setup test test-ci lint lint-check lint-format lint-fix lint-imports lint-design lint-typing evals eval-prompts tailwind-build tailwind-watch up db-reset \
	swebench swebench-evaluate swebench-mini swebench-mini-evaluate swerebench swerebench-evaluate

help:
	@echo "Available commands:"
	@echo "  make setup          - Set up local development environment"
	@echo "  make up             - Start this checkout's stack (in a worktree: its own slot)"
	@echo "  make db-reset       - Copy the main database into this worktree's slot again, then start it"
	@echo "  make test           - Run tests with coverage report"
	@echo "  make lint           - Run lint, format and import-layering checks"
	@echo "  make lint-check     - Run lint check only (ruff)"
	@echo "  make lint-format    - Check code formatting"
	@echo "  make lint-imports   - Check app import layering (import-linter)"
	@echo "  make lint-design    - Validate DESIGN.md against the design.md spec (needs Node)"
	@echo "  make lint-fix       - Fix linting and formatting issues"
	@echo "  make lint-typing    - Run type checking with ty"
	@echo "  make lock           - Update uv lock"
	@echo "  make integration-tests          - Run integration tests"
	@echo "  make eval-prompts SUITES=... OUT=...  - Run eval suites DAIV_EVAL_REPEATS times, appending metrics to OUT"
	@echo "  make eval-prompts CASES=... OUT=...   - The same for single test ids, to confirm a flip compare_runs reports"
	@echo "  make swebench-mini [SWEBENCH_SAMPLES=5] - Run DAIV on SWE-bench Verified Mini; a rerun resumes"
	@echo "  make swebench-mini-evaluate             - Grade those predictions with the SWE-bench harness (Docker)"

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

DESIGNMD := @google/design.md@0.4.0

# The CLI exits 0 on warnings (contrast, orphaned tokens); the gate fails on those too.
lint-design:
	npx --yes $(DESIGNMD) lint DESIGN.md | tee /dev/stderr | node -e 'const { summary: s } = JSON.parse(require("fs").readFileSync(0, "utf8")); process.exit(s.errors + s.warnings ? 1 : 0)'

lint-fix:
	uv run --only-group=dev ruff check . --fix
	uv run --only-group=dev ruff format .
	uv run --only-group=dev pyproject-fmt pyproject.toml
	git ls-files -z -- '*templates/*.html' | xargs -0r uv run --only-group=dev djade --target-version 6.0
	$(MAKE) --no-print-directory lint-imports

lint-typing:
	uv run ty check daiv

makemessages:
	uv run django-admin makemessages --ignore=*/node_modules/* --ignore=.venv --no-location --no-wrap --all

compilemessages:
	uv run django-admin compilemessages --ignore='.*'

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
EVAL_SUITE_MODEL_VARS := ASK_USER SKILLS TODOS WEB_SEARCH SUBAGENTS CODE_REVIEW INBOX DELEGATION BROWSER
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

# SWE-bench: DAIV writes predictions, then the official harness grades them in Docker. Each generate/evaluate pair
# shares its dataset, split and SWEBENCH_PREDICTIONS; another model, thinking level or DAIV version needs its own path.
EVAL_HOST_ENV = DB_HOST=$${DB_HOST:-127.0.0.1} \
	DJANGO_REDIS_URL=$${DJANGO_REDIS_URL:-redis://127.0.0.1:6379/0} \
	DJANGO_REDIS_SESSION_URL=$${DJANGO_REDIS_SESSION_URL:-redis://127.0.0.1:6379/1} \
	DJANGO_REDIS_CHECKPOINT_URL=$${DJANGO_REDIS_CHECKPOINT_URL:-redis://127.0.0.1:6379/0} \
	DAIV_SANDBOX_URL=$${DAIV_SANDBOX_URL:-http://127.0.0.1:8888} \
	CODEBASE_GITLAB_URL=$${CODEBASE_GITLAB_URL:-http://127.0.0.1:8929} \
	LANGSMITH_TRACING=$${LANGSMITH_TRACING:-false} LANGCHAIN_TRACING_V2=$${LANGCHAIN_TRACING_V2:-false} \
	PYTHONPATH=daiv:.
SWEBENCH_MODEL ?= openrouter:minimax/minimax-m3
SWEBENCH_THINKING ?= high
SWEBENCH_SEED ?= 0
SWEBENCH_WORKERS ?= 4
# The harness counts every container whose name contains the run ID as unstopped, so not "daiv".
SWEBENCH_RUN_ID ?= grade
VERIFIED_DATASET := SWE-bench/SWE-bench_Verified
# Mini lacks the image and eval-script columns swebench 5 grades with; its tasks are graded against Verified.
MINI_DATASET := MariusHobbhahn/swe-bench-verified-mini
SWEREBENCH_DATASET := nebius/SWE-rebench-leaderboard
# A split is unseen only by models trained before it; 2026_03 is the newest on Hugging Face as of 2026-11-18.
SWEREBENCH_SPLIT ?= 2026_03
SWEBENCH_HARNESS := uv run --no-project --python 3.11 --with swebench==5.0.2 python -m swebench.harness.run_evaluation
SWEREBENCH_HARNESS := uv run --no-project --python 3.11 \
	--with git+https://github.com/SWE-rebench/SWE-bench-fork@e4907b7a90eafaa1f0a6428fd04fe31cdd8b4284 \
	python -m swebench.harness.run_evaluation --namespace swerebench

swebench swerebench: SWEBENCH_SAMPLES ?= 10
swebench swebench-evaluate: SWEBENCH_PREDICTIONS ?= eval-runs/swebench/verified/predictions.json
swebench-mini swebench-mini-evaluate: SWEBENCH_PREDICTIONS ?= eval-runs/swebench/mini/predictions.json
swerebench swerebench-evaluate: SWEBENCH_PREDICTIONS ?= eval-runs/swerebench/$(SWEREBENCH_SPLIT)/predictions.json

# $(1) dataset, $(2) split; runs on the host against the main stack, where a variable set in the shell wins.
swebench_generate = $(EVAL_HOST_ENV) \
	uv run --env-file docker/local/app/config.env --env-file docker/local/app/config.secrets.env python -m evals.swebench \
	--dataset-path "$(1)" --dataset-split "$(2)" --output-path "$(SWEBENCH_PREDICTIONS)" \
	--model-names $(SWEBENCH_MODEL) --thinking-level $(SWEBENCH_THINKING) --seed $(SWEBENCH_SEED) \
	$(if $(SWEBENCH_IDS),--instance-ids $(SWEBENCH_IDS),$(if $(SWEBENCH_SAMPLES),--num-samples $(SWEBENCH_SAMPLES))) \
	$(if $(SWEBENCH_RETRY_FAILED),--retry-failed)

# $(1) dataset, $(2) split, $(3) harness; its logs and report land next to the predictions.
swebench_evaluate = cd "$(dir $(SWEBENCH_PREDICTIONS))" && $(3) --dataset_name "$(1)" --split "$(2)" \
	--predictions_path "$(notdir $(SWEBENCH_PREDICTIONS))" --max_workers $(SWEBENCH_WORKERS) --run_id $(SWEBENCH_RUN_ID)

swebench:
	$(call swebench_generate,$(VERIFIED_DATASET),test)

swebench-evaluate:
	$(call swebench_evaluate,$(VERIFIED_DATASET),test,$(SWEBENCH_HARNESS))

swebench-mini:
	$(call swebench_generate,$(MINI_DATASET),test)

swebench-mini-evaluate:
	$(call swebench_evaluate,$(VERIFIED_DATASET),test,$(SWEBENCH_HARNESS))

swerebench:
	$(call swebench_generate,$(SWEREBENCH_DATASET),$(SWEREBENCH_SPLIT))

swerebench-evaluate:
	$(call swebench_evaluate,$(SWEREBENCH_DATASET),$(SWEREBENCH_SPLIT),$(SWEREBENCH_HARNESS))

docs-serve:
	uv run --only-group=docs mkdocs serve -o -a localhost:4000 -w docs/

up:
	docker/local/stack up

db-reset:
	docker/local/stack db-reset

tailwind-build:
	docker compose exec app tailwindcss -i daiv/static_src/css/input.css -o daiv/static/css/styles.css --minify

tailwind-watch:
	docker compose exec app tailwindcss -i daiv/static_src/css/input.css -o daiv/static/css/styles.css --watch

langsmith-fetch:
	uv run langsmith-fetch traces --project-uuid 00d1a04e-0087-4813-9a18-5995cd5bee5c --limit 7 --include-metadata ./daiv-traces
