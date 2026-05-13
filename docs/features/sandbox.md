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
| `memory_bytes` | `null` (unlimited) | Memory limit in bytes. |
| `cpus` | `null` (unlimited) | CPU limit. |
| `command_policy.allow` | `[]` | Commands to explicitly allow. |
| `command_policy.disallow` | `[]` | Commands to explicitly block. |

## Custom base image

The `base_image` option is one of the most powerful sandbox settings. By providing your own Docker image, you give the agent access to your project's exact toolchain — the same runtimes, package managers, and utilities your team uses every day.

### Requirements

Your base image **must include `git`**. The sandbox mounts the repository and the agent relies on git to inspect the codebase (e.g., `git diff`, `git log`, `git status`). Without it, many sandbox operations will fail.

### Examples

| Project type | Recommended image | Why |
|---|---|---|
| Python | `python:3.12-bookworm` (default) | Includes pip, git, and common build tools |
| Node.js | `node:22-bookworm` | Includes npm/npx and git |
| Go | `golang:1.23-bookworm` | Includes go toolchain and git |
| Multi-language | Your own image | Pre-install all runtimes and dependencies |

### Building your own image

For the best experience, create an image with your project's dependencies pre-installed. This avoids the agent spending time on `npm install` or `pip install` on every task.

```dockerfile
FROM python:3.12-bookworm

# Ensure git is available
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# Pre-install project dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt
```

Then reference it in `.daiv.yml`:

```yaml
sandbox:
  base_image: "your-registry.example.com/your-project-sandbox:latest"
```

!!! tip
    Pre-installing dependencies in your image makes the agent significantly faster — it can jump straight into running tests or builds instead of waiting for package installation.
