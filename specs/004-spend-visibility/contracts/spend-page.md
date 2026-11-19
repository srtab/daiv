# Contract: the Spend page and the sessions-list additions

The UI surfaces this feature adds or changes. Field meanings are in [data-model.md](../data-model.md). The [Spend page mockup](https://claude.ai/artifact/EYxfkU58T7b3FodUYzw5Sd) shows this contract with sample data.

## `GET /dashboard/insights/spend/` (`insights_spend`)

Signed-in users. HTML, `dashboard/insights_spend.html`, extending `base_app.html`. It shares **Insights**' header, with a segmented nav: **Activity** (`insights`) and **Spend** (`insights_spend`).

| Query param | Values | Default | Invalid value |
|---|---|---|---|
| `period` | `today`, `7d`, `30d`, `90d`, `all` | `30d` | falls back to the default |
| `scope` | `mine`, `all` | `all` for admins, `mine` for members | falls back to the default |

### Sections, in order

1. **Summary**: six tiles, three per row, one per row on a phone.
   - **The six figures**: total cost, total tokens, prompt cache hit rate, runs with spend, average cost per run, and average cost per run that changed code.
   - **Floor marker**: the total cost carries a `+` in `status-found` when unpriced tokens, or runs that recorded nothing, are in the window.
   - **Cache saving**: the hit-rate tile adds a line in `status-clear`, "saved about $X against no caching", with `+` when the saving is a floor.
   - **Change badge** (`_change_badge.html`): each tile's change against the previous window is a pill showing the arrow and the amount, followed by "was …" in `text-faint`. Its tone says whether the move is good:

     | Tile | Better when it goes |
     |---|---|
     | Total cost, tokens, average cost per run | down |
     | Cost per run that changed code | down |
     | Prompt cache hit rate | up |
     | Runs with spend | neither: always neutral |

     Good is `status-clear`, bad is `status-fail`, neutral is `text-muted`, each on a weak tint. **All time** has no previous window, so it shows no badge.
   - **Comparison note**: one line under the tiles names both windows.
2. **Behind the totals**: two panels side by side, stacked on a phone. Each has a title, its total, a one-sentence explanation, a split bar and one row per part. A row shows a swatch, a label, the amount, the share and a one-line meaning.
   - **Tokens by kind**: cache read, uncached input, cache write and output. The output row's meaning gives the reasoning tokens within it. The three input kinds share one hue in different shades; output has its own.
   - **How the cost was worked out**: estimated from list prices, and reported by the provider. A last row is "unknown source", shown only when backfilled rows are in the window. An amber note gives the unpriced tokens and names their models.
3. **Spend over time**: `_stacked_bars.html`, one column per bucket (`Window.bucket`). Segments go to the five costliest models in the window, and the rest to **Other**. The legend shows each model's total. The chart is hidden for **Today**.
   - **Link**: each column links to the sessions list with `spend_from`/`spend_to` set to the column's days.
   - **Hover card**: pointing at a column, or focusing its link, shows a card (`role="tooltip"`, linked by `aria-describedby`) with:
     - the bucket's label and total;
     - one line per model in legend order, with its swatch, cost and share;
     - the bucket's run count and prompt cache hit rate;
     - "Click to open these sessions".

     The card opens to the right of columns in the first half of the chart and to the left of the rest. It is server-rendered and shown by Alpine on `mouseenter`/`focus`, and hidden on `mouseleave`/`blur`.
4. **Breakdowns**: five tables, **By person**, **By repository**, **Started by**, **By model** and **By provider**, ranked by cost.
   - **Row**: name, cost, a share bar, then a meta line with the share, tokens and runs, and a cache tag.
   - **Cache tag**: "N% cache hits", coloured `status-clear` at 85% or more, `text-muted` from 70% to 84%, and `status-found` under 70%. With no reported records the tag reads "cache not reported" in `text-faint`.
   - **Rows by table**:
     - **Started by** lists trigger types and then **Background**, with one indented row per `SpendTask`. It always shows in full.
     - **By person** includes the **DAIV** row. A deleted user shows their snapshot name and "deleted user". An external account shows its platform and "external".
     - **By provider** shows each provider's stored name, a note on how it was reached ("direct API", or "cost reported by OpenRouter"), and how many models it served. Backfilled rows with no provider group under **Unknown provider**.
     - **By model** shows the chart's swatch beside each model that has one.
   - **Folded**: the other four tables show their top 5 rows and a **Rest (N {people|repositories|models|providers})** row that carries the remainder, so each table adds up to the total.
   - **Show all**: a full-width button under a folded table, "Show all N {people|repositories|models|providers}", replaces **Rest** with rows 6–100 in place. "Show the top 5 only" folds them back. Past 100 rows the list ends with "N more not shown". With the export in place it reads "N more in the CSV export", linked to `insights_spend_export`.
   - **Links**: each row links to the sessions list with `scope=all`, the window's `spend_from`/`spend_to`, and that row's `spend_*` param. **Rest**, the **DAIV** row, **Memory consolidation**, a batch title's row and **Unknown provider** have no link, and show a tooltip saying why.
   - **Legend line**: one line under the tables defines "cache hits" and the three colours.
5. **Most expensive sessions**: the ten sessions with the most run spend in the window (background spend excluded, as on the session's own **Spend** section). Each row shows the session's origin as a pill, its title (linking to `session_detail`), repository, owner, models and runs in the window, and its run spend in the window. "Sort sessions by cost" links to the sessions list with `sort=cost&scope=all` and the window's `spend_from`/`spend_to`, which lists the same sessions first, in the same order.
6. **Coverage note**: shown only when there is something to say:
   - "The total is a floor: tokens from … have no price";
   - "N runs in this period recorded no spend";
   - "M runs are still running; their spend so far is included".

   It ends with a link to the docs on how DAIV counts spend.
7. **Actions**: **Download CSV** in the toolbar (→ `insights_spend_export` with the same `period` and `scope`).

**Empty window**: "Nothing was spent in this period", with no tiles, panels, chart or tables (spec User Story 1, scenario 6).

**Visibility**: every number is computed over `SpendRecord.objects.visible_to(user)`, narrowed to `started_by(user)` under `scope=mine`. A member never sees a row, name or repository outside that set (FR-013).

## `GET /dashboard/insights/spend/export.csv` (`insights_spend_export`)

Signed-in users. Takes the same `period` and `scope`. Returns `text/csv; charset=utf-8`, streamed, as `daiv-spend-<start>-<end>.csv`. Its columns are in [spend-export-api.md](spend-export-api.md). The cost column sums to the page's total cost for the same params (SC-002).

## Sessions list (`session_list`) additions

### Filters (`SessionFilter`)

| Param | Value | Matches |
|---|---|---|
| `spend_from` | date | records with `occurred_at` ≥ local midnight of the date |
| `spend_to` | date | records with `occurred_at` < local midnight of the next day |
| `spend_person` | `u<user id>`, `x<provider>:<uid>`, or `daiv` | records with that person |
| `spend_repo` | repository id | records on that repository |
| `spend_trigger` | a `SessionOrigin` value | run-spend records of that trigger type |
| `spend_task` | a `SpendTask` value | background records of that task |
| `spend_model` | model name | records on that model |
| `spend_provider` | provider slug | records served by that provider |

When any `spend_*` param is present, all of them combine into one `Exists(SpendRecord(session=OuterRef("pk"), …))`. The list then holds exactly the sessions with a record matching every given param.

- **Chips.** Each param shows as a clearable chip: the person by name, the repository by path, the provider by its stored name.
- **Widgets.** The filter bar gains **Person**, **Model** and **Provider** selects, which set `spend_person`, `spend_model` and `spend_provider` with no dates (FR-015). Their options are the distinct people, models and providers in `SpendRecord.objects.visible_to(user)`.
- **Scope.** The `spend_*` params join `DEEP_LINK_PARAMS`, so a link carrying them opens on **Everyone**.

### Sort

| Param | Values | Default |
|---|---|---|
| `sort` | `recent`, `cost` | `recent` (by `last_active_at`, as today) |

`cost` orders by the session's run spend: the `cost_usd` sum of its run-spend records, within `spend_from`/`spend_to` when given and over all time otherwise. It sorts highest first, with sessions that have no priced spend last. Each row's cost shows this same figure under either sort. A **Sort** control in the filter bar sets it. Pagination keeps the param.
