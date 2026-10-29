# Contract: Site configuration model maps

## Storage and environment

| Setting | Environment override | Value |
|---------|----------------------|-------|
| `agent_skill_models` | `DAIV_AGENT_SKILL_MODELS` | JSON object `{"<skill>": "<provider:model>"}` |
| `agent_subagent_models` | `DAIV_AGENT_SUBAGENT_MODELS` | JSON object `{"<subagent>": "<provider:model>"}` |

- When the environment variable is set, it wins and the dashboard section is read-only.
- A malformed variable is logged at ERROR and treated as `{}`.
- A saved change applies to runs that start after the commit.

## Dashboard: Agent group, "Skill and subagent models" section

Two lists of rows, one for skills and one for subagents. Each row has:

- **Name**: a text input. It suggests `list_global_skills()` names for skills, and the built-in
  subagents except `explore` for subagents. Any text is accepted.
- **Model**: the agent picker, without the thinking-effort row, limited to enabled providers.

Rows can be added and removed.

| Input | Result on save |
|-------|----------------|
| A row with a valid name and spec | Saved |
| A spec whose provider is unknown, disabled or has no API key | Not saved; error on that row naming the entry |
| A blank name, or a duplicate name in the same list | Not saved; error on that row |
| A subagent row named `explore` | Not saved; error pointing to the Explore model setting |
| A name outside the suggestions | Saved; warning message after save naming the entry |
| A row with a name and no model | Not saved; error on that row |
