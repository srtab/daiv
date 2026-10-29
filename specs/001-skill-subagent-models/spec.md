# Feature Specification: Configurable Skill and Subagent Models

**Feature Branch**: `claude/model-changes-by-skill-558d2e`

**Created**: 2026-10-29

**Status**: Draft

**Input**: User description (from the design discussion that preceded `/speckit-specify`): "Changing the model a skill runs on forces me to copy each skill file into my repositories. Make it configurable instead: map skill and subagent names to models, instance-wide from site configuration and per repository in `.daiv.yml`, so the code-review skill and its detectors can run on chosen models without editing or copying files."

## Clarifications

### Session 2026-10-29

- Q: Should operators be able to stop repositories from overriding the instance's skill and subagent models in `.daiv.yml`? → A: No. Repositories can always override; this release has no operator lock.
- Q: How should operators enter skill and subagent model assignments in the dashboard's site configuration? → A: As rows of a name and a model. Names are suggested from the built-in and global skills and subagents, and any name can be typed; models are picked from the configured providers. Saving warns about an unknown name but accepts it.
- Q: When a model named in a repository's `.daiv.yml` can't be used, who should be told? → A: Operators through the error log, plus a notice on the run in DAIV's session view. Nothing extra is posted to the merge or pull request.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Choose the model a skill runs on, for every repository (Priority: P1)

An operator wants every code review on the instance to run on a chosen model, for example a stronger model than the one the rest of the run uses. They open site configuration, assign that model to the `code-review` skill, and save. From the next run on, wherever the agent invokes `code-review`, the work that follows the invocation runs on the assigned model, in every repository, without anyone editing or copying a skill file.

**Why this priority**: This is the problem that started the feature: today the only way to change a skill's model is to edit the built-in file (a deploy) or copy the skill into each repository, where the copy drifts from upstream changes.

**Independent Test**: Assign a model to `code-review` in site configuration, run a code review in two different repositories, and confirm in each run's usage that the review's model calls were served by the assigned model while calls before the skill was invoked were served by the run's model.

**Acceptance Scenarios**:

1. **Given** site configuration assigns model M to `code-review` and a repository has no entry for it, **When** a run in that repository invokes `code-review`, **Then** the run's model calls after the invocation are served by M until the user's next message.
2. **Given** the same assignment, **When** a run never invokes `code-review`, **Then** every model call in the run is served by the run's model.
3. **Given** the `code-review` skill file declares its own model and site configuration assigns a different one, **When** the skill is invoked, **Then** the site configuration's model serves the calls.
4. **Given** an operator removes the assignment, **When** the next run invokes `code-review`, **Then** the skill runs on the model it would use without the entry.
5. **Given** site configuration assigns model M to `code-review` and the user pinned the run to model P, **When** the run invokes `code-review`, **Then** M serves the calls after the invocation and P serves the calls before it.

---

### User Story 2 - Choose the model each subagent runs on (Priority: P2)

An operator wants the code-review detectors to run on different models: the security detector on a stronger model, the tests detector on a cheaper one. In site configuration they assign a model to each detector by name. Every run that delegates to those detectors uses the assigned models; detectors without an entry keep their current model.

**Why this priority**: Detectors do most of a code review's model work, so their models drive the review's cost and quality. Today a detector's model can only change through a deploy.

**Independent Test**: Assign models to `cr-security` and `cr-tests` only, run a code review, and confirm in the run's usage that those two detectors were served by their assigned models and the other detectors by the run's model.

**Acceptance Scenarios**:

1. **Given** site configuration assigns model M to `cr-security`, **When** a run delegates to `cr-security`, **Then** every model call that detector makes is served by M.
2. **Given** a subagent defined in a repository's subagent files has an entry, **When** a run in that repository delegates to it, **Then** the entry's model serves it, over the model its own file declares.
3. **Given** a skill with an assigned model is active, **When** the agent delegates to a subagent, **Then** the subagent runs on its own entry's model, or the model it would use without one, never on the skill's model.

---

### User Story 3 - Override a model for one repository (Priority: P3)

A repository maintainer wants their repository's code reviews on a different model than the instance default, for example a cheaper model for a low-risk repository. They add an entry for `code-review` to the repository's `.daiv.yml`. Runs in that repository use their entry; other repositories keep the site configuration's model, and the repository keeps the site's entries for every name it does not override.

**Why this priority**: Per-repository control matters, but the instance-wide setting already removes the copying; this refines it.

**Independent Test**: With a site entry for `code-review`, add a different entry to one repository's `.daiv.yml`, run code reviews in that repository and another, and confirm each used its expected model.

**Acceptance Scenarios**:

1. **Given** site configuration assigns M1 to `code-review` and a repository's `.daiv.yml` assigns M2, **When** a run in that repository invokes `code-review`, **Then** M2 serves the calls.
2. **Given** that repository's `.daiv.yml` has no entry for `cr-security` and site configuration assigns M3 to it, **When** that run delegates to `cr-security`, **Then** M3 serves it.
3. **Given** a repository entry is empty, **When** a run invokes the named skill, **Then** the site configuration's entry applies, as if the repository had no entry.

---

### Edge Cases

- **Unusable model**: An entry names a provider that is not configured, is disabled, or has no API key. The skill or subagent runs on the model it would use without the entry, the run continues, operators see the failure at ERROR level, and the run shows a notice in DAIV's session view. A subagent is never left out of the run because its configured model is unusable.
- **Unknown name**: An entry names a skill or subagent that the run does not have. The entry is ignored and the run is unaffected. In site configuration, saving such an entry shows a warning when the name matches no built-in or global skill or subagent.
- **Conversation too large**: The conversation no longer fits the configured skill model's context window. Those calls are served by the run's model, as for a model declared in the skill file.
- **Two skills in one turn**: Two skills with different models are invoked in the same turn. The most recently applied skill model serves the calls that follow.
- **Configuration changes mid-session**: A change applies to runs that start after it, never to a run already in progress.
- **Run with a user-chosen model**: The user picked a model in the chat model picker, chose Max, or pinned the run to an exact model. The configured skill or subagent model still applies to the calls it covers, and the user's choice serves the rest of the run.
- **Restored session**: An expired session is restored. No skill model stays active, so the next turn starts on the run's model.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Operators MUST be able to assign a model spec to a skill by name in site configuration, and the assignment MUST apply to every repository on the instance.
- **FR-002**: Operators MUST be able to assign a model spec to a subagent by name in site configuration, including each code-review detector, the general-purpose subagent, and subagents defined in repository files.
- **FR-003**: Repository maintainers MUST be able to assign a model spec to a skill or a subagent by name in the repository's `.daiv.yml`. An entry overrides the site configuration's entry for the same name only, and an empty entry counts as no entry. The first release includes both site configuration and the repository file.
- **FR-004**: For a given name, the model MUST resolve in this order, first match wins: the repository's entry, then the site configuration's entry, then the model the skill or subagent file declares, then the model it uses today. A model the user chose for the run, including an exact pin, sits in that last place and never beats a configured entry.
- **FR-005**: A configured skill model MUST serve the run's model calls from the skill's invocation until the user's next message, the same lifetime as a model declared in the skill file.
- **FR-006**: A configured subagent model MUST serve every model call that subagent makes.
- **FR-007**: A configured model MUST run with the run's thinking level and fall back to the run's fallback models when it fails.
- **FR-008**: Site configuration MUST present assignments as rows of a name and a model. The name field MUST suggest the built-in and global skills and subagents and MUST accept any typed name. The model MUST be picked from the models of the configured providers.
- **FR-008a**: Saving site configuration MUST refuse an entry whose model spec names no configured provider and say which entry is invalid. It MUST warn about, but still save, an entry whose name matches no built-in or global skill or subagent, since that name may exist only in repositories.
- **FR-009**: When a configured model cannot be used at run time, the skill or subagent MUST run on the model it would use without the entry, and the run MUST continue. The failure MUST be logged at ERROR level naming the entry, and the run MUST show a notice in DAIV's session view naming the skill or subagent, the model that could not be used, and the model that served instead. The run MUST NOT post anything extra to the merge or pull request.
- **FR-010**: Entries naming a skill or subagent that the run does not have MUST be ignored without affecting the run.
- **FR-011**: When the conversation does not fit a configured skill model's context window, the run's model MUST serve that call.
- **FR-012**: A configuration change MUST take effect for runs that start after it, without a deploy and without editing or copying any skill or subagent file.
- **FR-013**: A run's usage report MUST attribute each model call to the model that actually served it, so operators can confirm which configured models were used.
- **FR-014**: The user documentation MUST describe both places to configure models, the resolution order, and the fallback behaviour.

### Key Entities

- **Skill model assignment**: A skill name paired with a model spec, held either in site configuration (applies instance-wide) or in a repository's `.daiv.yml` (applies to that repository).
- **Subagent model assignment**: A subagent name paired with a model spec, with the same two scopes as a skill model assignment.
- **Model spec**: A `provider:model` string naming one model on one configured provider.
- **Provider**: A configured LLM provider that model specs resolve against; an assignment is usable only if its provider exists, is enabled, and has a key.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator changes the model of the code-review skill for every repository on the instance with one configuration change, copying zero files into any repository.
- **SC-002**: An operator changes the model of any single code-review detector with one configuration change, without a deploy.
- **SC-003**: A repository maintainer overrides the instance's model for one skill or subagent by adding one entry to their repository's configuration.
- **SC-004**: In 100% of runs where a configured model is unusable, the run completes on the fallback model rather than failing, an operator can find the failure in the logs, and anyone viewing the run's session sees which entry could not be used.
- **SC-005**: For any run, an operator can tell from the run's usage which model served the skill's calls and each subagent's calls.

## Assumptions

- **Builds on this branch**: The feature extends the per-skill model switching committed on this branch: a skill-declared model, its lifetime until the user's next message, and the context-window guard.
- **Model only**: An entry holds a model spec only; the thinking level is always the run's. Per-entry thinking levels are out of scope.
- **Repository trust**: Repositories can always override site entries, with the same trust they already have to choose the run's main model. Operators get no lock against repository overrides in this release; a lock, if one is needed later, should cover the run's main model too and is a separate feature.
- **Explore keeps its own setting**: The explore subagent keeps its existing dedicated model setting and is not configured through these assignments, so one model has one place to configure it.
- **Exact names**: Entries match skill and subagent names exactly. A repository skill that shadows a built-in skill of the same name receives the same entry.
- **Platform neutral**: The feature behaves the same on GitLab and GitHub, and with any configured provider.
- **Detectors stay built-in**: Code-review detectors are still loaded only from the built-in skill. This feature changes their models, not where they come from.
