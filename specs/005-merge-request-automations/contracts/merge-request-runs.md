# Contract: `sessions.merge_request_runs`, starting a run on a merge request

One service that every trigger calls to run something on a merge request: `webhooks` for mentions, `jobs` and `mcp_api` for merge-request-targeted jobs, and `sessions.automations` for automation firings. It holds the direct path that moves here from `webhooks` (research R1).

## Inputs

| Type | Fields | From |
|---|---|---|
| `Requester` | `user: User \| None`, `provider`, `uid`, `username`, `acts_as_user: bool` | the mention's author, the job's caller or CI actor, the firing's actor |
| `Mention` | `note_id`, `comment_id`, `body`, `web_url`, `on_diff` | `webhooks` (unchanged shape, moved) |
| `Request` | `prompt`, `source: SessionOrigin` (`API_JOB`, `MCP_JOB`, `AUTOMATION`), `request_key` (the job's run id, or the firing id) | `jobs`, `mcp_api`, `automations` |

## `MergeRequestRuns(client, repo_id, merge_request_iid)`

`astart(requester, input, *, title, use_max=False, thread_id=None, muted=None, ask_user=True) -> StartOutcome`

**Routing**:
1. **Owning session** (only when `thread_id` is not given; automations always give their own): when `Ownerships(repo_id).afind_open(iid)` returns one:
   - an archived owner gets the archived reply (`_reply_archived`) and `StartOutcome.archived`;
   - a `Mention` posts `MENTION` (today's `_post_comment`);
   - a `Request` posts `MERGE_REQUEST_JOB` with `acts_as_user` and `source` (research R3), and returns the joined next run.
2. **Otherwise, the direct path:**
   - `thread_id` defaults to `compute_thread_id(repo_id, MERGE_REQUEST, iid)`;
   - an archived session gets the archived reply;
   - the sandbox environment resolves with `user=None`;
   - in one transaction: `address_merge_request_task.get_or_enqueue(repo_id, merge_request_id, request_key, …)` and `create_run(trigger_type=…, merge_request_iid=iid, user=requester.user, external_*=…, acts_as_user=…, muted=…)`.
   - The dedup key is (`repo_id`, `merge_request_id`, `request_key`), where `request_key` is `note:{note_id}` for a mention, `job:{run_id}` for a job, or `firing:{id}` for an automation.

**Outcome**: `StartOutcome(run_id, status: READY | QUEUED, archived: bool)`.

## `address_merge_request_task` (moved, renamed)

- **Context:** the same as today's `address_mr_comments_task`. The merged-merge-request guard and the missing-branch comment are unchanged.
- **Agent input:** the `Mention`'s note text, fetched by id as today, or the `Request.prompt` as `HumanMessage(name=requester.username)`.
- **Spec:** `Scope.MERGE_REQUEST` with the `MergeRequest` in `RuntimeCtx`, `ask_user_enabled=ask_user`, and the acting user taken from `Run.acting_user_id`.
- **Success:** a response becomes a top-level merge request comment. A question becomes a reply in the mention's thread, or a top-level comment when there is no mention.
- **Failure:** the "unable" note. An automation run's note names the automation: "Automation `review` could not complete on this commit."

`webhooks.tasks.address_mr_comments_task` stays importable for one release, as an alias with the old keyword arguments that forwards to the new task.

## Guarantees

- **Mentions:** a mention behaves exactly as before on both paths. The existing `webhooks` tests move with the code and keep passing.
- **Serialisation:** runs in one session never overlap; the session slot serialises them.
- **Archived sessions:** none gets a new run. The trigger gets the archived reply, except an automation firing, which records `closed`.
