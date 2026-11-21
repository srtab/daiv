---

description: "Task list for merge request automations"
---

# Tasks: Merge Request Automations

**Input**: Design documents from `specs/005-merge-request-automations/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md).

**Tests**: Included. Constitution principle IV and `patterns/tests.md` require a test that fails without each behaviour change.
- Tests stub every git-platform client, model and the sandbox, and make no network call.
- CI tokens are signed in the test with a generated RSA key, and the platform's signing-key endpoint is stubbed.
- An async test that writes uses `django_db(transaction=True)`.
- A test that touches the jobs throttle clears the cache first.
- The settle delay is a patched module constant.

Principle V applies: evals run before and after (T004, T076).

**Organization**: Tasks are grouped by user story so each story can be built and tested on its own. US1 is the MVP.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: the user story the task belongs to (US1–US4)

## Path Conventions

- Paths are relative to the repository root: `daiv/`, `tests/unit_tests/`, `docs/`.
- Tests mirror `daiv/`, one file per module, and import without the `daiv.` prefix.
- Names, texts, defaults and limits quoted below come from the contracts and the data model. Use them verbatim.

---

## Phase 1: Setup

**Purpose**: approval, the refined spec, a clean baseline and the one new direct dependency.

- [X] T001 Record the maintainer's approval of plan.md and refinements SR1–SR3 in the **Status** line of specs/005-merge-request-automations/plan.md (done 2026-11-21).
- [X] T002 Apply refinements SR1–SR3 from specs/005-merge-request-automations/research.md to specs/005-merge-request-automations/spec.md, in FR-005, FR-011 and FR-016 (done 2026-11-21).
- [ ] T003 Confirm the next free migration numbers.
  - When planned they were `daiv/sessions/migrations/0043` and `daiv/core/migrations/0028`. Renumber the migrations named below if `main` has moved.
  - Run `make test` once before any change, and note pre-existing failures so later ones can be told apart.
- [ ] T004 Take the eval baseline before any code change: `make eval-prompts SUITES="inbox code_review" OUT=eval-runs/005-before.jsonl`. It needs the LLM key and GitLab credentials (AGENTS.md).
- [ ] T005 Add PyJWT as a direct, pinned dependency with `uv add "pyjwt[crypto]==2.15.1"`, the version already in uv.lock. This updates pyproject.toml and uv.lock. Do not edit either by hand.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**:
- Move the direct merge request path from `webhooks` into `sessions/merge_request_runs/` (research R1), with mention behaviour unchanged.
- Add `Run.acts_as_user` (R6).

US1 and US3 start runs through the moved service, and US2 and US3 need `acts_as_user`.

**⚠️ CRITICAL**: no user story work can begin until this phase is complete.

### Moving the direct merge request path (R1, contracts/merge-request-runs.md)

- [ ] T006 Create the package `daiv/sessions/merge_request_runs/` with `__init__.py`. Move `daiv/webhooks/managers/base.py` to `daiv/sessions/merge_request_runs/base.py`, keeping its class names and behaviour. Point `daiv/webhooks/managers/issue_addressor.py` at the new module. Move `tests/unit_tests/webhooks/managers/test_base.py` to `tests/unit_tests/sessions/merge_request_runs/test_base.py`, adjusting imports and patch targets only.
- [ ] T007 Move `daiv/webhooks/managers/review_addressor.py` to `daiv/sessions/merge_request_runs/addressor.py`, with behaviour unchanged. Move `tests/unit_tests/webhooks/managers/test_review_addressor.py` and `test_review_addressor_footer.py` to `tests/unit_tests/sessions/merge_request_runs/test_addressor.py`, adjusting imports and patch targets only. Move the shared fixtures of `tests/unit_tests/webhooks/managers/conftest.py` that these tests use into `tests/unit_tests/sessions/merge_request_runs/conftest.py`.
- [ ] T008 Move `_start_run`, `_reply_archived`, `_pinned_model`, `Author` and `Mention` from `daiv/webhooks/dispatch.py` into `daiv/sessions/merge_request_runs/starter.py`, as class `RunStarter`.
  - It keeps today's signature and behaviour: the task, the requester, the scope, the iid, the task kwargs, the trigger type, the title, the reply and the ref.
  - `Author` becomes `Requester`, with fields `user: User | None`, `provider`, `uid`, `username` and `acts_as_user: bool = True`.
  - The issue paths in `daiv/webhooks/dispatch.py` call `RunStarter` too, because it serves the `ISSUE` and `MERGE_REQUEST` scopes alike.
  - Move the matching tests from `tests/unit_tests/webhooks/test_dispatch.py` into `tests/unit_tests/sessions/merge_request_runs/test_starter.py`.
- [ ] T009 Create `MergeRequestRuns(client, repo_id, merge_request_iid)` in `daiv/sessions/merge_request_runs/service.py`.
  - `astart(requester, input, *, title, use_max=False, thread_id=None, muted=None, ask_user=True) -> StartOutcome(run_id, status, archived)` holds the owned-versus-unowned branching of today's `dispatch_mr_comment_run`, including `_post_comment` and `_fetch_source_branch`.
  - At this stage `input` is only a `Mention`.
  - `daiv/webhooks/dispatch.py` `dispatch_mr_comment_run` becomes a thin call to it.
  - Move the merge-request mention tests of `tests/unit_tests/webhooks/test_dispatch.py` into `tests/unit_tests/sessions/merge_request_runs/test_service.py`.
- [ ] T010 Move `address_mr_comments_task` from `daiv/webhooks/tasks.py` to `daiv/sessions/merge_request_runs/tasks.py` as `address_merge_request_task`.
  - It deduplicates on (`repo_id`, `merge_request_id`, `request_key`), and a mention passes `request_key=f"note:{mention_note_id}"`.
  - Keep `address_mr_comments_task` in `daiv/webhooks/tasks.py` as a one-release alias that accepts the old keyword arguments and forwards to the new task, so task rows already queued under the old path still run.
  - Move its tests to `tests/unit_tests/sessions/merge_request_runs/test_tasks.py`, and add a test in `tests/unit_tests/webhooks/test_tasks.py` that the alias forwards.
- [ ] T011 Run `make lint-imports`. The moves must add no `ignore_imports` line to `.importlinter`. Run the moved test files and `tests/unit_tests/webhooks/`; every moved test passes unchanged in meaning.

### Attribution without authority (R6)

- [ ] T012 [P] Write failing tests in `tests/unit_tests/sessions/test_models.py`: `Run.acting_user_id` returns `user_id` when `acts_as_user` is true, and `None` when it is false, even with a user set.
- [ ] T013 [P] Write failing tests in `tests/unit_tests/sessions/test_queue.py`, `tests/unit_tests/sessions/inbox/test_drain.py` and `tests/unit_tests/sessions/test_admission.py`: a run with `acts_as_user=False` and a linked user is enqueued and drained with acting user `None`. Admission resolves its MCP pool and sandbox environment with `user=None`, so no USER-scoped server or environment is chosen.
- [ ] T014 Add `acts_as_user = models.BooleanField(default=True)` and the property `acting_user_id` (`user_id if acts_as_user else None`) to `Run` in `daiv/sessions/models.py`, with migration `daiv/sessions/migrations/0043_run_acts_as_user.py`. `create_run` in `daiv/sessions/services.py` accepts `acts_as_user: bool = True`.
- [ ] T015 Switch every reader that treats `Run.user_id` as the acting user to `Run.acting_user_id`:
  - `daiv/sessions/queue.py` (`aenqueue_run`);
  - `daiv/sessions/services.py` (`aget_task_run` returns it as `user_id`);
  - `daiv/sessions/inbox/drain.py` (the run user's environments and access, at the sites R6 names);
  - `daiv/sessions/admission.py` (the MCP pool and `aresolve_repo_envs` step 2).

  T012 and T013 pass.

**Checkpoint**: mentions behave as before, from their new home. `make test` is green, and runs can be attributed without lending authority.

---

## Phase 3: User Story 1 - Run a prompt on a merge request from CI or a script (Priority: P1) 🎯 MVP

**Goal**: `POST /api/jobs` and the MCP job tool accept `repos[].merge_request`. The run goes where a mention by the caller would, and its final message is posted on the merge request (FR-001–FR-006).

**Independent Test**: quickstart steps 1 and 2. A job naming an open merge request produces one review comment, and a second job after a push reviews only the new commit. A job on a DAIV-owned merge request waits for the owning session and runs as a turn of it.

### Tests for User Story 1

> Write these tests first and make sure they fail before implementing.

- [ ] T016 [P] [US1] Failing tests in `tests/unit_tests/jobs/api/test_views.py` for every row of the validation table in contracts/jobs-api.md:
  - `merge_request` with `ref` or a per-repository `prompt` → `422`;
  - with `shared_session: true` or `thread_id` → `400` with "A merge request target can't be combined with shared_session or thread_id.";
  - more than one `repos` entry → `400`;
  - a missing or other-repository merge request → a `failed` entry "Merge request !{iid} was not found in {repo_id}.";
  - a closed or merged one → "Merge request !{iid} is closed." / "Merge request !{iid} is merged.";
  - no write access → `404`.
- [ ] T017 [P] [US1] Failing tests in `tests/unit_tests/jobs/api/test_views.py` for routing:
  - **Unowned merge request:** the response is `READY`. Exactly one `Run` exists with `trigger_type="api_job"`, `merge_request_iid` set, and session id `compute_thread_id(repo_id, MERGE_REQUEST, iid)`. `address_merge_request_task` is enqueued with `request_key` starting `job:`.
  - **Owned merge request:** a `MERGE_REQUEST_JOB` inbox event is posted to the owning session, and the response is `QUEUED` while the session is busy.
  - **Status:** `GET /api/jobs/{id}` returns `merge_request_url` for the merge request.
- [ ] T018 [P] [US1] Failing tests in `tests/unit_tests/sessions/test_models.py`:
  - two `READY` `api_job` runs in one session, both with `merge_request_iid`, are allowed;
  - two without `merge_request_iid` still raise `IntegrityError`;
  - `test_active_constraint_literals_match_enums` also asserts the `merge_request_iid__isnull=True` condition.
- [ ] T019 [P] [US1] Failing tests in `tests/unit_tests/sessions/merge_request_runs/test_service.py`. With a `Request` input on the unowned path, `astart`:
  - records `trigger_type` from `Request.source`;
  - sets `acts_as_user`, `muted` and the requester's `user` and `external_*` on the run;
  - deduplicates on `request_key`;
  - answers an archived session with the archived reply.

  On the owned path it posts `MERGE_REQUEST_JOB` with payload `{prompt, agent_model?, acts_as_user, source}` and source key `job({run_id})`.
- [ ] T020 [P] [US1] Failing tests in `tests/unit_tests/sessions/merge_request_runs/test_addressor.py`. With a `Request` input:
  - the agent input is `HumanMessage(name=requester.username, content=prompt)`;
  - a response becomes one top-level merge request comment;
  - a question becomes a top-level comment, because there is no mention thread;
  - a failure posts the unable note;
  - `ask_user=False` reaches the run spec as `ask_user_enabled=False`.
- [ ] T021 [P] [US1] Failing tests in `tests/unit_tests/sessions/inbox/test_kinds.py`, `test_render.py`, `test_complete.py` and `test_drain.py` for `MERGE_REQUEST_JOB`:
  - it wakes;
  - its run's trigger type is `payload.source`;
  - it renders the prompt as a request from the requester, with `MENTION_REPLY_NOTE`'s wording;
  - the reply is a new top-level comment;
  - after `MAX_EVENT_ATTEMPTS` it posts the "could not complete" note;
  - `acts_as_user` from the payload reaches the run.
- [ ] T022 [P] [US1] Failing tests in `tests/unit_tests/mcp_api/test_server_jobs.py`: the job tool accepts `merge_request` with the same rules and routing as the jobs API.

### Implementation for User Story 1

- [ ] T023 [US1] In `daiv/sessions/models.py`, add `merge_request_iid__isnull=True` to the condition of the `run_one_active_per_session` constraint, with migration `daiv/sessions/migrations/0044_run_active_constraint_merge_request.py`. Update the constraint's comment to say merge-request runs share deterministic sessions like webhook runs (two lines at most). T018 passes.
- [ ] T024 [US1] In `daiv/jobs/api/schemas.py`, add `merge_request: int | None = Field(default=None, ge=1)` to `RepoSubmitItem`. A validator refuses `merge_request` together with `ref` or `prompt` (`422`), and a request-level check returns the `400`s of T016.
- [ ] T025 [US1] Add the `Request` input (`prompt`, `source: SessionOrigin`, `request_key`) to `daiv/sessions/merge_request_runs/service.py`.
  - **Unowned path:** `astart` passes `trigger_type=source`, `merge_request_iid`, `acts_as_user`, `muted` and `ask_user` through `RunStarter`, and enqueues `address_merge_request_task` with `request_key`.
  - **Owned path:** it posts `EventKind.MERGE_REQUEST_JOB` through `Inbox.apost` and returns the joined next run.

  T019 passes.
- [ ] T026 [US1] In `daiv/sessions/merge_request_runs/addressor.py` and `tasks.py`, build the agent input from a `Request` and post its answer or question as a top-level comment. Thread `ask_user` into the run spec. T020 passes.
- [ ] T027 [US1] Add `EventKind.MERGE_REQUEST_JOB` in `daiv/sessions/inbox/kinds.py` (wakes; trigger type from `payload["source"]`).
  - `daiv/sessions/inbox/render.py`: render the prompt with the mention note's wording.
  - `daiv/sessions/inbox/complete.py`: post the reply as a top-level comment when the payload has no `discussion_id`, and the failure note after `MAX_EVENT_ATTEMPTS`.
  - `daiv/sessions/inbox/drain.py`: read `acts_as_user`.

  T021 passes.
- [ ] T028 [US1] In `daiv/sessions/multi_repo.py` and `daiv/sessions/services.py`, route a `repos` entry with `merge_request` to `MergeRequestRuns.astart`. Use `Request(source=API_JOB or MCP_JOB, request_key=f"job:{uuid4()}")` and `Requester(user=caller, …)`.
  - **Checks:** first check the merge request with `RepoClient.get_merge_request` (it exists in this repository and is open), and return the `failed` messages of T016 verbatim.
  - **Admission:** keep `aadmit` and its write-access check unchanged.
- [ ] T029 [US1] In `daiv/jobs/api/views.py`, map `StartOutcome` to `JobSubmitJobItem` (`READY` or `QUEUED`). T016 and T017 pass.
- [ ] T030 [US1] In `daiv/mcp_api/server.py`, accept `merge_request` on the job tool and call the same `sessions` path. T022 passes.

**Checkpoint**: US1 works on its own with an API key (quickstart steps 1–2).

---

## Phase 4: User Story 2 - CI signs in as the pipeline, and the run belongs to whoever triggered it (Priority: P2)

**Goal**: the jobs API accepts a GitLab CI ID token or a GitHub Actions OIDC token. The run is attributed to the pipeline's triggering account and acts as no one (FR-007–FR-013, SR1, SR2).

**Independent Test**: quickstart step 3. A pipeline's token submits a job attributed to the pusher. Wrong-audience, other-project and expired tokens get `401`. Polling with another project's token gets `404`.

### Tests for User Story 2

- [ ] T031 [P] [US2] Failing tests in `tests/unit_tests/accounts/api/test_ci_identity.py`, for GitLab and for GitHub, with an in-test RSA key and a stubbed signing-key endpoint:
  - **Accepted:** a valid token yields a `CIIdentity` with `provider`, `repo_id`, `repo_platform_id`, `actor_uid`, `actor_username`, `user` and `sha`, and `pk == f"ci:{provider}:{actor_uid}"`.
  - **Refused, without saying which check failed:** another `aud`; another `iss`; an expired token; one outside the 60 s leeway; a non-`RS256` algorithm; a bad signature; a `project_id`/`repository_id` that differs from the repository's platform id (a fork).
  - **Not a token:** a non-JWT bearer value returns `None`, so an API key still authenticates through `AuthBearer`.
  - **Unreachable keys:** an unreachable signing-key endpoint refuses and logs at ERROR.
  - **Issuers:** `CODEBASE_GITHUB_ACTIONS_OIDC_ISSUER` overrides the GitHub issuer. The GitLab issuer is `CODEBASE_GITLAB_URL`, else `https://gitlab.com`.
- [ ] T032 [P] [US2] Failing tests in `tests/unit_tests/jobs/api/test_views.py` for token-authenticated submissions:
  - more than one `repos` entry, or any of `thread_id`, `shared_session` or `references` → `400` with "A CI identity token submits one job on its own repository.";
  - `mcp_overrides` naming a personal server → `400`;
  - `allowed_usernames` set without the actor's login → `401`.
  - **Attribution:**
    - a linked actor gives `Run.user` set and `acts_as_user=False`;
    - an unlinked actor gives `user=None` with `external_provider`, `external_uid` and `external_username` set;
    - `muted` defaults to `True`, and an explicit `"muted": false` gives `False`.
  - **Throttle:** keyed by `ci:{provider}:{actor_uid}` (clear the cache first).
- [ ] T033 [P] [US2] Failing tests in `tests/unit_tests/jobs/api/test_views.py` for polling with a token:
  - `200` for a run on the token's repository whose person is the actor, either the linked `user` or a matching `external_provider` and `external_uid`;
  - `404` for any other run.

### Implementation for User Story 2

- [ ] T034 [P] [US2] Add the deployment settings:
  - `JOBS_CI_IDENTITY_AUDIENCE: str | None`, default `None` meaning `EXTERNAL_URL`, in `daiv/core/conf.py` (env `DAIV_JOBS_CI_IDENTITY_AUDIENCE`);
  - `GITHUB_ACTIONS_OIDC_ISSUER: str = "https://token.actions.githubusercontent.com"` in `daiv/codebase/conf.py` (env `CODEBASE_GITHUB_ACTIONS_OIDC_ISSUER`).
- [ ] T035 [US2] Create `daiv/accounts/api/ci_identity.py` with three classes:
  - **`CIIdentityVerifier`:**
    - one `jwt.PyJWKClient` per issuer, cached at module level;
    - signing keys from `{iss}/oauth/discovery/keys` (GitLab) or `{iss}/.well-known/jwks` (GitHub);
    - `algorithms=["RS256"]`, `leeway=60`;
    - requires `iss`, `aud`, `exp`, `iat`;
    - claim mapping per contracts/jobs-api.md;
    - the repository id compared with the platform id from `RepoClient`. Add the id to the `Repository` value object in `daiv/codebase/base.py` and both clients if it is missing, with a test in `tests/unit_tests/codebase/clients/`.
  - **`CIIdentity`:** the in-memory principal of data-model.md.
  - **`CIIdentityBearer(HttpBearer)`:** returns `None` for anything that isn't a three-segment JWT whose header has a `kid`, and raises `401` with `{"detail": "Unauthorized"}` for a token that fails. Failures log at ERROR only when the issuer is unreachable.

  T031 passes.
- [ ] T036 [US2] In `daiv/jobs/api/views.py`:
  - set `jobs_router` to `auth=[CIIdentityBearer(), AuthBearer()]`;
  - for a `CIIdentity`, enforce the submission rules of T032 and check `allowed_usernames` against `actor_username` with `RepositoryConfig.is_user_allowed`;
  - default `muted` to `True` unless `"muted"` is in the request's set fields;
  - make `get_job_status` apply the visibility rule of T033 to a `CIIdentity`.
- [ ] T037 [US2] In `daiv/sessions/multi_repo.py`, `daiv/sessions/services.py` and `daiv/sessions/admission.py`, accept a requester that is a `CIIdentity`:
  - **Run fields:** `user=identity.user`, `external_provider`, `external_uid`, `external_username` and `acts_as_user=False`, also for merge request targets through `Requester(acts_as_user=False)`.
  - **Access (SR1):** keep the catalog check and skip the `RepositoryAccess` write check, because the verified token is the access proof.
  - **Overrides:** resolve `mcp_overrides` with `user=None`.

  T032 passes.
- [ ] T038 [US2] In `daiv/core/api/throttling.py`, make `JobsRateThrottle` key a `CIIdentity` on its `pk`. T032's throttle case and T033 pass.

**Checkpoint**: US1 and US2 work independently. A CI job can review a merge request with no stored DAIV secret (quickstart step 3).

---

## Phase 5: User Story 3 - Repository automations that need no CI (Priority: P3)

**Goal**: `.daiv.yml` automations run a prompt on merge request opened, updated and ready events. A burst of pushes becomes one run, each commit fires at most once, and runs are attributed to the actor, muted and non-interactive (FR-014–FR-023).

**Independent Test**: quickstart step 4. An automation merged to the default branch reviews a new merge request about 60 seconds after it opens. Five quick pushes give one run. A re-delivered webhook gives none. A draft waits until it is marked ready.

### Tests for User Story 3

- [ ] T039 [P] [US3] Failing tests in `tests/unit_tests/codebase/test_repo_config.py` for `automations`:
  - **Accepted:** a valid entry parses.
  - **Dropped with an ERROR log naming the repository and reason, while the other entries apply:**
    - a `name` not matching `^[a-z0-9][a-z0-9-]{0,63}$`;
    - an empty `on` or an unknown event;
    - a blank `prompt`;
    - an unknown key in an entry or in `filters`;
    - a later duplicate `name`.
  - **Wrong type:** a non-list `automations` drops only that section, with an ERROR log.
  - **Cache:** the section round-trips through the config cache (`model_dump(exclude_unset=True)` then `RepositoryConfig(**cached)`).
- [ ] T040 [P] [US3] Failing tests in `tests/unit_tests/webhooks/gitlab/test_callbacks.py` and `tests/unit_tests/webhooks/github/test_callbacks.py` for every mapping row in contracts/webhook-events.md.
  - **Events:** GitLab `open`; `update` with `oldrev`; `update` with `changes.draft` going `true` → `false`; `update` with both. GitHub `opened`, `synchronize`, `ready_for_review`.
  - **Normalised event:** each yields the right `kind`, `head_sha`, `target_branch`, `draft` and `actor`.
  - **Ignored:** title or label updates and other GitHub actions.
  - **Unchanged:** close, merge and reopen keep their behaviour.
- [ ] T041 [P] [US3] Failing tests in `tests/unit_tests/sessions/automations/test_service.py` for `Automations.aon_event`:
  - **No firing when:** `automations_enabled` is false; there are no automations; `on` doesn't include the event; `drafts=false` on a draft; `target_branches` doesn't match; an owned merge request without `daiv_merge_requests`; `allowed_usernames` excludes the actor.
  - **Matching events:** the service upserts one `pending` firing with `due_at = now + AUTOMATION_SETTLE_DELAY`, marks the older pending firing `superseded`, and enqueues `fire_automation_task` with `run_after=due_at`.
  - **Re-delivery:** a re-delivered payload changes nothing.
- [ ] T042 [P] [US3] Failing tests in `tests/unit_tests/sessions/automations/test_firings.py` for `AutomationFirings.afire`:
  - **Exits:** a firing that is no longer `pending`, or not yet due, exits.
  - **Statuses:**
    - `closed` when the merge request is closed or merged, or its head moved;
    - `disabled` when the site switch is off or the automation is gone from `.daiv.yml`;
    - `filtered` when a complete changed-path list matches no `paths` glob (with `!` exclusions);
    - an incomplete list starts the run and logs at INFO.
  - **A fired run has:** `trigger_type="automation"`, session id `compute_automation_thread_id(repo_id, name, iid)` and title `"{name} on !{iid}"`, `user` (the actor's linked account) and `external_*`, `acts_as_user=False`, `muted=True`, `ask_user_enabled=False`, `request_key=f"firing:{id}"`. The firing is `fired`, with `run` set.
  - **Failure:** a failed run posts "Automation `{name}` could not complete on this commit."
- [ ] T043 [P] [US3] Failing tests in `tests/unit_tests/codebase/clients/gitlab/` and `tests/unit_tests/codebase/clients/github/` for `get_merge_request_changed_paths`:
  - a rename gives both paths;
  - GitLab `overflow` → `complete=False`;
  - GitHub pages through `get_files()` and reports `complete=False` at the platform's 3,000-file cap.
- [ ] T044 [P] [US3] Failing tests in `tests/unit_tests/core/test_site_settings.py` and `test_site_configuration.py`:
  - `automations_enabled` defaults to `True`, and `automations_max_daiv_push_runs` to `3` with a minimum of 1;
  - env vars `DAIV_AUTOMATIONS_ENABLED` and `DAIV_AUTOMATIONS_MAX_DAIV_PUSH_RUNS` win over the database;
  - the **Automations** group matches `automations_*`.
- [ ] T045 [P] [US3] Failing test in `tests/unit_tests/sessions/test_ids.py`: `compute_automation_thread_id(repo_id, name, iid)` is stable, and differs by repository, name and iid.

### Implementation for User Story 3

- [ ] T046 [US3] Add `SessionOrigin.AUTOMATION = "automation", _("Automation")` in `daiv/sessions/models.py`.
  - **Membership:** keep it out of `PERSON_STARTED`, `webhooks()`, `_INBOX_TRIGGER_LITERALS` and `run_one_active_per_session`.
  - **Dashboards:** add it to `ORIGIN_SEGMENT_CSS` in `daiv/dashboard/insights.py`, and to `_started_from` in `daiv/dashboard/home.py` as "Started by an automation".
  - **Migration:** `daiv/sessions/migrations/0045_automations.py` alters the choices of `Run.trigger_type`, `Session.origin` and `SpendRecord.trigger_type`, and drops and re-adds `run_trigger_type_valid` and `session_origin_valid`, following `0017_delegated_jobs.py`.
  - **Tests:** update `tests/unit_tests/sessions/test_models.py` and `tests/unit_tests/dashboard/test_views.py` membership tests.
- [ ] T047 [US3] Add `AutomationFiring` to `daiv/sessions/models.py` in the same migration `0045_automations.py`, with every field of data-model.md verbatim:
  - **Fields:**
    - `id` UUID pk;
    - `repo_id` `CharField(255)` indexed; `merge_request_iid` `PositiveIntegerField`; `automation` `CharField(64)`; `head_sha` `CharField(64)`;
    - `event` choices `opened`, `updated`, `ready`;
    - `actor_provider`, `actor_uid`, `actor_username` `CharField`; `actor` FK `User` null;
    - `daiv_commit` `BooleanField` null;
    - `status` choices `pending`, `fired`, `superseded`, `automation_commit`, `capped`, `filtered`, `closed`, `disabled`;
    - `due_at` `DateTimeField`;
    - `run` FK `Run` null `SET_NULL`;
    - `cap_notice_posted` `BooleanField(default=False)`;
    - `created_at`, `updated_at`.
  - **Constraints and index:**
    - unique (`repo_id`, `merge_request_iid`, `automation`, `head_sha`);
    - partial unique (`repo_id`, `merge_request_iid`, `automation`) where `status='pending'`;
    - index (`repo_id`, `merge_request_iid`, `status`, `created_at`).
- [ ] T048 [P] [US3] Add `compute_automation_thread_id(repo_slug, automation, iid)` to `daiv/sessions/ids.py`, a UUID from `"{slug}:automation:{name}/{iid}"` in the same scheme as `compute_thread_id`. T045 passes.
- [ ] T049 [P] [US3] Add the section to `daiv/codebase/repo_config.py`:
  - **Models:** `AutomationFilters` (`drafts: bool = False`, `target_branches: list[str] = []`, `paths: list[str] = []`, `daiv_merge_requests: bool = False`, `extra="forbid"`) and `Automation` (`name`, `on`, `prompt`, `filters`, `extra="forbid"`).
  - **Field:** `RepositoryConfig.automations`, parsed one entry at a time with `SkipValidation` and a `BeforeValidator`, after the `ModelChoiceMap` pattern in `daiv/core/model_choice.py`.

  T039 passes.
- [ ] T050 [P] [US3] Add the site settings: `automations_enabled` (`BooleanField(null=True)`) and `automations_max_daiv_push_runs` (`PositiveIntegerField(null=True, validators=[MinValueValidator(1)])`).
  - `daiv/core/models.py`: the fields, plus `FieldGroup(key="automations", match=("automations_*",), category="Runtime")`.
  - `daiv/core/site_settings.py`: defaults `True` and `3`, and the `SiteSnapshot` fields.
  - `daiv/configuration/forms.py`: the form fields.
  - Migration `daiv/core/migrations/0028_automations_settings.py`.

  T044 passes.
- [ ] T051 [P] [US3] Add `ChangedPaths(paths: list[str], complete: bool)` and abstract `get_merge_request_changed_paths(repo_id, merge_request_id)` in `daiv/codebase/clients/base.py`. Implement it in `daiv/codebase/clients/gitlab/client.py` (`mr.changes()`, with `overflow` → incomplete) and `daiv/codebase/clients/github/client.py` (paged `pr.get_files()`). T043 passes.
- [ ] T052 [US3] In `daiv/webhooks/gitlab/models.py`, add `MergeRequestEvent.oldrev: str | None`, `last_commit` (`id`), `draft: bool = False`, and a top-level `changes` with `draft.previous` and `draft.current`.
  - `daiv/webhooks/gitlab/callbacks.py`: `MergeRequestCallback.accept_callback` also accepts `open` and `update`. `process_callback` builds the normalised event per contracts/webhook-events.md and calls `Automations(repo_id).aon_event`. An `update` with both `oldrev` and a draft change yields `updated` then `ready`.
- [ ] T053 [US3] In `daiv/webhooks/github/callbacks.py`, the pull request callback also accepts `opened`, `synchronize` and `ready_for_review`, and calls `Automations(repo_id).aon_event` with the normalised event. Add any missing fields to `daiv/webhooks/github/models.py`. T040 passes.
- [ ] T054 [US3] Create `daiv/sessions/automations/__init__.py` and `daiv/sessions/automations/service.py` with `Automations(repo_id).aon_event(event)`, per contracts/webhook-events.md, "What `aon_event` does":
  - `AUTOMATION_SETTLE_DELAY = timedelta(seconds=60)`;
  - matching on `on`, `drafts`, `target_branches` (fnmatch), `daiv_merge_requests` (an open `MergeRequestOwnership`) and `allowed_usernames`;
  - the upsert, supersede and enqueue in one transaction.

  T041 passes.
- [ ] T055 [US3] Create `daiv/sessions/automations/firings.py` with `AutomationFirings.afire(firing_id)` and `daiv/sessions/automations/tasks.py` with `fire_automation_task`.
  - **Checks:** due, still pending, open, same head, switch on and automation still declared.
  - **Paths filter:** applied through `get_merge_request_changed_paths`.
  - **Start:** the actor resolves through `resolve_linked_user`. The run starts through `MergeRequestRuns.astart(Requester(..., acts_as_user=False), Request(prompt, source=AUTOMATION, request_key=f"firing:{id}"), title=f"{name} on !{iid}", thread_id=compute_automation_thread_id(...), muted=True, ask_user=False)`.
  - **Status:** recorded on the firing.

  T042 passes, except the failure text (T056).
- [ ] T056 [US3] In `daiv/sessions/merge_request_runs/addressor.py`, an automation run's failure note reads "Automation `{name}` could not complete on this commit." T042 passes in full.

**Checkpoint**: US1–US3 work independently. Automations run with no CI (quickstart step 4).

---

## Phase 6: User Story 4 - Automations on DAIV's own merge requests, without loops (Priority: P4)

**Goal**: an opted-in automation runs when DAIV pushes to its own merge request, in a session of its own. A commit an automation pushed never fires, and runs on DAIV's commits are capped per merge request until a person pushes (FR-024–FR-027).

**Independent Test**: quickstart step 5. An opted-in review runs after DAIV's push in "review on !N", not in the owning session. An always-pushing automation runs once per person's push. Four DAIV pushes in a row give three runs and one cap comment.

### Tests for User Story 4

- [ ] T057 [P] [US4] Failing tests in `tests/unit_tests/sessions/test_commits.py`: `DaivCommits(repo_id).ais_daiv(sha)` is true for an ownership's `last_daiv_sha`, any run's `pushed_sha` and any session's `sibling_pushed_shas`, and false otherwise. `tests/unit_tests/sessions/inbox/test_ownership.py` still passes for `aclassify_push`.
- [ ] T058 [P] [US4] Failing tests in `tests/unit_tests/sessions/automations/test_firings.py`:
  - **Automation commit:** a head commit pushed by an `automation` run becomes `automation_commit` and starts nothing.
  - **Cap:** with `automations_max_daiv_push_runs=3`, three DAIV-commit firings fire, and the fourth becomes `capped`, posts the cap comment once and sets `cap_notice_posted`. A fifth capped firing posts nothing.
  - **Reset:** a person's commit fires and resets the count.
  - **Recorded:** `daiv_commit` is recorded on every firing that reaches the guards.
- [ ] T059 [P] [US4] Failing tests in `tests/unit_tests/sessions/automations/test_service.py` and `test_firings.py` for an owned merge request:
  - **Opted in** (`daiv_merge_requests=True`): it fires, in the `compute_automation_thread_id` session and not the owning one.
  - **Owning session untouched:** the owning session gets no inbox event from the automation's run or comment (FR-025).
  - **Not opted in:** it does not fire.

### Implementation for User Story 4

- [ ] T060 [US4] Create `daiv/sessions/commits.py` with `DaivCommits(repo_id)` and `ais_daiv(sha)`, extracting the checks from `Ownerships.aclassify_push` in `daiv/sessions/inbox/ownership.py`, which now calls it. T057 passes.
- [ ] T061 [US4] In `daiv/sessions/automations/firings.py`, before starting a run, apply the guards:
  - **Automation commit:** skip as `automation_commit` when `Run.objects.filter(repo_id=…, trigger_type=AUTOMATION)` has `pushed_sha == head_sha`, or `head_sha` in `sibling_pushed_shas`.
  - **DAIV commit:** set `daiv_commit` from `DaivCommits.ais_daiv`.
  - **Cap:** when `daiv_commit` is true, count the `fired` rows on the merge request with `daiv_commit=True` created after the newest `fired` row with `daiv_commit=False`. At or above `site_settings.automations_max_daiv_push_runs`, record `capped`. The first `capped` row since that person row posts "Automations are paused on this merge request after {n} runs started by DAIV's own commits. They resume on the next push by a person." and sets `cap_notice_posted`.

  T058 passes.
- [ ] T062 [US4] Confirm `Automations.aon_event` and `afire` route an opted-in owned merge request to the automation's own session, through the `thread_id` argument of `MergeRequestRuns.astart`, which bypasses the owned-session branch. Add nothing that posts to the owning session's inbox. T059 passes.

**Checkpoint**: all four stories work independently (quickstart step 5).

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: documentation (Principle VII), the glossary, the changelog, evals (Principle V) and the gates.

- [ ] T063 [P] Write `docs/features/automations.md`, with a `description` in its front matter, covering:
  - what an automation is;
  - the `.daiv.yml` example and entry table from contracts/repository-config.md;
  - the three events;
  - the filters, including the incomplete-paths rule (SR3);
  - the 60-second settle and one run per commit;
  - attribution, mute and no questions;
  - DAIV's own merge requests and the two loop guards, with the cap and its comment;
  - the site switch.

  Add it to `nav` and `llmstxt` in `mkdocs.yml`.
- [ ] T064 [P] Update `docs/features/jobs-api.md`:
  - `repos[].merge_request` in **Request fields**, with its validation and where the run goes;
  - a "CI identity tokens" section with the GitLab `id_tokens` and GitHub Actions examples, the checks, attribution, mute default (SR2) and polling rule from contracts/jobs-api.md;
  - replace the comment-posting workaround with the code-review-on-every-push example (FR-028).
- [ ] T065 [P] Update `docs/features/pull-request-assistant.md`: under **Review reports**, link to automations and the jobs API merge request target as the way to review every push.
- [ ] T066 [P] Update `docs/customization/repository-config.md`: an `automations` row in the section table, and a subsection with the entry and filter keys, so `test_docs_reference.py` passes.
- [ ] T067 [P] Update the reference pages so `test_docs_reference.py` and `test_env_variables_doc.py` pass:
  - `docs/reference/site-configuration.md`: the **Automations** group (category Runtime);
  - `docs/reference/env-variables.md`: `DAIV_AUTOMATIONS_ENABLED`, `DAIV_AUTOMATIONS_MAX_DAIV_PUSH_RUNS`, `DAIV_JOBS_CI_IDENTITY_AUDIENCE` and `CODEBASE_GITHUB_ACTIONS_OIDC_ISSUER`.
- [ ] T068 [P] Update `docs/features/sessions.md` and `docs/features/spend.md` for the **Automation** origin and trigger type.
- [ ] T069 [P] Add **Automation** ("A `.daiv.yml` entry that runs a prompt on a merge request when it is opened, updated or marked ready") and **Automation firing** ("One automation's decision on one merge request head commit: pending, fired, or skipped with a reason (`AutomationFiring`)") to `constitutions/glossary/README.md`.
- [ ] T070 [P] Add three `Added` lines under **Unreleased** in `CHANGELOG.md`, in Common Changelog form, each linking the pull request:
  - the jobs API merge request target;
  - CI identity tokens;
  - repository automations.
- [ ] T071 Run `make lint-fix` scoped to the changed paths (repo-wide unsafe fixes can strip untracked files), then `make lint`. Run `uv run --only-group=dev djade --target-version 6.0` on any new template, and `rm -rf .ruff_cache` before trusting a clean ruff result.
- [ ] T072 Run `make lint-typing`. Fix the new modules' diagnostics, and add no file to `[[tool.ty.overrides]]`.
- [ ] T073 Run `make test`. Every task's tests pass, and failures are compared with T003's baseline.
- [ ] T074 Run `uv run --only-group=docs mkdocs build --strict`, `tests/unit_tests/test_docs_style.py` and `tests/unit_tests/test_docs_links.py`.
- [ ] T075 Run quickstart.md steps 1–5 against the local stack (`make up`), and record what was run and what was not in the pull request.
- [ ] T076 Run `make eval-prompts SUITES="inbox code_review" OUT=eval-runs/005-after.jsonl`, then `uv run evals/compare_runs.py eval-runs/005-before.jsonl eval-runs/005-after.jsonl`. Confirm any `unconfirmed` verdict with its printed re-run, and put the table in the pull request (Principle V).

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)**: T003–T005 first. T004 must run before any code change.
- **Foundational (Phase 2)**: depends on Setup and blocks every story. Within it, T006 → T007 → T008 → T009 → T010 → T011 run in order, because they move code between the same modules. T012–T015 can run alongside them, since they touch other files.
- **US1 (Phase 3)**: depends on Phase 2.
- **US2 (Phase 4)**: depends on Phase 2 (`acts_as_user`). Its merge-request-targeted case reuses US1's `Request` path (T025), but a branch-target token job works without US1.
- **US3 (Phase 5)**: depends on Phase 2, and on US1's T025–T026 for the `Request` input it starts runs with.
- **US4 (Phase 6)**: depends on US3.
- **Polish (Phase 7)**: depends on the stories being shipped. T063 needs US3–US4, T064 needs US1–US2.

### Within each story

Tests come first and must fail. Then models and migrations, then services, then endpoints and callbacks.

### Parallel opportunities

- **Phase 2:** T012 and T013 alongside the moves.
- **US1:** T016–T022, all tests in different files.
- **US2:** T031–T033, and T034 alongside the tests.
- **US3:**
  - tests T039–T045;
  - then T048, T049, T050 and T051 together, before T052–T055.
- **US4:** T057–T059.
- **Polish:** T063–T070, all different files.

---

## Parallel Example: User Story 3

```text
# Tests, together:
T039 tests/unit_tests/codebase/test_repo_config.py
T040 tests/unit_tests/webhooks/{gitlab,github}/test_callbacks.py
T041 tests/unit_tests/sessions/automations/test_service.py
T042 tests/unit_tests/sessions/automations/test_firings.py
T043 tests/unit_tests/codebase/clients/{gitlab,github}/
T044 tests/unit_tests/core/test_site_settings.py, test_site_configuration.py
T045 tests/unit_tests/sessions/test_ids.py

# Then independent building blocks, together:
T048 daiv/sessions/ids.py
T049 daiv/codebase/repo_config.py
T050 daiv/core/models.py, site_settings.py, configuration/forms.py, core/migrations/0028
T051 daiv/codebase/clients/{base,gitlab/client,github/client}.py
```

---

## Implementation Strategy

### MVP first (US1)

1. Phase 1, then Phase 2. Mentions keep working from their new home, and `make test` is green.
2. Phase 3 (US1). Stop and validate with quickstart steps 1–2. CI can then review every push with an API key, with results on the merge request, which already replaces both workarounds.

### Incremental delivery

1. **US2:** keyless CI and per-person spend. Validate with quickstart step 3.
2. **US3:** automations with no CI. Validate with quickstart step 4.
3. **US4:** DAIV's own merge requests and the loop guards. Validate with quickstart step 5.
4. **Polish:** docs, glossary, changelog, gates and evals. Each story can ship in its own pull request, provided its docs and changelog lines land with it (Principle VII).
