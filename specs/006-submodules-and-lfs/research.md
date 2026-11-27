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
what the sandbox's path-scoped egress rule for a submodule publish matches on (R17).
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

1. Look the repository up with `client.get_repository(path)`. A lookup the platform refuses means DAIV's account
   cannot read it.
2. Check the person's access (R4).
3. In the parent, run `git submodule init -- <path>`, set `submodule.<name>.url` to the resolved HTTPS URL (R2), then
   run `git -c core.hooksPath=/dev/null submodule update --checkout -- <path>`. It clones the submodule and checks out
   the recorded commit; `--checkout` overrides any `update` mode a repository sets.
4. Recurse into the submodule's own `.gitmodules`.

The environment of step 3 holds only that submodule's credential (`client.get_git_auth_env(sub_repository)`), never the
main repository's. This matters because git's `http.<url>.extraHeader` is multi-valued: with a host-wide key for the
main token and a path key for the submodule token both present, git sends both headers.

Minting goes through a new `RepoClient.with_git_auth(repository, operation)`. GitLab overrides it with the clone's
existing tolerance for token propagation lag and its stale-token self-heal, which move out of `GitLabClient._clone`
into this method. The main clone then uses the same method. GitHub mints one repository-scoped installation token per
call.

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
  created: a submodule the agent added, or a repository it cloned inside the checkout, which status lists only as
  untracked. `reset` restores those paths' index entries from `HEAD`. That covers an
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

- the person this run belongs to can't write to the repository;
- the submodule that contains it isn't published;
- the repository is published from another checkout (its path).

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
- `logger.info` covers the expected skips: outside the platform, the person lacks access, a missing commit, over the
  cap.
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
  the publish integrates the submodule's remote branch on push (`integrate_on_reject` for an existing merge request),
  so a push someone made to the submodule's branch between turns is kept, not overwritten.
- A setting that keeps submodules read-only while the person can write them: write access is the platform's own
  statement of who may change a repository (spec Assumptions).

## R14. Which submodules DAIV publishes (FR-019, FR-024)

**Decision**: `RepoCheckout` gives every checked-out submodule a publish verdict, `publish_block: PublishBlock | None`.
`None` means DAIV publishes its changes. The checks run in this order, and the first that fails is the reason:

1. **`published_elsewhere`**: the repository is the session's main or an extra repository, or a submodule at an
   earlier path in checkout order. `CheckoutOptions.session_repo_ids` carries the session's repositories (FR-024).
2. **`parent_not_published`**: the submodule that contains it has a block. A nested submodule's new commit can only be
   recorded by a parent DAIV publishes.
3. **`daiv_no_write`**: the repository is not in DAIV's `RepositoryCatalog`. The catalog is the set DAIV's account
   publishes to: GitLab projects where it is Maintainer, the GitHub App's installation repositories. That is the
   same set as for main and extra repositories.
4. **`person_no_write`**: the run's person can't write to the repository.

R4's predicate becomes `RunRepoAccess`. It has three methods, each built by the executor from `codebase.authorization`:

- `can_read(repo_id)`: R4 unchanged.
- `can_write(repo_id)`: `can_run(user, repo_id)` for a DAIV user. For a platform account only, it is a new
  `platform_account_can_write(uid, repo_id)` over the same fresh `RepositoryAccess` rows, with `access_level ==
  "write"`. The sync maps GitLab Developer and above, and GitHub push and above, to write.
- `daiv_can_publish(repo_id)`: catalog membership.

`DENY_ALL` answers no to all three.

**Rationale**: the access mirror already records each member's level. So the person's write access costs a database
read, not a platform call, and webhook runs are judged by the same data as DAIV-user runs, as in R4. Settling the
verdict at checkout lets the agent know from the start which submodules it can change (FR-009). It also lets the
publish skip a token mint that would fail.

**Alternatives considered**:

- A separate `.daiv.yml` switch for publishing, rejected (spec Assumptions): write access is the platform's own answer
  to who may change a repository.
- Trying the push and reporting a refusal, rejected as the only check: the agent would edit a submodule only to learn
  at the end of the turn that nothing could be published. A refused push still ends as a publish failure (FR-021).

## R15. A submodule is a handle of its parent, not an extra repository

**Decision**: `RepoHandle` gains three fields:

- `workspace_path`: `/workspace/repo`, `/workspace/repos/<slug>`, or `<parent's workspace_path>/<path>`. It replaces
  `extra_repo_path(handle)` in the prompt and in `for_repo`.
- `submodules: tuple[RepoHandle, ...]`: on a main or extra handle, every checked-out submodule, nested ones included,
  in checkout order.
- `submodule: SubmoduleRole | None`, which holds:
  - `path` (from the top-level repository's root);
  - `parent_path`;
  - `recorded_commit`;
  - `target_branch` (R17);
  - `publish_block`;
  - `note` (R19, R21).

`set_runtime_ctx` builds a submodule's handle from its `CheckoutReport` entry:

- the `Repository` the checkout looked up;
- its `RepositoryConfig.get_config(repo_id)`, for `default_branch`, `omit_content_patterns` and `context_file_name`,
  as for an extra repository;
- `git.Repo(<clone>/<path>)`, which GitPython opens through the `.git` file;
- `current_ref` and `head_detached` as R19 leaves them.

Submodule handles are never in `RuntimeCtx.repos`. So the extra-repository file routes, the extra-repositories seed
archive and the prompt's "Other repositories" list don't duplicate them: they travel inside their parent's checkout
and archive (R7). The disk-run file tools already reach them under their parent's route.

`for_repo(handle)` serves a submodule like an extra repository, at `handle.workspace_path`:

- **Disk runs:** `LocalGitRunner(handle.gitrepo, auth_env=client.get_git_auth_env(sub_repository))`.
- **Sandbox runs:** `SandboxGitRunner(repo_path=handle.workspace_path)`, with R18's credential.

**Rationale**: every place that reads `extra_repos` assumes a separate clone at `/workspace/repos/<slug>`. A flag on
the existing extra handle would have to be checked at each of them, while a separate collection is ignored by them
all. The publisher only needs a context and a `PublishWorkspace`, and both accept a nested path.

## R16. Publish order and the recorded commit (FR-021)

**Decision**:

1. **Order.** `GitMiddleware.aafter_agent` and draft recovery keep today's repository order, main first and then each
   extra. Before each repository's own publish, a new `apublish_submodules(ctx, workspace, handle, ...)` publishes that
   repository's unblocked submodules, deepest path first. Each submodule publish is
   `GitChangePublisher(_extra_repo_ctx(ctx, sub), workspace.for_repo(sub), ...)`. Its `PublishScope` follows the
   top-level repository's settings (nested submodules follow their superproject), so LFS paths and blocked nested
   submodules stay out of it as R8 describes.
2. **What the parent records.** `PublishScope` gains `record: Mapping[str, str]`, which maps a direct child's path to
   the commit its parent records. A child's entry is one of:
   - the `pushed_sha` of its publish this turn;
   - when it did not change this turn, its `HEAD`, if that equals the head of its working branch on the remote.
     That covers a person's push to the submodule's merge request between turns. Both values come from the child's
     own status snapshot (`rev-parse HEAD`, `ls-remote`), so a warm sandbox is judged by its own checkout.
3. **How the parent commits it.**
   - `status_snapshot` counts an entry whose commit differs from the index as a change.
   - It adds `git diff --submodule=log -- <path>` for each entry to the diff the metadata reads, so a pointer-only
     commit still gets a message and title that name the submodule's commits.
   - `commit_all` runs `git update-index --cacheinfo 160000,<sha>,<path>` for each entry, after R8's reset of the
     kept-out gitlinks.
4. **A failed child.** A child whose publish fails adds no entry. Its parent's gitlink stays as it was (R8), and the
   failure is a `RepoPublish.error` naming the submodule (FR-021).
5. **Where the results go.** The submodule's `RepoPublish`, with new `submodule_path` and `parent_repo_id` fields,
   joins `repo_publishes`. From there it reaches the job status's `merge_requests` and `publish_errors`, the transcript,
   `Run.sibling_pushed_shas`, the CI watch claim and the session page with no new path (FR-026). Turn-end persistence
   sends it to `SessionSubmodule` (R22) instead of `SessionRepository`.

**Rationale**: the parent can only record a commit after it is on the remote, so the publish runs innermost first.
`update-index --cacheinfo` records exactly the pushed commit. `git add` would stage whatever the checkout's `HEAD` is,
which a failed integration or a warm sandbox can leave elsewhere.

**Alternatives considered**: publishing the parent first and amending it after the children, rejected because it
makes two pushes to the parent per turn, and a failed child would leave the parent pointing at a commit that was
never pushed.

## R17. The branch and target of a submodule's merge request (FR-020)

**Decision**: on its first publish, a submodule's `HEAD` is detached at the recorded commit, so the publisher takes
its new-merge-request path:

- **Branch:** `unique_branch_name` names it from the metadata, and the publisher pushes `HEAD:<branch>`.
- **Target:** `run_base_branch` returns `handle.submodule.target_branch` for a submodule handle. That is the `branch`
  `.gitmodules` sets for the submodule, where `.` means the parent's base branch, and otherwise the submodule
  repository's default branch. `_live_target_branch` already falls back to the default branch when the branch is
  not on the remote.
- **Diff:** the diff and the merge request show `merge-base origin/<target> HEAD` to the working tree, so only the
  agent's changes appear even when the recorded commit lags the target. The submodule's clone has every remote branch,
  because `git submodule update` makes a full clone.

## R18. The credential for a submodule's publish in a sandbox (FR-004)

**Decision**:

- **Disk runs:** `for_repo` gives the submodule its own auth environment (R15).
- **Sandbox runs:** the egress credentials stay as they are minted at acquire, main and extras first, so their
  `__daiv_git_platform__<i>` indices don't move. A new `SandboxSession.aadd_repository(repository)` appends one
  credential for a submodule repository, mints it and pushes `update_egress`. It ignores
  `CREDENTIAL_REFRESH_WINDOW_SECONDS`, because the main publish has usually just refreshed. The sandbox workspace's
  `for_repo(sub).authenticate` calls it just before that submodule's publish. The rule is scoped to the path of the
  repository's `clone_url`, and the submodule's `origin` is that URL (R2).
- **Later turns:** a warm sandbox that is reused gets the acquire-time list again on its next turn. The submodule's
  credential then leaves the sandbox until its next publish.

**Rationale**: the agent's turn runs with no credential for a submodule's repository. One exists only for the
publish that needs it (security rule 3, narrowest and shortest-lived).

**Alternatives considered**: minting a credential at acquire for every submodule DAIV may publish, rejected because it
puts tokens for repositories the run may never change into the sandbox for the whole turn.

## R19. Later turns continue on the submodule's branch (FR-023)

**Decision**:

- **Branch hints.** The executor reads the session's `SessionSubmodule` rows (R22) and passes each top-level
  repository's working branches to the checkout as `CheckoutOptions.branches` (`{path: branch}`).
- **Checking the branch out.** After it checks a submodule out at the recorded commit, `RepoCheckout` checks out
  `-B <branch> origin/<branch>` when `git merge-base --is-ancestor <recorded> origin/<branch>` holds. The publish then
  finds the open merge request on `current_ref`, as for an extra repository.
- **When it doesn't hold.** The branch no longer contains what the parent records, for example after R20 kept the
  remote's commit. The submodule then stays detached, and `note` tells the agent that changes there open a new merge
  request.
- **A missing branch** leaves the submodule detached. Turn-end persistence clears the row's `ref` and merge request,
  as `apersist_extra_repo_state` does for an extra repository.

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

1. **The merge callback.** GitLab's `MergeRequestCallback` and GitHub's `PullRequestCallback` handle a merge as today,
   with one change. Sessions that hold the merged merge request as a `SessionSubmodule` row (R22) are left out of that
   merge's `MergeArchive` and handed to `SubmoduleMerges(repo_id, iid).aenqueue()`, which enqueues
   `settle_submodule_merge_task`.
2. **The task.** It asks the platform two things, through two new `RepoClient` methods that both clients implement:
   - `get_merged_commit(repo_id, iid)`. On GitLab that is `merge_commit_sha`, else `squash_commit_sha`, else the merge
     request's head (a fast-forward merge). On GitHub it is `merge_commit_sha`, which is the merge, squash or last
     rebased commit.
   - `is_ancestor(repo_id, row.pushed_sha, merged)`. GitLab answers through `repository/merge_base`, GitHub through
     `compare` (`ahead` or `identical`).

   When the pushed commit is an ancestor, the parent's commit stays reachable, so the task settles the archive as
   today and nothing runs (US2 #9). Otherwise it posts a waking `submodule_merged` inbox event to the session, with
   `repo_id` set to the submodule's repository, its `path`, `pushed_sha` and `merged_sha`. It does not archive the
   session.
3. **The run.**
   - The drain starts a run as for any event DAIV wakes on its own. It acts as the session's owner, on the session's
     branch. When that branch is gone because the parent's merge request merged, the run falls back to the default
     branch.
   - The executor passes `CheckoutOptions.pins` (`{path: (pushed_sha, merged_sha)}`). Where the recorded commit equals
     `pushed_sha`, `RepoCheckout` checks the submodule out at `merged_sha`, and the parent's `record` takes it (R16).
     Otherwise it leaves the submodule alone and notes that the branch no longer records DAIV's commit.
   - The parent's publish then updates its open merge request, or opens a new one from the default branch (US2 #7,
     #8).
   - The event renders as a DAIV-written prompt that tells the model what the harness did, and the model's reply
     says so in the session.
   - Turn-end persistence clears the row's `ref` and merge request.
4. **Nested submodules.** When the parent is itself a submodule, its open merge request is updated, and the
   superproject records the parent's new head in the same publish. When the parent's own merge request merges later,
   this repeats.

**Rationale**:

- The merge callback is where DAIV already learns of merges. The inbox is how DAIV makes a session act on its own,
  with one run per session and the requester rules already in place.
- Checking ancestry first avoids a run for each merge-commit merge.
- The pointer change itself is the harness's work, deterministic and tested. The model only reports it.

**Alternatives considered**:

- A turn with no model call, rejected: no inbox kind does deterministic work instead of a run today, so it would be a
  new mechanism beside the existing one.
- Re-pointing at every merge, rejected because merge commits would cost a run that changes nothing.
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
- **Fields:**
  - `session` (`related_name="submodules"`);
  - `parent_repo_id`: the session repository whose checkout holds it;
  - `path`: from that repository's root;
  - `repo_id`;
  - `ref`;
  - `pushed_sha`;
  - `merge_request_iid`, `merge_request_web_url`, `merge_request_title`, `merge_request_draft`;
  - `diff_stats`;
  - timestamps.

  It is unique on `(session, repo_id)`, which is FR-024's one checkout per repository, and indexed on
  `(repo_id, merge_request_iid)`.
- **Written:**
  - by the executor right after the checkout and before the agent starts, one row per checked-out submodule whose
    repository is not already the session's (FR-027);
  - at turn end, by `apersist_submodule_state` from the `RepoPublish` entries that carry a `submodule_path`.
- **Readers that change:**
  - **Visibility:** `_readable_repos_q` also requires read access to every submodule row (FR-027, SC-008).
  - **Memory:** the skip asks a new `Session.aholds_other_repositories()`, which is true with an extra repository or a
    submodule row.
  - **Merge lookup:** `on_merge_request` matches submodule rows too, for the merge request filter and R21.
  - **Session page:** `page_state` shows a card per submodule row that has a merge request, labelled with its path
    and parent.
  - **Executor:** it reads the rows for R19's branches and R21's pins.
  - **Inbox drain:** its access check for non-mention events also requires read access to submodule rows, as it
    requires write access to extra repositories.

**Rationale**: a separate model keeps "a `SessionRepository` row is an extra repository" true, so the existing
readers stay correct without a filter, and a reader added later cannot mistake a submodule for one.

## R23. Linking the merge requests (FR-022)

**Decision**:

- `RelatedMergeRequest` gains a `relation`: `sibling` (today's), `merges_first` (a submodule's merge request, shown on
  the merge request of the repository that records it) or `recorded_by` (the reverse).
- `render_related_block` writes one group per relation inside the existing marker block. On the parent:
  `**Merge first** (submodules this merge request points at):`. On the submodule: `**Recorded by**:`. The siblings
  keep today's header.
- `_alink_related` runs when the turn published to more than one repository, submodules included, not only when the
  session has extra repositories.
