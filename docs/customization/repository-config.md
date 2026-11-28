---
description: "Configure DAIV per repository with a .daiv.yml file: who can trigger it, which diff content it omits, feature toggles, submodules and Git LFS, and model overrides."
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

# Submodules and Git LFS content
checkout:
  submodules: true
  lfs: true

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

## Submodules and LFS

By default DAIV clones the repository alone: submodule directories are empty and files Git LFS tracks hold their pointer text. The `checkout` section turns on each one for this repository.

```yaml
checkout:
  submodules: true
  lfs: true
```

| Section | Option | Default | Description |
|---------|--------|---------|-------------|
| `checkout` | `submodules` | `false` | Check out the repository's submodules, nested ones included, at the commits the branch records, and publish the changes inside those the run's person can write as merge requests in their own repositories. |
| `checkout` | `lfs` | `false` | Download the content of LFS-tracked files, in the repository and in the submodules DAIV checked out, up to the operator's limit, `CODEBASE_LFS_MAX_CLONE_BYTES` (see [Environment variables](../reference/env-variables.md)). Files past the limit keep their pointer text. |

Each repository of a session, the main one and each extra one, follows its own `.daiv.yml`. Nested submodules follow the repository that started the clone: DAIV reads a submodule's own `.daiv.yml` only for its default branch, omitted diff content and context file name, never for its `checkout` section. At the start of every run the agent is told which submodules and LFS files DAIV fetched, which it left out and why, and which submodules it may change.

### What DAIV fetches, and with whose access

DAIV checks out a submodule only when all of these hold:

- Its URL is on the run's git platform: an HTTPS or SSH URL on the same host, or a relative URL. DAIV fetches it over HTTPS whatever the URL's scheme, and never contacts another host or uses another protocol.
- Its repository is one DAIV works on: in the repository list DAIV syncs, which on GitLab means DAIV's account is a Maintainer of the project. Only there can DAIV mint a credential for that repository alone.
- The person the run belongs to can read the repository, as DAIV's repository access records it. DAIV records members only, so a public or internal repository counts only for its members. For a run a DAIV user started, the rules that decide which repositories that user sees apply; for a label, mention or pipeline from a platform account, that account's access applies; for a run DAIV starts on its own, the session owner's.

Each submodule is fetched with a short-lived credential minted for its repository alone, never the main repository's. LFS content comes only from the platform's own LFS API: an `lfs.url` in the repository's `.lfsconfig` is ignored. A submodule or LFS file DAIV can't fetch never fails the run: its directory stays empty, or the file keeps its pointer text, and the agent is told why.

### Which submodule changes DAIV publishes

DAIV publishes the changes inside a checked-out submodule when the run's person can write its repository, and every submodule between it and the repository that contains it is published too. Changes inside any other submodule are read-only and stay out of the publish. A session publishes each repository from one checkout only: the session's own repository when a submodule is also the main or an extra repository, else the first checkout of it.

When the agent changed files inside a published submodule, DAIV:

1. Publishes the innermost submodules first, each to a merge request in its own repository. The merge request targets the branch `.gitmodules` names for that submodule when its repository has that branch, and the repository's default branch otherwise.
2. Records each submodule, in the repository that contains it, at exactly the commit it pushed, in that repository's own merge request.
3. Links the merge requests to each other. The merge request of the repository that records a submodule lists the submodule's under **Merge first**.

A submodule whose publish fails leaves the commit its parent records as it was, and the run's reply names it. Later turns of the session continue on each submodule's branch and update the same merge request. On GitLab, each submodule's project needs DAIV's webhook, as every project DAIV works on does, so DAIV learns when a submodule's merge request merges.

### After a submodule's merge request merges

Merge a submodule's merge request before the merge request that records it. When the submodule's merge request is squashed or rebased as it merges, the commit DAIV recorded is no longer on any of that repository's branches. DAIV then wakes the session and records the commit the merge produced: in the parent's open merge request, or in a new one from the default branch when the parent's merge request already merged. When the parent's branch no longer records the commit DAIV pushed, DAIV changes nothing. Either way the session says what DAIV did. A merge commit that keeps DAIV's commit needs no change, and DAIV starts no run for it.

### What DAIV doesn't publish

- Changes to LFS-tracked files, new ones included, in the repository or in a submodule DAIV publishes.
- Changes inside read-only submodules.
- Adding, removing or moving a submodule. Edits to `.gitmodules` are published like any other file.

DAIV publishes the rest of the run's changes, and the run's reply names what it left out and why. A turn whose only changes are left out publishes nothing.

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

Run a skill or a subagent on a chosen model, at a chosen thinking level, in this repository, by name. An entry overrides the site's entry for the same name field by field: an entry that sets only a model keeps the site entry's level, and the other way round. Every other site entry still applies. Site entries are set in the dashboard: a skill's on the **Models** tab of its page, which also holds the code-review detectors', and the general-purpose and explore subagents' under **Configuration** → **Agent** → **Subagent models**. Each of those rows has a model picker and effort dots, including **Off**, and an empty row shows what it inherits.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `models.skills.<name>` | `str \| {model, thinking_level} \| null` | site entry | Model and thinking level for the skill's model calls, from its invocation until your next message. |
| `models.subagents.<name>` | `str \| {model, thinking_level} \| null` | site entry | Model and thinking level for every call the subagent makes. |

An entry is a string, which sets the model only, or a mapping with either or both of `model` and `thinking_level`. `thinking_level` is one of `minimal`, `low`, `medium`, `high`, `xhigh` or `off`, in any letter case. `off` turns thinking off and stops the level from being inherited from a later step; an unset level inherits.

```yaml
models:
  skills:
    code-review: {thinking_level: medium}
    plan: null   # unset: the site entry, if any, applies
  subagents:
    cr-security: {thinking_level: xhigh}
    cr-correctness: {model: "openrouter:anthropic/claude-opus-5.5", thinking_level: high}
    cr-tests: {thinking_level: off}
    my-repo-subagent: "openrouter:z-ai/glm-5.2"   # a string sets the model only
```

Replace `<name>` with the name of a skill or subagent. For a given name, the model and the thinking level each resolve in this order, first step that sets the field wins, and a model from one step combines with a level from another:

- **A skill:**
    1. this repository's entry;
    2. the site's entry (the skill page's **Models** tab);
    3. `metadata.model` and `metadata.thinking_level` in a built-in or global skill file (a repository skill's metadata is ignored);
    4. the run's model and thinking level.
- **A code-review detector** (`cr-*`):
    1. this repository's entry;
    2. the site's entry;
    3. `model` and `thinking_level` in the detector's charter;
    4. the `code-review` skill's model and thinking level, resolved as above;
    5. the run's model and thinking level.
- **The explore subagent:**
    1. this repository's entry;
    2. the site's entry;
    3. its built-in model, `openrouter:anthropic/claude-haiku-5.5` (a model only);
    4. the run's thinking level, and the run's model when Haiku cannot be built.
- **The general-purpose subagent:**
    1. this repository's entry;
    2. the site's entry;
    3. the run's model and thinking level.
- **A custom subagent:**
    1. this repository's entry;
    2. the site's entry;
    3. `model` and `thinking_level` in its file;
    4. the run's model and thinking level.

A model picked for the run, `daiv-max`, or an exact model passed to the jobs API does not override a configured model. A thinking level set at any step beats the run's level, `daiv-max`'s included. The fallback models keep the site's fallback thinking level.

DAIV checks the values when a run uses them, not when the file loads, so a typo never drops the rest of `.daiv.yml`. An unknown `thinking_level` is dropped and logged as an error, and the rest of the entry still applies; so is a key other than `model` and `thinking_level`, such as a misspelled `thinking`. A model that can't be used falls back to the model the file declares (for a detector, then the `code-review` skill's model), else the run's model, not to the site entry, and DAIV logs the failure as an error. Names that match no skill or subagent are ignored. See [Agent skills](agent-skills.md#run-a-skill-on-another-model) and [Subagents](../features/subagents.md#subagent-model-precedence).
