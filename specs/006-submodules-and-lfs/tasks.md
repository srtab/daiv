---

description: "Task list for submodule and Git LFS checkout"
---

# Tasks: Submodules and Git LFS

**Input**: Design documents from `specs/006-submodules-and-lfs/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md),
[contracts/](contracts/), [quickstart.md](quickstart.md).

**Tests**: Included. Constitution principle IV and `patterns/tests.md` require a test that fails without each
behaviour change.

- **Stubs:** tests stub every git-platform client and the sandbox, and make no network call.
- **Real git:** git runs for real on temporary local repositories, as `test_git_manager.py` does. A stub client
  resolves platform paths to local bare repositories, and `RepoCheckout` is built with `allowed_protocol="file"` in
  tests (research R12).
- **LFS:** `git lfs pull` and `git lfs smudge` are stubbed at `RepoCheckout`'s command boundary. Stat and index
  behaviour runs on real git, with a test-only clean filter standing in for git-lfs.
- **Constants:** a test that shortens GitLab's propagation retry patches the module constant
  `codebase.clients.gitlab.client.CLONE_TOKEN_PROPAGATION_RETRY_BACKOFF_SECONDS`, never `time`.

Principle V: the prompt section and the notice render only for opted-in repositories, which no eval case has. No
eval runs, and the pull request declares the change unmeasured (T057).

**Organization**: tasks are grouped by user story. US1 (submodules) is the MVP. US2 (LFS) is independent of US1 once
Phase 2 is done. US3 (failures degrade the run) builds on both.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: the user story the task belongs to (US1, US2, US3)

## Path Conventions

- Paths are relative to the repository root: `daiv/`, `tests/unit_tests/`, `docs/`, `docker/`.
- Tests mirror `daiv/`, one file per module, and import without the `daiv.` prefix.
- Names, texts, defaults and limits quoted below come from the contracts and the data model. Use them verbatim.

---

## Phase 1: Setup

**Purpose**: approval, the refined spec and a clean baseline.

- [X] T001 Record the maintainer's approval of plan.md and refinement SR1 in the **Status** line of specs/006-submodules-and-lfs/plan.md (done 2026-11-27).
- [X] T002 Apply SR1 from specs/006-submodules-and-lfs/research.md (R4) to FR-005 of specs/006-submodules-and-lfs/spec.md, and record it under **Clarifications** (done 2026-11-27).
- [ ] T003 Run `make test` once before any change and note pre-existing failures, so later ones can be told apart.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: the settings, the value types, the credential hook, the access predicate and the plumbing every story
writes into:

- the report on `RepoHandle`;
- `PublishScope` and `KeptOut` through `GitManager` and the publisher;
- the prompt section and the turn-end notice.

With both settings off, every command, prompt and reply stays as it is today (FR-016).

**⚠️ CRITICAL**: no user story work can begin until this phase is complete.

### Tests for Phase 2

- [ ] T004 [P] In tests/unit_tests/codebase/test_repo_config.py, assert that `RepositoryConfig().checkout` has `submodules is False` and `lfs is False`, and that YAML `checkout: {submodules: true, lfs: true}` parses to both `True`.
- [ ] T005 [P] In tests/unit_tests/codebase/clients/test_base.py:
  - `RepoClient.with_git_auth(repository, operation)` calls `operation` once with `get_git_auth_env(repository)`;
  - `complete_checkout(repo, repository, CheckoutOptions.off(), DENY_ALL)` returns a report whose `is_empty` is true and runs no git command (spy on `RepoCheckout`'s command runner).
- [ ] T006 [P] In tests/unit_tests/codebase/clients/gitlab/test_client.py, test `GitLabClient.with_git_auth`. With `CLONE_TOKEN_PROPAGATION_RETRY_BACKOFF_SECONDS` patched to zeros:
  - an auth error is retried with the same ephemeral token through the backoff;
  - once the retries are spent, the cached token is invalidated (`invalidate_clone_token`) and one fresh token is tried;
  - a PAT token is never retried;
  - a non-auth error propagates at once.

  The existing `load_repo` tests keep passing unchanged, proving `_clone` now goes through `with_git_auth`.
- [ ] T007 [P] In tests/unit_tests/codebase/clients/github/test_client.py, assert that `GitHubClient.with_git_auth` mints a repository-scoped installation token (`_mint_installation_token`) on each call and passes its `GitAuthEnv` to the operation.
- [ ] T008 [P] In tests/unit_tests/codebase/test_authorization.py, test `platform_account_can_view(uid, repo_id)`:
  - true with a fresh `RepositoryAccess` row for the configured provider, uid and repository;
  - false with no row, with a row past the hard TTL, or with a row for another provider.
- [ ] T009 [P] In tests/unit_tests/automation/agent/test_context.py, test `set_runtime_ctx`:
  - it calls `repo_client.complete_checkout` once per repository, main and extra, after the clones;
  - each call gets `CheckoutOptions` built from that repository's own `RepositoryConfig.checkout` and `CODEBASE_LFS_MAX_CLONE_BYTES`, and the `read_access` passed to `set_runtime_ctx` (`DENY_ALL` when omitted);
  - each call runs in a worker thread, not on the event loop (assert `threading.current_thread()` is not the loop's);
  - `RepoHandle.checkout` holds the returned report.
- [ ] T010 [P] In tests/unit_tests/sessions/executor/test_run.py, assert the predicate the executor passes as `read_access`, by the run's person:
  - a `Run` whose `user` is set checks `codebase.authorization.can_view(user, repo_id)`;
  - a `Run` with only `external_uid` checks `platform_account_can_view(external_uid, repo_id)`;
  - a `Run` with neither, or a spec with no `run_id`, denies every repository;
  - a run with `acts_as_user=False` still checks its attributed person.

  Use `django_db(transaction=True)`.
- [ ] T011 [P] In tests/unit_tests/automation/agent/middlewares/test_git.py:
  - with every handle's report empty, the rendered git system prompt equals today's byte for byte;
  - with a report holding checked-out and skipped submodules and skipped LFS files, the section from contracts/agent-context.md §1 renders once per such repository, in repository order;
  - lists stop at 20 entries plus "and N more";
  - each `SkipReason` renders its phrase verbatim;
  - a URL's user info never appears;
  - the "not published" bullet renders whenever either setting is on;
  - a `KeptOut` on the main outcome or on an extra repository's `RepoPublish` adds the notice of §2 as a visible assistant message beside any conflicts notice, naming the repository when the session has extra repositories. An empty `KeptOut` adds nothing.
- [ ] T012 [P] In tests/unit_tests/automation/agent/workspace/test_git_manager.py, assert that `status_snapshot(..., scope=PublishScope())` and `commit_all(message)` send exactly today's git argument lists (record the runner's calls), and that `RepoStatus.kept_out` is empty.

### Implementation for Phase 2

- [ ] T013 [P] In daiv/codebase/repo_config.py, add `class Checkout(BaseModel)` with `submodules: bool = Field(default=False, description=...)` and `lfs: bool = Field(default=False, description=...)`, and `RepositoryConfig.checkout: Checkout = Field(default_factory=Checkout, description=...)`, after `memory`.
- [ ] T014 [P] In daiv/codebase/conf.py, add `LFS_MAX_CLONE_BYTES: int = Field(default=268435456, ge=0, description=...)`. The environment variable is `CODEBASE_LFS_MAX_CLONE_BYTES`, and `0` downloads no LFS content.
- [ ] T015 [P] Create daiv/codebase/clients/checkout.py with the types of data-model.md:
  - `CheckoutOptions` (frozen): `submodules`, `lfs`, `lfs_max_bytes`, with classmethods `off()` and `from_config(checkout, lfs_max_bytes)`;
  - `RepoReadAccess` (Protocol, `__call__(repo_id: str) -> bool`) and `DENY_ALL`;
  - `SkipReason` (StrEnum): `outside_platform`, `daiv_no_access`, `person_no_access`, `commit_missing`, `over_limit`, `fetch_failed`;
  - `SubmoduleCheckout` (frozen): `path`, `url`, `repo_id: str | None`, `commit`, `skipped: SkipReason | None`;
  - `LfsFile` (frozen): `path`, `size`, `skipped: SkipReason | None`;
  - `CheckoutReport` (frozen): `options`, `submodules`, `lfs_files`, with `skipped_submodules`, `skipped_lfs_files`, `is_empty` and `EMPTY`;
  - class `RepoCheckout(client, *, options, read_access, allowed_protocol="https")`, whose `complete(repo, repository) -> CheckoutReport` returns the empty report for `options.off()`. Its command runner runs git subprocesses with the environment it is given, as `_run_git` does in automation/agent/workspace/git_runners.py; tests replace it.
- [ ] T016 In daiv/codebase/clients/base.py, add two concrete `RepoClient` methods:
  - `with_git_auth(repository, operation: Callable[[GitAuthEnv | None], T]) -> T`, which calls `operation(self.get_git_auth_env(repository))` once;
  - `complete_checkout(repo, repository, options, read_access) -> CheckoutReport`, which returns `RepoCheckout(self, options=options, read_access=read_access).complete(repo, repository)`.
- [ ] T017 In daiv/codebase/clients/gitlab/client.py, override `with_git_auth`. Move into it the propagation retry (`_clone_awaiting_token_propagation`) and the stale-token self-heal from `_clone`, generalised over `operation`. The PAT is still never retried. Rewrite `_clone` to call `self.with_git_auth(repository, clone_with)`, where `clone_with` removes a partial clone directory before each attempt, as today.
- [ ] T018 [P] In daiv/codebase/clients/github/client.py, override `with_git_auth` to mint `_mint_installation_token(repository)` per call and pass `GitAuthEnv.for_token(repository.clone_url, token)`. Make `load_repo` clone through it.
- [ ] T019 [P] In daiv/codebase/authorization.py, add `platform_account_can_view(uid: str, repo_id: str) -> bool`. It reads `_fresh_rows(uid).filter(repo_id=repo_id).exists()`, after `_maybe_enqueue_backstop()`, as `_resolve_uid` does.
- [ ] T020 In daiv/automation/agent/context.py:
  - add `RepoHandle.checkout: CheckoutReport` (default `CheckoutReport.EMPTY`);
  - add `read_access: RepoReadAccess = DENY_ALL` to `set_runtime_ctx`;
  - after the clones, complete every handle with `asyncio.gather(*(asyncio.to_thread(repo_client.complete_checkout, h.gitrepo, h.repository, CheckoutOptions.from_config(h.config.checkout, codebase_settings.LFS_MAX_CLONE_BYTES), read_access) for h in handles))`, and rebuild each handle with `dataclasses.replace(handle, checkout=report)` before building `RuntimeCtx`.
- [ ] T021 In daiv/sessions/executor/run.py, build the `read_access` predicate from the run's person and pass it to `set_runtime_ctx`:
  - load `user` (with `is_admin`), `external_provider` and `external_uid` from `Run` by `spec.run_id`;
  - a user → `can_view(user, repo_id)`; only an external uid → `platform_account_can_view(uid, repo_id)`; neither, or no `run_id` → `DENY_ALL`.

  Keep it a small class (`RunReadAccess`) with `__call__`, per code-organization rule 1. It runs in the checkout's worker thread, so its ORM calls are synchronous.
- [ ] T022 In daiv/automation/agent/workspace/git_manager.py:
  - add `PublishScope` (frozen; `submodules: bool = False`, `lfs: bool = False`, `from_config(checkout)`);
  - add `KeptOut` (`lfs_paths`, `submodules`, falsy when empty) and `KeptOutSubmodule` (`path`, `commit_moved`, `changed_files` capped at 20, `more`);
  - add `RepoStatus.kept_out`;
  - add `scope: PublishScope = PublishScope()` to `status_snapshot`, and `keep_out: Sequence[str] = ()` to `commit_all`. With the defaults, both send exactly today's commands.
- [ ] T023 Thread the scope and the result through the publisher:
  - daiv/automation/agent/publishers.py: `GitChangePublisher.publish` passes `PublishScope.from_config(self.ctx.config.checkout)` to `status_snapshot`, and the snapshot's kept-out paths (gitlinks and LFS paths) to `commit_all`; `PublishOutcome` gains `kept_out: KeptOut | None`, which `_apublish_extra_repo` copies onto `RepoPublish`;
  - daiv/automation/agent/git_state.py: `RepoPublish.kept_out`, round-tripped through `to_state` and `from_state`.
- [ ] T024 In daiv/automation/agent/middlewares/git.py:
  - render the per-repository section of contracts/agent-context.md §1 from `handle.checkout` in `GIT_SYSTEM_PROMPT` (mustache data built in `awrap_model_call`; nothing renders for an empty report);
  - add `_kept_out_notice(kept_out, where)`, rendering §2, to the turn-end messages beside `_conflicts_notice`, for the main outcome and for each extra repository.

**Checkpoint**: settings parse, and every run completes an empty checkout. Prompts, commands and replies are unchanged
for repositories that leave both settings off.

---

## Phase 3: User Story 1 - The agent works with the code in a repository's submodules (Priority: P1) 🎯 MVP

**Goal**: with `checkout.submodules: true`, every reachable submodule the person can read is checked out, recursively
and on both platforms, each with its own credential. Changes inside submodules are never published, and are named in
the reply.

**Independent Test**: spec US1, and quickstart scenarios 1–7 for submodules.

### Tests for User Story 1

- [ ] T025 [P] [US1] In tests/unit_tests/codebase/clients/test_checkout.py, test URL resolution (research R2), using a stub client whose clone URL is `https://git.example.com/group/app.git`:
  - `../lib.git` → `group/lib`;
  - `https://git.example.com/group/lib.git` → `group/lib`;
  - `git@git.example.com:group/lib.git` → `group/lib`;
  - `ssh://git@git.example.com:2222/group/lib.git` → `group/lib`;
  - a nested `../inner.git` resolves against its parent's resolved URL;
  - `https://other.example.com/x.git`, `file:///tmp/x`, `/tmp/x`, `ext::sh -c x` and `git://git.example.com/x.git` give `outside_platform`, with no git command run for them;
  - `submodule.<name>.url` in the parent's `.git/config` is rewritten to `https://git.example.com/group/lib.git`;
  - the report's `url` has user info stripped.
- [ ] T026 [US1] In tests/unit_tests/codebase/clients/test_checkout.py, with real git, `allowed_protocol="file"` and a stub client mapping `group/lib` and `group/inner` to local bare repositories, test a superproject with `libs/lib` (holding a nested `vendor/inner`):
  - both are checked out at the recorded commits, not at the branch tips;
  - the report lists `libs/lib` and `libs/lib/vendor/inner` in that order;
  - `git status --porcelain` in the superproject is empty.
- [ ] T027 [US1] In tests/unit_tests/codebase/clients/test_checkout.py, test access:
  - `get_repository` raising → `daiv_no_access`, the directory stays empty and `with_git_auth` is never called;
  - `read_access` returning false → `person_no_access`, with the same effects, and its nested submodules are absent from the report;
  - `DENY_ALL` checks out nothing.
- [ ] T028 [US1] In tests/unit_tests/codebase/clients/test_checkout.py, test credentials:
  - each submodule's commands run inside `client.with_git_auth(<that submodule's Repository>, ...)`;
  - their environment holds that `GitAuthEnv`'s header and not the main repository's;
  - `GIT_ALLOW_PROTOCOL` equals the allowed protocol;
  - the update runs with `core.hooksPath=/dev/null` and `--checkout`, so a `.gitmodules` `update = !cmd` never runs (assert a marker file is not created).
- [ ] T029 [P] [US1] In tests/unit_tests/automation/agent/workspace/test_git_manager.py, with real git and a superproject holding a checked-out submodule and `PublishScope(submodules=True)`:
  - an edit inside the submodule only → `status_snapshot` reports `dirty` false, an empty diff, and `kept_out.submodules[0].changed_files == ("f",)`;
  - an edit in the superproject and inside the submodule → `commit_all` commits only the superproject file;
  - the submodule's HEAD moved → the gitlink is not in the commit, and `commit_moved` is true;
  - more than 20 changed files → 20 names and `more`.
- [ ] T030 [P] [US1] In tests/unit_tests/automation/agent/test_publishers.py, test the publish:
  - a turn whose only change is inside a submodule publishes nothing, raises nothing, and returns `kept_out` (FR-013);
  - with a local bare "origin" whose branch moved the gitlink during the run, a publish that integrates on reject pushes a branch whose gitlink is the remote's (FR-014).
- [ ] T031 [P] [US1] In tests/unit_tests/automation/agent/workspace/test_session.py, assert that `_make_repo_archive` keeps `.git/modules/**` and the submodule's `.git` file, and that `git status --porcelain` is empty in a copy extracted with `tarfile`.

### Implementation for User Story 1

- [ ] T032 [US1] In daiv/codebase/clients/checkout.py, implement the reading of `.gitmodules` (`git config -f .gitmodules -z --get-regexp '^submodule\..*\.(path|url)$'`), the URL resolution and the host and protocol policy of research R2, and the rewrite of `submodule.<name>.url`.
- [ ] T033 [US1] In daiv/codebase/clients/checkout.py, implement each submodule's checkout (research R3):
  - `client.get_repository(repo_id)` (`daiv_no_access` when it raises), then `read_access(repo_id)` (`person_no_access`);
  - inside `client.with_git_auth(sub_repository, ...)`: `git submodule init -- <path>`, the URL rewrite, then `git -c core.hooksPath=/dev/null submodule update --checkout -- <path>`, with an environment of only `GitAuthEnv.as_env()`, `GIT_NO_PROMPT_ENV`, `GIT_ALLOW_PROTOCOL` and the variables `_INHERITED_ENV` lists;
  - recursion into the submodule's own `.gitmodules`.
- [ ] T034 [US1] In daiv/automation/agent/workspace/git_manager.py, with `scope.submodules`, implement research R8 for submodules:
  - add `--ignore-submodules=all` to the snapshot's `status` and `diff` calls;
  - add `status --porcelain=v2 -z` to batch A, and read its submodule entries;
  - add one `-C <path> status --porcelain -z` per changed submodule, in the same round trip as batch B;
  - fill `RepoStatus.kept_out`;
  - in `commit_all`, after `add -A`, run `git --literal-pathspecs reset -q -- <paths>` when `keep_out` is non-empty.

**Checkpoint**: US1 is shippable alone. Quickstart scenarios 1 (submodule parts), 2–7 and 9 pass.

---

## Phase 4: User Story 2 - The agent reads the real content of LFS-tracked files (Priority: P2)

**Goal**: with `checkout.lfs: true`, LFS files hold their content, up to the cap. git status stays clean in sandboxes
without git-lfs, and the sandbox copy carries the content once. LFS paths are never published.

**Independent Test**: spec US2, and quickstart scenarios 1–3 and 8 for LFS.

### Tests for User Story 2

- [ ] T035 [P] [US2] In tests/unit_tests/codebase/clients/test_checkout.py, test LFS listing and selection (research R5), with real git and pointers committed by hand:
  - LFS paths come from `check-attr filter`; an LFS-attributed blob that is not a pointer is skipped;
  - sizes come from the pointers;
  - selection is in path order, superproject first, under `lfs_max_bytes`;
  - a file larger than what is left is `over_limit`, while a later smaller file still fits;
  - `lfs_max_bytes=0` marks every file `over_limit`;
  - the stubbed `git lfs pull` gets `--include` batches with `*?[]\` escaped, each under 64 KiB of argv;
  - a path with a comma goes to `git lfs smudge` alone;
  - every LFS command's environment holds `lfs.url=<clone_url>/info/lfs` and the `filter.lfs.*` keys, and a `.lfsconfig` `lfs.url` in the repository never reaches the stub.
- [ ] T036 [US2] In tests/unit_tests/codebase/clients/test_checkout.py, test the status after the copy (research R6), with real git and a test-only clean filter standing in for git-lfs during the worker refresh:
  - after `complete`, `core.checkStat` is `minimal` and `core.trustctime` is `false` in the superproject's and the submodule's git config;
  - every fetched file's mtime is at least 2 s in the past;
  - after `_make_repo_archive` and `tarfile` extraction, `git status --porcelain` run with `GIT_CONFIG_NOSYSTEM=1` and an empty `HOME` is empty;
  - an edited LFS file shows ` M`, and a new `*.bin` file shows `??`.
- [ ] T037 [P] [US2] In tests/unit_tests/codebase/clients/gitlab/test_client.py and tests/unit_tests/codebase/clients/github/test_client.py, assert that the clone's environment holds `GIT_LFS_SKIP_SMUDGE=1` beside the credential keys.
- [ ] T038 [P] [US2] In tests/unit_tests/automation/agent/workspace/test_session.py:
  - `_make_repo_archive` and `_make_repos_archive` leave out `.git/lfs/**` and `.git/modules/<name>/lfs/**`, and keep every other path;
  - without an `lfs` directory, the archive's member list is unchanged.
- [ ] T039 [P] [US2] In tests/unit_tests/automation/agent/workspace/test_git_manager.py, with real git and `PublishScope(lfs=True)`:
  - an edited, a new, a deleted and a renamed LFS-tracked path are all left out of the commit and listed in `kept_out.lfs_paths`;
  - a non-LFS edit beside them is committed;
  - a turn whose only changes are LFS paths reports `dirty` false;
  - the snapshot's `status`, `diff` and `ls-files --others` calls carry `:(exclude,attr:filter=lfs)`.

### Implementation for User Story 2

- [ ] T040 [P] [US2] Add `git-lfs` to the runtime `apt-get install` list of docker/production/app/Dockerfile and to that of docker/local/app/Dockerfile. Don't run `git lfs install`: DAIV passes the filter config per command (research R11).
- [ ] T041 [P] [US2] Add `GIT_LFS_SKIP_SMUDGE=1` to the clone environment in daiv/codebase/clients/gitlab/client.py (`clone_with` in `_clone`) and daiv/codebase/clients/github/client.py (`load_repo`).
- [ ] T042 [US2] In daiv/codebase/clients/checkout.py, implement research R5:
  - list pointers per repository;
  - select across the superproject and the fetched submodules under `options.lfs_max_bytes`;
  - fetch each repository's selection inside `client.with_git_auth(<that repository>, ...)`, with `git lfs pull --include` batches and the comma fallback;
  - fill `CheckoutReport.lfs_files`.
- [ ] T043 [US2] In daiv/codebase/clients/checkout.py, implement research R6 for every repository with fetched content: set `core.checkStat=minimal` and `core.trustctime=false` in that repository's config, set the mtimes of the fetched files to now − 2 s, and run `git update-index -q --refresh` with the `filter.lfs.*` keys in the environment.
- [ ] T044 [P] [US2] In daiv/automation/agent/workspace/session.py, add a tar filter to `_make_repo_archive` and `_make_repos_archive` that drops any directory named `lfs` directly inside a git directory (`.git/lfs`, `.git/modules/**/lfs`) (research R7).
- [ ] T045 [US2] In daiv/automation/agent/workspace/git_manager.py, with `scope.lfs`, implement research R8 for LFS:
  - add the pathspec `.` `:(exclude,attr:filter=lfs)` to the snapshot's `status`, `diff` and `ls-files --others` calls;
  - add `status --porcelain -z -- :(attr:filter=lfs)` to batch A and parse it into `kept_out.lfs_paths`, keeping both paths of a rename;
  - pass those paths to `commit_all`'s reset.

**Checkpoint**: US1 and US2 both work. Quickstart scenarios 1–9 pass on a sandbox run and on a disk run.

---

## Phase 5: User Story 3 - What cannot be fetched is reported, and the run goes on (Priority: P3)

**Goal**: any submodule or LFS failure becomes a reason in the report and never fails the run. DAIV-side faults log
at ERROR, without secrets.

**Independent Test**: spec US3, and quickstart scenario 10.

### Tests for User Story 3

- [ ] T046 [P] [US3] In tests/unit_tests/codebase/clients/test_checkout.py, test failures:
  - a submodule whose recorded commit is absent from its repository → `commit_missing`, and the other submodules are still checked out;
  - a submodule whose fetch fails with an auth error after `with_git_auth`'s retries, or with a network error → `fetch_failed`;
  - an unexpected exception inside one submodule's checkout → `fetch_failed` for it, a `logger.exception`, and the rest continue;
  - a failed `git lfs pull` batch → `fetch_failed` for its files;
  - git-lfs missing on the worker (`FileNotFoundError` from the runner) → every selected file `fetch_failed`, and one ERROR naming the missing `git-lfs` program;
  - `complete` never raises.
- [ ] T047 [US3] In tests/unit_tests/codebase/clients/test_checkout.py, use `caplog` to check levels: `daiv_no_access` and `fetch_failed` log at ERROR, naming the repository and the submodule's path. `outside_platform`, `person_no_access`, `commit_missing` and `over_limit` log at INFO. The stub token's text appears in no record.
- [ ] T048 [P] [US3] In tests/unit_tests/automation/agent/test_context.py, assert that a `complete_checkout` that raises anyway is logged with `logger.exception`, and `set_runtime_ctx` still yields with `CheckoutReport.EMPTY` on that handle (FR-008).

### Implementation for User Story 3

- [ ] T049 [US3] In daiv/codebase/clients/checkout.py, classify failures and log them as research R10 says:
  - commit missing: git's "did not contain" or "not our ref" texts, through a new `codebase.utils.is_git_object_missing_text`;
  - auth: `codebase.utils.is_git_auth_error_text`;
  - any other failure is a fetch failure.

  Wrap each submodule and each LFS batch so one failure never stops the rest, and make `complete` catch anything left with `logger.exception`. Log messages carry no environment and no URL user info.
- [ ] T050 [US3] In daiv/automation/agent/context.py, wrap the per-handle completion of T020 so an exception from `complete_checkout` is logged with `logger.exception` and leaves `CheckoutReport.EMPTY` on that handle. Note in a one-line comment why continuing is safe: the clone itself is intact (errors rule 2).

**Checkpoint**: all three stories work. Quickstart scenario 10 passes.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [ ] T051 [P] Document the feature in docs/customization/repository-config.md:
  - a `checkout` row group (keys, types, defaults and behaviour from contracts/repository-config.md), and a `checkout` block in the full example;
  - a short section on what DAIV fetches and with whose access, the member-only rule (SR1), what it does not publish, and how to change a submodule, linking to [Start a chat over several repositories](../features/sessions.md#start-a-chat-over-several-repositories).

  `test_docs_reference.py` must pass.
- [ ] T052 [P] Add `CODEBASE_LFS_MAX_CLONE_BYTES` with its default and meaning to docs/reference/env-variables.md; `test_env_variables_doc.py` must pass.
- [ ] T053 [P] In docs/getting-started/deployment.md, state that DAIV's image ships `git-lfs`, and that a custom image needs the `git-lfs` package for `checkout.lfs`.
- [ ] T054 Add one line under **Unreleased** → **Added** in CHANGELOG.md, linking the implementation pull request, e.g. "Check out a repository's submodules and Git LFS content when its `.daiv.yml` turns them on ([#NNNN](…))".
- [ ] T055 Run `make lint-fix`, `make lint`, `make lint-typing`, `make test` and `uv run --only-group=docs mkdocs build --clean --strict`, and fix what they report. Record each command and its result for the pull request (tests rule 5).
- [ ] T056 Run the quickstart scenarios of specs/006-submodules-and-lfs/quickstart.md on the local stack, both platforms where available. Report any scenario not run as unverified.
- [ ] T057 Write the implementation pull request's body:
  - the constitution check evidence of plan.md;
  - the commands and results of T055 and T056;
  - Principle V's statement that the prompt and notice changes are unmeasured, because no eval case opts in, so no claim about agent quality is made (agent-evals rule 2).

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)**: none; T001 and T002 are done.
- **Foundational (Phase 2)**: depends on Setup and blocks every story.
- **US1 (Phase 3)** and **US2 (Phase 4)**: each depends only on Phase 2. Both edit daiv/codebase/clients/checkout.py and daiv/automation/agent/workspace/git_manager.py, so they run one after the other, US1 first.
- **US3 (Phase 5)**: depends on US1 and US2, whose checkout paths it hardens.
- **Polish (Phase 6)**: depends on the stories it documents; T051–T053 can start once Phase 2 fixes the settings' names.

### Within each phase

- Tests first; they must fail before the implementation tasks start.
- T015 before T016; T016 before T017 and T018; T013–T016 before T020; T019 before T021; T022 before T023 before T024.
- In US1: T032 before T033. In US2: T042 before T043.

### Parallel opportunities

- Phase 2 tests T004–T012 are all [P].
- Phase 2 implementation: T013, T014, T015, T018 and T019 are [P].
- Within US1, T025, T029, T030 and T031 are [P] (one per test file); T026–T028 share T025's file. Within US2, T035 and
  T037–T039 are [P] (T036 shares T035's file), and so are implementation tasks T040, T041 and T044. In US3, T046 and
  T048 are [P].
- Docs T051–T053 are [P].

---

## Parallel Example: User Story 1

```bash
# The US1 tests, one per file, together; T026–T028 follow T025 in test_checkout.py:
T025 test_checkout.py — URL resolution (then T026 checkout, T027 access, T028 credentials)
T029 test_git_manager.py — submodule scope
T030 test_publishers.py — only-submodule publish, FR-014
T031 test_session.py — archive keeps .git/modules
```

---

## Implementation Strategy

### MVP first (US1)

1. Phase 1, then Phase 2: nothing changes for any repository yet.
2. Phase 3: submodules for repositories that opt in.
3. Stop and validate with quickstart scenarios 1–7 and 9 before starting US2.

### Incremental delivery

1. Foundation + US1 → submodules, safe publishing, the agent told.
2. + US2 → LFS content, clean sandboxes, LFS kept out of publishes.
3. + US3 → failures degrade, and operator logs.
4. Polish → docs, changelog, gates, quickstart, PR statement.

One pull request carries all of it, as the spec describes one feature. The phases are the order of work and review.
