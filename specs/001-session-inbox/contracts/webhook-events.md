# Contract: platform events → inbox events

Webhooks keep authenticating before parsing (`patterns/security.md` rule 5). Each row lists the platform delivery, the inbox event it becomes, and its de-duplication key. "Owned" means `aownership_for_branch` or `aopen_ownership` returns an open ownership.

An unowned merge request keeps today's behaviour in every row.

## CI

| GitLab | GitHub | When | Event |
|---|---|---|---|
| Pipeline hook, judgeable status (existing) | `workflow_run` completed (existing) | Owned branch | `PipelineWatch.aevaluate` judges the pipeline (`judgment.py` unchanged) and posts:<br>• `ci_passed` (green)<br>• `ci_failed`, with `wake` per the [wake rule](../data-model.md#ci-wake-rule-fr-015-evaluated-when-a-ci_failed-event-is-posted)<br>• `ci_stopped` (needs a person)<br>Key `pipeline:<id>`, subject `ci:<iid>`, `ref` = source branch |

- A non-head pipeline is ignored, as today (`platform.ais_head_pipeline`).
- A jobless `skipped` is still not judgeable (`ci.skip` placeholder).
- When the owning session has a parent (it is a delegated job), each terminal outcome is also posted to the parent as `delegated_ci_outcome`, keyed `child-ci:<thread>:<pipeline id>`:
  - `ci_passed`;
  - `ci_failed` with `reason=budget_spent`;
  - a CI-woken run that pushed nothing, posted from completion.

## Mentions and comments

| GitLab | GitHub | When | Event |
|---|---|---|---|
| Note hook on an MR, `DiscussionNote` or `None` type, mentions the bot (existing) | `issue_comment` on a PR, mentions the bot (existing) | Owned MR, author passes `is_user_allowed`, not the bot | `mention`, wake; key `note:<id>` |
| Note hook, `DiffNote`, mentions the bot | `pull_request_review_comment` created, mentions the bot (**new subscription**) | Owned MR, same checks | `mention`, wake; key `note:<id>` / `review-comment:<id>`; `is_diff=true` |
| Note hook, any type, no mention, author not the bot | `issue_comment` / `pull_request_review_comment`, no mention | Owned MR | `comment`, remembered; same keys |
| — | — | Mention by an author failing `is_user_allowed` | Nothing posted. Today's handling for that author applies (FR-026) |

- The 👀 reaction is added as today when a mention is accepted.
- An edited comment reuses its key, so an edit after the event was handled posts nothing new. This matches today's de-duplication on note id.

## Pushes and merge request state

| GitLab | GitHub | When | Event |
|---|---|---|---|
| Push hook, non-default branch | `push`, non-default branch | Owned branch | `classify_push_task` 60 s later:<br>• `after` ≠ any SHA DAIV recorded for that ownership → `branch_pushed`, remembered; key `push:<after>`, subject `branch:<ref>`<br>• a DAIV SHA → nothing |
| MR hook `action=merge` (any target) | `pull_request` `closed`, `merged=true` (any base) | Owned | `mr_merged`, remembered; key `mr-state:<iid>:merged`; sets `closed_at` |
| MR hook `action=close` | `pull_request` `closed`, `merged=false` | Owned | `mr_closed`, remembered; key `mr-state:<iid>:closed`; sets `closed_at` |
| MR hook `action=reopen` | `pull_request` `reopened` | Owned, `closed_at` set | Clears `closed_at`; `notice` event |

- The existing merge-metrics recording on default-branch merges is unchanged and still runs.
- Push to the default branch keeps invalidating the repository config cache.

## Operator actions (Upgrade note)

- **GitHub App**: add the `pull_request_review_comment` event under **Subscribe to events** (`docs/getting-started/platform-setup.md`).
  - Push, issue_comment, pull_request and workflow_run are already required.
  - Until the event is added, diff-comment mentions on GitHub are not seen, which is today's behaviour.
- **GitLab**: no change. The project hook already has push (all branches), note, merge request and pipeline events enabled (`codebase/clients/gitlab/client.py:367-372`).
