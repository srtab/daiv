# Notifications

DAIV tells you when your work needs attention. When a run finishes with something to look at — an issue found, a situation that needs review, or an outright failure — DAIV writes an in-app notification and delivers it to external channels like email, Rocket Chat, and Telegram. Clean runs stay silent and appear only in the Feed.

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

DAIV reaches you through channels. The in-app bell is always available; email, Rocket Chat, and Telegram are external delivery channels.

- **In-app bell**

  The notification bell in the dashboard header shows your unread count and a dropdown of recent items. The full history lives at `/dashboard/notifications/`.

- **Email**

  Delivered to your account email. DAIV keeps an email channel binding in sync with your account address automatically — there is nothing to connect.

- **Rocket Chat**

  A direct message from the DAIV bot, when your administrator has enabled Rocket Chat and you have bound your `@username`.

- **Telegram**

  A direct message from the DAIV bot, when your administrator has enabled Telegram and you have linked your chat.

### The in-app bell and list

The bell entry is written for **notify-worthy** runs — those classified as found-issues, needs-attention, or failed. All-clear runs are silent and do not produce a bell entry.

- The bell dropdown shows your ten most recent notifications and marks them read when you open it.
- `/dashboard/notifications/` lists your full history with `All` / `Unread` / `Read` filters and a **Mark all as read** action.
- The unread badge — and the **N running** badge next to *Sessions* in the sidebar — update live over a server-sent-events stream (`GET /api/nav/events`), so a new notification or a run starting or finishing shows up without a page reload. The stream requires Redis (already required for chat and caching); without it the badges simply show their page-load values.

### Email

Email needs no setup. When your account is created (or your email changes), DAIV maintains a verified email channel binding pointing at your account address. Email is delivered for any notify-worthy, un-muted run.

### Rocket Chat

Rocket Chat is an optional integration. It appears as a channel only when an administrator has enabled it for the instance, after which you bind your own Rocket Chat handle so DAIV can DM you.

### Telegram

Telegram is an optional integration. It appears as a channel only when an administrator has enabled it for the instance, after which you link your own Telegram chat so the DAIV bot can message you.

Unlike Rocket Chat, a Telegram bot cannot start a conversation with you — you have to message it first. So connecting is a handshake rather than a username you type.

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

## Connecting Telegram

If your administrator has enabled Telegram for the instance, link your chat so DAIV can message you:

1. Open **`/accounts/channels/`**.
1. In the **Telegram** row, select **Connect**. DAIV hands you off to Telegram with a single-use link.
1. Telegram opens a chat with the DAIV bot and sends `/start` for you. The bot replies to confirm, and the row on your channels page shows a **Verified** badge with your Telegram handle.

Verification is inherent to this flow — you demonstrably messaged the bot — so there is nothing else to confirm.

To stop delivery, either select **Disconnect** on your channels page, send `/stop` to the bot, or block the bot in Telegram. All three unlink the chat, and the row goes back to **Not configured** — select **Connect** again whenever you want it back.

If the row reads Unverified

Blocking the bot normally removes the link outright, but if DAIV could not be told at the time (its webhook was unreachable, or Telegram was refusing the chat for another reason), the link stays on the row marked **Unverified**. It delivers nothing in that state. Select **Connect** on the row to redo the handshake; if that does not clear it, **Disconnect** first and then **Connect**.

The link expires in 10 minutes

The **Connect** link is valid for 10 minutes and stops working as soon as any chat is linked to your account. If it expires, the bot tells you so — start again from your channels page. Do not forward the link to anyone: whoever opens it first links *their* chat to *your* account.

One chat, one account

DAIV keeps at most one Telegram chat per account and one account per chat. Connecting from a different Telegram account replaces the previous link rather than adding a second one, and `/start` from a chat already linked elsewhere re-points it to you.

Group chats are refused

DAIV notifications are per-account, so the bot only links one-to-one chats. Adding it to a group and sending `/start` there gets a "message me directly" reply and links nothing.

Enabling Telegram is an administrator task

The bot token is configured under **Dashboard > Configuration > Telegram** (`/dashboard/configuration/telegram/`), which requires the **admin** role. Until an admin enables it there, the channel does not appear on your channels page. Sending notifications only needs outbound internet access, but the connect handshake needs DAIV to be reachable from the internet over HTTPS — on an instance without public ingress, Telegram delivery works for chats already linked, but new links cannot be made.

One bot token serves exactly one instance

Telegram allows a single webhook per bot, so a second environment configured with the same token steals the handshake from the first. DAIV's reconcile job surfaces that in the logs — naming the URL it found — but it cannot prevent it. Give staging its own bot.

## How delivery works

When a run finishes with a notify-worthy classification, DAIV records the notification and one delivery row per external channel, then dispatches each delivery on a background worker:

- A channel with no usable binding (for example Rocket Chat or Telegram before you connect, or an unknown channel) is recorded as **skipped** rather than attempted.
- Transient failures are retried up to three attempts with a backoff between tries; a permanent failure (such as a refused recipient or a disabled channel) is marked **failed** and not retried. When the provider names its own wait — Telegram's flood control does — the next attempt is delayed to at least that long instead of the standard backoff.
- Blocking the DAIV bot in Telegram unlinks your chat. Telegram normally tells DAIV directly, and the link is removed — your channels page reads **Not configured**. When that message does not arrive, the refusal is noticed on the next delivery instead, and the link is kept but flipped to **unverified**: that condition never recovers on its own, so continuing to retry would only burn attempts. Later notifications record as **skipped**, and the row shows **Unverified** alongside a **Connect** control until you redo the handshake.
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
