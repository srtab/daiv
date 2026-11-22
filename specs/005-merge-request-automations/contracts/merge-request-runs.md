# Contract: `sessions.merge_request_runs`, starting a run on a merge request

One service that the trigger apps call to run something on a merge request: `webhooks` for mentions, and `jobs` and `mcp_api` for merge-request-targeted jobs. It holds the direct path that moves here from `webhooks` (research R1).

## Inputs

| Type | Fields | From |
|---|---|---|
| `Caller` | `user: User \| None`, `provider`, `uid`, `username`, `acts_as_user: bool` | the mention's author, the job's submitter or CI actor |
| `Mention` | `note_id`, `comment_id`, `body`, `web_url`, `on_diff` | `webhooks` (unchanged shape, moved) |
| `Request` | `prompt`, `trigger_type: SessionOrigin` (`API_JOB`, `MCP_JOB`), `request_key` (a fresh `uuid4` hex per job) | `jobs`, `mcp_api` |

## `MergeRequestRuns(client, repo_id, merge_request_iid)`

`astart(caller, input, *, title, use_max=False, muted=None) -> StartOutcome`

**Routing**:
1. **Owning session:** when `Ownerships(repo_id).afind_open(iid)` returns one:
   - an archived owner returns `StartOutcome.archived`; a `Mention` also gets the archived reply (`_reply_archived`), and a `Request` gets nothing posted (research R9);
   - a `Mention` posts `MENTION` (today's `_post_comment`);
   - a `Request` posts `MERGE_REQUEST_JOB` with `acts_as_user` and `trigger_type` through `MergeRequestJobs.post`, which creates the event, with no subject, and its own `QUEUED` run under the session lock. The run has `runs_alone=True` and the caller's person fields, so it is never another event's next run. `post` refuses when the session is archived or the merge request isn't open (research R3). It returns that run.
2. **Otherwise, the direct path:**
   - the session is `compute_thread_id(repo_id, MERGE_REQUEST, iid)`;
   - an archived session returns `StartOutcome.archived`; a `Mention` also gets the archived reply, and a `Request` gets nothing posted;
   - the sandbox environment resolves with `user=None`;
   - in one transaction, through `RunStarter`: `address_merge_request_task.get_or_enqueue(repo_id, merge_request_id, request_key, …)` and `create_run(trigger_type=…, merge_request_iid=iid, user=caller.user, external_*=…, acts_as_user=…, muted=…)`.
   - The dedup key is (`repo_id`, `merge_request_id`, `request_key`), where `request_key` is `note:{note_id}` for a mention or `job:{uuid}` for a job.

**Outcome**: `StartOutcome(run_id: UUID | None, status: READY | QUEUED | None, archived: bool, thread_id: str)`. An archived outcome has no run, and `thread_id` gives the jobs layer the session link for its `failed` message.

## `address_merge_request_task` (moved, renamed)

- **Context:** the same as today's `address_mr_comments_task`. For a `Mention`, the merged-merge-request guard and the missing-branch comment are unchanged.
- **Guards for a `Request`**, checked before the run starts (research R9):
  - a merged or closed merge request posts "This merge request was merged before the job could start, so I didn't run it." ("…closed…") and finishes the run `FAILED`, never started, with that text as its error;
  - an archived session finishes the run `FAILED`, never started, and posts nothing.
- **Agent input:** the `Mention`'s note text, fetched by id as today, or the `Request.prompt` as `HumanMessage(name=caller.username)`.
- **Spec:** `Scope.MERGE_REQUEST` with the `MergeRequest` in `RuntimeCtx`, and the acting user taken from `Run.acting_user_id`.
- **Success:** a response becomes a top-level merge request comment. A question becomes a reply in the mention's thread, or a top-level comment when there is no mention.
- **Failure:** the "unable" note, as today.

`webhooks.tasks.address_mr_comments_task` stays importable for one release, as an alias with the old keyword arguments that forwards to the new task.

## Guarantees

- **Mentions:** a mention behaves exactly as before on both paths. The existing `webhooks` tests move with the code and keep passing.
- **Serialisation:** runs in one session never overlap; the session slot serialises them.
- **Archived sessions:** none gets a new run. A mention gets the archived reply, and a job gets a `failed` entry.
- **Jobs that can't start:** a job accepted before its merge request merged or closed, or before its session was archived, ends `FAILED` without starting, on both paths. On the owned path the drain stops its `MERGE_REQUEST_JOB` event at take time (research R9).
