# Data Model: Configurable Skill and Subagent Models

## SiteConfiguration (existing model, two new fields)

| Field | Type | Default | Rules |
|-------|------|---------|-------|
| `agent_skill_models` | JSON object `{skill name: model spec}` | `{}` | Keys are non-blank and unique. Each value parses as a model spec whose provider is configured, enabled and has an API key (FR-008a). An unknown key is saved with a warning. |
| `agent_subagent_models` | JSON object `{subagent name: model spec}` | `{}` | Same rules as above; the key `explore` is refused. |

Both fields can be overridden from the environment with `DAIV_AGENT_SKILL_MODELS` and
`DAIV_AGENT_SUBAGENT_MODELS` (a JSON object). While a variable is set, its section is read-only.
The `SiteSnapshot` exposes each map read-only.

## Repository configuration `models` section (existing pydantic model, two new keys)

| Key | Type | Default | Rules |
|-----|------|---------|-------|
| `models.skills` | `dict[str, str \| None]` | `{}` | Values are not validated on load. A `null` or blank value counts as unset. |
| `models.subagents` | `dict[str, str \| None]` | `{}` | Same as above. An `explore` key is ignored when the run's settings are resolved. |

## AgentSettings (existing per-run value, two new fields)

| Field | Type | Built from |
|-------|------|------------|
| `skill_models` | read-only `Mapping[str, str]` | `site.agent_skill_models` overlaid with the repository's non-empty `models.skills` entries |
| `subagent_models` | read-only `Mapping[str, str]` | `site.agent_subagent_models` overlaid with the repository's non-empty `models.subagents`, without `explore` |

The run's overrides (exact pin, picker choice, Max) do not affect either map.

**Resolution for one name**, first usable wins:

1. the `AgentSettings` entry;
2. the model the skill or subagent file declares;
3. the model it uses today: the run's model, or for a skill the model already serving the
   conversation.

An unusable entry at step 1 falls through to step 2 and records a notice naming the model that
served. An unusable file-declared model behaves as it does today: a skill keeps the run's model,
and a subagent is skipped, unless a configured entry also failed, in which case the run's model
serves.

## Run (existing model, one new field)

| Field | Type | Default |
|-------|------|---------|
| `notices` | JSON list of notice objects | `[]` |

**ModelFallbackNotice** (one list item):

| Key | Type | Meaning |
|-----|------|---------|
| `kind` | `"model_fallback"` | Notice type; the only one in this release |
| `target` | `"skill"` \| `"subagent"` | What the entry was for |
| `name` | string | The skill or subagent name |
| `configured_model` | string | The spec that could not be used |
| `served_model` | string | The spec that served instead |
| `reason` | `"unknown_provider"` \| `"provider_unavailable"` \| `"init_failed"` | Category only, never exception text |

Notices are appended during the run and kept after it ends. A skill's notice is recorded once per
run per entry, matching the per-run model cache.

## Pseudo-turn `run_notice` (session view)

`sessions.transcript` turns each notice into one item, placed next to its run's turns:

```text
{id: "run-notice-<run id>-<index>", role: "run_notice", message: <rendered sentence>}
```

The message reads "<name> ran on <served_model>: <configured_model> could not be used
(<reason label>)."
