# Contract: Jobs API, merge request target and CI identity

Changes to `POST /api/jobs` and `GET /api/jobs/{job_id}`. Everything not listed here keeps its current behaviour ([Jobs API](../../../docs/features/jobs-api.md)). The MCP job tool (`mcp_api/server.py`) accepts the same `merge_request` field with the same rules. It does not accept CI identity tokens.

## Request: `repos[].merge_request`

```json
{
  "repos": [{"repo_id": "group/project", "merge_request": 42}],
  "prompt": "/code-review"
}
```

| Field | Type | Rules |
|---|---|---|
| `repos[].merge_request` | integer ≥ 1, optional | The merge request (GitLab iid, GitHub number) to run on, in place of `ref` |

**Validation**:

| Condition | Response |
|---|---|
| `merge_request` together with `ref`, or with a per-repository `prompt` | `422` |
| `merge_request` with `shared_session: true` or with `thread_id` | `400`: "A merge request target can't be combined with shared_session or thread_id." |
| More than one `repos` entry when any entry has `merge_request` | `400` |
| The merge request does not exist, or belongs to another repository | entry in `failed`: "Merge request !42 was not found in group/project." |
| The merge request is closed or merged | entry in `failed`: "Merge request !42 is closed." / "…is merged." |
| The session the job would run in is archived: the owning session, or the merge request's own session when none owns it | entry in `failed`: "The DAIV session of merge request !42 is archived. Unarchive it to run jobs on it: {session URL}". Nothing is posted on the merge request |
| The caller lacks write access to the repository | `404`, as today, without confirming the merge request |

**Where the run goes** (FR-006):
- **No DAIV session owns the merge request:** the run goes into the merge request's session. Its status is `READY`, and it waits for the session's slot.
- **A DAIV session owns it:** the job becomes a `MERGE_REQUEST_JOB` inbox event in the owning session, posted with a run of its own. The status is `QUEUED` when the session is busy, else `READY`. Each job has its own `job_id`, and it runs as one turn of the owning session.

**Result**:
- **Comment:** the final message is posted as one comment on the merge request, from DAIV's account.
- **Status:** `GET /api/jobs/{id}` returns the same text in `result`, and `merge_request_url` is the merge request.
- **Failure:** a short failure comment on the merge request, and `status: FAILED`.
- **A question:** the question is the run's one comment on the merge request, and `status: WAITING_INPUT`. A person answers by mentioning DAIV on the merge request, or the caller sends a new job with the same target.
- **Not started, merge request merged or closed:** the run never starts when the merge request is merged or closed after the job was accepted. DAIV posts "This merge request was merged before the job could start, so I didn't run it." ("…closed…" for a closed one) on the merge request. The job is `FAILED` with that text as its error.
- **Not started, session archived:** the run never starts when the session is archived after the job was accepted. The job is `FAILED`, and nothing is posted on the merge request.

## Authentication: CI identity token

`Authorization: Bearer <token>`, where `<token>` is one of:
- a DAIV API key (unchanged);
- a CI identity token from the configured git platform.

**GitLab CI**:

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

**GitHub Actions** (`permissions: id-token: write`):

```bash
TOKEN=$(curl -sS -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
  "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=https://daiv.example.com" | jq -r .value)
```

**Verification**, all required:

| Check | GitLab | GitHub |
|---|---|---|
| Signature | `RS256` by a key in `{CODEBASE_GITLAB_URL}/oauth/discovery/keys` (`https://gitlab.com` when unset) | `RS256` by a key in `{iss}/.well-known/jwks` |
| `iss` | `CODEBASE_GITLAB_ID_TOKEN_ISSUER`, else `CODEBASE_GITLAB_URL`, else `https://gitlab.com` | `CODEBASE_GITHUB_ACTIONS_OIDC_ISSUER`, else `https://token.actions.SUBDOMAIN.ghe.com` when the host of `CODEBASE_GITHUB_URL` is `SUBDOMAIN.ghe.com` or `api.SUBDOMAIN.ghe.com`, else `https://HOST/_services/token` when the host of `CODEBASE_GITHUB_URL` is neither `github.com` nor `api.github.com`, else `https://token.actions.githubusercontent.com` |
| `aud` | `DAIV_JOBS_CI_IDENTITY_AUDIENCE` (default `DAIV_EXTERNAL_URL`) | same |
| `exp`, `iat` | valid, 60 s leeway | same |
| `jti` | present | same |
| Repository | `project_id` = the platform id of `repos[0].repo_id` | `repository_id` = the platform id of `repos[0].repo_id` |
| Allowlist | `user_login` in `allowed_usernames` when set | `actor` in `allowed_usernames` when set |
| First attempt | not checked | `run_attempt` = `1`; a re-run's `actor` is the first attempt's |
| Single use, on `POST` only | no accepted job for this `jti` yet | same |

**Single use**: `POST /api/jobs` claims the token's `jti` after the checks above and before it starts anything, until the token expires, so a submission they refuse never holds it. When admission refuses the submission (`400` or `404`) or it accepts no job (a `failed` entry), the claim is released, so a retry with the same token works. A submission that fails with an unexpected server error keeps the claim, since its job may have started. `GET /api/jobs/{job_id}` doesn't claim.

**Responses**:

| Condition | Response |
|---|---|
| Any verification check fails, the token was already used for an accepted job, or the signing keys, the repository or the claim store are unreachable | `401` `{"detail": "Unauthorized"}`, with no detail on which check failed |
| The repository is not in DAIV's catalog | `404`, as for API keys |
| `repos` has more than one entry, or the body has `thread_id`, `shared_session` or `references` | `400`: "A CI identity token submits one job on its own repository." |
| `mcp_overrides` names a personal server | `400`, as an unknown server name |

**Logs**: every refusal logs once at WARNING with the failed check (`malformed`, `algorithm`, `signature`, `issuer`, `audience`, `expired`, `missing claim`, `re-run`, `repository`, `allowed usernames` or `reused`) and the repository path the token claims, escaped and cut to 200 characters. The token itself is never logged. Signing keys or a repository the platform fails to serve (`signing keys unreachable`, `repository unreachable`), and an unreachable claim store, log once at ERROR instead.

**Attribution**:
- **Person:** the run's `user` is the actor's linked, active DAIV account. Otherwise it is null, with `external_provider`, `external_uid` and `external_username` set to the actor.
- **No personal tools:** `acts_as_user` is false.
- **Notifications:** `muted` defaults to `true`, and `"muted": false` keeps notifications for a linked actor.
- **Rate limit:** counted per the actor's linked user (its pk), else per `ci:{provider}:{actor_uid}`.

## Polling with a CI identity token

`GET /api/jobs/{job_id}` with a valid token returns the run only when:
- the run's `repo_id` is the token's repository;
- the run's person is the token's actor: either `user` is the actor's linked account, or `external_provider` and `external_uid` match.

Otherwise it answers `404`.
