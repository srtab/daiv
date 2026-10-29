# Implementation Plan: Configurable Skill and Subagent Models

**Branch**: `claude/model-changes-by-skill-558d2e` | **Date**: 2026-10-29 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/001-skill-subagent-models/spec.md`

## Summary

Operators map skill and subagent names to model specs in site configuration. A repository can
override those entries per name in `.daiv.yml`. Each run resolves both maps once, and the
repository's entries win.

- **Skills:** a configured model replaces the model the skill file declares, using the per-skill
  switching already on this branch.
- **Subagents:** the `general-purpose` subagent, the code-review detectors and repository
  subagents are built on their configured model when the agent is assembled.
- **Failures:** a configured model that can't be used falls back to the model the skill or
  subagent would use with no configured entry. The failure is logged at ERROR and recorded as a notice on
  the run, which DAIV's session view shows. Decisions are in [research.md](research.md).

## Technical Context

**Language/Version**: Python 3.14

**Primary Dependencies**: Django 6 (ASGI), pydantic (repository config), Deep Agents on LangGraph,
Alpine.js and Tailwind for the configuration and session pages

**Storage**: PostgreSQL. Two `JSONField`s on `core.SiteConfiguration`, one `JSONField` on
`sessions.Run`. Two migrations, both adding fields with defaults; no data migration.

**Testing**: pytest with pytest-django, `make test`. Models are stubbed; no real LLM calls.

**Target Platform**: Linux server; the web and worker containers

**Project Type**: Django web service with an LLM agent runtime

**Performance Goals**: None new. Model resolution is a dictionary lookup per name, and each
configured model is built once per run.

**Constraints**:
- `.daiv.yml` values must not be validated on load, so a typo can't drop the rest of the file
  (research R3).
- `automation` cannot import `sessions`, so notices go through an injected collaborator (R7).
- No exception text in a notice.

**Scale/Scope**: A few entries per map: the 5 built-in skills, 7 built-in subagents, plus custom
ones.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | How the design complies | Status |
|-----------|-------------------------|--------|
| I. Secure and isolated | No new credentials, egress or entry points. Entries resolve only against configured `Provider` rows. Repositories may override entries with the trust they already have for `models.agent` (clarification). Notices hold reason categories, never exception text. | Pass |
| II. Architecture boundaries | The `configuration` app (top layer) reads names from `skills` and `automation`. `automation` defines a `RunNotices` protocol that `sessions` implements and the executor injects, the same pattern as `session_links` and `delegator`. No new package, no new `ignore_imports`. | Pass |
| III. Platform and provider neutrality | No platform branching. Specs resolve through `parse_model_spec`. The defaults are empty maps, and both maps can be overridden with `DAIV_AGENT_SKILL_MODELS` / `DAIV_AGENT_SUBAGENT_MODELS`. | Pass |
| IV. Tests prove behaviour | Each behaviour change has tests that fail without it (Project Structure → tests). The `chat-stream.js` change is checked by hand in quickstart scenario 5, because the repository has no JavaScript test harness. | Pass (planned) |
| V. Measured agent behaviour | With no entries configured, prompts, tools and models are unchanged, so this is not an agent-behaviour change. The pull request declares it unmeasured (R9). | Pass |
| VI. Honest failure handling | An unusable configured model is logged at ERROR, and the run continues on the fallback model with a persisted notice. Nothing extra is posted to the merge or pull request. | Pass |

Quality gates for the change: `make lint`, `make lint-typing`, `make test`, a strict docs build,
and an `Unreleased` entry in `CHANGELOG.md`.

**Post-design re-check**: still passes. The Phase 1 design adds `Run.notices` and a sessions-side
recorder injected by the executor; neither changes a layer contract or the agent's state
channels.

## Project Structure

### Documentation (this feature)

```text
specs/001-skill-subagent-models/
├── spec.md
├── plan.md              # This file
├── research.md          # Phase 0
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/
│   ├── daiv-yml.md          # models.skills / models.subagents
│   ├── site-configuration.md # fields, env vars, form behaviour
│   └── run-notice.md        # notice payload and session-view rendering
├── checklists/requirements.md
└── tasks.md             # /speckit-tasks (not created here)
```

### Source Code (repository root)

```text
daiv/
├── core/
│   ├── models.py                      # SiteConfiguration.agent_skill_models / agent_subagent_models
│   ├── site_settings.py               # defaults, SiteSnapshot attributes, JSON env parsing
│   └── migrations/0020_agent_skill_subagent_models.py
├── codebase/
│   └── repo_config.py                 # Models.skills / Models.subagents
├── automation/agent/
│   ├── agent_settings.py              # AgentSettings.skill_models / subagent_models, precedence row 14
│   ├── notices.py                     # RunNotices protocol, ModelFallbackNotice
│   ├── configured_models.py           # builds a configured spec and classifies a failure; reports a fallback (ERROR log + notice)
│   ├── subagents.py                   # configured-model resolution for general-purpose, detectors, custom
│   ├── graph.py                       # passes maps, thinking level and notices through
│   └── middlewares/skills.py          # configured spec before metadata.model; ERROR + notice
├── sessions/
│   ├── models.py                      # Run.notices
│   ├── migrations/0020_run_notices.py
│   ├── notices.py                     # RunNoticeRecorder (implements RunNotices)
│   ├── executor/run.py                # injects the recorder into create_daiv_agent
│   ├── transcript.py                  # run_notice pseudo-turns
│   └── templates/sessions/session_detail.html  # notice chip
├── chat/static/chat/js/chat-stream.js # run_notice turns skipped like run-status markers
├── configuration/
│   ├── forms.py                       # model-map formset, shared spec validation, explore refusal
│   ├── views.py                       # formset wiring, unknown-name warnings, env lock
│   └── templates/configuration/_agent_model_maps_section.html, agent_model_map_row.html
└── static_src/css/input.css           # chat-run-status--notice variant

docs/
├── customization/agent-skills.md      # metadata.model section → precedence with configuration
├── customization/repository-config.md # models.skills / models.subagents rows
├── features/subagents.md              # configured subagent models
└── reference/site-configuration.md    # Agent group: skill and subagent models

tests/unit_tests/
├── core/test_site_settings.py         # JSON env override, read-only snapshot maps
├── codebase/test_repo_config.py       # maps load; a bad spec does not drop the file
├── automation/agent/test_agent_settings.py      # overlay, empty entries, explore dropped, pin ignored
├── automation/agent/test_subagents.py           # configured > frontmatter > run; fallback without skip
├── automation/agent/middlewares/test_skills.py  # configured > metadata; ERROR + notice on failure
├── sessions/test_notices.py           # recorder persists the list
├── sessions/test_transcript.py        # run_notice pseudo-turns
└── configuration/test_forms.py, test_views.py   # row validation, warning, explore refusal, env lock
```

**Structure Decision**: The changes extend existing apps in their own layers; no new app or
top-level package is added. `automation/agent/notices.py` and `sessions/notices.py` are new
modules inside existing packages.

## Complexity Tracking

No constitution violations to justify.
