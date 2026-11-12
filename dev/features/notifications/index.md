# Notifications

DAIV tells you when your work needs attention. When a run finishes with something to look at, such as an issue found, a situation that needs review or an outright failure, DAIV writes an in-app notification and delivers it to external channels: email, Rocket.Chat and Telegram. Clean runs stay silent and appear only in their session, under **Sessions**.

Notifications are per user. Each recipient gets their own copy, delivered through that user's own channels.

## What produces a notification

DAIV classifies every finished run and notifies only when the outcome warrants it. The classifications that notify are **found-issues**, **needs-attention**, **needs-input** and **failed**. A run classified **all-clear** is silent: it appears in its session, under **Sessions**, and produces no notification. The exception is a run that a CI failure woke: it always notifies its owner, all-clear included (see [pipeline watch](https://docs.daivagent.com/dev/features/pipeline-watch/#notifications)).

A run is **needs-input** when the agent stopped to ask you a question instead of guessing. The notification's subject says the run is waiting for your answer, and its body lists the questions; open the session to answer them. A run that an issue or merge or pull request webhook started is the exception: DAIV posts its question as a comment on the issue or request, which mentions whoever triggered the run, so DAIV sends no separate notification for it.

Runs that webhooks start, such as [issue addressing](https://docs.daivagent.com/dev/features/issue-addressing/index.md) and the [pull request assistant](https://docs.daivagent.com/dev/features/pull-request-assistant/index.md), report back in the issue or request thread. Like runs you start from the dashboard, the [jobs API](https://docs.daivagent.com/dev/features/jobs-api/index.md) or the [MCP endpoint](https://docs.daivagent.com/dev/features/mcp-endpoint/index.md), they also notify their initiator when the outcome notifies. To silence a run you start through the API or MCP, pass the `muted` flag.

These events cover runs and schedules:

| Event                                               | When it fires                                                                                                                                                                                     | Recipients                                                                                                                      |
| --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| **Job finished** (`job.finished`)                   | A single agent run finishes with an outcome that notifies.                                                                                                                                        | The user who started the run.                                                                                                   |
| **Job batch finished** (`job_batch.finished`)       | Every run in a multi-run batch has finished. DAIV sends one rollup, not one message per run.                                                                                                      | The batch owner and, for a [scheduled](https://docs.daivagent.com/dev/features/scheduled-jobs/index.md) batch, its subscribers. |
| **Schedule finished** (`schedule.finished`)         | A run of a [scheduled job](https://docs.daivagent.com/dev/features/scheduled-jobs/index.md) finishes with an outcome that notifies.                                                               | The schedule's owner and subscribers.                                                                                           |
| **Schedule can't run** (`schedule_dispatch.failed`) | A [scheduled job](https://docs.daivagent.com/dev/features/scheduled-jobs/#when-a-schedule-cant-run) fails to start a run. DAIV sends it once per failing streak, even when the schedule is muted. | The schedule's owner.                                                                                                           |

A batch is a group of runs that share a batch ID, for example a scheduled job that fans out across several repositories. For a multi-run batch, DAIV suppresses the per-run notifications and sends one **Job batch finished** rollup once the last run has finished. The rollup says how many runs notified and how many were all-clear. A run waiting for an answer counts as notifying, and the rollup lists it right after the failures.

Other notifications, such as the one DAIV sends when [pipeline watch](https://docs.daivagent.com/dev/features/pipeline-watch/#notifications) runs out of attempts, are described with their features.

## Channels

DAIV reaches you through channels. In-app notifications are always on. Email, Rocket.Chat and Telegram are external channels, and your administrator decides which of the last two appear.

| Channel         | What you get                                                                                                         | What it needs                                                                                                   |
| --------------- | -------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| In-app          | **Notifications** in the sidebar shows your unread count and opens your full history at `/dashboard/notifications/`. | Nothing.                                                                                                        |
| Email           | A message to your account email address.                                                                             | Nothing. DAIV keeps a verified email channel in sync with your account address, so there is nothing to connect. |
| **Rocket Chat** | A direct message from the DAIV bot.                                                                                  | An administrator has enabled Rocket.Chat, and you have connected your `@username`.                              |
| **Telegram**    | A direct message from the DAIV bot.                                                                                  | An administrator has enabled Telegram, and you have connected your chat.                                        |

A message about a run includes the run's summary. For a run waiting for an answer, that is the list of questions.

### The in-app list

DAIV writes an in-app entry for each run whose outcome notifies, and none for an all-clear run.

- `/dashboard/notifications/` lists your full history. Filter it with **All**, **Unread** and **Read**, or select **Mark all as read**. Opening a notification marks it read. The **Settings** link opens your [notification channels](#connect-a-channel).
- The unread badge next to **Notifications** and the session lists in the sidebar update live over a server-sent events stream (`GET /api/nav/events`), so a new notification or a run starting or finishing appears without a reload. The stream needs Redis. Without it, they show their values from when the page loaded.

## Connect a channel

Email and in-app need no setup. Rocket.Chat and Telegram need an administrator to enable them first, and you then connect your own account on the channels page.

You need:

- For Rocket.Chat, an administrator who has enabled it in **Settings** → **Configuration** → **Rocket Chat**, with the instance URL, bot user ID and auth token. See [Site configuration](https://docs.daivagent.com/dev/reference/site-configuration/#setting-groups).
- For Telegram, an administrator who has enabled it in **Settings** → **Configuration** → **Telegram**, as described in [Set up Telegram](https://docs.daivagent.com/dev/reference/site-configuration/#set-up-telegram). Telegram delivery to chats that are already linked needs only outbound internet access, but connecting a new chat needs DAIV reachable from the internet over HTTPS. On an instance without public ingress, you cannot link new chats.
- Until the channel is enabled, its row does not appear on your channels page.

To open the channels page, select **Notification channels** in the account menu at the bottom of the sidebar (`/accounts/channels/`).

### Connect Rocket.Chat

1. On the **Notification channels** page, find the **Rocket Chat** row.
1. Enter your Rocket.Chat `@username`. DAIV strips a leading `@`.
1. Select **Connect**.

DAIV checks the username against the Rocket.Chat instance. On success, the row shows a **Verified** badge and your handle. If the user is unknown or the instance is unreachable, DAIV shows an error and saves nothing: it stores no unverified binding.

To stop Rocket.Chat delivery, select **Disconnect** in the same row.

### Connect Telegram

A Telegram bot cannot start a conversation with you, so connecting is a handshake: you message the bot first, through a single-use link that DAIV gives you.

Warning

The link links whichever Telegram chat opens it first to your DAIV account. Do not forward it or share it.

1. On the **Notification channels** page, find the **Telegram** row.
1. Select **Connect**. DAIV hands you off to Telegram.
1. In Telegram, open the chat with the DAIV bot. Telegram sends `/start` for you.
1. Read the bot's reply, which confirms the connection, and check that the row on your channels page shows **Verified** and your Telegram handle.

You have nothing else to confirm, because messaging the bot is the verification.

The link works for 10 minutes and stops working as soon as any chat is linked to your account. If it expires, the bot tells you, and you start again from the channels page. If the row shows `Not ready — ask an administrator to finish the Telegram setup.` instead of **Connect**, ask your administrator to finish the setup.

A few rules apply to the link:

- DAIV keeps at most one Telegram chat per account and one account per chat. Connecting from a different Telegram account replaces the previous link, and `/start` from a chat that is linked elsewhere moves it to you.
- The bot links only one-to-one chats. If you add it to a group and send `/start` there, it replies `Message me directly — DAIV notifications are per-account and cannot go to a group.` and links nothing.

To stop Telegram delivery, do any one of these: select **Disconnect** on your channels page, send `/stop` to the bot, or block the bot in Telegram. Each unlinks the chat, and the row goes back to **Not configured**; select **Connect** to link again.

If DAIV could not be told that you blocked the bot, for example because its webhook was unreachable or Telegram refused the chat for another reason, the link stays on the row marked **Unverified** and delivers nothing. Select **Connect** to redo the handshake. If that does not clear it, select **Disconnect**, then **Connect**.

## Muting

Notifications fire on outcomes that notify, and you have no per-outcome setting. The only control is muting:

- **Scheduled runs.** A schedule's **Mute notifications** setting, off by default, silences every notification for that schedule's runs: no in-app entry and no external delivery. It does not silence **Schedule can't run**, which reports that the schedule did not run at all. Muting applies to the owner and every subscriber alike.
- **Other runs.** Runs you start from the dashboard, the [jobs API](https://docs.daivagent.com/dev/features/jobs-api/index.md) or the [MCP endpoint](https://docs.daivagent.com/dev/features/mcp-endpoint/index.md) notify you on an outcome that notifies. Pass the `muted` flag in the API or MCP call to silence a run.

A muted run produces no in-app entry at all: muting is full silence. DAIV has no setting for turning email off while keeping in-app notifications.

## How delivery works

When a run finishes with an outcome that notifies, DAIV records the notification, then one delivery for each external channel, and sends each delivery from a background worker:

- A channel with no usable connection, such as Rocket.Chat or Telegram before you connect, or a channel DAIV does not know, is recorded as **skipped** rather than attempted.
- DAIV retries a transient failure, up to three attempts in all, waiting 1 minute before the second and 5 minutes before the third. A permanent failure, such as a refused recipient or a disabled channel, is marked **failed** and not retried. When the provider names its own wait, as Telegram's flood control does, DAIV delays the next attempt to at least that long, up to one hour.
- Blocking the DAIV bot in Telegram normally unlinks your chat at once. When that message does not reach DAIV, DAIV notices the refusal on the next delivery instead. It keeps the link and marks it **Unverified**, because that condition never recovers by itself and retrying would only use up attempts. Later notifications are **skipped**, until you redo the handshake.
- The in-app entry does not depend on external delivery. DAIV writes it even when every external channel is skipped or fails.

## Related pages

- [Scheduled jobs](https://docs.daivagent.com/dev/features/scheduled-jobs/index.md): recurring runs with a mute setting and a subscriber list.
- [Jobs API](https://docs.daivagent.com/dev/features/jobs-api/index.md): submit runs programmatically, with a `muted` flag.
- [Sessions](https://docs.daivagent.com/dev/features/sessions/index.md): every notification links to its session or batch in the sessions list.
- [MCP endpoint](https://docs.daivagent.com/dev/features/mcp-endpoint/index.md): submit jobs from MCP clients, with a `muted` flag.
