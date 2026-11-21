# Data Model: Merge Request Automations

Phase 1 of [plan.md](plan.md), for user stories 1 and 2. Decisions behind each change are in [research.md](research.md).

## Run (`sessions.Run`, changed)

| Field | Change | Meaning |
|---|---|---|
| `acts_as_user` | **new**, `BooleanField(default=True)` | Whether the run acts as `user`: loads their personal MCP servers and USER sandbox environments and assigns merge requests to them. `False` for runs started by a CI identity token. `user` and `external_*` still decide spend, visibility and "Mine" (R6) |
| `merge_request_iid` | unchanged; now also set on API and MCP runs that target a merge request | Their session is the merge request's, or its owning session |

**Constraint `run_one_active_per_session`**: the condition gains `merge_request_iid__isnull=True` (R2):

```text
status IN (READY, RUNNING)
AND trigger_type IN (api_job, mcp_job, delegated_job, delegation_resume)
AND merge_request_iid IS NULL
```

`test_active_constraint_literals_match_enums` keeps checking the literals. A new assertion pins the null condition.

**Acting user**: `Run.acting_user_id` is a property: `user_id if acts_as_user else None`. Every reader that used `user_id` as the acting user switches to it: `queue.aenqueue_run`, `services.aget_task_run`, `inbox/drain.py` and `admission.py`.

## InboxEvent kind `MERGE_REQUEST_JOB` (`sessions.inbox.kinds`, new)

| Aspect | Value |
|---|---|
| Wakes | yes |
| Requester | the job's caller (`author` = DAIV user, or `author_provider`/`author_uid`/`author_username` for a CI actor) |
| Payload | `{prompt: str, agent_model?: str, acts_as_user: bool, source: "api_job" \| "mcp_job"}` |
| Subject | `merge_request(iid)` |
| Source key | `job({request_key})`, unique per session |
| Run | its own `QUEUED` run, created with the event under the session lock (`MergeRequestJobs.post`), as a leg follow-up's is. The post is refused when the session is archived or the merge request isn't open |
| Trigger type of its run | `payload.source` |
| Taken | alone, with the session's remembered events; its own run is promoted to `READY` |
| Stopped at take | merge request merged or closed: failure `mr_merged` or `mr_closed`, the not-started note posted on commit, the run `FAILED` and never started. Session archived: failure `session_archived`, the run `FAILED`, nothing posted |
| Reply | the final message as a new top-level comment on the merge request; after `MAX_EVENT_ATTEMPTS`, the "could not complete" note |

## Session (`sessions.Session`, unchanged schema)

A merge-request-targeted job on a merge request no DAIV session owns uses the same session a mention would: `compute_thread_id(repo_id, MERGE_REQUEST, iid)`. When the job creates that session, its `user` is the caller, as the first commenter's is today.

## MergeRequest (`codebase.base.MergeRequest`, changed)

| Field | Change | Meaning |
|---|---|---|
| `closed` | **new**, `bool = False` | Closed without being merged. Set only by `get_merge_request`, as `merged` is: GitLab `state == "closed"`, GitHub `state == "closed"` and not merged |

## Deployment settings (changed)

| Setting | Module | Default | Meaning |
|---|---|---|---|
| `DAIV_JOBS_CI_IDENTITY_AUDIENCE` | `core/conf.py` | `DAIV_EXTERNAL_URL` | The `aud` a CI identity token must carry |
| `CODEBASE_GITHUB_ACTIONS_OIDC_ISSUER` | `codebase/conf.py` | `https://token.actions.githubusercontent.com` | The GitHub Actions token issuer; GitHub Enterprise Server sets `https://HOST/_services/token` |

The GitLab issuer is `CODEBASE_GITLAB_URL`, else `https://gitlab.com`.

## CIIdentity (`accounts.api.ci_identity`, in memory)

The principal `request.auth` holds on a token-authenticated request. It has no table.

| Attribute | Source |
|---|---|
| `provider` | `gitlab` or `github`, the active `CODEBASE_CLIENT` |
| `repo_id`, `repo_platform_id` | the token's `project_path`/`project_id` (GitLab) or `repository`/`repository_id` (GitHub), checked against the repository |
| `actor_uid`, `actor_username` | `user_id`/`user_login` or `actor_id`/`actor` |
| `user` | `resolve_linked_user(provider, actor_uid)`, or `None` |
| `sha` | `sha`, or `None` when a GitHub token lacks it |
| `jti`, `expires_at` | `jti`, `exp` |
| `pk` | `"ci:{provider}:{actor_uid}"`, the throttle key |

## Used CI tokens (cache, no table)

| Aspect | Value |
|---|---|
| Key | `jobs:ci-token:{provider}:{jti}` |
| Set | by `POST /api/jobs` with `cache.aadd`, before the submission starts anything |
| Expires | at the token's `exp` plus the 60 s leeway |
| Released | deleted when the submission accepts no job |
| Unreachable | the submission is refused with `401`, and the failure logs at ERROR |
