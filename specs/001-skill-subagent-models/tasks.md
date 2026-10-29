---
description: "Task list for configurable skill and subagent models"
---

# Tasks: Configurable Skill and Subagent Models

**Input**: Design documents from `specs/001-skill-subagent-models/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md)

**Tests**: Included. Constitution IV (non-negotiable) requires every behaviour change to ship with
tests that fail without it. Write each story's tests first and watch them fail.

**Organization**: Tasks are grouped by user story so each story can be implemented and tested on
its own.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an unfinished task)
- **[Story]**: The user story the task belongs to (US1, US2, US3)
- Paths are relative to the repository root. Tests import without the `daiv.` prefix and use
  `uv run`.

---

## Phase 1: Setup

**Purpose**: Record the baseline the feature builds on.

- [X] T001 Confirm the per-skill model prototype (commit `bacfbbb1e`, `metadata.model` in `daiv/automation/agent/middlewares/skills.py`) is on the branch, and run `make test` to record a green baseline before any change

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Site storage for both maps, the per-run maps, the notice pipeline and the shared
configured-model helper. Every user story depends on them.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

### Tests for the foundation (write first, watch them fail)

- [X] T002 [P] Add tests in `tests/unit_tests/core/test_site_settings.py`:
  - `agent_skill_models` and `agent_subagent_models` default to `{}`;
  - `DAIV_AGENT_SKILL_MODELS` / `DAIV_AGENT_SUBAGENT_MODELS` set to a JSON object override the stored value;
  - a malformed variable is logged at ERROR and treated as `{}`;
  - the snapshot's maps reject item assignment (read-only).
- [X] T003 [P] Add tests in `tests/unit_tests/automation/agent/test_agent_settings.py`:
  - `resolve_agent_settings` copies the site maps into `AgentSettings.skill_models` / `subagent_models`;
  - an `explore` key is dropped from the subagent map;
  - a run with `RunOverrides(model_names=...)`, `agent_model=...` or `use_max=True` gets the same maps (clarification Q1).
- [X] T004 [P] Add tests in `tests/unit_tests/automation/agent/test_configured_models.py` (new file), stubbing `get_model`:
  - `build_configured_model` builds the spec with the given thinking level and returns no reason;
  - it returns `(None, "unknown_provider")` on `ValueError`, `(None, "provider_unavailable")` on `RuntimeError` (disabled provider or missing key) and `(None, "init_failed")` on any other exception, without logging;
  - `report_model_fallback` logs at ERROR naming the target, name, configured model and served model, and records a `ModelFallbackNotice` when given a recorder;
  - neither puts exception text in the notice.
- [X] T005 [P] Add tests in `tests/unit_tests/sessions/test_notices.py` (new file, `django_db(transaction=True)`):
  - `RunNoticeRecorder.record` buffers;
  - `aflush()` writes the full list to `Run.notices`;
  - a second `aflush()` after another `record` keeps both notices;
  - a recorder with no run id stores nothing and logs.
- [X] T006 [P] Add tests in `tests/unit_tests/sessions/test_transcript.py`:
  - a run with two notices yields two `run_notice` pseudo-turns after that run's turns;
  - ids are `run-notice-<run id>-<index>`;
  - a run rebuilt from its row (`_synthetic_turns`) also yields its notices;
  - notices come before the run's status marker;
  - the message is "<name> ran on <served_model>: <configured_model> could not be used (<reason label>)", using the reason labels in `specs/001-skill-subagent-models/contracts/run-notice.md`;
  - a run with `notices=[]` yields none.

### Implementation for the foundation

- [X] T007 Add `agent_skill_models` and `agent_subagent_models` to `SiteConfiguration` in `daiv/core/models.py`:
  - each is a `JSONField(default=dict, blank=True)` holding "JSON object `{skill name: model spec}`" / "`{subagent name: model spec}`";
  - let the Agent group's `agent_*` pattern match both, so `TestGetFieldGroups` keeps passing;
  - leave them out of `SiteConfigurationForm.Meta.fields`, so the group template skips them and only the formsets in T023/T032 render and save them.
- [X] T008 Generate `daiv/core/migrations/0020_agent_skill_subagent_models.py` with `uv run python daiv/manage.py makemigrations core` (depends on T007)
- [X] T009 In `daiv/core/site_settings.py` (depends on T007):
  - add `{}` defaults to `_build_field_defaults`;
  - add typed `SiteSnapshot` attributes (`Mapping[str, str]`), exposed as `MappingProxyType` copies so the shared cached default is never mutated;
  - parse `DAIV_AGENT_SKILL_MODELS` / `DAIV_AGENT_SUBAGENT_MODELS` as JSON objects following `_parse_auth_headers_json`, with a malformed value logged at ERROR and treated as `{}`.
- [X] T010 In `daiv/automation/agent/agent_settings.py` (depends on T009):
  - add `skill_models` and `subagent_models` (read-only `Mapping[str, str]`) to `AgentSettings`;
  - fill them in `resolve_agent_settings` from the site snapshot, dropping `explore` from the subagent map and ignoring `RunOverrides`;
  - add row 14 to the module's precedence docstring.
- [X] T011 [P] Create `daiv/automation/agent/notices.py`:
  - a frozen `ModelFallbackNotice` dataclass with fields:
    - `kind` (`"model_fallback"`);
    - `target` (`"skill"` | `"subagent"`);
    - `name`, `configured_model`, `served_model`;
    - `reason` (`"unknown_provider"` | `"provider_unavailable"` | `"init_failed"`);
  - an `as_dict()` method;
  - a `RunNotices` protocol with `record(notice) -> None` and `async aflush() -> None`.
- [X] T012 Create `daiv/automation/agent/configured_models.py` (depends on T011):
  - `build_configured_model(*, spec, thinking_level) -> tuple[BaseChatModel | None, FallbackReason | None]` calls `get_model(model=spec, thinking_level=thinking_level)` and maps `ValueError` → `unknown_provider`, `RuntimeError` → `provider_unavailable`, and anything else → `init_failed`; it does not log;
  - `report_model_fallback(notices, *, target, name, configured_model, served_model, reason)` logs at ERROR with every field and calls `notices.record(...)` when `notices` is given; callers report only once they know the model that served;
  - a two-line comment states why continuing is safe (`constitutions/patterns/errors.md` rule 2).
- [X] T013 Add `notices = models.JSONField(default=list, blank=True)` to `Run` in `daiv/sessions/models.py`, and generate `daiv/sessions/migrations/0020_run_notices.py` with `uv run python daiv/manage.py makemigrations agent_sessions`.
- [X] T014 Create `RunNoticeRecorder` in `daiv/sessions/notices.py` (depends on T011, T013). It implements `RunNotices`, bound to a run id that may be `None`:
  - `record` appends `notice.as_dict()`;
  - `aflush` runs `Run.objects.filter(pk=run_id).aupdate(notices=<full list>)`;
  - with no run id it logs and stores nothing.
- [X] T015 Add an optional `notices: RunNotices | None = None` parameter to `create_daiv_agent` in `daiv/automation/agent/graph.py`, documented in its Args, and call `await notices.aflush()` before returning when it is set. In `daiv/sessions/executor/run.py`, pass `notices=RunNoticeRecorder(run_id)` to `create_daiv_agent` (depends on T011, T014).
- [X] T016 In `daiv/sessions/transcript.py`, emit one `run_notice` pseudo-turn per item of `run.notices`, shaped `{id, role: "run_notice", message}` as in `specs/001-skill-subagent-models/data-model.md`, in both places a run's turns are built: `annotate_transcript`, after the run's own turns and before its `_marker`, and `_synthetic_turns`. A notice is therefore never the last turn of a run that has a status marker (depends on T013).
- [X] T017 Render notices on the session page (depends on T016):
  - in `daiv/chat/static/chat/js/chat-stream.js`, add `isMarker(turn)`, true for `run_status` and `run_notice`, and use it everywhere the code skips `run_status` before reading `turn.segments`: `findLatestTodos`, `collectOutputs`, the replay de-duplication, `isTurnVisible` (returning true), `canCopyTurn`, `turnHasActions` and the publish-phase sweep;
  - leave `runStatus` and `_lastRunAborted` checking `run_status` only: both read the last turn, which is never a notice when a marker exists;
  - in `daiv/sessions/templates/sessions/session_detail.html`, change the article branch to `x-if="!isMarker(turn)"` and add a `run_notice` branch: a neutral chip with the `exclamation-triangle` icon and `turn.message`, without a Retry button;
  - add a `chat-run-status--notice` variant in `daiv/static_src/css/input.css`, following `DESIGN.md`;
  - the repository has no JavaScript test harness, so T042 checks this by hand (quickstart scenario 5).

**Checkpoint**: The maps resolve per run, a configured spec can be built or fall back with a
persisted notice, and notices render on the session page. Run T002–T006: they now pass.

---

## Phase 3: User Story 1 - Choose the model a skill runs on, for every repository (Priority: P1) 🎯 MVP

**Goal**: An operator assigns a model to a skill in site configuration, and the skill's model
calls run on it in every repository.

**Independent Test**:
1. Add `code-review` → M in site configuration.
2. Run a code review in two repositories.
3. In each run's usage, calls after the skill was invoked are billed to M; earlier calls are
   billed to the run's model.

### Tests for User Story 1 (write first, watch them fail)

- [X] T018 [P] [US1] Add tests in `tests/unit_tests/automation/agent/middlewares/test_skills.py`, extending `TestSkillModel` (a stubbed `get_model` returns distinct fake models per spec). Cover:
  - a configured spec for `cheap` beats its `metadata.model`;
  - a configured spec for a skill with no `metadata.model` applies;
  - a configured spec that raises falls back to `metadata.model`, logs at ERROR, and records exactly one `ModelFallbackNotice` with `target="skill"` per run however often the skill is invoked, whose `served_model` is the `metadata.model` spec when that builds, else the run's model spec;
  - `aflush()` is awaited after recording;
  - a failing `metadata.model` with no configured entry still logs at WARNING and records no notice.
- [X] T019 [P] [US1] Add tests in `tests/unit_tests/configuration/test_forms.py` for the skills formset. Saving:
  - folds valid rows into `agent_skill_models`;
  - refuses a row whose model's provider is unknown, disabled or keyless, with an error naming the entry;
  - refuses a blank name, a duplicate name and a name without a model.
- [X] T020 [P] [US1] Add tests in `tests/unit_tests/configuration/test_views.py`:
  - the Agent group renders the "Skill and subagent models" section with a datalist of `list_global_skills()` names;
  - saving a name outside the suggestions shows a `messages.warning` naming it;
  - with `DAIV_AGENT_SKILL_MODELS` set, the section is read-only and posting does not change the stored map.

### Implementation for User Story 1

- [X] T021 [US1] In `daiv/automation/agent/middlewares/skills.py`:
  - add `configured_models: Mapping[str, str]`, `run_model_spec: str` and `notices: RunNotices | None` constructor arguments;
  - at invocation, try the configured spec through `build_configured_model`, then `metadata.model` through the existing `_skill_model`, then keep the run's model;
  - when the configured spec failed, call `report_model_fallback(target="skill", ...)` with `served_model` set to the `metadata.model` spec if it built, else `run_model_spec`, then `await notices.aflush()`; do this once per run per entry;
  - keep the WARNING path for file-declared models;
  - update the class docstring's precedence sentence.
- [X] T022 [US1] In `daiv/automation/agent/graph.py`, pass `configured_models=settings.skill_models`, `run_model_spec=settings.agent.names[0]` and `notices=notices` to `SkillsMiddleware` (depends on T021).
- [X] T023 [US1] In `daiv/configuration/forms.py`:
  - extract the provider check from `_validate_model_api_keys` into a reusable helper;
  - add a `ModelMapRowForm` (name, model) and a `formset_factory` for it;
  - add a fold-to-dict function that enforces non-blank, unique names and a usable model per row, following `build_headers_from_formset` in `daiv/mcp_connectors/forms.py`;
  - instantiate it for skills with prefix `skill_models`.
- [X] T024 [US1] In `daiv/configuration/views.py`, wire the skills formset into `SiteConfigurationGroupView` for the Agent group (depends on T023):
  - save it in the same `transaction.atomic` as the form;
  - after save, add `messages.warning` for names outside `list_global_skills()`;
  - render read-only when `DAIV_AGENT_SKILL_MODELS` is set, like the web-fetch auth headers section.
- [X] T025 [US1] Create `daiv/configuration/templates/configuration/_agent_model_maps_section.html` and `daiv/configuration/templates/configuration/agent_model_map_row.html`:
  - rows via the `djangoFormset` component;
  - a name `<input list>` with a `<datalist>` of suggestions;
  - the `automation/_agent_picker.html` partial with `with_effort=False` and `field_name_model` set to the row's prefixed name;
  - include the section from the Agent group hook in `daiv/configuration/templates/configuration/site_configuration_group.html`;
  - run `uv run --only-group=dev djade --target-version 6.0` on both new files.
- [X] T026 [US1] Rewrite "Running a skill on another model" in `docs/customization/agent-skills.md`:
  - site configuration first, then `metadata.model`;
  - the precedence order;
  - that an unusable configured model falls back with a notice.

**Checkpoint**: User Story 1 works end to end and passes T018–T020.

---

## Phase 4: User Story 2 - Choose the model each subagent runs on (Priority: P2)

**Goal**: An operator assigns models to subagents by name, including each code-review detector.
Detectors without an entry keep their model.

**Independent Test**:
1. Add `cr-security` and `cr-tests` entries.
2. Run a review.
3. Those two detectors are billed to their models, and the others to the run's model.

### Tests for User Story 2 (write first, watch them fail)

- [X] T027 [P] [US2] Add tests in `tests/unit_tests/automation/agent/test_subagents.py`, with a stubbed `get_model`:
  - for detectors, a configured entry beats the charter's `model:`, which beats the run's model;
  - a configured entry that fails falls back to the charter model, or else the run's model; the detector is still loaded, and a `ModelFallbackNotice` with `target="subagent"` is recorded whose `served_model` is the spec that served;
  - when a configured entry and the file's `model:` both fail, the subagent runs on the run's model instead of being skipped;
  - a repository subagent (`load_custom_subagents`) behaves the same;
  - an invalid file-declared `model:` with no entry still skips the subagent;
  - configured models get the run's thinking level.
- [X] T028 [P] [US2] Add tests in `tests/unit_tests/automation/agent/test_graph_construction.py`:
  - `general-purpose` uses its configured entry when one is set;
  - `create_daiv_agent` calls `notices.aflush()` once after build when notices were recorded.
- [X] T029 [P] [US2] Add tests in `tests/unit_tests/configuration/test_forms.py` and `tests/unit_tests/configuration/test_views.py` for the subagent formset:
  - a row named `explore` is refused with an error pointing to the Explore model setting;
  - suggestions are `BUILTIN_SUBAGENT_NAMES` without `explore`;
  - valid rows save into `agent_subagent_models`.

### Implementation for User Story 2

- [X] T030 [US2] In `daiv/automation/agent/subagents.py`:
  - add `configured_models: Mapping[str, str]`, `thinking_level`, `run_model_spec` and `notices` keyword arguments to `load_builtin_code_review_detectors` and `load_custom_subagents`;
  - resolve each subagent's model as configured entry (via `build_configured_model`) → file `model:` → the run's model;
  - when the configured entry failed, call `report_model_fallback(target="subagent", ...)` with the spec that served; if the file's `model:` also fails, use the run's model instead of skipping;
  - without a configured entry, leave the existing skip-on-invalid behaviour for file-declared models unchanged.
- [X] T031 [US2] In `daiv/automation/agent/graph.py` (depends on T030):
  - resolve the `general-purpose` model through `build_configured_model` before `create_general_purpose_subagent`, falling back to the run's model and reporting the fallback;
  - pass `settings.subagent_models`, `chain.thinking_level`, `settings.agent.names[0]` and `notices` to both loaders.
- [X] T032 [US2] Extend `daiv/configuration/forms.py`, `daiv/configuration/views.py` and `_agent_model_maps_section.html` with the subagent formset (prefix `subagent_models`) (depends on T023–T025):
  - refuse `explore`;
  - suggest `BUILTIN_SUBAGENT_NAMES - {EXPLORE_NAME}`;
  - warn on unknown names;
  - lock the section when `DAIV_AGENT_SUBAGENT_MODELS` is set.
- [X] T033 [US2] Document configured subagent models in `docs/features/subagents.md`: site configuration entries, the precedence over the file's `model:`, the run's thinking level, the fallback with a notice, and that `explore` keeps its own setting.

**Checkpoint**: User Stories 1 and 2 both work on their own; T027–T029 pass.

---

## Phase 5: User Story 3 - Override a model for one repository (Priority: P3)

**Goal**: A repository's `.daiv.yml` overrides site entries by name, for that repository only.

**Independent Test**:
1. With a site entry for `code-review`, set a different `models.skills.code-review` in one
   repository's `.daiv.yml`.
2. Reviews there use the repository's model, and other repositories keep the site's.

### Tests for User Story 3 (write first, watch them fail)

- [X] T034 [P] [US3] Add tests in `tests/unit_tests/codebase/test_repo_config.py`:
  - `models.skills` and `models.subagents` parse as `dict[str, str | None]` with default `{}`;
  - a `null` value is accepted;
  - a value that is not a valid model spec (e.g. `nosuchprovider:model`) still loads the whole file, and other settings keep their values.
- [X] T035 [P] [US3] Add tests in `tests/unit_tests/automation/agent/test_agent_settings.py`:
  - a repository entry overrides only the site entry with the same name;
  - a `null` or blank repository entry leaves the site entry in place;
  - a repository `subagents.explore` entry is dropped;
  - a repository entry still applies on a run with `RunOverrides(model_names=...)`.

### Implementation for User Story 3

- [X] T036 [US3] Add `skills: dict[str, str | None] = Field(default_factory=dict, ...)` and `subagents: dict[str, str | None] = Field(default_factory=dict, ...)` to `Models` in `daiv/codebase/repo_config.py`, with descriptions. Values are not validated on load (research R3).
- [X] T037 [US3] In `resolve_agent_settings` in `daiv/automation/agent/agent_settings.py`, overlay the repository's non-empty `models.skills` / `models.subagents` entries on the site maps, and update row 14 of the precedence docstring to name the repository layer (depends on T036).
- [X] T038 [US3] Add `models.skills` and `models.subagents` rows, an example and the precedence to `docs/customization/repository-config.md`, noting that values are checked at run time, not when the file loads.

**Checkpoint**: All three stories work on their own; T034–T035 pass.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T039 [P] Add the "Skill and subagent models" section to the Agent row of `docs/reference/site-configuration.md`, and `DAIV_AGENT_SKILL_MODELS` / `DAIV_AGENT_SUBAGENT_MODELS` to `docs/reference/env-variables.md`.
- [X] T040 [P] Add an `Unreleased` → `Added` entry to `CHANGELOG.md`: per-skill and per-subagent models from site configuration and `.daiv.yml`, with a notice on the run when one can't be used.
- [X] T041 Run `make lint`, `make lint-typing` and `make test`, and `uv run --only-group=docs mkdocs build --strict` for the docs. Report each result, and report any check not run as not run.
- [ ] T042 Walk the manual scenarios in `specs/001-skill-subagent-models/quickstart.md` on a local stack. Report which ran and which did not.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: none.
- **Foundational (Phase 2)**: depends on T001; blocks every user story.
- **User Stories (Phases 3–5)**: each depends only on Phase 2.
  - US2's dashboard task T032 extends the files US1 creates in T023–T025. If US2 is built before
    US1, T032 creates those files instead.
- **Polish (Phase 6)**: after the stories being shipped.

### Within Phase 2

- T007 → T008 and T009.
- T009 → T010.
- T011 → T012 and T014.
- T013 → T014 and T016.
- T014 → T015.
- T016 → T017.

### Within Each User Story

- Tests first, and they must fail.
- Agent runtime, then wiring in `graph.py`, then dashboard, then docs.

### Parallel Opportunities

- Phase 2 tests T002–T006 are all [P].
- Phase 2: T011 can run alongside T007–T010.
- Each story's test tasks are [P].
- After Phase 2, US1's runtime tasks (T021–T022), US2's runtime tasks (T030–T031) and US3 (T036–T038) touch different files. The exception is `graph.py`, which T022 and T031 both edit, so those two run one after the other.

---

## Parallel Example: User Story 1

```bash
# Tests together:
Task: "T018 skill precedence and notice tests in tests/unit_tests/automation/agent/middlewares/test_skills.py"
Task: "T019 skills formset tests in tests/unit_tests/configuration/test_forms.py"
Task: "T020 Agent-group section tests in tests/unit_tests/configuration/test_views.py"

# Then runtime and dashboard in parallel (different files):
Task: "T021 configured spec before metadata.model in daiv/automation/agent/middlewares/skills.py"
Task: "T023 model-map formset in daiv/configuration/forms.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 only)

1. Phase 1, then Phase 2.
2. Phase 3 (US1): operators set the `code-review` model once for every repository, which is the
   problem this feature started from.
3. Stop and validate with quickstart scenario 1.

### Incremental Delivery

1. Foundation → US1 (MVP).
2. US2 (detector and subagent models).
3. US3 (repository overrides).
4. Polish.

Each step leaves the suite green and the earlier stories working.
