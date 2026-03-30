# Slash Commands & Skills

DAIV responds to slash commands in issue and merge/pull request comments. Some are built-in commands with fixed behavior, others are **skills** — modular capabilities that give DAIV specialized expertise for specific tasks.

From your perspective, both work the same way:

```
@daiv /<command> [arguments]
```

Use `/help` to see everything available in the current context.

## Built-in commands

These commands have fixed behavior and are always available:

| Command                     | Scopes          | Description                                                        |
| --------------------------- | --------------- | ------------------------------------------------------------------ |
| `/help`                     | Issues, MRs/PRs | Lists all available commands and skills                            |
| `/agents`                   | Issues, MRs/PRs | Lists available subagents with descriptions                        |
| `/clear`                    | Issues, MRs/PRs | Resets the conversation context to start fresh                     |
| `/clone-to-topics <topics>` | Issues only     | Clones the issue to all repositories matching the specified topics |

### /help

```
@daiv /help
```

Shows all available slash commands **and** skills for the current scope. The output includes both built-in commands and any skills (built-in or custom) available in the repository.

### /agents

```
@daiv /agents
```

Lists the available subagents with their names and descriptions. See [Subagents](https://srtab.github.io/daiv/dev/features/subagents/index.md) for details.

### /clear

```
@daiv /clear
```

Clears the conversation history for the current issue or merge/pull request. Useful when DAIV's context becomes stale or it starts drifting.

### /clone-to-topics

```
@daiv /clone-to-topics backend, api
```

Clones the current issue (title, description, and labels) to all repositories matching **all** the specified topics. The current repository is excluded. Only available on issues.

## Built-in skills

Skills are invoked the same way as commands. DAIV ships with the following built-in skills:

| Skill             | Description                                                                                  |
| ----------------- | -------------------------------------------------------------------------------------------- |
| `/plan`           | Explores the codebase in read-only mode and produces an implementation plan                  |
| `/code-review`    | Reviews a merge/pull request for correctness, tests, performance, security, and architecture |
| `/security-audit` | Audits code for security vulnerabilities, injection flaws, hardcoded secrets, and risks      |
| `/init`           | Analyzes the repository and generates or updates an `AGENTS.md` guidance file                |
| `/skill-creator`  | Guides you through creating a new custom skill                                               |

### /plan

```
@daiv /plan implement rate limiting for the API
```

DAIV explores the codebase without making any changes and posts a detailed implementation plan. This is what runs automatically when an issue is addressed without the `daiv-auto` label (see [Issue Addressing](https://srtab.github.io/daiv/dev/features/issue-addressing/index.md)).

### /code-review

```
@daiv /code-review
```

Reviews the current merge/pull request diff for correctness, test coverage, performance, security issues, and architecture concerns. Posts numbered findings grouped by severity.

### /security-audit

```
@daiv /security-audit
```

Performs a dedicated security scan of the code changes, looking for injection flaws, hardcoded secrets, authentication issues, and data handling risks.

### /init

```
@daiv /init
```

Analyzes the repository structure and generates an `AGENTS.md` file with guidance for AI coding agents, following the recommendations from [the AGENTS.md research paper](https://arxiv.org/abs/2602.11988). If one already exists, it updates it.

### /skill-creator

```
@daiv /skill-creator
```

Walks you through creating a new custom skill for your repository. See [Agent Skills](https://srtab.github.io/daiv/dev/customization/agent-skills/index.md) for more on custom skills.

## Custom commands (skills)

Custom slash commands are implemented as **skills**. Creating a skill automatically registers it as a command — there is no separate registration step.

To create a custom command like `/my-command`:

1. Create `.agents/skills/my-command/SKILL.md` with YAML frontmatter
1. Invoke it with `@daiv /my-command`

Custom commands appear alongside built-in ones in `/help` and are invoked the same way. For the full guide on creating skills, see [Agent Skills](https://srtab.github.io/daiv/dev/customization/agent-skills/index.md).
