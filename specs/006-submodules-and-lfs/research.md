# Research: Submodules and Git LFS

Phase 0 of [plan.md](plan.md). Each item resolves one design question the spec leaves open. "Verified" marks a
claim checked by experiment on git 2.43 and git-lfs 3.4 while planning; the experiments are summarised where they
matter.

## R1. The checkout completes in `codebase.clients`, after the clone

**Decision**: `RepoClient.load_repo` keeps its signature and still clones only the superproject. A new class,
`RepoCheckout` in `daiv/codebase/clients/checkout.py`, completes a clone: it checks out submodules and fetches LFS
content, and returns a `CheckoutReport`. `RepoClient.complete_checkout(repo, repository, options, read_access)` builds
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
`.git/config` to `<scheme>://<host>/<path>.git`, so every fetch goes over the same HTTPS transport as the main clone.
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

**Decision**: the run executor builds a `RepoReadAccess` predicate (`repo_id -> bool`) from the run's person, as
recorded on its `Run` row, and passes it to `set_runtime_ctx`:

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
3. refreshes the index (`git update-index -q --refresh`) with the `filter.lfs.*` config in the command's environment,
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
  - submodules: `git status --porcelain=v2 -z`, read for submodule entries (`S...`);
  - files inside each changed submodule: `git -C <path> status --porcelain -z`, capped at 20 paths per submodule in
    the notice.
- **`commit_all(message, keep_out=...)`**: runs `git add -A`, then `git --literal-pathspecs reset -q -- <gitlink paths
  and kept-out LFS paths>`, then `git commit`. `reset` restores those paths' index entries from `HEAD`. That covers an
  edited, new, deleted or renamed LFS path, and a moved gitlink. The kept-out paths are the agent's few edits, so the
  argv stays short.
- **FR-014**: `_integrate_remote` rebases or merges the run's commits, which never touch a gitlink, onto the remote,
  so the result keeps the remote's gitlink. The submodule's checkout then lags the index. The next publish's `reset`
  of gitlink paths keeps that lag out, where `git add -A` would otherwise revert the remote's bump.

`PublishOutcome` and `RepoPublish` gain `kept_out: KeptOut | None`. `GitMiddleware` renders it as a visible notice
beside the merge conflicts notice, for the main repository and each extra repository. Draft recovery
(`sessions/executor/recovery.py`) uses the same publisher, so it applies the same guard.

**Alternatives considered**: `submodule.<name>.ignore=all` written into the clone's config, rejected because git
commands differ in which of them honour it. Excluding paths with pathspecs on `git add`, rejected because `attr`
magic is not supported there.

## R9. What the agent is told (FR-009)

**Decision**: `GIT_SYSTEM_PROMPT` gains a section per repository with either setting on. It lists:

- which submodules were checked out, and that changes inside them and to LFS-tracked files are not published;
- each submodule or LFS file left out, with its reason;
- the route for changing a submodule: open its repository as an extra repository of a chat session.

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
- Publishing LFS files and submodule changes: later features, per the clarification.
