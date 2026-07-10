# Scheduled Jobs

Scheduled Jobs let you run DAIV agents on a recurring basis — no CI pipeline or cron script required. Define a prompt, pick one or more repositories and a frequency, and DAIV handles the rest.

This is useful when you want to:

- **Automate recurring tasks** — e.g., weekly dependency audits, nightly code quality scans
- **Keep repositories tidy** — e.g., close stale branches every Monday
- **Generate periodic reports** — e.g., summarise recent changes for a changelog draft
- **Run maintenance prompts** — e.g., check for TODO comments or outdated documentation

## Creating a schedule

Navigate to **Dashboard > Schedules > Create schedule** and fill in the form:

| Field                   | Description                                                                                                                                                                                                                         |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Name**                | A short label for the schedule (e.g., "Weekly dep audit")                                                                                                                                                                           |
| **Prompt**              | What the agent should do — same format as a Jobs API prompt                                                                                                                                                                         |
| **Repositories**        | One to 20 repositories, added through the repo picker. Each entry has its own branch or ref — leave a repository's ref blank to start from its default branch. Each run [fans out](#how-it-works) into one agent run per repository |
| **Agent model**         | The model and thinking level the runs use, chosen with the agent picker. Leave it on the default to use the instance's configured model                                                                                             |
| **Frequency**           | How often the job runs (see [Frequency options](#frequency-options))                                                                                                                                                                |
| **Time**                | Time of day for Daily, Weekdays, and Weekly schedules                                                                                                                                                                               |
| **Date & time**         | The specific date and time a one-off (**Once**) schedule fires                                                                                                                                                                      |
| **Notify on**           | When the owner (and any [subscribers](#subscribers)) get a finish notification — defaults to *Never*                                                                                                                                |
| **Subscribers**         | Other DAIV users to CC on finish notifications (see [Subscribers](#subscribers))                                                                                                                                                    |
| **Sandbox environment** | The named [sandbox](https://srtab.github.io/daiv/dev/features/sandbox/index.md) environment the runs use. Leave it unset to fall back to the per-repository environment resolution                                                  |

Schedule times use the DAIV instance's configured timezone (set by the deployment); there is no per-schedule timezone choice. See [Timezone handling](#timezone-handling).

Once created, the schedule is **active** immediately. You can pause and resume it at any time — directly from the list (see [Managing schedules](#managing-schedules)) or via the **Enabled** checkbox on the edit page.

## Frequency options

| Frequency    | Cron equivalent      | Notes                                                                    |
| ------------ | -------------------- | ------------------------------------------------------------------------ |
| **Hourly**   | `0 * * * *`          | Runs at the top of every hour                                            |
| **Daily**    | `{mm} {HH} * * *`    | Runs once a day at the specified time                                    |
| **Weekdays** | `{mm} {HH} * * 1-5`  | Monday through Friday at the specified time                              |
| **Weekly**   | `{mm} {HH} * * 1`    | Every Monday at the specified time                                       |
| **Custom**   | *(user-provided)*    | Any valid five-field cron expression                                     |
| **Once**     | *(none — date/time)* | A one-off run at a specific **Date & time** instead of a cron expression |

Tip

The **Custom** frequency accepts standard five-field cron expressions (minute, hour, day-of-month, month, day-of-week). Use it for non-standard intervals like "every 6 hours" (`0 */6 * * *`) or "first Monday of the month" (`0 9 1-7 * 1`).

One-off schedules

A **Once** schedule runs a single time at the chosen date and time, then retires automatically — it is disabled, has no next run, and shows as a read-only **Fired** card. A fired one-off cannot be re-enabled; use **Duplicate** (see [Managing schedules](#managing-schedules)) to run it again.

## How it works

A background task runs every minute and checks for schedules whose next run time has passed. For each due schedule it:

1. Fans the schedule out into one agent run per target repository — enqueuing each via the same task backend used by the [Jobs API](https://srtab.github.io/daiv/dev/features/jobs-api/index.md) — and records the resulting batch ID
1. Records the run timestamp and increments the run counter
1. Computes the next run time based on the cron expression and the configured timezone (a **Once** schedule retires instead)

```
sequenceDiagram
    participant Dispatcher as Cron dispatcher (every minute)
    participant DB as Database
    participant Worker as Job worker

    Dispatcher->>DB: Query enabled schedules where next_run_at <= now
    loop For each due schedule
        loop For each target repository
            Dispatcher->>Worker: Enqueue agent run (repo_id, prompt, ref)
        end
        Dispatcher->>DB: Update last_run_at, last_run_batch_id, run_count, next_run_at
    end
```

Schedules use `SELECT ... FOR UPDATE (SKIP LOCKED)` to prevent double-dispatch if the dispatcher overlaps, and each schedule is processed in its own database savepoint so that one failure does not affect others.

If a schedule fails to dispatch, its next run time is still advanced to prevent repeated re-firing. If even that recovery fails, the schedule is automatically disabled to avoid an infinite retry loop.

## Timezone handling

Schedule times are interpreted in the configured timezone. DAIV converts the local fire time to UTC for storage, which means DST transitions are handled automatically — a daily job set to 09:00 `Europe/Lisbon` will always fire at 09:00 local time, even across clock changes.

## Managing schedules

From the **Scheduled Jobs** list, each schedule's actions menu lets you:

- **Pause / Resume** a schedule directly from the list — no need to open the edit page (it is also available via the **Enabled** checkbox there)
- **Run now** to dispatch an immediate batch run without waiting for the next scheduled time. You land on the resulting session (or the [Sessions](https://srtab.github.io/daiv/dev/features/sessions/index.md) list filtered to the batch when it spans multiple repositories)
- **Duplicate** a schedule to create a new one pre-filled from this one — the only way to re-run a fired one-off schedule
- **Edit** a schedule to change any field, including the **Enabled** checkbox
- **Delete** a schedule permanently — any currently running job will complete, but no new runs will be dispatched

Fired one-off (**Once**) schedules only offer **Duplicate** and **Delete**.

Each schedule card shows its name, status (**Active**, **Paused**, or **Fired**), frequency, the target repository (or "*N* repositories" when it targets more than one), next run time, last run time, total run count, and the owner avatar. Clicking the run count opens the [Sessions](https://srtab.github.io/daiv/dev/features/sessions/index.md) list filtered to that schedule's sessions.

Note

Non-admin users only see their own schedules; admins see everyone's. The owner avatar on each card makes it clear whose schedule it is.

## Subscribers

Schedule owners can CC other DAIV users on the finish notifications for their schedules. Subscribers:

- Receive the same notification as the owner whenever the schedule's `Notify on` condition matches (e.g., "On success only" or "Always").
- Gain **read-only** access to the sessions produced by the schedule — they can click through from the notification and view the session detail, transcript, and run timeline.
- Do **not** see the schedule itself in their own Scheduled Jobs list, and cannot edit, pause, run, or delete it.

### Adding subscribers

On the schedule form, use the **Subscribers** search to find a user by username, email, or name. Click a result to add them as a chip. Remove a chip with the × button. Save the schedule to persist the subscriber list.

Only the owner (or an admin) can change a schedule's subscribers.

### Self-unsubscribe

When a subscriber opens a session produced by a schedule they are CC'd on, the session detail page shows an **Unsubscribe** button next to the schedule name. Clicking it removes the subscriber from that schedule — no owner action needed.

Notification preferences

All subscribers inherit the schedule's `Notify on` setting. There is no per-subscriber override today. If the owner changes the setting, every subscriber's notification behavior changes with it.

## Relationship with the Jobs API

Scheduled Jobs and the [Jobs API](https://srtab.github.io/daiv/dev/features/jobs-api/index.md) use the same underlying task to execute agent work. The difference is how the job is triggered:

|                | Jobs API                 | Scheduled Jobs            |
| -------------- | ------------------------ | ------------------------- |
| **Trigger**    | HTTP request             | Automatic (cron-based)    |
| **Auth**       | API key                  | Dashboard login           |
| **Management** | Programmatic             | Web UI                    |
| **Use case**   | One-off or script-driven | Recurring, set-and-forget |

Info

If you need programmatic control over scheduling (e.g., creating schedules from a script), use the [Jobs API](https://srtab.github.io/daiv/dev/features/jobs-api/index.md) with an external scheduler like GitLab CI scheduled pipelines or system cron.

## Schedule templates

Admins can curate reusable **schedule templates** so teammates start from a known-good configuration rather than assembling a schedule from scratch.

Templates are managed at **Dashboard → Schedule templates** (admin nav). A template carries the same fields as a schedule (prompt, default repositories, frequency, time or cron, agent model and thinking level, notification preference) plus an optional description that helps users pick the right one. The repo picker is optional on templates — leave it empty to let users choose at schedule-create time.

When at least one template exists, templates are discoverable in three places:

- The **Scheduled Jobs** list page gains a **From template** button next to *Create schedule*.
- The schedule **empty state** gains a **Start from template** call-to-action alongside *Create blank schedule*.
- The schedule **create form** opens with a **Browse templates** row at the top.

All three open the same right-side gallery drawer. Each card shows the template name, frequency, default-repos summary, and description; expanding a card reveals the full description, the list of pre-filled repos (with their branches), frequency detail, and notification preference. Clicking **Use this template** loads the schedule create form with every field pre-filled — you can still edit any of them before saving.

If you have unsaved changes in the create form, applying a template from the gallery prompts for confirmation first so you do not lose work.

Deleting a template never affects existing schedules: template values are copied into the schedule at creation time, and there is no link back.
