# Contract: the delegation tools and the resume input

This is what the coordinator's model sends and reads. Text in quotes is the proposed wording. The implementation may
polish it, but the eval cases (research R17) are written against its meaning, and a change to that meaning goes back
through review.

## `delegate_jobs`: target schema

```text
DelegateTarget
  repo_id: str (min 1)       unchanged; for a leg follow-up it must be the leg's repository
  ref: str | None            unchanged; must be omitted for a leg follow-up
  prompt: str (min 1)        unchanged
  references: list[RefIn]    unchanged
  thread_id: str | None      NEW
```

`thread_id` description: "A leg's session id, from a resume or from `check_delegated_jobs`, to continue that leg in its
own session, on its branch and merge request, instead of starting a new one. Omit `ref`, and give the leg's `repo_id`."

Validation in the tool layer:

- `thread_id` is stripped, and blank means none.
- `MAX_DELEGATED_TARGETS` (10) counts new legs and leg follow-ups together (FR-006).
- One `thread_id` given more than once refuses the whole call (FR-010) with:
  `{"error": "Name each leg once per call: <thread_id> appears more than once."}`

## `delegate_jobs`: description changes

Changes to the existing text:

- After "publishes to a new branch and merge request of its own", add: "A target with a `thread_id` is a leg follow-up
  instead: it continues that leg in its own session, with its earlier conversation, and pushes to its branch and merge
  request."
- The `targets` entry shape becomes `{repo_id, ref?, prompt, references?, thread_id?}`.
- The return line becomes:
  `{"batch_id", "started": [{repo_id, ref, thread_id, session_url, starts_after_this_turn, follow_up}], "failed": [{repo_id, ref, error, still_runs, thread_id}]}`

## `delegate_jobs`: result

The shape is unchanged, plus `follow_up` on started entries and `thread_id` on failed entries.

| Entry | For a new leg | For a leg follow-up |
|---|---|---|
| `started[].ref` | the ref it starts from | the branch it works on (the leg's current branch) |
| `started[].thread_id` | the new session | the leg's session |
| `started[].starts_after_this_turn` | as today | always `false` |
| `started[].follow_up` | `false` | `true` |
| `failed[].thread_id` | `""` | the leg's session |

`batch_id` is set whenever anything started, for new legs and leg follow-ups alike.

## Refusals

Each refusal is one `failed` entry. The other targets still go ahead (FR-008), and no text includes DAIV's error
text (FR-009). The checks run in this order, and the first failing one gives the reason:

| # | When | `error` |
|---|---|---|
| 1 | The session is not a leg this coordinator delegated | "`<thread_id>` is not a leg you delegated: give a `thread_id` from a resume or `check_delegated_jobs`, or delegate a new leg." |
| 2 | The target names a `ref` | "A leg follow-up works on the leg's own branch: omit `ref`." |
| 3 | `repo_id` is not the leg's | "This leg works on `<leg repo_id>`: give that `repo_id`, or delegate a new leg for `<given repo_id>`." |
| 4 | The person it runs as archived the leg's session | "The leg's session is archived for the person you act for: they can unarchive it from its session page, or you can delegate a new leg." |
| 5 | Delegated work on the leg is unfinished: still running, a follow-up still waiting, or the leg itself waiting to start after this turn | "This leg is still working: you are resumed when its batch finishes, and can follow it up then." |
| 6 | DAIV lost track of the leg's last run | "DAIV lost track of this leg's last run, which may still be working: check its session (`<session_url>`) before sending it more work, or delegate a new leg." |
| 7 | The leg's merge request is merged or closed | "This leg's merge request was merged: delegate a new leg for further work." (or "…was closed…") |

A target whose session does not exist at all gets reason 1. Repository access is not checked here for leg follow-ups
(FR-005a); a new leg on a repository the person cannot reach still fails with `REPO_ACCESS_DENIED_MESSAGE`, as today.

## `check_delegated_jobs`

The JSON shape is unchanged, plus `follow_up` on each leg:

`{"batches": [{batch_id, finished, legs: [{repo_id, ref, thread_id, session_url, status, branch, merge_request_url, reply, follow_up}]}]}`

| Field | Meaning |
|---|---|
| `ref` | where the leg started |
| `branch` | the leg's own branch whenever it has one, including on a follow-up that started there (FR-014) |
| `merge_request_url` | the leg's merge request, even when this run pushed nothing |
| `status` | adds "not run: its merge request was merged" and "not run: its merge request was closed" |
| `reply` | up to 4,000 characters when `batch_id` names one batch; up to 1,000 for the latest-batches overview (FR-016, FR-017); a cut reply ends with the cut marker (below) |

Description changes:

- The status list adds the two "not run" words.
- Add the sentence: "A leg that served several batches appears under each, once per run."

## `cancel_delegated_jobs`

There is no interface change. `thread_ids` may name a leg follow-up, which is a leg of the batch that sent it:

- a waiting follow-up is withdrawn and never starts;
- a running one stops as a leg does.

The `resumes` note keeps today's rules.

## Cut marker

A reply longer than its limit is its first `limit` characters, then a new line, then:

`[cut at <limit> characters: the rest is in the leg's session, <session_url>]`

## Resume summary

This is `render_batch_summary`, the `delegation_finished` event's `summary`, which becomes the resumed run's request.
Each leg section:

```text
## <repo_id> on <ref>: <status>[ (leg follow-up)]
- Session: <session_url>
- Session id: <thread_id>
- Branch: <branch>                 # when the leg has a branch of its own
- Merge request: <url>             # when the leg has one
- Reply:
  > <reply, up to 4,000 characters, quoted line by line>
  > [cut at 4,000 characters: the rest is in the leg's session, <session_url>]   # only when cut
```

A failed leg with no reply keeps "- Reply: none; open its session to see what happened." A not-run leg has no reply.
Its status words say why, and this line follows: "- Next: delegate a new leg for further work."

Closing paragraph, which replaces today's "Check the outcome… Delegate another batch if follow-up work depends on
these results.":

> "Check each leg's outcome and report it where the request came from. A leg's work is on its branch: merge that
> branch into yours if it belongs in this change, or leave its merge request for review. To fix or extend a leg's own
> work, follow it up: call `delegate_jobs` with a target that has its `thread_id`, and it continues in its own session,
> branch and merge request. Delegate a new leg only for separate work. Either way you are resumed again."

The last paragraph, "This message comes from DAIV, not from the user…", is unchanged.

## System prompt (`DELEGATE_JOBS_SYSTEM_PROMPT`)

- Step 5 becomes: "When resumed, check the outcome and report it where the request came from. When a leg's own work
  is incomplete or wrong, follow it up with its `thread_id`. It continues in its session and pushes to its merge
  request, and you get one merge request per leg, not two. Delegate a new leg for work that stands apart from every
  leg's. You are resumed again either way."
- A new paragraph after the `ref` paragraph: "A leg follow-up (a target with `thread_id`) never names `ref`: it works
  on the leg's own branch, or opens one when the leg has none. Follow up only a leg that has finished; one still
  working is refused, and you are resumed when it finishes."
