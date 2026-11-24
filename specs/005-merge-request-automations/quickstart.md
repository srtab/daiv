# Quickstart: validating merge request targets and CI identity

End-to-end checks that prove user stories 1 and 2 work, run against this checkout's local stack. The rules each step checks are in [contracts/](contracts/), and the fields in [data-model.md](data-model.md).

## Prerequisites

- **Stack:** `make up`, with the local GitLab and a DAIV user who signed in with it. A worktree gets its own stack (CONTRIBUTING.md, "Worktree stacks").
- **Test project:** a GitLab project in DAIV's catalog with a GitLab runner registered, and an open merge request `!1` with at least one commit.
- **API key:** a DAIV API key for the signed-in user, in `$DAIV_API_KEY`.
- **Unit suite:** `make test` passes, including the new modules' test files.

## 1. A job on a merge request no session owns (P1)

1. `POST /api/jobs` with `{"repos":[{"repo_id":"<project>","merge_request":1}],"prompt":"/code-review"}`.
   **Expected:** `202`, job `READY`. One "Code Review #1" comment appears on `!1` from DAIV's account. `GET /api/jobs/{id}` is `SUCCESSFUL`, and its `result` equals the comment.
2. Push a commit to `!1`'s branch and repeat step 1.
   **Expected:** "Code Review #2", covering only the new commit.
3. Repeat without pushing.
   **Expected:** a one-line "already reviewed" comment.
4. Send two jobs within a second.
   **Expected:** both `202`, no `IntegrityError` in the logs, and their two runs execute one after the other in the merge request's session.
5. Name a closed merge request.
   **Expected:** the entry appears in `failed` with "…is closed.", and no run.
6. Send two jobs on an open merge request `!3`, and close `!3` while the first one runs.
   **Expected:** the first completes and posts its result. The second never starts: `!3` gets "This merge request was closed before the job could start, so I didn't run it.", and its job is `FAILED` with that text.
7. Archive `!1`'s session in the dashboard, then send a job naming `!1`.
   **Expected:** the entry appears in `failed` with the session link. Nothing is posted on `!1`. Unarchive the session before step 3.

## 2. A job on a merge request DAIV owns (P1)

1. Let a chat or job session publish a merge request `!2`.
2. `POST /api/jobs` naming `!2`, sent while that session is running.
   **Expected:** status `QUEUED`. When the owning session's current turn ends, the job runs as a turn of that session, acting as the caller. Its result is a new top-level comment on `!2`.
3. Send two jobs naming `!2` while the session is running.
   **Expected:** two different `job_id`s, each `QUEUED`, which run as two turns, one after the other.

## 3. CI identity token (P2)

1. In the test project add the GitLab job from [contracts/jobs-api.md](contracts/jobs-api.md#authentication-ci-identity-token), with `aud` set to `DAIV_EXTERNAL_URL`. Push to `!1`.
   **Expected:**
   - The pipeline job gets `202` and a review comment appears.
   - In **Insights → Spend**, the run is under the pusher, or under their GitLab account marked external.
   - The run's session shows no personal MCP servers, and its sandbox environment is not one of the pusher's personal ones.
2. Change `aud` to another value.
   **Expected:** `401`, and the web log has a WARNING naming the `audience` check and the project path, without the token.
3. Use the token of a pipeline in another project to target the test project.
   **Expected:** `401`.
4. Poll `GET /api/jobs/{id}` with the token from step 1.
   **Expected:** `200`. With a token from another project's pipeline: `404`.
5. Add `allowed_usernames: [someone-else]` to `.daiv.yml` on the default branch and push.
   **Expected:** `401`.
6. In the step 1 job, send the same `curl` twice with the same `$DAIV_ID_TOKEN`.
   **Expected:** the first gets `202`, the second `401`, and a WARNING names the `reused` check. Polling with the token still gets `200`.

## 4. Quality gates

```bash
make lint
```

```bash
make lint-typing
```

```bash
make test
```

```bash
uv run --only-group=docs mkdocs build --strict
```

```bash
make eval-prompts SUITES="inbox code_review" OUT=eval-runs/005-after.jsonl
```

```bash
uv run evals/compare_runs.py eval-runs/005-before.jsonl eval-runs/005-after.jsonl
```

**Expected:** every gate passes, and `compare_runs.py` reports no `regressed` verdict.
