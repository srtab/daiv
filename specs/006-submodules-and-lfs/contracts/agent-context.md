# Contract: What the agent and the person see

Two texts change. Both render only for a repository with `checkout.submodules` or `checkout.lfs` on, so every other
run's prompt and reply stay byte for byte as today (FR-016). Wording here is the plan's draft; the implementation
owns the final text, and the tests pin its structure, not its prose.

## 1. The system prompt's git context (FR-009)

`GIT_SYSTEM_PROMPT` (`automation/agent/middlewares/git.py`) gains one section per repository whose checkout report is
not empty, after the repository's lines and in the same order as the repositories.

```text
## Submodules and LFS files in `<repository>`

- Submodules checked out: `libs/core`, `libs/core/vendor/fmt`.
- Not checked out (the directory is empty):
  - `libs/billing` (`group/billing`): the person this run belongs to can't read that repository.
  - `third_party/zlib` (`https://github.com/madler/zlib`): it is outside this git platform.
- LFS files not downloaded (they hold their pointer text): `assets/model.bin` (over the LFS download limit), and 12 more.
- Changes inside submodules and to LFS-tracked files, new ones included, are not published: the harness leaves them out
  of the commit and says so when your turn ends. To change a submodule's code, ask the user to open its repository as
  an extra repository of a chat session.
```

**Rules**:

- The "checked out" line lists at most 20 paths, then a count.
- The "not checked out" list is complete; each entry gives the path, the repository path or the URL with user info
  stripped, and one reason phrase.
- The LFS list names at most 20 files, then a count, and appears only when the LFS setting is on and a file was left
  out.
- The last bullet always renders when either setting is on.
- No token, credential, raw git error or setting value ever appears (FR-009, errors rule 5).

**Reason phrases** (`SkipReason`):

| Value | Phrase |
|---|---|
| `outside_platform` | it is outside this git platform |
| `daiv_no_access` | DAIV's account can't read that repository |
| `person_no_access` | the person this run belongs to can't read that repository |
| `commit_missing` | the commit this branch records no longer exists in that repository |
| `over_limit` | over the LFS download limit |
| `fetch_failed` | the download failed |

## 2. The turn-end notice (FR-013)

When a publish keeps anything out, the turn ends with a visible assistant message, like today's merge-conflicts
notice. It is added for the main repository and for each extra repository, naming the repository when the session has
more than one.

```text
I didn't publish some of my changes because DAIV doesn't publish them for this repository:
- Inside the submodule `libs/core`: `src/parser.c`, `include/parser.h`.
- The submodule `libs/ui` now points at a different commit.
- The submodule `libs/old` was removed or moved.
- LFS-tracked files: `assets/logo.png`, `data/new.bin`.
To change a submodule, open its repository as an extra repository of a chat session.
```

**Rules**:

- The notice renders only when `KeptOut` is non-empty.
- It accompanies a successful publish of the rest; it is never the reason a turn fails.
- When nothing else changed, the turn publishes nothing and the notice is the turn's only report of those changes.
- The chat's publish result event is unchanged: `status` reflects what was pushed.
