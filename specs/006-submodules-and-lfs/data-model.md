# Data Model: Submodules and Git LFS

Phase 1 of [plan.md](plan.md). The feature adds **no database table, column or migration**. It adds two settings, a
few in-memory value types that live for one run, and fields on three existing dataclasses.

## Settings

### `.daiv.yml`: `checkout` (in `codebase/repo_config.py`)

| Key | Type | Default | Rule |
|---|---|---|---|
| `checkout.submodules` | `bool` | `false` | Check out the repository's submodules recursively (FR-001, FR-002) |
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

What one clone completes: `submodules: bool`, `lfs: bool`, `lfs_max_bytes: int`. Built from the repository's
`RepositoryConfig.checkout` and the operator setting. `CheckoutOptions.off()` is the default and makes
`complete_checkout` return an empty report without running git (FR-016).

### `RepoReadAccess` (Protocol)

`__call__(repo_id: str) -> bool`: whether the run's person may read a repository (FR-005, research R4). Built by the
run executor; `DENY_ALL` is the default when no person is known.

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

### `SubmoduleCheckout` (frozen dataclass)

One submodule of a clone.

| Field | Type | Notes |
|---|---|---|
| `path` | `str` | Relative to the top of the clone (`libs/core`, `libs/core/vendor/x` for nested) |
| `url` | `str` | As written in `.gitmodules`, with any user info stripped before the agent sees it |
| `repo_id` | `str \| None` | The platform repository path it resolved to; `None` when outside the platform |
| `commit` | `str` | The commit the parent records |
| `skipped` | `SkipReason \| None` | `None` when checked out |

### `LfsFile` (frozen dataclass)

`path: str` (relative to the top of the clone), `size: int` (from the pointer), `skipped: SkipReason | None`.

### `CheckoutReport` (frozen dataclass)

What one clone fetched and left out: `options: CheckoutOptions`, `submodules: tuple[SubmoduleCheckout, ...]`,
`lfs_files: tuple[LfsFile, ...]`, plus derived `skipped_submodules` and `skipped_lfs_files`. An empty report (both
settings off) renders nothing. Built once per clone by `RepoCheckout.complete`, never persisted: each run clones anew
and rebuilds it.

**Validation**: a skipped submodule's own `.gitmodules` is never read, so submodules nested under it are not
listed. An LFS file is listed only for the superproject and for submodules that were checked out.

## Publish-scoped value types (in `automation/agent/workspace/git_manager.py`)

### `PublishScope` (frozen dataclass)

`submodules: bool`, `lfs: bool`, from the repository's `checkout` settings. It tells `GitManager.status_snapshot` and
`commit_all` what to keep out (research R8). `PublishScope()` (both `False`) keeps today's commands byte for byte
(FR-016).

### `KeptOut` (frozen dataclass)

What a publish left out (FR-013): `lfs_paths: tuple[str, ...]`, and `submodules: tuple[KeptOutSubmodule, ...]`. Each
`KeptOutSubmodule` holds `path: str`, `commit_moved: bool` and `changed_files: tuple[str, ...]`, capped at 20, with a
`more: int` count. Falsy when empty.

## Changed dataclasses

| Type | Module | Change |
|---|---|---|
| `RepoHandle` | `automation/agent/context.py` | `+ checkout: CheckoutReport` (defaults to the empty report) |
| `RepoStatus` | `automation/agent/workspace/git_manager.py` | `+ kept_out: KeptOut` |
| `PublishOutcome` | `automation/agent/publishers.py` | `+ kept_out: KeptOut \| None` |
| `RepoPublish` | `automation/agent/git_state.py` | `+ kept_out: KeptOut \| None`, carried through `to_state` so the turn-end notice can render it |

`set_runtime_ctx` gains `read_access: RepoReadAccess = DENY_ALL`. `RunSpec` is unchanged: the executor reads the run's
person from its `Run` row by `spec.run_id`.

## State transitions

None persist. Within one clone, a submodule goes from *declared* (in `.gitmodules`) to exactly one of *checked out* or
*skipped(reason)*. An LFS file goes from *pointer* to *fetched* or *skipped(reason)*. A publish moves each changed
path into either the commit or `KeptOut`.
