---
description: "Trigger DAIV agents programmatically with the Jobs REST API from CI pipelines, scripts or other tools: submit a prompt, get a job ID, poll the result."
---

# Jobs API

The jobs API runs DAIV agents outside the git webhook flow. You send a prompt and a list of repositories, DAIV returns a job ID for each run, and you poll that ID for the result.

Use it to run agents from a CI pipeline, chain agent tasks, trigger runs from Slack bots, scripts or other tools, or start a one-off run with `curl`. A job can also run on a merge request and post its result there, for example a code review on every push ([Run a job on a merge request](#run-a-job-on-a-merge-request)). To run a prompt on a recurring schedule without any external trigger, use [scheduled jobs](scheduled-jobs.md).

## Before you start

You need:

- A DAIV account with an API key, created as described in [Create an API key](#create-an-api-key). A GitLab CI job or a GitHub Actions workflow can use its [CI identity token](#ci-identity-tokens) instead.
- Write access to every repository you name in `repos`. A CI identity token is the proof of access for its own repository.
- For multi-repository sessions (`shared_session`), a daiv-sandbox release with path-scoped egress rules and repository seeding. Deploy the sandbox release first: with an older sandbox the run fails before any command runs, with an error that says so. Single-repository runs are unaffected.

## Create an API key

An API key belongs to one user and authenticates the jobs API and every other API surface, including the [MCP endpoint](mcp-endpoint.md#api-key-authentication), chat completions and the sessions API. Create it from the dashboard, or from the command line for headless provisioning.

!!! warning
    DAIV shows the full key once, when you create it, and cannot show it again. Copy it before you leave the page; if you lose it, revoke the key and create another.

To create a key in the dashboard:

1. Open the account menu at the bottom of the sidebar and select **API keys** (`/accounts/api-keys/`).
2. In **Create a new key**, enter a name, for example `CI/CD`.
3. Select **Create key**.
4. Select **Copy** next to **Your new API key**.

To create a key from the command line, run `django-admin` in the DAIV app container:

```bash
docker compose exec app django-admin create_api_key <username> --name "my-key"
```

Replace `<username>` with the username of the key's owner. The command prints the key once, in the form `prefix.secret`. For the `--expires-at` option and its time-zone rule, see [`create_api_key`](../reference/management-commands.md#create_api_key).

!!! warning
    Revoking a key cannot be undone. The key stops authenticating immediately on every API surface, so any script or client that uses it fails until you give it another key.

To stop a key from working, select **Revoke** next to it on the **API keys** page.

Admins see every user's keys; other users see their own.

## Authenticate a request

Pass the key as a Bearer token in the `Authorization` header of every request:

```bash
curl -X POST https://daiv.example.com/api/jobs \
  -H "Authorization: Bearer <api-key>" \
  -H "Content-Type: application/json" \
  -d '...'
```

Replace `<api-key>` with your key. A missing or invalid key returns `401`. A CI job can pass its identity token the same way; see [CI identity tokens](#ci-identity-tokens).

## Rate limiting

DAIV limits job submissions per authenticated user, or for a [CI identity token](#ci-identity-tokens) per account that triggered the pipeline, and returns `429 Too Many Requests` when you exceed the limit. The same budget applies to the [MCP endpoint](mcp-endpoint.md) and to chat completions. An admin sets the rate in **Configuration** under the **Jobs** group, or with `DAIV_JOBS_THROTTLE_RATE`; the default and the accepted formats are in [Environment variables](../reference/env-variables.md#jobs-api).

## Submit a job

Send a `POST` request to `/api/jobs` with a prompt and the repositories to run it on. For example:

```bash
curl -s -X POST https://daiv.example.com/api/jobs \
  -H "Authorization: Bearer $DAIV_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "repos": [{"repo_id": "mygroup/myproject"}],
    "prompt": "List all Python files and summarize the project structure"
  }'
```

DAIV answers `202 Accepted`:

```json
{
  "batch_id": "5b9e8a3c-9b7e-4c0d-a1f5-7e2c8d4b1a90",
  "jobs": [
    {
      "job_id": "1adfbf7a-917e-4f2e-8f54-17a27c006ec5",
      "repo_id": "mygroup/myproject",
      "ref": null,
      "thread_id": "9c1e8a3c-9b7e-4c0d-a1f5-7e2c8d4b1a90",
      "status": "READY"
    }
  ],
  "failed": []
}
```

Each entry in `jobs` is an independent run, so poll each `job_id` separately. Its `status` is `READY` when the run can start, or `QUEUED` when the session named by `thread_id` is busy; see [Job lifecycle](#job-lifecycle). DAIV reports a repository it rejects before enqueuing, such as an unknown `repo_id`, in `failed` as `{repo_id, ref, error}`, and still runs the rest of the batch.

For every field of the request, see [Request fields](#request-fields).

## Poll job status

Send a `GET` request to `/api/jobs/{job_id}`. DAIV answers `200 OK`:

```json
{
  "job_id": "1adfbf7a-917e-4f2e-8f54-17a27c006ec5",
  "status": "SUCCESSFUL",
  "thread_id": "9c1e8a3c-9b7e-4c0d-a1f5-7e2c8d4b1a90",
  "result": "Here are the Python files...",
  "merge_request_url": null,
  "artifacts": [
    {
      "id": "3f1c2b6e-0d7a-4f0e-9c4b-2a8d6e1f5b70",
      "title": "Dependency audit",
      "filename": "dependency-audit.html",
      "content_type": "text/html",
      "kind": "html",
      "size": 48213,
      "url": "https://daiv.example.com/dashboard/sessions/9c1e8a3c-9b7e-4c0d-a1f5-7e2c8d4b1a90/artifacts/3f1c2b6e-0d7a-4f0e-9c4b-2a8d6e1f5b70/",
      "download_url": "https://daiv.example.com/dashboard/sessions/9c1e8a3c-9b7e-4c0d-a1f5-7e2c8d4b1a90/artifacts/3f1c2b6e-0d7a-4f0e-9c4b-2a8d6e1f5b70/raw/?download=1"
    }
  ],
  "error": null,
  "created_at": "2026-03-27T18:22:39.012Z",
  "started_at": "2026-03-27T18:22:39.401Z",
  "finished_at": "2026-03-27T18:22:52.402Z"
}
```

`merge_request_url` holds the merge request DAIV opened when the agent committed and pushed code changes, and is `null` otherwise, for example on a read-only triage run. In a [multi-repository session](#multi-repository-sessions) it is the main repository's merge request, else the first one opened. `thread_id` identifies the thread the job ran on; pass it back as `thread_id` on a new submission to continue the conversation.

`artifacts` lists the files the agent published with its `publish_artifact` tool, such as reports, datasets and charts; see [Artifacts](sessions.md#artifacts). Each entry has the viewer page (`url`), a direct download (`download_url`), and a `kind` (`markdown`, `html`, `image`, `text` or `other`) that says how the viewer renders it. Opening either URL needs a signed-in DAIV user who can see the run. An artifact that a later job on the same thread revises keeps its ID and URLs, and appears under the job that first published it and under every job that revised it, with its current file ([Revise an artifact](sessions.md#revise-an-artifact)). The list is empty for a run that published nothing, and fills as the run progresses, so you can read it while the job is `RUNNING`. If DAIV cannot list the artifacts, `artifacts` is empty and `artifacts_error` says why; the rest of the status is unaffected.

`result` holds the agent's last response in full, not the rest of the run's messages. DAIV deletes a finished job's stored task result after 14 days, and from then on `result` holds the stored summary of the response, cut at 2,000 characters; the job's token usage and cost are kept. The MCP `get_job_status` tool behaves the same.

For the meaning of each `status`, see [Job status values](#job-status-values).

## Answer a question

When a job comes back `WAITING_INPUT`, the agent stopped to ask you a question instead of guessing. To answer it:

1. Read `question` for the questions asked, and `result` for their rendered text.
2. Send a new `POST /api/jobs` request with the same `thread_id` and your answer as `prompt`:

    ```bash
    curl -s -X POST https://daiv.example.com/api/jobs \
      -H "Authorization: Bearer $DAIV_API_KEY" \
      -H "Content-Type: application/json" \
      -d '{
        "repos": [{"repo_id": "mygroup/myproject"}],
        "prompt": "Use PostgreSQL.",
        "thread_id": "9c1e8a3c-9b7e-4c0d-a1f5-7e2c8d4b1a90"
      }'
    ```

3. Poll the new `job_id`. DAIV starts a new run that continues the agent's work from where it stopped.

The `thread_id` rules in [Request fields](#request-fields) apply to the answer too: `repos` has exactly one entry, or for a [multi-repository session](#continuing-a-multi-repository-session) the main repository alone or the session's full list, and the thread's most recent run must be yours.

You do not have to poll for the question. When a job ends in `WAITING_INPUT`, DAIV sends you a [notification](notifications.md) listing the questions, unless you muted it or the job asked on a merge request, mentioning you ([Run a job on a merge request](#run-a-job-on-a-merge-request)). For a multi-repository batch, the run counts in the batch rollup, and appears in the email and Rocket.Chat versions, once every repository's run has settled.

## Run a job on a merge request

Name a merge request with `merge_request` on the `repos` entry, in place of `ref`. DAIV runs the prompt with the merge request as its context, on its source branch, the way a mention of DAIV on that merge request would, and posts the run's final message as one comment on the merge request, from DAIV's account. `merge_request` is the GitLab merge request IID or the GitHub pull request number. For example, to review merge request `!42`:

```json
{
  "repos": [{"repo_id": "mygroup/myproject", "merge_request": 42}],
  "prompt": "/code-review"
}
```

The entry must be the only one in `repos`, with no `ref` and no per-repository `prompt` (`422` otherwise), and the request can't set `shared_session` or `thread_id` (`400`). You need write access to the repository, as for any job; without it the request gets the usual opaque `404`, which does not confirm the merge request exists.

The run goes where a mention by you would:

- **No DAIV session owns the merge request:** the run goes into the merge request's own session, which later mentions on the merge request share, so they have the job's result in context. The job starts `READY`, and runs after any run already in that session.
- **A DAIV session owns it** (DAIV published the merge request): the job runs as one turn of that session, acting as you. It is `QUEUED` while the session is busy. Each job keeps its own `job_id`, so two jobs sent while the session is busy run as two turns, one after the other.

`references`, `mcp_overrides` and `environment` apply to the job's run only. In a session that owns the merge request, the references join the session's for that run, and `mcp_overrides` apply on top of the session's own; the session keeps its own references and MCP servers for later turns. A named `environment` replaces the session's for that turn.

The job ends with one of these on the merge request:

- **The result:** the final message, also returned in `result`.
- **A question:** the question, mentioning the git platform account your DAIV account is linked to, if any, and the job is `WAITING_INPUT`. Anyone who mentions DAIV on the merge request answers it, or you send a new job with the same target. A question that mentions you sends no separate [notification](notifications.md#what-produces-a-notification).
- **A failure:** a short comment that DAIV could not complete the request, naming the provider's failure or a DAIV restart that interrupted the run when either caused it, and the job is `FAILED`. To run it again, send the job again: its session does not offer **Retry** for it ([Retry a run](sessions.md#retry-a-run)).
- **A merge or close before the run started**, including while the job waited for another run in its session: DAIV posts "This merge request was merged before the job could start, so I didn't run it." ("closed" for a closed one), and the job is `FAILED` with that text in `error`.
- **A deleted source branch:** when the run goes into the merge request's own session and the source branch no longer exists, DAIV posts "The source branch of this merge request no longer exists, so I didn't run the job.", and the job is `FAILED` with that text in `error`. A merge request merged or closed meanwhile gets the merge or close note instead.
- **A repository you can't run on:** when the session that owns the merge request also works on a repository you can't run on, DAIV posts "The person this job would run as can't run on every repository its session works on, so I didn't run it." The job is `FAILED` with that text in `error`, or, when the session was idle, refused in `failed` with that text.

DAIV refuses the job, and puts it in `failed` with the reason, when the merge request is not in the repository (`Merge request !42 was not found in mygroup/myproject.`), is merged or closed (`Merge request !42 is merged.`), its DAIV session is archived, the owning session works on a repository you can't run on (above), or the run could not start (`The job on merge request !42 could not start.`). Only the refusal for a repository you can't run on posts on the merge request. An archived session's refusal links the session to unarchive. A session archived after DAIV accepted the job ends the job `FAILED` with `Not run: its session was archived` in `error`, and DAIV posts nothing on the merge request.

To run this from CI on every push, see [Review every push to a merge request](#review-every-push-to-a-merge-request).

## CI identity tokens

A GitLab CI job or a GitHub Actions workflow can authenticate to the jobs API with the short-lived identity token its platform signs for it, so your CI stores no DAIV key. The run belongs to the account that triggered the pipeline, not to a shared key's owner. DAIV accepts identity tokens from the git platform it is configured for, on `POST /api/jobs` and `GET /api/jobs/{job_id}`; the MCP endpoint does not take them.

### Request a token in GitLab CI

Add an `id_tokens` entry whose `aud` is DAIV's address, `DAIV_EXTERNAL_URL`, and pass it as the Bearer token:

```yaml
daiv-job:
  id_tokens:
    DAIV_ID_TOKEN:
      aud: https://daiv.example.com
  script:
    - >
      curl --fail-with-body -sS -X POST "https://daiv.example.com/api/jobs"
      -H "Authorization: Bearer $DAIV_ID_TOKEN" -H "Content-Type: application/json"
      -d "{\"repos\":[{\"repo_id\":\"$CI_PROJECT_PATH\"}],\"prompt\":\"List all TODO comments\"}"
```

### Request a token in GitHub Actions

Give the workflow the `id-token: write` permission, and request a token for DAIV's address:

```yaml
permissions:
  id-token: write
  contents: read
```

```bash
TOKEN=$(curl -sS -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
  "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=https://daiv.example.com" | jq -r .value)
```

A `pull_request` workflow for a pull request from a fork gets no `id-token` permission, so it can't request a token, unless your repository is private and its Actions settings send write tokens to workflows from fork pull requests. Such a workflow runs in your repository, so its token names your repository, and DAIV accepts its job unless `allowed_usernames` leaves out the account that triggered it.

### What DAIV checks

DAIV accepts a token only when all of these hold:

| Check | GitLab | GitHub |
|---|---|---|
| Signature | `RS256`, by a key GitLab publishes at `<CODEBASE_GITLAB_URL>/oauth/discovery/keys` | `RS256`, by a key GitHub publishes at `<issuer>/.well-known/jwks` |
| Issuer | `CODEBASE_GITLAB_ID_TOKEN_ISSUER`, by default `CODEBASE_GITLAB_URL`, or `https://gitlab.com` when both are unset | `CODEBASE_GITHUB_ACTIONS_OIDC_ISSUER`, by default `https://token.actions.<subdomain>.ghe.com` when `CODEBASE_GITHUB_URL` is a GHE.com enterprise at `<subdomain>.ghe.com`, `https://<host>/_services/token` when it is a GitHub Enterprise Server at `<host>`, else `https://token.actions.githubusercontent.com` |
| Audience | `DAIV_JOBS_CI_IDENTITY_AUDIENCE`, by default `DAIV_EXTERNAL_URL` | the same |
| Expiry | not expired, with 60 seconds of leeway | the same |
| Repository | the token's `project_id` is the ID of the job's repository | the token's `repository_id` is the ID of the job's repository |
| Allowed users | when the repository's `allowed_usernames` is set, it lists the token's `user_login` | it lists the token's `actor` |
| First attempt | not checked | the token's `run_attempt` is `1` |
| Single use | no job was accepted with this token yet | the same |

The repository is matched by its numeric ID, which a rename does not change, so a pipeline in a fork can't submit jobs on the repository it was forked from. A token that fails a check gets `401` with `{"detail": "Unauthorized"}`, which does not say which check failed. DAIV logs each refusal once at `WARNING`, with the check and the repository path the token names, and never the token. When DAIV can't get a usable set of signing keys from the platform, or the platform fails to answer when DAIV reads the job's repository, the request gets `401` and DAIV logs an `ERROR` instead. The variables are in [Environment variables](../reference/env-variables.md#jobs-api).

On GitHub, the token of a re-run names the account that started the workflow run's first attempt, not the account that re-ran it, so DAIV refuses it rather than attribute the job to the wrong account. To submit the job again, start a new workflow run instead, for example by pushing a commit, or with a `workflow_dispatch` trigger.

### What a token can submit

A request a token authenticates submits one job on the token's own repository:

- `repos` has exactly one entry, the token's repository, which must be in DAIV's repository list (`404` otherwise).
- It can't set `thread_id`, `shared_session` or `references` (`400`: `A CI identity token submits one job on its own repository.`).
- `mcp_overrides` can name global MCP servers only; a personal server's name is an unknown server (`400`).
- It can target a branch with `ref` or a merge request with `merge_request`. When a DAIV session that covers other repositories owns the merge request, the run checks them out too, so the triggering account must be linked to a DAIV account that can run on each of them; otherwise DAIV doesn't run the job ([Run a job on a merge request](#run-a-job-on-a-merge-request)).

DAIV accepts one job per token: a second submission with a token whose job DAIV already accepted gets `401`. A CI job that sends two jobs requests two tokens, with two `id_tokens` entries on GitLab or two token requests on GitHub. A submission DAIV refused (`400`, `404` or a `failed` entry) does not use the token, so you can retry with it. A submission that fails with a server error does use it, since DAIV may have started its job.

### Who the run belongs to

The run belongs to the account that triggered the pipeline, never to a value in the request body: its DAIV account when one is linked through OAuth and active, else the platform account, which spend and the sessions list show as external.

The run acts as no one. It loads the global MCP servers only, never the triggering person's personal servers or personal [sandbox environments](sandbox-environments.md), DAIV does not assign merge requests it opens to them, and it can't [delegate](orchestration.md). Its notifications are muted unless the request sets `"muted": false`, which keeps them for a linked account.

### Poll with a token

`GET /api/jobs/{job_id}` with a token returns a job when it ran on the token's repository and belongs to the token's triggering account; any other job ID gets `404`. Polling does not use the token up, so the token that submitted a job can poll it until the token expires.

## Request fields

These are the fields of the `POST /api/jobs` body.

| Field                  | Type             | Required | Description |
|------------------------|------------------|----------|-------------|
| `repos`                | array of objects | yes      | 1–20 repositories to run against. Each item is `{ "repo_id": "group/project", "ref": "branch", "prompt": "..." }`. `ref` is optional, must be a branch that exists on the remote, and defaults to the repository's default branch. `prompt` is optional, must not be blank, and replaces the batch `prompt` for that repository only. A new merge or pull request targets `ref`, and DAIV assigns it to the API key's owner when their DAIV account is OAuth-linked to the git platform. An item can name `merge_request`, a merge request number, in place of `ref`; see [Run a job on a merge request](#run-a-job-on-a-merge-request). |
| `prompt`               | string           | yes      | The prompt to send to the agent. It runs as an independent job against each repository in `repos`, except one that gives its own `prompt`, and it titles the batch. |
| `agent_model`          | string           | no       | The model for this batch. DAIV rejects an invalid model and thinking-level combination with `400`. |
| `agent_thinking_level` | string           | no       | The agent's reasoning effort: `minimal`, `low`, `medium`, `high` or `xhigh`. DAIV rejects an invalid combination with `400`. |
| `muted`                | boolean          | no       | Mute notifications for every job in this batch. Default `false`, or `true` for a request a [CI identity token](#ci-identity-tokens) authenticates. |
| `environment`          | string           | no       | The [sandbox environment](sandbox-environments.md), by name or ID, for every job in the batch. DAIV rejects an environment it cannot resolve with `400`. |
| `shared_session`       | boolean          | no       | Run all repositories in `repos` in one session instead of one job each. At most 5 entries. See [Multi-repository sessions](#multi-repository-sessions). |
| `thread_id`            | string (UUID)    | no       | Continue an existing thread. `repos` must hold exactly one entry (a [multi-repository session](#continuing-a-multi-repository-session) also accepts its full list), the thread's most recent run must be yours, and the session must not be an [archived session](sessions.md#archived-sessions); otherwise DAIV returns `400`. If the session is busy, the job joins the session's next run for its branch in `QUEUED` state, which starts once the current run ends; see [Job lifecycle](#job-lifecycle). |
| `references`           | array of objects | no       | Up to 20 external work-item references, such as Jira, Sentry or RT tickets and git platform issues, that DAIV links into the merge or pull request it creates. Each item is `{ "key": "PROJ-123", "url": "https://…", "provider": "jira", "relation": "relates" }`, and only `key` is required. The per-field rules are in [References](mcp-endpoint.md#references); DAIV rejects an invalid reference with `422` and stores nothing. |
| `mcp_overrides`        | object           | no       | MCP servers to switch on or off for every job in the batch, relative to the default set: `{"grafana": true}` adds an on-demand server, `{"sentry": false}` drops an active one. Names are those of the global servers and your own personal ones. DAIV rejects an unknown name with `400`, and a value other than `true` or `false` with `422`. On a continued thread (`thread_id`) the overrides apply on top of the thread's own, so a server you do not name keeps its setting. DAIV returns a continuation that would change them while another run on the thread is unfinished in `failed`, since that run would pick them up too. |

DAIV validates the body strictly and rejects unknown fields with `422`, `use_max` and `notify_on` included. Use `muted` to silence notifications, and `agent_model` and `agent_thinking_level` to choose the model.

## Job status values

| Status | Meaning |
|--------|---------|
| `QUEUED` | The job waits for the session's current run to finish. Only occurs for thread continuations (`thread_id` supplied) and for jobs on a merge request a busy session owns. |
| `READY` | The job is waiting for a worker. |
| `RUNNING` | The agent is executing. |
| `SUCCESSFUL` | The run completed, and `result` holds the agent's last response. |
| `WAITING_INPUT` | The agent stopped to ask you a question. This is a final state, not a step towards `SUCCESSFUL`. `question` holds the questions asked, and `result` their rendered text. |
| `FAILED` | The agent hit an error, and `error` holds a message. When a model provider stopped the run, for example because it ran out of credits, hit its spending limit or rejected DAIV's API key, the message names the provider and the cause. A job on a merge request that never started says why. Otherwise it is a generic failure text. |

## Error responses

For `GET /api/jobs/{job_id}`:

| Code | When |
|------|------|
| `401` | The API key is missing or invalid, or the CI identity token fails a check. |
| `404` | The job ID is not found or is not a valid ID, or, with a CI identity token, the job did not run on its repository for its triggering account. |

For `POST /api/jobs`:

| Code | When |
|------|------|
| `400` | The request is invalid: a bad `agent_model` or `agent_thinking_level`; no `agent_model` given and no system default model configured (a model set only in `.daiv.yml` does not count); an unresolvable `environment`; an unknown MCP server in `mcp_overrides`; an invalid `shared_session` list (more than 5 entries, a repository listed twice, or a per-repository `prompt`); an invalid `thread_id` continuation (an unknown or unowned thread, an archived session, more than one repository for a single-repository session, a `ref` on a multi-repository session, or anything but the main repository alone or the full list in order); a `merge_request` with `shared_session`, `thread_id` or another `repos` entry; or a CI identity token's request for more than one job on its own repository. |
| `401` | The API key is missing or invalid, or the CI identity token fails a check or already submitted an accepted job. |
| `404` | A repository in `repos` is not found, or you lack write access to it. The response does not say which one. |
| `422` | The body is malformed or has unknown fields, for example `use_max` or `notify_on`, a `references` entry is invalid, or a `merge_request` entry also sets `ref` or `prompt`. |
| `429` | You exceeded the [rate limit](#rate-limiting). |

## Multi-repository sessions

By default, several entries in `repos` fan out into independent jobs, one session per repository. To let the agent work across repositories in one conversation, for example a backend change and the client that consumes it, set `shared_session: true`:

```json
{
  "repos": [
    {"repo_id": "mygroup/backend"},
    {"repo_id": "mygroup/client", "ref": "develop"}
  ],
  "shared_session": true,
  "prompt": "Add a `locale` field to the profile endpoint and use it in the client."
}
```

- You can also start such a session from the dashboard chat, which takes the same list: see [Start a chat over several repositories](sessions.md#start-a-chat-over-several-repositories).
- `repos` takes up to 5 entries, the first being the main one, each repository at most once, and no per-repository `prompt`: the session runs the one `prompt`.
- The response holds one job and one `thread_id`. The job's `repo_id` is the first entry, the main repository.
- You need write access to every repository. If one is missing or unknown, the call fails with the usual opaque `404`.
- The main repository's `.daiv.yml`, model, sandbox environment, skills, subagents and [memory](memory.md) drive the session. DAIV checks it out at `/workspace/repo` and the others at `/workspace/repos/<repository path>`. The agent is told about them and reads their `AGENTS.md`.
- The agent reads the main repository's memory, but a multi-repository run adds no observations to it, since its transcript holds the other repositories' code.

### Merge requests

At the end of each turn, every repository that changed gets its own commit, branch and merge request. Unchanged repositories get nothing, and one repository failing to publish does not stop the others. Each merge request's description lists the session's other merge requests as platform references (`group/project!12`, `owner/repo#12`), which the platform links and titles only for readers who can see that repository. External references, such as a support ticket, go into every merge request, but only the main repository's closes them; the others relate to them. GitLab or GitHub issue references go only into the main repository's.

The job status adds two lists:

| Field | Meaning |
|-------|---------|
| `merge_requests` | `[{"repo_id": "...", "url": "..."}]`, one per opened or updated merge request, the merge requests of [submodules](../customization/repository-config.md#submodules-and-lfs) DAIV published included. |
| `publish_errors` | `[{"repo_id": "...", "error": "..."}]`, one per repository that could not be published, submodules included. |

`merge_request_url` stays: the main repository's merge request, else the first one opened.

### Continuing a multi-repository session

Pass the session's `thread_id`. `shared_session` is optional and ignored, because the session decides whether a continuation is shared: a single-repository session continues as one even with `shared_session: true`. Send the main repository alone, or the identical list in the same order, and no `ref`. Anything else returns `400`. Each repository continues on its stored working branch. If that branch is gone because its merge request was merged, the repository falls back to its default branch and opens a new merge request the next time it changes.

### Visibility and CI

A multi-repository session, with its runs, transcript, artifacts and merge request links, is visible only to its owner, admins, and users who can read every repository in it.

The session owns every merge request it opens, the main repository's and each other repository's, so their [CI watch](pipeline-watch.md) results and `@daiv` comments reach the session's [inbox](sessions.md#the-session-inbox). The session's next run handles a CI failure or a mention on any of them, on the main repository's branch with every repository checked out, and DAIV answers a mention on the merge request it was made on. Each merge request has its own CI fix attempts. In the dashboard, a multi-repository session continued in chat keeps all its repositories and its main repository cannot be switched, and the session page lists every repository and merge request.

## Job lifecycle

A job moves through these statuses:

1. `POST /api/jobs` creates the job as `READY`, or as `QUEUED` when you supply a `thread_id`, or name a merge request a session owns, and the session is busy.
2. A `QUEUED` job becomes `READY` when the session's current run finishes.
3. A worker picks up a `READY` job and it becomes `RUNNING`.
4. A `RUNNING` job ends as `SUCCESSFUL` when the agent completes, `WAITING_INPUT` when the agent asks a question, or `FAILED` when the agent errors.
5. A `WAITING_INPUT` job does not change. Your answer is a new job: `POST /api/jobs` with the same `thread_id` and your answer as `prompt`. It starts at `READY`, or at `QUEUED` when the session is busy.

```mermaid
stateDiagram-v2
    [*] --> READY: POST /api/jobs
    [*] --> QUEUED: POST /api/jobs (thread continuation or owned merge request, session busy)
    QUEUED --> READY: The session's current run finishes
    READY --> RUNNING: Worker picks up job
    RUNNING --> SUCCESSFUL: Agent completes
    RUNNING --> WAITING_INPUT: Agent asks a question
    RUNNING --> FAILED: Agent errors
    SUCCESSFUL --> [*]
    WAITING_INPUT --> [*]: Answer with a new job on the same thread_id
    FAILED --> [*]
```

A job on a merge request a busy session owns starts in `QUEUED` and runs as a turn of its own ([Run a job on a merge request](#run-a-job-on-a-merge-request)). Otherwise a job starts in `QUEUED` only when you supply a `thread_id` and the session is busy. The job then joins the session's next run for its branch (`ref`, or the session's branch when you leave it out), which starts at `READY` once the current run ends. Everything waiting for the session on that branch goes into the same run: another job, a chat message, a CI failure DAIV is fixing. So two jobs sent while the session is busy can return the same `job_id`; poll it once and read one `result` for both. A job on a different branch gets a next run of its own. Every other submission starts at `READY`.

`SUCCESSFUL`, `WAITING_INPUT` and `FAILED` are final for that job. `WAITING_INPUT` does not progress to `SUCCESSFUL` on its own; it waits for a new job on the same thread (see [Answer a question](#answer-a-question)).

## Examples

### Poll from a shell script

Replace `<api-key>` with your DAIV API key before you run the script.

```bash
#!/bin/bash
DAIV_URL="https://daiv.example.com"
API_KEY="<api-key>"

# Submit (single-repo batch)
JOB_ID=$(curl -s -X POST "$DAIV_URL/api/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"repos":[{"repo_id":"mygroup/myproject"}],"prompt":"List all TODO comments"}' \
  | jq -r '.jobs[0].job_id')

echo "Job submitted: $JOB_ID"

# Poll until done
while true; do
  RESPONSE=$(curl -s "$DAIV_URL/api/jobs/$JOB_ID" \
    -H "Authorization: Bearer $API_KEY")
  STATUS=$(echo "$RESPONSE" | jq -r '.status')

  case "$STATUS" in
    SUCCESSFUL) echo "$RESPONSE" | jq -r '.result'; break ;;
    FAILED)     echo "Job failed"; exit 1 ;;
    *)          sleep 10 ;;
  esac
done
```

### Chain two jobs in a scheduled GitLab pipeline

This job in a scheduled GitLab CI pipeline triages support tickets, then creates issues from the report:

```yaml
# .gitlab-ci.yml
triage-and-create-issues:
  rules:
    - if: $CI_PIPELINE_SOURCE == "schedule"
  script:
    # Step 1: Triage tickets
    - |
      JOB_ID=$(curl -s -X POST "$DAIV_URL/api/jobs" \
        -H "Authorization: Bearer $DAIV_API_KEY" \
        -H "Content-Type: application/json" \
        -d '{
          "repos": [{"repo_id": "mygroup/myproject"}],
          "prompt": "Triage the RT queue and identify code-related tickets. Return a summary with ticket IDs, descriptions, and priorities."
        }' | jq -r '.jobs[0].job_id')

    # Step 2: Poll until done
    - |
      while true; do
        STATUS=$(curl -s "$DAIV_URL/api/jobs/$JOB_ID" \
          -H "Authorization: Bearer $DAIV_API_KEY" | jq -r '.status')
        [ "$STATUS" = "SUCCESSFUL" ] && break
        [ "$STATUS" = "FAILED" ] && exit 1
        sleep 30
      done
      TRIAGE=$(curl -s "$DAIV_URL/api/jobs/$JOB_ID" \
        -H "Authorization: Bearer $DAIV_API_KEY" | jq -r '.result')

    # Step 3: Chain — create issues from the triage report
    - |
      curl -s -X POST "$DAIV_URL/api/jobs" \
        -H "Authorization: Bearer $DAIV_API_KEY" \
        -H "Content-Type: application/json" \
        -d "$(jq -n --arg prompt "Based on this triage report, create GitLab issues for each actionable item: $TRIAGE" \
          '{repos: [{repo_id: "mygroup/myproject"}], prompt: $prompt}')"
```

Store `DAIV_URL` and `DAIV_API_KEY` as CI/CD variables in your GitLab project settings, and turn on **Mask variable** and **Protect variable** for the API key.

### Review every push to a merge request

These CI jobs ask DAIV to run `/code-review` on the pipeline's merge request at every push, with the pipeline's [CI identity token](#ci-identity-tokens), so you store no DAIV key. Each review is posted on the merge request and covers only the commits since the previous one; see [Review reports](pull-request-assistant.md#review-reports). Replace `https://daiv.example.com` with DAIV's address.

In GitLab CI:

```yaml
daiv-code-review:
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  id_tokens:
    DAIV_ID_TOKEN:
      aud: https://daiv.example.com
  script:
    - >
      curl --fail-with-body -sS -X POST "https://daiv.example.com/api/jobs"
      -H "Authorization: Bearer $DAIV_ID_TOKEN" -H "Content-Type: application/json"
      -d "{\"repos\":[{\"repo_id\":\"$CI_PROJECT_PATH\",\"merge_request\":$CI_MERGE_REQUEST_IID}],\"prompt\":\"/code-review\"}"
```

In GitHub Actions:

```yaml
# .github/workflows/daiv-code-review.yml
name: DAIV code review
on:
  pull_request:
    types: [opened, synchronize, reopened]
permissions:
  id-token: write
  contents: read
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - name: Ask DAIV to review the pull request
        env:
          DAIV_URL: https://daiv.example.com
          PR_NUMBER: ${{ github.event.pull_request.number }}
        run: |
          TOKEN=$(curl -sS -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
            "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=$DAIV_URL" | jq -r .value)
          curl --fail-with-body -sS -X POST "$DAIV_URL/api/jobs" \
            -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
            -d "{\"repos\":[{\"repo_id\":\"$GITHUB_REPOSITORY\",\"merge_request\":$PR_NUMBER}],\"prompt\":\"/code-review\"}"
```

The CI job ends as soon as DAIV accepts the review; the review itself appears on the merge request when its run finishes.

## Related pages

- [Request Tracker triage](../integrations/rt/index.md): an end-to-end example of using the jobs API from an RT Scrip to triage new support tickets.
- [MCP endpoint](mcp-endpoint.md): the same submission and polling through an MCP client.
- [Scheduled jobs](scheduled-jobs.md): recurring runs without an external trigger.
