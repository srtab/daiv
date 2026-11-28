# Contract: What the agent and the person see

Four texts change. The first two render only for a repository with `checkout.submodules` or `checkout.lfs` on, and
the last two only once DAIV has published a submodule. Every other run's prompt, reply and merge request stay byte for
byte as today (FR-016). Wording here is the plan's draft; the implementation owns the final text, and the tests pin
its structure, not its prose.

## 1. The system prompt's git context (FR-009)

`GIT_SYSTEM_PROMPT` (`automation/agent/middlewares/git.py`) gains one section per repository whose checkout report is
not empty, after the repository's lines and in the same order as the repositories.

```text
## Submodules and LFS files in `<repository>`

- Submodules whose changes are published as their own merge request, with this repository updated to point at it:
  - `libs/core` (`group/core`, into `main`).
  - `libs/core/vendor/fmt` (`group/fmt`, into `main`): continues merge request `group/fmt!7` on its branch.
- Submodules checked out read-only (changes there are not published):
  - `libs/ui` (`group/ui`): the person this run belongs to can't write to that repository.
  - `libs/shared` (`group/shared`): that repository is published from `/workspace/repos/group/shared`.
- Not checked out (the directory is empty):
  - `libs/billing` (`group/billing`): the person this run belongs to can't read that repository.
  - `libs/vendor` (`other/vendor`): DAIV doesn't work on that repository.
  - `third_party/zlib` (`https://github.com/madler/zlib`): it is outside this git platform.
- LFS files not downloaded (they hold their pointer text): `assets/model.bin` (over the LFS download limit), and 12 more.
- Changes to LFS-tracked files, adding or removing a submodule, and changes inside read-only submodules are not
  published: the harness leaves them out of the commit and says so when your turn ends. Don't commit inside a
  submodule; the harness does that for the published ones.
```

**Rules**:

- The published list gives, per submodule, its path, its repository and the merge request's target branch. A
  submodule on its working branch names its merge request. A note from R19 replaces that: "its branch no longer
  contains the commit this repository records, so changes there open a new merge request".
- Each read-only entry gives the path, the repository and one `PublishBlock` phrase.
- Each "not checked out" entry gives the path, the repository path or the URL with user info stripped, and one reason
  phrase.
- Each of the four lists stops at 20 entries, then "and N more"; the LFS list appears only when the LFS setting is on
  and a file was left out.
- The last bullet always renders when either setting is on; its parts follow the settings that are on.
- No token, credential, raw git error or setting value ever appears (FR-009, errors rule 5).

**Reason phrases** (`SkipReason`):

| Value | Phrase |
|---|---|
| `outside_platform` | it is outside this git platform |
| `not_catalogued` | DAIV doesn't work on that repository |
| `daiv_no_access` | DAIV's account can't read that repository |
| `person_no_access` | the person this run belongs to can't read that repository |
| `commit_missing` | the commit this branch records no longer exists in that repository |
| `over_limit` | over the LFS download limit |
| `fetch_failed` | the download failed |

**Block phrases** (`PublishBlock`):

| Value | Phrase |
|---|---|
| `published_elsewhere` | that repository is published from `<workspace path>` |
| `parent_not_published` | the submodule that contains it is read-only |
| `person_no_write` | the person this run belongs to can't write to that repository |

## 2. The turn-end notice (FR-013, FR-021, FR-025)

When a publish keeps anything out, a submodule's publish failed, or a pin was applied or left, the turn ends with a
visible assistant message, like today's merge-conflicts notice. It is added for the main repository and for each
extra repository, naming the repository when the session has more than one.

```text
I didn't publish some of my changes:
- Inside the read-only submodule `libs/ui` (the person this run belongs to can't write to that repository): `src/button.ts`.
- The submodule `libs/old` was removed or moved, which DAIV doesn't publish.
- `tools/vendored` was added as a submodule, which DAIV doesn't publish.
- The submodule `libs/core` is still at the commit the branch's latest push set: someone moved it while I worked, and their commit wins. My changes there are in `group/core!12`.
- The submodule `libs/fmt` couldn't be published, so this repository still points at its old commit.
- LFS-tracked files: `assets/logo.png`, `data/new.bin`.
```

A `submodule_merged` run adds one of these lines, in a message of its own when nothing else is reported:

```text
Updated the submodule `libs/core` to `1a2b3c4`, the commit `group/core!12` was merged as.
Left the submodule `libs/core` as it is: this branch no longer records the commit DAIV pushed to `group/core!12`.
```

**Rules**:

- The notice renders only when `KeptOut` is non-empty, a submodule's `RepoPublish` has an error, or a handle has a
  `pinned` commit or a pin `note`.
- It accompanies a successful publish of the rest; it is never the reason a turn fails.
- When nothing else changed, the turn publishes nothing and the notice is the turn's only report of those changes.
- A published submodule is not in the notice: its merge request is reported like an extra repository's, through the
  publish result events and the session page.
- The chat's publish result event is unchanged: `status` reflects what was pushed.

## 3. The prompt of a `submodule_merged` run (FR-025)

A run woken only by `submodule_merged` gets a DAIV-written prompt, rendered from the event alone, as a CI-only run's
is today (`sessions/inbox/render.py`):

```text
The merge request `group/core!12` for the submodule `libs/core` was merged as commit `1a2b3c4`, which doesn't contain
the commit this repository records. The harness records `1a2b3c4` for `libs/core` when your turn ends, if this
branch still records DAIV's commit, and reports the outcome itself. Change nothing else; reply in one sentence that
the submodule's merge request was merged.
```

The outcome is the notice of §2, which the harness writes from the checkout. The prompt never claims what the run
will find.

## 4. The merge request descriptions (FR-022)

The existing related block (`<!-- daiv:related-merge-requests -->` … `<!-- /daiv:related-merge-requests -->`) gains
groups, computed per merge request from each entry's `parent_repo_id`. On a merge request whose repository records
published submodules:

```markdown
**Merge first** (submodules this merge request points at):
- group/core!12
```

On a submodule's merge request:

```markdown
**Recorded by**:
- group/app!34
```

In a nested chain, the middle merge request shows both groups: its child under **Merge first** and its parent under
**Recorded by**. Today's `**Related merge requests** (same DAIV session):` group keeps listing every other merge
request of the session. A block with one group renders only that group. References use the platform's syntax (`!` on
GitLab, `#` on GitHub), so each platform links and titles them for readers who can see them.
