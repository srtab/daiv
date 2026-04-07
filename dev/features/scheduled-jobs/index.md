# Scheduled Jobs

Scheduled Jobs let you run DAIV agents on a recurring basis — no CI pipeline or cron script required. Define a prompt, pick a repository and frequency, and DAIV handles the rest.

This is useful when you want to:

- **Automate recurring tasks** — e.g., weekly dependency audits, nightly code quality scans
- **Keep repositories tidy** — e.g., close stale branches every Monday
- **Generate periodic reports** — e.g., summarise recent changes for a changelog draft
- **Run maintenance prompts** — e.g., check for TODO comments or outdated documentation

## Creating a schedule

Navigate to **Dashboard > Scheduled Jobs > Create schedule** and fill in the form:

| Field            | Description                                                          |
| ---------------- | -------------------------------------------------------------------- |
| **Name**         | A short label for the schedule (e.g., "Weekly dep audit")            |
| **Prompt**       | What the agent should do — same format as a Jobs API prompt          |
| **Repository**   | Repository identifier, e.g. `mygroup/myproject`                      |
| **Branch / Ref** | Git branch or tag. Leave blank for the repository's default branch   |
| **Frequency**    | How often the job runs (see [Frequency options](#frequency-options)) |
| **Time**         | Time of day for Daily, Weekdays, and Weekly schedules                |
| **Timezone**     | IANA timezone for the schedule (e.g., `Europe/Lisbon`, `UTC`)        |

Once created, the schedule is **active** immediately. You can pause and resume it at any time from the edit page.

## Frequency options

| Frequency    | Cron equivalent     | Notes                                       |
| ------------ | ------------------- | ------------------------------------------- |
| **Hourly**   | `0 * * * *`         | Runs at the top of every hour               |
| **Daily**    | `{mm} {HH} * * *`   | Runs once a day at the specified time       |
| **Weekdays** | `{mm} {HH} * * 1-5` | Monday through Friday at the specified time |
| **Weekly**   | `{mm} {HH} * * 1`   | Every Monday at the specified time          |
| **Custom**   | *(user-provided)*   | Any valid five-field cron expression        |

Tip

The **Custom** frequency accepts standard five-field cron expressions (minute, hour, day-of-month, month, day-of-week). Use it for non-standard intervals like "every 6 hours" (`0 */6 * * *`) or "first Monday of the month" (`0 9 1-7 * 1`).

## How it works

A background task runs every minute and checks for schedules whose next run time has passed. For each due schedule it:

1. Enqueues a job via the same task backend used by the [Jobs API](https://srtab.github.io/daiv/dev/features/jobs-api/index.md)
1. Records the run timestamp and increments the run counter
1. Computes the next run time based on the cron expression and the schedule's timezone

```
sequenceDiagram
    participant Dispatcher as Cron dispatcher (every minute)
    participant DB as Database
    participant Worker as Job worker

    Dispatcher->>DB: Query enabled schedules where next_run_at <= now
    loop For each due schedule
        Dispatcher->>Worker: Enqueue job (repo_id, prompt, ref)
        Dispatcher->>DB: Update last_run_at, run_count, next_run_at
    end
```

Schedules use `SELECT ... FOR UPDATE (SKIP LOCKED)` to prevent double-dispatch if the dispatcher overlaps, and each schedule is processed in its own database savepoint so that one failure does not affect others.

If a schedule fails to dispatch, its next run time is still advanced to prevent repeated re-firing. If even that recovery fails, the schedule is automatically disabled to avoid an infinite retry loop.

## Timezone handling

Schedule times are interpreted in the configured timezone. DAIV converts the local fire time to UTC for storage, which means DST transitions are handled automatically — a daily job set to 09:00 `Europe/Lisbon` will always fire at 09:00 local time, even across clock changes.

## Managing schedules

From the **Scheduled Jobs** list you can:

- **Edit** a schedule to change any field, including toggling the **Enabled** checkbox to pause or resume it
- **Delete** a schedule permanently — any currently running job will complete, but no new runs will be dispatched

The list shows the schedule name, status (Active / Paused), frequency, repository, next run time, last run time, and total run count. Admin users also see the schedule owner.

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
