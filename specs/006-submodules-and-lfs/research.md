# Research: Submodules and Git LFS

Phase 0 of [plan.md](plan.md). Each item resolves one design question the spec leaves open. "Verified" marks a
claim checked by experiment on git 2.43 and git-lfs 3.4 while planning; the experiments are summarised where they
matter.

## R1. The checkout completes in `codebase.clients`, after the clone

**Decision**: `RepoClient.load_repo` keeps its signature and still clones only the superproject. A new class,
`RepoCheckout` in `daiv/codebase/clients/checkout.py`, completes a clone: it checks out submodules and fetches LFS
content, and returns a `CheckoutReport`. `RepoClient.complete_checkout(repo, repository, options, access)` builds
it with the client, so both platforms share one implementation. Once `set_runtime_ctx` has cloned the main repository
and each extra repository, it completes every clone with that repository's own `.daiv.yml` settings, each in a worker
thread (`asyncio.to_thread`) and all in parallel. It then stores the report on `RepoHandle.checkout`. A worker thread
is required twice over: the main clone runs on the event loop today, and both the access check (R4, a database read)
and the submodule fetches are blocking.

Both clients' `load_repo` add `GIT_LFS_SKIP_SMUDGE=1` to the clone's environment. A worker image whose system git
config carries an LFS filter would otherwise download LFS content for every repository, which FR-016 forbids. DAIV's
images today have no git-lfs, so the variable changes nothing for them.

**Rationale**: completing a checkout needs the platform client (repository lookup, credential minting), so it sits in
`codebase.clients` (layering rule 5) as one class with one concern (code-organization rule 1). Keeping `load_repo`
unchanged leaves the eval clients (`evals/swe_client.py`, `evals/reviewbench.py`) and their tests untouched, and the
`.daiv.yml` defaults keep the step off for them.

**Alternatives considered**: a `load_repo(..., checkout=...)` parameter, rejected because every implementation
(GitLab, GitHub and both eval clients) would repeat the call. Running the step in `automation`, rejected because
it would call platform APIs from the agent runtime rather than through `codebase.clients`.

## R2. Which submodule URLs DAIV follows

**Decision**: `RepoCheckout` reads `.gitmodules` at the checked-out commit (`git config -f .gitmodules -z
--get-regexp '^submodule\..*\.(path|url)$'`) and resolves each URL to a repository path on the run's platform:

- **Relative** (`../lib.git`, `./x`): resolved against the parent's URL, as git does. The parent is the superproject's
  clone URL, or the resolved HTTPS URL of the submodule that contains it.
- **HTTPS** (`https://host/group/lib.git`): taken as is.
- **SSH** (`git@host:group/lib.git`, `ssh://git@host[:port]/group/lib.git`): turned into the platform repository path
  on `host`.

Only a URL whose host equals the clone URL's host is followed. DAIV rewrites `submodule.<name>.url` in the parent's
`.git/config` to the resolved repository's own `clone_url` (from `get_repository`, R3), so every fetch goes over the same
HTTPS transport as the main clone. The submodule's `origin` then matches its repository's clone URL exactly, which is
what the sandbox's path-scoped egress rule for a submodule publish matches on (R18).
Any other scheme (a local path, `file://`, `ext::`, `git://`) or host is reported as outside the platform and never
contacted (FR-003). As a second barrier, each submodule fetch runs with `GIT_ALLOW_PROTOCOL=https` (or the clone URL's
scheme, when it is `http`).

**Rationale**: resolving the URL to a repository path is what lets DAIV look the repository up, check the person's
access and mint its own credential (R3, R4). Rewriting to HTTPS keeps SSH-style `.gitmodules`, which many teams use,
working without SSH keys on the worker.

**Alternatives considered**: following any HTTPS host, rejected by FR-003: a repository-chosen URL would make the
worker fetch from hosts the operator never approved. An allow-list of extra hosts is left for a later feature (spec
Assumptions).

## R3. Each submodule is fetched alone, with its own credential

**Decision**: DAIV checks out submodules one at a time and recurses itself, rather than running `git submodule
update --recursive`. For each submodule:

1. Check that the repository is in DAIV's `RepositoryCatalog`, strictly: an empty catalog holds nothing. Only there
   can DAIV mint a credential for that repository alone (on GitLab a project access token needs the Maintainer role
   the catalog requires; on GitHub an installation token needs the app installed). Otherwise the submodule is
   `not_catalogued`.
2. Look the repository up with `client.get_repository(path)`. A lookup the platform refuses means DAIV's account
   cannot read it.
3. Check the person's access (R4).
4. In the parent, run `git submodule init -- <path>`, set `submodule.<name>.url` to the resolved HTTPS URL (R2), then
   run `git -c core.hooksPath=/dev/null submodule update --checkout --no-recommend-shallow -- <path>`. It clones the
   submodule with every branch and checks out the recorded commit. `--checkout` overrides any `update` mode a
   repository sets, and `--no-recommend-shallow` ignores a `shallow = true`, which would leave a single-branch shallow
   clone that later steps can't diff or branch from (R17, R19).
5. Recurse into the submodule's own `.gitmodules`.

The environment of step 3 holds only that submodule's credential (`client.get_git_auth_env(sub_repository)`), never the
main repository's. This matters because git's `http.<url>.extraHeader` is multi-valued: with a host-wide key for the
main token and a path key for the submodule token both present, git sends both headers.

Minting goes through a new `RepoClient.with_git_auth(repository, operation, *, scoped_only=False)`. GitLab overrides
it with the clone's existing tolerance for token propagation lag and its stale-token self-heal, which move out of
`GitLabClient._clone` into this method. The main clone then uses the same method. GitHub mints one repository-scoped
installation token per call. A submodule's fetch and publish pass `scoped_only=True`: GitLab then never falls back to
the PAT (`GITLAB_PAT_FALLBACK`), which reaches every project the account can, and a failed mint is a `fetch_failed`
for that submodule (FR-004). The egress credential of R18 follows the same rule.

**Rationale**: FR-004 forbids the main token from reaching another repository, and each recursion level needs its own
access check (FR-005), which `--recursive` cannot do. Moving the propagation retry lets a submodule's freshly minted
GitLab token survive the same lag the main clone already handles.

**Alternatives considered**: one host-wide header holding a token valid for every repository (the PAT), rejected by
FR-004 and security rule 3. `--depth 1` submodule fetches, rejected for now: fetching a commit that is not a branch
tip depends on server settings, and the main clone is full too. Partial clones (`--filter=blob:none`), rejected
because a sandbox would have to fetch missing blobs later, with credentials it does not hold for the submodule.

## R4. Whose access decides (FR-005)

**Decision**: the run executor builds a read predicate (`repo_id -> bool`, `RunRepoAccess.can_read` once R14 adds
write access) from the run's person, as recorded on its `Run` row, and passes it to `set_runtime_ctx`:

- **A DAIV user** (`Run.user`): `codebase.authorization.can_view(user, repo_id)`, the rule behind repository
  visibility in DAIV. Admins pass.
- **A platform account only** (`Run.external_uid`): a new `codebase.authorization.platform_account_can_view(uid,
  repo_id)`, which reads the same fresh `RepositoryAccess` rows.
- **Neither, on a run DAIV started on its own** (a CI fix, a `submodule_merged` repair): such a run's requester is
  the session's owner (`Requester.of_event`), who is no DAIV user when an unlinked platform account started the
  session. The predicate then uses `Session.external_provider` and `Session.external_uid`, as for a platform account
  (FR-005). The run's own fields are left as they are, so attribution and grouping don't change.
- **Neither, or no run id**: the predicate denies everything, so no submodule is fetched.

The person used is the run's attributed one, whatever `acts_as_user` says: a CI identity run checks the pipeline's
triggering account. The access check limits what the run sees; it grants no authority.

**Rationale**: `RepositoryAccess` already mirrors every member of every repository DAIV's account can see, keyed by
platform uid and including accounts with no DAIV login (`codebase/models.py`). So webhook runs and DAIV-user runs are
checked against the same data, with its freshness TTL, and without a platform call per submodule.

**Alternatives considered**: asking the platform per submodule (GitLab member lookup, GitHub collaborator permission),
rejected because it adds an API call per submodule per run and a second definition of read access beside the mirror.
DAIV's own access, rejected by the clarification.

**Spec refinement SR1 (for approval)**: the mirror lists members only. A public or internal repository therefore
counts as readable only for its members, although the platform lets anyone, or any signed-in user, read it. FR-005
should say "may read the submodule's repository as DAIV's repository access sees it" and the docs should state the
member-only rule. Without SR1, DAIV would need each repository's visibility, which `Repository` does not carry today.

## R5. Which LFS files are fetched, and how

**Decision**: after the submodules, `RepoCheckout` lists the LFS files of the superproject and of each fetched
submodule with git alone:

- `git ls-files -z`, piped to `git check-attr -z --stdin filter`, gives the paths whose filter is `lfs`.
- `git cat-file --batch` reads each one's blob, which is parsed as an LFS pointer (version, `oid sha256:`, `size`).
- A blob that is not a pointer is skipped, because it was committed without LFS.

Files are taken in path order, superproject first, while the running total of their pointer sizes stays within
`CODEBASE_LFS_MAX_CLONE_BYTES`. That is a new operator setting in `CodebaseSettings`, defaulting to 256 MiB; `0`
fetches no LFS content anywhere. A file that would pass the cap is reported as over the cap, and later smaller files
can still fit.

The selected files are fetched per repository with `git lfs pull --include=<paths>`, in batches that keep each argv
under 64 KiB. Paths are escaped for git-lfs's gitignore-style patterns, and a path holding a comma (git-lfs's list
separator) is fetched alone with `git lfs smudge`. Each command's environment holds:

- the repository's own credential (R3);
- `lfs.url=<clone_url>/info/lfs`, which overrides any `lfs.url` a repository's `.lfsconfig` sets (FR-006): git config
  outranks `.lfsconfig`;
- `filter.lfs.*` and `GIT_TERMINAL_PROMPT=0`.

Object downloads follow the URLs the platform's LFS API returns, which may point at the platform's object storage on
another host. That host receives no git credential, because the header is keyed to the platform host.

**Rationale**: reading pointers with git gives exact sizes and works with the git-lfs 3.3 that Debian bookworm ships,
which predates `git lfs ls-files --json`. Greedy selection in path order is deterministic, so two runs on one commit
fetch the same files.

**Alternatives considered**: all-or-nothing, rejected because one large asset would cost every other file. Per-file
`git lfs smudge` for everything, rejected because it makes one LFS API round trip per file. A `.daiv.yml` include
list, left out: the spec asks for an on/off setting, and the cap bounds the cost.

**Cap default**: 256 MiB. The sandbox seed archive is built in memory on the worker
(`workspace/session.py::_make_repo_archive`), so the cap also bounds the memory a seed adds. Ordinary fixture and
asset sets fit.

## R6. A clean git status after the copy into the sandbox (FR-011)

**Problem (verified)**: the copy into the sandbox changes each file's inode and ctime. Git then re-reads every file
whose stat data changed. A sandbox without an LFS clean filter hashes an LFS file's real content, which no longer
matches the pointer in the index, so every fetched LFS file shows as modified.

**Decision**: in each repository where LFS content was fetched, `RepoCheckout`:

1. sets `core.checkStat=minimal` and `core.trustctime=false` in the repository's own config (`.git/config`, or
   `.git/modules/<name>/config` for a submodule). Git then compares only whole-second mtime, size and file type;
2. sets the mtime of each fetched file two seconds in the past;
3. refreshes the index (`git update-index -q --refresh`) with the `filter.lfs.*` config in the command's environment
   (`RepoCheckout`'s `lfs_filter_config`, which tests replace with a test-only filter),
   so the worker records the new stat data against the pointer. The index is now newer than every LFS file, so no
   entry is racily clean.

daiv-sandbox extracts the seed with Docker's `put_archive`, which keeps each member's mtime
(`daiv_sandbox/sessions.py`, `_sanitize_archive_stream`). Python's `tarfile` keeps mtime and size, so the stat data
still matches in the sandbox, and git there does not re-read LFS files the agent left alone.

**Verified**: a clone with fetched LFS content, after these three steps, copied with `tarfile` as
`_make_repo_archive` does, shows a clean `git status` with no LFS filter configured. After an edit to an LFS file, it
shows ` M <path>`. A new file matching an LFS pattern shows as untracked.

The same holds on disk runs: `LocalGitRunner` starts git with no LFS filter unless the image's system config sets one,
and the files never move.

**Alternatives considered**:

- `skip-worktree` on LFS paths, rejected: git then hides the agent's edits, so DAIV would have to re-hash every LFS
  file at publish to name them (FR-013), and merges treat the bit in ways that differ between git versions.
- Copying the git-lfs binary into the sandbox, rejected: the sandbox image's CPU architecture is not known, and DAIV
  would be installing a program into the operator's image.
- A shell clean filter that computes pointers in the sandbox, rejected: it depends on `sha256sum`, `mktemp` and `wc`
  being in the image, and a failing filter falls back to committing the raw content.

## R7. The sandbox copy carries LFS content once (FR-015)

**Decision**: `_make_repo_archive` and `_make_repos_archive` leave out the `lfs` directory directly inside every git
directory (`.git/lfs`, `.git/modules/**/lfs`). That is git-lfs's object store; the checked-out files already hold the
content. Submodule git directories (`.git/modules/...`) and their `.git` files are kept: the gitfiles hold relative
paths (`gitdir: ../.git/modules/lib`, verified), so they resolve inside the sandbox.

**Rationale**: SC-006. Without LFS, no `lfs` directory exists, so the archive is unchanged (FR-016).

## R8. The publish keeps submodules and LFS paths out (FR-012 to FR-014)

**Problem (verified)**:

- An edit inside a submodule shows as ` M lib` in the superproject. `git add -A` stages nothing for it, so a commit
  whose only change is that edit fails with "nothing to commit".
- A submodule whose HEAD moved, which includes a checkout the remote's integration left behind, makes `git add -A`
  stage the new gitlink.
- `git status`, `git diff` and `git ls-files` accept `:(exclude,attr:filter=lfs)`. `git add` rejects `attr` pathspec
  magic.

**Decision**: the publisher builds a `PublishScope` from the repository's checkout settings and hands it to
`GitManager`, so disk runs and sandbox runs share one code path (layering rule 4):

- **`status_snapshot(..., scope)`**: with submodules on, it adds `--ignore-submodules=all` to `status` and `diff`.
  With LFS on, it adds the pathspec `. ':(exclude,attr:filter=lfs)'` to `status`, `diff` and `ls-files --others`.
  `dirty`, the merge request diff, the commit message diff and the diff stats then ignore both, so a turn whose only
  changes are kept out publishes nothing and does not fail (FR-013).
- **The same batch lists what was kept out**:
  - LFS: `git status --porcelain -z -- ':(attr:filter=lfs)'`;
  - submodules: `git status --porcelain=v2 -z`. Every `S...` entry counts, and so does every entry whose `HEAD` mode is
    `160000`: a deleted or renamed submodule shows as an ordinary entry, not an `S...` one;
  - files inside each changed submodule: `git -C <path> status --porcelain -z`, capped at 20 paths per submodule in
    the notice.
- **`commit_all(message, keep_out=...)`**: runs `git add -A`, then `git --literal-pathspecs reset -q -- <gitlink paths
  and kept-out LFS paths>`, then `git commit`. Before the commit it also resets every path that `git diff --cached
  --raw -z` shows with the new mode `160000` and that `record` (R16) doesn't name. That catches a gitlink `git add -A`
  created: a submodule the agent added, or a repository it cloned inside the checkout. Status lists such a repository
  only as untracked (`?? dir/`), so the snapshot treats an untracked directory holding `.git` as kept out (`added`),
  not as a change. When the index still equals `HEAD` after the resets, `commit_all` makes no commit and the publish
  reports nothing published, so a turn whose only changes were kept out never fails on "nothing to commit"
  (FR-013). `reset` restores those paths' index entries from `HEAD`. That covers an
  edited, new, deleted or renamed LFS path, and a moved gitlink. The kept-out paths are the agent's few edits, so the
  argv stays short.
- **FR-014**: `_integrate_remote` rebases or merges the run's commits, which never touch a gitlink, onto the remote,
  so the result keeps the remote's gitlink. The submodule's checkout then lags the index. The next publish's `reset`
  of gitlink paths keeps that lag out, where `git add -A` would otherwise revert the remote's bump.

- **`fast_forward(branch, scope)`**: the start-of-turn fast-forward reads the same scoped status. Changes kept out of an
  earlier publish then don't count as unpublished work, and don't stop a reused sandbox from catching up with its
  branch.

`PublishOutcome` and `RepoPublish` gain `kept_out: KeptOut | None`. `GitMiddleware` renders it as a visible notice
beside the merge conflicts notice, for the main repository and each extra repository. Draft recovery
(`sessions/executor/recovery.py`) uses the same publisher, so it applies the same guard.

**Alternatives considered**: `submodule.<name>.ignore=all` written into the clone's config, rejected because git
commands differ in which of them honour it. Excluding paths with pathspecs on `git add`, rejected because `attr`
magic is not supported there.

## R9. What the agent is told (FR-009)

**Decision**: `GIT_SYSTEM_PROMPT` gains a section per repository with either setting on. It lists:

- which submodules were checked out, and for each whether DAIV publishes the changes made inside it (R14) or why
  not;
- that changes to LFS-tracked files, and inside the submodules DAIV does not publish, are not published;
- each submodule or LFS file left out, with its reason.

At most 20 LFS files are listed by name, then a count. A repository with both settings off renders no new text, so
existing prompts and eval cases are unchanged. A warm sandbox reused on a later turn gets the section again, built
from that turn's clone.

Reasons render as:

- outside the platform;
- DAIV's account can't read the repository;
- the person this run belongs to can't read the repository;
- the recorded commit is missing;
- over the LFS download limit;
- the download failed.

Why a checked-out submodule is not published (`PublishBlock`, R14):

- the repository is published from another checkout (its path);
- the submodule that contains it isn't published;
- the person this run belongs to can't write to the repository.

A new skip reason, `not_catalogued` (R3), reads "DAIV doesn't work on that repository".

None carries a URL credential, a token, a raw git error or a setting's value. The submodule's path and its
repository's path are shown, because the person can already read both in `.gitmodules`.

**Measurement (Principle V)**: the new text changes what the model sees only in runs on opted-in repositories, which
no existing eval case has. The pull request declares the change unmeasured (agent-evals rule 2), and makes no claim
about agent quality.

## R10. Logging (FR-010)

**Decision**:

- `logger.error` names the repository and the submodule path, never a token, when the fault is DAIV's to fix: DAIV's
  account can't read a submodule's repository; a credential can't be minted or is rejected after the retries; a
  fetch fails on the network.
- `logger.info` covers the expected skips: outside the platform, not in DAIV's catalog, the person lacks access or
  write access, a missing commit, over the cap. Many teams keep some submodules outside DAIV on purpose, so a catalog
  miss is no operator error.
- The LFS pull logs `logger.error` on a failed batch, and its files are reported as a failed download.

**Rationale**: errors rule 1. The expected skips come from the repository's content or the person's access, which the
operator cannot change.

## R11. Configuration surface

**Decision**:

- **`.daiv.yml`**: a new `checkout` section, `submodules: false` and `lfs: false` (FR-001). It is read from the
  default branch like every other key (`RepositoryConfig.get_config`). Nested submodules follow their superproject's
  setting (spec Assumptions).
- **Operator**: `CODEBASE_LFS_MAX_CLONE_BYTES` (R5), documented in `docs/reference/env-variables.md`.
- **Images**: `git-lfs` joins the apt list of `docker/production/app/Dockerfile` and `docker/local/app/Dockerfile`.
  The deployment page states it for operators who build their own image. No `git lfs install`: DAIV passes
  `filter.lfs.*` per command (R5, R6), so the image's git config stays as it is.

## R12. Testing approach

**Decision**:

- Real git on temporary local repositories, as `test_git_manager.py` and `test_context.py` already do. Submodule tests
  serve the submodule repositories through a stub client whose `get_repository` maps platform paths to local bare
  repositories. The allowed protocol is a constructor argument of `RepoCheckout`, so a test can allow `file`.
- `git lfs pull` sits behind `RepoCheckout`'s command runner. LFS tests stub it to write each selected file's content
  from a fixture and assert the `--include` batches. The stat, index and attribute logic runs on real git, with a
  test-only clean filter standing in for git-lfs.
- Unit tests make no network call (tests rule 3).

## R13. Rejected and deferred

- A limit on the number or nesting depth of submodules: git sets none, and the person's own access bounds what can be
  fetched. Added later if runs show a need.
- Refreshing a warm sandbox's submodules after `fast_forward` moves a gitlink: the sandbox holds no credential for the
  submodule's repository. The publish guard keeps the remote's gitlink (FR-014), and the agent reads the checkout from
  the turn that started the sandbox.
- Publishing LFS files: a later feature, per the clarification. Submodule changes are published (R14 to R22).
- Refreshing a warm sandbox's submodule checkouts at the start of a turn, like the main repository's fast-forward:
  the sandbox holds no submodule credential until a publish (R18). A warm sandbox keeps the previous turn's submodule
  state; the publish integrates the submodule's remote branch on push (`integrate_on_reject` for an existing merge
  request), so a push someone made between turns is kept, not overwritten. Nothing a parent records depends on the
  sandbox's submodule checkout: R16 records only commits DAIV just pushed and the pins the worker checked (R21).
- Recording a person's push to a submodule's branch in its parent when DAIV didn't change the submodule: it would
  need the remote's state in the sandbox, and the next change DAIV makes there builds on that push anyway.
- A setting that keeps submodules read-only while the person can write them: write access is the platform's own
  statement of who may change a repository (spec Assumptions).

## R14. Which submodules DAIV publishes (FR-019, FR-024)

**Decision**: every checked-out submodule gets a publish verdict, `publish_block: PublishBlock | None`. `None` means
DAIV publishes its changes. Fetching already required DAIV's catalog (R3), so DAIV can always push where it fetched.
The blocks, in the order they are checked:

1. **`published_elsewhere`**: another checkout in the session publishes the repository. That checkout is the
   session's main or an extra repository, or the first checkout of that repository, in session order, that no other
   block stops. Session order is the main repository's submodules in checkout order, then each extra repository's.
   This is decided by `set_runtime_ctx` after every repository's checkout has finished, because each `RepoCheckout`
   sees one clone only. Since it needs the other blocks first, `set_runtime_ctx` applies it last, but it takes
   precedence over them in the report (FR-024).
2. **`parent_not_published`**: the submodule that contains it has a block. A nested submodule's new commit can only be
   recorded by a parent DAIV publishes. `set_runtime_ctx` propagates this after step 1.
3. **`person_no_write`**: the run's person can't write the repository. `RepoCheckout` checks it.

R4's predicate becomes `RunRepoAccess`, with three methods that the executor builds from `codebase.authorization`:

- `can_read(repo_id)`: R4 unchanged.
- `can_write(repo_id)`: `can_run(user, repo_id)` for a DAIV user. For a platform account, a new
  `platform_account_can_write(uid, repo_id)` over the same fresh `RepositoryAccess` rows with
  `access_level == RepoAccessLevel.WRITE`. The sync maps GitLab Developer and above, and GitHub push and above, to
  write.
- `in_catalog(repo_id)`: a new strict `is_catalogued(repo_id)`, false when the catalog is empty (R3).

`DENY_ALL` answers no to all three.

**Rationale**:

- The access mirror already records each member's level, so write access costs a database read, and webhook runs
  are judged by the same data as DAIV-user runs (R4).
- Settling the verdict at the start lets the agent know which submodules it can change (FR-009).

**Alternatives considered**: a separate `.daiv.yml` switch for publishing, rejected (spec Assumptions): write access is
the platform's own answer to who may change a repository.

## R15. A submodule is a handle of its parent, not an extra repository

**Decision**: `RepoHandle` gains three fields:

- `workspace_path`: `/workspace/repo`, `/workspace/repos/<slug>`, or `<root's workspace_path>/<path>`. It replaces
  `extra_repo_path(handle)` in the prompt and in `for_repo`.
- `submodules: tuple[RepoHandle, ...]`: on a main or extra handle, every checked-out submodule, nested ones included,
  in checkout order.
- `submodule: SubmoduleRole | None`, which holds:
  - `path` (from the root's top);
  - `parent_path` (`""` under the root);
  - `recorded_commit`;
  - `target_branch` (R17);
  - `publish_block`;
  - `pinned` (R21);
  - `note` (R19, R21).

`set_runtime_ctx` builds a submodule's handle from its `CheckoutReport` entry:

- the `Repository` the checkout looked up;
- its `RepositoryConfig.get_config(repo_id)`, for `default_branch`, `omit_content_patterns` and `context_file_name`,
  as for an extra repository;
- `git.Repo(<clone>/<path>)`, which GitPython opens through the `.git` file;
- `current_ref` and `head_detached` as R19 leaves them.

Submodule handles are never in `RuntimeCtx.repos`. So the extra-repository file routes, the extra-repositories seed
archive and the prompt's "Other repositories" list don't duplicate them: they travel inside their parent's checkout
and archive (R7). The disk-run file tools already reach them under their root's route.

`for_repo(handle)` serves a submodule like an extra repository, at `handle.workspace_path`:

- **Disk runs:** `LocalGitRunner(handle.gitrepo, auth_env=...)`, with the auth environment from
  `with_git_auth(..., scoped_only=True)` (R3).
- **Sandbox runs:** `SandboxGitRunner(repo_path=handle.workspace_path)`, with R18's credential.

**Rationale**: every place that reads `extra_repos` assumes a separate clone at `/workspace/repos/<slug>`. A flag on
the existing extra handle would have to be checked at each of them, while a separate collection is ignored by them
all. The publisher only needs a context and a `PublishWorkspace`, and both accept a nested path.

## R16. Publish order and the recorded commit (FR-021)

**Decision**:

1. **Order.** `GitMiddleware.aafter_agent` and draft recovery keep today's repository order, main first and then each
   extra. Before each repository's own publish, a new `apublish_submodules(ctx, workspace, handle, ...)` publishes that
   repository's unblocked submodules, deepest path first. Each submodule publish is
   `GitChangePublisher(_extra_repo_ctx(ctx, sub), workspace.for_repo(sub), ...)`. Its `PublishScope` follows the root's
   settings (nested submodules follow their superproject), so LFS paths and blocked nested submodules stay out of it
   as R8 describes.
2. **What a parent records.** `PublishScope` gains `record: Mapping[str, str]`, which maps a direct child's path to
   the commit its parent records. There are exactly two sources:
   - a child published this turn records its `PublishOutcome.pushed_sha`;
   - a child with a `pinned` commit records it (R21). A pin needs no checkout, because `update-index --cacheinfo`
     writes the gitlink directly. So it works the same on disk, in a fresh sandbox and in a warm one.
   Nothing reads the sandbox's submodule `HEAD`, which a warm sandbox may hold from an earlier turn (R13).
3. **How the parent commits it.**
   - `status_snapshot` counts an entry whose commit differs from the index as a change.
   - It adds `git diff --submodule=log -- <path>` for each entry to the diff the metadata reads. A pin's commit may be
     missing from a warm sandbox's submodule, so the diff then falls back to the two short commit ids.
   - `commit_all` runs `git update-index --cacheinfo 160000,<sha>,<path>` for each entry, after R8's resets.
4. **A failed child.** A child whose publish fails adds no entry. Its parent's gitlink stays as it was (R8), and the
   failure is a `RepoPublish.error` naming the submodule (FR-021).
5. **Where the results go.** The submodule's `RepoPublish` gains three fields: `submodule_path`, `root_repo_id` and
   `parent_repo_id`, the last being the repository whose commit records it. The publish joins `repo_publishes`, which
   feeds the job status's `merge_requests` and `publish_errors`, the transcript, `Run.sibling_pushed_shas` and the CI
   watch claim (FR-026). The turn-end code that reads `repo_publishes` assumes one entry per extra repository today,
   so T067 changes these places:
   - `_sibling_notices` and `_alink_related` stop zipping against `ctx.extra_repos` (`strict=True`) and look each
     publish's repository up by `repo_id`;
   - `update["repo_publishes"]` is written whenever there are extra or submodule publishes;
   - a failed main publish is recorded and noticed, not raised, whenever submodules were published, as in a
     multi-repository session. The submodules' results, already pushed, then reach the job status and their rows.

   Turn-end persistence sends a submodule publish to `SessionSubmodule` (R22) instead of `SessionRepository`.

**Rationale**:

- The parent can only record a commit after it is on the remote, so the publish runs innermost first.
- `update-index --cacheinfo` records exactly the commit wanted. `git add` would stage whatever the checkout's `HEAD`
  is, which a failed integration or a warm sandbox can leave elsewhere.

**Alternatives considered**:

- Publishing the parent first and amending it after the children, rejected because it makes two pushes to the parent
  per turn, and a failed child would leave the parent pointing at a commit that was never pushed.
- Also recording an unchanged child whose remote branch moved (a person's push), rejected (R13).

## R17. The branch and target of a submodule's merge request (FR-020)

**Decision**:

- **Target branch.** `RepoCheckout` settles `target_branch` at checkout:
  - `.gitmodules`' `branch` for the submodule, where `.` means the parent's base branch;
  - else, or when that branch is not among the submodule clone's remote branches, the submodule repository's default
    branch.

  Checking at checkout matters. `status_snapshot` runs `merge-base origin/<target> HEAD` before the publisher's
  `_live_target_branch` fallback is ever reached, and fails on a missing branch.
- **Base branch.** `run_base_branch` returns `handle.submodule.target_branch` for any submodule handle with no open
  merge request on its `current_ref`. So a branch hint whose merge request merged or closed never becomes a target
  (R19).
- **First publish.** The submodule's `HEAD` is detached at the recorded commit, so the publisher takes its
  new-merge-request path. `unique_branch_name` names the branch from the metadata, and the publisher pushes
  `HEAD:<branch>`.
- **Diff.** The diff and the merge request show `merge-base origin/<target> HEAD` to the working tree, so only the
  agent's changes appear even when the recorded commit lags the target. `--no-recommend-shallow` (R3) keeps every
  remote branch in the submodule's clone.

## R18. The credential for a submodule's publish in a sandbox (FR-004)

**Decision**:

- **Disk runs:** `for_repo` gives the submodule its own scoped auth environment (R15).
- **Sandbox runs:**
  - The egress credentials stay as they are minted at acquire, main and extras first, so their
    `__daiv_git_platform__<i>` indices don't move.
  - A new `SandboxSession.aadd_repository(repository, mint)` appends one credential for a submodule repository. `mint`
    is `lambda: client.get_git_egress_credential(repository, scoped_only=True)`, passed in by the sandbox workspace
    from the handle's client, since `SandboxSession` holds no client.
  - It always pushes `update_egress`, whatever `CREDENTIAL_REFRESH_WINDOW_SECONDS` says, because the rule is new
    rather than a refresh. A second call for the same repository mints a fresh token in the same slot.
  - The sandbox workspace's `for_repo(sub).authenticate` calls it just before that submodule's publish.
  - The rule is scoped to the path of the repository's `clone_url`, and the submodule's `origin` is that URL (R2).
- **Later turns:** a warm sandbox that is reused gets the acquire-time list again on its next turn. The submodule's
  credential then leaves the sandbox until its next publish.

**Rationale**: the agent's turn runs with no credential for a submodule's repository. One exists only for the
publish that needs it (security rule 3, narrowest and shortest-lived).

**Alternatives considered**: minting a credential at acquire for every submodule DAIV may publish, rejected because it
puts tokens for repositories the run may never change into the sandbox for the whole turn.

## R19. Later turns continue on the submodule's branch (FR-023)

**Decision**:

- **Branch hints.** The executor reads the session's `SessionSubmodule` rows (R22) and passes each root's working
  branches to the checkout as `CheckoutOptions.branches` (`{path: branch}`).
- **Checking the branch out.** On the worker, after checking a submodule out at the recorded commit, `RepoCheckout`
  checks out `-B <branch> origin/<branch>` when `git merge-base --is-ancestor <recorded> origin/<branch>` holds. The
  publish then finds the open merge request on `current_ref`, as for an extra repository. A warm sandbox keeps its
  own checkout (R13), and the publish pushes `HEAD:<branch>`, integrating the remote first.
- **When it doesn't hold.** The branch no longer contains what the parent records, for example after R20 kept the
  remote's commit. The submodule then stays detached, and `note` tells the agent that changes there open a new merge
  request.
- **A missing branch** leaves the submodule detached.
- **Clearing rows.** Turn-end persistence never clears a row's branch or merge request. A row's `ref` and merge
  request fields are cleared when that merge request merges or closes (R21), since `apersist_extra_repo_state`, the
  model for persistence, never clears a ref either.

**Rationale**: checking out a branch that doesn't contain the recorded commit would make the parent's next publish
drop whatever the recorded commit added.

## R20. A remote that moved a pointer DAIV also moves (FR-014)

**Problem**: the run's commits never touched a gitlink before. Now a parent's commit can record a child at a new
commit. If the remote branch moved the same gitlink meanwhile, `_integrate_remote`'s rebase or merge conflicts, and
today that aborts the parent's whole publish (`GitPushStaleError`).

**Decision**: `_integrate_remote` takes the scope's `record` paths. When the rebase or merge stops and every
conflicted path is one of them, each is resolved to the remote's commit:

- read stage 2 in a rebase (upstream) or stage 3 in a merge (`FETCH_HEAD`) from `ls-files -u -z`, and write it back
  with `update-index --cacheinfo`;
- continue with `-c core.editor=true rebase --continue`, or commit the merge. A commit the resolution leaves empty is
  dropped.

The publish reports the path in `KeptOut` as `remote_moved`. Any other conflict aborts as today. The submodule's own
merge request is already pushed and stays. On the next turn R19 leaves the submodule at the remote's commit.

**Rationale**: FR-014 says the remote's commit wins. Aborting would also throw away the parent's other changes.

## R21. Pointing the parent at the merged commit (FR-025)

**Decision**:

1. **The merge or close callback.** GitLab's `MergeRequestCallback` and GitHub's `PullRequestCallback` change in two
   ways.
   - **On a merge**, before `MergeArchive`, they mark each `SessionSubmodule` row holding the merged merge request
     `merge_pending=True`, then enqueue `settle_submodule_merge_task(repo_id, iid)`. `without_live_work` counts a
     session with a `merge_pending` row as live work. So any `MergeArchive` meanwhile, the submodule's own or the
     parent's merged right after, leaves the session `archive_pending` instead of archiving it.
   - **On a close** without merge, they clear the rows' `ref` and merge request fields, and nothing else happens.
2. **The task.** For each `merge_pending` row on that merge request, it asks the platform two things, through new
   `RepoClient` methods that both clients implement:
   - `get_merged_commit(repo_id, iid)`. On GitLab that is `merge_commit_sha`, else `squash_commit_sha`, else the merge
     request's head (a fast-forward merge). On GitHub it is `merge_commit_sha`, which is the merge, squash or last
     rebased commit.
   - `is_ancestor(repo_id, row.recorded_sha, merged)`. GitLab answers through `repository/merge_base`, GitHub through
     `compare` (`ahead` or `identical`).

   Then, in one transaction:
   - When the recorded commit is not an ancestor, it posts a waking `submodule_merged` event through
     `Inbox(thread_id).apost`. The event has `repo_id` set to the submodule's repository, the data-model payload, and
     the source key `submodule_merged:<repo_id>!<iid>`, so a redelivered webhook posts nothing twice.
   - It clears the row's `ref`, merge request fields and `merge_pending`.
   - It calls `MergeArchive.settle(thread_id)`. With the event pending, `settle` archives nothing. Without it, `settle`
     archives the session if a merge left it pending.

   A row whose merge request iid no longer matches is skipped. A platform error is handled in four steps:
   - it is logged at ERROR, naming the repository and merge request;
   - a remembered `notice` event tells the session the pointer couldn't be checked;
   - `merge_pending` is cleared;
   - the session is settled.

   Nothing is silently held.
3. **The run.**
   - The drain starts it as for any event DAIV wakes on its own. Its trigger type is a new `SessionOrigin`,
     `SUBMODULE_MERGE`, listed in `daiv_authored()` and `classified()` like a CI fix, so transcript authorship, the
     consecutive-run limit and Insights treat it as DAIV's own.
   - It is not gated: each merge wakes the session at most once.
   - Its person follows R4, including the session's platform account when the owner has no DAIV user.
   - **Where it starts.** At start, the executor asks the platform for the parent's merge request: the session's, an
     extra row's, or an outer `SessionSubmodule` row's. When that merge request has merged, the parent starts from its
     default branch with no target merge request, whether or not its source branch still exists. Otherwise it starts
     on its branch as usual.
   - **The pin.** The executor passes `CheckoutOptions.pins` (`{path: (recorded_sha, merged_sha)}`). On the worker,
     `RepoCheckout` compares the commit the parent's clone records at `path` with `recorded_sha`:
     - equal: the submodule's `pinned` is `merged_sha`, and the parent's `record` takes it (R16);
     - not equal: `pinned` stays empty, and `note` says the branch no longer records DAIV's commit.
   - **Publish and report.** The parent's publish updates its open merge request, or opens a new one from the
     default branch (US2 #7, #8). The turn-end notice reports either outcome deterministically from the handle
     (contracts/agent-context.md §2). The event renders one DAIV-written prompt from its payload, asking the model to
     change nothing else and to confirm in one sentence (contracts §3).
4. **Nested submodules.** When the parent is itself a submodule, its open merge request is updated, and the
   superproject records the parent's new pushed head in the same publish (R16). When the parent's own merge request
   merges later, this repeats.
5. **Webhooks.** On GitLab, the submodule's project must send DAIV its merge request events. It is in DAIV's catalog
   (R3), which `setup_webhooks` covers, and the docs say so (T094).

**Rationale**:

- The merge callback is where DAIV already learns of merges, and the inbox is how DAIV makes a session act on its
  own, with one run per session and the requester rules already in place.
- The ancestry check avoids a run for each merge-commit merge.
- The pointer change is the harness's work, deterministic and tested. The model only confirms it, and the notice
  carries the facts.

**Alternatives considered**:

- A turn with no model call, rejected: no inbox kind does deterministic work instead of a run today, so it would be a
  new mechanism beside the existing one.
- Re-pointing at every merge, rejected because merge commits would cost a run that changes nothing.
- Reusing `MR_WEBHOOK` or `PIPELINE_WEBHOOK` as the trigger type, rejected: the first counts as a person's run, and
  the second as a CI fix.
- Marking the parent's merge request draft until its submodules merge, rejected: DAIV would be overriding the
  reviewers' own use of draft. The merge-first note (R23) and this repair cover the risk.

## R22. Session state for submodules (FR-023, FR-026, FR-027)

**Decision**: a new model, `SessionSubmodule`, not more `SessionRepository` rows.

- **Why not `SessionRepository`.** Every reader of those rows treats one as a repository the person chose, which DAIV
  clones at `/workspace/repos/<slug>` and on which it requires write access:
  - chat continuation (`chat/api/views.py` 259–264);
  - jobs continuation (`sessions/multi_repo.py` 110–116, 181–191, 344–348);
  - Retry (`sessions/header.py` 83);
  - the inbox drain's access checks (`sessions/inbox/drain.py` 266–270, 386–391);
  - the executor's clone (`sessions/executor/run.py` 421–434);
  - the repository cards (`sessions/page_state.py`);
  - the memory skip.

  A read-only submodule row would break most of them.
- **Fields:** see data-model.md.
  - `root_repo_id`: the session repository whose checkout holds it.
  - `parent_repo_id`: the repository whose commit records it, the root or an outer submodule's.
  - `path`, from the root's top.
  - `recorded_sha`: the commit the parent records, as DAIV's last successful parent publish set it.
  - `merge_pending`.
  - The same branch, merge request and size fields as `SessionRepository`.

  It is unique on `(session, repo_id)`, which is FR-024's one published checkout per repository, and indexed on
  `(repo_id, merge_request_iid)`.
- **Written:**
  - by the executor right after the checkout and before the agent starts, one row per checked-out submodule whose
    repository is not already the session's (FR-027), the first checkout in session order when there are several;
  - at turn end, by `apersist_submodule_state`. It takes the `RepoPublish` entries that carry a `submodule_path`, and
    copies their `recorded_sha`. That is filled in only once the parent's publish succeeded with the record: by
    `apublish_submodules` when the parent is a submodule, and by `GitMiddleware` or draft recovery after the root's
    own publish. A pinned child gets such an entry too, with `published` false. It runs after every run that
    published, whatever its `persist_ref`, so merge-request-comment runs (`persist_ref=False`) keep their submodule
    state too. Only the session's own ref follows `persist_ref`.
- **Readers that change:**
  - **Visibility:** `_readable_repos_q` also requires read access to every submodule row (FR-027, SC-008).
  - **Live work:** `without_live_work` counts a `merge_pending` row (R21).
  - **Memory:** the skip asks a new `Session.aholds_other_repositories()`, which is true with an extra repository or a
    submodule row.
  - **Merge lookup:** `on_merge_request` matches submodule rows too, for the merge request filter and R21.
  - **Session page:** `page_state` shows a card per submodule row that has a merge request, labelled with its path
    and parent.
  - **Executor:** it reads the rows for R19's branches and R21's pins and parent.
  - **Inbox drain:** a non-mention requester also needs read access to submodule rows, as they need write access to
    extra repositories. A CI identity's `_proves_access` checks the same.

**Rationale**: a separate model keeps "a `SessionRepository` row is an extra repository" true, so the existing
readers stay correct without a filter, and a reader added later cannot mistake a submodule for one.

## R23. Linking the merge requests (FR-022)

**Decision**:

- `RelatedMergeRequest` gains `parent_repo_id`, the repository that records it (`""` for none).
- `render_related_block(viewer, others)` sorts each other merge request into one group, from the point of view of
  the merge request being rendered:
  - `**Merge first** (submodules this merge request points at):` for those whose parent is the viewer;
  - `**Recorded by**:` for the one that is the viewer's parent;
  - today's `**Related merge requests** (same DAIV session):` for the rest.

  The same entries thus render differently on each merge request: in a nested chain, the middle merge request shows
  its child under **Merge first** and its parent under **Recorded by**.
- `_alink_related` keeps today's condition, anything published in a session with extra repositories, OR-ed with "a
  submodule merge request was published this turn". So a main-only turn still refreshes the extras' links as today
  (FR-016).
