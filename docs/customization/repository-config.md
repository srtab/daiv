---
description: "Configure DAIV per repository with a .daiv.yml file: who can trigger it, which diff content it omits, feature toggles and model overrides."
---

# Repository configuration

A `.daiv.yml` file in the root of a repository's default branch changes how DAIV behaves in that repository: who can trigger it, which features run, and which models it uses. A repository without the file uses the defaults on this page and the site-wide settings.

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

# Files whose diff content the commit and merge request writer does not see
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

memory:
  enabled: true

# Model overrides
models:
  agent:
    model: "openrouter:anthropic/claude-sonnet-5.5"
    fallback_model: "openrouter:openai/gpt-6.1-sol"
    thinking_level: "medium"
  diff_to_metadata:
    model: "openrouter:anthropic/claude-sonnet-5.5"
    fallback_model: "openrouter:openai/gpt-6.1-sol"
  skills:
    code-review: "openrouter:anthropic/claude-sonnet-5.5"
  subagents:
    cr-security: "openrouter:anthropic/claude-opus-5.5"
```

## How DAIV reads the file

DAIV always reads `.daiv.yml` from the platform's default branch, whatever the `default_branch` option says. It caches the file for 1 hour per repository, and a push to the default branch clears the cache, so a change you merge takes effect after that push. If the file is missing, DAIV uses the defaults. If DAIV cannot parse the file or one of its values, it logs an error and uses the defaults for the whole file. Model entries under `models.skills` and `models.subagents` are the exception: DAIV checks them when a run uses them, so a typo there never drops the rest of the file.

## Repository settings

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `default_branch` | `str \| null` | Repository default | Base branch for merge requests. |
| `context_file_name` | `str \| null` | `"AGENTS.md"` | Name of the [AGENTS.md](https://agents.md/) guidance file. Set to `null` to disable. |
| `suggest_context_file` | `bool` | `true` | Suggest creating the context file when DAIV opens a merge request and the file is missing. See [AGENTS.md suggestion](../features/issue-addressing.md#agentsmd-suggestion). |
| `session_link` | `bool` | `true` | Link back to the DAIV sessions that produced the changes, from the description of merge requests DAIV opens and from a `DAIV-Session` trailer on every commit. See [Linking back to sessions](../features/sessions.md#linking-back-to-sessions). |

The context file tells DAIV about your repository's structure, conventions and constraints. Generate one with the `/init` skill; see [Slash Commands & Skills](../features/slash-commands.md#init). For branch names and commit messages, see [Set branch and commit conventions](agent-skills.md#set-branch-and-commit-conventions).

`suggest_context_file` and `session_link` also have site-wide switches in [Site Configuration](../reference/site-configuration.md). DAIV applies them only when both the repository and the site allow them, so `true` here cannot turn on a feature the site turned off.

## Access control

Access control restricts which users can interact with DAIV on this repository. It matters most on public repositories, where you want to stop arbitrary users from triggering DAIV.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `allowed_usernames` | `list[str]` | `[]` | Usernames allowed to interact with DAIV. When empty, all users are allowed. |

```yaml
allowed_usernames:
  - alice
  - bob
  - charlie
```

When the list is empty or omitted, all users can interact with DAIV. When it lists users, only they can trigger DAIV through issues, comments and merge request reviews. Username matching is case-insensitive.

!!! warning
    The allowlist decides who can **trigger** DAIV, not who it reads. On a merge request a DAIV session owns, a comment that doesn't mention DAIV is remembered from anyone and shown to that session's next run as context (see [Pull request assistant](../features/pull-request-assistant.md#a-merge-request-daiv-published)). On a public repository, treat those comments as untrusted input.

## Omitted diff content

`omit_content_patterns` lists files whose changes stay in the diff, but whose content DAIV replaces with a placeholder before the diff reaches the model that writes commit messages and merge request descriptions. It keeps large generated files out of that model's input.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `omit_content_patterns` | `list[str]` | `["*package-lock.json", "*pnpm-lock.yaml", "*.lock", "*.svg"]` | Path patterns whose diff content is left out of generated commit messages and merge request descriptions. |

Patterns follow [fnmatch](https://docs.python.org/3/library/fnmatch.html) syntax and match against the file's path in the repository.

## Feature toggles

All features are on by default. To turn one off, set its `enabled` option to `false`:

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
| `pipeline_watch` | `enabled` | `true` | [Pipeline Watch](../features/pipeline-watch.md): set it to `false` to stop DAIV watching CI on the merge requests it publishes. |
| `pipeline_watch` | `max_attempts` | site-wide | [Pipeline Watch](../features/pipeline-watch.md): the maximum number of fix attempts before DAIV hands the merge request back. DAIV caps it at the site-wide limit. |
| `orchestration` | `enabled` | `true` | [Orchestration](../features/orchestration.md): let the agent delegate sub-jobs to other repositories, or to new sessions on this one. |
| `memory` | `enabled` | `true` | Repository memory: capture observations from finished runs, consolidate them, and add them to the system prompt of later runs. Set it to `false` to opt this repository out. |

`pipeline_watch.enabled` and `memory.enabled` apply only while the matching site-wide setting is on too: **Pipeline Watch** and **Memory** in [Site Configuration](../reference/site-configuration.md). The other toggles apply to the repository alone.

## Model overrides

A repository can use different models from the site's, for example smaller models on simple projects and larger ones on complex projects. A model is written as a model spec such as `openrouter:anthropic/claude-sonnet-5.5`; see [LLM providers](../getting-started/llm-providers.md).

The model for a run comes from the first source in this list that sets one, highest priority first:

1. **A model picked for the run**, in the agent picker or as `agent_model` through the API and MCP.
2. **The `daiv-max` issue label**, described in [Issue Addressing](../features/issue-addressing.md#max-mode). Max mode uses the site's max thinking level, not `thinking_level`.
3. **The `models` section of `.daiv.yml`.**
4. **Site settings**: environment variables or the Site Configuration page.

DAIV tries the model from the highest source that sets one first. The `models.agent` model and fallback follow it, or the site's models where the file sets none.

### Agent

The main DAIV agent handles issue addressing, pull request assistance and all interactive tasks.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `models.agent.model` | `str` | site setting | Primary model. |
| `models.agent.fallback_model` | `str` | site setting | Fallback if the primary model fails. |
| `models.agent.thinking_level` | `"minimal" \| "low" \| "medium" \| "high" \| "xhigh" \| null` | site setting | Thinking depth. Set to `null` to disable. |

A model picked for a single run is tried first, with the model and fallback above after it, and a thinking level picked for a run beats `thinking_level`.

### Diff to metadata

The diff to metadata agent generates pull request titles, descriptions and commit messages from diffs.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `models.diff_to_metadata.model` | `str` | site setting | Primary model. |
| `models.diff_to_metadata.fallback_model` | `str` | site setting | Fallback if the primary model fails. |

### Skill and subagent models

Run a skill or a subagent on a chosen model in this repository, by name. An entry overrides the site's entry for the same name only; every other site entry still applies. Site entries are set in the dashboard: a skill's on the **Models** tab of its page, which also holds the code-review detectors', and the general-purpose and explore subagents' under **Configuration → Agent**.

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

Replace `<name>` with the name of a skill or subagent. For a given name, the model resolves in this order, first match wins:

1. this repository's entry;
2. the site's entry (a skill page's **Models** tab, or **Configuration → Agent → Subagent models**);
3. the model the subagent file, or a built-in or global skill file, declares (a repository skill's `metadata.model` is ignored);
4. the run's model.

A model picked for the run, `daiv-max`, or an exact model passed to the jobs API does not override these entries. Configured models run with the run's thinking level.

DAIV checks the values when a run uses them, not when the file loads, so a typo never drops the rest of `.daiv.yml`. An entry that can't be used falls back to the model the file declares, else the run's model, not to the site entry, and DAIV logs the failure as an error. Names that match no skill or subagent are ignored. See [Agent Skills](agent-skills.md#running-a-skill-on-another-model) and [Subagents](../features/subagents.md#choosing-a-subagents-model).
