# Research: Configurable Skill and Subagent Models

Each section settles one unknown from the plan's Technical Context. Paths are relative to the
repository root.

## R1. Where the site-level maps live

- **Decision**: Two `JSONField`s on `SiteConfiguration`, `agent_skill_models` and
  `agent_subagent_models`, each a `{name: model spec}` object, default `{}`. They flow through
  `site_settings` like every other field: a default in `_build_field_defaults`, a typed
  `SiteSnapshot` attribute, and the `DAIV_AGENT_SKILL_MODELS` / `DAIV_AGENT_SUBAGENT_MODELS`
  environment variables (JSON objects), parsed the way `_parse_auth_headers_json` parses
  `DAIV_WEB_FETCH_AUTH_HEADERS` (`daiv/core/site_settings.py:37-59`).
- **Rationale**: A run takes one `site_settings.snapshot()` (`daiv/sessions/executor/run.py:363`),
  and `SiteConfiguration.save()` already clears the cache after commit, so a saved map reaches the
  next run and never a run in progress (FR-012) with no new cache. Constitution III requires every
  default to be overridable from the environment, and `_parse_env_value` handles only scalars, so
  the JSON parse is needed.
- **Alternatives considered**: A separate `ModelAssignment` table with a model formset, like
  `WebFetchAuthHeader`. Rejected: it needs its own cache and snapshot plumbing for a map of a few
  entries.
- **Watch**: The snapshot exposes the maps read-only (`MappingProxyType`), because the cached
  defaults dict is shared across callers and a `{}` default could otherwise be mutated in place.

## R2. Dashboard UI

- **Decision**: A skill's entries are set on the skill's own page, which splits into SKILL.md,
  Files, Models and Usage tabs; the code-review detectors are on the code-review page, since their
  charters ship in its `agents/` folder. The `general-purpose` and `explore` subagents belong to no
  skill and stay in the Agent group of site configuration, as two fixed rows. (Revised 2026-11-11:
  first decided as free-form rows of a name and a model in the Agent group.)
- **Layering**: `skills` may not import `configuration`, `sessions` or `automation`. So the
  Models tab is a `configuration` view and the Usage tab a `dashboard` view, both added to the
  `skills` URL namespace in `daiv/urls.py`, and all four tabs share `skills.pages.SkillPageMixin`
  and `skills/skill_page.html`. `skills.services.skill_subagents` reads the detector charters'
  frontmatter for their names, a summary and any declared model.
- **Form**: `configuration.forms.ModelEntriesForm` builds a picker per entry, with the settings
  picker in model-only mode and a "default" hint naming what serves while unset. Only a changed
  entry is validated with `model_spec_error`, so a stale stored entry is kept and flagged rather
  than blocking the save. A map set by its environment variable renders locked.
- **Alternatives considered**:
  - Free-form rows with typed names in the Agent group: they put a skill's model away from the
    skill, and suggested names only the dashboard already knew. A repository-only name is better
    set in that repository's `.daiv.yml`.
  - A JSON textarea: rejected by clarification Q2.

## R3. Repository maps in `.daiv.yml`

- **Decision**: `models.skills` and `models.subagents` on the `Models` pydantic model
  (`daiv/codebase/repo_config.py:134`), typed `dict[str, str | None]` with a default of `{}`.
  Values stay plain strings; they are not `ModelName` and not validated on load.
- **Rationale**: A `ValidationError` makes `get_config` discard the whole file and fall back to
  defaults (`daiv/codebase/repo_config.py:266-274`). Validating a model spec on load would let one
  typo silently disable every other setting in the file. A bad spec is caught at run time instead,
  where FR-009's ERROR log reaches operators.
- **Empty entries**: A `null` or blank value counts as unset, matching how the precedence table
  in `daiv/automation/agent/agent_settings.py` treats a null `models.agent.model`.

## R4. Resolving the maps per run

- **Decision**: `AgentSettings` gains `skill_models` and `subagent_models`, as read-only
  `Mapping[str, str]` fields. `resolve_agent_settings` builds each as the site map overlaid with
  the repository's non-empty entries, and drops `explore` from the subagent map. The module's
  precedence docstring gains row 14.
- **Rationale**: This is the single place that already combines site, repository and run
  (`agent_settings.py:85-104`). The run's overrides (`model_names`, `agent_model`, `use_max`) are
  not consulted, which is clarification Q1: a configured entry beats even an exact pin.

## R5. Applying a configured skill model

- **Decision**: `SkillsMiddleware` takes the run's `skill_models` and the run's model spec. At
  invocation, the skill tool tries the configured spec first, then the skill's `metadata.model`,
  then keeps the run's model. When a configured spec cannot be built, it:
  1. falls through to the file-declared model, or else the run's model;
  2. logs at ERROR, naming the entry and the spec (R7).

  A call the skill model fails goes to the run's fallback models: the model swap sits inside
  `ModelFallbackMiddleware`, and a fallback model's call passes through it unchanged.

  The prototype's lifetime (until the next user follow-up), the context-window guard and the
  per-run model cache stay as they are (`daiv/automation/agent/middlewares/skills.py:243-267`).
- **Watch**: The prototype logs an invalid spec at WARNING. For a configured entry the constitution
  needs ERROR (`constitutions/patterns/errors.md` rule 1). The WARNING level stays only for a
  model declared in a skill file.

## R6. Applying a configured subagent model

- **Decision**: Subagent models are built when the agent is assembled (`create_daiv_agent`,
  `daiv/automation/agent/graph.py:216-270`), so they are resolved there, by name, through one
  helper. The helper builds the configured spec with the run's thinking level
  (`chain.thinking_level`) and, on failure, logs at ERROR and returns nothing, so the caller uses
  its existing default. It applies to:
  - `general-purpose`: configured entry, else the run's model;
  - code-review detectors (`load_builtin_code_review_detectors`): configured entry, else the
    charter's `model:`, else the run's model;
  - repository subagents (`load_custom_subagents`): configured entry, else the file's `model:`,
    else the run's model.
- **Behaviour kept**: Without a configured entry, an unusable model declared in a file still skips
  that subagent, as today (`daiv/automation/agent/subagents.py:248-262`, `:616-622`). When a
  configured entry fails and the file's model fails too, the run's model serves, so a subagent is
  never left out because its configured model is unusable (spec Edge Cases).
- **Rationale**: A subagent's own stack builds its summarization from its own model
  (`_shared_subagent_middleware`), so a configured subagent model needs no context-window guard.
  Fallback models stay the run's (FR-007).

## R7. Reporting a model that can't be used

- **Decision**: One ERROR log per spec per run, naming the target kind, the spec and the skill or
  subagent it was for, with the traceback. The run posts nothing extra to the merge or pull
  request. A configured model whose call fails gets no handling of its own: every stack already
  has `ModelFallbackMiddleware`, so the run's fallback models serve the call.
- **Rationale**: The operator who can fix the entry reads the logs and Sentry, and ERROR records
  reach both. Each configured spec is built once per run, so the log is not repeated within a run.
- **Watch**: Subagents are built when the agent is assembled, so a subagent entry that can't be
  built is logged by every run, whether or not the run dispatches that subagent. With no fallback
  models configured, a call the configured model fails errors like any other model call.
- **Alternatives considered**:
  - A notice persisted on the run and shown as a chip in the session view. Built, then removed on
    2026-11-11: it needed a `Run` field and migration, a recorder injected across the
    `automation`/`sessions` boundary, pseudo-turns in three transcript paths, client changes, and a
    middleware to delay a subagent's notice until it was dispatched.
  - A failover from a failing configured model to the model used with no entry, ahead of the
    fallback models. Built, then removed with the notices: it added a middleware to every subagent
    stack and per-run failure state for an ordering the fallback models already cover.

## R8. Usage attribution

- **Finding**: Usage is already keyed by the model that served each call
  (`daiv/automation/agent/usage_tracking.py:212-241`) and shown per model in the session details
  (`chat/_usage_body.html`). FR-013 is met without changes.
- **SC-005**: Met by per-model usage: the run's usage lists every model that served a call.
  Telling apart a skill and a subagent that run on the same model is out of scope.

## R9. Measurement

- **Decision**: With no entries configured, prompts, tools and model choice are unchanged, so this
  is not an agent-behaviour change under `constitutions/patterns/agent-evals.md`. The pull request
  declares it unmeasured. Comparing models for `/code-review` remains a ReviewBench exercise
  (`evals/reviewbench.py --model-name`), outside this feature.
