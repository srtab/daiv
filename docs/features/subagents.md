# Subagents

DAIV's main agent can delegate work to specialized subagents. Each subagent is optimized for a specific type of task — one for fast codebase exploration, another for complex multi-step work. The main agent decides when to delegate based on the nature of the task.

You can see the available subagents by running `/agents` — it works in the global/chat scope as well as on any issue or merge/pull request. The output lists every available subagent, so alongside the two built-ins described below you may also see the code-review detectors and any custom subagents defined for the repository.

## Available subagents

### General-purpose

A full-capability agent for researching complex questions, searching for code, and executing multi-step tasks. It has access to the same tools as the main agent:

- Filesystem operations (read, search, edit)
- Git platform tools (GitLab/GitHub)
- Web search and fetch
- Sandbox command execution
- Task tracking

The main agent delegates to the general-purpose subagent when a task requires multiple rounds of searching and reasoning — for example, investigating a bug across several files or researching how an external library works before making changes.

### Explore

A fast, read-only agent specialized for codebase navigation. It can:

- Find files by glob patterns (e.g., `src/components/**/*.tsx`)
- Search code for keywords and regex patterns
- Read and analyze file contents

It **cannot** modify files, run commands, or access external resources. This constraint makes it fast and safe for quick lookups.

The main agent delegates to the explore subagent when it needs to quickly locate files, understand code structure, or gather context before making decisions. You can control the depth of exploration with thoroughness levels: "quick", "medium", or "very thorough".

## How delegation works

The main agent chooses which subagent to use based on the task:

- **Need to find a file or understand code structure?** → Explore subagent
- **Need to research, run commands, or do multi-step work?** → General-purpose subagent
- **Need to make code changes directly?** → Main agent handles it itself

Each subagent runs in its own isolated context — it does not see the main conversation and cannot exchange messages with the main agent. It works autonomously and returns a single final report, which the main agent uses to continue the task.

## Custom subagents

You can define your own specialized subagents on a per-repository basis. Custom subagents are markdown files stored in `.agents/subagents/` at the repository root — one file per subagent.

```
your-repository/
├── .agents/
│   └── subagents/
│       ├── my-agent.md
│       └── data-analyst.md
└── src/
```

### File format

Each `.md` file contains YAML frontmatter with the subagent's metadata, followed by a markdown body that becomes the subagent's system prompt:

```markdown
---
name: database-migration
description: Specialized agent for planning and reviewing database migrations. Use when the task involves schema changes, data migrations, or ORM model modifications.
model: openrouter:anthropic/claude-sonnet-4.6  # optional
---

You are a database migration specialist. When given a task:

1. Review the current schema by reading model files
2. Identify all affected tables and relationships
3. Plan the migration steps in order
4. Check for data loss risks
5. Suggest rollback strategies
```

### Required fields

| Field | Description |
|-------|-------------|
| `name` | Unique identifier for the subagent. The main agent uses this when delegating tasks. |
| `description` | What this subagent does. The main agent uses this to decide when to delegate — be specific and include trigger phrases. |

The markdown body (after the frontmatter) is **required** and becomes the subagent's system prompt.

### Optional fields

| Field | Description |
|-------|-------------|
| `model` | Override the model used by this subagent. Use the `provider:model-name` format (e.g., `openrouter:anthropic/claude-haiku-4.5`). If not specified, the main agent's model is used. A [configured subagent model](#choosing-a-subagents-model) takes precedence. |

### Capabilities

Custom subagents have the same capabilities as the built-in general-purpose subagent:

- Filesystem operations (read, search, edit)
- Git platform tools (GitLab/GitHub)
- Web search and fetch (if enabled)
- Sandbox command execution (if enabled)
- Task tracking

### Writing a good description

The `description` field is how the main agent decides whether to delegate to your subagent. Include specific trigger phrases:

```yaml
# Good — specific triggers the agent can match
description: >
  Specialized agent for API endpoint design and implementation.
  Use when the task involves creating new REST endpoints, modifying
  API responses, or designing request/response schemas.

# Bad — too vague
description: Helps with API stuff.
```

## Choosing a subagent's model

Operators can run any subagent on a chosen model by name, without editing its file: the general-purpose and explore subagents, each code-review detector (`cr-security`, `cr-tests`, …) and custom subagents. A subagent's model is the first of these that is set:

1. its entry under `models.subagents` in the repository's `.daiv.yml` (see [Model overrides](../customization/repository-config.md#skill-and-subagent-models));
2. its entry in site configuration, under **Agent → Skill and subagent models** (or `DAIV_AGENT_SUBAGENT_MODELS`);
3. the `model` its file or detector charter declares (Claude Haiku 5.5 for explore);
4. the main agent's model.

A configured model runs with the run's thinking level and falls back to the main agent's fallback models. A model the user picks for the run does not override an entry, and a skill's model never carries over to a subagent it delegates to.

When an entry names a provider that is unknown, disabled or missing its API key, the subagent runs on its file's `model`, else the main agent's model; it is never left out of the run. When the subagent is dispatched, the failure is logged at ERROR, and the session view shows a notice naming the subagent, the model that could not be used and the model that served.

## Related pages

- [Agent Architecture](../reference/agent-architecture.md) — which model each subagent runs, its tool stack, and its fallback behaviour
- [Agent Skills](../customization/agent-skills.md) — author the reusable instruction packages the main agent loads from `.agents/skills/`
- [Slash Commands & Skills](slash-commands.md) — run `/agents` to list every subagent available in a repository
