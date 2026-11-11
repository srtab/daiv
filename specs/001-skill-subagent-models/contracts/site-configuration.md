# Contract: Site configuration model maps

## Storage and environment

| Setting | Environment override | Value |
|---------|----------------------|-------|
| `agent_skill_models` | `DAIV_AGENT_SKILL_MODELS` | JSON object `{"<skill>": "<provider:model>"}` |
| `agent_subagent_models` | `DAIV_AGENT_SUBAGENT_MODELS` | JSON object `{"<subagent>": "<provider:model>"}` |

- When the environment variable is set, it wins and the dashboard section is read-only.
- A malformed variable is logged at ERROR and treated as `{}`.
- A saved change applies to runs that start after the commit.

## Dashboard

A skill's page (`/dashboard/skills/<name>/`) has four tabs, each its own URL: SKILL.md, Files,
Models (`models/`) and Usage (`usage/`). The Models tab holds the skill's `agent_skill_models` entry
and, for code-review, an `agent_subagent_models` entry per detector. The Agent group of site
configuration holds the `general-purpose` and `explore` entries.

Each row is one entry: the agent picker, without the thinking-effort row and limited to enabled
providers, beside the name of what serves while the entry is unset. Saving writes the changed
entries and keeps every other entry of the map.

| Input | Result on save |
|-------|----------------|
| A changed entry with a usable spec | Saved |
| A changed entry whose provider is unknown, disabled or has no API key | Not saved; error on that row |
| An entry cleared with "Use default" | Removed from the map |
| An unchanged entry whose provider became unusable | Kept; the row is flagged |
| A map set by its environment variable | Its rows are read-only and keep the environment's value |
