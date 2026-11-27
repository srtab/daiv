# Data Model: Submodules and Git LFS

Phase 1 of [plan.md](plan.md). The feature adds two settings, run-scoped value types, fields on existing dataclasses,
one table (`SessionSubmodule`) and one inbox event kind. The migrations are one for the table and one for the inbox
kind's check constraint.

## Settings

### `.daiv.yml`: `checkout` (in `codebase/repo_config.py`)

| Key | Type | Default | Rule |
|---|---|---|---|
| `checkout.submodules` | `bool` | `false` | Check out the repository's submodules recursively, and publish changes inside those the person can write (FR-001, FR-002, FR-019) |
| `checkout.lfs` | `bool` | `false` | Fetch LFS content for the repository and the submodules DAIV checked out (FR-001, FR-006) |

A new `Checkout(BaseModel)` holds them; `RepositoryConfig.checkout: Checkout = Field(default_factory=Checkout)`. Read
from the default branch, like every key. Nested submodules have no settings of their own: they follow the repository
that started the clone.

### Operator: `CODEBASE_LFS_MAX_CLONE_BYTES` (in `codebase/conf.py`)

| Setting | Type | Default | Rule |
|---|---|---|---|
| `LFS_MAX_CLONE_BYTES` | `int`, `>= 0` | `268435456` (256 MiB) | The LFS content one clone may download, summed over the repository and its submodules. `0` downloads none (FR-007) |

## Run-scoped value types (in `codebase/clients/checkout.py`)

### `CheckoutOptions` (frozen dataclass)

What one clone completes:

| Field | Type | Notes |
|---|---|---|
| `submodules` | `bool` | |
| `lfs` | `bool` | |
| `lfs_max_bytes` | `int` | |
| `session_repo_ids` | `frozenset[str]` | The session's main and extra repositories (R14, FR-024) |
| `branches` | `Mapping[str, str]` | Submodule path → working branch, from the session's `SessionSubmodule` rows (R19) |
| `pins` | `Mapping[str, tuple[str, str]]` | Submodule path → (`pushed_sha`, `merged_sha`) for a `submodule_merged` run (R21) |

`CheckoutOptions.off()` is the default and makes `complete_checkout` return an empty report without running git
(FR-016).

### `RunRepoAccess` (Protocol)

What the run's person and DAIV may do with a repository (FR-005, FR-019; research R4, R14). Built by the run executor;
`DENY_ALL` answers no to everything.

| Method | Answers |
|---|---|
| `can_read(repo_id) -> bool` | The person may read it, as DAIV's repository access records it |
| `can_write(repo_id) -> bool` | The person may write it, as DAIV's repository access records it |
| `daiv_can_publish(repo_id) -> bool` | The repository is in DAIV's `RepositoryCatalog` |

### `SkipReason` (StrEnum)

Why a submodule or LFS file was not fetched. Each value has one agent-facing phrase
([contracts/agent-context.md](contracts/agent-context.md)).

| Value | Applies to | Logged as |
|---|---|---|
| `outside_platform` | submodule | info |
| `daiv_no_access` | submodule | error |
| `person_no_access` | submodule | info |
| `commit_missing` | submodule | info |
| `over_limit` | LFS file | info |
| `fetch_failed` | submodule, LFS file | error |

### `PublishBlock` (StrEnum)

Why DAIV does not publish the changes inside a checked-out submodule (R14). Checked in this order.

| Value | Meaning | Logged as |
|---|---|---|
| `published_elsewhere` | The repository is the session's main or an extra repository, or a submodule at an earlier path | not logged |
| `parent_not_published` | The submodule that contains it is blocked | not logged |
| `daiv_no_write` | The repository is not in DAIV's catalog | info |
| `person_no_write` | The run's person can't write the repository | info |

### `SubmoduleCheckout` (frozen dataclass)

One submodule of a clone.

| Field | Type | Notes |
|---|---|---|
| `path` | `str` | Relative to the top of the clone (`libs/core`, `libs/core/vendor/x` for nested) |
| `url` | `str` | As written in `.gitmodules`, with any user info stripped before the agent sees it |
| `repo_id` | `str \| None` | The platform repository path it resolved to; `None` when outside the platform |
| `repository` | `Repository \| None` | The looked-up repository, for the handle (R15); `None` when skipped before the lookup |
| `commit` | `str` | The commit the parent records |
| `skipped` | `SkipReason \| None` | `None` when checked out |
| `publish_block` | `PublishBlock \| None` | Set only when checked out; `None` means DAIV publishes it |
| `target_branch` | `str` | The merge request's target (R17) |
| `branch` | `str` | The working branch checked out (R19), or `""` when detached |
| `note` | `str` | `""`, or the R19 or R21 note the agent reads |

### `LfsFile` (frozen dataclass)

`path: str` (relative to the top of the clone), `size: int` (from the pointer), `skipped: SkipReason | None`.

### `CheckoutReport` (frozen dataclass)

What one clone fetched and left out: `options: CheckoutOptions`, `submodules: tuple[SubmoduleCheckout, ...]`,
`lfs_files: tuple[LfsFile, ...]`, plus derived `skipped_submodules`, `checked_out_submodules` and `skipped_lfs_files`.
An empty report (both settings off) renders nothing. Built once per clone by `RepoCheckout.complete`, never
persisted: each run clones anew and rebuilds it.

**Validation**: a skipped submodule's own `.gitmodules` is never read, so submodules nested under it are not
listed. An LFS file is listed only for the superproject and for submodules that were checked out. A nested
submodule under a blocked one is blocked with `parent_not_published`.

## Run-scoped value types (in `automation/agent/context.py`)

### `SubmoduleRole` (frozen dataclass)

What makes a `RepoHandle` a submodule's (R15): `path`, `parent_path` (`""` under the top-level repository),
`recorded_commit`, `target_branch`, `publish_block: PublishBlock | None`, `note: str`.

## Publish-scoped value types (in `automation/agent/workspace/git_manager.py`)

### `PublishScope` (frozen dataclass)

| Field | Type | Notes |
|---|---|---|
| `submodules` | `bool` | Keep gitlinks and changes inside submodules out (R8) |
| `lfs` | `bool` | Keep LFS paths out (R8) |
| `record` | `Mapping[str, str]` | Direct child path → commit to record (R16); the only gitlinks a commit may change |

It tells `GitManager.status_snapshot`, `commit_all`, `fast_forward` and `_integrate_remote` what to keep out, what to
record and which conflicts to resolve (R8, R16, R20). `PublishScope()` keeps today's commands byte for byte (FR-016).

### `KeptOut` (frozen dataclass)

What a publish left out (FR-013): `lfs_paths: tuple[str, ...]`, and `submodules: tuple[KeptOutSubmodule, ...]`. Each
`KeptOutSubmodule` holds:

- `path: str`;
- `commit_moved: bool`;
- `removed: bool`: the submodule was deleted or moved;
- `added: bool`: a gitlink the agent created, by adding a submodule or cloning a repository inside the checkout;
- `remote_moved: bool`: R20 kept the remote's commit;
- `block: PublishBlock | None`: why its changes were kept out;
- `changed_files: tuple[str, ...]`, capped at 20, with a `more: int` count.

A submodule DAIV published, and whose parent recorded it, is not in `KeptOut`. `KeptOut` is falsy when empty.

## Changed dataclasses

| Type | Module | Change |
|---|---|---|
| `RepoHandle` | `automation/agent/context.py` | `+ checkout: CheckoutReport` (defaults to the empty report); `+ workspace_path: str`; `+ submodules: tuple[RepoHandle, ...]`; `+ submodule: SubmoduleRole \| None` |
| `RepoStatus` | `automation/agent/workspace/git_manager.py` | `+ kept_out: KeptOut`; `+ remote_head: str \| None` (the working branch's remote head, for R16's record rule) |
| `PublishOutcome` | `automation/agent/publishers.py` | `+ kept_out: KeptOut \| None` |
| `RepoPublish` | `automation/agent/git_state.py` | `+ kept_out: KeptOut \| None`; `+ submodule_path: str` (`""` for an extra repository); `+ parent_repo_id: str`; all three carried through `to_state` |
| `RelatedMergeRequest` | `automation/agent/sibling_links.py` | `+ relation: Literal["sibling", "merges_first", "recorded_by"]` (R23) |

`set_runtime_ctx` gains `access: RunRepoAccess = DENY_ALL`, `submodule_branches` and `submodule_pins` (per top-level
repository, R19 and R21). `RunSpec` is unchanged: the executor reads the run's person from its `Run` row by
`spec.run_id`.

## Database

### `SessionSubmodule` (new, in `sessions/models.py`)

A submodule repository DAIV fetched in a session (R22).

| Field | Type | Notes |
|---|---|---|
| `session` | FK `Session`, `related_name="submodules"`, cascade | |
| `parent_repo_id` | `CharField(255)` | The session repository whose checkout holds it |
| `path` | `TextField` | From that repository's root |
| `repo_id` | `CharField(255)` | |
| `ref` | `CharField(255)`, blank | Working branch; blank until published, and after its branch is gone |
| `pushed_sha` | `CharField(64)`, blank | The last commit DAIV pushed to `ref` |
| `merge_request_iid` | `PositiveIntegerField`, null | |
| `merge_request_web_url` | `URLField(500)`, blank | |
| `merge_request_title` | `TextField`, blank | |
| `merge_request_draft` | `BooleanField`, default `False` | |
| `diff_stats` | `JSONField`, null | As on `SessionRepository` |
| `created`, `modified` | timestamps | |

**Constraints**: unique `(session, repo_id)`; index `(repo_id, merge_request_iid)`. A row is never written for a
repository that is the session's main or an extra repository.

### `EventKind.SUBMODULE_MERGED` (new value, in `sessions/models.py`)

A waking inbox event DAIV posts on its own (R21). Its payload (`SubmoduleMergedPayload` in `sessions/inbox/kinds.py`)
is `path`, `parent_repo_id`, `merge_request_iid`, `pushed_sha`, `merged_sha`. `InboxEvent.repo_id` is the submodule's
repository. The `inbox_event_kind_valid` check constraint needs its migration.

## State transitions

- **Within one clone**: a submodule goes from *declared* (in `.gitmodules`) to exactly one of *checked out* or
  *skipped(reason)*. A checked-out one is *publishable* or *blocked(reason)*. An LFS file goes from *pointer* to
  *fetched* or *skipped(reason)*.
- **Within one publish**: each changed path goes into the commit or into `KeptOut`. Each publishable submodule ends
  *published*, *unchanged*, or *failed*. Its parent records the commit only when it was published, or when it was
  unchanged and its `HEAD` is its remote branch's head.
- **`SessionSubmodule`**:
  1. *read*: created at the run's checkout.
  2. *published*: has a `ref`, a `pushed_sha` and a merge request.
  3. After the merge request merges, one of two things:
     - *repointed*: R21's run recorded the merged commit, and the row's `ref` and merge request are cleared;
     - *settled*: the pushed commit was on the target branch, so the row is left as is.

  A row whose branch is gone also has its `ref` and merge request cleared at turn end. It goes back to *published*
  on its next publish.
