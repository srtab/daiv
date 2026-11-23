---

description: "Task list for merge request targets and CI identity"
---

# Tasks: Merge Request Automations (user stories 1 and 2)

**Input**: Design documents from `specs/005-merge-request-automations/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md).

**Scope**: user stories 1 and 2. Repository automations (stories 3 and 4) are excluded from the plan and deferred; see plan.md *Scope*.

**Tests**: Included. Constitution principle IV and `patterns/tests.md` require a test that fails without each behaviour change.
- Tests stub every git-platform client, model and the sandbox, and make no network call.
- CI tokens are signed in the test with a generated RSA key, and the platform's signing-key endpoint is stubbed.
- An async test that writes uses `django_db(transaction=True)`.
- A test that touches the jobs throttle clears the cache first.

Principle V applies: evals run before and after (T004, T048).

**Organization**: Tasks are grouped by user story so each story can be built and tested on its own. US1 is the MVP.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: the user story the task belongs to (US1, US2)

## Path Conventions

- Paths are relative to the repository root: `daiv/`, `tests/unit_tests/`, `docs/`.
- Tests mirror `daiv/`, one file per module, and import without the `daiv.` prefix.
- Names, texts, defaults and limits quoted below come from the contracts and the data model. Use them verbatim.

---

## Phase 1: Setup

**Purpose**: approval, the refined spec, a clean baseline and the one new direct dependency.

- [X] T001 Record the maintainer's approval of plan.md and refinements SR1–SR3 in the **Status** line of specs/005-merge-request-automations/plan.md (done 2026-11-21; the plan updated after clarification and analysis was approved again the same day).
- [X] T002 Apply refinements SR1–SR3 from specs/005-merge-request-automations/research.md to specs/005-merge-request-automations/spec.md, in FR-005, FR-011 and FR-016 (done 2026-11-21).
- [X] T003 Confirm the next free migration number. After the rebase onto `main` on 2026-11-21 it was `daiv/sessions/migrations/0045`; renumber the migrations named below if `main` has moved again. Run `make test` once before any change, and note pre-existing failures so later ones can be told apart.
- [ ] T004 Take the eval baseline before any code change: `make eval-prompts SUITES="inbox code_review" OUT=eval-runs/005-before.jsonl`. It needs the LLM key and GitLab credentials (AGENTS.md).
- [X] T005 Add PyJWT as a direct, pinned dependency with `uv add "pyjwt[crypto]==2.15.1"`, the version already in uv.lock. This updates pyproject.toml and uv.lock. Do not edit either by hand.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**:
- Move the direct merge request path from `webhooks` into `sessions/merge_request_runs/` (research R1), with mention behaviour unchanged.
- Add `Run.acts_as_user` (R6).

US1 starts runs through the moved service, and both stories carry `acts_as_user` through it.

**⚠️ CRITICAL**: no user story work can begin until this phase is complete.

### Moving the direct merge request path (R1, contracts/merge-request-runs.md)

- [X] T006 Create the package `daiv/sessions/merge_request_runs/` with `__init__.py`. Move `daiv/webhooks/managers/base.py` to `daiv/sessions/merge_request_runs/base.py`, keeping its class names and behaviour. Point `daiv/webhooks/managers/issue_addressor.py` at the new module. Move `tests/unit_tests/webhooks/managers/test_base.py` to `tests/unit_tests/sessions/merge_request_runs/test_base.py`, adjusting imports and patch targets only.
- [X] T007 Move `daiv/webhooks/managers/review_addressor.py` to `daiv/sessions/merge_request_runs/addressor.py`, with behaviour unchanged. Move `tests/unit_tests/webhooks/managers/test_review_addressor.py` and `test_review_addressor_footer.py` to `tests/unit_tests/sessions/merge_request_runs/test_addressor.py`, adjusting imports and patch targets only. Move the shared fixtures of `tests/unit_tests/webhooks/managers/conftest.py` that these tests use into `tests/unit_tests/sessions/merge_request_runs/conftest.py`.
- [X] T008 Move `_start_run`, `_reply_archived`, `_pinned_model`, `Author` and `Mention` from `daiv/webhooks/dispatch.py` into `daiv/sessions/merge_request_runs/starter.py`, as class `RunStarter`.
  - It keeps today's signature and behaviour: the task, the requester, the scope, the iid, the task kwargs, the trigger type, the title, the reply and the ref.
  - `Author` becomes `Requester`, with fields `user: User | None`, `provider`, `uid`, `username` and `acts_as_user: bool = True`.
  - The issue paths in `daiv/webhooks/dispatch.py` call `RunStarter` too, because it serves the `ISSUE` and `MERGE_REQUEST` scopes alike.
  - Move the matching tests from `tests/unit_tests/webhooks/test_dispatch.py` into `tests/unit_tests/sessions/merge_request_runs/test_starter.py`.
- [X] T009 Create `MergeRequestRuns(client, repo_id, merge_request_iid)` in `daiv/sessions/merge_request_runs/service.py`.
  - `astart(requester, input, *, title, use_max=False, muted=None) -> StartOutcome(run_id, status, archived)` holds the owned-versus-unowned branching of today's `dispatch_mr_comment_run`, including `_post_comment` and `_fetch_source_branch`.
  - At this stage `input` is only a `Mention`.
  - `daiv/webhooks/dispatch.py` `dispatch_mr_comment_run` becomes a thin call to it.
  - Move the merge-request mention tests of `tests/unit_tests/webhooks/test_dispatch.py` into `tests/unit_tests/sessions/merge_request_runs/test_service.py`.
- [X] T010 Move `address_mr_comments_task` from `daiv/webhooks/tasks.py` to `daiv/sessions/merge_request_runs/tasks.py` as `address_merge_request_task`.
  - It deduplicates on (`repo_id`, `merge_request_id`, `request_key`), and a mention passes `request_key=f"note:{mention_note_id}"`.
  - Keep `address_mr_comments_task` in `daiv/webhooks/tasks.py` as a one-release alias that accepts the old keyword arguments and forwards to the new task, so task rows already queued under the old path still run.
  - Move its tests to `tests/unit_tests/sessions/merge_request_runs/test_tasks.py`, and add a test in `tests/unit_tests/webhooks/test_tasks.py` that the alias forwards.
- [X] T011 Run `make lint-imports`. The moves must add no `ignore_imports` line to `.importlinter`. Run the moved test files and `tests/unit_tests/webhooks/`; every moved test passes unchanged in meaning.

### Attribution without authority (R6)

- [X] T012 [P] Write failing tests in `tests/unit_tests/sessions/test_models.py`: `Run.acting_user_id` returns `user_id` when `acts_as_user` is true, and `None` when it is false, even with a user set.
- [X] T013 [P] Write failing tests in `tests/unit_tests/sessions/test_queue.py`, `tests/unit_tests/sessions/inbox/test_drain.py` and `tests/unit_tests/sessions/test_admission.py`: a run with `acts_as_user=False` and a linked user is enqueued and drained with acting user `None`. Admission resolves its MCP pool and sandbox environment with `user=None`, so no USER-scoped server or environment is chosen.
- [X] T014 Add `acts_as_user = models.BooleanField(default=True)` and the property `acting_user_id` (`user_id if acts_as_user else None`) to `Run` in `daiv/sessions/models.py`, with migration `daiv/sessions/migrations/0045_run_acts_as_user.py`. `create_run` in `daiv/sessions/services.py` accepts `acts_as_user: bool = True`.
- [X] T015 Switch every reader that treats `Run.user_id` as the acting user to `Run.acting_user_id`:
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

- [X] T016 [P] [US1] Failing tests in `tests/unit_tests/jobs/api/test_views.py` for every row of the validation table in contracts/jobs-api.md:
  - `merge_request` with `ref` or a per-repository `prompt` → `422`;
  - with `shared_session: true` or `thread_id` → `400` with "A merge request target can't be combined with shared_session or thread_id.";
  - more than one `repos` entry → `400`;
  - a missing or other-repository merge request → a `failed` entry "Merge request !{iid} was not found in {repo_id}.";
  - a closed or merged one → "Merge request !{iid} is closed." / "Merge request !{iid} is merged.";
  - an archived session, owning or the merge request's own → a `failed` entry "The DAIV session of merge request !{iid} is archived. Unarchive it to run jobs on it: {session URL}", with no comment posted on the merge request;
  - no write access → `404`.
- [X] T017 [P] [US1] Failing tests in `tests/unit_tests/jobs/api/test_views.py` for routing:
  - **Unowned merge request:** the response is `READY`. Exactly one `Run` exists with `trigger_type="api_job"`, `merge_request_iid` set, and session id `compute_thread_id(repo_id, MERGE_REQUEST, iid)`. `address_merge_request_task` is enqueued with `request_key` starting `job:`.
  - **Owned merge request:** a `MERGE_REQUEST_JOB` inbox event is posted to the owning session with its own run, and the response is `QUEUED` while the session is busy. Two jobs sent while it is busy get two different `job_id`s.
  - **Status:** `GET /api/jobs/{id}` returns `merge_request_url` for the merge request.
- [X] T018 [P] [US1] Failing tests in `tests/unit_tests/sessions/test_models.py`:
  - two `READY` `api_job` runs in one session, both with `merge_request_iid`, are allowed;
  - two without `merge_request_iid` still raise `IntegrityError`;
  - `test_active_constraint_literals_match_enums` also asserts the `merge_request_iid__isnull=True` condition;
  - two `QUEUED` `api_job` runs with linked events for one session, branch and person are allowed when both have `runs_alone=True`, and when one of them is that person's ordinary next run, for a linked user and for an external account alike;
  - two ordinary next runs for one session, branch and person still raise `IntegrityError`, and the literals test also asserts the `runs_alone=False` condition of both next-run constraints.
- [X] T019 [P] [US1] Failing tests in `tests/unit_tests/sessions/merge_request_runs/test_service.py`. With a `Request` input on the unowned path, `astart`:
  - records `trigger_type` from `Request.source`;
  - sets `acts_as_user`, `muted` and the requester's `user` and `external_*` on the run;
  - deduplicates on `request_key`;
  - returns `StartOutcome.archived` with the `thread_id` for an archived session, posts nothing and enqueues nothing, while a `Mention` there still gets the archived reply.

  On the owned path it posts `MERGE_REQUEST_JOB` through `MergeRequestJobs.post`, with payload `{prompt, agent_model?, acts_as_user, source}` and source key `job({request_key})`, and returns the event's own run. An archived owner returns `StartOutcome.archived` with nothing posted.
- [X] T020 [P] [US1] Failing tests in `tests/unit_tests/sessions/merge_request_runs/test_addressor.py`. With a `Request` input:
  - the agent input is `HumanMessage(name=requester.username, content=prompt)`;
  - a response becomes one top-level merge request comment;
  - a question becomes a top-level comment, because there is no mention thread, and the job's status is `WAITING_INPUT`;
  - a failure posts the unable note;
  - a merged or closed merge request at task start posts "This merge request was merged before the job could start, so I didn't run it." ("…closed…"), and the run finishes `FAILED`, never started, with that text as its error. No agent runs;
  - an archived session at task start finishes the run `FAILED`, never started, and posts nothing;
  - a `Mention` on a merged merge request still gets today's note and a successful result.
- [X] T021 [P] [US1] Failing tests in `tests/unit_tests/sessions/inbox/test_merge_request_jobs.py`, `test_kinds.py`, `test_splits.py`, `test_render.py`, `test_complete.py` and `test_drain.py` for `MERGE_REQUEST_JOB`:
  - `MergeRequestJobs.post` creates the event and its own `QUEUED` run in one transaction, and refuses under the session lock when the session is archived or the merge request isn't open (`MergeRequestState.is_open` from `MergeRequestOwnership.states_of`, where a draft counts as open);
  - its run has `runs_alone=True`, the requester as its person (`user`, or `external_provider`, `external_uid` and `external_username` for an unlinked CI actor), and `acts_as_user`, `muted`, the prompt and `merge_request_iid`;
  - the event has no subject. Two jobs from one person on one branch both post, get two runs, and both run: neither is superseded;
  - `NextRuns.queued` leaves the job's run out. A mention or chat message from the same person while the job waits gets an ordinary next run, never the job's run, and the take never drops the job's run;
  - it wakes, and the drain takes it alone with the remembered events and promotes its own run to `READY`. A leg follow-up still takes its own run as before;
  - at take time, a merge request whose `MergeRequestOwnership.states_of` state is merged or closed fails the event with `mr_merged` or `mr_closed`, finishes its run `FAILED` and never started, and posts the not-started note once the take commits;
  - in an archived session, a held event fails with `session_archived`, its run finishes `FAILED`, and nothing is posted;
  - its run's trigger type is `payload.source`;
  - it renders the prompt as a request from the requester, with `MENTION_REPLY_NOTE`'s wording;
  - the reply is a new top-level comment;
  - after `MAX_EVENT_ATTEMPTS` it posts the "could not complete" note;
  - `acts_as_user` from the payload reaches the run.
- [X] T022 [P] [US1] Failing tests in `tests/unit_tests/mcp_api/test_server_jobs.py`: the job tool accepts `merge_request` with the same rules and routing as the jobs API.

### Implementation for User Story 1

- [X] T023 [US1] In `daiv/sessions/models.py`, with migration `daiv/sessions/migrations/0046_run_runs_alone_and_constraints.py`:
  - add `merge_request_iid__isnull=True` to the condition of the `run_one_active_per_session` constraint, and update its comment to say merge-request runs share deterministic sessions like webhook runs (two lines at most);
  - add `runs_alone = models.BooleanField(default=False)` to `Run`, and `runs_alone=False` to the conditions of `run_one_next_run_per_user` and `run_one_next_run_per_account`.

  T018 passes.
- [X] T024 [US1] In `daiv/jobs/api/schemas.py`, add `merge_request: int | None = Field(default=None, ge=1)` to `RepoSubmitItem`. A validator refuses `merge_request` together with `ref` or `prompt` (`422`), and a request-level check returns the `400`s of T016.
- [X] T025 [US1] Add the `Request` input (`prompt`, `source: SessionOrigin`, `request_key`) to `daiv/sessions/merge_request_runs/service.py`.
  - **Unowned path:** `astart` passes `trigger_type=source`, `merge_request_iid`, `acts_as_user` and `muted` through `RunStarter`, and enqueues `address_merge_request_task` with `request_key`.
  - **Owned path:** it posts `EventKind.MERGE_REQUEST_JOB` through `MergeRequestJobs.post` (T027) and returns the event's own run.
  - **Archived session**, on either path: return `StartOutcome.archived` with the `thread_id`, and post nothing for a `Request`.

  T019 passes.
- [X] T026 [US1] In `daiv/sessions/merge_request_runs/addressor.py` and `tasks.py`, build the agent input from a `Request` and post its answer or question as a top-level comment. Before the run starts, a `Request` checks the `merged` and `closed` flags of `get_merge_request` and the session's `archived_at`, and stops as research R9 says. T020 passes.
  - **Closed flag:** add `closed: bool = False` to the `MergeRequest` value object in `daiv/codebase/base.py`. `get_merge_request` sets it on both clients as it sets `merged`: GitLab `state == "closed"`, GitHub `state == "closed"` and not merged. Test each client in `tests/unit_tests/codebase/clients/`. T028's submission check reads it too.
- [X] T027 [US1] Add `EventKind.MERGE_REQUEST_JOB` in `daiv/sessions/inbox/kinds.py` (wakes; no subject; trigger type from `payload["source"]`).
  - `daiv/sessions/inbox/merge_request_jobs.py` (new): `MergeRequestJobs.post`, `stop_reason`, `stop`, `stop_held` and `settle_unstartable`, after the same methods of `LegFollowUps` in `leg_follow_ups.py`.
    - `post` creates the run with `runs_alone=True` and the requester's fields from `Requester.run_fields`.
    - `stop` posts the not-started note with `transaction.on_commit` when the merge request was merged or closed.
  - `daiv/sessions/inbox/next_runs.py`: `queued` filters `runs_alone=False`.
  - `daiv/sessions/inbox/splits.py`: `runs_alone` and `takes_context` are true for it.
  - `daiv/sessions/inbox/drain.py`:
    - the take's own-run step, which handles `LEG_FOLLOW_UP` only today, handles both kinds, each through its own class;
    - stop held `MERGE_REQUEST_JOB` events in an archived session alongside `LegFollowUps.stop_held`;
    - read `acts_as_user`.
  - `daiv/sessions/inbox/render.py`: render the prompt with the mention note's wording.
  - `daiv/sessions/inbox/complete.py`: post the reply as a top-level comment when the payload has no `discussion_id`, and the failure note after `MAX_EVENT_ATTEMPTS`.

  T021 passes.
- [X] T028 [US1] In `daiv/sessions/multi_repo.py` and `daiv/sessions/services.py`, route a `repos` entry with `merge_request` to `MergeRequestRuns.astart`. Use `Request(source=API_JOB or MCP_JOB, request_key=f"job:{uuid4().hex}")` and `Requester(user=caller, …)`.
  - **Checks:** first check the merge request with `RepoClient.get_merge_request` (it exists in this repository and is open), and return the `failed` messages of T016 verbatim. "Closed" reads the `closed` flag from T026. Map `StartOutcome.archived` to the archived-session `failed` message, linking `session_detail` for its `thread_id`.
  - **Admission:** keep `aadmit` and its write-access check unchanged.
- [X] T029 [US1] In `daiv/jobs/api/views.py`, map `StartOutcome` to `JobSubmitJobItem` (`READY` or `QUEUED`). T016 and T017 pass.
- [X] T030 [US1] In `daiv/mcp_api/server.py`, accept `merge_request` on the job tool and call the same `sessions` path. T022 passes.

**Checkpoint**: US1 works on its own with an API key (quickstart steps 1–2).

---

## Phase 4: User Story 2 - CI signs in as the pipeline, and the run belongs to whoever triggered it (Priority: P2)

**Goal**: the jobs API accepts a GitLab CI ID token or a GitHub Actions OIDC token. The run is attributed to the pipeline's triggering account and acts as no one (FR-007–FR-013, SR1, SR2).

**Independent Test**: quickstart step 3. A pipeline's token submits a job attributed to the pusher. Wrong-audience, other-project and expired tokens get `401`. Polling with another project's token gets `404`.

### Tests for User Story 2

- [X] T031 [P] [US2] Failing tests in `tests/unit_tests/accounts/api/test_ci_identity.py`, for GitLab and for GitHub, with an in-test RSA key and a stubbed signing-key endpoint:
  - **Accepted:** a valid token yields a `CIIdentity` with `provider`, `repo_id`, `repo_platform_id`, `actor_uid`, `actor_username`, `user`, `sha` (`None` when a GitHub token has no `sha` claim), `jti` and `expires_at`, and `pk == f"ci:{provider}:{actor_uid}"`.
  - **Refused, without saying which check failed:** another `aud`; another `iss`; an expired token; one outside the 60 s leeway; a non-`RS256` algorithm; a bad signature; no `jti`; a `project_id`/`repository_id` that differs from the repository's platform id (a fork).
  - **Refusal logs:** each refusal logs one WARNING naming its check and the claimed repository path, and the token's text appears in no log record.
  - **Not a token:** a non-JWT bearer value returns `None`, so an API key still authenticates through `AuthBearer`.
  - **Unreachable keys:** an unreachable signing-key endpoint refuses and logs at ERROR.
  - **Single use (`CIIdentityTokens`):** the first claim of a `jti` succeeds and a second fails. Two concurrent claims let exactly one through. A released claim can be made again. The entry expires at `exp` plus 60 s. An unreachable cache refuses and logs at ERROR.
  - **Issuers:** `CODEBASE_GITHUB_ACTIONS_OIDC_ISSUER` overrides the GitHub issuer. The GitLab issuer is `CODEBASE_GITLAB_URL`, else `https://gitlab.com`.
- [X] T032 [P] [US2] Failing tests in `tests/unit_tests/jobs/api/test_views.py` for token-authenticated submissions:
  - more than one `repos` entry, or any of `thread_id`, `shared_session` or `references` → `400` with "A CI identity token submits one job on its own repository.";
  - `mcp_overrides` naming a personal server → `400`;
  - `allowed_usernames` set without the actor's login → `401`.
  - **Attribution:**
    - a linked actor gives `Run.user` set and `acts_as_user=False`;
    - an unlinked actor gives `user=None` with `external_provider`, `external_uid` and `external_username` set;
    - an actor linked to an inactive DAIV account is attributed as an unlinked one, external;
    - `muted` defaults to `True`, and an explicit `"muted": false` gives `False`.
  - **Throttle:** keyed by `ci:{provider}:{actor_uid}` (clear the cache first).
  - **Single use:**
    - a second submission with a token whose job was accepted → `401`, and a WARNING names the `reused` check;
    - a token whose submission got a `400` or a `failed` entry can submit again;
    - polling with a used token still answers `200`.
- [X] T033 [P] [US2] Failing tests in `tests/unit_tests/jobs/api/test_views.py` for polling with a token:
  - `200` for a run on the token's repository whose person is the actor, either the linked `user` or a matching `external_provider` and `external_uid`;
  - `200` as well for a job on a merge request a DAIV session owns, whose own run (T021) carries the actor;
  - `404` for any other run.

### Implementation for User Story 2

- [X] T034 [P] [US2] Add the deployment settings:
  - `JOBS_CI_IDENTITY_AUDIENCE: str | None`, default `None` meaning `EXTERNAL_URL`, in `daiv/core/conf.py` (env `DAIV_JOBS_CI_IDENTITY_AUDIENCE`);
  - `GITHUB_ACTIONS_OIDC_ISSUER: str | None`, default `None` meaning `https://HOST/_services/token` when the host of `CODEBASE_GITHUB_URL` is neither `github.com` nor `api.github.com` (GitHub Enterprise Server), else `https://token.actions.githubusercontent.com`, in `daiv/codebase/conf.py` (env `CODEBASE_GITHUB_ACTIONS_OIDC_ISSUER`).
- [X] T035 [US2] Create `daiv/accounts/api/ci_identity.py` with three classes:
  - **`CIIdentityVerifier`:**
    - one `jwt.PyJWKClient` per issuer, cached at module level;
    - signing keys from `{iss}/oauth/discovery/keys` (GitLab) or `{iss}/.well-known/jwks` (GitHub);
    - `algorithms=["RS256"]`, `leeway=60`;
    - requires `iss`, `aud`, `exp`, `iat`;
    - claim mapping per contracts/jobs-api.md;
    - the repository id compared with the platform id from `RepoClient`. Add the id to the `Repository` value object in `daiv/codebase/base.py` and both clients if it is missing, with a test in `tests/unit_tests/codebase/clients/`.
  - **`CIIdentity`:** the in-memory principal of data-model.md.
  - **`CIIdentityBearer(HttpBearer)`:** returns `None` for anything that isn't a three-segment JWT whose header has a `kid`, and raises `401` with `{"detail": "Unauthorized"}` for a token that fails. Each refusal logs once at WARNING with its check and the claimed repository path, never the token. Only an unreachable issuer logs at ERROR.
  - **`CIIdentityTokens`:** `aclaim(identity) -> bool` and `arelease(identity)` over the cache entry of data-model.md (*Used CI tokens*). An unreachable cache counts as a failed claim and logs at ERROR.

  T031 passes.
- [X] T036 [US2] In `daiv/jobs/api/views.py`:
  - set `jobs_router` to `auth=[CIIdentityBearer(), AuthBearer()]`;
  - for a `CIIdentity`, enforce the submission rules of T032 and check `allowed_usernames` against `actor_username` with `RepositoryConfig.is_user_allowed`;
  - in `submit_job`, claim the token with `CIIdentityTokens.aclaim` before starting anything (`401` on a failed claim, with the `reused` WARNING), and release it when the submission accepts no job;
  - default `muted` to `True` unless `"muted"` is in the request's set fields;
  - make `get_job_status` apply the visibility rule of T033 to a `CIIdentity`.
- [X] T037 [US2] In `daiv/sessions/multi_repo.py`, `daiv/sessions/services.py` and `daiv/sessions/admission.py`, accept a requester that is a `CIIdentity`:
  - **Run fields:** `user=identity.user`, `external_provider`, `external_uid`, `external_username` and `acts_as_user=False`, also for merge request targets through `Requester(acts_as_user=False)`.
  - **Access (SR1):** keep the catalog check and skip the `RepositoryAccess` write check, because the verified token is the access proof.
  - **Overrides:** resolve `mcp_overrides` with `user=None`.

  T032 passes.
- [X] T038 [US2] In `daiv/core/api/throttling.py`, make `JobsRateThrottle` key a `CIIdentity` on its `pk`. T032's throttle case and T033 pass.

**Checkpoint**: US1 and US2 work independently. A CI job can review a merge request with no stored DAIV secret (quickstart step 3).

---

## Phase 5: Polish & Cross-Cutting Concerns

**Purpose**: documentation (Principle VII), the changelog, evals (Principle V) and the gates.

- [X] T039 [P] Update `docs/features/jobs-api.md`:
  - `repos[].merge_request` in **Request fields**, with its validation and where the run goes. On a merge request a DAIV session owns, each job gets its own `job_id` and runs as one turn of that session;
  - what a merge request job can end with, from contracts/jobs-api.md:
    - the result comment;
    - a question, which leaves the job `WAITING_INPUT` until someone mentions DAIV on the merge request;
    - the failure comment;
    - the not-started note when the merge request merges or closes before the run starts;
    - the `failed` entry for an archived session, with nothing posted on the merge request;
  - a "CI identity tokens" section with the GitLab `id_tokens` and GitHub Actions examples, the checks, attribution, mute default (SR2) and polling rule from contracts/jobs-api.md;
  - one accepted job per token: a second submission with the same token gets `401`, so a CI job that sends two jobs requests two tokens (two `id_tokens` entries on GitLab, two token requests on GitHub). A refused submission doesn't use the token up, and polling with a used token still works;
  - replace the comment-posting workaround with the code-review-on-every-push example (FR-028).
- [X] T040 [P] Update `docs/features/pull-request-assistant.md`: under **Review reports**, link to the jobs API merge request target as the way to review every push.
- [X] T041 [P] Update `docs/reference/env-variables.md` with `DAIV_JOBS_CI_IDENTITY_AUDIENCE` and `CODEBASE_GITHUB_ACTIONS_OIDC_ISSUER`, so `test_env_variables_doc.py` passes.
- [X] T042 [P] Add two `Added` lines under **Unreleased** in `CHANGELOG.md`, in Common Changelog form, each linking the pull request:
  - the jobs API merge request target;
  - CI identity tokens.
- [X] T043 Run `make lint-fix` scoped to the changed paths (repo-wide unsafe fixes can strip untracked files), then `make lint`. Run `rm -rf .ruff_cache` before trusting a clean ruff result.
- [X] T044 Run `make lint-typing`. Fix the new modules' diagnostics, and add no file to `[[tool.ty.overrides]]`.
- [X] T045 Run `make test`. Every task's tests pass, and failures are compared with T003's baseline.
- [X] T046 Run `uv run --only-group=docs mkdocs build --strict`, `tests/unit_tests/test_docs_style.py` and `tests/unit_tests/test_docs_links.py`.
- [ ] T047 Run quickstart.md steps 1–3 against the local stack (`make up`), and record what was run and what was not in the pull request.
- [ ] T048 Run `make eval-prompts SUITES="inbox code_review" OUT=eval-runs/005-after.jsonl`, then `uv run evals/compare_runs.py eval-runs/005-before.jsonl eval-runs/005-after.jsonl`. Confirm any `unconfirmed` verdict with its printed re-run, and put the table in the pull request (Principle V).

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)**: T003–T005 first. T004 must run before any code change.
- **Foundational (Phase 2)**: depends on Setup and blocks both stories. Within it, T006 → T007 → T008 → T009 → T010 → T011 run in order, because they move code between the same modules. T012–T015 can run alongside them, since they touch other files.
- **US1 (Phase 3)**: depends on Phase 2. T027 comes before T025, which posts through `MergeRequestJobs`.
- **US2 (Phase 4)**: depends on Phase 2. Its merge-request-targeted case reuses US1's `Request` path (T025), but a branch-target token job works without US1.
- **Polish (Phase 5)**: T039 and T040 need US1 and US2. T041 needs T034.

### Within each story

Tests come first and must fail. Then models and migrations, then services, then endpoints.

### Parallel opportunities

- **Phase 2:** T012 and T013 alongside the moves.
- **US1:** T016–T022, all tests in different files.
- **US2:** T031–T033, and T034 alongside the tests.
- **Polish:** T039–T042, all different files.

---

## Parallel Example: User Story 1

```text
# Tests, together:
T016, T017 tests/unit_tests/jobs/api/test_views.py      (one file: write them in one pass)
T018 tests/unit_tests/sessions/test_models.py
T019 tests/unit_tests/sessions/merge_request_runs/test_service.py
T020 tests/unit_tests/sessions/merge_request_runs/test_addressor.py
T021 tests/unit_tests/sessions/inbox/test_{merge_request_jobs,kinds,splits,render,complete,drain}.py
T022 tests/unit_tests/mcp_api/test_server_jobs.py
```

---

## Implementation Strategy

### MVP first (US1)

1. Phase 1, then Phase 2. Mentions keep working from their new home, and `make test` is green.
2. Phase 3 (US1). Stop and validate with quickstart steps 1–2. CI can then review every push with an API key, with results on the merge request, which already replaces both workarounds.

### Incremental delivery

1. **US2:** keyless CI and per-person spend. Validate with quickstart step 3.
2. **Polish:** docs, changelog, gates and evals. Each story can ship in its own pull request, provided its docs and changelog line land with it (Principle VII).
