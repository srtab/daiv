---

description: "Task list for submodule and Git LFS checkout, and publishing submodule changes"
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
  tests (research R12). Submodule publish tests push to local bare remotes, one per repository.
- **LFS:** `git lfs pull` and `git lfs smudge` are stubbed at `RepoCheckout`'s command boundary. Stat and index
  behaviour runs on real git, with a test-only clean filter standing in for git-lfs.
- **Constants:** a test that shortens GitLab's propagation retry patches the module constant
  `codebase.clients.gitlab.client.CLONE_TOKEN_PROPAGATION_RETRY_BACKOFF_SECONDS`, never `time`.
- **Database:** tests that touch models or the drain use `django_db(transaction=True)` where the code under test opens
  its own transactions, as the existing inbox and executor tests do.

Principle V: the prompt section, the notice and the `submodule_merged` prompt render only for opted-in repositories
or sessions that published a submodule, which no eval case has. No eval runs, and the pull request declares the
change unmeasured (T097).

**Organization**: tasks are grouped by user story.

- **US1** (read submodules) is the MVP.
- **US2** (publish submodule changes) builds on US1.
- **US3** (LFS) depends only on Phase 2.
- **US4** (failures degrade the run) builds on the other three.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: the user story the task belongs to (US1, US2, US3, US4)

## Path Conventions

- Paths are relative to the repository root: `daiv/`, `tests/unit_tests/`, `docs/`, `docker/`.
- Tests mirror `daiv/`, one file per module, and import without the `daiv.` prefix.
- Names, texts, defaults and limits quoted below come from the contracts and the data model. Use them verbatim.

---

## Phase 1: Setup

**Purpose**: approval, the refined spec and a clean baseline.

- [X] T001 Record the maintainer's approval of plan.md and refinement SR1 in the **Status** line of specs/006-submodules-and-lfs/plan.md (done 2026-11-27, for the read-only plan).
- [X] T002 Apply SR1 from specs/006-submodules-and-lfs/research.md (R4) to FR-005 of specs/006-submodules-and-lfs/spec.md, and record it under **Clarifications** (done 2026-11-27).
- [X] T003 Record the maintainer's approval of the 2026-11-27 revision: the **Status** line of specs/006-submodules-and-lfs/plan.md and the five decisions under **For the maintainer's eye** (done 2026-11-27; approved without changes).
- [ ] T004 Run `make test` once before any change and note pre-existing failures, so later ones can be told apart.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: the settings, the value types, the credential hooks, the access predicate and the plumbing every story
writes into:

- the report and the workspace path on `RepoHandle`;
- `PublishScope` and `KeptOut` through `GitManager` and the publisher;
- the prompt section and the turn-end notice.

With both settings off, every command, prompt and reply stays as it is today (FR-016).

**⚠️ CRITICAL**: no user story work can begin until this phase is complete.

### Tests for Phase 2

- [ ] T005 [P] In tests/unit_tests/codebase/test_repo_config.py, assert that `RepositoryConfig().checkout` has `submodules is False` and `lfs is False`, and that YAML `checkout: {submodules: true, lfs: true}` parses to both `True`.
- [ ] T006 [P] In tests/unit_tests/codebase/clients/test_base.py:
  - `RepoClient.with_git_auth(repository, operation)` calls `operation` once with `get_git_auth_env(repository)`;
  - `complete_checkout(repo, repository, CheckoutOptions.off(), DENY_ALL)` returns a report whose `is_empty` is true and runs no git command (spy on `RepoCheckout`'s command runner).
- [ ] T007 [P] In tests/unit_tests/codebase/clients/gitlab/test_client.py, test `GitLabClient.with_git_auth`. With `CLONE_TOKEN_PROPAGATION_RETRY_BACKOFF_SECONDS` patched to zeros:
  - an auth error is retried with the same ephemeral token through the backoff;
  - once the retries are spent, the cached token is invalidated (`invalidate_clone_token`) and one fresh token is tried;
  - a PAT token is never retried;
  - a non-auth error propagates at once;
  - with `scoped_only=True` and `GITLAB_PAT_FALLBACK` on, a failed project-token mint raises instead of using the PAT. `get_git_egress_credential(repository, scoped_only=True)` does the same.

  The existing `load_repo` tests keep passing unchanged, proving `_clone` now goes through `with_git_auth`.
- [ ] T008 [P] In tests/unit_tests/codebase/clients/github/test_client.py, assert that `GitHubClient.with_git_auth` mints a repository-scoped installation token (`_mint_installation_token`) on each call and passes its `GitAuthEnv` to the operation, with `scoped_only` either way.
- [ ] T009 [P] In tests/unit_tests/codebase/test_authorization.py:
  - `platform_account_can_view(uid, repo_id)` is true with a fresh `RepositoryAccess` row for the configured provider, uid and repository, and false with no row, a row past the hard TTL, or a row for another provider;
  - `platform_account_can_write(uid, repo_id)` is true only for a fresh row with `access_level=RepoAccessLevel.WRITE`;
  - `is_catalogued(repo_id)` is true only for a repository in `RepositoryCatalog`, and false for every repository while the catalog is empty.
- [ ] T010 [P] In tests/unit_tests/automation/agent/test_context.py, test `set_runtime_ctx`:
  - it calls `repo_client.complete_checkout` once per repository, main and extra, after the clones;
  - each call gets `CheckoutOptions` built from that repository's own `RepositoryConfig.checkout` and `CODEBASE_LFS_MAX_CLONE_BYTES`, and the `access` passed to `set_runtime_ctx` (`DENY_ALL` when omitted);
  - each call runs in a worker thread, not on the event loop (assert `threading.current_thread()` is not the loop's);
  - `RepoHandle.checkout` holds the returned report;
  - `RepoHandle.workspace_path` is `/workspace/repo` for the main handle and `/workspace/repos/<slug>` for an extra one, and the git system prompt's "Other repositories" lines are unchanged byte for byte.
- [ ] T011 [P] In tests/unit_tests/sessions/executor/test_run.py, assert the `RunRepoAccess` the executor passes as `access`, by the run's person:
  - a `Run` whose `user` is set: `can_read` checks `can_view(user, repo_id)` and `can_write` checks `can_run(user, repo_id)`;
  - a `Run` with only `external_uid`: `platform_account_can_view` and `platform_account_can_write`;
  - a `Run` with neither, of a trigger type in `SessionOrigin.daiv_authored()`, in a session whose `external_uid` is set: the session's platform account, as the line above. The `Run`'s fields stay unchanged;
  - `in_catalog` checks `is_catalogued` for every person;
  - any other `Run` with neither, or a spec with no `run_id`, answers no to all three;
  - a run with `acts_as_user=False` still checks its attributed person.
- [ ] T012 [P] In tests/unit_tests/automation/agent/middlewares/test_git.py:
  - with every handle's report empty, the rendered git system prompt equals today's byte for byte;
  - with a report holding checked-out and skipped submodules and skipped LFS files, the section of contracts/agent-context.md §1 renders once per such repository, in repository order. Every checked-out submodule is in the read-only list with no reason (T045 adds the published list and the `PublishBlock` phrases);
  - lists stop at 20 entries plus "and N more";
  - each `SkipReason` renders its phrase verbatim;
  - a URL's user info never appears;
  - the "not published" bullet renders whenever either setting is on;
  - a `KeptOut` on the main outcome or on an extra repository's `RepoPublish` adds the notice of §2 as a visible assistant message beside any conflicts notice, naming the repository when the session has extra repositories. An empty `KeptOut` adds nothing.
- [ ] T013 [P] In tests/unit_tests/automation/agent/workspace/test_git_manager.py, assert that `status_snapshot(..., scope=PublishScope())`, `fast_forward(branch)`, `commit_all(message)` and `push_head_to(..., integrate_on_reject=True)` send exactly today's git argument lists (record the runner's calls), and that `RepoStatus.kept_out` is empty.
- [ ] T014 [P] In tests/unit_tests/automation/agent/test_git_state.py, assert that `RepoPublish` round-trips `kept_out`, `submodule_path`, `root_repo_id`, `parent_repo_id` and `recorded_sha` through `to_state` and `from_state`, and that a state written before this change (without the keys) loads with `""` and `None`.

### Implementation for Phase 2

- [ ] T015 [P] In daiv/codebase/repo_config.py, add `class Checkout(BaseModel)` with `submodules: bool = Field(default=False, description=...)` and `lfs: bool = Field(default=False, description=...)`, and `RepositoryConfig.checkout: Checkout = Field(default_factory=Checkout, description=...)`, after `memory`.
- [ ] T016 [P] In daiv/codebase/conf.py, add `LFS_MAX_CLONE_BYTES: int = Field(default=268435456, ge=0, description=...)`. The environment variable is `CODEBASE_LFS_MAX_CLONE_BYTES`, and `0` downloads no LFS content.
- [ ] T017 [P] First move `_INHERITED_ENV` out of daiv/automation/agent/workspace/git_runners.py into daiv/codebase/clients/base.py as `GIT_INHERITED_ENV`, beside `GIT_NO_PROMPT_ENV`, and import it back in git_runners.py: `codebase` must not import `automation` (layering). Then create daiv/codebase/clients/checkout.py with the types of data-model.md:
  - `CheckoutOptions` (frozen): `submodules`, `lfs`, `lfs_max_bytes`, `branches`, `pins`, with classmethods `off()` and `from_config(checkout, lfs_max_bytes, *, branches=..., pins=...)`;
  - `RunRepoAccess` (Protocol: `can_read`, `can_write`, `in_catalog`) and `DENY_ALL`;
  - `SkipReason` (StrEnum): `outside_platform`, `not_catalogued`, `daiv_no_access`, `person_no_access`, `commit_missing`, `over_limit`, `fetch_failed`;
  - `PublishBlock` (StrEnum): `published_elsewhere`, `parent_not_published`, `person_no_write`;
  - `SubmoduleCheckout` (frozen) with every field of data-model.md; `LfsFile` (frozen): `path`, `size`, `skipped`;
  - `CheckoutReport` (frozen): `options`, `submodules`, `lfs_files`, with `skipped_submodules`, `checked_out_submodules`, `skipped_lfs_files`, `is_empty` and `EMPTY`;
  - class `RepoCheckout(client, *, options, access, allowed_protocol="https", lfs_filter_config=LFS_FILTER_CONFIG)`, whose `complete(repo, repository, *, parent_base_branch) -> CheckoutReport` returns the empty report for `options.off()`. `LFS_FILTER_CONFIG` holds the `filter.lfs.*` keys that run git-lfs; tests pass a test-only clean filter instead. Its command runner runs git subprocesses with the environment it is given, built from `GIT_INHERITED_ENV` and never from `os.environ` whole; tests replace it.
- [ ] T018 In daiv/codebase/clients/base.py, add two concrete `RepoClient` methods:
  - `with_git_auth(repository, operation: Callable[[GitAuthEnv | None], T], *, scoped_only=False) -> T`, which calls `operation(self.get_git_auth_env(repository))` once;
  - `complete_checkout(repo, repository, options, access, *, parent_base_branch="") -> CheckoutReport`, which returns `RepoCheckout(self, options=options, access=access).complete(repo, repository, parent_base_branch=parent_base_branch)`.

  Add `scoped_only=False` to `get_git_egress_credential` too.
- [ ] T019 In daiv/codebase/clients/gitlab/client.py, override `with_git_auth`. Move into it the propagation retry (`_clone_awaiting_token_propagation`) and the stale-token self-heal from `_clone`, generalised over `operation`. The PAT is still never retried. With `scoped_only=True`, `_get_clone_token` and `_git_egress_token` never fall back to the PAT, and a failed mint raises. Rewrite `_clone` to call `self.with_git_auth(repository, clone_with)`, where `clone_with` removes a partial clone directory before each attempt, as today.
- [ ] T020 [P] In daiv/codebase/clients/github/client.py, override `with_git_auth` to mint `_mint_installation_token(repository)` per call and pass `GitAuthEnv.for_token(repository.clone_url, token)`. Make `load_repo` clone through it.
- [ ] T021 [P] In daiv/codebase/authorization.py, add:
  - `platform_account_can_view(uid, repo_id) -> bool`: `_fresh_rows(uid).filter(repo_id=repo_id).exists()`, after `_maybe_enqueue_backstop()`, as `_resolve_uid` does;
  - `platform_account_can_write(uid, repo_id) -> bool`: the same, filtered on `access_level=RepoAccessLevel.WRITE`;
  - `is_catalogued(repo_id) -> bool`: membership in `RepositoryCatalog`, strictly. Unlike `assert_catalogued`, an empty catalog holds nothing.
- [ ] T022 In daiv/automation/agent/context.py:
  - add `RepoHandle.checkout: CheckoutReport` (default `CheckoutReport.EMPTY`) and `RepoHandle.workspace_path: str`, set to `REPO_PATH` for the main handle and `f"{EXTRA_REPOS_PATH}/{slug}"` for an extra one; make every caller of `extra_repo_path(handle)` read `handle.workspace_path`, then remove `extra_repo_path`;
  - add `access: RunRepoAccess = DENY_ALL` to `set_runtime_ctx`;
  - after the clones, complete every handle with `asyncio.gather(*(asyncio.to_thread(repo_client.complete_checkout, h.gitrepo, h.repository, CheckoutOptions.from_config(h.config.checkout, codebase_settings.LFS_MAX_CLONE_BYTES, branches=..., pins=...), access) for h in handles))`, and rebuild each handle with `dataclasses.replace(handle, checkout=report)` before building `RuntimeCtx`. Until US2, `branches` and `pins` are empty.
- [ ] T023 In daiv/sessions/executor/run.py, build the `RunRepoAccess` of T011 and pass it to `set_runtime_ctx` as `access`. Load `user` (with `is_admin`), `external_provider`, `external_uid` and `trigger_type` from `Run` by `spec.run_id`, and the session's `external_provider` and `external_uid` for the fallback. Keep it a small class (e.g. `PersonRepoAccess`) with the three methods, per code-organization rule 1. It runs in the checkout's worker thread, so its ORM calls are synchronous.
- [ ] T024 In daiv/automation/agent/workspace/git_manager.py:
  - add `PublishScope` (frozen; `submodules: bool = False`, `lfs: bool = False`, `record: Mapping[str, str]`, defaulting to an empty immutable mapping; `from_config(checkout, record=...)`);
  - add `KeptOut` (`lfs_paths`, `submodules`, falsy when empty) and `KeptOutSubmodule` (`path`, `commit_moved`, `removed`, `added`, `remote_moved`, `block`, `changed_files` capped at 20, `more`);
  - add `RepoStatus.kept_out`;
  - add `scope: PublishScope = PublishScope()` to `status_snapshot`, `fast_forward` and `push_head_to` (which hands it to `_integrate_remote`), and `keep_out: Sequence[str] = ()` and `record: Mapping[str, str]` (empty by default) to `commit_all`. With the defaults, all four send exactly today's commands;
  - give `fast_forward` on daiv/automation/agent/workspace/disk.py and daiv/automation/agent/workspace/sandbox.py a `scope` parameter, and make `_fast_forward` in daiv/sessions/executor/run.py pass `PublishScope.from_config(ctx.config.checkout)`, since the workspaces hold no context of their own.
- [ ] T025 Thread the scope and the result through the publisher:
  - daiv/automation/agent/publishers.py: `GitChangePublisher.publish` takes an optional `scope` (default `PublishScope.from_config(self.ctx.config.checkout)`), passes it to `status_snapshot` and `push_head_to`, and passes the snapshot's kept-out paths (gitlinks and LFS paths) to `commit_all`; `PublishOutcome` gains `kept_out: KeptOut | None`, which `_apublish_extra_repo` copies onto `RepoPublish`;
  - daiv/automation/agent/git_state.py: `RepoPublish.kept_out`, `submodule_path`, `root_repo_id`, `parent_repo_id` and `recorded_sha` (strings default `""`), round-tripped through `to_state` and `from_state`.
- [ ] T026 In daiv/automation/agent/middlewares/git.py:
  - render the per-repository section of contracts/agent-context.md §1 from `handle.checkout` in `GIT_SYSTEM_PROMPT` (mustache data built in `awrap_model_call`; nothing renders for an empty report). Until T067, every checked-out submodule is listed as read-only;
  - add `_kept_out_notice(kept_out, where)`, rendering §2, to the turn-end messages beside `_conflicts_notice`, for the main outcome and for each extra repository.

**Checkpoint**: settings parse, and every run completes an empty checkout. Prompts, commands and replies are unchanged
for repositories that leave both settings off.

---

## Phase 3: User Story 1 - The agent works with the code in a repository's submodules (Priority: P1) 🎯 MVP

**Goal**: with `checkout.submodules: true`, every reachable submodule the person can read is checked out, recursively
and on both platforms, each with its own credential. Until US2, changes inside submodules are never published, and
are named in the reply.

**Independent Test**: spec US1, with this phase's unit tests. The quickstart's scenarios expect US2's lists, so they
run after US2.

### Tests for User Story 1

- [ ] T027 [P] [US1] In tests/unit_tests/codebase/clients/test_checkout.py, test URL resolution and the catalog (research R2, R3), using a stub client whose clone URL is `https://git.example.com/group/app.git`:
  - `../lib.git` → `group/lib`;
  - `https://git.example.com/group/lib.git` → `group/lib`;
  - `git@git.example.com:group/lib.git` → `group/lib`;
  - `ssh://git@git.example.com:2222/group/lib.git` → `group/lib`;
  - a nested `../inner.git` resolves against its parent's resolved URL;
  - `https://other.example.com/x.git`, `file:///tmp/x`, `/tmp/x`, `ext::sh -c x` and `git://git.example.com/x.git` give `outside_platform`, with no git command run for them;
  - `in_catalog` false gives `not_catalogued`, with no lookup and no git command;
  - `submodule.<name>.url` in the parent's `.git/config` is rewritten to the stub repository's `clone_url`, and the checked-out submodule's `remote.origin.url` equals it;
  - the report's `url` has user info stripped.
- [ ] T028 [US1] In tests/unit_tests/codebase/clients/test_checkout.py, with real git, `allowed_protocol="file"` and a stub client mapping `group/lib` and `group/inner` to local bare repositories, test a superproject with `libs/lib` (holding a nested `vendor/inner`):
  - both are checked out at the recorded commits, not at the branch tips;
  - the report lists `libs/lib` and `libs/lib/vendor/inner` in that order, with `parent_path` `""` and `libs/lib`;
  - `git status --porcelain` in the superproject is empty;
  - with `shallow = true` in `.gitmodules`, the submodule's clone is not shallow and has every remote branch.
- [ ] T029 [US1] In tests/unit_tests/codebase/clients/test_checkout.py, test access:
  - `get_repository` raising → `daiv_no_access`, the directory stays empty and `with_git_auth` is never called;
  - `can_read` returning false → `person_no_access`, with the same effects, and its nested submodules are absent from the report;
  - `DENY_ALL` checks out nothing.
- [ ] T030 [US1] In tests/unit_tests/codebase/clients/test_checkout.py, test credentials:
  - each submodule's commands run inside `client.with_git_auth(<that submodule's Repository>, ..., scoped_only=True)`;
  - their environment holds that `GitAuthEnv`'s header and not the main repository's;
  - `GIT_ALLOW_PROTOCOL` equals the allowed protocol;
  - the update runs with `core.hooksPath=/dev/null`, `--checkout` and `--no-recommend-shallow`, so a `.gitmodules` `update = !cmd` never runs (assert a marker file is not created).
- [ ] T031 [P] [US1] In tests/unit_tests/automation/agent/workspace/test_git_manager.py, with real git and a superproject holding a checked-out submodule and `PublishScope(submodules=True)`:
  - an edit inside the submodule only → `status_snapshot` reports `dirty` false, an empty diff, and `kept_out.submodules[0].changed_files == ("f",)`;
  - an edit in the superproject and inside the submodule → `commit_all` commits only the superproject file;
  - the submodule's HEAD moved → the gitlink is not in the commit, and `commit_moved` is true;
  - the submodule's directory deleted, or moved with `git mv` → neither the gitlink's deletion nor its rename is in the commit, and `removed` is true for the old path;
  - a submodule added with `git submodule add` → no new gitlink is in the commit, `added` is true, and the `.gitmodules` edit is committed like any file;
  - a repository cloned inside the checkout as the only change → `dirty` false, `added` true, no commit and no error;
  - more than 20 changed files → 20 names and `more`;
  - `fast_forward` on a checkout whose only local changes are inside the submodule still answers `advanced` when the remote moved ahead.
- [ ] T032 [P] [US1] In tests/unit_tests/automation/agent/test_publishers.py, test the publish:
  - a turn whose only change is inside a submodule publishes nothing, raises nothing, and returns `kept_out` (FR-013);
  - FR-014, with a local bare "origin": a publish integrates a remote commit that moved the gitlink, and its submodule's checkout still sits at the old commit. A second publish from the same checkout must then push a branch whose gitlink is still the remote's. Without T036's reset, `git add -A` stages the old commit and the test fails.
- [ ] T033 [P] [US1] In tests/unit_tests/automation/agent/workspace/test_session.py, assert that `_make_repo_archive` keeps `.git/modules/**` and the submodule's `.git` file, and that `git status --porcelain` is empty in a copy extracted with `tarfile`.

### Implementation for User Story 1

- [ ] T034 [US1] In daiv/codebase/clients/checkout.py, implement the reading of `.gitmodules` (`git config -f .gitmodules -z --get-regexp '^submodule\..*\.(path|url|branch)$'`), the URL resolution and the host and protocol policy of research R2, the catalog check of R3, and the rewrite of `submodule.<name>.url` to the looked-up repository's `clone_url`.
- [ ] T035 [US1] In daiv/codebase/clients/checkout.py, implement each submodule's checkout (research R3):
  - `access.in_catalog(repo_id)` (`not_catalogued`), then `client.get_repository(repo_id)` (`daiv_no_access` when it raises), then `access.can_read(repo_id)` (`person_no_access`);
  - inside `client.with_git_auth(sub_repository, ..., scoped_only=True)`: `git submodule init -- <path>`, the URL rewrite, then `git -c core.hooksPath=/dev/null submodule update --checkout --no-recommend-shallow -- <path>`, with an environment of only `GitAuthEnv.as_env()`, `GIT_NO_PROMPT_ENV`, `GIT_ALLOW_PROTOCOL` and the variables `GIT_INHERITED_ENV` lists (T017);
  - recursion into the submodule's own `.gitmodules`;
  - `repository` and `parent_path` on the `SubmoduleCheckout`, for US2's handle.
- [ ] T036 [US1] In daiv/automation/agent/workspace/git_manager.py, with `scope.submodules`, implement research R8 for submodules:
  - add `--ignore-submodules=all` to the snapshot's `status` and `diff` calls;
  - add `status --porcelain=v2 -z` to batch A. Read as submodule changes every `S...` entry and every entry whose `HEAD` mode is `160000`, so a deleted or renamed submodule is kept out too. Read an untracked directory holding `.git` as `added`, not as a change;
  - add one `-C <path> status --porcelain -z` per changed submodule, in the same round trip as batch B;
  - fill `RepoStatus.kept_out`;
  - in `commit_all`, after `add -A`, run `git --literal-pathspecs reset -q -- <paths>` for `keep_out` and for every path `diff --cached --raw -z` shows with the new mode `160000` that `record` doesn't name. Then skip the commit when `diff --cached --quiet` finds the index equal to `HEAD`, and report that nothing was committed so the publisher publishes nothing;
  - in `fast_forward`, add `--ignore-submodules=all` to its `status --porcelain`, so changes kept out of earlier publishes don't count as unpublished work.

**Checkpoint**: US1 works alone. Every submodule is read-only until US2: nothing publishes submodules yet, the prompt
lists every checked-out submodule as read-only, and `KeptOut` names every edit inside a submodule.

---

## Phase 4: User Story 2 - The agent's changes inside a submodule become a merge request in its repository (Priority: P2)

**Goal**: changes inside submodules the person can write become a merge request in each submodule's repository,
innermost first, and the parent records the commit DAIV pushed. The merge requests link each other, later turns
continue on the same branches, a squash or rebase merge makes DAIV re-point the parent, and sessions that fetched a
submodule are hidden from people who can't read it.

**Independent Test**: spec US2, and quickstart scenarios 1–13 and 17.

### Tests for User Story 2

- [ ] T037 [P] [US2] In tests/unit_tests/codebase/clients/test_checkout.py, test the checkout's publish verdict and target (research R14, R17), with stub `RunRepoAccess` answers and real git:
  - `can_write` false → `person_no_write`; true → `publish_block is None`;
  - `target_branch` is `.gitmodules`' `branch` when the submodule's remote has it;
  - it is the `parent_base_branch` argument for `branch = .` when the remote has that branch;
  - it is the submodule repository's default branch otherwise, including a named branch missing on the remote.
- [ ] T038 [US2] In tests/unit_tests/codebase/clients/test_checkout.py, with real git and local bare remotes, test branch hints and pins (research R19, R21):
  - a branch in `branches` whose head contains the recorded commit is checked out as a local branch at its head, and the report's `branch` names it;
  - a branch that doesn't contain the recorded commit leaves the submodule detached at the recorded commit, with the R19 note;
  - a branch missing on the remote leaves it detached with `branch == ""` and no note;
  - a pin whose `recorded_sha` equals the commit the parent records gives `pinned == merged_sha`, and leaves the submodule's checkout where it was;
  - a pin whose `recorded_sha` differs gives `pinned == ""` and the R21 note.
- [ ] T039 [P] [US2] In tests/unit_tests/codebase/clients/gitlab/test_client.py and tests/unit_tests/codebase/clients/github/test_client.py, test the two new methods with stubbed API objects:
  - `get_merged_commit(repo_id, iid)`. GitLab: `merge_commit_sha`, else `squash_commit_sha`, else the merge request's head `sha`. GitHub: `merge_commit_sha`. `None` for an unmerged merge request.
  - `is_ancestor(repo_id, ancestor, descendant)`. GitLab: true when `repository_merge_base([ancestor, descendant])` returns `ancestor`. GitHub: true when `compare(ancestor, descendant).status` is `ahead` or `identical`.
- [ ] T040 [P] [US2] In tests/unit_tests/automation/agent/test_context.py, test submodule handles and the session-wide verdicts (research R14, R15):
  - each checked-out submodule becomes a `RepoHandle` in its root handle's `submodules`, in checkout order, with `workspace_path` `/workspace/repo/libs/lib` and `/workspace/repo/libs/lib/vendor/inner`, and under `/workspace/repos/<slug>/` for an extra repository's;
  - its `config` comes from the submodule repository's own `RepositoryConfig`, and its `submodule` role carries the report's path, parent path, recorded commit, target branch, pin and note;
  - submodule handles are never in `ctx.repos` or `ctx.extra_repos`;
  - a submodule whose repository is the session's extra repository → `published_elsewhere`;
  - the same repository as a submodule of the main and of an extra repository → the main's checkout is publishable and the other is `published_elsewhere`;
  - an earlier checkout blocked by `person_no_write` doesn't count as publishing, so a later one isn't `published_elsewhere` for it;
  - a nested submodule under a blocked one → `parent_not_published`.
- [ ] T041 [P] [US2] In tests/unit_tests/automation/agent/workspace/test_git_manager.py, with real git, test `PublishScope.record` (research R16):
  - a record entry whose commit differs from the index makes `dirty` true with no other change. The diff the metadata reads holds `git diff --submodule=log` for it, or the two short commit ids when the commit is missing from the submodule's clone;
  - `commit_all(..., record={"libs/lib": sha})` commits a tree whose `libs/lib` entry is `160000 commit <sha>`, even when the submodule's clone lacks that commit. Every other gitlink and kept-out path stays as in HEAD.
- [ ] T042 [US2] In tests/unit_tests/automation/agent/workspace/test_git_manager.py, test research R20 with a local bare remote:
  - the remote branch moved `libs/lib`, and the run's commit records `libs/lib` at another commit and changes `README.md`. After `push_head_to(..., integrate_on_reject=True, scope=PublishScope(submodules=True, record={"libs/lib": ...}))`, the pushed branch has the remote's `libs/lib` and the run's `README.md`, and the outcome's `KeptOutSubmodule` has `remote_moved` true;
  - the same with the integration as a merge (the run's commits include a merge commit);
  - a conflict on an ordinary file still aborts with `GitPushStaleError`;
  - a commit left empty by the resolution is dropped.
- [ ] T043 [P] [US2] In tests/unit_tests/automation/agent/test_publishers.py, test `apublish_submodules` with real git, local bare remotes for the superproject and each submodule, and a stub merge request client:
  - changes in `libs/lib` → `group/lib` is pushed to a new branch, and its merge request targets the handle's `target_branch`. The returned record maps `libs/lib` to that `pushed_sha`, and its `RepoPublish` has `submodule_path="libs/lib"`, `root_repo_id`, and `parent_repo_id` equal to the superproject's;
  - nested changes → the order is `vendor/inner`, then `libs/lib`. `libs/lib`'s commit records `vendor/inner` at its pushed commit, and `vendor/inner`'s `recorded_sha` is set after `libs/lib`'s publish succeeds;
  - a pinned child → a record entry with the merged commit and a `RepoPublish` with `published` false;
  - a stubbed push failure on `libs/lib` → no record entry, `RepoPublish.error` set, and the other submodules still publish;
  - a blocked submodule's changes are kept out of its parent;
  - an unchanged, unpinned submodule adds no record entry;
  - `run_base_branch` returns `target_branch` for a submodule handle on a branch with no open merge request.
- [ ] T044 [P] [US2] In tests/unit_tests/automation/agent/test_sibling_links.py, assert that `render_related_block(viewer, others)` sorts each other merge request by `parent_repo_id` into the groups of contracts/agent-context.md §4, as seen from `viewer`. A nested chain's middle merge request shows both **Merge first** and **Recorded by**. Only the groups present render, and re-rendering replaces the marker block instead of adding one.
- [ ] T045 [P] [US2] In tests/unit_tests/automation/agent/middlewares/test_git.py:
  - `aafter_agent` publishes in the order: main's submodules, main recording them, then each extra repository's submodules before that repository. Submodule publishes are in `repo_publishes`, and `update["repo_publishes"]` is written in a session with no extra repository;
  - the root's record entries get their `recorded_sha` only when the root's publish succeeded;
  - `_sibling_notices` and `_alink_related` handle submodule publishes beside extra ones, without `ValueError` from the `strict=True` zip;
  - a failed main publish after a submodule publish is recorded and noticed, not raised, and the submodule's results still reach the state;
  - `_alink_related` still runs on a main-only turn in a session with extra repositories (today's condition), and also on a turn that published a submodule merge request in a session with no extra repository;
  - the prompt section lists published and read-only submodules as contracts/agent-context.md §1 shows, including a working branch's merge request, the R19 note and each `PublishBlock` phrase;
  - a failed submodule publish, an applied pin and a left pin each add their line to the notice of §2.
- [ ] T046 [P] [US2] In tests/unit_tests/automation/agent/workspace/test_sandbox.py and tests/unit_tests/automation/agent/workspace/test_session.py:
  - `for_repo(sub)` runs git at `sub.workspace_path`. Its `authenticate` calls `aadd_repository(sub.repository, mint)` once before returning the manager, where `mint` calls `get_git_egress_credential(sub.repository, scoped_only=True)`;
  - `aadd_repository` appends one credential after the acquire-time ones, whose secret indices stay unchanged, and calls `update_egress` even within `CREDENTIAL_REFRESH_WINDOW_SECONDS`;
  - a second call for the same repository reuses its slot;
  - on disk, `for_repo(sub)` builds `LocalGitRunner` over the submodule's directory with the submodule's scoped auth environment.
- [ ] T047 [P] [US2] In tests/unit_tests/sessions/test_models.py:
  - `SessionSubmodule` is unique on `(session, repo_id)`, and cascades on session delete;
  - `SessionOrigin.SUBMODULE_MERGE` is in `daiv_authored()` and `classified()`;
  - `Session.aholds_other_repositories()` is true with an extra repository or a submodule row, and false with neither.
- [ ] T048 [P] [US2] In tests/unit_tests/sessions/test_managers.py:
  - a non-owner who can read the main repository and every extra one but not a submodule row's repository doesn't see the session; one who can read all of them does; owners and admins are unaffected;
  - `without_live_work` excludes a session with a `merge_pending` row;
  - `on_merge_request(iid, repo_id=...)` matches a `SessionSubmodule` row's merge request.
- [ ] T049 [P] [US2] In tests/unit_tests/sessions/test_multi_repo.py, test `apersist_submodule_state`:
  - it writes `ref`, the merge request fields and `diff_stats` from a submodule `RepoPublish`, and `recorded_sha` when the entry has one;
  - it skips an entry with an error, and never clears `ref` or a merge request;
  - a pinned entry with `published` false updates `recorded_sha` only.
- [ ] T050 [P] [US2] In tests/unit_tests/memory/test_extraction_task.py, assert that extraction is skipped for a session that has a `SessionSubmodule` row and no extra repository.
- [ ] T051 [P] [US2] In tests/unit_tests/sessions/test_page_state.py, assert a repository card for each submodule row with a merge request, labelled with its path and parent, and none for a row without one. Its `can_act` follows write access to its repository.
- [ ] T052 [P] [US2] In tests/unit_tests/sessions/executor/test_run.py:
  - after the checkout and before the agent runs, the executor creates a `SessionSubmodule` row per checked-out submodule whose repository is not the session's main or extra one, and leaves existing rows' state alone. Assert the row exists inside the stubbed agent call;
  - it passes each root's `branches` from its rows, and `pins` from a taken `submodule_merged` event;
  - at turn end, a submodule `RepoPublish` updates its `SessionSubmodule` row, not a `SessionRepository` row, also for a run with `persist_ref=False`. With `arm_watch`, its merge request is claimed for the CI watch. It appears in `Run.merge_requests` and `Run.sibling_pushed_shas`;
  - in a `submodule_merged` run whose parent merge request has merged, the parent starts from its default branch with no target merge request, also while its source branch still exists.
- [ ] T053 [P] [US2] In tests/unit_tests/sessions/executor/test_recovery.py, assert that draft recovery publishes each repository's submodules before it, as drafts, and fills the root's record entries' `recorded_sha` after the root's publish.
- [ ] T054 [P] [US2] In tests/unit_tests/sessions/inbox/test_kinds.py, test_render.py and test_drain.py:
  - `submodule_merged` has a registered payload, wakes the session, is not gated, and gives its run the trigger type `SUBMODULE_MERGE`;
  - a run woken only by it renders the DAIV-written prompt of contracts/agent-context.md §3 from the payload alone;
  - a non-mention event whose requester can't read a submodule row's repository is failed as `access_denied`, while a mention is not;
  - `_proves_access` refuses a CI identity whose linked user can't read a submodule row's repository.
- [ ] T055 [P] [US2] In tests/unit_tests/sessions/test_submodule_merges.py (new), test `settle_submodule_merge_task` with a stub client, for each `merge_pending` row on the merge request:
  - `recorded_sha` is an ancestor of the merged commit → the row's `ref`, merge request and `merge_pending` are cleared, no event is posted, and `MergeArchive.settle` archives the session it left pending;
  - it isn't → one waking `submodule_merged` event with the data-model payload, `InboxEvent.repo_id` the submodule's repository and source key `submodule_merged:<repo_id>!<iid>`, the row cleared, and the session not archived;
  - running the task twice (a redelivered webhook) posts one event;
  - a platform error → an ERROR log naming the repository and merge request, a remembered `notice` event saying the pointer couldn't be checked, the row cleared, and the session settled;
  - a row whose merge request iid no longer matches is skipped.
- [ ] T056 [P] [US2] In tests/unit_tests/webhooks/gitlab/test_callbacks.py, tests/unit_tests/webhooks/github/test_callbacks.py and tests/unit_tests/sessions/test_auto_archive.py:
  - a merge of a merge request that a `SessionSubmodule` row holds sets `merge_pending` before `MergeArchive` runs, and enqueues the task. `MergeArchive` leaves that session pending, not archived;
  - a merge of the parent's merge request right after, before the task ran, also leaves it pending;
  - other sessions on the same merge request are archived as today;
  - a close without merge clears the row's `ref` and merge request, and enqueues nothing.

### Implementation for User Story 2

- [ ] T057 [US2] In daiv/codebase/clients/checkout.py, implement the checkout's part of research R14 and R17: `person_no_write` from `access.can_write`, and `target_branch` checked against the submodule clone's remote branches.
- [ ] T058 [US2] In daiv/codebase/clients/checkout.py, implement `branches` and `pins` (research R19, R21). Branch hints run `merge-base --is-ancestor`, then `checkout -B <branch> origin/<branch>`. Pins compare the parent's recorded commit with `recorded_sha` and set `pinned` or the note, with no checkout. Fill `branch`, `pinned` and `note`.
- [ ] T059 [P] [US2] In daiv/codebase/clients/base.py, add abstract `get_merged_commit(repo_id, iid) -> str | None` and `is_ancestor(repo_id, ancestor, descendant) -> bool`. Implement both in daiv/codebase/clients/gitlab/client.py and daiv/codebase/clients/github/client.py as T039 describes. In `SWERepoClient` (evals/swe_client.py) they raise `NotImplementedError`, since evals publish nothing.
- [ ] T060 [US2] In daiv/automation/agent/context.py:
  - add `SubmoduleRole`, and `RepoHandle.submodules` and `RepoHandle.submodule`;
  - after every checkout is complete, apply `published_elsewhere` and then `parent_not_published` across the whole session, in session order (research R14);
  - build a handle per checked-out submodule (research R15): the report's `Repository`, `RepositoryConfig.get_config(repo_id)` in a worker thread, `git.Repo(<clone>/<path>)`, `current_ref` and `head_detached` from the checkout, and `workspace_path`;
  - pass the root's base branch to `complete_checkout` for `branch = .`;
  - add `submodule_branches` and `submodule_pins` to `set_runtime_ctx`, and build each root's `CheckoutOptions` from them.
- [ ] T061 [US2] In daiv/automation/agent/workspace/git_manager.py, implement `PublishScope.record` (research R16): a record entry that differs from the index makes the snapshot dirty, adds `diff --submodule=log -- <path>` (or the short ids) to batch B, and is excluded from `KeptOut`. `commit_all` runs `update-index --cacheinfo 160000,<sha>,<path>` per entry after the resets.
- [ ] T062 [US2] In daiv/automation/agent/workspace/git_manager.py, implement research R20 in `_integrate_remote`:
  - when the rebase or merge stops and every path in `ls-files -u -z` is a `scope.record` gitlink, write back the remote's stage with `update-index --cacheinfo` and continue (`-c core.editor=true rebase --continue`, or `commit --no-edit`), dropping an empty commit;
  - report the paths so the publisher puts them in `KeptOut` as `remote_moved`;
  - any other conflict keeps today's abort.
- [ ] T063 [US2] In daiv/automation/agent/workspace/session.py, add `SandboxSession.aadd_repository(repository, mint)` (research R18). It appends to the session's credential list, or reuses the repository's slot, calls `mint`, rebuilds with `with_platform_credentials`, and calls `update_egress`, ignoring the refresh window.
- [ ] T064 [US2] In daiv/automation/agent/workspace/disk.py and daiv/automation/agent/workspace/sandbox.py, make `for_repo(handle)` use `handle.workspace_path`. For a submodule handle in a sandbox, `authenticate` awaits `aadd_repository(handle.repository, mint)` with a scoped mint from `handle.client` before building the manager. On disk, it uses the submodule's own scoped auth environment.
- [ ] T065 [US2] In daiv/automation/agent/publishers.py:
  - `run_base_branch` returns `handle.submodule.target_branch` for a submodule handle with no open merge request on its `current_ref`;
  - add `apublish_submodules(ctx, workspace, handle, *, settings, session_links, skip_ci=False, recovering=False) -> tuple[list[RepoPublish], dict[str, str]]`. It publishes `handle.submodules` whose `publish_block is None`, deepest path first. Each one's `PublishScope` follows the root's settings, plus `record` for its direct children, published or pinned. It sets `recorded_sha` on a child's entry once its submodule parent's publish succeeded, and returns the entries and the record map for the root's direct children (research R16);
  - one submodule's failure never stops the others or its parent.
- [ ] T066 [US2] In daiv/automation/agent/sibling_links.py, add `RelatedMergeRequest.parent_repo_id`, and make `render_related_block(viewer, others)` group per viewer (research R23).
- [ ] T067 [US2] In daiv/automation/agent/middlewares/git.py:
  - in `aafter_agent`, call `apublish_submodules` before the main publish and before each extra repository's. Pass the record map into that repository's publish scope, fill the children's `recorded_sha` when it succeeded, and append the submodule publishes to `repo_publishes`;
  - write `update["repo_publishes"]` whenever there are extra or submodule publishes;
  - record and notice a failed main publish, instead of raising it, whenever submodules were published;
  - make `_sibling_notices` and `_alink_related` look each publish's repository up by `repo_id` instead of zipping with `ctx.extra_repos`;
  - run `_alink_related` on today's condition or when a submodule merge request was published;
  - extend the prompt section with the published and read-only lists of contracts/agent-context.md §1;
  - add failed submodule publishes, `remote_moved` entries and pin outcomes to the notice of §2.
- [ ] T068 [US2] In daiv/sessions/executor/recovery.py, publish each repository's submodules before it through `apublish_submodules` with `recovering=True`, and fill the root's record entries' `recorded_sha` as T067 does.
- [ ] T069 [US2] In daiv/sessions/models.py:
  - add `SessionSubmodule` (data-model.md), `EventKind.SUBMODULE_MERGED` and `SessionOrigin.SUBMODULE_MERGE`, the latter in `daiv_authored()` and `classified()`;
  - add `Session.aholds_other_repositories()`;
  - write the migrations (`uv run python daiv/manage.py makemigrations sessions`): the table, plus the `inbox_event_kind_valid`, `session_origin_valid`, `run_trigger_type_valid` and `run_one_next_run_per_*` constraints.
- [ ] T070 [US2] In daiv/sessions/multi_repo.py, add `apersist_submodule_state(session_id, publishes)` (T049) and `aload_submodules(session_id)`.
- [ ] T071 [US2] In daiv/sessions/executor/run.py:
  - after `set_runtime_ctx` returns and before the agent runs, create the missing `SessionSubmodule` rows from every handle's checked-out submodules, the first checkout per repository;
  - pass `submodule_branches` from the rows and `submodule_pins` from a taken `submodule_merged` event;
  - call `apersist_submodule_state` after every run that published, outside the `persist_ref` gate, and claim submodule merge requests for the CI watch when `arm_watch` is set;
  - for a `submodule_merged` run, look up the parent's merge request (the session's, an extra row's or an outer submodule row's) through the platform. When it has merged, start that repository from its default branch with no target merge request.
- [ ] T072 [P] [US2] In daiv/sessions/managers.py, extend `_readable_repos_q` with `~Exists` over `SessionSubmodule` rows outside the viewable set, `without_live_work` with `Exists` over `merge_pending` rows, and `on_merge_request` with `SessionSubmodule` rows.
- [ ] T073 [P] [US2] In daiv/memory/tasks.py, make the skip ask `Session.aholds_other_repositories()`, keeping the comment on why.
- [ ] T074 [P] [US2] In daiv/sessions/page_state.py and daiv/sessions/templates/sessions/_repository_card.html, add cards for submodule rows with a merge request, labelled "submodule `<path>` of `<parent>`". Run `uv run --only-group=dev djade --target-version 6.0` on the template.
- [ ] T075 [US2] Register the new kind in daiv/sessions/inbox/ and around it:
  - kinds.py: `SubmoduleMergedPayload` in `PAYLOADS`, the kind in `PRECEDENCE` after `CI_FAILED`, and `_TRIGGERS` mapping it to `SessionOrigin.SUBMODULE_MERGE`;
  - render.py: the DAIV-written prompt of contracts/agent-context.md §3;
  - drain.py: the read check on submodule rows for non-mention requesters, beside the write check on extra repositories, and in `_proves_access`;
  - daiv/sessions/inbox_display.py and daiv/chat/static/chat/js/transcript-labels.js: the kind's label.
- [ ] T076 [US2] Create daiv/sessions/submodule_merges.py with `SubmoduleMerges(repo_id, iid)`. Its `amark_merged()` sets `merge_pending` and enqueues `settle_submodule_merge_task`, and its `aclose()` clears the rows. The task implements research R21 step 2 and T055's outcomes, posting through `Inbox(thread_id).apost(...)` and settling through `MergeArchive.settle`.
- [ ] T077 [US2] In daiv/webhooks/gitlab/callbacks.py and daiv/webhooks/github/callbacks.py, call `SubmoduleMerges(repo_id, iid).amark_merged()` on a merge, before `MergeArchive`, and `aclose()` on a close without merge.

**Checkpoint**: US1 and US2 work. Quickstart scenarios 1–13 pass for submodules on a sandbox run and a disk run.

---

## Phase 5: User Story 3 - The agent reads the real content of LFS-tracked files (Priority: P3)

**Goal**: with `checkout.lfs: true`, LFS files hold their content, up to the cap. git status stays clean in sandboxes
without git-lfs, and the sandbox copy carries the content once. LFS paths are never published, in the superproject or
in a submodule DAIV publishes.

**Independent Test**: spec US3, and quickstart scenarios 1–3 and 14 for LFS.

### Tests for User Story 3

- [ ] T078 [P] [US3] In tests/unit_tests/codebase/clients/test_checkout.py, test LFS listing and selection (research R5), with real git and pointers committed by hand:
  - LFS paths come from `check-attr filter`; an LFS-attributed blob that is not a pointer is skipped;
  - sizes come from the pointers;
  - selection is in path order, superproject first, under `lfs_max_bytes`;
  - a file larger than what is left is `over_limit`, while a later smaller file still fits;
  - `lfs_max_bytes=0` marks every file `over_limit`;
  - the stubbed `git lfs pull` gets `--include` batches with `*?[]\` escaped, each under 64 KiB of argv;
  - a path with a comma goes to `git lfs smudge` alone;
  - every LFS command's environment holds `lfs.url=<clone_url>/info/lfs` and the `lfs_filter_config` keys, even when the repository's `.lfsconfig` sets another `lfs.url`.
- [ ] T079 [US3] In tests/unit_tests/codebase/clients/test_checkout.py, test the status after the copy (research R6), with real git and a test-only clean filter passed as `lfs_filter_config`, standing in for git-lfs during the worker refresh:
  - after `complete`, `core.checkStat` is `minimal` and `core.trustctime` is `false` in the superproject's and the submodule's git config;
  - every fetched file's mtime is at least 2 s in the past;
  - after `_make_repo_archive` and `tarfile` extraction, `git status --porcelain` run with `GIT_CONFIG_NOSYSTEM=1` and an empty `HOME` is empty;
  - an edited LFS file shows ` M`, and a new `*.bin` file shows `??`.
- [ ] T080 [P] [US3] In tests/unit_tests/codebase/clients/gitlab/test_client.py and tests/unit_tests/codebase/clients/github/test_client.py, assert that the clone's environment holds `GIT_LFS_SKIP_SMUDGE=1` beside the credential keys.
- [ ] T081 [P] [US3] In tests/unit_tests/automation/agent/workspace/test_session.py:
  - `_make_repo_archive` and `_make_repos_archive` leave out `.git/lfs/**` and `.git/modules/<name>/lfs/**`, and keep every other path;
  - without an `lfs` directory, the archive's member list is unchanged.
- [ ] T082 [P] [US3] In tests/unit_tests/automation/agent/workspace/test_git_manager.py, with real git and `PublishScope(lfs=True)`:
  - an edited, a new, a deleted and a renamed LFS-tracked path are all left out of the commit and listed in `kept_out.lfs_paths`;
  - a non-LFS edit beside them is committed;
  - a turn whose only changes are LFS paths reports `dirty` false;
  - the snapshot's `status`, `diff` and `ls-files --others` calls carry `:(exclude,attr:filter=lfs)`;
  - `fast_forward` on a checkout whose only local change is an LFS-tracked file still answers `advanced` when the remote moved ahead;
  - in a submodule published under a scope with `lfs=True`, an LFS-tracked edit is kept out of the submodule's commit too.

### Implementation for User Story 3

- [ ] T083 [P] [US3] Add `git-lfs` to the runtime `apt-get install` list of docker/production/app/Dockerfile and to that of docker/local/app/Dockerfile. Don't run `git lfs install`: DAIV passes the filter config per command (research R11).
- [ ] T084 [P] [US3] Add `GIT_LFS_SKIP_SMUDGE=1` to the clone environment in daiv/codebase/clients/gitlab/client.py (`clone_with` in `_clone`) and daiv/codebase/clients/github/client.py (`load_repo`).
- [ ] T085 [US3] In daiv/codebase/clients/checkout.py, implement research R5:
  - list pointers per repository;
  - select across the superproject and the fetched submodules under `options.lfs_max_bytes`;
  - fetch each repository's selection inside `client.with_git_auth(<that repository>, ...)`, with `scoped_only=True` for a submodule, `git lfs pull --include` batches and the comma fallback;
  - fill `CheckoutReport.lfs_files`.
- [ ] T086 [US3] In daiv/codebase/clients/checkout.py, implement research R6 for every repository with fetched content: set `core.checkStat=minimal` and `core.trustctime=false` in that repository's config, set the mtimes of the fetched files to now − 2 s, and run `git update-index -q --refresh` with the checkout's `lfs_filter_config` keys in the environment.
- [ ] T087 [P] [US3] In daiv/automation/agent/workspace/session.py, add a tar filter to `_make_repo_archive` and `_make_repos_archive` that drops any directory named `lfs` directly inside a git directory (`.git/lfs`, `.git/modules/**/lfs`) (research R7).
- [ ] T088 [US3] In daiv/automation/agent/workspace/git_manager.py, with `scope.lfs`, implement research R8 for LFS:
  - add the pathspec `.` `:(exclude,attr:filter=lfs)` to the snapshot's `status`, `diff` and `ls-files --others` calls;
  - add `status --porcelain -z -- :(attr:filter=lfs)` to batch A and parse it into `kept_out.lfs_paths`, keeping both paths of a rename;
  - pass those paths to `commit_all`'s reset;
  - in `fast_forward`, add the same exclusion pathspec to its `status --porcelain`.

**Checkpoint**: US1, US2 and US3 work. Quickstart scenarios 1–14 pass on a sandbox run and on a disk run.

---

## Phase 6: User Story 4 - What cannot be fetched or published is reported, and the run goes on (Priority: P4)

**Goal**: any submodule or LFS failure becomes a reason in the report and never fails the run. DAIV-side faults log
at ERROR, without secrets.

**Independent Test**: spec US4, and quickstart scenario 16.

### Tests for User Story 4

- [ ] T089 [P] [US4] In tests/unit_tests/codebase/clients/test_checkout.py, test failures:
  - a submodule whose recorded commit is absent from its repository → `commit_missing`, and the other submodules are still checked out;
  - a submodule whose fetch fails with an auth error after `with_git_auth`'s retries, a failed scoped mint, or a network error → `fetch_failed`;
  - an unexpected exception inside one submodule's checkout → `fetch_failed` for it, a `logger.exception`, and the rest continue;
  - a branch hint or pin whose git command fails → the submodule stays at the recorded commit, detached, with no pin and no note;
  - a failed `git lfs pull` batch → `fetch_failed` for its files;
  - git-lfs missing on the worker (`FileNotFoundError` from the runner) → every selected file `fetch_failed`, and one ERROR naming the missing `git-lfs` program;
  - `complete` never raises.
- [ ] T090 [US4] In tests/unit_tests/codebase/clients/test_checkout.py, use `caplog` to check levels:
  - `daiv_no_access` and `fetch_failed` log at ERROR, naming the repository and the submodule's path;
  - `outside_platform`, `not_catalogued`, `person_no_access`, `commit_missing`, `over_limit` and `person_no_write` log at INFO;
  - the stub token's text appears in no record.
- [ ] T091 [P] [US4] In tests/unit_tests/automation/agent/test_context.py, assert that a `complete_checkout` that raises anyway is logged with `logger.exception`, and `set_runtime_ctx` still yields with `CheckoutReport.EMPTY` and no submodule handles on that handle (FR-008).

### Implementation for User Story 4

- [ ] T092 [US4] In daiv/codebase/clients/checkout.py, classify failures and log them as research R10 says:
  - commit missing: git's "did not contain" or "not our ref" texts, through a new `codebase.utils.is_git_object_missing_text`;
  - auth: `codebase.utils.is_git_auth_error_text`;
  - any other failure, including a failed scoped mint, is a fetch failure.

  Wrap each submodule, each branch hint or pin, and each LFS batch, so one failure never stops the rest. Make `complete` catch anything left with `logger.exception`. Log messages carry no environment and no URL user info.
- [ ] T093 [US4] In daiv/automation/agent/context.py, wrap the per-handle completion of T022 so an exception from `complete_checkout` is logged with `logger.exception` and leaves `CheckoutReport.EMPTY` on that handle. Note in a one-line comment why continuing is safe: the clone itself is intact (errors rule 2).

**Checkpoint**: all four stories work. Quickstart scenario 16 passes.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [ ] T094 [P] Document the feature in docs/customization/repository-config.md:
  - a `checkout` row group (keys, types, defaults and behaviour from contracts/repository-config.md), and a `checkout` block in the full example;
  - a section on what DAIV fetches and with whose access: only repositories DAIV works on, and the member-only rule (SR1);
  - which submodule changes it publishes and how: who must be able to write, innermost first, the parent recording the pushed commit, the merge-first note, and what happens after a squash or rebase merge;
  - on GitLab, that each submodule's project needs DAIV's webhook, as `setup_webhooks` installs for every catalogued project;
  - what it does not publish: LFS-tracked files, read-only submodules, adding or removing a submodule.

  `test_docs_reference.py` must pass.
- [ ] T095 [P] Update the other pages:
  - docs/features/sessions.md: submodule merge requests on the session page; a session that fetched a submodule is visible only to its readers and adds no memory; the **Submodule Merge** row in the session origins table;
  - docs/features/jobs-api.md: `merge_requests` and `publish_errors` include submodule repositories;
  - docs/reference/env-variables.md: `CODEBASE_LFS_MAX_CLONE_BYTES` with its default and meaning, so `test_env_variables_doc.py` passes;
  - docs/getting-started/deployment.md: DAIV's image ships `git-lfs`, and a custom image needs the `git-lfs` package for `checkout.lfs`.
- [ ] T096 Add one line under **Unreleased** → **Added** in CHANGELOG.md, linking the implementation pull request, e.g. "Check out a repository's submodules and Git LFS content when its `.daiv.yml` turns them on, and publish changes inside submodules as their own merge requests ([#NNNN](…))".
- [ ] T097 Run `make lint-fix`, `make lint`, `make lint-typing`, `make test` and `uv run --only-group=docs mkdocs build --clean --strict`, and fix what they report. Record each command and its result for the pull request (tests rule 5). Then write the implementation pull request's body:
  - the constitution check evidence of plan.md;
  - the commands and results, and those of T098;
  - Principle V's statement that the prompt, notice and post-merge prompt changes are unmeasured, because no eval case opts in, so no claim about agent quality is made (agent-evals rule 2).
- [ ] T098 Run the quickstart scenarios of specs/006-submodules-and-lfs/quickstart.md on the local stack, both platforms where available. Report any scenario not run as unverified.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)**: T001 to T003 are done; T004 is the baseline before any change.
- **Foundational (Phase 2)**: depends on Setup and blocks every story.
- **US1 (Phase 3)**: depends on Phase 2.
- **US2 (Phase 4)**: depends on US1, whose checkout it extends.
- **US3 (Phase 5)**: depends only on Phase 2. It edits daiv/codebase/clients/checkout.py and daiv/automation/agent/workspace/git_manager.py like US1 and US2, so it runs after them. Its last T082 case needs US2.
- **US4 (Phase 6)**: depends on US1, US2 and US3, whose checkout paths it hardens.
- **Polish (Phase 7)**: depends on the stories it documents; T094 and T095 can start once Phase 2 fixes the settings' names.

### Within each phase

- Tests first; they must fail before the implementation tasks start.
- Phase 2:
  - T017 before T018, and T018 before T019 and T020;
  - T015 to T018 before T022, and T021 before T023;
  - T024 before T025, and T025 before T026.
- US1: T034 before T035.
- US2:
  - T057 and T058 before T060, and T059 before T071 and T076;
  - T060 before T064, T065 and T071; T061 and T062 before T065; T063 before T064;
  - T065 and T066 before T067 and T068;
  - T069 before T070 to T077, and T070 before T071; T075 and T076 before T077.
- US3: T085 before T086.

### Parallel opportunities

- **Phase 2:** tests T005–T014 are all [P]. Implementation tasks T015, T016, T017, T020 and T021 are [P].
- **US1:** T027, T031, T032 and T033 are [P], one per test file. T028–T030 share T027's file.
- **US2:**
  - tests T037, T039–T041 and T043–T056 are [P]. T038 shares T037's file, and T042 shares T041's;
  - implementation tasks T059, T072, T073 and T074 are [P].
- **US3:** T078 and T080–T082 are [P], and T079 shares T078's file. Implementation tasks T083, T084 and T087 are [P].
- **US4:** T089 and T091 are [P].
- **Docs:** T094 and T095 are [P].

---

## Parallel Example: User Story 2

```bash
# The US2 tests, one per file, together; T038 follows T037 in test_checkout.py, T042 follows T041:
T037 test_checkout.py — verdict and target (then T038 branch hints and pins)
T039 gitlab/github test_client.py — merged commit, ancestry
T040 test_context.py — submodule handles, session-wide verdicts
T041 test_git_manager.py — record (then T042 remote moved a pointer)
T043 test_publishers.py — apublish_submodules
T044 test_sibling_links.py — groups per viewer
T045 middlewares/test_git.py — order, turn-end state, prompt, notice, links
T046 test_sandbox.py, test_session.py — credential added before a submodule's publish
T047–T056 sessions, memory, inbox, webhooks
```

---

## Implementation Strategy

### MVP first (US1)

1. Phase 1, then Phase 2: nothing changes for any repository yet.
2. Phase 3: read-only submodules for repositories that opt in.
3. Stop and check US1's unit tests before starting US2. The quickstart runs after US2, since its scenarios expect the
   published and read-only lists.

### Incremental delivery

1. Foundation + US1 → submodules read-only, safe publishing, the agent told.
2. + US2 → submodule changes published as their own merge requests, linked, repaired after squash or rebase merges;
   sessions hidden from non-readers of their submodules.
3. + US3 → LFS content, clean sandboxes, LFS kept out of publishes.
4. + US4 → failures degrade, and operator logs.
5. Polish → docs, changelog, gates, quickstart, PR statement.

One pull request carries all of it, as the spec describes one feature: the reporter needs US2 before DAIV can replace
their stopgap, so no read-only version ships on its own. The phases are the order of work and review.
