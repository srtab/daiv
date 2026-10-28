# DAIV Constitution

Companion rules are indexed in [index.md](index.md). Every rule names the failure it
prevents; implementation mechanics and current gaps are linked references.

## Core Principles

### I. Secure and Isolated by Default

DAIV runs an agent against other people's repositories with their credentials, so every path
fails closed. The agent's commands run only in a sandbox, whose network is closed unless an
allow-list opens it. Credentials are the narrowest and shortest-lived the platform allows. A
user reaches only the repositories their git platform lets them reach.

**Prevents:** untrusted code running on the DAIV host, exfiltration through open egress,
credentials that outlive or outreach their purpose, and unauthenticated or unauthorised runs.

**Rules:** `patterns/security.md`.

### II. Enforced Architecture Boundaries

Apps depend on the apps below them and nothing else; the import contracts are binding, and
an exception is a reviewed design decision. Agent code asks its `Workspace` rather than
branching on run mode, and every concern uses the extension point that already exists for it.

**Prevents:** trigger apps coupled to each other or to the runtime, features that work in
one run mode only, and parallel mechanisms for one job.

**Rules:** `architecture/layering.md`.

### III. Platform and Provider Neutrality

A feature works on GitLab and GitHub unless its spec scopes it to one and says why.
Behaviour never depends on a hardcoded LLM provider or model, and every default can be
overridden by the operator.

**Prevents:** users on one platform or provider silently losing features, and deployments
locked to a provider they have no key for.

**Rules:** `patterns/platforms-and-providers.md`.

### IV. Tests Prove Project Behavior (NON-NEGOTIABLE)

Every behaviour change ships with tests that fail without it. Tests cover DAIV's own logic,
not the framework's, make no real LLM or network calls, and leave no state behind. Evidence
is what was actually run.

**Prevents:** green builds that prove nothing, suites that break on upgrades without finding
a bug, and failures that depend on test order.

**Rules:** `patterns/tests.md`.

### V. Measured Agent Behavior

A change meant to alter agent behaviour is a claim about the model. It SHOULD be measured
before and after; an unmeasured change says so, and no improvement or regression is claimed
without the comparison behind it.

**Prevents:** prompt changes shipped on intuition, and run-to-run noise reported as a result.

**Rules:** `patterns/agent-evals.md`.

### VI. Honest Failure Handling

Failures reach whoever can act on them: the agent through a tool's result, the operator
through an ERROR log. A usable partial result is returned flagged as degraded rather than
discarded. Nothing reports a success that did not happen, and no failure discloses a secret.

**Prevents:** silent failures behind a clean Sentry, wasted agent work, users trusting work
that did not land, and disclosure through error paths.

**Rules:** `patterns/errors.md`.

## Technology Constraints

These are the selected project technologies. Changes follow Governance.

- **Runtime:** Python 3.14; dependencies managed by `uv`, pinned with `==`, changed only
  through `uv add` and `uv remove`
- **Framework:** Django 6, under ASGI
- **API:** django-ninja with Pydantic schemas
- **Persistence:** PostgreSQL; Redis for cache, sessions and live run streams
- **Queue:** Django's tasks framework on `django-tasks-db`, with `django-crontask` for
  schedules
- **Agent:** Deep Agents on LangGraph; LLM providers configured as `Provider` rows
- **Code execution:** daiv-sandbox; `daiv/core/sandbox/schemas.dump.json` is the canonical
  wire schema, and a change to it ships with the matching daiv-sandbox release
- **Front end:** Django templates, Tailwind and Alpine.js, following `DESIGN.md`
- **Tests:** pytest with pytest-django, pytest-xdist and pytest-cov
- **Static checks:** Ruff, pyproject-fmt, Djade, import-linter; ty with `django-stubs`
- **Diagnostics:** Sentry and LangSmith
- **Docs:** MkDocs Material, built with `--strict`
- **CI:** GitHub Actions

## Development Workflow and Quality Gates

Feature work follows `/speckit-specify` → `/speckit-clarify` → `/speckit-plan` →
`/speckit-tasks` → `/speckit-analyze` → `/speckit-implement`, with human review between
stages. Implementation starts only after a maintainer approves the plan. The agent implements
within the approved scope and reports a check it did not run as unverified.

A change MUST NOT merge until its gates pass:

- **`make lint`:** Ruff and its formatter, pyproject-fmt, Djade on tracked templates, and
  import-linter. Every change
- **`make lint-typing`:** ty over `daiv/` in the full environment; warnings fail too. Every
  change
- **`make test`:** the unit suite with a coverage report. Every change
- **Docs build:** `mkdocs build --strict`, run by CI on pull requests that touch the docs. A
  new page is in both the `mkdocs.yml` `nav` and the `llmstxt` sections
- **Evals:** per [agent evals](patterns/agent-evals.md). Agent-behaviour changes
- **Constitution check:** the plan and the review name the affected principles and the
  evidence of compliance. Every change

User-facing changes add an `Unreleased` entry to `CHANGELOG.md` in Common Changelog form,
listing notable changes only. A breaking change starts with **Breaking:** and carries an
**Upgrade note** when operators must act.

## Governance

**Precedence:** this charter governs where its companions are silent or disagree, and with
them outranks `AGENTS.md` and the code. Each companion owns its subject but cannot contradict
a charter principle. Resolve a disagreement by correction, an approved exception or an
amendment; do not work around it.

**Approval:** any pull request may propose an amendment to the charter or a companion; a
maintainer approves it in review. A draft is not approval.

**Amendments:** one is required when a governing rule changes, not because an implementation
touches a governed area. A governing-rule change and its implementation ship together. The
pull request carries the impact report: changed or revoked clauses, reasons, compatibility
implications, enforcement changes and remaining gaps. Do not keep an amendment ledger here.

Versions cover the charter and its companions:

- **MAJOR:** a principle is removed or redefined, including a material change in what it
  requires.
- **MINOR:** a principle, section or companion is added, or guidance materially expands
  without redefining a requirement.
- **PATCH:** wording or clarification that preserves meaning.

If the classification is arguable, the pull request records the reasoning for the approver.
Finalize the version and amendment date when approval is recorded; keep the original
ratification date.

**Exceptions:** a deviation from a MUST needs an approved record in the pull request naming
the clause, reason, owner, expiry and residual gap. A TODO is not an exception. A permanent
deviation needs an amendment; departing from a SHOULD needs a reason in review.

**Gates:** do not disable, narrow or skip a gate to get a change through. If a gate
misimplements a rule, fix the gate; amend the rule only when its requirement changes.

**Compliance:** record unresolved requirements and enforcement gaps below rather than
claiming protection that does not exist.

### Open Questions and Pending Enforcement

Code MUST NOT silently decide an open question; resolve it first, and amend the affected
documents when the answer changes their rules.

- **TODO(EVAL_GATE):** no pipeline runs the evals, which need LLM credentials and a GitLab
  instance. Principle V rests on the author and the reviewer.
- **TODO(TYPE_CHECKING):** the files `[[tool.ty.overrides]]` in `pyproject.toml` lists are
  exempt from every ty rule, so a new error in one of them passes the gate. Each fix removes
  its file and nothing is added; closed when the list is empty.
- **TODO(LOG_LEVELS):** nothing fails an operator-actionable failure logged below ERROR.
  Until something does, review checks it.
- **TODO(PLATFORM_PARITY):** `RepoClient` makes both clients implement each abstract method,
  but nothing checks that a feature behaves the same on both platforms. Until something
  does, review checks it.

**Version:** 1.0.0, proposed | **Ratified:** pending | **Last Amended:** pending
