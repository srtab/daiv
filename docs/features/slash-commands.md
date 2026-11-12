---
description: "Slash commands and skills DAIV responds to in issue and merge or pull request comments, including /plan and /code-review, plus your own custom skills."
---

# Slash commands and skills

DAIV responds to slash commands in issue and merge or pull request comments, and some also work in the dashboard chat. Built-in commands have fixed behavior. **Skills** are packaged instructions that give DAIV expertise for a specific task. You invoke both the same way:

```text
@daiv /<command> [arguments]
```

Use `/help` to list everything available where you are.

## Built-in commands

These commands have fixed behavior and are always available:

| Command | Scopes | Description |
|---------|--------|-------------|
| `/help` | Issues, MRs/PRs, dashboard chat | Lists all available commands and skills |
| `/agents` | Issues, MRs/PRs, dashboard chat | Lists available subagents with descriptions |
| `/clear` | Issues, MRs/PRs | Resets the conversation context to start fresh |

### /help

```text
@daiv /help
```

Lists the slash commands and skills available in the current scope: the built-in commands, and the built-in and custom skills of the repository.

### /agents

```text
@daiv /agents
```

Lists the available subagents with their names and descriptions. See [Subagents](subagents.md) for details.

### /clear

```text
@daiv /clear
```

Starts a fresh conversation context for the current issue or merge/pull request. Use it when DAIV's context is stale or DAIV starts drifting. The earlier conversation stays in the session's history; DAIV stops seeing it.

### In the dashboard chat

`/help` and `/agents` are also registered for the global scope, so they work in the dashboard chat too. Typing `/` in the chat composer opens a list of suggestions with those commands and the global skills. Per-repository skills do not appear there: they live in the repository's sandbox, which DAIV cannot read while the page renders.

## Built-in skills

DAIV ships with these built-in skills:

| Skill | Description |
|-------|-------------|
| `/plan` | Explores the codebase in read-only mode and produces an implementation plan |
| `/code-review` | Reviews a merge/pull request for correctness, security, performance, structure, and your repository's custom review rules |
| `/security-audit` | Audits code for security vulnerabilities, injection flaws, hardcoded secrets, and risks |
| `/init` | Analyzes the repository and generates or updates an `AGENTS.md` guidance file |
| `/skill-creator` | Guides you through creating a new custom skill |

### /plan

```text
@daiv /plan implement rate limiting for the API
```

DAIV explores the codebase without making any changes and posts a detailed implementation plan. DAIV runs it automatically when it addresses an issue without the `daiv-auto` label (see [Issue addressing](issue-addressing.md)).

### /code-review

```text
@daiv /code-review
```

Reviews the current merge/pull request diff for correctness, security, performance, structure, and your repository's [custom review rules](pull-request-assistant.md#custom-review-rules). Posts numbered findings grouped by severity, as a single report comment.

How it scores on the ReviewBench benchmark, and at what cost, is on the [Benchmarks](../reference/benchmarks.md) page.

### /security-audit

```text
@daiv /security-audit
```

Performs a dedicated security scan of the code changes, looking for injection flaws, hardcoded secrets, authentication issues, and data handling risks.

### /init

```text
@daiv /init
```

Analyzes the repository structure and generates an `AGENTS.md` file: short, repository-specific guidance for AI coding agents, covering verified commands, a repository map, invariants and pitfalls, and where changes usually go. If the file already exists, `/init` updates it to cut duplication and low-signal text.

### /skill-creator

```text
@daiv /skill-creator
```

Walks you through creating a custom skill for your repository. See [Agent skills](../customization/agent-skills.md) for more on custom skills.

## Create a custom command

A custom slash command is a skill: creating the skill registers the command, with no separate step. To create a command named `/my-command`:

1. Create `.agents/skills/my-command/SKILL.md`, starting with YAML frontmatter.
2. In a comment, invoke it with `@daiv /my-command`.

The command then appears in `/help` next to the built-in ones. [Agent skills](../customization/agent-skills.md) explains how to write a skill.
