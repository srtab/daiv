# Repository Config

Customize DAIV's behavior per repository using a `.daiv.yml` file in the root of the default branch.

## Full example

```
# Repository settings
default_branch: main
context_file_name: "AGENTS.md"
suggest_context_file: true

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

# Sandbox
sandbox:
  base_image: "python:3.12-bookworm"
  network_enabled: false
  cpus: 2.0
  memory_bytes: 4294967296
  command_policy:
    allow: []
    disallow: []

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

| Option                 | Type          | Default            | Description                                                                                                                                                                                             |
| ---------------------- | ------------- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `default_branch`       | `str \| null` | Repository default | Branch DAIV uses to load `.daiv.yml` and as the base for merge requests.                                                                                                                                |
| `context_file_name`    | `str \| null` | `"AGENTS.md"`      | Name of the [AGENTS.md](https://arxiv.org/abs/2602.11988) guidance file. Set to `null` to disable.                                                                                                      |
| `suggest_context_file` | `bool`        | `true`             | Suggest creating the context file when DAIV opens a merge request and the file is missing. See [AGENTS.md suggestion](https://srtab.github.io/daiv/dev/features/issue-addressing/#agentsmd-suggestion). |

Tip

The `AGENTS.md` file helps DAIV understand your repository's structure, conventions, and constraints. You can generate one using the `/init` skill — see [Slash Commands & Skills](https://srtab.github.io/daiv/dev/features/slash-commands/#init).

## Access control

Restrict which users can interact with DAIV on this repository. This is particularly useful for **public repositories** where you want to prevent arbitrary users from triggering DAIV.

| Option              | Type        | Default | Description                                                                 |
| ------------------- | ----------- | ------- | --------------------------------------------------------------------------- |
| `allowed_usernames` | `list[str]` | `[]`    | Usernames allowed to interact with DAIV. When empty, all users are allowed. |

```
allowed_usernames:
  - alice
  - bob
  - charlie
```

When the list is empty or omitted, **all users** can interact with DAIV (default behavior). When populated, only the listed users can trigger DAIV through issues, comments, and merge request reviews. Username matching is case-insensitive.

Tip

Push events (e.g., configuration cache invalidation) are not affected by the allowlist — they are system-level operations tied to the webhook, not to individual users.

## File access

Control which files DAIV can read.

| Option                  | Type        | Default                                                        | Description                                                                                                             |
| ----------------------- | ----------- | -------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `omit_content_patterns` | `list[str]` | `["*package-lock.json", "*pnpm-lock.yaml", "*.lock", "*.svg"]` | Files matching these patterns are visible to the agent but their content is not read. Useful for large generated files. |

Patterns follow [fnmatch](https://docs.python.org/3/library/fnmatch.html) syntax.

## Feature toggles

All features are enabled by default. Disable any you don't need:

```
issue_addressing:
  enabled: false

pull_request_assistant:
  enabled: false

slash_commands:
  enabled: false
```

| Section                  | Option    | Default | Description                                                                                         |
| ------------------------ | --------- | ------- | --------------------------------------------------------------------------------------------------- |
| `issue_addressing`       | `enabled` | `true`  | [Issue Addressing](https://srtab.github.io/daiv/dev/features/issue-addressing/index.md)             |
| `pull_request_assistant` | `enabled` | `true`  | [Pull Request Assistant](https://srtab.github.io/daiv/dev/features/pull-request-assistant/index.md) |
| `slash_commands`         | `enabled` | `true`  | [Slash Commands & Skills](https://srtab.github.io/daiv/dev/features/slash-commands/index.md)        |

## Sandbox

Configure the sandbox for command execution. See [Sandbox](https://srtab.github.io/daiv/dev/features/sandbox/index.md) for a full explanation of what the sandbox does and how the command policy works.

| Option                            | Type            | Default                | Description                             |
| --------------------------------- | --------------- | ---------------------- | --------------------------------------- |
| `sandbox.base_image`              | `str \| null`   | `python:3.12-bookworm` | Docker image. Set to `null` to disable. |
| `sandbox.network_enabled`         | `bool`          | `false`                | Allow network access.                   |
| `sandbox.cpus`                    | `float \| null` | `null`                 | CPU limit.                              |
| `sandbox.memory_bytes`            | `int \| null`   | `null`                 | Memory limit in bytes.                  |
| `sandbox.command_policy.allow`    | `list[str]`     | `[]`                   | Command prefixes to explicitly permit.  |
| `sandbox.command_policy.disallow` | `list[str]`     | `[]`                   | Command prefixes to block.              |

## Branch naming and commit conventions

DAIV generates branch names and commit messages automatically. You can define your conventions in `AGENTS.md`:

```
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

1. **Issue labels** (`daiv-max`) — see [Issue Addressing](https://srtab.github.io/daiv/dev/features/issue-addressing/#max-mode)
1. **`.daiv.yml` models section** — per-repository overrides
1. **Environment variables** — global defaults

### Agent

The main DAIV agent used for issue addressing, pull request assistance, and all interactive tasks.

| Option                        | Type                                               | Default             | Description                               |
| ----------------------------- | -------------------------------------------------- | ------------------- | ----------------------------------------- |
| `models.agent.model`          | `str`                                              | `claude-sonnet-4.6` | Primary model.                            |
| `models.agent.fallback_model` | `str`                                              | `gpt-5.3-codex`     | Fallback if the primary model fails.      |
| `models.agent.thinking_level` | `"minimal" \| "low" \| "medium" \| "high" \| null` | `medium`            | Thinking depth. Set to `null` to disable. |

### Diff to metadata

Generates pull request titles, descriptions, and commit messages from diffs.

| Option                                   | Type  | Default             | Description                          |
| ---------------------------------------- | ----- | ------------------- | ------------------------------------ |
| `models.diff_to_metadata.model`          | `str` | `claude-sonnet-4.6` | Primary model.                       |
| `models.diff_to_metadata.fallback_model` | `str` | `gpt-5.3-codex`     | Fallback if the primary model fails. |

Note

The diff to metadata agent reads your `AGENTS.md` file to follow your branch naming and commit message conventions.
