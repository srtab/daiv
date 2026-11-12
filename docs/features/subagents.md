---
description: "How DAIV's main agent delegates to specialized subagents for fast codebase exploration and complex multi-step tasks, and how to list them with /agents."
---

# Subagents

DAIV's main agent can delegate work to subagents. Each subagent is specialized for one kind of task: one explores the codebase quickly, another handles complex multi-step work. The main agent decides when to delegate, based on the task.

To see the subagents available in a repository, run [`/agents`](slash-commands.md#agents). It works in the dashboard chat and on any issue or merge or pull request. The list holds every available subagent: the two built-in subagents below, the code-review detectors, and any custom subagents the repository defines.

## Built-in subagents

### General-purpose

The general-purpose subagent researches complex questions, searches for code, and carries out multi-step tasks. It has the main agent's tools:

- Filesystem operations (read, search, edit)
- Git platform tools (GitLab or GitHub)
- Web search and fetch, when enabled
- Sandbox command execution, when the run has a [sandbox](sandbox.md)
- Task tracking
- The MCP tools the main agent has

The main agent delegates to it when a task needs several rounds of searching and reasoning, for example investigating a bug across several files or researching how an external library works before making changes.

### Explore

The explore subagent is a fast, read-only agent for navigating a codebase. Its tools list directories, read files, find files by glob pattern (for example `src/components/**/*.tsx`), and search code for keywords and regular expressions. It cannot modify files, run commands, or reach external resources, which keeps it fast and safe for quick lookups.

The main agent delegates to it to locate files, understand code structure, or gather context before deciding. The main agent gives it a thoroughness level: `quick`, `medium` or `very thorough`. Explore runs on `openrouter:anthropic/claude-haiku-5.5` unless a [configured model](#subagent-model-precedence) replaces it.

### Code-review detectors

The `/code-review` skill delegates to read-only detector subagents: `cr-correctness`, `cr-security`, `cr-performance`, `cr-structure`, `cr-tests` and `cr-custom-rules`. See [Agent Architecture](../reference/agent-architecture.md) for how they run.

## How delegation works

The main agent chooses a subagent from the task:

- To find a file or understand code structure, it uses the explore subagent.
- To research, run commands, or do multi-step work, it uses the general-purpose subagent.
- To make code changes directly, it does the work itself.

Each subagent runs in its own isolated context. It does not see the main conversation and cannot exchange messages with the main agent. It works on its own and returns a single final report, which the main agent uses to continue the task.

## Add a custom subagent

A custom subagent is a Markdown file in the repository. You need write access to the repository.

1. In the repository root, create the directory `.agents/subagents/` if it does not exist.
2. In it, create one `.md` file for the subagent. The file name is free: the subagent's name comes from the file's `name` field.
3. Start the file with YAML frontmatter that sets `name` and `description`.
4. Optional: set `model` in the frontmatter to run the subagent on a model of your choice.
5. After the frontmatter, write the subagent's system prompt in Markdown.
6. Commit the file to the repository.
7. On the next run, send `/agents` to check that the subagent is listed.

```text
my-repository/
├── .agents/
│   └── subagents/
│       ├── database-migration.md
│       └── data-analyst.md
└── src/
```

A subagent file looks like this:

```markdown
---
name: database-migration
description: Specialized agent for planning and reviewing database migrations. Use when the task involves schema changes, data migrations, or ORM model modifications.
model: openrouter:anthropic/claude-sonnet-5.5
---

You are a database migration specialist. When given a task:

1. Review the current schema by reading model files
2. Identify all affected tables and relationships
3. Plan the migration steps in order
4. Check for data loss risks
5. Suggest rollback strategies
```

A custom subagent has the same tools as the built-in [general-purpose subagent](#general-purpose).

## Custom subagent file format

DAIV reads the `.md` files directly inside `.agents/subagents/`, not those in subdirectories. It skips a file that breaks the rules below and logs a warning, and the subagent does not appear in `/agents`.

| Part | Required | Description |
|------|----------|-------------|
| `name` | Yes | The subagent's unique name, which the main agent uses to delegate. It cannot be `general-purpose`, `explore`, or a code-review detector's name such as `cr-security`: DAIV skips a file that uses one. |
| `description` | Yes | What the subagent does. The main agent reads it to decide when to delegate, so make it specific and include trigger phrases. |
| `model` | No | The model to run on, as `provider:model-name`, for example `openrouter:anthropic/claude-haiku-5.5`. Without it, the subagent runs on the main agent's model. A [configured model](#subagent-model-precedence) takes precedence. If DAIV cannot build the model and none is configured, it skips the subagent. |
| Body | Yes | The Markdown after the frontmatter becomes the subagent's system prompt. An empty body makes DAIV skip the file. |

DAIV ignores any other frontmatter key.

## Write a subagent description

The `description` is how the main agent decides whether to delegate to your subagent. Include specific trigger phrases:

```yaml
# Good: specific triggers the agent can match
description: >
  Specialized agent for API endpoint design and implementation.
  Use when the task involves creating new REST endpoints, modifying
  API responses, or designing request/response schemas.

# Bad: too vague
description: Helps with API stuff.
```

## Subagent model precedence

Operators can run any subagent on a chosen model by name, without editing its file: the general-purpose and explore subagents, each code-review detector, and custom subagents. A custom subagent's file lives in the repository, so the dashboard does not list it. Set its model in `.daiv.yml`, or for every repository in `DAIV_AGENT_SUBAGENT_MODELS`.

A subagent's model is the first of these that is set:

1. Its entry under `models.subagents` in the repository's `.daiv.yml` (see [Model overrides](../customization/repository-config.md#skill-and-subagent-models)).
2. Its entry in the dashboard or in `DAIV_AGENT_SUBAGENT_MODELS`. For the general-purpose and explore subagents, it is under **Configuration → Agent → Subagent models**. For the detectors, it is on the **Models** tab of the `code-review` skill's page.
3. The `model` that its file or detector charter declares (`openrouter:anthropic/claude-haiku-5.5` for explore).
4. The main agent's model.

A configured model runs with the run's thinking level and falls back to the main agent's fallback models. A model that the user picks for the run does not override an entry, and a skill's model never carries over to a subagent it delegates to.

When an entry names a provider that is unknown, disabled or missing its API key, the subagent runs on its file's `model`, else on the main agent's model, so it is never left out of the run. DAIV logs the failure at ERROR. When a call on the configured model fails, for example because the provider does not know the model name, the main agent's fallback models serve it, as for any other model.

## Related pages

- [Agent Architecture](../reference/agent-architecture.md): which model each subagent runs, its tool stack, and its fallback behaviour
- [Agent Skills](../customization/agent-skills.md): author the reusable instruction packages the main agent loads from `.agents/skills/`
- [Slash commands and skills](slash-commands.md): run `/agents` to list every subagent available in a repository
