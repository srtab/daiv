---
description: "Every DAIV agent execution is recorded as a session: chat with the agent live, start background runs, and review transcripts, timelines and usage."
---

# Sessions

DAIV records every agent execution in a **Session**. A session is one agent thread — a persistent conversation context tied to a repository and ref. Every way you can trigger DAIV (webhooks, the Jobs API, the MCP endpoint, scheduled jobs, or a chat conversation you type yourself) produces a session, and every time the agent runs against that thread produces a **Run** inside it.

Navigate to **Dashboard > Sessions** (`/dashboard/sessions/`) to see the unified list. Clicking any session opens its detail page — the transcript, run timeline, and timing and usage for every execution.

!!! info "What you can see"
    Admins see all sessions across every repository. Regular users see their own sessions, sessions started by a GitLab/GitHub account they signed in to DAIV with (matched by the account's id, not its username), and sessions from [Scheduled Jobs](scheduled-jobs.md) they subscribe to. The same scoping applies to the detail page, live updates, and retries.

---

## Sessions and Runs

A **Session** groups all runs that share the same agent thread:

- **Session** — the thread. Has a repository, ref, origin (how it was first triggered), title, and an optional link to an issue or merge request, schedule, or user.
- **Run** — one agent execution inside that session. A session can have many runs: retries, follow-up chat turns, and API submissions that continue the thread all add new runs to the same session.

A session can also span several repositories: a [Jobs API](jobs-api.md#multi-repository-sessions) or MCP request with `shared_session` opens one session over up to five repositories, with the first as the main one. The agent edits them together, each changed repository gets its own merge request, and the session page lists every repository and merge request. Only users who can read all of them (and admins) can see it.

A chat conversation is a session where the origin is `Chat`; each turn you send creates a new Run. A webhook event (issue or MR) creates a session tied to the issue or merge request; the single agent invocation is its first Run.

---

## Origin types

Each session is tagged with the origin that created it:

| Origin | Source | Example |
|--------|--------|---------|
| **Chat** | Dashboard session workspace | You type a prompt and stream a reply |
| **API Run** | [Jobs API](jobs-api.md) `POST /api/jobs` | A CI pipeline or script submits a prompt |
| **MCP Run** | [MCP Endpoint](mcp-endpoint.md) `submit_job` tool | Claude Code or Cursor delegates a task |
| **Scheduled Run** | [Scheduled Jobs](scheduled-jobs.md) cron dispatch | A weekly dependency audit fires on Monday |
| **UI Run** | Dashboard run composer (see below) | You start a run from **Dashboard > Sessions** |
| **Issue Webhook** | GitLab/GitHub issue event | An issue is labelled `daiv` |
| **MR/PR Webhook** | GitLab/GitHub merge request event | A reviewer mentions `@daiv` on a merge request |
| **Delegated Run** | A coordinator's `delegate_jobs` call ([Orchestration](orchestration.md)), including a leg follow-up that continues the session | One repository's part of a multi-repository change |

---

## Home

The Home page (`/dashboard/`) opens with **Needs you**: what is waiting on you, up to eight items. Questions come first, then failures (Failed, CI still failing, Can't run), then Found issues, then Needs attention; newest first within each.

- **Needs input** — your session's latest run asked you a question. It leaves when you answer.
- **Failed** — your session's latest run failed. It leaves once a newer run starts in the session (**Retry** starts one), once you run the same prompt on the same repository again, or 14 days after it failed; it stays in Sessions. A run you cancelled doesn't count.
- **CI still failing** — DAIV's CI watch on a merge/pull request gave up. Shown for 7 days.
- **Can't run** — one of your schedules can't start its runs. It leaves when the schedule starts a run again or you pause it.
- **Found issues** / **Needs attention** — your session's latest run was classified that way. Shown for 7 days, since a fix made outside DAIV leaves no trace it can see.

A session counts once, as the first of these it matches: a question in a session whose CI watch gave up shows as Needs input. A session with no activity for 90 days leaves the list.

Below it are your five most recent sessions, the ways to start work (issue labels, mentions and slash commands on your Git platform; this instance's MCP URL for your editor; Chat and Background run here), and **Your week**: what the runs in your own sessions came to over the last 7 days, a column a day stacked by outcome (changed code, done without changes, needed you, failed), the merge requests its runs worked on, and where you started work from. A day's column opens your sessions that ran that day. Team statistics are on Insights.

## Insights

**Insights** (`/dashboard/insights/`) shows how DAIV is used across the repositories you can see, over Today, 7 days (the default), 30 days, 90 days or all time.

- **Agent activity** — runs, success rate, runs that changed code and average duration, each compared with the period of the same length just before, up to the same time of day (the page names both). Running now is live and not compared.
- **Runs over time** — a column per day (weeks for 30 and 90 days, months for all time), stacked by outcome: changed code, done without changes, needs input, failed. A column opens the sessions active in its days. Hidden for Today.
- **Sessions by origin** — each origin's sessions this period and the period before, the change, and a small bar per day. Every count for this period opens the session list it counts; the period before and the bars count sessions with a run in those days, so later activity doesn't move them.
- **Code velocity** — see [Merge Metrics](merge-metrics.md).

## Sessions list

The sessions list shows all sessions in reverse chronological order. Each row displays:

- **Status indicator** — colour-coded dot (queued, pending, running, successful, failed) reflecting the latest run's state
- **Title** — the session title (generated automatically from the first prompt or issue/MR subject)
- **Repository** — which repository the agent operates on
- **Origin badge** — the session's origin (see above)
- **Timing and usage** — when the session was last active, and the total tokens and cost across all its runs

!!! note "Queued runs"
    An API or MCP job that continues a busy session doesn't start immediately: it joins the session's next run for its branch, shown as **Queued**, which starts once the current run finishes (see [The session inbox](#the-session-inbox)).

### Filtering

Use the filter controls at the top of the list to narrow results:

| Filter | Description |
|--------|-------------|
| **Status** | Queued, Pending, Running, Successful, or Failed |
| **Origin type** | Chat, API Run, MCP Run, Scheduled Run, UI Run, Issue Webhook, MR/PR Webhook, or Delegated Run |
| **Repository** | Search and select a specific repository |
| **Date range** | From / To date pickers |
| **Schedule** | Pre-applied when navigating from a scheduled job's run count |
| **Batch** | Pre-applied when viewing a multi-repo submission group (one prompt submitted across several repositories shares a batch ID) |
| **Merge request** | Pre-applied when arriving from the **view sessions** link in a merge request description. Always paired with a repository, since IIDs are per-project; clearing the repository clears it too |

Filters are combined with AND logic and reflected in the URL query string, so filtered views can be bookmarked or shared.

### Live updates

Sessions with in-flight runs (Queued, Pending, or Running) update automatically via server-sent events. The status dot and timing update in real time without a page refresh.

---

## Session detail

Click any session to see its full detail page, which includes:

- **Origin and status** — the session's title and status in the header, and who or what started it in the sidebar's **Info** tab.
- **Sidebar** — repositories and merge requests, CI, the environment, usage and outputs (see [The session page](#the-session-page)).
- **Transcript** — the full conversation transcript streams live during active runs, whatever started them (chat, job, webhook or schedule), and is replayed on return visits and mid-run refreshes — reopening the session rejoins the live stream and replays any events it missed (a fresh page load from the start of the run; an automatic reconnect only the events since it dropped).
- **Up next** — at the end of the conversation, what the session will do next, in order: each run still to start, as whom, with the requests that start it, then the updates the next run will be told (see [The session inbox](#the-session-inbox)). A CI failure or finished delegated batch DAIV won't act on says why, such as *not fixing: out of attempts*; a request DAIV couldn't handle shows in the conversation as a *not handled* line with the reason. A run DAIV started itself opens with the message it received, and **Show request** expands the full prompt the run got.

The detail page updates in place whenever the session changes — a run starting or ending, a message joining Up next, a CI result — without polling or reloading. An open page also streams a chat turn sent from another tab or by someone else. Without Redis (`DJANGO_REDIS_URL` unset), the page can't hear about changes, so it checks every few seconds while a run is in flight instead.

---

## Artifacts

A run's final message is a paragraph of text, and the agent's workspace is thrown away when the run ends. Tasks whose deliverable is a *document* — a dependency audit, a code-quality report, a changelog draft, a chart — need somewhere else to put it. That is what **artifacts** are for.

The agent has a `publish_artifact` tool: it writes the file to its scratchpad (`/workspace/tmp/...`), publishes it, and gets back a URL that it includes in its final response. DAIV copies the file out of the sandbox into its own storage and attaches it to the run, so it stays available after the sandbox is gone and without committing anything to the repository.

Published files appear inline in the transcript, as part of the turn that published them: adjacent `publish_artifact` calls in one turn group into a single **Artifacts** card, one row per file. Each row shows the title (linking to the viewer page, `/dashboard/sessions/<thread_id>/artifacts/<id>/`, opened in a new tab), a kind pill, the file size, and a download icon. A publish still in progress shows "Publishing…"; a publish that failed shows its path with a red × and the error message in red — in the same card as any files that did publish. Reloading the page renders the same rows, though publishes from consecutive model steps can land in separate cards. If a session has no archived transcript (its checkpoint expired before DAIV archived sessions), its artifacts still show on the session page, one card per run, below the expired notice.

What the viewer shows depends on the file type:

| File type | Viewer |
|-----------|--------|
| Markdown (`.md`) | Rendered server-side, sanitized, in the DAIV shell; **Copy source** copies the raw Markdown |
| HTML (`.html`) | Rendered inside a **sandboxed frame** — inline CSS and scripts run, but in an opaque origin with no access to DAIV cookies, storage or pages. Fetch/XHR and form posts are blocked; scripts, styles, images and fonts may still load over HTTPS. **Full screen** expands the frame |
| Images (`.png`, `.svg`, `.jpg`, `.gif`, `.webp`) | Displayed inline on a checkerboard, so transparency shows; click to open at full size |
| Text, CSV, JSON, XML, YAML, logs | Shown as preformatted text; **Copy source** copies it |
| Anything else (`.pdf`, archives, …) | Download only |

Markdown and text files over 1 MiB are not previewed; the viewer offers the download instead.

The viewer's header shows the artifact's kind, its repository, and a link back to the session run that produced it. **Open raw** serves the file as-is under the same sandbox policy, so opening an HTML report in its own tab is as safe as the embedded frame. Whoever can open the session can open its artifacts.

Navigate to **Dashboard > Artifacts** (`/dashboard/artifacts/`) for every artifact you can see across all sessions — your own sessions, plus sessions on repositories you can read (admins see every artifact) — newest first, grouped by date (Today, Yesterday, Previous 7 days, Previous 30 days, then by month), 25 per page. Search by title or filename, and filter by kind (All, Markdown, HTML, Image, Text, or File) and repository. Each row links to the viewer, back to the session and run that produced it, and to a download. The viewer's breadcrumb links back to this list.

Artifacts are also listed in the [Jobs API](jobs-api.md#poll-job-status) and [MCP](mcp-endpoint.md) job-status responses, with absolute viewer and download URLs, so a CI pipeline or an editor assistant can hand the report to a person.

!!! note "Limits and storage"
    A file is capped at `DAIV_ARTIFACT_MAX_BYTES` (default 10 MiB) and a run at `DAIV_ARTIFACTS_PER_RUN_MAX` files (default 20). Files live in Django's default file storage under `MEDIA_ROOT` (`/home/daiv/data/media` in the containers), which the `app` and `worker` containers must share through one volume — see the [deployment guide](../getting-started/deployment.md). Deleting a run deletes its artifacts and their files.

### Revising an artifact

Ask the agent to change a report it already published — "add a section on licenses", "make the table sortable" — and it revises the artifact instead of publishing a new one: it edits the file and publishes it again with the artifact's id, so the file is replaced and **the URL stays the same**. If the file is no longer in its workspace (an earlier run published it), the agent first copies it back with `fetch_artifact`. This works across runs of the same session, but never across sessions: the agent can only read or revise artifacts of the session it runs in. You can paste an artifact's URL into the conversation to point the agent at it.

A revised artifact stays on the run that first published it — on the dashboard, and in the per-run artifact cards of a session with no transcript. The Jobs API and MCP job-status responses list it under that run and under every run that revised it. Every transcript row for it shows its current title, type and size, rows are marked **Updated** once it has been revised, and the viewer's header shows when it was last updated. Only the latest version is kept; revisions do not count towards the per-run file limit.

---

## Chat sessions

Chat is the interactive dashboard workspace where you converse with the agent in real time. Go to **Dashboard > Sessions** and click **New session** (`/dashboard/sessions/new/`) to open the empty workspace.

Before your first message, set up the **Workspace** card in the hero: the repositories the session works on, and its sandbox environment. A session is bound to its repositories and a ref for each:

- Selecting a repository defaults the ref to its **default branch**; you can switch to any other branch.
- If the chosen ref already has an open merge/pull request, the workspace surfaces it.
- The ref is the branch the work is based on: when the agent opens a new merge/pull request, that ref is its **target branch**. If it has been deleted from the remote by the time the agent publishes, the request targets the default branch instead.
- The new merge/pull request is **assigned to you**, when your DAIV account is linked to the git platform through OAuth login.

Send stays disabled until a repository is selected; then type your first message. The session gets its own URL (`/dashboard/sessions/<thread_id>/`) so you can bookmark it or share it with teammates who have access.

!!! note "A session's repositories and refs are fixed"
    The repositories and their refs are fixed for the life of the session. To work against a different repository or branch, start a new session.

### Starting a chat over several repositories

The Workspace card takes up to 5 repositories, for work that spans them (a backend change and the client that consumes it). Use **Add repository** to add one, and the branch button on a row to pick its ref. Each repository can be listed once, on any branch.

- The first repository is the **main** one, tagged **Main** once there is more than one. Its `.daiv.yml`, sandbox environment, skills and memory drive the session. Use **Make main** on another row to promote it; removing the main row promotes the next one.
- The **Environment** field shows `Auto · <environment>` for the current main repository while it is on **Auto**.
- The list is fixed after your first message: later turns in the session reuse it, as in the [Jobs API](jobs-api.md#multi-repository-sessions).
- Every repository the agent changes gets its own merge/pull request, as described under [Merge requests](jobs-api.md#merge-requests).

As the agent works, the workspace streams updates live:

- **Assistant text** — replies render as Markdown.
- **Steps** — consecutive steps (thinking and tool calls) collapse into one line summarising what they did, such as "Read 3 files, ran 2 commands", with any failures and the lines added and removed. Open it to list the steps; each step opens to its details.
- **Finished runs** — when a run ends, its steps fold behind one **Worked for 3m 12s · 14 steps** line; the answer, its artifacts and any question the agent asked stay visible below it. An answered question collapses to one quiet line.
- **Todos** — when the agent plans with a todo list, the side rail shows the list with a `done/total` count.
- **Files changed** — files the agent reads or edits are collected in the rail; click one to jump to the tool call that touched it.

While a run is streaming you can press **Stop** to cancel it server-side. If DAIV hasn't started answering yet, Stop takes your message back: it leaves the conversation, the agent never sees it, and its text returns to the composer. Refreshing the page or losing the connection does not stop the run — reopening the session rejoins the live stream. Only one run can be in flight per session at a time.

A message you send while the session is busy is not refused: it shows as **Pending** and waits for the session's next run, which starts as soon as the current one ends and answers it together with anything else that arrived meanwhile. The page follows that run when it starts. See [The session inbox](#the-session-inbox).

Stop is offered to the session's owner, on chat runs and on the runs a job or the session's inbox started; a run started by a webhook on an issue or a merge request no session owns streams without it.

### Model and sandbox environment

Each session pins three choices, set before your first message (the environment in the Workspace card) and then locked for the rest of the session:

| Choice | What it controls | Default |
|--------|------------------|---------|
| **Agent model** | Which configured LLM the agent runs on | The admin-configured system default |
| **Thinking level** | Reasoning effort: Minimal, Low, Medium, High, or Extra high | The admin-configured default (if any) |
| **Sandbox environment** | The named [Sandbox Environment](sandbox-environments.md) the agent's commands run in | **Auto** — resolved for the main repository |

These selections are fixed at session creation. To use a different model, effort level, or sandbox environment, start a new session.

### The session page

The header carries only the session's title (rename it from the pencil when you can run agents on the repository), its status and a **⋯** menu with **Copy session link**, the linked issue and Archive. Everything about the work itself sits in the sidebar beside the transcript.

From 1100px wide the sidebar is always open. Below that it is a sheet, which a summary button in the header opens: the main repository, `+N` for the others, the merge request (`!iid`, or a count) and a CI dot. The sheet opens on the Overview tab, scrolled to the repositories with that section unfolded.

The sidebar has two tabs, and remembers the tab and which sections you folded in this browser:

- **Overview** — foldable sections, each with a one-line summary beside its name while folded:
    - **Repositories** (**Repository** for a lone one) — a card per repository.
    - **Environment** — the sandbox and the model.
    - **Context** and **Spend** — how full the model's context window is, and what the session has cost.
    - **Outputs** — the artifacts the session published, once there are any.
- **Info** — status, who started the session, its owner, the number of runs, when it was created and its **Session ID** (with a copy button), then, on a session that delegated work, the **Delegated jobs** of each batch (see [Orchestration](orchestration.md#seeing-the-batch)).

A repository card shows the repository, tagged **Main** when the session spans several; the branch; the merge request with its title and a **Draft** badge; the merge request's CI status, with **What DAIV found** when a run explained a failure; and the lines added and removed with the number of files changed. A repository where the agent ran but changed nothing shows as a quiet card marked *no changes*. A repository DAIV could not publish shows the reason on its card.

A card's **⋯** menu, offered once the repository has a merge request, holds:

- **Open on GitLab** (or **GitHub**) and **Copy link**
- **Other sessions on !iid**, on the main repository's card: the [sessions list](#filtering) filtered to that merge request
- **Fix CI failures**, for the people who can run agents on the repository: turns the [CI watch](pipeline-watch.md#the-session-page) on or off for that merge request

### Merge request awareness

When the agent commits changes, DAIV creates or updates a merge/pull request on the session's ref. The session's repository card links to it and flags drafts. A pre-existing open request for the ref is detected and shown even before the agent runs.

The request itself links back to the session that produced it — see [Linking back to sessions](#linking-back-to-sessions), which applies to every origin, not just chat.

!!! note "Expired state"
    The agent's working state lives in the checkpointer and expires (`DJANGO_REDIS_CHECKPOINT_TTL_MINUTES`), but DAIV archives each session when a run ends. An expired session keeps its transcript, and your next message picks up where it left off: DAIV restores the conversation from the archive first, and fails the run if it can't. Archived conversations are kept as long as their session exists; nothing prunes them automatically. A session with no archived transcript, such as one whose checkpoint expired before DAIV archived sessions, shows an "expired" notice; start a fresh session to continue.

### Answering a question

When the agent hits a request that's genuinely ambiguous or a decision that's yours to make, it stops and asks instead of guessing. The run ends in the amber **Needs input** status, and the last turn renders as a card with 1 to 4 questions, each with:

- **Option buttons** — click one to select it; a multi-select question is marked "Pick any that apply", and clicking toggles an option on or off alongside the others
- **A free-text field** — type your own answer if none of the options fit, or if the question has no options at all

**Answer** sends all of your answers as a single message, one line per question; any question you leave blank is sent as "No preference". **Skip** declines the whole card and tells the agent to use its best judgment. Either way the agent carries on with reasonable defaults for what you didn't answer, states the assumptions it made, and doesn't ask the same questions again. Both start a new run on the same thread that picks up where the agent left off, and you can also just type a reply in the composer.

The card is only interactive on the session's last turn, and only while no run is in flight — once answered it stays visible but goes inert (options disabled, free-text field and buttons hidden), keeps the options your answer picked highlighted, even after a reload, and your answer shows as the next message. This isn't chat-only: the same card renders on a webhook or job session's transcript wherever a run ended on a question.

For job runs (a dashboard job submission, the [Jobs API](jobs-api.md) or [MCP](mcp-endpoint.md)) and [scheduled](scheduled-jobs.md) runs, DAIV also sends a [notification](notifications.md) listing the questions, so you know the run is waiting on you. Chat runs don't notify, and webhook runs rely on the question comment on the issue or merge/pull request, which mentions you.

---

## The session inbox

Everything that asks an existing session to act arrives in its inbox: a CI result on a merge request the session published, a mention or a comment on that merge request, a chat message sent while the session was busy, a follow-up job, a delegated batch finishing, a coordinator's **Leg follow-up** asking a delegated session to continue its work. Each one either **wakes** the agent or is only **remembered**.

- **Waking** — a person's request (a chat message, a mention, a follow-up job) and a leg follow-up always wake the agent. A CI failure wakes it only on a commit DAIV pushed, once per commit, while attempts are left (see [Pipeline Watch](pipeline-watch.md)); a finished delegated batch wakes it to carry on.
- **Remembered** — a CI pass, a pipeline that needs a person, a push someone else made to the branch, a merge or close, a comment that doesn't mention DAIV. These start no run. They appear in the session's timeline as compact rows between runs, and the next run on the session gets them as context, so the agent knows the branch moved before it pushes.

One run handles everything one person is waiting for on the session's branch: a message you sent to your session and a CI failure that arrived during the same run are answered together, in one run, when the current one ends. A builtin slash command such as `/help` is the exception: it runs on its own, after what you sent before it and before what you sent after it, since DAIV answers a command only when it is the whole request. A leg follow-up also runs on its own, as a Delegated Run that answers only its coordinator, with what the session remembered as context; it is never retried, and it does not run when the session's merge request was merged or closed meanwhile (see [Orchestration](orchestration.md#following-up-a-leg)). Requests from different people wait for runs of their own, one after another, so one person's request never runs with another's access or tools. A message you sent while the session was busy, a follow-up job sent without a branch and a finished delegated batch follow it to the branch the current run publishes to; a mention or a CI result stays with its merge request's branch, and a run on another of the session's merge requests updates that merge request. A run started by a warm sandbox whose branch someone pushed to brings its checkout up to the remote first, unless it holds commits it hasn't published, and says so to the agent.

A run acts as the person whose request woke it: a mention by a teammate runs with the DAIV account they signed in to with that GitLab/GitHub account, and its personal MCP servers, not the session owner's. A commenter who never signed in to DAIV with that platform account, or whose DAIV account is deactivated, gets no personal tools, even when a DAIV user shares their username or email. Before it starts, a run checks that the person it acts as may still run on the repository, except for a mention, which is checked where it is made; a person who may not only has their own requests fail. A session an issue or merge request comment starts is owned the same way: by the DAIV account linked to its author's GitLab/GitHub account, else by no one until that account is linked, when its sessions and runs, the merge requests those sessions own and its requests still waiting pass to that user. A run DAIV woke for a CI failure acts as the session's owner, and one that resumes a finished delegated batch as the user who delegated it, muted when their run was.

DAIV stops waking a session on its own after three runs in a row that nobody asked for, and tells the owner; CI failures and finished delegated batches are still remembered, and sending a message or mentioning DAIV picks it up again.

---

## Linking back to sessions

This applies to every session origin — chat, webhooks, scheduled and API runs alike — wherever the agent commits.

When DAIV *opens* a merge/pull request, its description carries a **view sessions** link. It resolves to the Sessions list filtered to that request, so it covers every session that worked on it — the one that opened it and each later one that addressed a review comment — and keeps working as more are added. A thread that produced several requests cannot tell which one the reader came from, so the link falls back to the producing session's transcript rather than guessing; the same fallback applies before any request is known.

Every commit DAIV makes also carries the producing session as a git trailer:

```
DAIV-Session: https://daiv.example.com/dashboard/sessions/<thread_id>/
```

Unlike the description, the trailer is per-commit and append-only: it survives a rewritten description, reaches requests DAIV merely adopts (whose description is left untouched), and travels with the commit when it is squashed or cherry-picked. It joins any trailer block the commit message already ends with, so `Co-authored-by` and `Closes` keep working.

Admins can turn both off for the whole instance (**Settings > Agent > Link sessions from merge requests**) or per repository with `session_link: false` in `.daiv.yml`.

---

## Starting a background run from the dashboard

You can launch an agent background run without typing in the chat workspace. Click **Start a run** on the Sessions page (or open **Dashboard > Runs > New**) to reach the run composer.

The composer accepts:

- **Prompt** — what you want the agent to do
- **Repositories** — one or more repositories. Submitting one prompt across multiple repositories creates a [batch](#filtering): each repository runs as an independent session.
- **Ref** — the starting branch each run reads from, and the target branch of any merge/pull request it opens (defaults to the repository's default branch)
- **Sandbox environment** — the named [sandbox environment](sandbox-environments.md) the run executes in
- **Agent model and thinking level** — per-run overrides (leave empty to inherit the repo defaults)
- Notifications are automatic: DAIV notifies on notify-worthy outcomes (found-issues, needs-attention, needs-input, failed). To silence a run, pass `muted` via the [Jobs API](jobs-api.md) or [MCP endpoint](mcp-endpoint.md); for scheduled runs, use the schedule's Mute toggle.

Runs started this way are tagged with the **UI Run** origin. Every submission, single- or multi-repository, takes you to the batch-filtered Sessions list, where each new session has its own row.

### Retrying a run

When the latest run in a session fails, its red failure marker in the transcript has a **Retry** button. It sends your next message in the same session: *The previous run failed. Please try again.* when the run got far enough to be in the conversation, or the run's own prompt again when it failed before that. Only the newest run offers it, and not while a run is working or queued. It shows for chat turns and for API, MCP, UI and scheduled runs; webhook runs and runs delegated by another session report back through their own flow, so they don't offer it. You need to own the session and be able to run agents on its repository, and it must not be archived. A run that was stopped (by you, by the session that delegated it, or by an interruption) shows a grey marker without **Retry**; send a message to carry on.

Every run shown in the transcript also has a link target on the session page, including the runs of a session whose transcript has expired: `<session URL>#run-<run id>` opens the session scrolled to that run. Each row of the **Artifacts** list uses it.

---

## Result retention

Run records are permanent, but the underlying task result (which holds the full output and traceback) is subject to the task backend's retention policy. When the task result is pruned, the run still shows a denormalized summary and error message captured at completion time, along with the token usage and cost figures — these survive pruning.

---

## How it works

A Session is created at the point of dispatch — when a webhook callback, API view, MCP tool, or schedule dispatcher enqueues a job, or when you send your first message in a new chat workspace. The Run record stores the trigger type, prompt, and a link to the background task result; the session holds the shared thread context.

```mermaid
sequenceDiagram
    participant Source as Trigger Source
    participant Handler as Callback / API / Dispatcher
    participant DB as Database
    participant Worker as Job Worker

    Source->>Handler: Event (webhook, API call, cron tick, chat prompt)
    Handler->>DB: Upsert Session (thread_id → repo, ref, origin)
    Handler->>Worker: Enqueue job task
    Handler->>DB: Create Run (status: Pending)
    Worker->>Worker: Execute agent
    Worker->>DB: Update task result (Running → Successful/Failed)
    Worker->>DB: Sync Run (status, timing, result, usage)
```

The `Run` model denormalizes key fields (status, timestamps, result summary, error message, token usage, cost) from the linked task result. This ensures the run record remains useful even after the task result row is pruned by the retention policy.

---

## Related pages

<div class="grid cards" markdown>

-   :octicons-container-24: **Sandbox Environments**

    ---

    Configure the named environments the agent's commands run in, and how Auto resolution picks one.

    [:octicons-arrow-right-24: Sandbox Environments](sandbox-environments.md)

-   :octicons-clock-24: **Scheduled Jobs**

    ---

    Recurring agent runs — each dispatch creates a session per repository.

    [:octicons-arrow-right-24: Scheduled Jobs](scheduled-jobs.md)

-   :octicons-terminal-24: **Jobs API**

    ---

    Submit runs programmatically; each job creates or continues a session.

    [:octicons-arrow-right-24: Jobs API](jobs-api.md)

-   :octicons-bell-24: **Notifications**

    ---

    Know the moment a run finishes, in the app, by email, or in Rocket Chat.

    [:octicons-arrow-right-24: Notifications](notifications.md)

</div>
