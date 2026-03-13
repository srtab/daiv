# Sandbox

The sandbox gives DAIV's agent the ability to execute shell commands inside an isolated Docker container. This lets it run tests, install dependencies, execute linters, inspect git history, and perform other operations that require a real shell environment.

The sandbox is powered by [daiv-sandbox](https://github.com/srtab/daiv-sandbox/), a companion service that manages container sessions.

## What the agent can do

With the sandbox enabled, DAIV can:

- Run tests and linters to validate changes
- Install and update dependencies (npm, pip, uv, etc.)
- Inspect git state (`git status`, `git diff`, `git log`)
- Execute build scripts and generators
- Run any command allowed by the command policy

Each sandbox session works on the repository's codebase and persists state across invocations within the same task, so the agent can install a dependency in one step and use it in the next.

## Command policy

Not all commands are safe for an AI agent to run. The sandbox enforces a layered command policy that controls what's allowed.

### Built-in safety rules

The following commands are **always blocked** and cannot be overridden:

| Category | Blocked commands |
|----------|-----------------|
| Git history mutation | `git commit`, `git push`, `git reset`, `git rebase`, `git filter-branch` |
| Git index manipulation | `git add`, `git hash-object`, `git update-index` |
| Destructive operations | `git clean`, `git checkout .`, `git restore .` |
| Branch/tag deletion | `git branch -D`, `git tag -d` |
| Git configuration | `git config` |
| Platform CLI tools | `gitlab`, `gh`, `python -m gitlab` |

These rules exist because DAIV manages git operations (commits, pushes, branches) through its own tools with proper safeguards. The sandbox is for everything else.

### Per-repository rules

You can add custom allow and disallow rules in your `.daiv.yml`:

```yaml
sandbox:
  command_policy:
    allow:
      - "npm install"
      - "pytest"
    disallow:
      - "rm -rf"
```

### Precedence

When a command is evaluated, rules are checked in this order:

1. **Built-in disallow** — always wins, cannot be overridden
2. **Repository disallow** — cannot be overridden by allow
3. **Repository allow** — permits commands not caught by 1 or 2
4. **Default** — everything else is allowed

## Configuration

The sandbox is configured per-repository in `.daiv.yml`:

```yaml
sandbox:
  base_image: "python:3.12-bookworm"  # Docker image (set to null to disable)
  network_enabled: false               # Allow network access
  ephemeral: false                     # Ephemeral sessions
  memory_bytes: 4294967296             # Memory limit (4 GB)
  cpus: 2.0                            # CPU limit
  command_policy:
    allow: []
    disallow: []
```

| Option | Default | Description |
|--------|---------|-------------|
| `base_image` | `python:3.12-bookworm` | Docker image for the sandbox. Set to `null` to disable the sandbox for this repository. |
| `network_enabled` | `false` | Whether the container can access the network. |
| `ephemeral` | `false` | Whether sessions are ephemeral (no state persistence). |
| `memory_bytes` | `null` (unlimited) | Memory limit in bytes. |
| `cpus` | `null` (unlimited) | CPU limit. |
| `command_policy.allow` | `[]` | Commands to explicitly allow. |
| `command_policy.disallow` | `[]` | Commands to explicitly block. |

!!! tip
    Use a custom `base_image` if your project needs specific tooling pre-installed (e.g., `node:18-alpine` for a Node.js project, or your own image with project-specific dependencies).
