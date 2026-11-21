# Research: Merge Request Automations

Phase 0 of [plan.md](plan.md). Each decision names what was chosen, why, and what was rejected. Paths are under `daiv/` unless they start with `docs/`, `tests/` or `specs/`.

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

**Decision**: Add `EventKind.MERGE_REQUEST_JOB` (wake) with the payload `{prompt, agent_model?, acts_as_user}`.
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

**Decision**: Add `Run.acts_as_user` (boolean, default `true`). Runs started by a CI identity token and automation runs set it to `false`.
- **Where it applies:** every place that turns `Run.user` into an acting user — `queue.py:75`, `aget_task_run`, the drain (`drain.py:362,421`) and admission (`admission.py:103-117,134-136`) — yields `None` when it is `false`.
- **Effect:**
  - no personal MCP servers: `build_runtime_servers` adds USER rows only for an acting user;
  - no USER sandbox environment: `resolve_env_for_run(user=None)`;
  - no assignee taken from the acting user (`publishers.py:573-596`).
- **Unchanged:** `Run.user` and the `external_*` fields still decide spend, visibility and "Mine".

**Rationale**: Today `acting_user_id` is always `Run.user_id`, and there is no way to attribute a run to a person without lending it their tools.

**Finding (out of scope)**: [MCP tools](../../docs/customization/mcp-tools.md) line 164 says a mention on a merge request no session owns has no acting user. The code acts as the linked user there (`webhooks/dispatch.py:307`, `webhooks/tasks.py:156`). Only the sandbox environment skips USER environments. The page should be corrected separately.

## R7. Automations live in `.daiv.yml` and are validated one entry at a time

**Decision**: Add `RepositoryConfig.automations: list[Automation]` (`codebase/repo_config.py`), parsed the way `ModelChoiceMap` parses model choices: `SkipValidation` plus a `BeforeValidator` that validates each entry, then drops and logs any invalid one (`core/model_choice.py:81-97` is the pattern).
- **Entry fields:** `name` (a unique slug), `on` (a non-empty subset of `merge_request.opened`, `merge_request.updated`, `merge_request.ready`), `prompt` (non-blank) and `filters`.
- **Filters:**
  - `drafts`: bool, default `false`;
  - `target_branches`: glob list, default any;
  - `paths`: glob list, default any;
  - `daiv_merge_requests`: bool, default `false`.
- **Duplicate names:** the first entry with a name wins, and later ones are dropped and logged.
- **Cache:** the list round-trips through the config cache's `model_dump(exclude_unset=True)`.
- **Reading rule:** automations come from the default branch only (FR-014), the rule the whole file already follows.
- **Docs test:** `test_docs_reference.py` counts a list as a single leaf, so `automations` needs one row in [Repository configuration](../../docs/customization/repository-config.md). Its subkeys are documented in a table of their own.

**Rationale**: FR-021 forbids losing the whole file to one bad entry. The repository already solved that for `models.skills`.

## R8. Events come from the merge request webhooks on both platforms

**Decision**:

| Event | GitLab (`object_kind: merge_request`) | GitHub (`pull_request`) |
|---|---|---|
| `merge_request.opened` | `action == "open"` | `action == "opened"` |
| `merge_request.updated` | `action == "update"` and `object_attributes.oldrev` present | `action == "synchronize"` |
| `merge_request.ready` | `action == "update"` and `changes.draft` goes `true` → `false` | `action == "ready_for_review"` |
| Actor | `user` (`id`, `username`) | `sender` (`id`, `login`) |
| Head commit | `object_attributes.last_commit.id` | `pull_request.head.sha` |
| Draft, target branch | `object_attributes.draft`, `target_branch` | `pull_request.draft`, `base.ref` |

- **Model changes:** `webhooks/gitlab/models.py` gains `oldrev`, `last_commit`, `draft` and a top-level `changes`. GitHub's `PullRequest` already has `draft`, `head` and `base`.
- **Callbacks:** each platform's `MergeRequestCallback` / `PullRequestCallback` accepts the new actions and calls `sessions.automations.Automations(repo_id).aon_event(...)`. Closing, merging and reopening keep their current handling.
- **Routing:** no new route. Both views already dispatch on the payload's shape.
- **Subscriptions:** GitLab's `setup_webhooks` already subscribes to merge request events (`gitlab/client.py:375`). The GitHub App already subscribes to **Pull request** ([platform setup](../../docs/getting-started/platform-setup.md)).

**Rationale**: The merge request event carries the actor, head, draft state and target branch in one payload. Push events would need a lookup to find the merge request.

## R9. Bursts combine on the trailing edge, and each commit fires once

**Decision**: Add a new `AutomationFiring` row per (repository, merge request, automation, head commit), described in [data-model.md](data-model.md).

1. **An event arrives.** For each matching automation, the service upserts the pending firing for that merge request and automation. It records the newest head, actor and event, and sets `due_at = now + AUTOMATION_SETTLE_DELAY` (60 s). Any older pending firing is marked `superseded`. Then it enqueues `fire_automation_task.using(run_after=due_at)`.
2. **The task runs.** It loads the firing and exits unless it is still `pending` and due. Then it:
   - re-reads the merge request (it must still be open, and its head must be the firing's head);
   - applies the loop guards (R10) and the path filter (R12);
   - starts the run through `MergeRequestRuns` (R1) in the automation's session (R11).
3. **Duplicate events.** A unique constraint on (`repo_id`, `merge_request_iid`, `automation`, `head_sha`) makes a re-delivered or duplicate event a no-op (FR-017, SC-003).

**Rationale**: There is no trailing-edge debounce today. `DeduplicatingTask` keeps the first task, and the inbox supersedes events only at drain time. The 60-second settle delay matches `PUSH_CLASSIFY_DELAY` (`sessions/inbox/ownership.py:57`), and for the same reason: DAIV writes `Run.pushed_sha` only after a publish finishes, so guards checked at fire time see it.

**Alternatives rejected**: *`DeduplicatingTask` keyed on the merge request*: it would fire on the first push in a burst, not the last.

## R10. Loop guards are checked at fire time

**Decision**:
- **Automation pushes (FR-026):** a head commit that a run with trigger type `AUTOMATION` pushed is skipped as `automation_commit`. That means `Run.pushed_sha` or `sibling_pushed_shas` on such a run.
- **DAIV commits:** the check behind `Ownerships.aclassify_push` (`ownership.py:305-334`) moves into a `DaivCommits(repo_id).ais_daiv(sha)` method that both push classification and automations use. A commit counts as DAIV's when it is an ownership's `last_daiv_sha`, any run's `pushed_sha`, or in any session's `sibling_pushed_shas`.
- **Cap (FR-027):** the cap is derived, not stored. The count is the `fired` firings on this merge request whose `daiv_commit` is true, counted after its latest `fired` firing that has `daiv_commit` false. When the count reaches `site_settings.automations_max_daiv_push_runs` (default 3), the firing is skipped as `capped`. DAIV posts one comment, only for the first `capped` firing since that latest person push. A person's push fires normally, which resets the count by definition.
- **Results stay out of the owning session (FR-025):** this needs no new code. Automations post no inbox events, and DAIV's own comments are dropped before they can become one (`gitlab/callbacks.py:151`, `github/callbacks.py:143`). Tests pin it.

**Rationale**: Deriving the cap keeps a single source of truth and needs no reset logic. Checking at fire time, after the settle delay, avoids the race between the webhook and DAIV's publish bookkeeping.

## R11. Each automation has a session per merge request

**Decision**: The session id is `compute_automation_thread_id(repo_id, automation, iid)`, a UUID derived from `"{slug}:automation:{name}/{iid}"`. It sits beside `compute_thread_id` in `sessions/ids.py`.
- **Session fields:** origin `AUTOMATION`, `merge_request_iid` set, no `user`.
- **Each run:** carries its triggering person (`user` and `external_*`), `acts_as_user=false`, `muted=true` and `ask_user_enabled=false`.
- **Visibility:** people who can read the repository see these sessions through the existing rules (`sessions/managers.py` `_readable_repos_q`).
- **Archiving:** `MergeArchive` archives them on merge, as it does any session with the merge request's iid.
- **Title:** `"{automation name} on !{iid}"`, which gives FR-023 its session-side label.

**Rationale**: FR-024 needs a session apart from the owner's. Keeping one per automation and merge request, rather than one per push, groups an automation's reports in one place, and gives the spend and session views one row per automation and merge request.

## R12. Changed paths come from a new `RepoClient` method; an incomplete list doesn't block

**Decision**: Add `RepoClient.get_merge_request_changed_paths(repo_id, iid) -> ChangedPaths(paths: list[str], complete: bool)`.
- **GitLab:** reads `mr.changes()`, using `new_path` plus `old_path` on renames and the `overflow` flag (`gitlab/client.py:1030-1065` already calls it).
- **GitHub:** pages through `pr.get_files()`, which the platform caps at 3,000 files.

When `complete` is false and no listed path matches, the automation still runs and logs at INFO. (Spec refinement SR3.)

**Rationale**: The path filter controls cost, not security. On a merge request too large to list, skipping the review would be the surprising outcome.

## R13. Site settings

**Decision**: Add two `SiteConfiguration` fields:
- `automations_enabled` (bool, default `true`);
- `automations_max_daiv_push_runs` (int ≥ 1, default 3).

Each is also declared on `SiteSnapshot`, gets a default in `core/site_settings.py`, and gets the env vars `DAIV_AUTOMATIONS_ENABLED` and `DAIV_AUTOMATIONS_MAX_DAIV_PUSH_RUNS`. A new `FieldGroup(key="automations", match=("automations_*",), category="Runtime")` groups them, with the migration `core/0028`. The pattern is pipeline watch (`core/models.py:298-310,723-729`).

The CI identity settings are deployment settings, not site configuration:
- `CODEBASE_GITHUB_ACTIONS_OIDC_ISSUER` (`codebase/conf.py`);
- `DAIV_JOBS_CI_IDENTITY_AUDIENCE` (`core/conf.py`).

**Rationale**: An off switch has to be changeable at runtime by an admin. Issuers and audiences are fixed per deployment, like `CODEBASE_GITLAB_URL`.

## R14. A new origin, `AUTOMATION`

**Decision**: Add `SessionOrigin.AUTOMATION = "automation"`, labelled "Automation". Migration `sessions/0043`:
- alters the choices on `Run`, `Session` and `SpendRecord`;
- drops and re-adds `run_trigger_type_valid` and `session_origin_valid`, following the precedent of `0017_delegated_jobs.py`;
- changes `run_one_active_per_session` (R2);
- adds `Run.acts_as_user` (R6);
- creates `AutomationFiring`.

**Membership decisions:**
- not in `PERSON_STARTED`, so an automation run that publishes doesn't restore CI attempts;
- not in `webhooks()`;
- not in the inbox trigger literals;
- in `insights.py` `ORIGIN_SEGMENT_CSS`, which a test requires;
- in `home.py` `_started_from` as "Started by an automation";
- `notifications/signals.py` needs no rule, because the runs are muted.

CI-token jobs keep `API_JOB`, since they arrive through the jobs API.

## R15. Agent behaviour and measurement

**Decision**: What the model sees changes in two places:
1. The new `MERGE_REQUEST_JOB` rendering (R3). It reuses the mention note's wording.
2. Automation and merge-request-targeted runs on the direct path give the model the prompt where a mention's comment would be. That is the same `HumanMessage` shape the direct path already uses, with the caller's name.

Neither changes a skill, tool description or subagent. Measure with `make eval-prompts SUITES="inbox code_review"` before and after (Principle V), compared with `evals/compare_runs.py`. ReviewBench is not needed, because `/code-review` is unchanged.

## R16. Documentation

**Decision** (Principle VII):
- **New page:** `docs/features/automations.md`, with automations, the events and filters, the loop guards and the cap. It goes in the `mkdocs.yml` `nav` and `llmstxt`.
- **Jobs API** ([jobs-api.md](../../docs/features/jobs-api.md)):
  - the `merge_request` field;
  - a "CI identity tokens" section with GitLab `id_tokens` and GitHub Actions examples;
  - polling rules;
  - the code-review-on-every-push example replacing the comment-posting workaround (FR-028).
- **Pull request assistant** ([pull-request-assistant.md](../../docs/features/pull-request-assistant.md)): a pointer from review reports to automations.
- **Repository configuration** ([repository-config.md](../../docs/customization/repository-config.md)): the `automations` section.
- **Reference pages:**
  - [site-configuration.md](../../docs/reference/site-configuration.md) and [env-variables.md](../../docs/reference/env-variables.md): the new group and env vars;
  - [sessions.md](../../docs/features/sessions.md) and [spend.md](../../docs/features/spend.md): the "Automation" origin.
- **Glossary** (`constitutions/glossary/README.md`): add **Automation** and **Automation firing**.
- **Changelog:** `CHANGELOG.md` gets three `Added` lines.

## Spec refinements for approval

These change the spec's wording. The maintainer approved them with the plan on 2026-11-21, and they are applied to the spec:

- **SR1 (FR-005):** add "For a job authenticated by a CI identity token, the platform-signed token naming the repository is the access proof" (R5).
- **SR2 (FR-011):** add that a token-authenticated job is muted unless the request sets `muted` to false (R5).
- **SR3 (FR-016):** add that when DAIV cannot list every changed path, the paths filter lets the run start (R12).
