# Quickstart: validating Spend Visibility

How to prove the feature works, end to end. What each step checks is defined in [spec.md](spec.md), [contracts/](contracts/) and [data-model.md](data-model.md); this page only says how to check it.

## Prerequisites

- This checkout's stack is running: `make up` (`preview_start "daiv"`). In a worktree it is the worktree's own stack (CONTRIBUTING.md, "Worktree stacks").
- Migrations are applied, including the backfill (data-model, Migrations).
- A provider is configured whose model has a price (any Anthropic or OpenAI model in genai-prices). A second, unpriced model is optional, for step 4.
- Two accounts: an admin, and a member who can read repository A but not repository B.

## 1. Unit tests (the CI gate)

```bash
make test
```

The feature's own files (paths in [plan.md](plan.md), Project Structure) can run alone:

```bash
uv run pytest tests/unit_tests/sessions/test_spend_recording.py tests/unit_tests/sessions/test_spend_ledger.py tests/unit_tests/dashboard/test_spend.py -n auto
```

Expected: all pass. A test that reverts its change fails (constitution IV).

## 2. Spend is written while a run runs, and survives its end (FR-001a)

1. As the admin, start a chat on repository A that needs several model calls (for example, "read the README and summarise the three largest modules").
2. While it runs, reload **Sessions**. The row's cost grows between reloads.
3. Start a second chat and stop it from the composer after two or three tool calls. Its cost stays on the row, and **Spend** on the session's **Overview** shows it.
4. Start a third chat, then kill the worker container mid-run (`docker compose kill` on the slot's worker service). After the worker restarts and the run is marked failed, its spend is still on the row.

Expected: three sessions with a cost, none showing "nothing recorded".

## 3. Background spend is recorded (FR-001, SC-004)

After step 2's first run has finished and been classified, open **Insights → Spend** for **Today** as the admin.

**Started by** shows a **Background** row, with indented **Session titles**, **Run classification** and, if memory is on for the repository, **Memory extraction**. Each has tokens and a cost, and appears within a minute of its task finishing.

## 4. The page adds up (User Story 1, FR-005 to FR-010, SC-002)

1. On **Spend**, for **Today**, check that each of the five breakdowns sums to the summary's total cost. Include the **Rest** and **DAIV** rows.
2. Follow a repository row. The sessions list opens with `scope=all`, `spend_from`, `spend_to` and `spend_repo`, and lists the sessions of step 2.
3. Optional: run a chat on the unpriced model. The total gains a `+`, and the coverage note names the model.

## 5. Providers, caching and the page's details (FR-003a, FR-005, FR-005a, FR-006, FR-007)

Compare each check with the [mockup](https://claude.ai/artifact/EYxfkU58T7b3FodUYzw5Sd).

1. **By provider.** It shows the provider that served step 2's runs, under its configured name. Rename that provider in **Configuration**, then reload: the row keeps the old name.
2. **Cache hit rate.** The hit-rate tile equals cache-read tokens divided by all input tokens in the **Tokens by kind** panel. Each breakdown row has a cache tag, coloured by the thresholds in the [page contract](contracts/spend-page.md). Under the tile, a saving line is shown.
3. **Change badges.** For **7 days**, with runs in both weeks, a rise in cost shows a red badge and a rise in cache hit rate a green one. **Runs with spend** stays grey.
4. **Behind the totals.** The parts of each panel add up to its total. The output row gives the reasoning tokens within it.
5. **Hover card.** Point at a chart column for **30 days**, then tab to it. Each time the card lists the column's cost per model, its runs and its cache hit rate, and opens on the side with room.
6. **Show all.** With more than five people, or a seeded database, **Show all** replaces **Rest** with the remaining rows in place, and **Show the top 5 only** folds them back.

## 6. Members see only what they may (User Story 2, FR-011 to FR-013, SC-006)

1. As the admin, run one chat on repository B.
2. Sign in as the member and open **Spend**. It opens on **Mine** and shows nothing they didn't start.
3. Switch to **Everyone**. Repository A's spend appears, and repository B is in no table, name, top-sessions row, CSV row or API row.

## 7. Export and API (User Story 5, FR-016, FR-017)

1. On **Spend**, choose **Download CSV**. Sum its `cost_usd` column and compare it with the page's total for the same period and scope:

   ```bash
   python -c "import csv,sys,decimal; print(sum(decimal.Decimal(r['cost_usd']) for r in csv.DictReader(open(sys.argv[1]))))" daiv-spend-*.csv
   ```

   They match.
2. With the member's API key, page through the API until `next_cursor` is `null`:

   ```bash
   curl -s -H "Authorization: Bearer $DAIV_API_KEY" "http://localhost:8000/api/spend/records?from=2026-11-01&to=2026-11-30"
   ```

   The rows equal the member's CSV for the same range. A range over 92 days returns 400.

## 8. Sessions list

1. **Sort** → **Cost**. Rows are in descending cost order, and sessions with no priced spend come last. From **Spend**, follow **Sort sessions by cost**: the list keeps the period and starts with the same sessions, in the same order, as **Most expensive sessions**.
2. Filter by **Person**, by **Model** and by **Provider**. Only sessions with spend by that person, on that model or through that provider remain.

## 9. The 2-second target (SC-003)

This is not part of `make test`.

1. Seed a local database with 100,000 runs and about 500,000 spend records over 90 days, across 5 providers, 40 people and 60 repositories. Use a throwaway script from the scratchpad that calls `bulk_create`; it isn't committed.
2. Time the page as the admin with a valid session cookie:

   ```bash
   curl -s -o /dev/null -w "%{time_total}\n" -b "sessionid=$SESSION" "http://localhost:8000/dashboard/insights/spend/?period=30d&scope=all"
   ```

Expected: under 2 seconds, warm, on a laptop-class machine. Record the figure in the pull request.
