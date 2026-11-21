# Implementation Plan: Merge Request Automations

**Branch**: `005-merge-request-automations` | **Date**: 2026-11-21 | **Spec**: [spec.md](spec.md) | **Status**: Approved by the maintainer on 2026-11-21, including the [spec refinements](research.md#spec-refinements-for-approval)

**Input**: Feature specification from `specs/005-merge-request-automations/spec.md`

## Summary

Today DAIV runs on a merge request only when someone mentions it there. This plan lets three things start that same run without a comment.

1. **A merge request target on jobs (P1).**
   - **The request:** `POST /api/jobs` and the MCP job tool accept `"merge_request": <iid>` on a `repos` entry.
   - **Where the run goes:** where a mention from the caller would (FR-006). DAIV already has a path for each case, and the job takes whichever one a mention would. With no owning session, it takes the direct path, which moves from `webhooks` down into `sessions/merge_request_runs/` so `jobs` and `mcp_api` can call it. With an owning session, it becomes a new inbox event kind, `MERGE_REQUEST_JOB`, in that session ([R1](research.md#r1-a-job-on-a-merge-request-reuses-the-two-mention-paths), [R3](research.md#r3-a-new-inbox-event-kind-for-a-job-on-an-owned-merge-request)).
   - **Concurrency:** runs on a merge request leave the one-active-run constraint, and the session slot serialises them ([R2](research.md#r2-merge-request-targeted-runs-leave-the-one-active-run-constraint)).
2. **CI identity tokens (P2).**
   - **Verification:** `CIIdentityBearer` verifies GitLab CI ID tokens and GitHub Actions OIDC tokens with PyJWT against the platform's published signing keys. It checks the issuer, the audience (DAIV's address), the expiry and the stable repository id ([R4](research.md#r4-ci-identity-tokens-are-verified-with-pyjwt-against-the-platforms-published-keys), [R5](research.md#r5-the-ci-principal-and-what-it-may-do)).
   - **Attribution:** the run belongs to the pipeline's triggering account, whether a linked DAIV user or an external platform account.
   - **No personal tools:** a new `Run.acts_as_user = false` keeps such runs from loading the person's tools or environments ([R6](research.md#r6-attribution-without-authority-runacts_as_user)).
3. **Repository automations (P3, P4).**
   - **Declaration:** `.daiv.yml` gains `automations` (`name`, `on`, `prompt`, `filters`), validated one entry at a time ([R7](research.md#r7-automations-live-in-daivyml-and-are-validated-one-entry-at-a-time)).
   - **Events:** the merge request webhooks on both platforms now also accept opened, updated-with-new-commits and marked-ready ([R8](research.md#r8-events-come-from-the-merge-request-webhooks-on-both-platforms)).
   - **Firings:** `AutomationFiring` rows combine a burst into one firing on the newest commit after 60 s, and fire each commit at most once ([R9](research.md#r9-bursts-combine-on-the-trailing-edge-and-each-commit-fires-once)).
   - **Guards at fire time:** a commit an automation pushed never fires, and firings on DAIV's own commits are capped per merge request, with the count derived from firings ([R10](research.md#r10-loop-guards-are-checked-at-fire-time)).
   - **Runs:** each automation gets one session per merge request, under the new `AUTOMATION` origin. Its runs are muted and cannot ask questions ([R11](research.md#r11-each-automation-has-a-session-per-merge-request), [R14](research.md#r14-a-new-origin-automation)).
   - **Operator controls:** a site switch and the cap live in site configuration ([R13](research.md#r13-site-settings)).

## Technical Context

**Language/Version**: Python 3.14.

**Primary Dependencies**:
- Django 6 and django-ninja: multiple auth classes on `jobs_router`, and the `RepoSubmitItem` schema;
- django-tasks-db (`run_after` for the settle delay);
- pydantic: the `RepositoryConfig` section and its per-entry validation;
- python-gitlab and PyGithub, through `RepoClient`;
- **PyJWT with the `crypto` extra, which becomes a direct dependency.** It is already locked at 2.15.1 through allauth, mcp and pygithub, so this adds no new package.

**Storage**: PostgreSQL. In `sessions`:
- one new table, `AutomationFiring`;
- one new `Run` column, `acts_as_user`;
- a changed `run_one_active_per_session` condition;
- a new `SessionOrigin` value, which alters the choices and check constraints on `Run`, `Session` and `SpendRecord`.

In `core`: two `SiteConfiguration` fields. That is two schema migrations (`sessions/0043`, `core/0028`) and no data migration. Detail in [data-model.md](data-model.md).

**Testing**: pytest, pytest-django and pytest-asyncio.
- **Database:** async tests that write use `django_db(transaction=True)`.
- **Tokens:** CI tokens are signed in-test with a generated RSA key, and the keys endpoint is stubbed, so tests make no network calls.
- **Platforms:** git-platform clients are stubbed per platform. Both platforms get a case for every event mapping and for the changed-paths method.
- **Clock:** the settle delay is a module constant patched in tests.
- **Evals:** `inbox` and `code_review` before and after ([R15](research.md#r15-agent-behaviour-and-measurement)).

**Target Platform**: Linux containers: the web process (jobs API, webhooks) and workers (firing task, runs).

**Project Type**: a Django web service with background workers that run the agent.

**Performance Goals**:
- **Burst handling:** a burst of pushes produces one run within settle delay + 1 minute of the last push (SC-003).
- **Token checks:** verification costs one keys fetch per platform per cache period, plus one repository lookup per request, which `RepoClient` already caches.
- **Webhooks:** each event adds one indexed upsert per matching automation.

**Constraints**:
- **Fail closed** on token verification: missing keys, an unreachable issuer, or an unknown repository all answer `401` or `404`, with no detail (Principle I).
- **Attribution without authority**: personal tools and environments never load for `acts_as_user = false` runs (FR-011, SC-007).
- **Unchanged behaviour:** mentions on both paths, and the existing jobs with `ref` or `thread_id`, behave exactly as before.
- **Layering:** the trigger apps call down into `sessions`. Nothing below `sessions` learns about automations except `codebase.repo_config`, which owns the `.daiv.yml` schema, and `core`, which owns the site settings.

**Scale/Scope**: at most one firing per push per automation, with typical repositories declaring one to three automations. The work lands in seven apps: `accounts`, `codebase`, `core`, `sessions`, `jobs`, `mcp_api` and `webhooks`. A small part is in `dashboard`, and there is one new docs page.

## Constitution Check

*GATE: checked before Phase 0 and again after Phase 1 design. Both pass. One design point needs the maintainer's eye; it is listed below.*

| Principle | How the plan complies | Evidence at review |
|---|---|---|
| I. Secure and isolated | The CI token is checked for signature, issuer, audience, expiry and the repository's stable id, and the check fails closed. The token is the only access proof, and only a pipeline of that repository can get one. `allowed_usernames` applies to the signed login. `acts_as_user = false` withholds personal MCP servers, USER sandbox environments and the assignee. `.daiv.yml` automations come from the default branch only, so a merge request cannot add one that runs on itself. Refusals don't say which check failed | Tests for forged, expired, wrong-audience, wrong-repository and fork tokens. Tests that `acts_as_user=false` runs get no personal servers and no USER environment. A feature-branch-only automation test |
| II. Architecture boundaries | The direct merge request path moves into `sessions` (layer 8), so `jobs`, `mcp_api` and `webhooks` call one service and none imports another. `CIIdentityBearer` lives in `accounts` (identity) and calls down into `codebase`. Automation config lives in `codebase.repo_config`, and the firing logic in `sessions`. No new `ignore_imports`: the one-release task alias in `webhooks.tasks` imports downwards. Behaviour sits in classes: `MergeRequestRuns`, `Automations`, `AutomationFirings`, `DaivCommits`, `CIIdentityVerifier` | `make lint-imports`; review |
| III. Platform and provider neutrality | Every event, claim and client method has a GitLab and a GitHub branch with the same meaning (R4, R8, R12). GitHub Enterprise Server has its own issuer setting. No model or provider is named; automation runs use the configured models | Paired GitLab/GitHub tests for event mapping, token claims and changed paths |
| IV. Tests prove behaviour | Each new or changed module has its mirror test file (Project Structure). Failing-first tests cover the merge request target on both paths, the constraint change, token verification, polling visibility, `acts_as_user`, per-entry config validation, event mapping, the trailing combine, one-firing-per-commit, both loop guards, the cap and its reset, the site switch, and the failure comment | `make test` output in the pull request |
| V. Measured agent behaviour | The `MERGE_REQUEST_JOB` rendering and the prompt-as-request input change what the model sees, so `inbox` and `code_review` are measured before and after on `EVAL_MODEL` (R15). `/code-review` itself is unchanged, so ReviewBench is not run | `compare_runs.py` output in the pull request |
| VI. Honest failure handling | Every run on a merge request ends with its result or a failure comment (FR-020, SC-002). A capped firing posts one comment. Skipped firings record their reason (`superseded`, `automation_commit`, `capped`, `filtered`, `closed`, `disabled`). An unreachable issuer and an invalid automation entry log at ERROR. Refusals carry no internals | Tests on each skip reason and on the failure and cap comments |
| VII. Documentation | New `docs/features/automations.md` (`nav` and `llmstxt`). Updated jobs API, pull request assistant, repository configuration, site configuration, environment variables, sessions and spend pages. Two glossary terms. Three `CHANGELOG.md` `Added` lines | `mkdocs build --strict`; `test_docs_reference.py`, `test_env_variables_doc.py`, `test_docs_style.py`, `test_docs_links.py` |

**Gates**: `make lint`, `make lint-typing`, `make test`, a docs build, the changelog lines, and evals for `inbox` and `code_review` (Principle V).

**For the maintainer's eye**:
- The `run_one_active_per_session` condition change (R2) lets merge-request-targeted API and MCP runs share a session, as webhook runs already do. The session slot still allows one running at a time.
- The R6 finding: [MCP tools](../../docs/customization/mcp-tools.md) says unowned-merge-request mentions load no personal servers, but the code loads them. It's out of scope here.

**Post-design re-check (after Phase 1)**: unchanged. The data model adds no import across layers. `AutomationFiring` and `acts_as_user` are both in `sessions`. The contracts expose no field a caller couldn't already see, and a CI principal reads only its own repository's runs, attributed to its own actor.

## Project Structure

### Documentation (this feature)

```text
specs/005-merge-request-automations/
├── spec.md
├── plan.md                    # this file
├── research.md                # Phase 0: R1–R16, SR1–SR3
├── data-model.md              # Phase 1: Run.acts_as_user, AutomationFiring, origins, inbox kind, config, settings
├── quickstart.md              # Phase 1: validation steps
├── contracts/
│   ├── jobs-api.md            # merge_request target, CI identity auth, polling
│   ├── repository-config.md   # the `automations` section of .daiv.yml
│   ├── webhook-events.md      # GitLab/GitHub payloads → automation events
│   └── merge-request-runs.md  # the sessions service every trigger calls
├── checklists/requirements.md
└── tasks.md                   # Phase 2 (/speckit-tasks; not created here)
```

### Source Code (repository root)

```text
daiv/
├── accounts/api/ci_identity.py            # new: CIIdentityBearer, CIIdentity, CIIdentityVerifier (PyJWKClient per issuer)
├── codebase/
│   ├── conf.py                            # + GITHUB_ACTIONS_OIDC_ISSUER
│   ├── repo_config.py                     # + Automation, AutomationFilters, automations (per-entry validation)
│   └── clients/base.py, gitlab/client.py, github/client.py   # + get_merge_request_changed_paths → ChangedPaths
├── core/
│   ├── conf.py                            # + JOBS_CI_IDENTITY_AUDIENCE (defaults to EXTERNAL_URL)
│   ├── models.py, site_settings.py        # + automations_enabled, automations_max_daiv_push_runs, FieldGroup
│   └── migrations/0028_automations_settings.py
├── sessions/
│   ├── models.py                          # + SessionOrigin.AUTOMATION, Run.acts_as_user, AutomationFiring, constraint condition
│   ├── ids.py                             # + compute_automation_thread_id
│   ├── merge_request_runs/                # new: moved from webhooks
│   │   ├── service.py                     #   MergeRequestRuns: astart (owned → inbox, else direct), reply_archived
│   │   ├── addressor.py                   #   moved review_addressor + managers/base, input = Mention | Request
│   │   └── tasks.py                       #   address_merge_request_task (dedup on repo, iid, request key)
│   ├── automations/                       # new
│   │   ├── service.py                     #   Automations: aon_event (match, upsert firing, enqueue)
│   │   ├── firings.py                     #   AutomationFirings: afire (guards, cap, paths), DaivCommits
│   │   └── tasks.py                       #   fire_automation_task
│   ├── inbox/kinds.py, render.py, complete.py, drain.py, ownership.py   # MERGE_REQUEST_JOB; acting user; DaivCommits reuse
│   ├── queue.py, services.py, multi_repo.py, admission.py              # merge_request target, acts_as_user
│   └── migrations/0043_merge_request_automations.py
├── jobs/api/schemas.py, views.py          # merge_request field; auth=[CIIdentityBearer(), AuthBearer()]; CI polling rule
├── mcp_api/server.py                      # merge_request on the job tool
├── webhooks/
│   ├── dispatch.py                        # MR mentions call sessions.merge_request_runs; issue paths stay
│   ├── tasks.py                           # address_mr_comments_task re-export (one release)
│   ├── managers/                          # review_addressor.py, base.py removed (moved)
│   ├── gitlab/models.py, callbacks.py     # oldrev, last_commit, draft, changes; open/update/ready → automations
│   └── github/models.py, callbacks.py     # opened/synchronize/ready_for_review → automations
├── dashboard/home.py, insights.py         # AUTOMATION origin label and segment
└── pyproject.toml, uv.lock                # pyjwt[crypto] direct

tests/unit_tests/                          # mirrors each module above, one test file per module
docs/
├── features/automations.md                # new
├── features/jobs-api.md, pull-request-assistant.md, sessions.md, spend.md
├── customization/repository-config.md
└── reference/site-configuration.md, env-variables.md
constitutions/glossary/README.md           # + Automation, Automation firing
CHANGELOG.md
```

**Structure Decision**: This is the existing Django monolith under `daiv/`, with tests mirroring it. The two new packages in `sessions` (`merge_request_runs`, `automations`) follow the `sessions/inbox/` precedent: a concern big enough for several modules gets its own package.

## Complexity Tracking

No constitution violations to justify.
