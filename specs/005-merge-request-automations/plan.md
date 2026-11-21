# Implementation Plan: Merge Request Automations

**Branch**: `005-merge-request-automations` | **Date**: 2026-11-21 | **Spec**: [spec.md](spec.md) | **Status**: Approved by the maintainer on 2026-11-21, including the [spec refinements](research.md#spec-refinements-for-approval). Rescoped the same day to user stories 1 and 2; see *Scope*. Updated the same day for the spec's later clarifications (questions, archived sessions, merge requests closed before a run starts, single-use CI tokens, refusal logs): research [R3](research.md#r3-a-new-inbox-event-kind-for-a-job-on-an-owned-merge-request-with-a-run-of-its-own), [R9](research.md#r9-a-job-that-cannot-start) and [R10](research.md#r10-one-submission-per-ci-token-and-refusal-logs).

**Input**: Feature specification from `specs/005-merge-request-automations/spec.md`

## Scope

This plan covers spec user stories 1 and 2:
- running a prompt on a merge request from CI or a script;
- CI identity tokens.

Repository automations, user stories 3 and 4 (FR-014–FR-027, SC-003 and SC-004), are excluded and deferred to a later feature. Their design is in this branch's first commit (`1b15a6c93`), in `research.md` R7–R14 and in the `contracts/repository-config.md` and `contracts/webhook-events.md` it held.

## Summary

Today DAIV runs on a merge request only when someone mentions it there. This plan lets a CI job or a script start that same run without a comment.

1. **A merge request target on jobs (P1).**
   - **The request:** `POST /api/jobs` and the MCP job tool accept `"merge_request": <iid>` on a `repos` entry.
   - **Where the run goes:** where a mention from the caller would (FR-006). DAIV already has a path for each case, and the job takes whichever one a mention would. With no owning session, it takes the direct path, which moves from `webhooks` down into `sessions/merge_request_runs/` so `jobs` and `mcp_api` can call it. With an owning session, it becomes a new inbox event kind, `MERGE_REQUEST_JOB`, posted in that session with a run of its own, as leg follow-ups are ([R1](research.md#r1-a-job-on-a-merge-request-reuses-the-two-mention-paths), [R3](research.md#r3-a-new-inbox-event-kind-for-a-job-on-an-owned-merge-request-with-a-run-of-its-own)).
   - **Concurrency:** runs on a merge request leave the one-active-run constraint, and the session slot serialises them ([R2](research.md#r2-merge-request-targeted-runs-leave-the-one-active-run-constraint)).
   - **Jobs that can't start:** a job on an archived session is refused with nothing posted on the merge request. A job whose merge request merges or closes before its run starts ends `FAILED` with a note on the merge request ([R9](research.md#r9-a-job-that-cannot-start)).
2. **CI identity tokens (P2).**
   - **Verification:** `CIIdentityBearer` verifies GitLab CI ID tokens and GitHub Actions OIDC tokens with PyJWT against the platform's published signing keys. It checks the issuer, the audience (DAIV's address), the expiry and the stable repository id ([R4](research.md#r4-ci-identity-tokens-are-verified-with-pyjwt-against-the-platforms-published-keys), [R5](research.md#r5-the-ci-principal-and-what-it-may-do)).
   - **Attribution:** the run belongs to the pipeline's triggering account, whether a linked DAIV user or an external platform account.
   - **No personal tools:** a new `Run.acts_as_user = false` keeps such runs from loading the person's tools or environments ([R6](research.md#r6-attribution-without-authority-runacts_as_user)).
   - **Single use and refusal logs:** a token submits one accepted job, tracked by its `jti` in the cache until it expires. Each refusal logs at WARNING with the failed check, never the token ([R10](research.md#r10-one-submission-per-ci-token-and-refusal-logs)).

## Technical Context

**Language/Version**: Python 3.14.

**Primary Dependencies**:
- Django 6 and django-ninja: multiple auth classes on `jobs_router`, and the `RepoSubmitItem` schema;
- python-gitlab and PyGithub, through `RepoClient`;
- **PyJWT with the `crypto` extra, which becomes a direct dependency.** It is already locked at 2.15.1 through allauth, mcp and pygithub, so this adds no new package.

**Storage**: PostgreSQL. In `sessions`:
- one new `Run` column, `acts_as_user`;
- a changed `run_one_active_per_session` condition.

That is two schema migrations in `sessions` (`0045`, `0046`) and no data migration. Detail in [data-model.md](data-model.md).

**Testing**: pytest, pytest-django and pytest-asyncio.
- **Database:** async tests that write use `django_db(transaction=True)`.
- **Tokens:** CI tokens are signed in-test with a generated RSA key, and the keys endpoint is stubbed, so tests make no network calls.
- **Platforms:** git-platform clients are stubbed per platform, and both platforms get a case for every token claim mapping.
- **Evals:** `inbox` and `code_review` before and after ([R7](research.md#r7-agent-behaviour-and-measurement)).

**Target Platform**: Linux containers: the web process (jobs API, webhooks) and workers (runs).

**Project Type**: a Django web service with background workers that run the agent.

**Performance Goals**: token verification costs one keys fetch per platform per cache period, plus one repository lookup per request, which `RepoClient` already caches. A token submission adds one cache write. A merge request target adds one merge request read per submission, and one more when its run starts.

**Constraints**:
- **Fail closed** on token verification: missing keys, an unreachable issuer or claim store, a reused token, or an unknown repository all answer `401` or `404`, with no detail (Principle I).
- **Attribution without authority**: personal tools and environments never load for `acts_as_user = false` runs (FR-011, SC-007).
- **Unchanged behaviour:** mentions on both paths, and the existing jobs with `ref` or `thread_id`, behave exactly as before.
- **Layering:** the trigger apps call down into `sessions`, and no trigger app imports another.

**Scale/Scope**: the work lands in six apps: `accounts`, `codebase` (settings, the repository id and the merge request's closed flag), `core` (settings), `sessions`, `jobs` and `mcp_api`, plus the code that moves out of `webhooks`. There are no new pages.

## Constitution Check

*GATE: checked before Phase 0 and again after Phase 1 design. Both pass. One design point needs the maintainer's eye; it is listed below.*

| Principle | How the plan complies | Evidence at review |
|---|---|---|
| I. Secure and isolated | The CI token is checked for signature, issuer, audience, expiry and the repository's stable id, and the check fails closed. The token is the only access proof, and only a pipeline of that repository can get one. It submits one accepted job, so a leaked token can't start more. `allowed_usernames` applies to the signed login. `acts_as_user = false` withholds personal MCP servers, USER sandbox environments and the assignee. Refusals don't say which check failed, and the logs never hold the token | Tests for forged, expired, wrong-audience, wrong-repository, fork and reused tokens, including a concurrent reuse. Tests that `acts_as_user=false` runs get no personal servers and no USER environment |
| II. Architecture boundaries | The direct merge request path moves into `sessions` (layer 8), so `jobs`, `mcp_api` and `webhooks` call one service and none imports another. `CIIdentityBearer` lives in `accounts` (identity) and calls down into `codebase`. No new `ignore_imports`: the one-release task alias in `webhooks.tasks` imports downwards. Behaviour sits in classes: `MergeRequestRuns`, `RunStarter`, `CIIdentityVerifier` | `make lint-imports`; review |
| III. Platform and provider neutrality | Every token claim has a GitLab and a GitHub mapping with the same meaning (R4), and GitHub Enterprise Server has its own issuer setting. Merge request reads go through `RepoClient`. No model or provider is named | Paired GitLab/GitHub tests for token claims and merge request targets |
| IV. Tests prove behaviour | Each new or changed module has its mirror test file (Project Structure). Failing-first tests cover the merge request target on both paths, the constraint change, token verification, polling visibility and `acts_as_user` | `make test` output in the pull request |
| V. Measured agent behaviour | The `MERGE_REQUEST_JOB` rendering and the prompt-as-request input change what the model sees, so `inbox` and `code_review` are measured before and after on `EVAL_MODEL` (R7). `/code-review` itself is unchanged, so ReviewBench is not run | `compare_runs.py` output in the pull request |
| VI. Honest failure handling | Every run on a merge request ends with one comment: its result, a question or a failure note (FR-003, SC-002). A job that can't start ends `FAILED`, never `QUEUED` forever, and says why (R9). Unreachable signing keys or claim store log at ERROR. Token refusals log at WARNING, since the operator need not act on them. Refusals carry no internals | Tests on the failure and not-started comments, the stopped inbox events, the refusal bodies and the log levels |
| VII. Documentation | Updated jobs API and pull request assistant pages, and the environment variables reference. Two `CHANGELOG.md` `Added` lines | `mkdocs build --strict`; `test_env_variables_doc.py`, `test_docs_style.py`, `test_docs_links.py` |

**Gates**: `make lint`, `make lint-typing`, `make test`, a docs build, the changelog lines, and evals for `inbox` and `code_review` (Principle V).

**For the maintainer's eye**:
- The `run_one_active_per_session` condition change (R2) lets merge-request-targeted API and MCP runs share a session, as webhook runs already do. The session slot still allows one running at a time.
- The R6 finding: [MCP tools](../../docs/customization/mcp-tools.md) says unowned-merge-request mentions load no personal servers, but the code loads them. It's out of scope here.

**Post-design re-check (after Phase 1)**: unchanged. The data model adds no import across layers, and `acts_as_user` is in `sessions`. The contracts expose no field a caller couldn't already see, and a CI principal reads only its own repository's runs, attributed to its own actor.

## Project Structure

### Documentation (this feature)

```text
specs/005-merge-request-automations/
├── spec.md
├── plan.md                    # this file
├── research.md                # Phase 0: R1–R10, SR1–SR3
├── data-model.md              # Phase 1: Run.acts_as_user, the constraint, inbox kind, settings, CIIdentity, used CI tokens
├── quickstart.md              # Phase 1: validation steps
├── contracts/
│   ├── jobs-api.md            # merge_request target, CI identity auth, polling
│   └── merge-request-runs.md  # the sessions service the trigger apps call
├── checklists/requirements.md
└── tasks.md                   # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
daiv/
├── accounts/api/ci_identity.py            # new: CIIdentityBearer, CIIdentity, CIIdentityVerifier (PyJWKClient per issuer), CIIdentityTokens (single use)
├── codebase/
│   ├── conf.py                            # + GITHUB_ACTIONS_OIDC_ISSUER
│   └── base.py, clients/                  # the repository's platform id, if Repository lacks it; MergeRequest.closed
├── core/conf.py                           # + JOBS_CI_IDENTITY_AUDIENCE (defaults to EXTERNAL_URL)
├── core/api/throttling.py                 # JobsRateThrottle keys a CIIdentity on its pk
├── sessions/
│   ├── models.py                          # + Run.acts_as_user and acting_user_id, constraint condition
│   ├── merge_request_runs/                # new: moved from webhooks
│   │   ├── starter.py                     #   RunStarter, Requester, Mention (from webhooks/dispatch.py)
│   │   ├── service.py                     #   MergeRequestRuns: astart (owned → inbox, else direct)
│   │   ├── base.py, addressor.py          #   moved webhooks/managers/base.py and review_addressor.py
│   │   └── tasks.py                       #   address_merge_request_task (dedup on repo, iid, request key; not-started guards)
│   ├── inbox/merge_request_jobs.py        # new: MergeRequestJobs (post with its own run, stop), after leg_follow_ups.py
│   ├── inbox/kinds.py, render.py, complete.py, drain.py, splits.py   # MERGE_REQUEST_JOB; acting user
│   ├── queue.py, services.py, multi_repo.py, admission.py # merge_request target, acts_as_user, CI requester
│   └── migrations/0045_run_acts_as_user.py, 0046_run_active_constraint_merge_request.py
├── jobs/api/schemas.py, views.py          # merge_request field; auth=[CIIdentityBearer(), AuthBearer()]; CI polling rule
├── mcp_api/server.py                      # merge_request on the job tool
├── webhooks/
│   ├── dispatch.py                        # MR mentions and issue runs call sessions.merge_request_runs
│   ├── tasks.py                           # address_mr_comments_task re-export (one release)
│   └── managers/                          # review_addressor.py, base.py removed (moved)
└── pyproject.toml, uv.lock                # pyjwt[crypto] direct

tests/unit_tests/                          # mirrors each module above, one test file per module
docs/
├── features/jobs-api.md, pull-request-assistant.md
└── reference/env-variables.md
CHANGELOG.md
```

**Structure Decision**: This is the existing Django monolith under `daiv/`, with tests mirroring it. The new `sessions/merge_request_runs/` package follows the `sessions/inbox/` precedent: a concern big enough for several modules gets its own package.

## Complexity Tracking

No constitution violations to justify.
