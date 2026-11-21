# Research: Merge Request Automations

Phase 0 of [plan.md](plan.md). Each decision names what was chosen, why, and what was rejected. Paths are under `daiv/` unless they start with `docs/`, `tests/` or `specs/`.

The plan covers user stories 1 and 2 only. The decisions for repository automations (stories 3 and 4), R7–R14 in this branch's first commit `ef67f2aaf`, are deferred with them.

## R1. A job on a merge request reuses the two mention paths

**Decision**: FR-006 says a job runs where a mention by its caller would. DAIV already has two paths for a mention, and the job uses whichever one a mention would take:

- **No DAIV session owns the merge request:** the direct path. Today this is `webhooks/dispatch.py` `dispatch_mr_comment_run` and `_start_run`, then `webhooks/tasks.py` `address_mr_comments_task`, then `webhooks/managers/review_addressor.py`. The run goes into the merge request's own session (`sessions/ids.py` `compute_thread_id`, scope `MERGE_REQUEST`), with `Scope.MERGE_REQUEST` and the `MergeRequest` in `RuntimeCtx`, and its final message is posted on the merge request (`_on_success`, `review_addressor.py:123-132`).
- **A DAIV session owns it:** an inbox event in the owning session, which the drain turns into a run acting as the requester. The reply is posted on the merge request (`sessions/inbox/complete.py` `_reply_to_mentions`).

The direct path moves from `webhooks` into a new `sessions/merge_request_runs/` package, so that `jobs`, `mcp_api` and `webhooks` can all call it. Trigger apps cannot import each other ([layering](../../constitutions/architecture/layering.md) layer 11). Everything that moves imports only from `sessions` or below: `Author`, `Mention`, `_start_run`, `_post_comment`, `_reply_archived`, `_fetch_source_branch`, the owned-versus-unowned branching of `dispatch_mr_comment_run`, `address_mr_comments_task`, `managers/review_addressor.py` and `managers/base.py`. `webhooks/dispatch.py` keeps the issue paths and calls the moved service for merge request mentions.

The moved task registers under a new path. `webhooks/tasks.py` re-exports it for one release, so task rows already queued under `webhooks.tasks.address_mr_comments_task` still resolve. `jobs/tasks.py` re-exports `run_job_task` the same way. This is an import from a higher layer into a lower one, so it needs no `ignore_imports` line.

**Rationale**: "Where a mention would run" is exactly what these two paths already do: the session, the lock, the merge request context the code-review skill reads as a review on the merge request, the reply, the archived-session reply and the merged-merge-request guard. Reusing them guarantees the parity FR-006 asks for.

**Alternatives rejected**:
- *Route every merge-request-targeted job through the inbox, including on merge requests no session owns.* The run would then go through the inbox (`Scope.GLOBAL`, with the merge request found from the branch by `GitMiddleware`) while mentions on the same session still take the direct path. One session would have two execution paths, and mentions and jobs on the same merge request would behave differently.
- *Duplicate the direct path in `jobs`.* That creates a second mechanism for one job (Principle II).

## R2. Merge-request-targeted runs leave the one-active-run constraint

**Decision**: Add `merge_request_iid__isnull=True` to the condition of `Run`'s `run_one_active_per_session` constraint (`sessions/models.py:557-563`). API and MCP runs on a merge request then behave like webhook runs, which share deterministic sessions and are already outside the constraint. The session slot (`sessions/executor/lock.py` `wait_for_slot`, one slot per session whatever the holder) still serialises them.

**Rationale**: Two CI jobs on one merge request, a minute apart, must both run, one after the other. Under today's constraint the second `create_run` would raise `IntegrityError`. Job runs never set `merge_request_iid` today (`sessions/services.py:494`), so the condition changes nothing for existing runs.

**Alternatives rejected**: *Answer the second job with `QUEUED` through the inbox*: that is R1's rejected mixed path. *A new trigger type outside the constraint*: it would split the "Started by" spend rows for API jobs in two.

## R3. A new inbox event kind for a job on an owned merge request

**Decision**: Add `EventKind.MERGE_REQUEST_JOB` (wake) with the payload `{prompt, agent_model?, acts_as_user, source}`.
- **Requester:** the job's caller, or the token's triggering account (R5).
- **Trigger type:** the drain gives the run `API_JOB` or `MCP_JOB`, taken from the event's `source`, and `merge_request_iid`.
- **Rendering** (`sessions/inbox/render.py`): the prompt as a request from the requester, with the same note `MENTION_REPLY_NOTE` carries: the final message is posted on the merge request.
- **Reply** (`complete.py`): a `MERGE_REQUEST_JOB` event has no `discussion_id`, so its reply is a new top-level comment on the merge request. A failure after `MAX_EVENT_ATTEMPTS` posts the same "could not complete" note mentions get.

**Rationale**: A `MENTION` event is defined by a platform comment: it has a note id, a thread and a 👀 reaction. A job has none of these, and spend must show it as an API or MCP run, not a webhook one.

**Alternatives rejected**: *Reuse `MENTION` with empty note fields*: the reply code and the reaction both assume a comment exists.

## R4. CI identity tokens are verified with PyJWT against the platform's published keys

**Decision**:
- **Library:** add `pyjwt[crypto]` as a direct dependency. It is already in `uv.lock` at 2.15.1, pulled in by allauth, mcp and pygithub. `jwt.PyJWKClient` fetches the platform's published signing keys (JWKS), caches them and refetches on an unknown `kid`.
- **Algorithms:** only `RS256`.
- **Clock leeway:** 60 seconds.
- **Required claims:** `iss`, `aud`, `exp`, `iat`, plus the repository and actor claims below.

| | GitLab | GitHub |
|---|---|---|
| Issuer (`iss`) | `CODEBASE_GITLAB_URL`, else `https://gitlab.com` | `https://token.actions.githubusercontent.com`; GitHub Enterprise Server sets `CODEBASE_GITHUB_ACTIONS_OIDC_ISSUER` (`https://HOST/_services/token`) |
| Signing keys | `{issuer}/oauth/discovery/keys` | `{issuer}/.well-known/jwks` |
| Repository | `project_id`, matched against the repository's platform id; `project_path` for logs | `repository_id`, matched against the repository's id; `repository` for logs |
| Triggering account | `user_id`, `user_login` | `actor_id`, `actor` |
| Commit | `sha` | `sha` |

- **Audience:** `DAIV_EXTERNAL_URL` (`core/conf.py:16`), the address DAIV already advertises. Operators can override it with `DAIV_JOBS_CI_IDENTITY_AUDIENCE`.
- **Repository match:** done by the stable numeric id the platform reports for the targeted `repo_id`, which `RepoClient` already loads. Paths can be renamed; GitLab warns against trusting mutable claims alone.
- **Fail closed:** when the signing keys cannot be fetched, the request gets `401` and the failure logs at ERROR. GitLab can't issue a token for a fork pipeline that names the upstream project, because in merge request pipelines `project_path` is the source project. GitHub gives fork `pull_request` workflows no `id-token` permission. Both therefore fail the repository match.

**Rationale**: This is the standard keyless CI-to-service exchange (cloud workload identity, Vault, PyPI trusted publishing). It removes the long-lived key from CI and binds each request to one pipeline, one repository and one person.

**Alternatives rejected**:
- *joserfc or authlib*: they are transitive through fastmcp and offer no key cache we'd use.
- *allauth's `jwtkit`*: it's an internal module, not a public API.
- *Trusting `GITLAB_USER_ID` from the body*: any branch's CI file could set it.

## R5. The CI principal and what it may do

**Decision**: Add `CIIdentityBearer` (`accounts/api/ci_identity.py`). `jobs_router` uses `auth=[CIIdentityBearer(), AuthBearer()]`; django-ninja tries them in order (`operation.py:575-592`).
- **Which tokens it claims:** only a bearer value that is a three-segment JWT whose header names a `kid`. For anything else it returns `None`, so API keys keep working. Being first, it stops JWTs from costing an API-key lookup.
- **`request.auth`:** a `CIIdentity`, not a `User`. It carries the platform, the repository id and path, the actor id and login, the linked DAIV user or `None`, and `pk = "ci:{provider}:{actor_id}"`, which `JobsRateThrottle` keys on (FR-012).
- **Submissions:**
  - exactly one `repos` entry, whose repository is the token's;
  - no `thread_id`, no `shared_session`, no `references`;
  - `mcp_overrides` may name global servers only (FR-009, FR-011).
- **Access:** the token is the access proof. Only a pipeline of that repository can obtain it, and the repository must be in DAIV's catalog. DAIV does not consult `RepositoryAccess`, because an unlinked actor has no rows.
- **Allowlist:** the repository's `allowed_usernames` must include the actor's login when the list is set (FR-012). It is checked against `user_login` or `actor`, which is safe here because the platform signs it.
- **Status polling:** `GET /api/jobs/{id}` with a token returns a run when its repository is the token's and the run's person is the token's actor (`Run.user`, or `external_provider` and `external_uid`). Any other run answers `404`.
- **Notifications:** `muted` defaults to `true` for token-authenticated submissions. An explicit `"muted": false` keeps notifications for a linked user. (Spec refinement SR2.)

**Rationale**: The token describes a pipeline, not a DAIV user. Pretending it's a `User` would pass `AuthBearer`-shaped checks that assume an account with stored access rows.

**Alternatives rejected**: *Exchanging the token for a short-lived DAIV API key*: an extra round trip and a credential to revoke, for no gain.

## R6. Attribution without authority: `Run.acts_as_user`

**Decision**: Add `Run.acts_as_user` (boolean, default `true`). Runs started by a CI identity token set it to `false`.
- **Where it applies:** every place that turns `Run.user` into an acting user — `queue.py:75`, `aget_task_run`, the drain (`drain.py:362,421`) and admission (`admission.py:103-117,134-136`) — yields `None` when it is `false`.
- **Effect:**
  - no personal MCP servers: `build_runtime_servers` adds USER rows only for an acting user;
  - no USER sandbox environment: `resolve_env_for_run(user=None)`;
  - no assignee taken from the acting user (`publishers.py:573-596`).
- **Unchanged:** `Run.user` and the `external_*` fields still decide spend, visibility and "Mine".

**Rationale**: Today `acting_user_id` is always `Run.user_id`, and there is no way to attribute a run to a person without lending it their tools.

**Finding (out of scope)**: [MCP tools](../../docs/customization/mcp-tools.md) line 164 says a mention on a merge request no session owns has no acting user. The code acts as the linked user there (`webhooks/dispatch.py:307`, `webhooks/tasks.py:156`). Only the sandbox environment skips USER environments. The page should be corrected separately.

## R7. Agent behaviour and measurement

**Decision**: What the model sees changes in two places:
1. The new `MERGE_REQUEST_JOB` rendering (R3). It reuses the mention note's wording.
2. Merge-request-targeted runs on the direct path give the model the prompt where a mention's comment would be. That is the same `HumanMessage` shape the direct path already uses, with the caller's name.

Neither changes a skill, tool description or subagent. Measure with `make eval-prompts SUITES="inbox code_review"` before and after (Principle V), compared with `evals/compare_runs.py`. ReviewBench is not needed, because `/code-review` is unchanged.

## R8. Documentation

**Decision** (Principle VII):
- **Jobs API** ([jobs-api.md](../../docs/features/jobs-api.md)):
  - the `merge_request` field;
  - a "CI identity tokens" section with GitLab `id_tokens` and GitHub Actions examples;
  - polling rules;
  - the code-review-on-every-push example replacing the comment-posting workaround (FR-028).
- **Pull request assistant** ([pull-request-assistant.md](../../docs/features/pull-request-assistant.md)): a pointer from review reports to the jobs API merge request target.
- **Environment variables** ([env-variables.md](../../docs/reference/env-variables.md)): `DAIV_JOBS_CI_IDENTITY_AUDIENCE` and `CODEBASE_GITHUB_ACTIONS_OIDC_ISSUER`.
- **Changelog:** `CHANGELOG.md` gets two `Added` lines.

## Spec refinements for approval

These change the spec's wording. The maintainer approved them with the plan on 2026-11-21, and they are applied to the spec:

- **SR1 (FR-005):** add "For a job authenticated by a CI identity token, the platform-signed token naming the repository is the access proof" (R5).
- **SR2 (FR-011):** add that a token-authenticated job is muted unless the request sets `muted` to false (R5).
- **SR3 (FR-016):** add that when DAIV cannot list every changed path, the paths filter lets the run start. It belongs to the deferred automations and applies when they are planned.
