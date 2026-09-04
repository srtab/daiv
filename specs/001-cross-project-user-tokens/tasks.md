---

description: "Task list for cross-project git platform access under the requesting user's identity"
---

# Tasks: Cross-Project Git Platform Access Under the Requesting User's Identity

**Input**: Design documents from `/specs/001-cross-project-user-tokens/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/)

**Tests**: Test tasks are included. Not as optional TDD — Constitution Principle IV makes `make test` a
CI gate and requires coverage of this project's own logic, and SC-002 / SC-006 are security outcomes
that only a test can assert. Tests cover DAIV's seams only; allauth's OAuth dance and the platform
CLIs are not re-tested (Principle IV prohibits re-testing framework behaviour).

**Organization**: grouped by user story. US1 and US2 are both P1 and ship together — see MVP scope.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1, US2, US3, US4 — maps to [spec.md](./spec.md)
- Paths are repo-relative; `pythonpath = ["daiv", "."]`, so tests import `from accounts...` not `from daiv.accounts...`

---

## Phase 1: Setup

**Purpose**: scaffolding only. No new dependency is added — `cryptography`, `django-allauth`,
`python-gitlab` and `PyGithub` are already pinned (research.md, Resolved unknowns).

- [X] T001 Create the empty module `daiv/accounts/credentials.py` with its docstring stating it is the only module permitted to decrypt a stored platform token, per [contracts/credential-store.md](./contracts/credential-store.md)
- [X] T002 [P] Create empty test modules `tests/unit_tests/accounts/test_credentials.py` and `tests/unit_tests/codebase/test_cross_project_authorization.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: the credential must exist and be resolvable before any story can act as a person.

**⚠️ CRITICAL**: no user story work can begin until this phase is complete.

### Credential storage

- [X] T004 Add the `PlatformCredential` model to `daiv/accounts/models.py` per [data-model.md](./data-model.md): `user`, `provider`, `host`, `platform_uid`, encrypted `access_token` / `refresh_token`, `expires_at`, `scopes`, `state`; unique on `(user, provider, host)`, indexed on `(provider, platform_uid)`
- [X] T005 Reuse the Fernet descriptor pattern from `daiv/core/models.py` for `access_token` and `refresh_token` (`_<field>_encrypted` TextField columns) — do not hand-roll encryption
- [X] T006 Enforce the model validation rules in `daiv/accounts/models.py`: reject `expires_at` set with `refresh_token` null (an expiring credential that can never renew), and require a non-empty token when `state = connected`
- [X] T007 Generate the migration for `PlatformCredential` in `daiv/accounts/migrations/`
- [X] T008 [P] Add `cross_project_access_enabled` (BooleanField, **default `False`**) to `SiteConfiguration` in `daiv/core/models.py` and generate its migration in `daiv/core/migrations/` (FR-019)
- [X] T003 Add a `cross_project` `FieldGroup` to `SiteConfiguration.FIELD_GROUPS` in `daiv/core/models.py`, `match=("cross_project_*",)`, `category="Agent tools"`, `toggle_field="cross_project_access_enabled"`, following the `web_search` / `web_fetch` groups. **Depends on T008** — the group names a field that must already exist

### Credential resolution service

- [X] T009 Implement the resolution order from [contracts/credential-store.md](./contracts/credential-store.md) in `daiv/accounts/credentials.py`, returning a usable token or a typed reason (`DISABLED`, `NO_ACTING_USER`, `NO_CREDENTIAL`, `EXPIRED`, `REVOKED`) — never a collapsed generic failure
- [X] T010 Implement the Redis token cache in `daiv/accounts/credentials.py` keyed on `(user_id, provider, host)` with TTL bounded by `expires_at`. **Not** `thread_id`-keyed — that shape serves one person's token to another on a resumed thread (FR-013, research.md D6)
- [X] T011 Implement atomic refresh in `daiv/accounts/credentials.py`: access token, refresh token and expiry written in one transaction, because GitLab rotates the refresh token on every use (research.md D5). Refresh on read when the token expires within the **5-minute** margin defined in [contracts/credential-store.md](./contracts/credential-store.md), as a named constant — not once per run (FR-009)
- [X] T012 Implement `store` / `status` / `revoke` in `daiv/accounts/credentials.py`; `revoke` clears both secrets and sets `state = revoked`. `status` returns state, expiry and granted scopes and **never** the secret

### Sign-in capture

- [X] T013 Widen the OAuth scopes in `daiv/daiv/settings/components/allauth.py`: GitLab `read_user` + `api`, with the requested scope operator-configurable and `read_api` documented as the narrower option (research.md D4). GitHub scopes are inert for a GitHub App
- [X] T014 Add the GitHub App user-to-server adapter to `daiv/accounts/socialaccount.py`, alongside the existing `GitLabServerAwareAdapter`, reusing `GITHUB_APP_ID` / `GITHUB_PRIVATE_KEY` (research.md D3). Handle both expiring and non-expiring user tokens
- [X] T015 Capture the token response into `PlatformCredential` from `SocialAccountAdapter` in `daiv/accounts/adapter.py`, copying `platform_uid` from the linked `SocialAccount.uid` and recording the scopes **actually granted**, not those requested. Leave `SOCIALACCOUNT_STORE_TOKENS` at its `False` default (research.md D2)

### Foundational tests

- [X] T016 [P] Test credential resolution, refresh, expiry and revoke transitions in `tests/unit_tests/accounts/test_credentials.py`, including that a failed refresh lands in `expired` and clears the secret
- [X] T017 [P] Test that the cache key is identity-derived and that two identities on one `thread_id` never share a token, in `tests/unit_tests/accounts/test_credentials.py` (FR-013)

**Checkpoint**: a signed-in person has a stored, refreshable credential. No tool can use it yet.

---

## Phase 3: User Story 1 — Read context from another project (Priority: P1) 🎯 MVP

**Goal**: the agent can query a second project the acting person can read.

**Independent Test**: as a person with access to projects A and B, ask a question answerable only from
B's issues or merge requests, and confirm the answer carries information absent from A.

### Implementation

- [X] T018 [US1] Add the optional `project` argument (default `""`) to both tool closures in `daiv/automation/agent/middlewares/git_platform.py`, present only when `cross_project_access_enabled` is on
- [X] T019 [US1] Implement `project` validation in `daiv/automation/agent/middlewares/git_platform.py` per [contracts/agent-tools.md](./contracts/agent-tools.md): reject a leading `-` (flag confusion in `argv`), whitespace and control characters; empty or equal-to-attached takes the unchanged path
- [X] T020 [US1] Implement identity selection in `_run_gitlab_subcommand` in `daiv/automation/agent/middlewares/git_platform.py` — service token for the attached project, resolved person's token for any other, injected into the existing `--project-id` position
- [X] T021 [US1] Implement the same selection in `_run_github_subcommand` in `daiv/automation/agent/middlewares/git_platform.py`, injected into the existing `--repo` position. Keep the `api` resource off `GITHUB_CLI_ALLOW_COMMANDS` — its exclusion reason is unchanged
- [X] T022 [US1] Ensure the cross-project GitHub path returns **no** `Command(update=...)` carrying credential material, so a person's token never reaches `GitPlatformState` or a checkpoint (FR-012, research.md D6). The existing service-token state field is untouched
- [X] T023 [US1] Update `GITLAB_TOOL_DESCRIPTION` and `GITHUB_TOOL_DESCRIPTION` in `daiv/automation/agent/middlewares/git_platform.py`: default targets the current project; naming another means acting as the requesting person; an inaccessible project is refused, not silently empty. Remove the now-false `project list --topic test` "this tool is project-scoped" example

### Tests

- [X] T024 [P] [US1] Test `project` validation rejections in `tests/unit_tests/automation/agent/middlewares/test_git_platform.py`
- [X] T025 [P] [US1] Test identity selection for both platforms in `tests/unit_tests/automation/agent/middlewares/test_git_platform.py`: empty and self-referential `project` use the service token; another project uses the person's
- [X] T026 [US1] Test that the attached-project path is unchanged with the capability both on and off, in `tests/unit_tests/automation/agent/middlewares/test_git_platform.py` (FR-003, FR-019, SC-005)
- [X] T064 [P] [US1] Test that the existing operation and output limits apply unchanged to a cross-project target, in `tests/unit_tests/automation/agent/middlewares/test_git_platform.py`: a subcommand outside `GITLAB_CLI_ALLOW_COMMANDS` / `GITHUB_CLI_ALLOW_COMMANDS` is refused identically, the GitHub `api` resource stays blocked, and an oversized result is evicted to the large-tool-results dir the same way (FR-018)

**Checkpoint**: cross-project reads work. **Do not deploy without Phase 4** — see MVP scope.

---

## Phase 4: User Story 2 — A person never sees more than they are entitled to (Priority: P1) 🎯 MVP

**Goal**: refusals are correct, complete, and never widened.

**Independent Test**: as a person with no access to project C, ask the agent to read it; confirm a
stated access failure and **zero** project C content anywhere in the run.

### Implementation

- [X] T027 [US2] Implement the full refusal vocabulary in `daiv/automation/agent/middlewares/git_platform.py` per [contracts/agent-tools.md](./contracts/agent-tools.md), one string per cause, each naming the project and the next step (FR-011, SC-004)
- [X] T028 [US2] Enforce the no-fallback invariant in `daiv/automation/agent/middlewares/git_platform.py`: a refused cross-project call is **never** retried under the service identity (FR-006). This is the single line that makes User Story 2 true
- [X] T029 [US2] Keep the platform-denied string in `daiv/automation/agent/middlewares/git_platform.py` ambiguous between "does not exist" and "not permitted", mirroring the platform, so the tool is not an existence oracle for private projects
- [X] T030 [US2] Suppress verbatim platform stderr on the cross-project path in `daiv/automation/agent/middlewares/git_platform.py`, where it could carry token fragments or names the person may not see (FR-012)
- [X] T031 [US2] Add the `CrossProjectAccessRecord` model to `daiv/codebase/models.py` per [data-model.md](./data-model.md) and generate its migration in `daiv/codebase/migrations/`
- [X] T032 [US2] Write one record per cross-project call — allowed *and* refused — in `daiv/automation/agent/middlewares/git_platform.py` (FR-016). A record-write failure is logged and does not fail the call; a refused call must still be recorded
- [X] T033 [US2] Assert the record carries no token and no fetched content, only that a project was reached, in `daiv/codebase/models.py` docstring and the test below
- [X] T034 [US2] Append the FR-015 marker to content published in a project other than the attached one (research.md D9), and recognise it in `accept_callback` in `daiv/codebase/clients/gitlab/api/callbacks.py` and `daiv/codebase/clients/github/api/callbacks.py`. The attached project needs nothing — it keeps the bot identity and today's `current_user.id` check
- [X] T065 [US2] Add `CrossProjectAccessRecordFilterSet` and an `AdminRequiredMixin` + `FilterView(strict=False)` list view to `daiv/codebase/views.py`, filtered on acting user, target project, outcome and date range — the repo's convention for operator-facing filtered lists (FR-016, SC-007)
- [X] T066 [US2] Add the URL for that view under `daiv/codebase/urls/`, a template following `daiv/accounts/templates/accounts/api_keys.html`, and a sidebar entry. Reusable styling goes in `daiv/static_src/css/input.css`, not a utility chain (DESIGN.md)

### Tests

- [X] T035 [P] [US2] Test every refusal string in `tests/unit_tests/automation/agent/middlewares/test_git_platform.py`, asserting the `error: ` prefix the existing tests rely on
- [X] T036 [P] [US2] Test the no-fallback invariant in `tests/unit_tests/codebase/test_cross_project_authorization.py`: a denial produces no second call under the service identity (FR-006, SC-002)
- [X] T037 [P] [US2] Test that no target-project content appears in the tool result, the audit record, or any state update on a denial, in `tests/unit_tests/codebase/test_cross_project_authorization.py` (FR-005)
- [X] T038 [P] [US2] Test that a person's token never appears in agent state or a checkpointed value, in `tests/unit_tests/automation/agent/middlewares/test_git_platform.py` (FR-012)
- [X] T039 [P] [US2] Test the FR-015 marker in `tests/unit_tests/codebase/clients/`: DAIV's own cross-project comment starts no run, and the same person's own comment still does (SC-006)
- [X] T067 [P] [US2] Test the audit list view in `tests/unit_tests/codebase/test_views.py`: an admin can answer "which projects were reached, under whose identity" for a given `thread_id`, non-admins are refused, and no token or fetched content appears in the rendered response (SC-007)

**Checkpoint**: MVP complete. The capability works and cannot leak.

---

## Phase 5: User Story 3 — Authorise DAIV during sign-in (Priority: P2)

**Goal**: people can see, renew and revoke the authorisation themselves.

**Independent Test**: sign in with no credential, consent, run a cross-project query successfully;
disconnect and confirm the query fails with a message pointing back to settings while
attached-project work still succeeds.

**Scope note**: the token-capture machinery is Foundational (T013–T015) so US1/US2 are deliverable for
an already-authorised person. This phase is the user-visible surface — which is exactly the split the
spec's own priority rationale describes.

### Implementation

- [X] T040 [P] [US3] Add `PlatformCredentialView` (status) and a revoke view to `daiv/accounts/views.py`, following `APIKeyListView` / `APIKeyRevokeView`
- [X] T041 [P] [US3] Add `daiv/accounts/urls/credentials.py` following `daiv/accounts/urls/api_keys.py`, and register it in `daiv/accounts/urls/__init__.py`
- [X] T042 [US3] Add `daiv/accounts/templates/accounts/platform_credential.html` showing state, expiry and granted scopes, with connect and disconnect actions — following `api_keys.html`. Reusable styling goes in `daiv/static_src/css/input.css`, not a utility chain (DESIGN.md)
- [X] T043 [US3] Add the credential entry to the account sidebar in `daiv/accounts/templates/accounts/_sidebar.html`
- [X] T044 [US3] Surface what the authorisation permits on the sign-in path, in `daiv/accounts/templates/account/login.html` and the provider listing built by `SocialAccountAdapter.list_apps` in `daiv/accounts/adapter.py` (FR-007)
- [X] T045 [US3] Implement the FR-010 path for a pre-existing user holding only the narrower prior authorisation: detect the insufficient grant from the recorded `scopes` in `daiv/accounts/credentials.py`, and name the re-authorisation step in the refusal returned by `daiv/automation/agent/middlewares/git_platform.py`. Attached-project capability is retained in full
- [X] T046 [P] [US3] Add locale strings via `make makemessages` — **scope the diff to this app**, since it rewrites every app's catalog

### Tests

- [X] T047 [P] [US3] Test the status and revoke views in `tests/unit_tests/accounts/test_views.py`, including that the response never contains the token
- [X] T048 [P] [US3] Test the FR-010 pre-existing-user path in `tests/unit_tests/accounts/test_credentials.py`: narrower authorisation ⇒ attached project works, cross-project refused with the named cause

**Checkpoint**: a person can manage their own authorisation end to end.

---

## Phase 6: User Story 4 — Event-triggered runs act for the triggering person (Priority: P2)

**Goal**: a run started by a label or comment reaches other projects as the person who triggered it.

**Independent Test**: label an issue as a platform user with a linked account and credential, and
confirm the run reads a second project; repeat as an unlinked user and confirm the run completes
inside the attached project with the limitation stated.

### Implementation

- [X] T049 [US4] Add an `acting_user_id` parameter to `address_issue_task` in `daiv/codebase/tasks.py` and forward it to `set_runtime_ctx` — the resolved user already exists on the run row but is never forwarded (research.md D7)
- [X] T050 [US4] Do the same for the remaining webhook-entered tasks in `daiv/codebase/tasks.py` (review/comment paths)
- [X] T051 [P] [US4] Pass the resolved `daiv_user.pk` from `daiv/codebase/clients/gitlab/api/callbacks.py` into the enqueued task
- [X] T052 [P] [US4] Do the same in `daiv/codebase/clients/github/api/callbacks.py`
- [X] T053 [US4] Correct the `acting_user_id` docstring in `daiv/codebase/context.py` — "`None` for webhook-triggered runs" stops being true, and it now selects a credential, not only MCP servers
- [X] T054 [US4] Match the credential on `(provider, platform_uid)` from the event's platform user, not on the resolved DAIV user alone, in `daiv/accounts/credentials.py`. `resolve_user` matches by username and email first, which can return an account that never linked this platform identity — acceptable for choosing an MR assignee, not for spending a credential ([contracts/credential-store.md](./contracts/credential-store.md))

### Tests

- [X] T055 [P] [US4] Test both branches in `tests/unit_tests/codebase/test_tasks.py` and the callback tests: a mapped triggering user reaches the second project; an unmapped one is refused with the limitation stated, and the attached project is unaffected (FR-014)

**Checkpoint**: all four stories are independently functional.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T056 [P] Document OAuth application setup per platform, the scopes and App permissions to grant, and the re-consent every existing user faces, in `docs/`
- [X] T057 [P] Document that rotating `DAIV_ENCRYPTION_KEY` invalidates every stored credential and forces re-authorisation, in `docs/`
- [X] T058 [P] Document any GitLab/GitHub capability gap that could not be closed, in the same change that ships it (FR-017, Constitution III)
- [X] T059 [P] Add the `CHANGELOG.md` entry with an upgrade note — re-consent is a visible behaviour change for every existing user (Constitution: Breaking changes)
- [X] T060 Add a retention or pruning path for `CrossProjectAccessRecord` in `daiv/codebase/tasks.py`, following how `RepositoryAccess` handles staleness — it grows per tool call
- [X] T061 Verify no token reaches a log record, including exception messages, across `daiv/accounts/credentials.py` and `daiv/automation/agent/middlewares/git_platform.py` (FR-012)
- [X] T062 Run `make lint` and `make test`; both are CI gates (Constitution IV)
- [ ] T063 Run every scenario in [quickstart.md](./quickstart.md) on a GitLab **and** a GitHub deployment (SC-008)
      — **NOT DONE**: needs two live deployments plus projects A/B/C with real, differing permissions. Cannot be run from a development checkout; the unit suite covers DAIV's own seams (T024–T039, T047–T048, T055, T061–T062) but not the platforms' behaviour.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)** → no dependencies
- **Foundational (Phase 2)** → depends on Setup; **blocks every user story**
- **US1 (Phase 3)** and **US2 (Phase 4)** → depend on Foundational. US2's audit and refusal work builds on US1's identity-selection branch, so within the MVP they are sequential, not parallel
- **US3 (Phase 5)** and **US4 (Phase 6)** → depend on Foundational only; independent of each other and of the MVP pair
- **Polish (Phase 7)** → depends on the stories being shipped

### Within Phase 2

T004 → T005 → T006 → T007 are sequential (same model). T008 is independent, and T003 follows it. T009–T012 depend on the
model. T013–T015 depend on T012 (`store`). T016–T017 depend on T009–T012.

### Critical ordering note

**US1 must not deploy without US2.** The spec is explicit that the permission guarantee ships with the
capability rather than after it, "because a capability that leaks is worse than no capability". T028
(no-fallback) and T032 (audit) are what make that true.

**T034 (FR-015) must not be deferred.** Its risk — a run loop on a live repository — is realised the
moment cross-project writes exist, which is Phase 3.

## Parallel opportunities

- Phase 1: T001 and T002 together
- Phase 2: T008 alongside the T004–T007 model chain; T016 and T017 together once the service exists
- Phase 3: T024, T025 and T064 together
- Phase 4: T035–T039 and T067 together (six distinct test concerns, four files)
- Phase 5: T040, T041, T046, T047, T048 largely parallel
- Phase 6: T051 and T052 together (different client packages)
- Phase 7: T056–T059 together
- Across phases: once Foundational lands, US3 and US4 can proceed in parallel with the MVP pair

## Parallel example: Phase 4 tests

```bash
uv run pytest tests/unit_tests/automation/agent/middlewares/test_git_platform.py tests/unit_tests/codebase/test_cross_project_authorization.py tests/unit_tests/codebase/clients/ -n auto
```

## Implementation strategy

### MVP: User Story 1 + User Story 2

Both are P1 and ship together. Complete Phase 1 → Phase 2 → Phase 3 → Phase 4, then **stop and
validate** with quickstart Scenarios 2, 3 and 4 — Scenario 3 (the refusal, with a grep for leaked
content rather than an eyeball check) is the one that decides whether this is deployable.

Ship with `cross_project_access_enabled` off, enable it for one deployment, then widen.

### Incremental delivery

1. Setup + Foundational → a credential exists and refreshes
2. US1 + US2 → **MVP**: cross-project access that cannot leak
3. US3 → people manage their own authorisation instead of an operator doing it
4. US4 → the label-and-comment flows gain the capability
5. Polish → docs, changelog, retention, cross-platform validation

### Parallel team strategy

After Foundational: one developer takes the MVP pair (Phases 3 and 4 are sequential with each other),
a second takes US3, a third takes US4. The three tracks touch disjoint files apart from
`daiv/accounts/credentials.py`, where T054 (US4) is a small addition to what Phase 2 built.

## Notes

- `asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed
- Tests import `from accounts...` / `from codebase...`, never `from daiv....`
- Never hand-edit `pyproject.toml`; this feature needs no dependency change at all
- Tools return `Command(update={...})` rather than mutating `runtime.state` — T022 deliberately returns neither for the cross-project path
- Commit per task or logical group; stop at any checkpoint to validate a story independently
