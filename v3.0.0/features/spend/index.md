# Spend

**Insights → Spend** (`/dashboard/insights/spend/`) shows what DAIV spent on models across the sessions you can see: how much, by whom, on which repositories, started by what, on which models and through which providers. Every figure adds up to the total, and every row opens the sessions behind it. How DAIV counts tokens and prices them is in [Usage and cost](https://docs.daivagent.com/latest/reference/monitoring/#usage-and-cost).

## Choose a period and a scope

The toolbar sets what the page covers:

- **Period**: **Today**, **7 days**, **30 days** (the default), **90 days** or **All time**. A record counts in the period of the run it belongs to, by when the run was created (a run that waited in the queue counts on the day it was queued), or of the background task, by when the call was made.
- **Scope**: **Mine** shows only the runs you started and the background work that served them. **Everyone** shows every record you may see: the runs you started, the runs of schedules you subscribe to, and the runs on repositories you can read. Administrators open on **Everyone** and see every record; members open on **Mine**.

With nothing spent in the period, the page says **Nothing was spent in this period** and shows nothing else.

## Read the summary

Six tiles open the page:

| Tile                          | What it shows                                                                                                                                                                                                                                                                                                |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **total cost**                | The priced cost of the period. A `+` after it marks a floor: some tokens have no price, or some runs recorded nothing                                                                                                                                                                                        |
| **tokens**                    | Input and output tokens of every model call                                                                                                                                                                                                                                                                  |
| **prompt cache hit rate**     | The share of input tokens served from the prompt cache, as [Usage and cost](https://docs.daivagent.com/latest/reference/monitoring/#usage-and-cost) counts it; **not reported** when no provider reported cache use. The line under it is the estimated saving, with `+` when part of it could not be priced |
| **runs with spend**           | Runs that recorded any spend                                                                                                                                                                                                                                                                                 |
| **average per run**           | Run spend divided by the runs with spend; background spend is left out                                                                                                                                                                                                                                       |
| **per run that changed code** | The same over successful runs that changed code                                                                                                                                                                                                                                                              |

Each tile compares the period with the period of the same length before it, up to the same time of day, and the line under the tiles names both. The badge is green when the figure moved the better way, red when it moved the worse way, and grey when it did not move or has no better way: cost, tokens and both averages are better when they go down, the cache hit rate when it goes up, and **runs with spend** is always grey. **All time** has nothing to compare with, so it shows no badges.

**Behind the totals** splits two figures:

- **Tokens by kind**: cache read, uncached input, cache write and output, with the reasoning tokens within the output.
- **How the cost was worked out**: estimated from list prices, and reported by the provider. Spend recorded before DAIV kept the two apart shows as **Unknown source**. A note under the panel names the tokens and models that have no price.

## Read the chart

**Spend over time** is a column per day (per week for 30 and 90 days, per month for all time), stacked by the five costliest models of the period, with the rest under **Other**. Point at a column, or move the keyboard focus to it, to see its cost per model, its runs and its cache hit rate. Select a column to open the sessions with spend in its days. The chart is hidden for **Today**.

## Read the breakdowns

Five tables rank the period's spend:

- **By person**: the person who started each run. A run a webhook started for someone with no DAIV account belongs to that git platform account, marked **external**. A deleted user keeps the name they had, marked **deleted user**; spend of users deleted before DAIV kept their name shows as **Unknown person**. **DAIV** holds work no person started: repository-wide background work and runs started for no one.
- **By repository**: the repository of each run or background task.
- **Started by**: the run's trigger type, then **Background** with a row per [background task](#background-spend). This table always shows every row.
- **By model**: each model, with its colour in the chart.
- **By provider**: the provider configured in DAIV that served the calls, by the name it had then, and how many models it served. Spend recorded before DAIV kept the provider shows as **Unknown provider**.

Each row shows the cost, its share of the total, the tokens, the runs and a cache tag: its prompt cache hit rate, green at 85% or more, grey from 70%, amber under 70%, or **cache not reported**. A cost with unpriced tokens ends in `+`, and a row with only unpriced tokens says **no price**.

The person, repository, model and provider tables show their top five rows and a **Rest** row that carries the remainder. **Show all** lists up to 100 rows in place, and past 100 the list links to the [CSV export](#export-spend-records) for the rest.

A row opens the sessions with spend matching it in the period, on **Everyone**. **Rest**, **DAIV**, **Memory consolidation**, a batch title's **No repository** row and **Unknown provider** have no single list of sessions; point at one to see why.

## Find the most expensive sessions

**Most expensive sessions** lists the ten sessions with the most run spend in the period, with their origin, repository, owner, models and runs. Background spend is left out, as on a session's own **Spend** section. **Sort sessions by cost** opens the sessions list sorted by the same figure for the same period, so the two lists start the same.

## What the figures leave out

A note at the end of the page appears when there is something to say: the models whose tokens have no price, the runs in the period that recorded no spend, and the runs still running, whose spend so far is included.

## Background spend

DAIV makes four kinds of model call outside a run, and counts each under **Started by → Background**:

| Task                     | When                                                                                                                  | Belongs to                                                                                                              |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| **Session titles**       | DAIV titles a session or a batch of runs                                                                              | The session's person and repository; a batch title has no session or repository and belongs to the batch's first person |
| **Run classification**   | DAIV classifies a finished run for notifications                                                                      | The run's person, session and repository                                                                                |
| **Memory extraction**    | DAIV extracts [repository memory](https://docs.daivagent.com/latest/features/memory/index.md) observations from a run | The run's person, session and repository                                                                                |
| **Memory consolidation** | DAIV consolidates a repository's observations                                                                         | The repository, under **DAIV**                                                                                          |

A retried task is charged again, and counts again.

## What is recorded and who can see it

A spend record holds who the spend belongs to (a DAIV user, a git platform account, or **DAIV**), the repository, session and run it served, the model and provider, and its tokens and cost. It never holds a prompt or a response.

Who can see a record follows [the scope](#choose-a-period-and-a-scope): a member sees the spend of every run they can open, including colleagues' runs on repositories they can read, by name, and can export it. Administrators see every record.

DAIV keeps spend records with no end date and never prunes them. A record outlives its run and its session, so past totals do not change, and a [deleted user's](https://docs.daivagent.com/latest/getting-started/accounts/#delete-a-user) records keep the name they had.

## Export spend records

**Download CSV** in the toolbar downloads every record behind the page, for its period and scope, as `daiv-spend-<start>-<end>.csv`, up to 10 downloads an hour per user. One row is one record: one model through one provider for one run, or for one background task call. Its `cost_usd` column adds up to the page's total cost.

| Column                                                                                                         | Meaning                                                                                                                  |
| -------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `occurred_at`                                                                                                  | When the record counts, in UTC                                                                                           |
| `kind`                                                                                                         | `run` or `background`                                                                                                    |
| `task`                                                                                                         | The background task, or empty                                                                                            |
| `trigger_type`                                                                                                 | The run's trigger type, or empty                                                                                         |
| `person`, `person_kind`                                                                                        | The DAIV username, the external username, the deleted user's name or `DAIV`; and `user`, `external`, `deleted` or `daiv` |
| `repo_id`, `session_id`, `run_id`, `schedule_id`                                                               | What the spend served; `run_id` is the jobs API's `job_id`                                                               |
| `model`, `provider`, `provider_name`                                                                           | The model, and the slug and name of the provider that served it                                                          |
| `input_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens`, `reasoning_tokens`, `total_tokens` | Tokens by kind; cache reads and writes are part of the input, reasoning part of the output                               |
| `cost_usd`                                                                                                     | The priced cost                                                                                                          |
| `reported_cost_usd`                                                                                            | The part the provider reported, or empty when the split is unknown                                                       |
| `unpriced_tokens`                                                                                              | Tokens with no price, which add nothing to the cost                                                                      |
| `cache_reported`                                                                                               | Whether the provider reported cache use; when `false`, a `cache_read_tokens` of 0 means unknown                          |
| `cache_saving_usd`, `cache_unpriced_tokens`                                                                    | The estimated saving from cache reads, and the cache-read tokens it could not price                                      |
| `cost_source`                                                                                                  | `reported`, `estimated`, `mixed`, `unknown` or `unpriced`                                                                |
| `final`                                                                                                        | `false` while the record's run is still running and its figures can still grow                                           |
| `updated_at`                                                                                                   | When the record last changed, in UTC                                                                                     |

## Read spend records through the API

`GET /api/spend/records` returns the same rows as JSON, for the user of the API key or the signed-in browser session. Send the key as `Authorization: Bearer <api-key>`, where `<api-key>` is an API key from your account.

```
curl -H "Authorization: Bearer <api-key>" "https://daiv.example.com/api/spend/records?from=2026-11-01&to=2026-11-30"
```

| Parameter | Required | Meaning                                      |
| --------- | -------- | -------------------------------------------- |
| `from`    | yes      | First local day, as `YYYY-MM-DD`             |
| `to`      | yes      | Last local day, at most 92 days after `from` |
| `scope`   | no       | `all` (the default) or `mine`                |
| `cursor`  | no       | The `next_cursor` of the previous page       |

A page holds up to 500 rows in `items`, oldest first, with the CSV's keys and `null` for an empty value. `next_cursor` is `null` on the last page; pass it back as `cursor` for the next one. Pages stay consistent while running runs rewrite their records, and a record that changes after you paged past it is not sent again: read a period again once its rows are `final` to get their last figures.

The API answers `400` with a `detail` for a missing or invalid date, `to` before `from`, a range over 92 days or a malformed cursor, `401` without valid credentials, and `429` past 60 requests a minute per user.
