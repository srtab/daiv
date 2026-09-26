# Sandbox

The sandbox gives DAIV's agent the ability to execute shell commands inside an isolated Docker container. This lets it run tests, install dependencies, execute linters, inspect git history, and perform other operations that require a real shell environment.

The sandbox is powered by [daiv-sandbox](https://github.com/srtab/daiv-sandbox/), a companion service that manages container sessions.

## What the agent can do

With the sandbox enabled, DAIV can:

- Run tests and linters to validate changes
- Install and update dependencies (npm, pip, uv, etc.) — requires network access, see the note below
- Inspect git state (`git status`, `git diff`, `git log`)
- Execute build scripts and generators
- Run any command allowed by the command policy

Network access is off by default

Installing or updating dependencies reaches out to package registries (npm, PyPI, etc.), which needs network access. The sandbox runs with networking **disabled** by default, so these commands fail until you enable `network_enabled` on the relevant [Sandbox Environment](https://srtab.github.io/daiv/dev/features/sandbox-environments/index.md).

Each sandbox session works on the repository's codebase and persists state across invocations within the same task, so the agent can install a dependency in one step and use it in the next.

## Command policy

Not all commands are safe for an AI agent to run. The sandbox enforces a layered command policy that controls what's allowed.

### Built-in safety rules

The following commands are **always blocked** and cannot be overridden:

| Category               | Blocked commands                                                                                                 |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------- |
| Git history mutation   | `git commit`, `git push`, `git reset`, `git rebase`, `git reflog delete`, `git filter-branch`, `git filter-repo` |
| Git index manipulation | `git add`, `git hash-object`, `git update-index`, `git commit-tree`                                              |
| Destructive operations | `git clean`, `git checkout .`, `git restore .`                                                                   |
| Branch/tag deletion    | `git branch -D` / `git branch --delete`, `git tag -d` / `git tag --delete`                                       |
| Git configuration      | `git config`                                                                                                     |
| Platform CLI tools     | `gitlab`, `gh`, `python -m gitlab`                                                                               |

These rules exist because DAIV manages git operations (commits, pushes, branches) through its own tools with proper safeguards. The sandbox is for everything else.

The policy checks every command in the invocation, including those in chains, pipelines, `if`/`case` statements, loops, subshells and functions, and it looks past leading variable assignments, so `HUSKY=0 git commit` is caught. It does not look inside command or process substitutions (`$(…)`, backticks, `<(…)`), variable expansions (`git $cmd`), `bash -c` or `eval` strings, or wrappers such as `env`, and it compares words exactly as written, so `/usr/bin/git push`, `\git push` and `git "push"` are not caught. Treat it as a guardrail that stops the agent's plain invocations, not as a security boundary.

### Global rules

In addition to the built-in safety rules, operators can block more commands with `DAIV_SANDBOX_COMMAND_POLICY_DISALLOW` (see the [Environment Variables](https://srtab.github.io/daiv/dev/reference/env-variables/index.md) reference). The value is a JSON array, for example `["curl", "npm publish"]`: each entry is a command name followed by any arguments that must appear after it in that order, though not necessarily adjacent, so `npm publish` also blocks `npm run publish`. Matching ignores case and the order of bundled short flags, so `rm -rf` also blocks `rm -fr`. `DAIV_SANDBOX_COMMAND_POLICY_ALLOW` takes the same format but currently has no effect (see [Precedence](#precedence)).

The policy is global: there is no per-repository or per-environment policy. A `sandbox.command_policy` section left in `.daiv.yml` from v2.0.0 is ignored without a warning, so move its `disallow` entries to `DAIV_SANDBOX_COMMAND_POLICY_DISALLOW`.

### Precedence

When a command is evaluated, rules are checked in this order:

1. **Built-in disallow** — always wins, cannot be overridden
1. **Configured disallow** — global `DAIV_SANDBOX_COMMAND_POLICY_DISALLOW` rules; cannot be overridden by allow
1. **Configured allow** — global `DAIV_SANDBOX_COMMAND_POLICY_ALLOW` rules; they allow only what step 4 already allows, so they change nothing
1. **Default** — everything else is allowed

## Configuring the sandbox

Sandbox runtime configuration (`base_image`, `cpus`, `memory`, `network`, and environment variables) is managed through the **Sandbox Environments** admin page rather than `.daiv.yml`. Each environment can be bound to specific repositories (`owner/repo`); when an agent runs in a repo claimed by an environment, that environment is auto-selected. Personal (USER-scoped) environments override organization-wide (GLOBAL-scoped) environments for the running user. See [Sandbox Environments](https://srtab.github.io/daiv/dev/features/sandbox-environments/index.md) for details.
