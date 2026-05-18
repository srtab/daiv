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

Custom allow and disallow rules were previously declared in `.daiv.yml`. Per-environment
command policies are a future iteration; for now, only the built-in safety rules apply.

### Precedence

When a command is evaluated, rules are checked in this order:

1. **Built-in disallow** — always wins, cannot be overridden
2. **Repository disallow** — cannot be overridden by allow
3. **Repository allow** — permits commands not caught by 1 or 2
4. **Default** — everything else is allowed

## Configuring the sandbox

Sandbox runtime configuration (`base_image`, `cpus`, `memory`, `network`, and
environment variables) is managed through the **Sandbox Environments** admin
page rather than `.daiv.yml`. Each environment can be bound to specific
repositories (`owner/repo`); when an agent runs in a repo claimed by an
environment, that environment is auto-selected. Personal (USER-scoped)
environments override organization-wide (GLOBAL-scoped) environments for the
running user. See the Sandbox Environments page for details.
