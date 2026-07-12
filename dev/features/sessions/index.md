# Sessions

DAIV records every agent execution in a **Session**. A session is one agent thread — a persistent conversation context tied to a repository and ref. Every way you can trigger DAIV (webhooks, the Jobs API, the MCP endpoint, scheduled jobs, or a chat conversation you type yourself) produces a session, and every time the agent runs against that thread produces a **Run** inside it.

Navigate to **Dashboard > Sessions** (`/dashboard/sessions/`) to see the unified list. Clicking any session opens its detail page — the transcript, run timeline, and timing and usage for every execution.

What you can see

Admins see all sessions across every repository. Regular users see their own sessions, sessions matched to their git platform username (from webhook payloads), and sessions from [Scheduled Jobs](https://srtab.github.io/daiv/dev/features/scheduled-jobs/index.md) they subscribe to. The same scoping applies to the detail page, live updates, and retries.

______________________________________________________________________

## Sessions and Runs

A **Session** groups all runs that share the same agent thread:

- **Session** — the thread. Has a repository, ref, origin (how it was first triggered), title, and an optional link to an issue or merge request, schedule, or user.
- **Run** — one agent execution inside that session. A session can have many runs: retries, follow-up chat turns, and FIFO-queued API submissions all add new runs to the same session.

A chat conversation is a session where the origin is `Chat`; each turn you send creates a new Run. A webhook event (issue or MR) creates a session tied to the issue or merge request; the single agent invocation is its first Run.

______________________________________________________________________

## Origin types

Each session is tagged with the origin that created it:

| Origin            | Source                                                                                            | Example                                        |
| ----------------- | ------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| **Chat**          | Dashboard session workspace                                                                       | You type a prompt and stream a reply           |
| **API Run**       | [Jobs API](https://srtab.github.io/daiv/dev/features/jobs-api/index.md) `POST /api/jobs`          | A CI pipeline or script submits a prompt       |
| **MCP Run**       | [MCP Endpoint](https://srtab.github.io/daiv/dev/features/mcp-endpoint/index.md) `submit_job` tool | Claude Code or Cursor delegates a task         |
| **Scheduled Run** | [Scheduled Jobs](https://srtab.github.io/daiv/dev/features/scheduled-jobs/index.md) cron dispatch | A weekly dependency audit fires on Monday      |
| **UI Run**        | Dashboard run composer (see below)                                                                | You start a run from **Dashboard > Sessions**  |
| **Issue Webhook** | GitLab/GitHub issue event                                                                         | An issue is labelled `daiv`                    |
| **MR/PR Webhook** | GitLab/GitHub merge request event                                                                 | A reviewer mentions `@daiv` on a merge request |

______________________________________________________________________

## Sessions list

The sessions list shows all sessions in reverse chronological order. Each row displays:

- **Status indicator** — colour-coded dot (queued, pending, running, successful, failed) reflecting the latest run's state
- **Title** — the session title (generated automatically from the first prompt or issue/MR subject)
- **Repository** — which repository the agent operates on
- **Origin badge** — the session's origin (see above)
- **Timing and usage** — when the session was last active, and the total tokens and cost across all its runs

Queued runs

API and MCP runs that continue a thread already in flight don't start immediately. The new run is created in the **Queued** state and released in FIFO order once the prior run on that thread finishes.

### Filtering

Use the filter controls at the top of the list to narrow results:

| Filter          | Description                                                                                                                 |
| --------------- | --------------------------------------------------------------------------------------------------------------------------- |
| **Status**      | Queued, Pending, Running, Successful, or Failed                                                                             |
| **Origin type** | Chat, API Run, MCP Run, Scheduled Run, UI Run, Issue Webhook, or MR/PR Webhook                                              |
| **Repository**  | Search and select a specific repository                                                                                     |
| **Date range**  | From / To date pickers                                                                                                      |
| **Schedule**    | Pre-applied when navigating from a scheduled job's run count                                                                |
| **Batch**       | Pre-applied when viewing a multi-repo submission group (one prompt submitted across several repositories shares a batch ID) |

Filters are combined with AND logic and reflected in the URL query string, so filtered views can be bookmarked or shared.

### Live updates

Sessions with in-flight runs (Queued, Pending, or Running) update automatically via server-sent events. The status dot and timing update in real time without a page refresh.

______________________________________________________________________

## Session detail

Click any session to see its full detail page, which includes:

- **Origin and status badges**
- **Context** — repository, branch/ref, linked schedule or issue/MR (as a clickable link), the agent model and thinking level, and the sandbox environment. Admins viewing another user's session also see the owning user.
- **Transcript** — for chat-origin sessions, the full conversation transcript streams live during active runs and is replayed on return visits and mid-run refreshes — reopening the session rejoins the live stream and replays any events it missed (a fresh page load from the start of the run; an automatic reconnect only the events since it dropped).

For in-flight sessions the detail page updates in real time until the run completes.

______________________________________________________________________

## Chat sessions

Chat is the interactive dashboard workspace where you converse with the agent in real time. Go to **Dashboard > Sessions** and click **New session** (`/dashboard/sessions/new/`) to open the empty workspace.

Before the composer appears, pick a **repository** in the hero picker. Each session is bound to a repository and a ref:

- Selecting a repository defaults the ref to its **default branch**; you can switch to any other branch.
- If the chosen ref already has an open merge/pull request, the workspace surfaces it.

Once a repository is selected, the prompt box appears and you can type your first message. The session gets its own URL (`/dashboard/sessions/<thread_id>/`) so you can bookmark it or share it with teammates who have access.

A session targets one repository and ref

The repository and ref are fixed for the life of the session. To work against a different repository or branch, start a new session.

As the agent works, the workspace streams updates live:

- **Assistant text and reasoning** — replies render as Markdown; thinking/reasoning is shown in collapsible segments.
- **Tool calls** — each tool call appears as an expandable card with its status (running, done, error), arguments, and result.
- **Todos** — when the agent plans with a todo list, the side rail shows the list with a `done/total` count.
- **Files changed** — files the agent reads or edits are collected in the rail; click one to jump to the tool call that touched it.

While a run is streaming you can press **Stop** to cancel it server-side. Refreshing the page or losing the connection does not stop the run — reopening the session rejoins the live stream. Only one run can be in flight per session at a time.

### Model and sandbox environment

Each session pins three choices, set in the hero before your first message and then locked for the rest of the session:

| Choice                  | What it controls                                                                                                                     | Default                               |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------- |
| **Agent model**         | Which configured LLM the agent runs on                                                                                               | The admin-configured system default   |
| **Thinking level**      | Reasoning effort: Minimal, Low, Medium, High, or Extra high                                                                          | The admin-configured default (if any) |
| **Sandbox environment** | The named [Sandbox Environment](https://srtab.github.io/daiv/dev/features/sandbox-environments/index.md) the agent's commands run in | **Auto** — resolved per repository    |

These selections are fixed at session creation. To use a different model, effort level, or sandbox environment, start a new session.

### Merge request awareness

When the agent commits changes, DAIV creates or updates a merge/pull request on the session's ref. The workspace shows an MR/PR pill that links to it and flags drafts. A pre-existing open request for the ref is detected and shown even before the agent runs.

Expired state

Session state lives in the agent checkpointer and can expire. Opening an expired session shows an "expired" notice; start a fresh session to continue.

______________________________________________________________________

## Starting a background run from the dashboard

You can launch an agent background run without typing in the chat workspace. Click **Start a run** on the Sessions page (or open **Dashboard > Runs > New**) to reach the run composer.

The composer accepts:

- **Prompt** — what you want the agent to do
- **Repositories** — one or more repositories. Submitting one prompt across multiple repositories creates a [batch](#filtering): each repository runs as an independent session, and after submission you land on the batch-filtered Sessions list.
- **Ref** — the starting branch or commit each run reads from (defaults to the repository's default branch)
- **Sandbox environment** — the named [sandbox environment](https://srtab.github.io/daiv/dev/features/sandbox-environments/index.md) the run executes in
- **Agent model and thinking level** — per-run overrides (leave empty to inherit the repo defaults)
- **Notify me** — when to send a notification for this run

Runs started this way are tagged with the **UI Run** origin. A single-repository submission takes you straight to the session detail page; a multi-repository submission takes you to the batch-filtered list.

### Retrying a run

Any finished, non-webhook, non-chat run is **retryable**. Open the session detail page, find the run in the timeline, and click **Retry** to open the run composer pre-filled with the original run's prompt, repository, ref, and agent model/thinking level. Adjust anything before submitting — the retry is a fresh run appended to the same session.

______________________________________________________________________

## Result retention

Run records are permanent, but the underlying task result (which holds the full output and traceback) is subject to the task backend's retention policy. When the task result is pruned, the run still shows a denormalized summary and error message captured at completion time, along with the token usage and cost figures — these survive pruning.

______________________________________________________________________

## How it works

A Session is created at the point of dispatch — when a webhook callback, API view, MCP tool, or schedule dispatcher enqueues a job, or when you send your first message in a new chat workspace. The Run record stores the trigger type, prompt, and a link to the background task result; the session holds the shared thread context.

```
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

______________________________________________________________________

## Legacy URLs

Old bookmarks to `/dashboard/activity/<id>/` and `/dashboard/chat/<thread_id>/` redirect permanently to the corresponding Sessions URLs — no broken links.

______________________________________________________________________

## Related

- **Sandbox Environments**

  ______________________________________________________________________

  Configure the named environments the agent's commands run in, and how Auto resolution picks one.

  [Sandbox Environments](https://srtab.github.io/daiv/dev/features/sandbox-environments/index.md)

- **Scheduled Jobs**

  ______________________________________________________________________

  Recurring agent runs — each dispatch creates a session per repository.

  [Scheduled Jobs](https://srtab.github.io/daiv/dev/features/scheduled-jobs/index.md)

- **Jobs API**

  ______________________________________________________________________

  Submit runs programmatically; each job creates or continues a session.

  [Jobs API](https://srtab.github.io/daiv/dev/features/jobs-api/index.md)

- **Notifications**

  ______________________________________________________________________

  Know the moment a run finishes, via the in-app bell, email, or Rocket Chat.

  [Notifications](https://srtab.github.io/daiv/dev/features/notifications/index.md)
