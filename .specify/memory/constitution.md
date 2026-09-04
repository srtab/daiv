<!--
Sync Impact Report
==================
Version change: (unfilled template, no version) → 1.0.0

Bump rationale: first ratified constitution. The previous file was the pristine
scaffold with every `[PLACEHOLDER]` unreplaced, so this is an initial adoption
rather than an amendment. Values were derived from README.md, AGENTS.md,
CONTRIBUTING.md, DESIGN.md, CHANGELOG.md, docs/, and .github/workflows/ci.yml.

Modified principles (placeholder → concrete):
- [PRINCIPLE_1_NAME] → I. Self-Hosted Sovereignty
- [PRINCIPLE_2_NAME] → II. Sandboxed Execution & Least-Privilege Credentials
- [PRINCIPLE_3_NAME] → III. Platform Parity Across Git Hosts
- [PRINCIPLE_4_NAME] → IV. Green Gates Before Merge
- [PRINCIPLE_5_NAME] → V. Deterministic, Pinned Environments

Added sections:
- VI. Failures Are Visible (one principle beyond the template's five slots)
- Additional Constraints (was [SECTION_2_NAME]/[SECTION_2_CONTENT])
- Development Workflow & Review Process (was [SECTION_3_NAME]/[SECTION_3_CONTENT])
- Governance body (was [GOVERNANCE_RULES]); [GUIDANCE_FILE] → AGENTS.md

Removed sections: none

Deferred TODOs: none
-->

# DAIV Constitution

## Core Principles

### I. Self-Hosted Sovereignty

DAIV is operator-owned infrastructure, and every feature MUST remain runnable on a deployment the
operator controls end to end.

- No feature MAY depend on a hosted service run by the DAIV maintainers.
- The LLM provider and model MUST stay operator-selectable. Providers live in database-backed
  `Provider` rows and the model catalogue in `daiv/automation/agent/constants.py`; no call site MAY
  hardcode a provider or model id.
- Third-party observability (LangSmith, Sentry, Rocket Chat) MUST be optional. With their
  credentials absent, the system MUST still be correct — only visibility is reduced.
- Code, credentials, and prompts MUST NOT be sent to any destination the operator has not
  configured.

Rationale: the project's reason to exist is that operators keep their code, their keys, and their
network. A feature that only works against a maintainer-run endpoint breaks that promise.

### II. Sandboxed Execution & Least-Privilege Credentials (NON-NEGOTIABLE)

Untrusted work — anything an agent or a repository decides to run — MUST execute under isolation,
with the narrowest credentials that can do the job.

- Agent-authored shell commands MUST run in the daiv-sandbox container, never in the app, worker,
  or scheduler process.
- Sandbox network access MUST be governed by the environment's declared egress policy. There is no
  unrestricted-network mode, and widening egress is an operator decision, never a code default.
- Repository credentials MUST be scoped to the target repository and as short-lived as the platform
  allows. Tokens MUST NOT be logged, echoed into diffs or agent output, or persisted in
  checkpoints.
- A user MUST NOT be able to act on a repository beyond the access the git platform grants them;
  authorization decisions MUST be mirrored from the platform, not invented locally.
- The sandbox wire contract in `daiv/core/sandbox/schemas.dump.json` is canonical. Drift MUST fail
  the test suite, not degrade to a warning.

Rationale: DAIV executes model-authored code against real repositories. Isolation and credential
scope are the only barriers between a bad generation and an operator's infrastructure.

### III. Platform Parity Across Git Hosts

GitLab and GitHub are both first-class targets.

- Platform interaction MUST go through the client abstraction in `daiv/codebase/clients/`. Feature
  code MUST NOT branch on the platform outside that layer.
- A feature that cannot reach parity MUST document the gap in `docs/` in the same change that ships
  it; an undocumented one-platform feature is incomplete.
- Shared vocabulary — including the bot labels `daiv`, `daiv-max`, and `daiv-auto` — MUST be
  referenced from `daiv/core/constants.py`, never hardcoded at call sites.

Rationale: parity is the integration promise. Divergence between hosts multiplies every future
change and silently strands half the users.

### IV. Green Gates Before Merge (NON-NEGOTIABLE)

- `make lint` and `make test` MUST pass before a pull request merges. CI enforces both, and runs
  tests only after lint succeeds.
- Tests MUST cover this project's own logic. Re-testing third-party framework behaviour is
  prohibited: it buys no signal and pins us to other projects' internals.
- `make lint-typing` (ty) is advisory. It carries a known baseline of Django field-descriptor false
  positives and MUST NOT be treated as a zero-error gate; contributors MUST NOT add a new class of
  typing error.
- A red gate MUST be fixed or explicitly reverted. Disabling, skipping, or narrowing a check to
  make it pass requires a stated reason in the pull request.

Rationale: the gates are the only automated statement about whether DAIV still works. Weakening
them to land a change converts a known failure into an unknown one.

### V. Deterministic, Pinned Environments

- Dependencies MUST be `==`-pinned and changed only through `uv add <pkg>==<version>` /
  `uv remove <pkg>`. Hand-editing `pyproject.toml` is prohibited.
- Python 3.14 is the only supported runtime (`requires-python = ">=3.14,<3.15"`).
- Developer and CI commands MUST be reachable through `make` targets or `uv run`, so the same
  invocation works on a host and inside the container.
- Documented dependency-upgrade blockers MUST be re-verified against the current releases before
  being declared stale; a blocker is removed with evidence, not by assumption.

Rationale: DAIV's behaviour depends on a fast-moving agent stack. Reproducibility is what makes a
regression attributable instead of mysterious.

### VI. Failures Are Visible

- No code path MAY swallow an error. A caught exception MUST be re-raised, surfaced to the caller
  or agent, or logged with enough context to act on.
- When a sub-step fails but a usable partial result exists and the failure is not agent-actionable,
  the flagged partial result MUST be returned rather than a bare error. Operator visibility is
  solved with logging, never by withholding output.
- `logger.warning` is NOT an alerting channel: the default Sentry logging integration only reports
  at ERROR. A quiet error tracker MUST NOT be cited as evidence that a path succeeded.
- User-facing failures MUST say what failed and what state was left behind — a pushed branch, an
  open merge request, a stopped sandbox.

Rationale: an agent that fails silently produces confident wrong answers, and the operator pays for
the debugging. Loud, attributable failure is cheaper than a plausible one.

## Additional Constraints

**Architecture placement.** New work goes where the existing structure puts it: agent tools in
`daiv/automation/agent/tools/`, built-in skills in `daiv/automation/agent/skills/<name>/`,
middleware in `daiv/automation/agent/middlewares/`, MCP tools in `daiv/mcp_server/server.py`,
shared settings in `daiv/daiv/settings/components/`. HTML surfaces are class-based views in
`daiv/<app>/views.py`; JSON surfaces are django-ninja routers under `daiv/<app>/api/`, registered in
`daiv/daiv/api.py`. Filtered lists use `django_filters.FilterSet` with `FilterView`, not hand-rolled
`request.GET` parsing.

**Agent state.** Tools MUST NOT mutate `runtime.state`; they return a `Command(update={...})` with
a `ToolMessage`. Repository memory `MemoryEntry` rows are append-only truth and
`RepositoryMemory.content` is a render cache — never model-generated, and budgets are enforced by
pruning, not slicing.

**UI.** `DESIGN.md` is authoritative for the dashboard: server-rendered Django templates with
Alpine.js, no SPA, dark mode only, Tailwind v4 theme tokens declared exactly once in
`daiv/static_src/css/input.css`, and no font or icon CDN. Reusable styling is a class in
`input.css`, not a long utility chain repeated across templates.

**Documentation.** Behavioural detail lives in module docstrings next to the code. `docs/` is the
operator-facing contract and MUST be updated in the change that alters behaviour. `AGENTS.md` holds
only non-obvious repository knowledge; it MUST NOT restate what the code already says. `CHANGELOG.md`
follows Common Changelog and records notable, user-facing changes only.

**Licensing.** DAIV is Apache-2.0. Contributions are accepted under that licence, and dependencies
MUST be licence-compatible.

## Development Workflow & Review Process

- Branch from `main` using `feat/`, `fix/`, `chore/`, or `security/` prefixes with a descriptive
  slug.
- Commit subjects use the imperative present tense, stay within 72 characters, and reference the
  issue or pull request where one exists.
- Every change reaches `main` through a pull request that has passed `make lint` and `make test`,
  updated documentation where behaviour changed, and received maintainer review.
- Secrets never enter the repository. Local credentials belong in `docker/local/app/config.secrets.env`,
  which is generated by `make setup` and stays untracked.
- Breaking changes MUST carry an upgrade note in `CHANGELOG.md` describing what an existing
  deployment will experience and the action required.
- Issue reports include reproduction steps, expected versus actual behaviour, and environment
  details.

## Governance

This constitution supersedes conflicting conventions elsewhere in the repository. `AGENTS.md` (and
the `CLAUDE.md` that includes it) carries day-to-day runtime guidance for contributors and agents;
where it disagrees with this document, this document wins and the guidance file MUST be corrected.

**Amendment procedure.** Amendments are proposed as a pull request editing
`.specify/memory/constitution.md`. The pull request MUST state the rationale, the version bump and
why that level applies, and any migration required of existing code, deployments, or documentation.
A maintainer approval is required to merge.

**Versioning policy.** This document is versioned with semantic versioning:

- **MAJOR** — a principle is removed or redefined in a backward-incompatible way, or governance
  changes such that previously compliant work is now non-compliant.
- **MINOR** — a principle or section is added, or existing guidance is materially expanded.
- **PATCH** — clarifications, wording, and typo fixes that do not change what is required.

**Compliance review.** Reviewers verify pull requests against these principles. Added complexity
MUST be justified in the pull request description. A knowing deviation is permitted only as an
explicit, time-boxed exception recorded in the pull request, naming the principle and the plan to
return to compliance; anything else MUST be fixed before merge.

**Version**: 1.0.0 | **Ratified**: 2026-09-03 | **Last Amended**: 2026-09-03
