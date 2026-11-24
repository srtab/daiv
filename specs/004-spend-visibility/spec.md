# Feature Specification: Spend Visibility

**Feature Branch**: `claude/daiv-token-spending-visibility-4a845f`

**Created**: 2026-11-18

**Status**: Approved on 2026-11-19, with refinements SR1–SR6 and the mockup review

**Input**: User description: "Now i don't have any vision on how, who and where the tokens/credits are being spent on DAIV. How are other players showing and organising it?". What DAIV already has, found while drafting:

- Every agent run already stores its input, output and total tokens, an estimated cost in US dollars, and the same figures per model, including cache and reasoning tokens. A call through OpenRouter carries the charge OpenRouter reported; any other call is priced from public list prices, and a model with no list price leaves the cost empty.
- Each run also records who started it (a DAIV user, or the git platform account behind a webhook), its repository, what started it (its trigger type: chat, issue, merge request, pipeline, schedule, API, MCP, delegation), its model, its session and, through the session, its schedule.
- Those figures are shown one session at a time: the session's **Spend** section, a cost on each row of the sessions list, the jobs API, the MCP tools and the batch notification. Nothing adds them up across sessions. **Insights** counts runs, outcomes and merges, but not spend.
- A run's spend is written only when the run finishes cleanly. A run that fails, is cancelled or loses its worker records nothing, however many tokens it used.
- Model calls DAIV makes outside a run are not recorded at all: session titles, run classification, and repository memory extraction and consolidation.
- DAIV has no spend limit or budget of any kind.

## How other products show spend

Surveyed on 2026-11-18 from public docs: Claude Code, the Anthropic Console, Cursor, GitHub Copilot, Devin, OpenAI, OpenRouter, Amp, GitLab Duo Agent Platform, Factory, LangSmith and Langfuse.

| Pattern | Who does it | What it means for DAIV |
|---|---|---|
| Tokens split into input, cache read, cache write and output, plus a dollar figure | Almost all; Copilot since mid-2026 | DAIV records these per model already; aggregate them, don't add a unit |
| Dollar figure labelled as an estimate from a price table, with the authoritative figure elsewhere | Claude Code, Anthropic Console, OpenRouter for own keys | DAIV prices from list prices or the provider's reported charge, so it must say which |
| Abstract credits (ACUs, AI credits, premium requests) | Devin, Copilot, GitLab, Codex | A vendor billing device; DAIV bills no one, so dollars are the honest unit |
| Same ladder of surfaces: cost on each session, a personal view, an admin dashboard, CSV, an API | Claude Code, Devin, Amp, GitLab, OpenRouter | DAIV has the first rung only |
| Admin dashboard: summary cards, spend per day stacked by model, ranked tables by user, model and project, top-N list | Anthropic Console, Amp, GitLab, OpenRouter, Langfuse | The core of this spec |
| Grouping by user or actor, model, project or key, day; then product, feature and session | Anthropic Admin API, Amp, OpenAI | DAIV's equivalents: person, model, repository, day; then trigger and session |
| Automated usage marked apart from people, charged to the organisation | GitLab ("automated flow"), Anthropic (service-account actors) | DAIV's trigger dimension separates schedules, webhooks and delegations |
| Drill-down from an aggregate to the session behind it | GitLab, Devin, Amp | Every breakdown row links to the sessions list |
| Budgets nested org → group → user, alerts at 50/80/100% that never block, separate hard stops, "request more" at the limit | Cursor, Copilot, Devin, GitLab, OpenAI, OpenRouter | Provider limits apply per API key and cannot see DAIV's users or repositories, so any control DAIV wants it must enforce itself |
| Operator-editable price table, never repricing history | LangSmith, Claude Code, Langfuse | Out of scope here; see Assumptions |
| Cost tied to outcomes (merged PRs, lines shipped) | Claude Code, Cursor | DAIV opens the merge requests itself, so it can tie cost to outcomes without guessing |

No surveyed product attributes spend to the git repository and trigger that caused it. Those are the dimensions a provider console cannot see, and the reason DAIV, not the provider, has to be the source of truth.

## Terms

- **Spend**: the tokens a model call used and what they cost. Tokens are counted by kind: input, cached input read, cache write, output and reasoning.
- **Cost**: spend in US dollars. It is **reported** when the provider said what it charged for the call, and **estimated** when DAIV priced it from list prices.
- **Unpriced spend**: tokens of a model that has no list price and no reported charge. They count toward tokens but add nothing to cost, so any total that includes them is a floor.
- **Run spend**: the spend of every model call inside one agent run, including fallback models, subagents and models that tools call. Recorded today.
- **Background spend**: the spend of model calls DAIV makes outside a run, on behalf of a session or repository: session titles, run classification, and memory extraction and consolidation.
- **Dimension**: what spend is grouped by: person, repository, trigger type (what started the run), model, provider, session, schedule, or background task.
- **Provider**: the LLM provider configured in DAIV that served a model call, such as Anthropic, OpenAI, OpenRouter or a self-hosted OpenAI-compatible server. One model can be served by more than one provider.
- **Prompt cache hit rate**: the share of a scope's input tokens the provider served from its prompt cache, that is cache-read tokens divided by all input tokens. It is weighted by tokens, not by requests, because tokens are what is billed.
- **Cache saving**: the estimated cost the cache hits avoided: for each model, its cache-read tokens times the difference between its input price and its cache-read price, from list prices.
- **Person**: the DAIV user who started the run. For a run a webhook started on behalf of someone with no DAIV account, the git platform account that triggered it. A schedule's runs belong to the schedule's owner. Background spend belongs to the person of the run or session it served, and to **DAIV** when it served only a repository. **DAIV** also holds runs no person started, such as a CI fix on a session nobody owns.

## Clarifications

### Session 2026-11-18

- Q: Should this feature include spend controls (budgets, alerts, hard limits), or only visibility? → A: Visibility only. Controls are a later spec, built on figures people have learned to trust. Recorded in the Assumptions; the former User Story 6 is removed.
- Q: Who may see spend on runs other people started? → A: The existing run visibility. A member sees spend on every run they can already open, including other people's runs on repositories they can read, broken down by person; administrators see everything. Recorded in FR-012.
- Q: Must the spend of a run that fails, is cancelled, or loses its worker still be counted? → A: Yes. Spend is recorded as each model call finishes, so a run that ends any way keeps the spend of every call that finished, and runs still going show their spend so far. Recorded in FR-001a, the Edge Cases and Key Entities.
- Q: In the by-person breakdown, should background spend go to the person whose session or run caused it, or to a separate DAIV row? → A: To that person when it served a run or session; repository-wide work, such as memory consolidation, goes to a "DAIV (background)" row. Recorded in Terms, FR-001 and FR-007. (SR2 later renamed the row **DAIV**.)
- Q: Should every signed-in person be able to export the spend rows they can see, as CSV and through the API, or only administrators? → A: Everyone, each limited to the rows they can see on the spend view. Recorded in User Story 5 and FR-016.

## Design reference

The [Spend page mockup](https://claude.ai/artifact/EYxfkU58T7b3FodUYzw5Sd) (sample data, DAIV's theme) is the agreed layout, reviewed on 2026-11-18. It settled these points, now in the requirements:

- **Summary as tiles**, three per row. Each change against the previous period is a coloured badge that says whether the move is better or worse, not only up or down (FR-005).
- **The prompt cache hit rate is a headline figure**, with the estimated cache saving under it, and it appears again on every breakdown row (FR-005, FR-007).
- **"Behind the totals"** replaces two dense lines of figures with two panels. Each has a split bar and a labelled, explained row per part: tokens by kind, and cost by how it was worked out (FR-005a).
- **Pointing at a chart column** shows that bucket's cost per model, its runs and its cache hit rate (FR-006).
- **A fifth breakdown, by provider**, because one model can come from several providers, and each provider sends its own invoice (FR-007, SC-005).
- **Every breakdown expands in place** to its full list, instead of stopping at its top rows and a **Rest** row (FR-007).

Refinements SR1–SR6 of the [research](research.md#spec-refinements-for-approval), which the mockup shows (the **DAIV** row, the **Started by** table and the **Insights** tab), were approved on 2026-11-19 and are applied in this spec.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - An administrator sees where the money went this month (Priority: P1)

An administrator opens the spend view and picks **30 days**. They see:
- the total cost, the tokens and the prompt cache hit rate for the period, each marked better or worse against the 30 days before;
- how much of the total is estimated or unpriced;
- a chart of spend per week, stacked by model. Pointing at a week shows its cost per model;
- five ranked tables splitting the same total by person, repository, trigger, model and provider. Each row shows its cost, tokens, share of the total, runs and cache hit rate.

The administrator sees that one repository's scheduled code reviews account for 40% of the month, with a cache hit rate well below the rest. They click that row and land on that repository's sessions, filtered to the period.

**Why this priority**: This is the question the request asks: how, who and where. The figures are already recorded on every run; without an aggregate view, the only way to answer is to open sessions one by one or query the database. Every product surveyed leads with this view.

**Independent Test**: Seed runs across three people, two repositories, three trigger types and two models over 60 days, with known costs. Open the view for 30 days as an administrator. Every total, share and change equals the sum computed by hand from the seeded rows, and every linked row opens a sessions list holding exactly the sessions with spend counted in that row.

**Acceptance Scenarios**:

1. **Given** runs in the period, **When** an administrator opens the spend view, **Then** it shows total cost, total tokens, the prompt cache hit rate with its estimated saving, runs with spend, average cost per run and average cost per run that changed code. It also shows each figure's change against the previous period of the same length, coloured by whether the change is better or worse.
2. **Given** runs with several models, **When** the view loads, **Then** a chart shows spend per day (per week for 30 and 90 days, per month for all time), stacked by model.
3. **Given** the period's runs, **When** the view loads, **Then** it ranks spend by person, by repository, by trigger, by model and by provider. Each row shows cost, tokens, share of the total, run count and prompt cache hit rate.
4. **Given** a row in any breakdown, **When** the administrator follows it, **Then** the sessions list opens filtered to that dimension's value and the period.
5. **Given** some runs used a model with no price, **When** the view loads, **Then** the total is marked as a floor, the share of tokens that is unpriced is shown, and the unpriced models are named.
6. **Given** a period with no runs, **When** the view loads, **Then** it says nothing was spent in the period rather than showing empty charts.
7. **Given** a chart bucket, **When** the administrator points at it or moves keyboard focus to it, **Then** it shows the bucket's cost per model with shares, its run count and its prompt cache hit rate.
8. **Given** a breakdown with more rows than it shows, **When** the administrator chooses to show all, **Then** every row of that breakdown appears in place, and the administrator can fold it back.
9. **Given** the same model was served by two providers, **When** the view loads, **Then** the by-model table shows it once and the by-provider table splits it between the two.

---

### User Story 2 - A member sees what their own work costs (Priority: P1)

A member opens the same view and sees their own spend by default: their total for the period, the trend, and the split by repository, trigger and model. They can switch to the repositories they can see, as the sessions list already lets them switch from **Mine** to **Everyone**.

**Why this priority**: People who start runs control most of the spend: which model they pick, how long a chat runs, which schedules they own. They cannot change what they cannot see. Every surveyed product gives individuals a view of their own usage.

**Independent Test**: Seed runs for two members on one shared repository and one private repository. Sign in as the first member; the default view shows only their runs. Switch scope; the view shows runs on repositories that member can read, and nothing from the repository they cannot read.

**Acceptance Scenarios**:

1. **Given** a signed-in member, **When** they open the spend view, **Then** it shows only the runs they started, and says so.
2. **Given** a member switches scope, **When** the view reloads, **Then** it covers the runs they may already see in the sessions list, and no others.
3. **Given** a member switches scope, **When** the view shows the by-person breakdown, **Then** it follows the visibility rule in FR-012.

---

### User Story 3 - Anyone finds the most expensive sessions (Priority: P2)

From the spend view, a person sees the ten most expensive sessions in the period, each with its title, repository, owner, origin, model, run count and cost. In the sessions list, they can sort by cost and filter by person and by model, so an outlier found in a breakdown leads straight to the sessions behind it.

**Why this priority**: Aggregates say where spend concentrates; a list of sessions says why. Most cost surprises are a few long or looping sessions, and today finding them means scanning the list by eye.

**Independent Test**: Seed sessions with known costs, open the spend view and confirm the ten listed are the ten most expensive visible sessions, in order. Follow **Sort sessions by cost**, which keeps the period, and confirm the sessions list shows the same sessions first, in the same order.

**Acceptance Scenarios**:

1. **Given** sessions in the period, **When** the spend view loads, **Then** it lists the ten most expensive sessions the viewer can see, highest first, each linking to the session.
2. **Given** the sessions list, **When** a person sorts by cost, **Then** sessions are ordered by their run spend, within the spend period when one is set and over all time otherwise, with sessions that have no priced spend last. Each row shows the figure it is sorted by.
3. **Given** the sessions list, **When** a person filters by a person, a model or a provider, **Then** only sessions with spend by that person, on that model or through that provider remain.

---

### User Story 4 - Spend outside runs is counted (Priority: P2)

An administrator compares DAIV's monthly total with the provider's invoice. The spend view now includes background spend as one more trigger value, split by task (titles, classification, memory), and attributed to the session or repository it served. The gap between DAIV's figure and the invoice narrows to what estimates cannot know.

**Why this priority**: Background calls run after every run; leaving them out makes every total low by an unknown amount, and hides a cost an administrator can change by picking a cheaper model for those tasks.

**Independent Test**: Run a session to completion so that its title, classification and memory extraction happen. The spend view's **Background** row shows one entry per task with tokens and cost, attributed to that session's repository; the session's own **Spend** section is unchanged.

**Acceptance Scenarios**:

1. **Given** DAIV generates a session title, classifies a run, or extracts or consolidates memory, **When** the call ends, **Then** its tokens and cost are recorded with the task, the model, and the session or repository it served.
2. **Given** recorded background spend, **When** the spend view loads, **Then** background spend appears as one more trigger value, can be split by task, and is included in the totals.
3. **Given** a background call fails partway, **When** it is recorded, **Then** whatever tokens the provider reported are kept.

---

### User Story 5 - Spend rows are exported for chargeback (Priority: P3)

An administrator downloads the period's spend as a CSV file, one row per run or background call, with date, person, repository, trigger type, session, schedule, model, provider, tokens by kind, cost and whether the cost is reported, estimated or unpriced. The same rows are available through the API for a reporting tool to pull.

**Why this priority**: Teams that split the bill by team or client need the raw rows in their own spreadsheet or BI tool. Aggregates on a page cannot serve every grouping they need.

**Independent Test**: Export a seeded period; the file has one row per run and background call in the period, and its cost column sums to the view's total.

**Acceptance Scenarios**:

1. **Given** the spend view for a period, **When** an administrator exports, **Then** they receive a CSV of every row in the period and scope they are viewing.
2. **Given** a member viewing the runs they can see, **When** they export, **Then** the CSV holds only those runs and the background spend that follows them, and nothing from runs they cannot open.
3. **Given** an API key, **When** a client requests spend rows for a date range, **Then** it receives the rows its user's export would hold, paginated.

---

### Edge Cases

- A run's cost is empty because its model has no price: its tokens count, its cost counts as zero in sums, and every total that includes it is marked as a floor.
- A run recorded nothing (it failed before any model call, or predates usage recording): it counts as a run with no spend and is named in the coverage note.
- A run fails, is cancelled or loses its worker partway: it keeps the spend of every call that finished, and counts in every total like any other run.
- A model name was recorded doubled by a streaming bug in older rows: it groups with the repaired name, as the session **Spend** section already does.
- A run's person is a git platform account with no DAIV user: it appears under that account's name, marked as external, and is never merged with a DAIV user of the same name.
- A DAIV user is deactivated or deleted: their past spend stays, under their name or as a deleted user.
- A session's runs span the period boundary: each run counts in the period it was created in, so a session can appear in two periods.
- A session ran on several repositories: its runs count under the main repository, which is how runs record it today.
- Runs still running when the view loads: the view includes the spend they have recorded so far, and says how many runs are in progress.
- A delegated run: it counts under its own session, the delegation trigger and its person. The delegating session's **Spend** section keeps showing only that session's own runs.
- Very large periods (all time on a busy site): the view loads within the target in SC-003.
- A provider is renamed or deleted: its past spend stays under the name it had when the spend was recorded.
- A provider reports no cache tokens (some self-hosted servers): its rows show the cache hit rate as not reported, never as 0%, and it adds nothing to the site-wide rate.
- A model's price list has no cache-read price: its cache hits count toward the hit rate but add nothing to the cache saving, and the saving is marked as a floor.
- A breakdown has hundreds of rows: showing all expands it to its first 100 rows and points to the CSV export for the rest.

## Requirements *(mandatory)*

### Functional Requirements

**Recording**

- **FR-001**: The system MUST record background spend for every model call DAIV makes outside a run: session titling, run classification, memory extraction and memory consolidation. Each record holds the task, the model, tokens by kind, cost and how it was priced, the time, the run, session or repository it served, and the person it belongs to (see Terms).
- **FR-001a**: The system MUST record a run's spend as each of its model calls finishes, not only when the run ends. A run that fails, is cancelled or loses its worker keeps the spend of every call that finished before it stopped; only calls that finished in the moment before a worker was killed, before their write completed, may be lost.
- **FR-002**: Recording spend MUST NOT fail or delay the work it measures. A recording failure is logged at ERROR and the work continues.
- **FR-003**: The system MUST keep, for each run and background record, whether its cost was reported by the provider, estimated from list prices, or unpriced.
- **FR-003a**: The system MUST keep, for each run and background record, the provider that served it, by the name the provider had then, and its estimated cache saving, priced when the call is recorded.

**The spend view**

- **FR-004**: The system MUST provide a spend view for signed-in users, reached as a **Spend** tab beside **Activity** on **Insights**, which Home's **Team insights** link opens, offering the same periods as **Insights**: today, 7 days, 30 days, 90 days and all time.
- **FR-005**: For the period, the view MUST show total cost, total tokens, the prompt cache hit rate with the estimated cache saving under it, runs with spend, average cost per run and average cost per run that changed code. Both averages divide run spend, not background spend, by the runs that recorded spend. Runs that recorded nothing are counted in the coverage note instead. Each is compared with the previous period of the same length, cut at the same time of day.
  - The change says whether it is better or worse: more cost, more tokens or a higher cost per run is worse; a higher cache hit rate or a lower cost per run is better; more runs is neutral.
- **FR-005a**: Below the summary, the view MUST break the period's figures down in two panels:
  - tokens by kind: uncached input, cache read, cache write and output, with the reasoning share of output;
  - cost by how it was worked out: reported by the provider, or estimated from list prices, with the unpriced tokens and their models named.

  Each part shows its amount, its share and one line saying what it means.
- **FR-006**: The view MUST show spend over time, one bucket per day for periods up to 7 days, per week for 30 and 90 days, and per month for all time, stacked by model. Pointing at a bucket, or moving keyboard focus to it, MUST show its cost per model with shares, its run count and its prompt cache hit rate.
- **FR-007**: The view MUST rank spend by person, repository, trigger type (what started the run, with background spend as one more value, split by task), model and provider.
  - **Rows**: each shows cost, tokens, share of the period's total, run count and prompt cache hit rate. The hit rate is marked good at 85% or more, neutral from 70% to 84%, and needing attention under 70%.
  - **Showing all**: a breakdown shows its top rows and a **Rest** row with the remainder, and can expand in place to every row, up to the limit in the Edge Cases.
  - **Person rows**: a person's row includes the background spend that belongs to them, and repository-wide background spend has its own **DAIV** row, which also holds runs no person started. Every breakdown adds up to the same total.
- **FR-008**: Each row in a breakdown, and each bucket in the chart, MUST link to the sessions list filtered to that value and the period, so the list holds exactly the sessions with spend counted in that figure. A row with no session behind it (the **DAIV** row, memory consolidation, a batch title, the **Rest** row) has no link, and says why.
- **FR-009**: The view MUST list the ten sessions with the most run spend in the period that the viewer can see, and link to the sessions list sorted the same way for the same period.
- **FR-010**: The view MUST state how much of the total is reported, estimated and unpriced, name the models without a price, and mark every total that includes unpriced tokens or runs with nothing recorded as a floor. It MUST label estimated costs as estimates.
- **FR-011**: The view MUST open on the viewer's own spend for members and on the whole site for administrators, and MUST let members switch to the runs they can see in the sessions list.
- **FR-012**: Visibility of spend MUST follow the existing run visibility: a member sees spend on every run they can already open (their own, and other people's on repositories they can read), including the by-person breakdown over those runs. Administrators see every run. Background spend follows the visibility of the session or repository it served; background spend tied to neither is visible to its person and administrators only.
- **FR-013**: A member MUST NOT see spend, names or repository names from runs they cannot see in the sessions list.

**The sessions list**

- **FR-014**: The sessions list MUST allow sorting by cost: each session's run spend, within the spend period when the list is filtered to one and over all time otherwise, with sessions that have no priced spend last. A row shows the cost it is sorted by.
- **FR-015**: The sessions list MUST allow filtering by person, by model and by provider, in addition to the filters it has today.

**Export**

- **FR-016**: Every signed-in user MUST be able to export the rows behind the view for the selected period and scope as CSV, limited to the rows they can see under FR-012, one row per run and per background record, with every dimension and figure in FR-001, FR-003 and FR-003a.
- **FR-017**: The same rows MUST be available through DAIV's API to API-key holders, filtered by date range and limited to what the key's user may see, paginated.

**Documentation**

- **FR-018**: The user documentation MUST describe the spend view, the export, the API, what each figure includes and excludes, and how far estimates can be from a provider's bill.

### Key Entities

- **Run spend**: already stored on each run: tokens, cost and the per-model split. It gains how its cost was priced (FR-003), and its provider and cache saving (FR-003a). It is written as calls finish rather than once at the end (FR-001a).
- **Background spend record**: one model call, or one task's calls, outside a run: task, model, provider, tokens by kind, cost, pricing source, cache saving, time, the run, session or repository it served, and the person it belongs to, if any.
- **Spend breakdown**: a derived view, not stored: spend summed over runs and background records by one dimension for one period and one scope.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An administrator can answer "which person, repository, trigger, model and provider spent the most last month, and where caching is weakest" from one page, without opening a session or querying the database.
- **SC-002**: For any period, the view's total equals the sum of its export's cost column, and the sum of each breakdown's rows equals the total.
- **SC-003**: The spend view loads in under 2 seconds for 30 days of activity on a site with 100,000 runs.
- **SC-004**: Background spend for every task in FR-001 appears in the view within one minute of the task finishing.
- **SC-005**: On a deployment that pays list prices and whose models all have prices, each provider row's 30-day total is within 10% of that provider's bill for the same API key, with the remaining difference explained by the estimate note in the docs.
- **SC-006**: No member sees a figure, name or repository from a run they cannot open, verified across every breakdown, the top-sessions list, the export and the API.

## Assumptions

- Spend controls are out of scope: no budgets, alerts or limits. Provider limits apply per API key and cannot see DAIV's people or repositories, so a later spec for controls will have DAIV enforce them itself, on the figures this feature records.
- The view lives on its own page beside **Insights**, sharing its period picker and its previous-period comparison, rather than adding sections to **Insights** itself.
- Cost stays in US dollars, as DAIV records it. No currency conversion, and no abstract credit unit: DAIV does not bill anyone, so dollars from the provider's price list are the honest unit.
- Spend is attributed to the run's person, main repository and trigger type as each run already records them. No new team or cost-centre entity; teams group by repository or person in the export.
- Breakdowns inside a run (by subagent, tool or step) are out of scope. LangSmith already provides them, and the monitoring docs point there.
- Setting prices for models that have no list price (self-hosted models), or contracted rates below list price, is out of scope. The view names unpriced models so an operator knows the total is a floor.
- The cache saving is an estimate from list prices, like every estimated cost. It is never computed from a provider's reported charge, which does not say what an uncached call would have cost.
- Cost per merged merge request is out of scope. Cost per run that changed code (FR-005) is the outcome measure for now.
- Existing runs keep the figures they have. Runs recorded before FR-003 are shown as "estimated or reported" where the source cannot be told apart. No backfill of background spend.
- Platform neutrality holds without extra work: nothing in this feature calls the git platform.
- Runs whose provider reports no usage (a known gap with some streamed OpenAI-compatible servers) show as recording nothing; fixing that recording is a separate change.
