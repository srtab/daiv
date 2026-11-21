# Quickstart: validating merge request automations

End-to-end checks that prove the feature works, run against this checkout's local stack. The rules each step checks are in [contracts/](contracts/), and the tables in [data-model.md](data-model.md).

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

## 2. A job on a merge request DAIV owns (P1)

1. Let a chat or job session publish a merge request `!2`.
2. `POST /api/jobs` naming `!2`, sent while that session is running.
   **Expected:** status `QUEUED`. When the owning session's current turn ends, the job runs as a turn of that session, acting as the caller. Its result is a new top-level comment on `!2`.

## 3. CI identity token (P2)

1. In the test project add the GitLab job from [contracts/jobs-api.md](contracts/jobs-api.md#authentication-ci-identity-token), with `aud` set to `DAIV_EXTERNAL_URL`. Push to `!1`.
   **Expected:**
   - The pipeline job gets `202` and a review comment appears.
   - In **Insights → Spend**, the run is under the pusher, or under their GitLab account marked external.
   - The run's session shows no personal MCP servers, and its sandbox environment is not one of the pusher's personal ones.
2. Change `aud` to another value.
   **Expected:** `401`.
3. Use the token of a pipeline in another project to target the test project.
   **Expected:** `401`.
4. Poll `GET /api/jobs/{id}` with the token from step 1.
   **Expected:** `200`. With a token from another project's pipeline: `404`.
5. Add `allowed_usernames: [someone-else]` to `.daiv.yml` on the default branch and push.
   **Expected:** `401`.

## 4. Repository automations (P3)

1. Merge to the default branch:

    ```yaml
    automations:
      - name: review
        on: [merge_request.opened, merge_request.updated, merge_request.ready]
        prompt: /code-review
    ```

2. Open a merge request.
   **Expected:** about 60 seconds later, one review comment. The session list shows a session "review on !N" with origin **Automation**, and no notification is sent.
3. Push five commits within 30 seconds.
   **Expected:** one run, on the fifth commit. The `AutomationFiring` rows show four `superseded` and one `fired`.
4. Re-deliver the last merge request webhook from GitLab's webhook page.
   **Expected:** no new run.
5. Open a draft merge request.
   **Expected:** nothing. Mark it ready: one review.
6. Add a second entry with `on: [merge_request.sometimes]`.
   **Expected:** an ERROR log names the entry, and the `review` automation still runs.
7. Turn off **Automations** in site configuration and push.
   **Expected:** the firing is `disabled` and no run starts.

## 5. DAIV's own merge requests and loops (P4)

1. On a DAIV-published merge request, mention DAIV to make a change.
   **Expected:** DAIV pushes, and `review` does not run, because `daiv_merge_requests` defaults to false.
2. Set `daiv_merge_requests: true` on the default branch, then make DAIV push again.
   **Expected:** a review runs in the "review on !N" session, not in the owning session, and the owning session starts no turn.
3. Add an automation whose prompt always commits, for example "append a line to CHANGELOG.md and push", with `daiv_merge_requests: true`. Push once as a person.
   **Expected:** exactly one run of it. Its own commit's firing is `automation_commit`.
4. Make DAIV push four times in a row through mentions.
   **Expected:** three automation runs, then one `capped` firing with a single cap comment. Push as a person: the automations run again.

## 6. Quality gates

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
