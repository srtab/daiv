# Quickstart: validating the Session Inbox

How to prove each user story works. The [contracts](contracts/) and the [data model](data-model.md) hold the details; this page only says what to run and what to expect.

## Prerequisites

- **Unit tests**: dependencies installed with `uv sync`. Run every Python tool with `uv run`.
- **End to end**:
  - the local stack (`make up`; in a worktree it gets its own stack, see `CONTRIBUTING.md` "Worktree stacks");
  - the local GitLab (`127.0.0.1:8929`) with credentials in `docker/local/app/config.secrets.env`;
  - a throwaway project whose CI can be made to fail on demand, for example a job that runs `exit 1` while a file `FAIL` exists.
- **GitHub parity**: a GitHub App installation subscribed to `pull_request_review_comment`, as the Upgrade note in [contracts/webhook-events.md](contracts/webhook-events.md) describes.

## Automated checks

```bash
uv run pytest tests/unit_tests/sessions/inbox tests/unit_tests/sessions/pipeline_watch tests/unit_tests/sessions/executor -q
```

```bash
uv run pytest tests/unit_tests/webhooks tests/unit_tests/chat tests/unit_tests/jobs tests/unit_tests/mcp_api tests/unit_tests/notifications -q
```

```bash
make test
```

```bash
make lint
```

```bash
make lint-typing
```

```bash
make eval-prompts SUITES="inbox" OUT=eval-runs/inbox-after.jsonl
```

Compare against a baseline taken on `main` before the change (Constitution V):

```bash
uv run evals/compare_runs.py eval-runs/inbox-before.jsonl eval-runs/inbox-after.jsonl
```

## Scenarios

### 1. CI failure handled in the publishing session (US1, SC-001, SC-002)

1. Create an issue in the throwaway project, add the `daiv` label, and ask for a one-line change.
2. Before DAIV publishes, create `FAIL` on the default branch, so the merge request's pipeline fails for a reason no code change in the merge request fixes.

**Expected**:
- One run with the "DAIV · CI failed" notice appears in the **issue session**. No merge request thread session is created.
- Its summary explains that the failure isn't caused by the change, and it pushes nothing.
- The header shows CI "failing" with that explanation, not "attempts exhausted".
- The owner receives a notification with the summary.

3. Delete `FAIL` and retry the pipeline.

**Expected**: the header shows "passing" and no run starts. The timeline shows "Pipeline … passed".

4. Retry the failed pipeline again without changing anything.

**Expected**: a `ci_failed` timeline entry with reason `already_examined`, and no run.

### 2. Events while busy (US2, SC-003, SC-004, SC-009)

1. In a chat session that owns a merge request, ask for something long-running.
2. While it runs, fail the merge request's CI, mention DAIV on the merge request, and send a second chat message.

**Expected**:
- The chat input accepts the message and shows it as queued (no 409).
- Exactly one next run starts within 1 minute of the first run ending.
- Its transcript shows your chat message; the mention reply is posted on the merge request.
- `InboxEvent` rows for the three events point to that run, with state `handled`.

3. While the first run is still going, look at the session's runs in the Django admin.

**Expected**: the next run is `QUEUED` with no task result, so no worker has picked it up. It becomes `READY` only when the first run ends.

### 3. Remembered events (US3)

1. Push a commit by hand to a DAIV merge request's branch, then merge the merge request.

**Expected**:
- About 60 s after the push, a `branch_pushed` timeline entry appears, then an `mr_merged` entry.
- No run starts for either.
- The next chat message's run says the branch moved, and works from the new head (`git log` in the run shows your commit).

### 4. One delivery path (US4)

1. From a chat session, delegate two jobs. While the session is busy, submit `POST /api/jobs` with its `thread_id`.

**Expected**:
- The job response's `job_id` equals the session's queued next run.
- When the delegated batch finishes, one run handles both the batch summary and the job prompt.
- Each delegated merge request's CI outcome appears in the delegating session's timeline as a remembered event.

2. In the Django admin, open **Inbox events** filtered by that session.

**Expected**: source, arrival time and state for each event (FR-034).

### 5. Mentions on an owned merge request (US5)

1. As a reviewer allowed by the repository's `.daiv.yml`, mention DAIV in a diff comment on a DAIV merge request (GitLab DiffNote; GitHub review comment).

**Expected**:
- The run appears in the owning session, attributed to the reviewer, acting as the owner.
- The reply is posted in the diff discussion.

2. Mention DAIV as a user **not** in the allowlist.

**Expected**: no run starts, and the mention is handled as it is today.

### Replay of the 2026-10-30 incident (SC-002)

Repeat scenario 1 with a CI step that fails on a GitHub authentication error (any job that exits non-zero without a code cause).

**Expected**: the session ends showing CI passing after the manual retry, and no "attempts exhausted" state appears anywhere.
