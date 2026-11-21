# Contract: Merge request webhook events → automation events

Both platforms' merge request callbacks keep handling close, merge and reopen as today. They also accept the actions below, and pass a normalised event to `sessions.automations.Automations(repo_id).aon_event(event)`. Webhook authentication is unchanged and happens before parsing ([security](../../../constitutions/patterns/security.md) rule 5).

## Normalised event

| Field | Meaning |
|---|---|
| `kind` | `opened`, `updated` or `ready` |
| `merge_request_iid` | GitLab `object_attributes.iid`, GitHub `pull_request.number` |
| `head_sha` | GitLab `object_attributes.last_commit.id`, GitHub `pull_request.head.sha` |
| `target_branch` | GitLab `object_attributes.target_branch`, GitHub `pull_request.base.ref` |
| `draft` | GitLab `object_attributes.draft`, GitHub `pull_request.draft` |
| `actor` | `{provider, uid, username}`: GitLab `user.id`/`user.username`, GitHub `sender.id`/`sender.login` |

## GitLab (`object_kind: merge_request`)

| Payload | Event |
|---|---|
| `object_attributes.action == "open"` | `opened` |
| `object_attributes.action == "update"` and `object_attributes.oldrev` is present | `updated` |
| `object_attributes.action == "update"` and `changes.draft.previous == true` and `changes.draft.current == false` | `ready` |
| any other `update` (title, labels, assignees) | ignored |

An `update` with both `oldrev` and a draft change yields `updated`, then `ready`. Both upsert the same pending firing, so the automation fires once on the head.

`webhooks/gitlab/models.py` gains `MergeRequestEvent.oldrev: str | None`, `last_commit: {id: str} | None`, `draft: bool = False`, and a top-level `changes: MergeRequestChanges | None` (`draft: {previous, current}`).

## GitHub (`pull_request`)

| `action` | Event |
|---|---|
| `opened` | `opened` |
| `synchronize` | `updated` |
| `ready_for_review` | `ready` |
| `reopened`, `edited`, `labeled`, others | unchanged (reopen keeps its handling) or ignored |

## What `aon_event` does

1. When `site_settings.automations_enabled` is false, nothing happens.
2. It loads `RepositoryConfig.automations`. When there are none, nothing happens.
3. It keeps the automations whose `on` includes the event and whose cheap filters pass: `drafts`, `target_branches`, `daiv_merge_requests` (the merge request has an open `MergeRequestOwnership`), and `allowed_usernames`. The `paths` filter is checked at fire time.
4. For each one kept, it upserts the pending `AutomationFiring` (newest head, actor, event, `due_at = now + 60 s`), marks any older pending firing `superseded`, and enqueues `fire_automation_task(firing_id)` with `run_after = due_at`.

Every step is idempotent for a re-delivered payload, because of the unique (`repo_id`, `merge_request_iid`, `automation`, `head_sha`) constraint.
