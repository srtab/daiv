# Repository Config

Customize DAIV's behavior per repository using a `.daiv.yml` file in the root of the default branch.

## Full example

```yaml
# Repository settings
default_branch: main
context_file_name: "AGENTS.md"
suggest_context_file: true
session_link: true

# Access control
allowed_usernames:
  - alice
  - bob

# Files the agent can see but won't read
omit_content_patterns:
  - "*.min.js"
  - "*.svg"
  - "*.lock"

# Feature toggles
issue_addressing:
  enabled: true

pull_request_assistant:
  enabled: true

slash_commands:
  enabled: true

pipeline_watch:
  enabled: true
  max_attempts: 3

# Verification hooks
hooks:
  before_publish:
    - "uv run ruff check ."
    - "uv run pytest -q"

# Model overrides
models:
  agent:
    model: "openrouter:anthropic/claude-sonnet-4.6"
    fallback_model: "openrouter:openai/gpt-5.3-codex"
    thinking_level: "medium"
  diff_to_metadata:
    model: "openrouter:anthropic/claude-sonnet-4.6"
    fallback_model: "openrouter:openai/gpt-5.3-codex"
```

## Repository settings

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `default_branch` | `str \| null` | Repository default | Branch DAIV uses to load `.daiv.yml` and as the base for merge requests. |
| `context_file_name` | `str \| null` | `"AGENTS.md"` | Name of the [AGENTS.md](https://agents.md/) guidance file. Set to `null` to disable. |
| `suggest_context_file` | `bool` | `true` | Suggest creating the context file when DAIV opens a merge request and the file is missing. See [AGENTS.md suggestion](../features/issue-addressing.md#agentsmd-suggestion). |
| `session_link` | `bool` | `true` | Link back to the DAIV sessions that produced the changes, from the description of merge requests DAIV opens and from a `DAIV-Session` trailer on every commit. See [Linking back to sessions](../features/sessions.md#linking-back-to-sessions). |

!!! tip
    The `AGENTS.md` file helps DAIV understand your repository's structure, conventions, and constraints. You can generate one using the `/init` skill — see [Slash Commands & Skills](../features/slash-commands.md#init).

## Access control

Restrict which users can interact with DAIV on this repository. This is particularly useful for **public repositories** where you want to prevent arbitrary users from triggering DAIV.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `allowed_usernames` | `list[str]` | `[]` | Usernames allowed to interact with DAIV. When empty, all users are allowed. |

```yaml
allowed_usernames:
  - alice
  - bob
  - charlie
```

When the list is empty or omitted, **all users** can interact with DAIV (default behavior). When populated, only the listed users can trigger DAIV through issues, comments, and merge request reviews. Username matching is case-insensitive.

!!! tip
    Push events (e.g., configuration cache invalidation) are not affected by the allowlist — they are system-level operations tied to the webhook, not to individual users.

## File access

Control which files DAIV can read.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `omit_content_patterns` | `list[str]` | `["*package-lock.json", "*pnpm-lock.yaml", "*.lock", "*.svg"]` | Files matching these patterns are visible to the agent but their content is not read. Useful for large generated files. |

Patterns follow [fnmatch](https://docs.python.org/3/library/fnmatch.html) syntax.

## Feature toggles

All features are enabled by default. Disable any you don't need:

```yaml
issue_addressing:
  enabled: false

pull_request_assistant:
  enabled: false

slash_commands:
  enabled: false
```

| Section | Option | Default | Description |
|---------|--------|---------|-------------|
| `issue_addressing` | `enabled` | `true` | [Issue Addressing](../features/issue-addressing.md) |
| `pull_request_assistant` | `enabled` | `true` | [Pull Request Assistant](../features/pull-request-assistant.md) |
| `slash_commands` | `enabled` | `true` | [Slash Commands & Skills](../features/slash-commands.md) |
| `pipeline_watch` | `enabled` | site-wide | [Pipeline Watch](../features/pipeline-watch.md) — disable to stop DAIV watching CI on published MRs. |
| `pipeline_watch` | `max_attempts` | site-wide | [Pipeline Watch](../features/pipeline-watch.md) — maximum fix attempts before giving up, clamped to the site-wide cap. |

## Verification hooks

`hooks.before_publish` lists commands DAIV runs to check the agent's work before it publishes:

```yaml
hooks:
  before_publish:
    - "uv run ruff check ."
    - "uv run pytest -q"
```

A command passes when it exits 0. When the agent finishes, DAIV runs the commands in order, from the repository root inside the sandbox. They run only in sandbox runs and never when the agent works on a local checkout. Each command has 10 minutes before it counts as failed.

When a command fails, DAIV sends the output back to the agent and the agent fixes the code, for up to 2 rounds. DAIV checks again after each round. A failure that was already there before the change is reported but not sent back for fixing.

!!! warning
    A check that changes files counts as failed, because it would leave the published code different from what was verified. Run formatters in check mode (`ruff format --check .`, not `ruff format .`).

### What happens when checks still fail

DAIV publishes the run with a **Verification** section that lists each command's result and, for failing ones, the end of its output in a collapsible block. It also lists files the checks themselves changed, and suspicious edits: test files, lint and test configuration, CI files and `.daiv.yml`. Suspicious edits are flagged for review, not blocked.

- A new merge request opens as a draft.
- On an existing merge request, the section goes in DAIV's reply comment instead of the description, and the merge request's draft status is not changed.
- The [pipeline watch](../features/pipeline-watch.md) CI fix loop stays off for the merge request, so DAIV doesn't keep trying after the checks gave up.

If the agent gets stuck in a loop and DAIV stops it, the checks still run and are reported, but no fix round is spent. If the checks can't run at all, for example when the sandbox fails, DAIV publishes normally and says in the report that the run was not verified, and why. The CI fix loop works as usual in that case.

### Default commands

A [Sandbox Environment](../features/sandbox-environments.md#default-before_publish-commands) can set default `before_publish` commands for every repository that uses it. A list in `.daiv.yml` replaces the environment's list entirely. Set `before_publish: []` to turn the checks off for the repository.

### Without commands

When no commands are declared, DAIV sends the agent one reminder if it edited files and ran no passing check after its last edit. The run is published as usual.

### Site switch

Administrators can turn verification off for all repositories with **Verification enabled** in the **Agent** section of the site configuration. It is on by default. When it is off, DAIV runs no `before_publish` commands and sends no reminder.

## Branch naming and commit conventions

DAIV generates branch names and commit messages automatically. You can define your conventions in `AGENTS.md`:

```markdown
## Branch naming
Use: pr/<issue-id>-<kebab-summary>

## Commit messages
Use: (<ISSUE-ID>) <type>: <summary>
Where <type> is one of: feat, fix, chore, docs, refactor, test
```

If no conventions are defined, DAIV defaults to:

- **Branches**: `<type>/<short-kebab-summary>`
- **Commits**: Conventional Commits style `<type>: <short summary>`

## Model overrides

Override the default models on a per-repository basis. Useful for using smaller models on simple projects or larger models on complex ones.

Configuration priority (highest to lowest):

1. **A model picked for the run** — in the agent picker, or as `agent_model` through the API and MCP
2. **Issue labels** (`daiv-max`) — see [Issue Addressing](../features/issue-addressing.md#max-mode); max mode uses the site's max thinking level, not `thinking_level`
3. **`.daiv.yml` models section** — per-repository overrides
4. **Site settings** — global defaults (environment variables or the configuration page)

### Agent

The main DAIV agent used for issue addressing, pull request assistance, and all interactive tasks.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `models.agent.model` | `str` | site setting | Primary model. |
| `models.agent.fallback_model` | `str` | site setting | Fallback if the primary model fails. |
| `models.agent.thinking_level` | `"minimal" \| "low" \| "medium" \| "high" \| "xhigh" \| null` | site setting | Thinking depth. Set to `null` to disable. |

A model picked for a single run (in the agent picker, or as `agent_model` through the API and MCP) is tried first, with the model and fallback above after it, and a thinking level picked for a run beats `thinking_level`.

### Diff to metadata

Generates pull request titles, descriptions, and commit messages from diffs.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `models.diff_to_metadata.model` | `str` | site setting | Primary model. |
| `models.diff_to_metadata.fallback_model` | `str` | site setting | Fallback if the primary model fails. |

!!! note
    The diff to metadata agent reads your `AGENTS.md` file to follow your branch naming and commit message conventions.
