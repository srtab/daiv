# Notifications

DAIV tells you when your work finishes. When an agent run, a multi-repository batch, or a scheduled job reaches a terminal state, DAIV writes an in-app notification and — depending on your preference — also delivers it to external channels like email and Rocket Chat. You decide *when* to be told (never, always, only on success, only on failure) and *where* the message lands.

Notifications are per-user: each recipient gets their own copy, with delivery resolved against that user's own channel bindings.

## What produces a notification

DAIV emits a notification when a run finishes. There are three event types:

| Event | When it fires | Recipients |
|-------|---------------|------------|
| **Job finished** (`job.finished`) | A single agent run reaches a terminal state (successful or failed) | The user who started the run |
| **Job batch finished** (`job_batch.finished`) | Every run in a multi-run batch is terminal — a single rollup, not one message per run | The batch owner (and, for [scheduled](scheduled-jobs.md) batches, any subscribers) |
| **Schedule finished** (`schedule.finished`) | A run tied to a [scheduled job](scheduled-jobs.md) finishes | The schedule owner and its subscribers |

!!! note "Batches collapse into one message"
    A batch is a group of runs sharing a batch ID — for example a [scheduled job](scheduled-jobs.md) that fans out across several repositories. DAIV suppresses the per-run notifications for a multi-run batch and sends a single **Job batch finished** rollup once the last sibling is terminal, summarising how many runs succeeded and failed.

!!! info "Webhook-triggered runs are silent"
    Runs triggered by a GitLab/GitHub issue or merge/pull-request webhook (for example [issue addressing](issue-addressing.md) or the [pull request assistant](pull-request-assistant.md)) do **not** generate notifications — those flows already report back inside the issue or MR/PR thread.

## Channels

DAIV reaches you through channels. The in-app bell is always available; email and Rocket Chat are external delivery channels.

<div class="grid cards" markdown>

-   :octicons-bell-24: __In-app bell__

    The notification bell in the dashboard header shows your unread count and a dropdown of recent items. The full history lives at `/dashboard/notifications/`.

-   :octicons-mail-24: __Email__

    Delivered to your account email. DAIV keeps an email channel binding in sync with your account address automatically — there is nothing to connect.

-   :simple-rocketdotchat: __Rocket Chat__

    A direct message from the DAIV bot, when your administrator has enabled Rocket Chat and you have bound your `@username`.

</div>

### The in-app bell and list

The bell entry is written for **every** terminal run that has a recipient, regardless of your notification preference — so the dashboard always reflects what happened. Your "Notify on" preference only controls whether DAIV *also* delivers to external channels (email, Rocket Chat).

- The bell dropdown shows your ten most recent notifications and marks them read when you open it.
- `/dashboard/notifications/` lists your full history with `All` / `Unread` / `Read` filters and a **Mark all as read** action.

### Email

Email needs no setup. When your account is created (or your email changes), DAIV maintains a verified email channel binding pointing at your account address. Email delivery is gated by your "Notify on" preference like any other external channel.

### Rocket Chat

Rocket Chat is an optional integration. It appears as a channel only when an administrator has enabled it for the instance, after which you bind your own Rocket Chat handle so DAIV can DM you.

## Choosing when to be notified

Every run carries a **Notify on** setting with four values:

| Value | Behaviour |
|-------|-----------|
| **Never** (`never`) | No external delivery |
| **Always** (`always`) | Deliver on any terminal outcome (success or failure) |
| **On success only** (`on_success`) | Deliver only when the run succeeds |
| **On failure only** (`on_failure`) | Deliver only when the run fails |

This setting gates external channels only — the in-app bell entry is always recorded.

### Where the effective value comes from

DAIV resolves the preference that applies to a finished run by precedence, taking the first that is set:

1. **The run's own "Notify on" override** — chosen when you start the run.
2. **The schedule's "Notify on"** — for [scheduled-job](scheduled-jobs.md) runs that did not set a per-run override. Schedules default to **Never**.
3. **Your default preference** — your account's `notify_on_jobs`, which applies to runs you start from the UI, [Jobs API](jobs-api.md), or [MCP endpoint](mcp-endpoint.md). It defaults to **On failure only**.
4. Otherwise, **Never**.

### Setting "Notify on" per run

- **Run composer** — the **Dashboard > Runs** composer (`/dashboard/runs/`) has a **Notify me** field, pre-filled from your default preference.
- **[Jobs API](jobs-api.md)** — pass `notify_on` in the submit payload.
- **[MCP endpoint](mcp-endpoint.md)** — pass the `notify_on` argument to `submit_job`.
- **[Scheduled jobs](scheduled-jobs.md)** — the schedule's **Notify on** field, applied to every run it fans out (defaults to **Never**).

Omit `notify_on` on a run and it falls through to the next level in the precedence chain above.

## Connecting Rocket Chat

If your administrator has enabled Rocket Chat for the instance, bind your handle so DAIV can message you:

1. Open **`/accounts/channels/`**.
2. In the **Rocket Chat** row, enter your `@username` and select **Connect**. (DAIV strips a leading `@` for you.)
3. DAIV verifies the username against the Rocket Chat instance. On success the row shows a **Verified** badge and your handle; an unknown user or an unreachable instance surfaces an error and nothing is saved.

Select **Disconnect** in the same row to remove the binding and stop Rocket Chat delivery.

!!! warning "Verification can fail"
    Connecting only succeeds when the configured Rocket Chat bot can look your username up. If the instance is temporarily unavailable or the user is not found, DAIV shows a message and leaves your channel unbound — no unverified binding is stored.

!!! note "Enabling Rocket Chat is an administrator task"
    The Rocket Chat instance URL, bot user ID, and auth token are configured under **Dashboard > Configuration > Rocket Chat** (`/dashboard/configuration/rocketchat/`), which requires the **admin** role. Until an admin enables it there, the channel does not appear on your channels page.

## How delivery works

When a run finishes, DAIV records the notification and one delivery row per external channel, then dispatches each delivery on a background worker:

- A channel with no usable binding (for example Rocket Chat before you connect, or an unknown channel) is recorded as **skipped** rather than attempted.
- Transient failures are retried up to three attempts with a backoff between tries; a permanent failure (such as a refused recipient or a disabled channel) is marked **failed** and not retried.
- The in-app bell entry is independent of external delivery — it is written even when every external channel is skipped or fails.

## Related

<div class="grid cards" markdown>

-   :octicons-clock-24: __[Scheduled Jobs](scheduled-jobs.md)__

    Recurring runs with their own **Notify on** setting and subscriber list.

-   :octicons-terminal-24: __[Jobs API](jobs-api.md)__

    Submit runs programmatically with a per-run `notify_on`.

-   :octicons-pulse-24: __[Sessions](sessions.md)__

    Every notification links to the session or batch in the sessions list.

-   :octicons-plug-24: __[MCP Endpoint](mcp-endpoint.md)__

    Submit jobs from MCP clients, with `notify_on` support.

</div>
