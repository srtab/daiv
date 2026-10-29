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

orchestration:
  enabled: false

# Model overrides
models:
  agent:
    model: "openrouter:anthropic/claude-sonnet-4.6"
    fallback_model: "openrouter:openai/gpt-5.3-codex"
    thinking_level: "medium"
  diff_to_metadata:
    model: "openrouter:anthropic/claude-sonnet-4.6"
    fallback_model: "openrouter:openai/gpt-5.3-codex"
  skills:
    code-review: "openrouter:anthropic/claude-sonnet-5.5"
  subagents:
    cr-security: "openrouter:anthropic/claude-opus-5.5"
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
| `orchestration` | `enabled` | `true` | [Orchestration](../features/orchestration.md) — let the agent delegate sub-jobs to other repositories, or to new sessions on this one. |

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

### Skill and subagent models

Run a skill or a subagent on a chosen model in this repository, by name. An entry overrides the site configuration's entry for the same name only; every other site entry still applies.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `models.skills.<name>` | `str \| null` | site entry | Model for the skill's model calls, from its invocation until your next message. |
| `models.subagents.<name>` | `str \| null` | site entry | Model for every call the subagent makes. |

```yaml
models:
  skills:
    code-review: "openrouter:anthropic/claude-sonnet-5.5"
    plan: null   # unset: the site entry, if any, applies
  subagents:
    cr-security: "openrouter:anthropic/claude-opus-5.5"
    my-repo-subagent: "openrouter:z-ai/glm-5.2"
```

For a given name, the model resolves in this order, first match wins:

1. this repository's entry;
2. the site configuration's entry (**Agent → Skill and subagent models**);
3. the model the skill or subagent file declares;
4. the run's model.

A model picked for the run, `daiv-max`, or an exact model passed to the jobs API does not override these entries. Configured models run with the run's thinking level.

Values are checked when a run uses them, not when the file loads, so a typo never drops the rest of `.daiv.yml`. An entry that can't be used falls back to the model the file declares, else the run's model, not to the site entry; the failure is logged at ERROR and the session view shows a notice. Names that match no skill or subagent are ignored. See [Agent Skills](agent-skills.md#running-a-skill-on-another-model) and [Subagents](../features/subagents.md#choosing-a-subagents-model).
