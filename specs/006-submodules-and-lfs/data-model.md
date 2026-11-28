# Data Model: Submodules and Git LFS

Phase 1 of [plan.md](plan.md). The feature adds two settings, run-scoped value types and fields on existing
dataclasses. It also adds one table (`SessionSubmodule`), one inbox event kind and one run trigger type. Migrations: the
table, and the check constraints the new kind and trigger type touch (see [Database](#database)).

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
| `branches` | `Mapping[str, str]` | Submodule path → working branch, from the session's `SessionSubmodule` rows (R19) |
| `pins` | `Mapping[str, tuple[str, str]]` | Submodule path → (`recorded_sha`, `merged_sha`) for a `submodule_merged` run (R21) |

`CheckoutOptions.off()` is the default and makes `complete_checkout` return an empty report without running git
(FR-016).

### `RunRepoAccess` (Protocol)

What the run's person may do, and where DAIV works (FR-004, FR-005, FR-019; research R3, R4, R14). Built by the run
executor; `DENY_ALL` answers no to everything.

| Method | Answers |
|---|---|
| `can_read(repo_id) -> bool` | The person may read it, as DAIV's repository access records it |
| `can_write(repo_id) -> bool` | The person may write it, as DAIV's repository access records it |
| `in_catalog(repo_id) -> bool` | The repository is in DAIV's `RepositoryCatalog`; false when the catalog is empty |

### `SkipReason` (StrEnum)

Why a submodule or LFS file was not fetched. Each value has one agent-facing phrase
([contracts/agent-context.md](contracts/agent-context.md)).

| Value | Applies to | Logged as |
|---|---|---|
| `outside_platform` | submodule | info |
| `not_catalogued` | submodule | info |
| `daiv_no_access` | submodule | error |
| `person_no_access` | submodule | info |
| `commit_missing` | submodule | info |
| `over_limit` | LFS file | info |
| `fetch_failed` | submodule, LFS file | error |

### `PublishBlock` (StrEnum)

Why DAIV does not publish the changes inside a checked-out submodule (R14).

| Value | Meaning | Set by | Logged as |
|---|---|---|---|
| `published_elsewhere` | Another checkout in the session publishes the repository | `set_runtime_ctx` | not logged |
| `parent_not_published` | The submodule that contains it is blocked | `set_runtime_ctx` | not logged |
| `person_no_write` | The run's person can't write the repository | `RepoCheckout` | info |

### `SubmoduleCheckout` (frozen dataclass)

One submodule of a clone.

| Field | Type | Notes |
|---|---|---|
| `path` | `str` | Relative to the top of the clone (`libs/core`, `libs/core/vendor/x` for nested) |
| `parent_path` | `str` | The containing submodule's path, `""` under the top |
| `url` | `str` | As written in `.gitmodules`, with any user info stripped before the agent sees it |
| `repo_id` | `str \| None` | The platform repository path it resolved to; `None` when outside the platform |
| `repository` | `Repository \| None` | The looked-up repository, for the handle (R15); `None` when skipped before the lookup |
| `commit` | `str` | The commit the parent records |
| `skipped` | `SkipReason \| None` | `None` when checked out |
| `publish_block` | `PublishBlock \| None` | Only `person_no_write` from the checkout; `set_runtime_ctx` adds the others |
| `target_branch` | `str` | The merge request's target, checked against the submodule's remote branches (R17) |
| `branch` | `str` | The working branch checked out (R19), or `""` when detached |
| `pinned` | `str` | The merged commit its parent records this turn (R21), or `""` |
| `note` | `str` | `""`, or the R19 or R21 note the agent reads |

### `LfsFile` (frozen dataclass)

`path: str` (relative to the top of the clone), `size: int` (from the pointer), `skipped: SkipReason | None`.

### `CheckoutReport` (frozen dataclass)

What one clone fetched and left out: `options: CheckoutOptions`, `submodules: tuple[SubmoduleCheckout, ...]`,
`lfs_files: tuple[LfsFile, ...]`, plus derived `skipped_submodules`, `checked_out_submodules` and `skipped_lfs_files`.
An empty report (both settings off) renders nothing. Built once per clone by `RepoCheckout.complete`, never
persisted: each run clones anew and rebuilds it.

**Validation**: a skipped submodule's own `.gitmodules` is never read, so submodules nested under it are not
listed. An LFS file is listed only for the superproject and for submodules that were checked out.

## Run-scoped value types (in `automation/agent/context.py`)

### `SubmoduleRole` (frozen dataclass)

What makes a `RepoHandle` a submodule's (R15): `path`, `parent_path`, `recorded_commit`, `target_branch`,
`publish_block: PublishBlock | None` (session-wide, R14), `pinned: str`, `note: str`.

## Publish-scoped value types (in `automation/agent/workspace/git_manager.py`)

### `PublishScope` (frozen dataclass)

| Field | Type | Notes |
|---|---|---|
| `submodules` | `bool` | Keep gitlinks and changes inside submodules out (R8) |
| `lfs` | `bool` | Keep LFS paths out (R8) |
| `record` | `Mapping[str, str]` | Direct child path → commit to record (R16); the only gitlinks a commit may change |

It tells `GitManager.status_snapshot`, `commit_all`, `fast_forward` and `push_head_to` (for `_integrate_remote`) what
to keep out, what to record and which conflicts to resolve (R8, R16, R20). `PublishScope()` keeps today's commands
byte for byte (FR-016).

### `KeptOut` (frozen dataclass)

What a publish left out (FR-013): `lfs_paths: tuple[str, ...]`, and `submodules: tuple[KeptOutSubmodule, ...]`. Each
`KeptOutSubmodule` holds:

- `path: str`;
- `commit_moved: bool`;
- `removed: bool`: the submodule was deleted or moved;
- `added: bool`: a gitlink or embedded repository the agent created;
- `remote_moved: bool`: R20 kept the remote's commit;
- `block: PublishBlock | None`: why its changes were kept out;
- `changed_files: tuple[str, ...]`, capped at 20, with a `more: int` count.

A submodule DAIV published, and whose parent recorded it, is not in `KeptOut`. `KeptOut` is falsy when empty.

## Changed dataclasses

| Type | Module | Change |
|---|---|---|
| `RepoHandle` | `automation/agent/context.py` | `+ checkout: CheckoutReport` (defaults to the empty report); `+ workspace_path: str`; `+ submodules: tuple[RepoHandle, ...]`; `+ submodule: SubmoduleRole \| None` |
| `RepoStatus` | `automation/agent/workspace/git_manager.py` | `+ kept_out: KeptOut` |
| `PublishOutcome` | `automation/agent/publishers.py` | `+ kept_out: KeptOut \| None`; `published` stays false when `commit_all` made no commit (R8) |
| `RepoPublish` | `automation/agent/git_state.py` | `+ kept_out: KeptOut \| None`; `+ submodule_path: str` (`""` for an extra repository); `+ root_repo_id: str`; `+ parent_repo_id: str`; `+ recorded_sha: str` (what its parent's successful publish recorded, `""` otherwise; a pinned child gets an entry with `published` false and this set); all carried through `to_state` |
| `RelatedMergeRequest` | `automation/agent/sibling_links.py` | `+ parent_repo_id: str` (R23) |

`set_runtime_ctx` gains `access: RunRepoAccess = DENY_ALL`, and `submodule_branches` and `submodule_pins` per root
(R19, R21). `RunSpec` is unchanged: the executor reads the run's person from its `Run` row by `spec.run_id`, falling
back to its `Session`'s platform account for a run DAIV started on its own (R4).

New client methods (`codebase/clients/base.py`, both platforms): `with_git_auth(repository, operation, *,
scoped_only=False)`, `get_git_egress_credential(repository, *, scoped_only=False)`, `get_merged_commit(repo_id, iid)
-> str | None` and `is_ancestor(repo_id, ancestor, descendant) -> bool`.

## Database

### `SessionSubmodule` (new, in `sessions/models.py`)

A submodule repository DAIV fetched in a session (R22).

| Field | Type | Notes |
|---|---|---|
| `session` | FK `Session`, `related_name="submodules"`, cascade | |
| `root_repo_id` | `CharField(255)` | The session repository whose checkout holds it |
| `parent_repo_id` | `CharField(255)` | The repository whose commit records it: the root, or an outer submodule's |
| `path` | `TextField` | From the root's top |
| `repo_id` | `CharField(255)` | |
| `ref` | `CharField(255)`, blank | Working branch; blank until published, and after its merge request merges or closes |
| `recorded_sha` | `CharField(64)`, blank | The commit its parent records, as DAIV's last successful parent publish set it |
| `merge_pending` | `BooleanField`, default `False` | Its merge request merged and R21's task hasn't settled it |
| `merge_request_iid` | `PositiveIntegerField`, null | |
| `merge_request_web_url` | `URLField(500)`, blank | |
| `merge_request_title` | `TextField`, blank | |
| `merge_request_draft` | `BooleanField`, default `False` | |
| `diff_stats` | `JSONField`, null | As on `SessionRepository` |
| `created`, `modified` | timestamps | |

**Constraints**: unique `(session, repo_id)`; index `(repo_id, merge_request_iid)`. A row is never written for a
repository that is the session's main or an extra repository.

### `EventKind.SUBMODULE_MERGED` and `SessionOrigin.SUBMODULE_MERGE` (new values, in `sessions/models.py`)

- **`EventKind.SUBMODULE_MERGED`**: a waking inbox event DAIV posts on its own (R21). Its payload,
  `SubmoduleMergedPayload` in `sessions/inbox/kinds.py`, holds `path`, `root_repo_id`, `merge_request_iid`,
  `recorded_sha` and `merged_sha`. `InboxEvent.repo_id` is the submodule's repository, and its source key is
  `submodule_merged:<repo_id>!<iid>`.
- **`SessionOrigin.SUBMODULE_MERGE`** ("Submodule Merge"): the trigger type of the run it wakes. It is in
  `daiv_authored()` and `classified()`, and in `_TRIGGERS` for the new kind, so `INBOX_TRIGGERS` includes it.

**Migrations**: one for the table, and one for the constraints these values touch:

- `inbox_event_kind_valid`;
- `session_origin_valid`;
- `run_trigger_type_valid`;
- the `run_one_next_run_per_*` constraints built from `_INBOX_TRIGGER_LITERALS`.

## State transitions

- **Within one clone**: a submodule goes from *declared* (in `.gitmodules`) to exactly one of *checked out* or
  *skipped(reason)*. Once `set_runtime_ctx` has seen every clone, a checked-out one is *publishable* or
  *blocked(reason)*. An LFS file goes from *pointer* to *fetched* or *skipped(reason)*.
- **Within one publish**: each changed path goes into the commit or into `KeptOut`. Each publishable submodule ends
  *published*, *unchanged*, or *failed*. Its parent records a commit only for a *published* child (its pushed commit)
  or a pinned one (the merged commit).
- **`SessionSubmodule`**:
  1. *read*: created at the run's checkout.
  2. *published*: it has a `ref` and a merge request, and `recorded_sha` once its parent recorded it.
  3. *merge pending*: its merge request merged, and the callback set `merge_pending`.
  4. *read* again: R21's task cleared `ref`, the merge request and `merge_pending`, after posting a repair when one
     was needed. A close without merge clears `ref` and the merge request directly.

  The repair run's parent publish sets `recorded_sha` to the merged commit.
