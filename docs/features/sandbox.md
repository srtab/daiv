---
description: "DAIV runs shell commands in an isolated Docker container powered by daiv-sandbox, to run tests, install dependencies, execute linters and inspect git history."
---

# Sandbox

The sandbox is the isolated runtime where DAIV's agent runs shell commands: tests, linters, dependency installs, build scripts and git inspection. [daiv-sandbox](https://github.com/srtab/daiv-sandbox/), a companion service, starts and manages it. The agent reaches it through the `bash` tool, and a command policy decides which commands it may run.

## What the agent can do

With a sandbox, the agent can:

- Run tests and linters to validate changes.
- Install and update dependencies (npm, pip, uv and others), which needs [network access](#prerequisites).
- Inspect git state (`git status`, `git diff`, `git log`).
- Execute build scripts and generators.
- Run any command that the [command policy](#command-policy) allows.

## Prerequisites

The agent runs commands only when it has a sandbox, and installs only when the sandbox has network access:

- **A base image.** A run has a sandbox only when its [sandbox environment](sandbox-environments.md) sets a base image. When no environment matches the run (none selected, none bound to the repository, and no global default), the run has no sandbox and the agent gets no `bash` tool. A fresh install seeds a global **Default** environment, so a fresh install starts with a sandbox. See [How an environment is resolved](sandbox-environments.md#how-an-environment-is-resolved).
- **Network access for installs.** Installing or updating dependencies reaches package registries such as npm and PyPI. A sandbox has no outbound network access unless its environment has **Network** set to **On** and an egress policy that allows the registries, so these commands fail until you set both. See [Network access and egress policy](sandbox-environments.md#network-access-and-egress-policy).

## Sandbox session lifetime

A sandbox session works on the repository's code and keeps its state between commands, so the agent can install a dependency in one step and use it in the next.

When a run ends, whether it finished, failed or stopped, DAIV stops the sandbox session. The next turn of the same session restarts it, so what the agent installed is still there. The turn starts from a fresh sandbox instead when the session's [environment changed](sandbox-environments.md#change-an-environment) or the sandbox service no longer has the old one.

## Command timeouts

Each command times out after 5 minutes. The agent can give a command it expects to run longer, such as a full test suite or a build, a longer timeout, up to the sandbox timeout (`DAIV_SANDBOX_TIMEOUT`, see [Environment variables](../reference/env-variables.md#sandbox-client-side)). A sandbox timeout below 5 minutes lowers the default too. A timed-out command ends with exit code `124`.

DAIV adds advice to the output of a timed-out command: run it again with a larger timeout when there is room, or narrow it down. With daiv-sandbox 0.5.0 a timed-out command can keep running in the sandbox, so DAIV also tells the agent to stop it before running it again.

## Command policy

Not every command is safe for an AI agent to run. DAIV checks each command against a command policy before it reaches the sandbox. A command DAIV cannot parse is blocked, and the agent gets the reason.

### Built-in rules

DAIV always blocks the following commands. No setting overrides them.

| Category | Blocked commands |
|----------|-----------------|
| Git history mutation | `git commit`, `git push`, `git reset`, `git rebase`, `git merge` (except `git merge --no-commit --no-ff <ref>` with no flag that commits or fast-forwards, and `git merge --abort`), `git pull`, `git cherry-pick`, `git revert`, `git am`, `git reflog delete`, `git filter-branch`, `git filter-repo` |
| Branch switching | `git checkout`, `git switch` |
| Git index manipulation | `git add`, `git stage`, `git hash-object`, `git update-index`, `git read-tree`, `git commit-tree` |
| Destructive operations | `git clean`, `git restore .` |
| Branch and tag deletion | `git branch -D` and `git branch --delete`, `git tag -d` and `git tag --delete` |
| Git configuration | `git config` |
| Platform CLI tools | `gitlab`, `gh`, `python -m gitlab` |

DAIV commits, pushes and manages branches through its own tools, so the sandbox is for everything else. DAIV pushes the branch the run started on, so the agent cannot switch branches and restores files with `git restore <path>` instead of `git checkout`.

The agent can still merge a branch in, for example to resolve a merge request's conflicts with its target branch. It runs `git merge --no-commit --no-ff <ref>` and resolves the conflicts, and DAIV commits the result as a merge commit at the end of the turn, listing the files that conflicted. If a conflicted file still contains conflict markers, DAIV commits and pushes nothing, and says so in its reply.

The policy checks every command in the invocation, including those in chains, pipelines, `if` and `case` statements, loops, subshells and functions. It looks past leading variable assignments, so `HUSKY=0 git commit` is blocked. It does not look inside command or process substitutions (`$(…)`, backticks, `<(…)`), variable expansions (`git $cmd`), `bash -c` or `eval` strings, or wrappers such as `env`, and it compares words exactly as written, so `/usr/bin/git push`, `\git push` and `git "push"` pass. It is a guardrail that stops the agent's plain invocations, not a security boundary.

### Global rules

An operator can block more commands with `DAIV_SANDBOX_COMMAND_POLICY_DISALLOW`, a JSON array such as `["curl", "npm publish"]`. See [Environment variables](../reference/env-variables.md#sandbox-client-side) for the variable. Each entry is a command name followed by the arguments that must appear after it, in that order but not necessarily next to each other, so `npm publish` also blocks `npm run publish`.

- For `git`, the first argument must be the git subcommand itself: `git revert` blocks `git -C repo revert` but not `git log --grep revert`.
- When a git option the policy does not know comes before the subcommand, the rule matches its arguments anywhere, so an unfamiliar option cannot hide a blocked subcommand.
- Matching ignores case and the order of bundled short flags, so `rm -rf` also blocks `rm -fr`.

`DAIV_SANDBOX_COMMAND_POLICY_ALLOW` takes the same format and has no effect (see [Precedence](#precedence)).

The policy is global: there is no per-repository or per-environment policy. DAIV ignores a `sandbox.command_policy` section left in `.daiv.yml` from v2.0.0 without a warning, so move its `disallow` entries to `DAIV_SANDBOX_COMMAND_POLICY_DISALLOW`.

### Precedence

DAIV checks the rules in this order and stops at the first that applies:

1. **Built-in rules** always win and cannot be overridden.
2. **Configured disallow rules** from `DAIV_SANDBOX_COMMAND_POLICY_DISALLOW` cannot be overridden by an allow rule.
3. **Configured allow rules** from `DAIV_SANDBOX_COMMAND_POLICY_ALLOW` allow only what step 4 already allows, so they change nothing.
4. **Default**: every other command is allowed.

## Sandbox configuration

A sandbox environment holds a sandbox's base image, CPUs, memory, network access and environment variables, not `.daiv.yml`. See [Sandbox environments](sandbox-environments.md) to create one and to see how DAIV picks the environment for a run.
