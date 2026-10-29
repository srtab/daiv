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

## R2. Site configuration UI

- **Decision**: The fields match the Agent group's `agent_*` pattern but stay out of
  `SiteConfigurationForm.Meta.fields`, so the generic field loop skips them. They are rendered as a
  custom section in the Agent group, through the group-specific include hook in
  `configuration/site_configuration_group.html:61-69`. Each map is a plain `formset_factory` of
  rows, folded into the JSON object on save, following the MCP headers precedent
  (`daiv/mcp_connectors/forms.py:200-279`) and the shared `djangoFormset` component
  (`daiv/core/static/core/js/django-formset.js`). Each row has:
  - a name `<input>` with a `<datalist>` of suggestions: `skills.services.list_global_skills()`
    for skills (built-in and custom global, `daiv/skills/services.py:202`), and
    `BUILTIN_SUBAGENT_NAMES` minus `explore` for subagents;
  - the reusable picker `automation/_agent_picker.html` with `with_effort=False` (model only),
    named after the row's formset prefix, so its provider list is already limited to enabled
    `Provider` rows.
- **Validation**: Each row's spec goes through the same check as `_validate_model_api_keys`
  (`daiv/configuration/forms.py:472-496`): `parse_model_spec`, then an enabled provider row with
  an API key. That check is extracted into a helper so both paths share it. An invalid row is a
  form error naming the entry (FR-008a). A name outside the suggestions is accepted and reported
  with `messages.warning` after save, the `_collect_provider_warnings` precedent
  (`daiv/configuration/views.py:93-99`). A subagent entry named `explore` is refused, because
  explore keeps its own setting (spec Assumptions). Duplicate names in one map are refused.
- **Env lock**: When the environment variable is set, the section renders read-only, like the
  web-fetch auth headers section (`daiv/configuration/views.py:116-127`).
- **Alternatives considered**:
  - A JSON textarea: rejected by clarification Q2.
  - Free-text rows without a picker (Q2 option B): rejected for the same reason.

## R3. Repository maps in `.daiv.yml`

- **Decision**: `models.skills` and `models.subagents` on the `Models` pydantic model
  (`daiv/codebase/repo_config.py:134`), typed `dict[str, str | None]` with a default of `{}`.
  Values stay plain strings; they are not `ModelName` and not validated on load.
- **Rationale**: A `ValidationError` makes `get_config` discard the whole file and fall back to
  defaults (`daiv/codebase/repo_config.py:266-274`). Validating a model spec on load would let one
  typo silently disable every other setting in the file. A bad spec is caught at run time instead,
  where FR-009's notice reaches the maintainer.
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
  2. logs at ERROR, naming the entry, the reason category and the model that served;
  3. records a notice (R7).

  The prototype's lifetime (until the next user follow-up), the context-window guard and the
  per-run model cache stay as they are (`daiv/automation/agent/middlewares/skills.py:243-267`).
- **Watch**: The prototype logs an invalid spec at WARNING. For a configured entry the constitution
  needs ERROR (`constitutions/patterns/errors.md` rule 1). The WARNING level stays only for a
  model declared in a skill file.

## R6. Applying a configured subagent model

- **Decision**: Subagent models are built when the agent is assembled (`create_daiv_agent`,
  `daiv/automation/agent/graph.py:216-270`), so they are resolved there, by name, through one
  helper. The helper builds the configured spec with the run's thinking level
  (`chain.thinking_level`) and, on failure, returns the reason category. The caller then uses its
  existing default and reports the fallback, which logs at ERROR and records a notice naming the
  model that served. It applies to:
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

## R7. The notice on a run

- **Findings**: Nothing in DAIV persists a notice on a run today.
  - `Run` (`daiv/sessions/models.py:277-366`) has no notices field.
  - The only persisted pseudo-turn is the failed or aborted run-status marker built by
    `transcript._marker` (`daiv/sessions/transcript.py:46-67`) and rendered as a chip in
    `sessions/session_detail.html:202-215`.
  - Custom events (`adispatch_custom_event`) are live-only. They also cannot be sent while the
    agent is being built, which is outside any runnable (`daiv/sessions/executor/run.py:376`).
- **Decision**:
  - **Storage:** a new `Run.notices` `JSONField` (a list, default `[]`).
  - **Recording:** a `RunNotices` protocol in `automation` with a synchronous `record(notice)`,
    which buffers, and an async `aflush()`, which saves. `record` is synchronous because the
    detector loader is synchronous. `create_daiv_agent` takes an optional `notices` collaborator,
    like `session_links` and `delegator`, and the executor passes a sessions-side recorder bound to
    the run id.
  - **Saving:** `aflush()` writes the whole buffered list with
    `Run.objects.filter(pk=...).aupdate(notices=...)`. It is called after the agent is built and
    after a skill records a notice, so a notice survives a reload while the run is still going.
  - **Display:** `transcript.py` emits one `run_notice` pseudo-turn per notice next to the run's
    turns, and the session template renders it as a neutral chip. The chip reuses the run-status
    chip styles in a new `--notice` variant, following `DESIGN.md`.
- **Rationale**: `automation` (layer 6) cannot import `sessions` (layer 8), so writing to `Run` goes
  through an injected collaborator, which is the existing extension point
  (`constitutions/architecture/layering.md` rule 5). No new `ignore_imports` is needed.
- **Disclosure**: A notice holds the target kind, name, configured spec, the spec that served, and
  a reason category: `unknown_provider`, `provider_unavailable` or `init_failed`. It never holds
  exception text (`constitutions/patterns/errors.md` rule 5). The run posts nothing extra to the
  merge or pull request.
- **Deferred**: Live display while the page is open would need a new CUSTOM frame handler in
  `chat-stream.js`. It is left out of this release, and the notice appears on the next load of
  the session.

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
