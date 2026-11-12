---
description: "Run DAIV agents on a cron schedule across one or more repositories for recurring work such as dependency audits and nightly code-quality scans."
---

# Scheduled jobs

Scheduled jobs run DAIV agents on a recurring basis, with no CI pipeline or cron script. You give a prompt, one or more repositories and a frequency, and DAIV starts the runs when they are due.

Use a schedule for recurring work such as a weekly dependency audit, a nightly code-quality scan, closing stale branches every Monday, a periodic summary of recent changes for a changelog draft, or a check for TODO comments or outdated documentation.

## Create a schedule

You need access to the repositories you want to run against. A schedule runs with its owner's repository permissions, so DAIV must be able to confirm that you have write access to each repository; see [When a schedule can't run](#when-a-schedule-cant-run).

1. In the sidebar, select **Schedules**.
2. Select **Create schedule**.
3. Enter a **Name**, for example `Weekly dep audit`.
4. In the prompt box, describe what the agent does in each run.
5. Select **Add repository** and choose one to 20 repositories. Each entry has its own branch; leave it on the default to start from the repository's default branch.
6. Choose a **Frequency**, then fill the field it shows (see [Frequency options](#frequency-options)).
7. Optionally set the other fields in [Schedule fields](#schedule-fields).
8. Select **Create schedule**.

The schedule is active as soon as you save it. To pause it, see [Manage schedules](#manage-schedules).

### Schedule fields

| Field | Description |
|-------|-------------|
| **Name** | A short label for the schedule. |
| Prompt box | What the agent does. It has the same form as a [jobs API](jobs-api.md) prompt. |
| Repositories | One to 20 repositories, each with its own branch. A merge or pull request a run opens targets that branch, and DAIV assigns it to the schedule's owner when their DAIV account is OAuth-linked to the git platform. Each run fans out into one agent run per repository. |
| Agent picker | The model and thinking level the runs use. The picker starts on the instance's default model, and the schedule stores the model it shows. A run tries that model first, then the repository's `models.agent` model as a fallback. A schedule created over MCP without `agent_model` has no stored model and uses the repository's model, else the system default; see [`schedule_job` parameters](mcp-endpoint.md#schedule_job-parameters). |
| Environment picker | The named [sandbox](sandbox.md) environment the runs use. It defaults to **Auto**, which resolves an environment for each repository. |
| **Frequency** | How often the schedule runs; see [Frequency options](#frequency-options). |
| **Time** | The time of day for **Daily**, **Weekdays** and **Weekly** schedules. |
| **Cron Expression** | The five-field cron expression for a **Custom** schedule. |
| **Date & time** | The moment a **Once** schedule fires. |
| **Intent** | What the schedule is for: **Watch & find**, **Do & change** or **Report**. The default is **Watch & find**. |
| **Mute notifications** | Silences all notifications for this schedule's runs, in-app and external. Off by default. The owner still gets the [Schedule can't run](#when-a-schedule-cant-run) notice. |
| **Subscribers** | Other DAIV users who get the finish notifications; see [Subscribers](#subscribers). |
| **Enabled** | On the edit page only. When off, the schedule does not run until you turn it on again. |

Schedule times are read in the instance's fixed time zone, and a schedule has no time zone of its own; see [Time zone](#time-zone).

## Frequency options

| Frequency | Cron equivalent | Notes |
|-----------|-----------------|-------|
| **Hourly** | `0 * * * *` | Runs at the top of every hour. |
| **Daily** | `{mm} {HH} * * *` | Runs once a day at the time you set. |
| **Weekdays** | `{mm} {HH} * * 1-5` | Runs Monday through Friday at the time you set. |
| **Weekly** | `{mm} {HH} * * 1` | Runs every Monday at the time you set. |
| **Custom** | Your expression | Any valid five-field cron expression: minute, hour, day of month, month, day of week. For example `0 */6 * * *` runs every 6 hours, and `0 9 1-7 * 1` runs on the first Monday of the month. |
| **Once** | None | Runs a single time at the **Date & time** you set. |

A **Once** schedule runs one time, then retires: DAIV disables it, gives it no next run, and shows it as a read-only **Fired** card. You cannot re-enable a fired one-off schedule; use **Duplicate** to run it again (see [Manage schedules](#manage-schedules)).

## How scheduled runs start

Every minute, DAIV checks for enabled schedules that are due. For each one it:

1. Starts one agent run per target repository, through the same task backend as the [jobs API](jobs-api.md), and records the batch ID.
2. Records the run time and adds one to the run count.
3. Works out the next run time from the schedule's frequency. A **Once** schedule retires instead.

If a schedule fails to start, DAIV still moves its next run time forward, so it does not fire again every minute. If even that recovery fails, DAIV disables the schedule. Either way, DAIV records the failure on the schedule and tells the owner; see [When a schedule can't run](#when-a-schedule-cant-run).

A schedule never starts twice when a check overruns the minute, and one schedule's failure does not stop the others. A schedule whose owner is inactive is skipped. After downtime, the times a schedule missed collapse into a single run, and DAIV then moves to the next time after now.

A scheduled run cannot ask you a question, because nobody is there to answer. The agent does not get the ask-the-user tool.

## Time zone

DAIV reads schedule times in the `Europe/Lisbon` time zone, and a schedule has no time zone of its own. A daily schedule set to 09:00 fires at 09:00 `Europe/Lisbon` all year, across daylight saving changes. The zone is fixed in the settings; see [Time zone](../reference/env-variables.md#time-zone).

## Manage schedules

On the **Scheduled Jobs** page, each schedule has an actions menu:

- **Pause** or **Resume** the schedule, without opening the edit page.
- **Run now** to start a batch immediately instead of waiting for the next scheduled time. You land on the resulting session, or on the [Sessions](sessions.md) list filtered to the batch when it spans several repositories.
- **Duplicate** the schedule to create a new one pre-filled from it. It is the only way to run a fired one-off schedule again.
- **Delete** the schedule; see [Delete a schedule](#delete-a-schedule).

A fired one-off schedule offers only **Duplicate** and **Delete**. To change a schedule, including its **Enabled** setting, select its card to open the edit page.

Each schedule card shows its name, its status (**Active**, **Failing**, **Paused** or **Fired**), its frequency, its target repository (or "*N* repositories" when it has several), its next and last run times, its run count and its owner's avatar. Selecting the run count opens the **Sessions** list filtered to that schedule's sessions.

Non-admin users see only their own schedules. Admins see everyone's, and the owner avatar on each card shows whose it is.

### Delete a schedule

!!! warning
    Deleting a schedule removes it permanently and cannot be undone. A run that is already running completes, but DAIV starts no new runs. The sessions the schedule produced remain, but its subscribers lose access to them.

1. In the schedule's actions menu, select **Delete**, or select **Delete schedule** on its edit page.
2. On the confirmation page, select **Delete schedule**.

## When a schedule can't run

A scheduled run can fail to start. Most often, DAIV could not confirm that the schedule's owner has write access to one of its repositories, because schedules run with their owner's repository permissions. It can also be an unexpected error, including every repository in the batch failing to start. When that happens:

- The schedule card shows **Failing** with the reason, and the edit page explains how to fix it. A paused schedule reads **Paused** but keeps the reason.
- The owner gets one **Schedule can't run** [notification](notifications.md) per failing streak, even when the schedule is muted. Subscribers get none, since they cannot fix the owner's access.
- A recurring schedule keeps trying at each scheduled time. A one-off schedule retires; give it a new date and time and enable it to try again.

The failing state clears at the next scheduled run that starts, or when you change the schedule's repositories. **Run now** does not clear it.

## Subscribers

A schedule's owner can add other DAIV users as subscribers, who get the schedule's finish notifications. A subscriber:

- Receives the same notifications as the owner on notify-worthy outcomes, unless the schedule is muted. Muting silences the owner and every subscriber alike, and there is no per-subscriber setting. The outcomes are found issues, needs attention, needs input and failed.
- Gets read-only access to the sessions the schedule produces: the session detail, transcript and run timeline.
- Does not see the schedule in their own list, and cannot edit, pause, run or delete it.

Only the owner or an admin can change a schedule's subscribers.

To add a subscriber:

1. Open the schedule's form and find **Subscribers**.
2. Select **Add subscriber** and search for a user by username, email or name.
3. Select a result to add the user.
4. Select **Save changes**.

To remove a subscriber, select the × on their chip in **Subscribers**, then select **Save changes**. A subscriber cannot unsubscribe themselves; ask the owner or an admin.

## Scheduled jobs and the jobs API

Scheduled jobs and the [jobs API](jobs-api.md) start the same kind of agent run. They differ in what starts it:

| | Jobs API | Scheduled jobs |
|---|---|---|
| **Trigger** | An HTTP request | The schedule, on its own |
| **Authentication** | API key | Dashboard sign-in |
| **Management** | Programmatic | Web UI |
| **Use** | A one-off or script-driven run | A recurring run |

To create schedules from a script, call the [`schedule_job` tool of the MCP endpoint](mcp-endpoint.md#schedule_job-parameters), or trigger runs from an external scheduler, such as a GitLab CI scheduled pipeline or system cron, through the jobs API.

## Schedule templates

A schedule template is a reusable starting point for a schedule. It carries the same settings as a schedule (prompt, default repositories, frequency, time or cron expression, agent model and thinking level, intent and mute setting), plus an optional description that helps people choose. A template has no sandbox environment and no **Date & time**. When you start a schedule from a template, DAIV copies the template's values into the schedule form, and you can change any of them before you save. Editing or deleting a template never changes the schedules made from it.

### Start a schedule from a template

You need at least one template; an admin creates them. Then:

1. Open the template gallery from the **Schedules** page, with **From template** or, on an empty list, **Start from template**. In the create form, use **Browse templates**.
2. Search for a template. Each card shows its name, frequency, default repositories and description, and you can expand it for the full detail.
3. Select **Use** on the template you want. If the create form has unsaved changes, DAIV asks `Discard your current changes and apply this template?` so you do not lose work.
4. Adjust the pre-filled fields, then select **Create schedule**.

### Create a schedule template

Creating templates needs the admin role.

1. In the sidebar, under the admin section, select **Schedule templates**.
2. Select **New template**.
3. Fill in the **Name** and an optional **Description**, then the same settings as a schedule. Repositories are optional: leave them empty to let people choose when they create the schedule.
4. Select **Create template**.

### Delete a schedule template

!!! warning
    Deleting a template removes it from the gallery and cannot be undone. Schedules made from it are unchanged.

Open the template and select **Delete template**, then confirm with **Delete template**.
