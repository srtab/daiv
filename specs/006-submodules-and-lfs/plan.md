# Implementation Plan: Submodules and Git LFS

**Branch**: `claude/magical-hypatia-atqoxm` | **Date**: 2026-11-27 | **Spec**: [spec.md](spec.md) | **Status**: Revised on 2026-11-27 for the widened scope: publishing changes inside submodules (US2, FR-019 to FR-026) and session visibility and memory (FR-027). **The revision awaits the maintainer's approval.** The approval recorded the same day, including spec refinement [SR1](research.md#r4-whose-access-decides-fr-005), and the `/speckit-analyze` fixes C1, H1 and M1–M4, cover the read-only parts, which are unchanged

**Input**: Feature specification from `specs/006-submodules-and-lfs/spec.md`

## Summary

A repository opts in through `.daiv.yml` (`checkout.submodules`, `checkout.lfs`). DAIV then completes each clone
after the main checkout. It publishes the changes inside submodules the person can write as merge requests in their
own repositories, with the parent pointing at them, and keeps everything else it cannot publish out of the publish.

1. **Completing the clone (US1, US3).**
   - **Where:** a new `RepoCheckout` in `codebase.clients` runs after `load_repo` for the main and each extra
     repository, and returns a `CheckoutReport` ([R1](research.md#r1-the-checkout-completes-in-codebaseclients-after-the-clone)).
   - **Submodules:** checked out one at a time and recursively, each only after three checks: its URL resolves to a
     repository on the platform's host ([R2](research.md#r2-which-submodule-urls-daiv-follows)), DAIV's account can
     read it, and the run's person can read it per DAIV's repository access mirror
     ([R4](research.md#r4-whose-access-decides-fr-005)). Each is fetched with a credential minted for it alone, never
     beside the main token ([R3](research.md#r3-each-submodule-is-fetched-alone-with-its-own-credential)).
   - **LFS:** pointers are read with git. Files are selected in path order under `CODEBASE_LFS_MAX_CLONE_BYTES`
     (256 MiB by default) and fetched with `git lfs pull` from the platform's own LFS endpoint
     ([R5](research.md#r5-which-lfs-files-are-fetched-and-how)).
   - **Clean status everywhere:** minimal stat checking, back-dated LFS files and an index refreshed on the worker
     keep the sandbox's git, which has no git-lfs, from seeing LFS files as changed. The sandbox copy leaves out git's
     LFS object store ([R6](research.md#r6-a-clean-git-status-after-the-copy-into-the-sandbox-fr-011),
     [R7](research.md#r7-the-sandbox-copy-carries-lfs-content-once-fr-015)).
2. **Publishing submodules (US2).**
   - **Which:** the checkout settles, per submodule, whether DAIV publishes it. It does when the person can write the
     repository, DAIV's catalog holds it, its parent is published, and no other checkout of the session publishes that
     repository ([R14](research.md#r14-which-submodules-daiv-publishes-fr-019-fr-024)).
   - **How:** a published submodule is a `RepoHandle` of its parent at its own workspace path, never an extra
     repository ([R15](research.md#r15-a-submodule-is-a-handle-of-its-parent-not-an-extra-repository)). Before each
     repository's publish, its submodules publish deepest first through the existing publisher. The parent then
     records each one at the commit DAIV pushed, with `update-index --cacheinfo`
     ([R16](research.md#r16-publish-order-and-the-recorded-commit-fr-021)). The merge request targets the branch
     `.gitmodules` names, else the default branch ([R17](research.md#r17-the-branch-and-target-of-a-submodules-merge-request-fr-020)).
   - **Credentials:** in a sandbox, each submodule's credential is added to the egress just before its publish
     ([R18](research.md#r18-the-credential-for-a-submodules-publish-in-a-sandbox-fr-004)).
   - **Later turns:** they check out the submodule's working branch when it still contains the recorded commit
     ([R19](research.md#r19-later-turns-continue-on-the-submodules-branch-fr-023)). A remote that moved the same
     pointer wins the rebase ([R20](research.md#r20-a-remote-that-moved-a-pointer-daiv-also-moves-fr-014)).
   - **Links:** the merge requests link each other, and the parent's says which merge first
     ([R23](research.md#r23-linking-the-merge-requests-fr-022)).
3. **After a submodule's merge request merges (US2).** The merge callback hands sessions that hold it to a task. The
   task asks the platform for the merged commit and whether it contains the commit DAIV pushed. If it doesn't, the
   task wakes the session with a new `submodule_merged` inbox event, whose run records the merged commit in the
   parent's open merge request, or in a new one ([R21](research.md#r21-pointing-the-parent-at-the-merged-commit-fr-025)).
4. **Session state (US2, FR-027).** A new `SessionSubmodule` table holds each fetched submodule repository, its path,
   and once published its branch and merge request. Visibility, the memory skip, the merge lookup, the session page
   and the inbox's access check read it. The `SessionRepository` readers, which assume extra repositories, are left
   alone ([R22](research.md#r22-session-state-for-submodules-fr-023-fr-026-fr-027)).
5. **Keeping changes out of the publish (US1, US3).** `GitManager` takes a `PublishScope`.
   - **Status and diffs:** they ignore submodules and LFS paths, except the pointers the scope records.
   - **Commit:** `commit_all` unstages gitlinks and LFS paths after `git add -A`, then records the scope's pointers.
   - **Report:** what was kept out comes back as `KeptOut` and ends the turn as a notice
     ([R8](research.md#r8-the-publish-keeps-submodules-and-lfs-paths-out-fr-012-to-fr-014)).
6. **Telling the agent (US4).** The git system prompt gains a section per opted-in repository: what was checked out,
   which submodules publish and which are read-only and why, what was left out and why
   ([R9](research.md#r9-what-the-agent-is-told-fr-009), [contracts/agent-context.md](contracts/agent-context.md)).
   DAIV-side faults log at ERROR ([R10](research.md#r10-logging-fr-010)).

## Technical Context

**Language/Version**: Python 3.14.

**Primary Dependencies**:

- **git**: GitPython for the clone, and git subprocesses for the checkout, as `LocalGitRunner` does today.
- **git-lfs**: Debian bookworm's package (3.3), new in both app images; no Python dependency is added.
- **pydantic and pydantic-settings**: the `.daiv.yml` section and the operator setting.
- **python-gitlab and PyGithub**: two new client calls each, for the merged commit and for ancestry (R21).

**Storage**: PostgreSQL. One new table, `SessionSubmodule`, and one new `EventKind` value, whose check constraint
needs a migration ([data-model.md](data-model.md#database)). No existing column changes.

**Testing**: pytest and pytest-django; real git on temporary local repositories, as in `test_git_manager.py`.

- **Platform:** git-platform clients are stubbed. The stub resolves platform paths to local bare repositories, and
  `RepoCheckout` takes its allowed protocol as an argument, so tests can allow `file`.
- **LFS:** `git lfs pull` is stubbed at `RepoCheckout`'s command boundary. Stat and index behaviour runs on real git,
  with a test-only clean filter ([R12](research.md#r12-testing-approach)).
- **Publishing submodules:** real git with local bare remotes for the superproject and each submodule, so pushes, the
  recorded gitlink and R20's conflict resolution run for real. Merge request calls are stubbed.
- **Network:** no test makes a network call.

**Target Platform**: Linux containers. The worker clones and completes the checkout, and the sandbox runs whatever
base image the environment names.

**Project Type**: a Django web service with background workers that run the agent.

**Performance Goals**: no cost for repositories that leave both settings off (SC-005). For opted-in repositories:

- **Clone:** one full clone per submodule, plus one repository lookup and one credential mint per submodule, and LFS
  downloads bounded by the cap.
- **Publish:** one publish per changed submodule, including its metadata model call, and one credential mint for it
  in a sandbox.
- **Merges:** two platform calls per merged submodule merge request. A run follows only when the parent needs a new
  pointer.

**Constraints**:

- **Fail closed:** only the platform's host, only HTTPS, only repositories the person and DAIV can read, and one
  credential per repository (FR-003 to FR-005).
- **Publishing:** only where the person can write and DAIV's catalog holds the repository (FR-019).
- **Never fail the run** over a submodule or an LFS file (FR-008). A submodule's failed publish never stops its
  parent's (FR-021).
- **Identical behaviour** on disk and sandbox runs, and on GitLab and GitHub.
- **No git-lfs needed** in sandbox images.

**Scale/Scope**:

- **Apps:**
  - `codebase`: settings, `.daiv.yml`, checkout, authorization, `with_git_auth`, and two client methods;
  - `automation`: context, workspace seed and egress, git manager, publisher, git middleware, sibling links;
  - `sessions`: the `SessionSubmodule` model, the executor's access predicate and hints, persistence, visibility,
    page state, the inbox kind and the merge task;
  - `webhooks`: the merge callbacks;
  - `memory`: the skip.
- **Elsewhere:** two Dockerfiles and four docs pages. No new page or endpoint.

## Constitution Check

*GATE: checked before Phase 0 and again after Phase 1 design. Both pass. The widened scope's decisions that need the
maintainer's approval are listed below.*

| Principle | How the plan complies | Evidence at review |
|---|---|---|
| I. Secure and isolated | Submodules come only from the platform's host over HTTPS (`GIT_ALLOW_PROTOCOL`), and only after DAIV's and the person's access is checked. Each is fetched with its own minted credential, and the main token is never in its environment. LFS comes only from the platform's endpoint, overriding `.lfsconfig`. No repository-supplied command runs on the worker: hooks are off, `--checkout` overrides `update` modes, and filters are DAIV's own. A submodule is published only where the person can write and DAIV's catalog holds the repository. Its credential reaches the sandbox only for its publish, scoped to its path (R18). A session that fetched a submodule is hidden from people who can't read it, and feeds no memory (FR-027) | Tests: an SSH URL on another host, a `file://` URL and an `ext::` URL are not fetched. The submodule fetch's environment holds only its own token. A person without access gets an empty directory. A person without write access gets a read-only submodule. `.lfsconfig` cannot redirect LFS. The egress list gains the submodule's rule only at its publish. A non-reader can't list or open the session |
| II. Architecture boundaries | The checkout is a class in `codebase.clients`; `automation` calls it. `sessions` builds the access predicate from `codebase.authorization` and owns `SessionSubmodule`, the merge task and the inbox kind. `webhooks` calls `sessions`, and `memory` asks `Session`. Every import points down, with no new `ignore_imports`. `GitManager` and the publisher serve both run modes, so no code branches on run mode. Submodule handles are a separate collection, so `SessionRepository` and `extra_repos` keep their meaning (R15, R22). The merge follow-up uses the inbox, the existing way a session acts on its own (R21) | `make lint-imports`; review |
| III. Platform and provider neutrality | One implementation on `RepoClient` uses `get_repository`, `get_git_auth_env` and the new `with_git_auth`, `get_merged_commit` and `is_ancestor`, which both clients implement. GitLab keeps its token propagation retry inside its override. No model or provider is chosen by the feature | Paired GitLab and GitHub client tests for each new method; quickstart scenario 17 |
| IV. Tests prove behaviour | Failing-first tests for every FR in the mirror test files (Project Structure). Reproduced first: the two verified failures (the LFS binary committed in the sandbox, the empty commit from a submodule edit), a parent recording a commit before its child is pushed, and a session visible to a non-reader of its submodule | `make test` output in the pull request |
| V. Measured agent behaviour | The new prompt section, the turn-end notice and the `submodule_merged` prompt render only for opted-in repositories or sessions that published a submodule, which no eval case has, so the suites see no change. The pull request declares the change unmeasured and claims no improvement (R9) | The PR's statement |
| VI. Honest failure handling | Unfetchable items degrade the run and are named to the agent (FR-008, FR-009). Kept-out changes and failed submodule publishes are named in the reply, never dropped (FR-013, FR-021, SC-004). The post-merge run says what it did, including doing nothing (FR-025). DAIV-side faults log at ERROR without secrets; expected skips at INFO (R10) | Tests on the reasons, the notice, the post-merge outcomes and the log levels |
| VII. Documentation | Changes to `docs/customization/repository-config.md`: the `checkout` keys, what DAIV fetches and with whose access, which submodule changes it publishes and how, the merge order and what happens after a submodule merges, and what it does not publish. Also `docs/features/sessions.md` (submodule merge requests on the session page, visibility), `docs/features/jobs-api.md` (`merge_requests` and `publish_errors` include submodules), `docs/reference/env-variables.md` (the cap) and `docs/getting-started/deployment.md` (git-lfs in custom images). One `CHANGELOG.md` `Added` line | `mkdocs build --clean --strict`; `test_docs_reference.py`, `test_env_variables_doc.py`, `test_docs_style.py` |

**Gates**: `make lint`, `make lint-typing`, `make test`, a docs build and the changelog line. No eval gate
(Principle V row).

**For the maintainer's eye**:

- **SR1 (approved):** DAIV's access mirror lists members only, so a public or internal submodule repository is
  fetched only for its members ([R4](research.md#r4-whose-access-decides-fr-005)).
- **Default cap:** 256 MiB, because the sandbox seed archive is built in the worker's memory
  ([R5](research.md#r5-which-lfs-files-are-fetched-and-how)).
- **`core.checkStat=minimal`:** set only in repositories where LFS content was fetched. Git then trusts whole-second
  mtime and size for every file in them, not only LFS files ([R6](research.md#r6-a-clean-git-status-after-the-copy-into-the-sandbox-fr-011)).
- **Needs approval: no separate publish switch.** `checkout.submodules: true` also publishes changes inside
  submodules the person can write. A repository that wants read-only submodules leaves its people without write
  access to them (spec Assumptions, [R14](research.md#r14-which-submodules-daiv-publishes-fr-019-fr-024)). The
  alternative is a second key, for example `checkout.publish_submodules`.
- **Needs approval: DAIV's catalog decides where it publishes.** A submodule outside the catalog (on GitLab, a project
  where DAIV's account is not Maintainer) is read-only, and the agent is told so up front
  ([R14](research.md#r14-which-submodules-daiv-publishes-fr-019-fr-024)).
- **Needs approval: the post-merge repair runs the agent.** A `submodule_merged` run has the harness move the pointer
  and the model report it, costing one model call per repair. It runs only after a squash or rebase merge (or a
  merge-commit merge whose result doesn't contain DAIV's commit). DAIV does not block the parent's merge, and does not
  mark the parent's merge request draft
  ([R21](research.md#r21-pointing-the-parent-at-the-merged-commit-fr-025)).
- **Needs approval: a separate table.** `SessionSubmodule` rather than more `SessionRepository` rows, so no reader of
  those rows needs a filter ([R22](research.md#r22-session-state-for-submodules-fr-023-fr-026-fr-027)).
- **Needs approval: who may continue a session.** The inbox drain asks a non-mention requester for read access to the
  session's submodule repositories, as it asks for write access to extra repositories. A mention on a merge request
  stays exempt, as today for extra repositories. Its run fetches only what its author can read (FR-005), but the
  session's earlier turns are still in the thread. That residual gap exists today for extra repositories, and this
  plan neither widens it beyond submodules nor closes it
  ([R22](research.md#r22-session-state-for-submodules-fr-023-fr-026-fr-027)).

**Post-design re-check (after Phase 1)**: unchanged. The data model adds one table and one inbox kind, with no import
across layers. The contracts expose nothing a person couldn't already read in `.gitmodules` or on the platform.

## Project Structure

### Documentation (this feature)

```text
specs/006-submodules-and-lfs/
├── spec.md
├── plan.md                    # this file
├── research.md                # Phase 0: R1–R23, SR1
├── data-model.md              # Phase 1: settings, run-scoped types, SessionSubmodule, the inbox kind
├── quickstart.md              # Phase 1: validation on the local stack
├── contracts/
│   ├── repository-config.md   # .daiv.yml checkout keys, the operator cap, the image
│   └── agent-context.md       # the prompt section, the turn-end notice, the post-merge prompt, the MR links
├── checklists/requirements.md
└── tasks.md                   # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
daiv/
├── codebase/
│   ├── conf.py                         # + LFS_MAX_CLONE_BYTES
│   ├── repo_config.py                  # + Checkout section, RepositoryConfig.checkout
│   ├── authorization.py                # + platform_account_can_view, platform_account_can_write, is_catalogued
│   └── clients/
│       ├── base.py                     # + complete_checkout, with_git_auth, get_merged_commit, is_ancestor; GIT_INHERITED_ENV
│       ├── checkout.py                 # new: CheckoutOptions, RunRepoAccess, DENY_ALL, SkipReason, PublishBlock,
│       │                               #      SubmoduleCheckout, LfsFile, CheckoutReport, RepoCheckout
│       ├── gitlab/client.py            # with_git_auth (propagation retry); GIT_LFS_SKIP_SMUDGE; get_merged_commit, is_ancestor
│       └── github/client.py            # with_git_auth; GIT_LFS_SKIP_SMUDGE; get_merged_commit, is_ancestor
├── automation/agent/
│   ├── context.py                      # completes each clone; RepoHandle.checkout, workspace_path, submodules, submodule
│   ├── publishers.py                   # PublishScope.record; apublish_submodules; run_base_branch for submodules
│   ├── git_state.py                    # RepoPublish.kept_out, submodule_path, parent_repo_id
│   ├── sibling_links.py                # RelatedMergeRequest.relation; grouped block
│   ├── middlewares/git.py              # prompt section; submodules before each repository's publish; notice; links
│   └── workspace/
│       ├── git_manager.py              # PublishScope, KeptOut; scope in status_snapshot, commit_all, fast_forward,
│       │                               #   _integrate_remote (R20)
│       ├── git_runners.py              # imports GIT_INHERITED_ENV from codebase.clients.base
│       ├── disk.py, sandbox.py         # for_repo at handle.workspace_path; fast_forward passes the scope
│       └── session.py                  # seed archives leave out <gitdir>/lfs; aadd_repository (R18)
├── sessions/
│   ├── models.py                       # + SessionSubmodule; EventKind.SUBMODULE_MERGED (+ migrations)
│   ├── managers.py                     # visibility and on_merge_request read SessionSubmodule
│   ├── multi_repo.py                   # apersist_submodule_state; Session.aholds_other_repositories
│   ├── page_state.py                   # submodule repository cards
│   ├── submodule_merges.py             # new: SubmoduleMerges, settle_submodule_merge_task
│   ├── executor/run.py                 # RunRepoAccess; rows written after checkout; branch and pin hints
│   ├── executor/recovery.py            # submodules before each repository's recovered publish
│   └── inbox/                          # kinds.py payload and triggers; render.py prompt; drain.py access check
├── webhooks/gitlab/callbacks.py        # merge → SubmoduleMerges before MergeArchive
├── webhooks/github/callbacks.py        # same
└── memory/tasks.py                     # skip asks aholds_other_repositories

docker/production/app/Dockerfile        # + git-lfs
docker/local/app/Dockerfile             # + git-lfs

docs/customization/repository-config.md
docs/features/sessions.md
docs/features/jobs-api.md
docs/reference/env-variables.md
docs/getting-started/deployment.md
CHANGELOG.md

tests/unit_tests/
├── codebase/
│   ├── test_repo_config.py             # checkout defaults and parsing
│   ├── test_authorization.py           # platform_account_can_view, platform_account_can_write, is_catalogued
│   └── clients/
│       ├── test_checkout.py            # new: URL resolution, access, credentials, recursion, publish verdicts,
│       │                               #      branch hints, pins, LFS selection, stat
│       ├── test_base.py                # complete_checkout and with_git_auth defaults
│       ├── gitlab/test_client.py       # with_git_auth; clone skips smudge; get_merged_commit, is_ancestor
│       └── github/test_client.py       # same
├── automation/agent/
│   ├── test_context.py                 # completion; submodule handles and workspace paths
│   ├── test_publishers.py              # kept_out; submodules publish first and are recorded; failures
│   ├── test_sibling_links.py           # relation groups
│   ├── middlewares/test_git.py         # prompt section; notice; order of publishes
│   └── workspace/
│       ├── test_git_manager.py         # scope; record; FR-014; R20 conflict resolution
│       ├── test_sandbox.py             # for_repo at a submodule's path; credential added before its publish
│       └── test_session.py             # archives; aadd_repository
├── sessions/
│   ├── test_models.py                  # SessionSubmodule constraints
│   ├── test_managers.py                # visibility; on_merge_request
│   ├── test_multi_repo.py              # apersist_submodule_state; aholds_other_repositories
│   ├── test_auto_archive.py            # a submodule merge is left to the task
│   ├── test_submodule_merges.py        # new: the task's outcomes
│   ├── test_page_state.py              # submodule cards
│   ├── executor/test_run.py            # access predicate; rows written before the agent; hints
│   ├── executor/test_recovery.py       # submodules before each recovered publish
│   └── inbox/                          # test_kinds.py, test_render.py, test_drain.py
├── webhooks/{gitlab,github}/test_callbacks.py  # merges hand submodule sessions to the task
├── memory/test_extraction_task.py      # skip for a session with a submodule row
├── test_docs_reference.py              # (unchanged; fails until the docs list the keys)
└── test_env_variables_doc.py           # (unchanged; fails until the docs list the cap)
```

**Structure Decision**: the feature extends existing apps at their current extension points. The new modules are
`codebase/clients/checkout.py`, beside the clients it serves, and `sessions/submodule_merges.py`, beside the other
merge handling. No new app, so `.importlinter` and `test_import_hygiene.py` need no change.

## Complexity Tracking

No constitution violation needs justifying: no new `ignore_imports`, no exception to a MUST.
