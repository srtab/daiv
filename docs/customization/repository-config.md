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

# Verification hooks
hooks:
  before_publish:
    - "uv run ruff check ."
    - "uv run pytest -q"

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

The context file tells DAIV about your repository's structure, conventions and constraints. Generate one with the `/init` skill; see [Slash commands and skills](../features/slash-commands.md#init). For branch names and commit messages, see [Set branch and commit conventions](agent-skills.md#set-branch-and-commit-conventions).

`suggest_context_file` and `session_link` also have site-wide switches in [Site configuration](../reference/site-configuration.md). DAIV applies them only when both the repository and the site allow them, so `true` here cannot turn on a feature the site turned off.

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
| `issue_addressing` | `enabled` | `true` | [Issue addressing](../features/issue-addressing.md) |
| `pull_request_assistant` | `enabled` | `true` | [Pull request assistant](../features/pull-request-assistant.md) |
| `slash_commands` | `enabled` | `true` | [Slash commands and skills](../features/slash-commands.md) |
| `pipeline_watch` | `enabled` | `true` | [Pipeline watch](../features/pipeline-watch.md): set it to `false` to stop DAIV watching CI on the merge requests it publishes. |
| `pipeline_watch` | `max_attempts` | site-wide | [Pipeline watch](../features/pipeline-watch.md): the maximum number of fix attempts before DAIV hands the merge request back. DAIV caps it at the site-wide limit. |
| `orchestration` | `enabled` | `true` | [Orchestration](../features/orchestration.md): let the agent delegate sub-jobs to other repositories, or to new sessions on this one. |
| `memory` | `enabled` | `true` | Repository memory: capture observations from finished runs, consolidate them, and add them to the system prompt of later runs. Set it to `false` to opt this repository out. |

`pipeline_watch.enabled` and `memory.enabled` apply only while the matching site-wide setting is on too: **Pipeline Watch** and **Memory** in [Site configuration](../reference/site-configuration.md). The other toggles apply to the repository alone.

## Verification hooks

`hooks.before_publish` lists commands DAIV runs to check the agent's work before it publishes:

```yaml
hooks:
  before_publish:
    - "uv run ruff check ."
    - "uv run pytest -q"
```

| Section | Option | Default | Description |
|---------|--------|---------|-------------|
| `hooks` | `before_publish` | the sandbox environment's list | Commands to run before publishing, up to 10. `[]` turns verification off for the repository. See [Default commands](#default-commands). |

A command passes when it exits 0. When the agent finishes, DAIV runs the commands in order, from the repository root inside the sandbox. They run only in sandbox runs and never when the agent works on a local checkout. You can list up to 10 commands. Each command has 10 minutes before it counts as failed. When the sandbox has `timeout`, DAIV then stops the command and every process it started, except ones that detach into a session of their own (`setsid`, daemons). All the checks in a run share 30 minutes: once those are spent, no further check starts and the run is published as not verified.

In a run that works on several repositories, only the main repository's commands run. Changes in the other repositories are published without verification.

When a command fails, DAIV sends the output back to the agent and the agent fixes the code, for up to 2 rounds. DAIV checks again after each round. A command that also fails on the code from before the change is reported as already failing and not sent back for fixing. If its output now has lines it didn't have before, for example a test that newly fails, DAIV shows the agent both outputs to fix what its change broke, and the report says the command's output changed. Such a command doesn't count as failed, because output also changes when, say, the agent adds passing tests.

Before a run is published as failed, DAIV runs each command that failed only on the agent's code once more. One that passes then is reported as possibly flaky and doesn't count as failed.

!!! warning
    A check that changes files counts as failed, because it would leave the published code different from what was verified. DAIV puts those files back as the agent left them, so nothing the checks write is published. Run formatters in check mode (`ruff format --check .`, not `ruff format .`), and add files the checks generate, such as coverage reports, to `.gitignore`.

### What happens when checks still fail

DAIV publishes the run with a **Verification** section that lists each command's result. The checks' output isn't published unless an administrator turns on [**Publish check output**](#site-switch). The section also lists files the checks themselves changed, and suspicious edits: test files, lint and test configuration, task runners such as a `Makefile`, CI files and `.daiv.yml`. Suspicious edits are flagged for review, not blocked.

- A new merge request opens as a draft.
- On an existing merge request, the merge request's draft status is not changed, and the section goes in DAIV's reply: the chat reply, the comment it posts on the issue or merge request, or the result of a job, MCP call or scheduled job. When DAIV opened the merge request, the section in its description is replaced with the new one, so it always shows the latest checks.
- The [pipeline watch](../features/pipeline-watch.md) counts the commit DAIV published as already examined, so CI failing on it doesn't wake the session for a fix after the checks gave up. The session still shows the pipeline's result, and the next commit DAIV pushes is watched as usual.

If the agent gets stuck in a loop and DAIV stops it, the checks still run and are reported, but no fix round is spent. If the checks can't run at all, for example when the sandbox fails, DAIV publishes normally and says in the report that the run was not verified, and why. The CI fix loop works as usual in that case.

### Default commands

A [sandbox environment](../features/sandbox-environments.md#before-publish-commands) can set default `before_publish` commands for every repository that uses it. A list in `.daiv.yml` replaces the environment's list entirely. Set `before_publish: []` to turn verification off for the repository: DAIV runs no commands and sends no reminder.

### Without commands

When no commands are declared, DAIV sends the agent one reminder if the files changed after the last test, linter or build the agent ran, or that check failed. The run is published as usual.

### Site switch

Administrators can turn verification off for all repositories with **Verification enabled** in the **Agent** group of [Site configuration](../reference/site-configuration.md). It is on by default. When it is off, DAIV runs no `before_publish` commands and sends no reminder.

**Publish check output**, in the same section, adds the end of each failing command's output to the **Verification** section, in a collapsible block. It is off by default. The values of the sandbox's environment variables are masked as `******`, as typed or URL- or base64-encoded, but values shorter than 8 characters are not.

!!! warning
    Masking only catches exact copies of a value. A check that prints a secret in another form, such as reversed, hex-encoded or split across lines, publishes it to everyone who can read the merge request. Removing the section later doesn't help: GitLab and GitHub keep earlier versions of a description and comment in their edit history. Turn this on only where everyone who can read the repository's merge requests may see the sandbox's secrets.

## Model overrides

A repository can use different models from the site's, for example smaller models on simple projects and larger ones on complex projects. A model is written as a model spec such as `openrouter:anthropic/claude-sonnet-5.5`; see [LLM providers](../getting-started/llm-providers.md).

The model for a run comes from the first source in this list that sets one, highest priority first:

1. **A model picked for the run**, in the agent picker or as `agent_model` through the API and MCP.
2. **The `daiv-max` issue label**, described in [Issue addressing](../features/issue-addressing.md#max-mode). Max mode uses the site's max thinking level, not `thinking_level`.
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

Run a skill or a subagent on a chosen model in this repository, by name. An entry overrides the site's entry for the same name only; every other site entry still applies. Site entries are set in the dashboard: a skill's on the **Models** tab of its page, which also holds the code-review detectors', and the general-purpose and explore subagents' under **Configuration** → **Agent**.

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
2. the site's entry (a skill page's **Models** tab, or **Configuration** → **Agent** → **Subagent models**);
3. the model the subagent file, or a built-in or global skill file, declares (a repository skill's `metadata.model` is ignored);
4. the run's model.

A model picked for the run, `daiv-max`, or an exact model passed to the jobs API does not override these entries. Configured models run with the run's thinking level.

DAIV checks the values when a run uses them, not when the file loads, so a typo never drops the rest of `.daiv.yml`. An entry that can't be used falls back to the model the file declares, else the run's model, not to the site entry, and DAIV logs the failure as an error. Names that match no skill or subagent are ignored. See [Agent skills](agent-skills.md#run-a-skill-on-another-model) and [Subagents](../features/subagents.md#subagent-model-precedence).
