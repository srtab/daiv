# Sessions

DAIV records every agent execution in a **session**: one persistent conversation with the agent, tied to a repository and ref. Every way you can trigger DAIV (webhooks, the jobs API, the MCP endpoint, scheduled jobs, or a chat you type yourself) produces a session, and every time the agent runs in that session adds a **run** to it.

Your own sessions are listed in the [app sidebar](#the-app-sidebar). The search icon beside its **Sessions** heading opens the full list (`/dashboard/sessions/`), with every session you can see. A session's page holds its transcript, its run timeline, and its timing and usage.

## Who can see a session

Admins see every session on every repository. Other users see their own sessions, sessions on repositories they can read on the Git platform, the sessions started by a GitLab or GitHub account they signed in to DAIV with (matched by the account's id, not its username), and the sessions of [scheduled jobs](https://docs.daivagent.com/dev/features/scheduled-jobs/index.md) they subscribe to. The same scoping applies to a session's page, its live updates and its retries.

## Sessions and runs

A session is the conversation. It has a repository, a ref, an origin (how it was first triggered), a title, and an optional link to an issue, a merge request, a schedule or a user.

A run is one agent execution inside a session. Retries, chat turns and API submissions that continue the session each add a run to the same session.

A run starts as **Queued** (waiting for the session's current run to end) or **Pending**, then becomes **Running**, and ends as **Done**, **Needs input** or **Failed**.

A run's record, with its token usage and cost, stays after DAIV deletes its full result: [Poll job status](https://docs.daivagent.com/dev/features/jobs-api/#poll-job-status) describes what a finished run's `result` holds by then.

A chat is a session whose origin is **Chat**, and each message you send is a run. A webhook event on an issue or merge request creates a session tied to it, and the single agent invocation is its first run.

A session can also span several repositories. A [jobs API](https://docs.daivagent.com/dev/features/jobs-api/#multi-repository-sessions) or MCP request with `shared_session` opens one session over up to 5 repositories, the first being the main one. The agent edits them together, each changed repository gets its own merge request, and the session page lists every repository and merge request. Only admins and users who can read all of its repositories can see such a session.

### Session origins

Each session carries the origin that created it:

| Origin                | Source                                                                                                                                                                       | Example                                                                    |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| **Chat**              | The dashboard chat                                                                                                                                                           | You type a prompt and read the reply as it streams                         |
| **API Run**           | [Jobs API](https://docs.daivagent.com/dev/features/jobs-api/index.md) `POST /api/jobs`                                                                                       | A CI pipeline or script submits a prompt                                   |
| **MCP Run**           | [MCP endpoint](https://docs.daivagent.com/dev/features/mcp-endpoint/index.md) `submit_job` tool                                                                              | Claude Code or Cursor delegates a task                                     |
| **Scheduled Run**     | [Scheduled jobs](https://docs.daivagent.com/dev/features/scheduled-jobs/index.md) dispatch                                                                                   | A weekly dependency audit fires on Monday                                  |
| **UI Run**            | The dashboard's [background run](#start-a-background-run) form                                                                                                               | You start a run from the dashboard                                         |
| **Issue Webhook**     | A GitLab or GitHub issue event                                                                                                                                               | An issue is labelled `daiv`                                                |
| **MR/PR Webhook**     | A GitLab or GitHub merge request event                                                                                                                                       | A reviewer mentions `@daiv` on a merge request                             |
| **Pipeline Webhook**  | A CI result that DAIV's [pipeline watch](https://docs.daivagent.com/dev/features/pipeline-watch/index.md) acts on                                                            | DAIV starts a run to fix a failed pipeline on a merge request it published |
| **Delegated Run**     | A coordinator's `delegate_jobs` call ([orchestration](https://docs.daivagent.com/dev/features/orchestration/index.md)), including a leg follow-up that continues the session | One repository's part of a multi-repository change                         |
| **Delegation Resume** | The run that resumes a coordinator once its delegated batch has finished ([orchestration](https://docs.daivagent.com/dev/features/orchestration/index.md))                   | The coordinator checks every leg's outcome and reports                     |

## The app sidebar

The sidebar lists your sessions, so any of them is one click away from every page:

- **Sessions**: your own sessions, most recently active first, 8 at a time. **Show more** adds 8, up to 80; past that it opens the sessions list. Each has a status dot and, while it is running, waiting for your input or failed, that state; otherwise how long ago it was active. The session you are viewing is always in the list. Sessions a schedule started are listed under their schedule instead. Beside the heading, the search icon (shortcut `/`) opens the sessions list with its search box focused, and **+** starts a chat.
- **Scheduled**: up to 10 schedules you own or subscribe to that are enabled or ran in the last 14 days, each with its next run. Expand one for its 3 latest runs: one row per run of the schedule, with the worst status across its repositories and, past one, how many repositories it ran on. A run on one repository opens its session; a wider one opens the sessions it started. **All runs** lists every session of the schedule, and the list icon beside the heading opens the schedules page. A schedule whose latest run failed or is waiting for input starts expanded; otherwise this browser remembers whether you expanded it.
- **Settings**, the cog beside your account, slides in the pages you set up once (**Sandbox environments**, **MCP servers** and **Memory**, under **Agent setup**, and for admins **Users**, **Configuration**, **Schedule templates**, **Skills** and **Global MCP servers**, under **Admin**) along with the documentation links. Nothing changes on the page until you pick one. **Back** (or Escape) slides your sessions back in.

The lists update as runs start and finish, without a reload. Where the sidebar narrows to icons, **+** starts a chat, the magnifier opens the session search, and **Sessions** and **Scheduled** open their pages.

To see more of long titles, drag the sidebar's right edge to widen it, or focus the edge and use the arrow keys; double-click the edge to reset it. This browser remembers the width. The sidebar never takes so much room that the page beside it gets cramped, so on smaller windows it widens less.

## Home

**Home** (`/dashboard/`) opens with **Needs you**: what is waiting on you, up to 8 items. Questions come first, then failures (**Failed**, **CI still failing**, **Can't run**), then **Found issues**, then **Needs attention**; newest first within each.

- **Needs input**: your session's latest run asked you a question. The item leaves when you answer.
- **Failed**: your session's latest run failed. The item leaves once a newer run starts in the session (**Retry** starts one), once you run the same prompt on the same repository again, or 14 days after the failure. The session stays in **Sessions**. A run you cancelled does not count.
- **CI still failing**: DAIV's CI watch on a merge request gave up. The item shows for 7 days.
- **Can't run**: one of your schedules cannot start its runs. The item leaves when the schedule starts a run again or you pause it.
- **Found issues** and **Needs attention**: your session's latest run was classified that way. The item shows for 7 days, because a fix made outside DAIV leaves no trace DAIV can see.

A session counts once, as the first of these it matches: a question in a session whose CI watch gave up shows as **Needs input**. A session with no activity for 90 days leaves the list.

Below **Needs you**, Home shows **Your week**: what the runs in your own sessions came to over the last 7 days. A column per day is stacked by outcome (**Changed code**, **Done, no changes**, **Needed you**, **Failed**), and the section lists the merge requests those runs worked on and where you started work from. A day's column opens your sessions that ran that day. Your recent sessions are in the [app sidebar](#the-app-sidebar).

Last comes **Ways to use DAIV**: how to start work from your Git platform (issue labels, mentions and slash commands), from your editor (this instance's MCP URL), and right here (**New chat** and **New background run**). Fold it away with **Hide**; this browser remembers. A user who has not started a run yet sees only **Needs you** and **Ways to use DAIV**.

Team statistics are on **Insights**, linked as **Team insights** at the top of Home.

## Insights

**Insights** (`/dashboard/insights/`) shows how DAIV is used across the repositories you can see. Choose a period: **Today**, **7 days** (the default), **30 days**, **90 days** or **All time**.

- **Agent activity**: runs, success rate, runs that changed code and average duration, each compared with the period of the same length immediately before, up to the same time of day (the page names both periods). **Running now** is live and not compared.
- **Runs over time**: a column per day (per week for 30 and 90 days, per month for all time), stacked by outcome: changed code, done without changes, needs input, failed. A column opens the sessions active in its days. The chart is hidden for **Today**.
- **Sessions by origin**: each origin's sessions this period and the period before, the change, and a small bar per day. Every count for this period opens the session list it counts. The previous period and the bars count sessions with a run in those days, so later activity does not move them.
- **Code velocity**: see [merge metrics](https://docs.daivagent.com/dev/features/merge-metrics/index.md).

## Sessions list

**Sessions** lists sessions, most recently active first. Each row shows:

- A status dot for the latest run (queued, pending, running, done, needs input or failed).
- The title, generated from the first prompt or the issue or merge request subject.
- The repository, the branch, and the merge request when there is one.
- An icon for the session's [origin](#session-origins).
- The latest run's model, its duration and the session's cost, then the owner and when the session was last active.

An API or MCP job that continues a busy session does not start at once. It joins the session's next run for its branch, shown as **Queued**, which starts when the current run ends ([The session inbox](#the-session-inbox) describes how).

The list shows your own sessions (**Mine**) or everyone's you can see (**Everyone**). A link that carries a repository, merge request, schedule or batch filter opens on **Everyone**.

### Filter the list

| Filter       | What it does                                                                                                                  |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| Search       | Matches a session's title or repository                                                                                       |
| Status       | **All**, **Needs input**, **Failed**, **Running**, **Queued**, **Pending** or **Done**, by the session's latest run           |
| **Type**     | One [origin](#session-origins), or **All types**                                                                              |
| **Time**     | **All time** (the default), **Today**, **Last 2 days**, **Last 7 days**, **Last 30 days**, or a **Custom range** of two dates |
| **Archived** | Swaps the list for the [archived sessions](#archived-sessions)                                                                |

Six more filters have no control. They appear as chips when you arrive from a link, and each chip has a button that clears it:

| Chip                   | Where the link comes from                                                                                                                                                                                                                                                                                                                                                                             |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Repository**         | A repository filter; clearing it clears the merge request too                                                                                                                                                                                                                                                                                                                                         |
| **Merge request**      | The **view sessions** link in a merge request description, or a repository card's menu; always paired with a repository, since merge request numbers are per project                                                                                                                                                                                                                                  |
| **Ran on**             | A day's column in **Your week** on **Home** or in **Runs over time** on **Insights**                                                                                                                                                                                                                                                                                                                  |
| **Schedule**           | The run count on a scheduled job                                                                                                                                                                                                                                                                                                                                                                      |
| **Batch**              | A multi-repository submission: one prompt sent to several repositories shares a batch                                                                                                                                                                                                                                                                                                                 |
| **Including archived** | A link that must show sessions whatever their state: the **view sessions** link in a merge request description, **Other sessions on !N** in a repository card's menu, a day in **Your week** on **Home** and its link to more merge requests, and a column in **Runs over time** or a count in **Sessions by origin** on **Insights**. The list then includes [archived sessions](#archived-sessions) |

Filters combine, and they are in the URL, so you can bookmark or share a filtered list.

Sessions with a run in flight (**Queued**, **Pending** or **Running**) update in place, status dot and timing included, without a reload.

### Archived sessions

An archived session is archived for everyone, not only for the person who archived it. It leaves **Sessions**, the app sidebar and **Needs you** on **Home**, and it takes no follow-ups until someone unarchives it.

A filtered list that hides archived sessions says how many match: select **Show archived** under the list to see them.

Links that open the list with the **Including archived** chip are listed in [Filter the list](#filter-the-list). **Your week** and **Insights** count archived sessions too.

DAIV archives a session when its merge request merges. It archives every session that carries the merge request's number, ran on it or owns it: the session that opened it, its review and comment sessions, and the issue sessions that published it. DAIV holds a session back while it has live work:

- A session that owns another open merge request stays listed, and DAIV archives it when that merge request merges.
- A session with a run in progress or a waiting request that would start a run is archived when that work ends.
- A session that delegated work is archived once its legs have no run in progress and no open merge request.

Closing a merge request without merging does not archive the sessions on it. DAIV handles each merge once: a session you unarchive stays unarchived if the platform sends the same merge again.

An archived session refuses new work:

- A chat message and a job that continues it with `thread_id` are refused, with a message that says to unarchive the session first.
- A leg follow-up for it is refused, and one already waiting stops with *not run: its session was archived*; see [Following up a leg](https://docs.daivagent.com/dev/features/orchestration/#following-up-a-leg).
- A label or mention on its issue, a mention on its merge request, or a mention on a merge request it owns starts nothing. DAIV replies with a link to the session, once per mention and at most once a day for a label.
- Requests already waiting in its [inbox](#the-session-inbox), and a CI failure or finished delegated batch that arrives, wait there until someone unarchives the session.

#### Archive a session

You need to be able to run agents on the session's repository. Archiving hides the session for everyone else, so check that nobody needs it first.

1. Open the session.
1. Select **⋯** → **Archive**.
1. In **Archive this session?**, select **Archive**.

#### Unarchive a session

You need the same access. The session comes back for everyone.

1. Open the archived session.
1. Select **Unarchive** beside **This session is archived.**, or **⋯** → **Unarchive**.

## Session detail

Select a session to open its page (`/dashboard/sessions/<thread_id>/`), which holds:

- **Header**: the session's title (rename it with the pencil, when you can run agents on the repository), its status, and a **⋯** menu with **Copy session link**, the linked issue and, when you can run agents on the repository, **Archive** or **Unarchive** ([archived sessions](#archived-sessions)).
- **Transcript**: the conversation, streaming live while a run is active, whatever started it (chat, job, webhook or schedule). Reopening the page rejoins the stream: a fresh load replays the run from its start, and an automatic reconnect replays only what you missed.
- **Up next**: at the end of the conversation, what the session does next, in order. It lists each run still to start, as whom, with the requests that start it, then the updates the next run is told ([The session inbox](#the-session-inbox) describes them). A CI failure or finished delegated batch that DAIV does not act on says why, such as `not fixing: out of attempts`. A request DAIV could not handle shows in the conversation as a `not handled` line with the reason. A run DAIV started itself opens with the message it received, and **Show request** expands the full prompt the run got.
- **Sidebar**: repositories and merge requests, CI, the environment, usage and outputs.

The page updates in place whenever the session changes: a run starting or ending, a message joining **Up next**, a CI result. A page open in another tab, or watched by someone else, streams a chat turn you send. Without Redis (`DJANGO_REDIS_URL` unset), the page cannot hear about changes, so it checks every few seconds while a run is in flight.

Every run in the transcript has a link target, `<session URL>#run-<run id>`, that opens the session scrolled to that run. This includes the runs of a session whose transcript has [expired](#expired-sessions). Each row of the **Artifacts** list and the artifact viewer's header use it.

### The sidebar

From 1100px wide the sidebar is always open. Below that it is a sheet, which a summary button in the header opens. The button shows the main repository, `+N` for the others, the merge request (`!iid`, or a count) and a CI dot. The sheet opens on **Overview**, scrolled to the repositories with that section unfolded.

The sidebar has two tabs, and your browser remembers the tab and which sections you folded:

- **Overview**: foldable sections, each with a one-line summary beside its name while folded.
  - **Repositories** (**Repository** for a lone one): a card per repository.
  - **Environment**: the **Sandbox** and the **Model**.
  - **Context** and **Spend**: how full the model's context window is, and what the session has cost.
  - **Outputs**: the artifacts the session published, once there are any.
- **Info**: status, who started the session, its owner, the number of runs, when it was created and its **Session ID** (with a copy button). On a session that delegated work, it also lists the **Delegated jobs** of each batch ([Seeing the batch](https://docs.daivagent.com/dev/features/orchestration/#seeing-the-batch) describes them).

A repository card shows:

- The repository, tagged **Main** when the session spans several.
- The branch.
- The merge request, with its title and a **Draft** badge.
- The merge request's CI status, with **What DAIV found** when a run explained a failure.
- The lines added and removed, and the number of files changed.

A repository where the agent ran but changed nothing shows as a quiet card marked `no changes`. A repository DAIV could not publish shows the reason on its card.

Once a repository has a merge request, the card's **⋯** menu offers:

- **Open on GitLab** (or **GitHub**) and **Copy link**.
- **Other sessions on !iid**, on the main repository's card: the [sessions list](#filter-the-list) filtered to that merge request.
- **Fix CI failures**, for the people who can run agents on the repository: turns the [CI watch](https://docs.daivagent.com/dev/features/pipeline-watch/#turn-the-watch-on-or-off-for-a-merge-request) on or off for that merge request.

When the agent commits changes, DAIV creates or updates a merge request on the session's ref, and the repository card links to it and flags drafts. A repository card shows an open merge request that already exists for the ref even before the agent runs.

### Images and files in tool results

When a tool hands the agent an image or a file (a screenshot from an MCP browser tool, or an image, PDF, audio or video file the agent opened with `read_file`), the tool's row in the transcript shows it under **Attached**, both while the run streams and after a reload.

- Images show as thumbnails that open full size in a new tab.
- PDFs, audio and video show as a chip with their size, which downloads the file.
- An image an MCP server only gives as a link to another site shows as a link, so viewing the transcript loads nothing from that site.

Anyone who can see the session can open these files, as they can read the rest of its transcript: its owner, people who can read its repository, and admins. That includes screenshots from MCP servers a person connected with their own account, so a screenshot of a page they were signed in to is visible to everyone who can see the session.

DAIV keeps these files with the run in its file storage under `MEDIA_ROOT` for as long as it keeps the run. DAIV does not delete runs on its own, and deleting a run deletes its files, but the session's saved agent state still holds each file as the tool returned it. DAIV keeps PNG, JPEG, GIF and WebP images, PDFs, audio and video, within the size and per-run limits that [Artifacts](https://docs.daivagent.com/dev/reference/env-variables/#artifacts) lists. A file it did not keep shows the reason instead, such as `Too large to keep`, and runs from before DAIV kept these files show `Not saved`. The agent always gets the full tool result, whether or not the file was kept.

## Artifacts

A run's final message is a paragraph of text, and the agent's workspace is discarded when the run ends. A task whose deliverable is a document (a dependency audit, a code-quality report, a changelog draft, a chart) needs somewhere else to put it. That place is an **artifact**.

The agent has a `publish_artifact` tool: it writes the file to its scratchpad (`/workspace/tmp/...`), publishes it, and gets back a URL that it includes in its final response. DAIV copies the file out of the sandbox into its own storage and attaches it to the run, so it stays available after the sandbox is gone and nothing is committed to the repository.

Published files appear in the transcript, in the turn that published them. Adjacent `publish_artifact` calls in one turn group into a single **Artifacts** card with one row per file. Each row shows the title (linking to the viewer, `/dashboard/sessions/<thread_id>/artifacts/<id>/`, in a new tab), a kind pill, the file size, and a download icon. A publish still in progress shows `Publishing`, and a publish that failed shows its path and the error message in red, in the same card as the files that did publish. Reloading renders the same rows, though publishes from consecutive model steps can land in separate cards. A session with no saved transcript still lists its artifacts on its page, one card per run, below the expired notice.

The limits on a file's size and on the files per run, and the storage that the `app` and `worker` containers must share, are on [Artifacts](https://docs.daivagent.com/dev/reference/env-variables/#artifacts) and in the [deployment guide](https://docs.daivagent.com/dev/getting-started/deployment/index.md). Deleting a run deletes its artifacts and their files.

Select **Artifacts** in the sidebar (`/dashboard/artifacts/`) for every artifact you can see across all sessions: those of your own sessions and of sessions on repositories you can read (admins see them all). The list is newest first, grouped by date (**Today**, **Yesterday**, **Previous 7 days**, **Previous 30 days**, then by month), 25 per page. Search by title or filename, and filter by kind (**All**, **Markdown**, **HTML**, **Image**, **Text** or **File**) and repository. Each row links to the viewer, back to the session and run that produced it, and to a download.

The [jobs API](https://docs.daivagent.com/dev/features/jobs-api/#poll-job-status) and [MCP](https://docs.daivagent.com/dev/features/mcp-endpoint/index.md) job-status responses list artifacts too, with absolute viewer and download URLs, so a CI pipeline or an editor assistant can hand the report to a person.

### Artifact viewers

The viewer depends on the file type:

| File type                                        | Viewer                                                                                          |
| ------------------------------------------------ | ----------------------------------------------------------------------------------------------- |
| Markdown (`.md`)                                 | Rendered on the server and sanitized, inside DAIV; **Copy source** copies the raw Markdown      |
| HTML (`.html`)                                   | Rendered in a sandboxed frame; **Full screen** expands the frame                                |
| Images (`.png`, `.svg`, `.jpg`, `.gif`, `.webp`) | Shown inline on a checkerboard, so transparency shows; select the image to open it at full size |
| Text, CSV, JSON, XML, YAML, logs                 | Shown as preformatted text; **Copy source** copies it                                           |
| Anything else (`.pdf`, archives)                 | Download only                                                                                   |

An HTML artifact's inline CSS and scripts run, in an opaque origin with no access to DAIV cookies, storage or pages. Fetch and XHR requests and form posts are blocked, and scripts, styles, images and fonts can still load over HTTPS.

Markdown and text files over 1 MiB are not previewed, and the viewer offers the download instead.

The viewer's header shows the artifact's kind, its repository, when it was published and last updated, and a link back to the run that produced it. **Open raw** serves the file as is, under the same sandbox policy, so opening an HTML report in its own tab is as safe as the embedded frame. Whoever can open the session can open its artifacts, and the viewer's breadcrumb links back to the **Artifacts** list.

## Revise an artifact

Ask the agent to change a report it already published, and it replaces the file instead of publishing a new artifact: the artifact keeps its URL. You need a session that holds the artifact: the agent reads and revises only the artifacts of the session it runs in, never those of another session.

1. In the session that published the report, send a message that says what to change, such as "add a section on licenses" or "make the table sortable". To point the agent at a specific artifact, paste its URL into the message.
1. Wait for the run to end. The agent edits the file and publishes it again with the artifact's id. If the file is no longer in its workspace because an earlier run published it, the agent copies it back first with `fetch_artifact`.
1. Open the artifact from the transcript. The URL is unchanged, the viewer's header shows when the artifact was last updated, and the transcript rows for it show the current title, type and size, marked **Updated**.

A revised artifact stays on the run that first published it, on the dashboard and in the per-run cards of a session with no transcript. The jobs API and MCP job-status responses list it under that run and under every run that revised it. DAIV keeps only the newest version, and a revision does not count towards the per-run file limit.

## Start a chat session

A chat is the interactive workspace where you work with the agent turn by turn. You need access to at least one repository that DAIV serves.

1. In the sidebar, select **New chat**, the **+** beside **Sessions** (`/dashboard/sessions/new/chat/`).
1. In the **Workspace** card, select **Add repository**, and pick the repository the session works on. The ref defaults to the repository's default branch.
1. To work from another branch, select the branch button on the repository's row, and pick the branch.
1. Optional: in **Environment**, pick the [sandbox environment](https://docs.daivagent.com/dev/features/sandbox-environments/index.md) the agent's commands run in. It stays on **Auto**, resolved for the main repository, until you pick one.
1. Optional: pick the agent model and thinking effort with the model picker in the message row, next to **Send**.
1. Type your first message and send it. The send button stays disabled until a repository is selected.

The session now has its own URL (`/dashboard/sessions/<thread_id>/`), which you can bookmark or share with teammates who have access. The repositories, their refs, the model, the thinking level and the sandbox environment are fixed from your first message for the life of the session. For what the workspace shows as the agent works, see [Chat workspace](#chat-workspace). To use a different repository, branch, model or environment, start a new session.

The ref is the branch the work is based on. When the agent opens a merge request, that ref is its target branch, and if the remote has deleted the branch by the time the agent publishes, the request targets the default branch instead. DAIV assigns the new merge request to you when your DAIV account is linked to the Git platform through OAuth login. If the ref already has an open merge request, the workspace shows it.

### Start a chat over several repositories

Use this for work that spans repositories, such as a backend change and the client that consumes it. The **Workspace** card takes up to 5 repositories, each listed once, on any branch.

1. In the **Workspace** card, add the first repository as in [Start a chat session](#start-a-chat-session). This is the main repository.
1. Select **Add repository** again, and pick another repository.
1. Select the branch button on a row to pick its ref.
1. Repeat steps 2 and 3 for each other repository.
1. Optional: to change the main repository, select **Make main** on another row. Removing the main row promotes the next one.
1. Send your first message.

Once there is more than one repository, the main one is tagged **Main**. Its `.daiv.yml`, sandbox environment, skills and memory drive the session. In **Environment**, `Auto · <environment>` shows the environment resolved for the main repository while the field is on **Auto**.

The list is fixed after your first message: later turns reuse it, as in the [jobs API](https://docs.daivagent.com/dev/features/jobs-api/#multi-repository-sessions). Every repository the agent changes gets its own merge request, as [Merge requests](https://docs.daivagent.com/dev/features/jobs-api/#merge-requests) describes.

## Chat workspace

As the agent works, the workspace shows:

- **Assistant text**: replies render as Markdown.
- **Steps**: consecutive steps (thinking and tool calls) collapse into one line that summarizes what they did, such as `read 3 files · ran 2 commands`, with any failures. Open it to list the steps, and open a step for its details.
- **Finished runs**: when a run ends, its steps fold behind one **Worked for 3m 12s · 14 steps** line. The answer, its artifacts and any question the agent asked stay visible below it, and an answered question collapses to one quiet line.
- **Progress**: when the agent plans with a todo list, a strip behind the message box names the step the run is on, and opens into the list.
- **Files changed**: the same strip lists the files the agent created or changed, with the lines added and removed. Select a file to jump to the tool call that touched it.

Only one run can be in flight per session at a time. While a run streams, select **Stop** to cancel it on the server. If DAIV has not started answering yet, **Stop** takes your message back: it leaves the conversation, the agent never sees it, and its text returns to the message box. Refreshing the page or losing the connection does not stop the run, because reopening the session rejoins the live stream. **Stop** is offered to the session's owner, on chat runs and on the runs a job or the session's inbox started. A run that a webhook started on an issue or merge request that no session owns streams without it.

A message you send while the session is busy is not refused. It shows as **Pending** and waits for the session's next run, which starts as soon as the current one ends and answers it together with anything else that arrived meanwhile. The page follows that run when it starts. [The session inbox](#the-session-inbox) describes the rule.

### Model, thinking level and sandbox environment

Each session pins three choices, set before your first message and fixed afterwards:

| Choice              | What it controls                                                                                                             | Default                                      |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------- |
| Agent model         | Which configured model the agent runs on                                                                                     | The default an admin configured for the site |
| Thinking level      | Reasoning effort, from minimal to extra high, set with the **Effort** control in the model picker                            | The default an admin configured, if any      |
| Sandbox environment | The [sandbox environment](https://docs.daivagent.com/dev/features/sandbox-environments/index.md) the agent's commands run in | **Auto**, resolved for the main repository   |

## Expired sessions

The agent's working state expires after the time set by `DJANGO_REDIS_CHECKPOINT_TTL_MINUTES` ([Redis](https://docs.daivagent.com/dev/reference/env-variables/#redis) lists its default). DAIV saves a copy of each session's conversation when a run ends, so an expired session keeps its transcript and your next message picks up where it left off: DAIV restores the conversation from that copy before the run. If DAIV cannot restore the saved conversation, the run fails instead of starting without it. DAIV keeps saved conversations for as long as their session exists, and nothing prunes them automatically.

A session with no saved transcript, such as one whose state expired before DAIV saved it, shows `This session's state has expired. Start a new session to continue.` Select **New session** to start a fresh one.

## Answer a question

When a request is ambiguous, or a decision is yours to make, the agent stops and asks instead of guessing. The run ends in the **Needs input** status, and the last turn shows a card with 1 to 4 questions. Each question offers option buttons and a free-text field. This also happens in webhook and job sessions: the same card shows wherever a run ended on a question.

1. Open the session. A session that needs you also appears under **Needs you** on **Home**.
1. For each question, select an option. A question marked `Pick any that apply.` takes several, and selecting an option again clears it.
1. If no option fits, or the question has none, type your own answer in the free-text field.
1. Select **Answer**.

DAIV sends all your answers as one message, one line per question, and a question you left blank is sent as `No preference`. The agent starts a new run on the same session that picks up where it left off. You can also type a reply in the message box instead of using the card.

To decline the whole card, select **Skip** in step 4. The agent carries on with reasonable defaults, states the assumptions it made, and does not ask the same questions again.

The card is interactive only on the session's last turn, and only while no run is in flight. Once answered, it stays visible but inert: the options are disabled, the free-text field and the buttons are hidden, the options your answer picked stay highlighted, even after a reload, and your answer shows as the next message.

For job runs (a dashboard submission, the [jobs API](https://docs.daivagent.com/dev/features/jobs-api/index.md) or [MCP](https://docs.daivagent.com/dev/features/mcp-endpoint/index.md)) and [scheduled](https://docs.daivagent.com/dev/features/scheduled-jobs/index.md) runs, DAIV also sends a [notification](https://docs.daivagent.com/dev/features/notifications/index.md) that lists the questions. Chat runs do not notify, and webhook runs rely on the question comment on the issue or merge request, which mentions you.

## Start a background run

A background run hands a task to the agent without a chat: you describe the task, DAIV runs it, and you are notified when it ends. You need access to at least one repository that DAIV serves.

1. Go to the start screen of a background run, by any of these routes:
   - On **Sessions**, select **New**, then the **Background run** card.
   - On **Home**, select **New background run**.
1. In the message box of **Start a run**, describe what the agent should do.
1. Select **Choose repository** and pick one or more repositories. The ref defaults to the repository's default branch, and you can pick another with the branch button on its row.
1. Optional: set the [fields below the message box](#background-run-fields).
1. Select **Start run**.

You land on the **Sessions** list filtered to the run's batch, where each repository's new session has its own row. Runs started this way have the **UI Run** origin.

### Background run fields

| Field                      | What it does                                                                                                                                       |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| Prompt                     | What you want the agent to do                                                                                                                      |
| Repositories               | 1 to 20 repositories. One prompt across several repositories creates a [batch](#filter-the-list): each repository runs as an independent session   |
| Ref                        | The branch each run reads from, and the target of any merge request it opens. Defaults to the repository's default branch                          |
| Sandbox environment        | The [sandbox environment](https://docs.daivagent.com/dev/features/sandbox-environments/index.md) the run executes in. **Auto** lets DAIV choose    |
| Agent model and **Effort** | The model and thinking level for this run, from the agent picker under the message box. Leave them on **default** to use the repository's defaults |
| **MCP servers**            | The MCP servers the run can use                                                                                                                    |

DAIV notifies you on outcomes that need you: found issues, needs attention, needs input and failed. To silence a run, pass `muted` through the [jobs API](https://docs.daivagent.com/dev/features/jobs-api/index.md) or the [MCP endpoint](https://docs.daivagent.com/dev/features/mcp-endpoint/index.md). For a scheduled run, use the schedule's **Mute notifications** setting.

## Retry a run

When a session's latest run fails, its failure marker in the transcript has a **Retry** button.

You can retry when all of these hold:

- You own the session, can run agents on its repository, and the session is not archived.
- The failed run is the newest one, and no run is working or queued.
- The run was a chat turn, or an API, MCP, UI or scheduled run. A webhook run, or a run delegated by another session, reports back through its own flow and does not offer **Retry**.

To retry:

1. Open the session whose latest run failed.
1. In the transcript, select **Retry** on the failure marker.

**Retry** sends your next message in the same session: `The previous run failed. Please try again.` when the run got far enough to be in the conversation, or the run's own prompt again when it failed before that.

A run that was stopped (by you, by the session that delegated it, or by an interruption) shows a grey marker without **Retry**. Send a message to carry on.

## The session inbox

Everything that asks an existing session to act arrives in its inbox:

- A CI result on a merge request the session published.
- A mention or a comment on that merge request.
- A chat message sent while the session was busy.
- A follow-up job.
- A delegated batch finishing.
- A coordinator's **Leg follow-up**, which asks a delegated session to continue its work.

Each one either **wakes** the agent or is only **remembered**:

- **Waking**: a person's request (a chat message, a mention, a follow-up job) and a leg follow-up always wake the agent. A CI failure wakes it only on a commit DAIV pushed, once per commit, while attempts are left ([Pipeline watch](https://docs.daivagent.com/dev/features/pipeline-watch/index.md) describes them). A finished delegated batch wakes it to carry on.
- **Remembered**: a CI pass, a pipeline that needs a person, a push someone else made to the branch, a merge or close, and a comment that does not mention DAIV. These start no run. They appear in the session's timeline as compact rows between runs, and the next run on the session gets them as context, so the agent knows the branch moved before it pushes.

How the inbox groups requests into runs:

- **One run per person and branch**: one run handles everything one person is waiting for on the session's branch. A message you sent to your session and a CI failure that arrived during the same run are answered together, in one run, when the current one ends.
- **Slash commands run alone**: a built-in slash command such as `/help` runs on its own, after what you sent before it and before what you sent after it, because DAIV answers a command only when it is the whole request.
- **Leg follow-ups run alone**: a leg follow-up runs on its own, as a **Delegated Run** that answers only its coordinator, with what the session remembered as context. DAIV never retries it, and it does not run when the session's merge request was merged or closed meanwhile, or the session was archived ([Following up a leg](https://docs.daivagent.com/dev/features/orchestration/#following-up-a-leg) describes it).
- **Different people, different runs**: requests from different people wait for runs of their own, one after another, so one person's request never runs with another's access or tools.
- **Which branch**: a message you sent while the session was busy, a follow-up job sent without a branch and a finished delegated batch follow the branch the current run publishes to. A mention or a CI result stays with its merge request's branch, and a run on another of the session's merge requests updates that merge request.
- **Warm sandbox**: a run that a warm sandbox started, on a branch someone pushed to, brings its checkout up to the remote first, unless it holds commits it has not published, and tells the agent it did.

A run acts as the person whose request woke it:

- A mention by a teammate runs as the DAIV account they signed in to with that GitLab or GitHub account, with its personal MCP servers, not the session owner's. A commenter who never signed in to DAIV with that platform account, or whose DAIV account is deactivated, gets no personal tools, even when a DAIV user shares their username or email.
- Before it starts, a run checks that the person it acts as may still run on the repository, except for a mention, which is checked where it is made. If that person may not, only their own requests fail.
- A session that an issue or merge request comment starts is owned by the DAIV account linked to the comment author's GitLab or GitHub account. Until that account is linked, it has no owner, and once it is linked, its sessions and runs, the merge requests those sessions own and its requests still waiting pass to that user.
- A run that DAIV woke for a CI failure acts as the session's owner. A run that resumes a finished delegated batch acts as the user who delegated it, muted when their run was.

DAIV stops waking a session on its own after 3 runs in a row that nobody asked for, and tells the owner. It still remembers CI failures and finished delegated batches. Sending a message or mentioning DAIV picks the session up again.

## Linking back to sessions

This applies to every session origin, wherever the agent commits.

When DAIV opens a merge request, its description carries a **view sessions** link. The link opens the **Sessions** list filtered to that request, so it covers every session that worked on it, the one that opened it and each later one that addressed a review comment, and it keeps working as sessions are added. A session that produced several requests cannot tell which one the reader came from, so the link then opens the producing session's transcript instead, and so it does before any request is known.

Every commit DAIV makes also carries the producing session as a git trailer:

```
DAIV-Session: https://daiv.example.com/dashboard/sessions/<thread_id>/
```

Unlike the description, the trailer is per commit and append-only. It survives a rewritten description, reaches requests that DAIV only adopts (whose description it leaves untouched), and travels with the commit when it is squashed or cherry-picked. It joins any trailer block the commit message already ends with, so `Co-authored-by` and `Closes` keep working.

An admin turns both off for the whole instance under **Configuration** → **Agent** → **Link sessions from merge requests**, or for one repository with `session_link: false` in `.daiv.yml` ([Repository configuration](https://docs.daivagent.com/dev/customization/repository-config/index.md) lists it).

## Related pages

- **Sandbox environments**

  ______________________________________________________________________

  Configure the named environments the agent's commands run in, and how Auto resolution picks one.

  [Sandbox environments](https://docs.daivagent.com/dev/features/sandbox-environments/index.md)

- **Scheduled jobs**

  ______________________________________________________________________

  Recurring agent runs, each dispatch creating a session per repository.

  [Scheduled jobs](https://docs.daivagent.com/dev/features/scheduled-jobs/index.md)

- **Jobs API**

  ______________________________________________________________________

  Submit runs programmatically; each job creates or continues a session.

  [Jobs API](https://docs.daivagent.com/dev/features/jobs-api/index.md)

- **Notifications**

  ______________________________________________________________________

  Know the moment a run finishes, in the app, by email, in Rocket.Chat or on Telegram.

  [Notifications](https://docs.daivagent.com/dev/features/notifications/index.md)
