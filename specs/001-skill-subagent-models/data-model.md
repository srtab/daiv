# Data Model: Configurable Skill and Subagent Models

## SiteConfiguration (existing model, two new fields)

| Field | Type | Default | Rules |
|-------|------|---------|-------|
| `agent_skill_models` | JSON object `{skill name: model spec}` | `{}` | Keys are non-blank and unique. Each value parses as a model spec whose provider is configured, enabled and has an API key (FR-008a). An unknown key is saved with a warning. |
| `agent_subagent_models` | JSON object `{subagent name: model spec}` | `{}` | Same rules as above. |

Both fields can be overridden from the environment with `DAIV_AGENT_SKILL_MODELS` and
`DAIV_AGENT_SUBAGENT_MODELS` (a JSON object). While a variable is set, its section is read-only.
The `SiteSnapshot` exposes each map read-only.

## Repository configuration `models` section (existing pydantic model, two new keys)

| Key | Type | Default | Rules |
|-----|------|---------|-------|
| `models.skills` | `dict[str, str \| None]` | `{}` | Values are not validated on load. A `null` or blank value counts as unset. |
| `models.subagents` | `dict[str, str \| None]` | `{}` | Same as above. |

## AgentSettings (existing per-run value, two new fields)

| Field | Type | Built from |
|-------|------|------------|
| `skill_models` | read-only `Mapping[str, str]` | `site.agent_skill_models` overlaid with the repository's non-empty `models.skills` entries |
| `subagent_models` | read-only `Mapping[str, str]` | `site.agent_subagent_models` overlaid with the repository's non-empty `models.subagents` |

The run's overrides (exact pin, picker choice, Max) do not affect either map.

**Resolution for one name**, first usable wins:

1. the `AgentSettings` entry;
2. the model the skill or subagent file declares;
3. the model it uses today: the run's model, or for a skill the model already serving the
   conversation.

An unusable entry at step 1 falls through to step 2 and is logged at ERROR. An unusable
file-declared model behaves as it does today: a skill keeps the run's model, and a subagent is
skipped, unless a configured entry also failed, in which case the run's model serves.
