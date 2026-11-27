# Implementation Plan: Submodules and Git LFS

**Branch**: `claude/magical-hypatia-atqoxm` | **Date**: 2026-11-27 | **Spec**: [spec.md](spec.md) | **Status**: Approved by the maintainer on 2026-11-27, including spec refinement [SR1](research.md#r4-whose-access-decides-fr-005), which the spec now carries (FR-005)

**Input**: Feature specification from `specs/006-submodules-and-lfs/spec.md`

## Summary

A repository opts in through `.daiv.yml` (`checkout.submodules`, `checkout.lfs`). DAIV then completes each clone
after the main checkout, and keeps whatever it cannot publish out of the publish.

1. **Completing the clone (US1, US2).**
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
2. **Keeping changes out of the publish (US1, US2).** `GitManager` takes a `PublishScope`.
   - **Status and diffs:** they ignore submodules and LFS paths, so they never count as changes.
   - **Commit:** `commit_all` unstages gitlinks and LFS paths after `git add -A`.
   - **Report:** what was kept out comes back as `KeptOut` and ends the turn as a notice
     ([R8](research.md#r8-the-publish-keeps-submodules-and-lfs-paths-out-fr-012-to-fr-014)).
3. **Telling the agent (US3).** The git system prompt gains a section per opted-in repository: what was checked out,
   what was left out and why, and that those changes are not published
   ([R9](research.md#r9-what-the-agent-is-told-fr-009)). DAIV-side faults log at ERROR
   ([R10](research.md#r10-logging-fr-010)).

## Technical Context

**Language/Version**: Python 3.14.

**Primary Dependencies**:

- **git**: GitPython for the clone, and git subprocesses for the checkout, as `LocalGitRunner` does today.
- **git-lfs**: Debian bookworm's package (3.3), new in both app images; no Python dependency is added.
- **pydantic and pydantic-settings**: the `.daiv.yml` section and the operator setting.

**Storage**: none. No model, column or migration ([data-model.md](data-model.md)).

**Testing**: pytest and pytest-django; real git on temporary local repositories, as in `test_git_manager.py`.

- **Platform:** git-platform clients are stubbed. The stub resolves platform paths to local bare repositories, and
  `RepoCheckout` takes its allowed protocol as an argument, so tests can allow `file`.
- **LFS:** `git lfs pull` is stubbed at `RepoCheckout`'s command boundary. Stat and index behaviour runs on real git,
  with a test-only clean filter ([R12](research.md#r12-testing-approach)).
- **Network:** no test makes a network call.

**Target Platform**: Linux containers. The worker clones and completes the checkout, and the sandbox runs whatever
base image the environment names.

**Project Type**: a Django web service with background workers that run the agent.

**Performance Goals**: no cost for repositories that leave both settings off (SC-005). For opted-in repositories, the
clone grows by one full clone per submodule, one repository lookup and credential mint per submodule, and LFS
downloads bounded by the cap.

**Constraints**:

- **Fail closed:** only the platform's host, only HTTPS, only repositories the person and DAIV can read, and one
  credential per repository (FR-003 to FR-005).
- **Never fail the run** over a submodule or an LFS file (FR-008).
- **Identical behaviour** on disk and sandbox runs, and on GitLab and GitHub.
- **No git-lfs needed** in sandbox images.

**Scale/Scope**:

- **Apps:** `codebase` (settings, `.daiv.yml`, checkout, authorization, `with_git_auth`), `automation` (context,
  workspace seed, git manager, publisher, git middleware) and `sessions` (the executor's access predicate).
- **Elsewhere:** two Dockerfiles and three docs pages. No new page or endpoint.

## Constitution Check

*GATE: checked before Phase 0 and again after Phase 1 design. Both pass. One spec refinement needs the maintainer's
approval; it is listed below.*

| Principle | How the plan complies | Evidence at review |
|---|---|---|
| I. Secure and isolated | Submodules come only from the platform's host over HTTPS (`GIT_ALLOW_PROTOCOL`), and only after DAIV's and the person's access is checked. Each is fetched with its own minted credential, and the main token is never in its environment. LFS comes only from the platform's endpoint, overriding `.lfsconfig`. No repository-supplied command runs on the worker: hooks are off, `--checkout` overrides `update` modes, and filters are DAIV's own. The sandbox gets no new credential or egress rule | Tests: an SSH URL on another host, a `file://` URL and an `ext::` URL are not fetched. The submodule fetch's environment holds only its own token. A person without access gets an empty directory. `.lfsconfig` cannot redirect LFS |
| II. Architecture boundaries | The checkout is a class in `codebase.clients`; `automation` calls it, and `sessions` builds the access predicate from `codebase.authorization`. Every import points down, with no new `ignore_imports`. `GitManager` serves both run modes, so no code branches on run mode | `make lint-imports`; review |
| III. Platform and provider neutrality | One implementation on `RepoClient` uses `get_repository`, `get_git_auth_env` and the new `with_git_auth`, which both clients implement. GitLab keeps its token propagation retry inside its override. No model or provider is involved | Paired GitLab and GitHub client tests for `with_git_auth`, and quickstart scenario 11 |
| IV. Tests prove behaviour | Failing-first tests for every FR in the mirror test files (Project Structure). The two verified failures, the LFS binary committed in the sandbox and the empty commit from a submodule edit, are reproduced first | `make test` output in the pull request |
| V. Measured agent behaviour | The new prompt section and turn-end notice render only for opted-in repositories, which no eval case has, so the suites see no change. The pull request declares the change unmeasured and claims no improvement (R9) | The PR's statement |
| VI. Honest failure handling | Unfetchable items degrade the run and are named to the agent (FR-008, FR-009). Kept-out changes are named in the reply, never dropped (FR-013, SC-004). DAIV-side faults log at ERROR without secrets; expected skips at INFO (R10) | Tests on the reasons, the notice and the log levels |
| VII. Documentation | Changes to `docs/customization/repository-config.md`: the `checkout` keys, what DAIV fetches and with whose access, what it does not publish, and how to change a submodule, linking to [Start a chat over several repositories](../../docs/features/sessions.md#start-a-chat-over-several-repositories). Changes to `docs/reference/env-variables.md` (the cap) and `docs/getting-started/deployment.md` (git-lfs in custom images). One `CHANGELOG.md` `Added` line | `mkdocs build --clean --strict`; `test_docs_reference.py`, `test_env_variables_doc.py`, `test_docs_style.py` |

**Gates**: `make lint`, `make lint-typing`, `make test`, a docs build and the changelog line. No eval gate
(Principle V row).

**For the maintainer's eye**:

- **SR1 (needs approval):** DAIV's access mirror lists members only, so a public or internal submodule repository is
  fetched only for its members. FR-005 would say "as DAIV's repository access sees it" ([R4](research.md#r4-whose-access-decides-fr-005)).
- **Default cap:** 256 MiB, because the sandbox seed archive is built in the worker's memory
  ([R5](research.md#r5-which-lfs-files-are-fetched-and-how)).
- **`core.checkStat=minimal`:** set only in repositories where LFS content was fetched. Git then trusts whole-second
  mtime and size for every file in them, not only LFS files ([R6](research.md#r6-a-clean-git-status-after-the-copy-into-the-sandbox-fr-011)).

**Post-design re-check (after Phase 1)**: unchanged. The data model adds no table and no import across layers, and
the contracts expose nothing a person couldn't already read in `.gitmodules`.

## Project Structure

### Documentation (this feature)

```text
specs/006-submodules-and-lfs/
├── spec.md
├── plan.md                    # this file
├── research.md                # Phase 0: R1–R13, SR1
├── data-model.md              # Phase 1: settings and run-scoped types; no database change
├── quickstart.md              # Phase 1: validation on the local stack
├── contracts/
│   ├── repository-config.md   # .daiv.yml checkout keys, the operator cap, the image
│   └── agent-context.md       # the prompt section and the turn-end notice
├── checklists/requirements.md
└── tasks.md                   # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
daiv/
├── codebase/
│   ├── conf.py                         # + LFS_MAX_CLONE_BYTES
│   ├── repo_config.py                  # + Checkout section, RepositoryConfig.checkout
│   ├── authorization.py                # + platform_account_can_view(uid, repo_id)
│   └── clients/
│       ├── base.py                     # + RepoClient.complete_checkout, RepoClient.with_git_auth
│       ├── checkout.py                 # new: CheckoutOptions, RepoReadAccess, DENY_ALL, SkipReason,
│       │                               #      SubmoduleCheckout, LfsFile, CheckoutReport, RepoCheckout
│       ├── gitlab/client.py            # with_git_auth takes the propagation retry from _clone; clone env + GIT_LFS_SKIP_SMUDGE
│       └── github/client.py            # with_git_auth mints per call; clone env + GIT_LFS_SKIP_SMUDGE
├── automation/agent/
│   ├── context.py                      # set_runtime_ctx(read_access=...) completes each clone in a thread; RepoHandle.checkout
│   ├── publishers.py                   # PublishScope from config; PublishOutcome.kept_out
│   ├── git_state.py                    # RepoPublish.kept_out
│   ├── middlewares/git.py              # prompt section per repository; kept-out notice
│   └── workspace/
│       ├── git_manager.py              # PublishScope, KeptOut; status_snapshot and commit_all honour the scope
│       └── session.py                  # seed archives leave out <gitdir>/lfs
└── sessions/executor/run.py            # builds RepoReadAccess from the Run's person

docker/production/app/Dockerfile        # + git-lfs
docker/local/app/Dockerfile             # + git-lfs

docs/customization/repository-config.md
docs/reference/env-variables.md
docs/getting-started/deployment.md
CHANGELOG.md

tests/unit_tests/
├── codebase/
│   ├── test_repo_config.py             # checkout defaults and parsing
│   ├── test_authorization.py           # platform_account_can_view
│   └── clients/
│       ├── test_checkout.py            # new: URL resolution, access, credentials, recursion, LFS selection, stat
│       ├── test_base.py                # complete_checkout and with_git_auth defaults
│       ├── gitlab/test_client.py       # with_git_auth retry and self-heal; clone skips smudge
│       └── github/test_client.py       # with_git_auth; clone skips smudge
├── automation/agent/
│   ├── test_context.py                 # read_access threaded; extra repositories completed by their own config
│   ├── test_publishers.py              # kept_out on outcomes; recovery path
│   ├── middlewares/test_git.py         # prompt section only when opted in; notice
│   └── workspace/
│       ├── test_git_manager.py         # scope on status_snapshot and commit_all; FR-014
│       └── test_session.py             # archives leave out lfs dirs, keep .git/modules
├── sessions/executor/test_run.py       # access predicate per person kind
├── test_docs_reference.py              # (unchanged; fails until the docs list the keys)
└── test_env_variables_doc.py           # (unchanged; fails until the docs list the cap)
```

**Structure Decision**: the feature extends existing apps at their current extension points. The only new module is
`codebase/clients/checkout.py`, beside the clients it serves. No new app, so `.importlinter` and
`test_import_hygiene.py` need no change.

## Complexity Tracking

No constitution violation needs justifying: no new `ignore_imports`, no exception to a MUST.
