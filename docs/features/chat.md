# Chat

Chat is an interactive workspace in the dashboard where you talk to the DAIV agent in real time against a repository and ref. You send a prompt, and the agent reads and edits code, runs commands in a sandbox, and streams its tool calls, todo list, and changed files back to you live — all on a single conversation thread you can leave and return to.

Reach for chat when you want a back-and-forth with the agent: exploring an unfamiliar codebase, iterating on a change across several turns, or debugging interactively. For automated, hands-off work — addressing a labelled issue or responding to review comments — use the [Pull Request Assistant](pull-request-assistant.md) and [Issue Addressing](issue-addressing.md) flows instead, which DAIV drives from webhooks without you watching.

!!! info "Sign-in required"
    Chat is a signed-in dashboard feature. Every page and the streaming endpoint require an authenticated session — there is no anonymous access.

---

## Starting a chat

Go to **Dashboard > Chat** (`/dashboard/chat/`) for the list of your past conversations, then click **New chat** (`/dashboard/chat/new/`) to open the empty workspace.

Before the composer appears, pick a **repository** in the hero picker. Each chat is bound to a repository and a ref:

- Selecting a repository defaults the ref to its **default branch**; you can switch to any other branch.
- If the chosen ref already has an open merge/pull request, the workspace surfaces it (see [Merge request awareness](#merge-request-awareness)).

Once a repository is selected, the prompt box fades in and you can type your first message. The conversation gets its own thread URL (`/dashboard/chat/<thread_id>/`) so you can bookmark it or share the link with teammates who have access.

!!! note "A chat targets one repository and ref"
    The repository and ref are fixed for the life of the thread. To work against a different repository or branch, start a new chat.

### Continuing a run from Activity

Any agent run recorded in the [Activity log](activity-tracking.md) that still has live state can be opened as a chat. From an activity's detail page, the **Continue as chat** action bridges to `/dashboard/chat/from-activity/<activity_id>/`, which creates (or reuses) a chat thread for that run and redirects you into it — the conversation picks up where the run left off.

!!! warning "Expired state"
    Conversation state lives in the agent checkpointer and can expire. Opening an expired run shows an "expired" notice and prompts you to start a fresh chat; the composer is hidden for expired threads.

---

## What you can do

Type a prompt describing the change, the bug, or what you want to explore, and press **Send** (or `Cmd+Enter` / `Ctrl+Enter`). The agent works the same way it does everywhere else in DAIV: it reads and edits files, runs shell commands inside a sandbox, and uses its skills and subagents.

As the agent works, the workspace streams updates live (over AG-UI):

- **Assistant text and reasoning** — the agent's replies render as Markdown; thinking/reasoning is shown in collapsible segments.
- **Tool calls** — each tool call appears as an expandable card with its status (running, done, error), arguments, and result. Sub-agent activity is folded in.
- **Todos** — when the agent plans with a todo list, the side rail shows the list with a `done/total` count.
- **Files changed** — files the agent reads or edits (including files touched by sandbox shell commands) are collected in the rail; click one to jump to the tool call that touched it.

A summary strip mirrors the essentials on narrower screens: `repository · ref · run status · todos · files`.

While a run is streaming you can press **Stop** to abort it. Only one run can be in flight per thread at a time — opening the same conversation in a second tab and submitting there returns a "run already in progress" response.

### Merge request awareness

When the agent commits changes, DAIV creates or updates a merge/pull request on the thread's ref (the standard DAIV post-run behaviour). The workspace shows an MR/PR pill that links to it and flags drafts. A pre-existing open request for the ref is detected and shown even before the agent runs.

---

## Choosing the model and sandbox environment

Each chat pins three choices, set in the hero before your first message and then locked for the rest of the thread:

| Choice | What it controls | Default |
|--------|------------------|---------|
| **Agent model** | Which configured LLM the agent runs on (the provider must be enabled) | The admin-configured system default |
| **Thinking level** | Reasoning effort: Minimal, Low, Medium, High, or Extra high | The admin-configured default (if any) |
| **Sandbox environment** | The named [Sandbox Environment](sandbox-environments.md) the agent's commands run in | **Auto** — resolved per repository |

These selections are **fixed at thread creation**. On later turns the composer shows them as locked pills (mirroring what the thread was created with) and the backend ignores any attempt to change them. To use a different model, effort level, or sandbox environment, start a new chat.

!!! note "Auto sandbox environment"
    Leaving the sandbox environment on **Auto** lets DAIV resolve an environment for the repository when the run starts. The workspace then swaps the locked pill from "Auto" to the resolved environment's name once the run begins. See [Sandbox Environments](sandbox-environments.md) for how environments are scoped and resolved.

!!! warning "Pinned model no longer available"
    If the model pinned to a thread has since been disabled or removed (for example, an admin turned off its provider), the next turn is refused with a message telling you to start a new thread to pick another model.

---

## Notes

- **Visibility** — the chat list and every conversation are scoped to you: you only see and resume your own threads.
- **Titles** — a thread's title is generated automatically from your first prompt; until it's ready the list shows "generating title…".
- **Relation to the rest of DAIV** — a chat run is a first-class agent run, so it also appears in the [Activity log](activity-tracking.md) (the chat thread and its activity share the same thread id). Per-run token usage and USD cost are surfaced there, alongside runs from webhooks, the [Jobs API](jobs-api.md), the [MCP endpoint](mcp-endpoint.md), and [Scheduled Jobs](scheduled-jobs.md).

---

## Related

<div class="grid cards" markdown>

-   :octicons-container-24: **Sandbox Environments**

    ---

    Configure the named environments the agent's commands run in, and how Auto resolution picks one.

    [:octicons-arrow-right-24: Sandbox Environments](sandbox-environments.md)

-   :octicons-people-24: **Subagents**

    ---

    The specialised agents whose activity is folded into the chat transcript.

    [:octicons-arrow-right-24: Subagents](subagents.md)

-   :octicons-pulse-24: **Activity Tracking**

    ---

    The unified log of every agent run — including chats — with timing, token usage, and cost.

    [:octicons-arrow-right-24: Activity Tracking](activity-tracking.md)

</div>
