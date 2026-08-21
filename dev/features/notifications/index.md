# Notifications

DAIV tells you when your work needs attention. When a run finishes with something to look at — an issue found, a situation that needs review, or an outright failure — DAIV writes an in-app notification and delivers it to external channels like email and Rocket Chat. Clean runs stay silent and appear only in the Feed.

Notifications are per-user: each recipient gets their own copy, with delivery resolved against that user's own channel bindings.

## What produces a notification

DAIV classifies every finished run and notifies only when the outcome warrants it. The three notify-worthy classifications are **found-issues**, **needs-attention**, and **failed**. Runs classified **all-clear** are silent — they live in the Feed but generate no notification.

There are three event types:

| Event                                         | When it fires                                                                                                                                   | Recipients                                                                                                                         |
| --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| **Job finished** (`job.finished`)             | A single agent run finishes with a notify-worthy classification                                                                                 | The user who started the run                                                                                                       |
| **Job batch finished** (`job_batch.finished`) | Every run in a multi-run batch is terminal — a single rollup, not one message per run                                                           | The batch owner (and, for [scheduled](https://srtab.github.io/daiv/dev/features/scheduled-jobs/index.md) batches, any subscribers) |
| **Schedule finished** (`schedule.finished`)   | A run tied to a [scheduled job](https://srtab.github.io/daiv/dev/features/scheduled-jobs/index.md) finishes with a notify-worthy classification | The schedule owner and its subscribers                                                                                             |

Batches collapse into one message

A batch is a group of runs sharing a batch ID — for example a [scheduled job](https://srtab.github.io/daiv/dev/features/scheduled-jobs/index.md) that fans out across several repositories. DAIV suppresses the per-run notifications for a multi-run batch and sends a single **Job batch finished** rollup once the last sibling is terminal, summarising how many runs were notify-worthy and how many were all-clear.

Webhook-triggered runs notify on worthy outcomes

Runs triggered by a GitLab/GitHub issue or merge/pull-request webhook (for example [issue addressing](https://srtab.github.io/daiv/dev/features/issue-addressing/index.md) or the [pull request assistant](https://srtab.github.io/daiv/dev/features/pull-request-assistant/index.md)) still report back inside the issue or MR/PR thread, and — like prompt-driven job runs (via the dashboard, [Jobs API](https://srtab.github.io/daiv/dev/features/jobs-api/index.md), or [MCP endpoint](https://srtab.github.io/daiv/dev/features/mcp-endpoint/index.md)) — now also fire a notification to their initiator when the outcome is notify-worthy. All-clear runs stay silent on both paths.

## Channels

DAIV reaches you through channels. The in-app bell is always available; email and Rocket Chat are external delivery channels.

- **In-app bell**

  The notification bell in the dashboard header shows your unread count and a dropdown of recent items. The full history lives at `/dashboard/notifications/`.

- **Email**

  Delivered to your account email. DAIV keeps an email channel binding in sync with your account address automatically — there is nothing to connect.

- **Rocket Chat**

  A direct message from the DAIV bot, when your administrator has enabled Rocket Chat and you have bound your `@username`.

### The in-app bell and list

The bell entry is written for **notify-worthy** runs — those classified as found-issues, needs-attention, or failed. All-clear runs are silent and do not produce a bell entry.

- The bell dropdown shows your ten most recent notifications and marks them read when you open it.
- `/dashboard/notifications/` lists your full history with `All` / `Unread` / `Read` filters and a **Mark all as read** action.
- The unread badge — and the **N running** badge next to *Sessions* in the sidebar — update live over a server-sent-events stream (`GET /api/nav/events`), so a new notification or a run starting or finishing shows up without a page reload. The stream requires Redis (already required for chat and caching); without it the badges simply show their page-load values.

### Email

Email needs no setup. When your account is created (or your email changes), DAIV maintains a verified email channel binding pointing at your account address. Email is delivered for any notify-worthy, un-muted run.

### Rocket Chat

Rocket Chat is an optional integration. It appears as a channel only when an administrator has enabled it for the instance, after which you bind your own Rocket Chat handle so DAIV can DM you.

## Muting

Notifications fire automatically on notify-worthy classifications — there is no per-outcome preference to configure. The only control is **Mute**.

**Per-schedule mute** — each schedule has a **Mute** checkbox (default off). When enabled, it silences *all* notifications for that schedule's runs: no bell entry and no external delivery. A per-run override is available via `Run.muted` (the same field the `muted` flag on the API/MCP call sets) for schedule runs when you need to silence a single dispatch without muting the whole schedule.

**Non-scheduled runs** — runs started from the dashboard, [Jobs API](https://srtab.github.io/daiv/dev/features/jobs-api/index.md), or [MCP endpoint](https://srtab.github.io/daiv/dev/features/mcp-endpoint/index.md) notify their initiator on a notify-worthy outcome. Pass the `muted` flag in the API or MCP call to silence a specific run.

Muting a schedule produces full silence. Disconnecting your email channel (from `/accounts/channels/`) suppresses email delivery without silencing in-app notifications.

## Connecting Rocket Chat

If your administrator has enabled Rocket Chat for the instance, bind your handle so DAIV can message you:

1. Open **`/accounts/channels/`**.
1. In the **Rocket Chat** row, enter your `@username` and select **Connect**. (DAIV strips a leading `@` for you.)
1. DAIV verifies the username against the Rocket Chat instance. On success the row shows a **Verified** badge and your handle; an unknown user or an unreachable instance surfaces an error and nothing is saved.

Select **Disconnect** in the same row to remove the binding and stop Rocket Chat delivery.

Verification can fail

Connecting only succeeds when the configured Rocket Chat bot can look your username up. If the instance is temporarily unavailable or the user is not found, DAIV shows a message and leaves your channel unbound — no unverified binding is stored.

Enabling Rocket Chat is an administrator task

The Rocket Chat instance URL, bot user ID, and auth token are configured under **Dashboard > Configuration > Rocket Chat** (`/dashboard/configuration/rocketchat/`), which requires the **admin** role. Until an admin enables it there, the channel does not appear on your channels page.

## How delivery works

When a run finishes with a notify-worthy classification, DAIV records the notification and one delivery row per external channel, then dispatches each delivery on a background worker:

- A channel with no usable binding (for example Rocket Chat before you connect, or an unknown channel) is recorded as **skipped** rather than attempted.
- Transient failures are retried up to three attempts with a backoff between tries; a permanent failure (such as a refused recipient or a disabled channel) is marked **failed** and not retried.
- The in-app bell entry is independent of external delivery — it is written even when every external channel is skipped or fails. (A muted run produces no bell entry at all — muting is full silence.)

## Related pages

- **[Scheduled Jobs](https://srtab.github.io/daiv/dev/features/scheduled-jobs/index.md)**

  Recurring runs with a mute toggle and subscriber list.

- **[Jobs API](https://srtab.github.io/daiv/dev/features/jobs-api/index.md)**

  Submit runs programmatically with a `muted` flag.

- **[Sessions](https://srtab.github.io/daiv/dev/features/sessions/index.md)**

  Every notification links to the session or batch in the sessions list.

- **[MCP Endpoint](https://srtab.github.io/daiv/dev/features/mcp-endpoint/index.md)**

  Submit jobs from MCP clients, with a `muted` flag.
