# Quickstart: validating submodules and Git LFS

Phase 1 of [plan.md](plan.md). How to show the feature works end to end on the local stack, after the unit tests
pass. The settings are in [contracts/repository-config.md](contracts/repository-config.md); the agent's and the
person's texts are in [contracts/agent-context.md](contracts/agent-context.md).

## Prerequisites

- The local stack running with this branch's image: `make up` (the stack includes GitLab and the sandbox). The image
  must hold `git-lfs`: `docker compose exec worker git lfs version`.
- A DAIV user signed in with GitLab, with the roles below, and a second GitLab account that is a member of `app` only.
- `git` and `git-lfs` on the machine used to push the fixtures.

## Fixture repositories (local GitLab)

| Project | Content | DAIV user | DAIV's account | Merge method |
|---|---|---|---|---|
| `demo/lib` | `lib.h`, and a nested submodule `vendor/inner` → `../inner.git` | Developer | Maintainer | Merge commit with squash allowed |
| `demo/inner` | `inner.c` | Developer | Maintainer | Squash |
| `demo/ui-lib` | `ui.h` | Reporter | Maintainer | Merge commit |
| `demo/ext-lib` | `ext.h` | Developer | Reporter | Merge commit |
| `demo/private-lib` | one file | not a member | Maintainer | Merge commit |
| `demo/app` | see below | Developer | Maintainer | Merge commit |

`demo/app` holds:

- submodules:
  - `libs/lib` → `../lib.git`;
  - `libs/lib-ssh` → `git@<gitlab-host>:demo/lib.git`;
  - `libs/ui` → `../ui-lib.git`;
  - `libs/ext` → `../ext-lib.git`;
  - `libs/private` → `../private-lib.git`;
  - `third_party/zlib` → `https://github.com/madler/zlib.git`;
- `git lfs track "*.bin"`, with `assets/small.bin` (1 MiB) and `assets/big.bin` (300 MiB);
- on the default branch, `.daiv.yml` with `checkout: {submodules: true, lfs: true}`.

Wait for the repository access sync (or run it), so that DAIV's access mirror lists the members and their roles, and
the repository catalog lists the projects where DAIV's account is Maintainer. Run `setup_webhooks` so every catalogued
project sends DAIV its events.

## Scenarios

| # | Do | Expect | Covers |
|---|---|---|---|
| 1 | Chat on `demo/app` with a sandbox environment: "List the files in every submodule and print the size of each file in `assets/`." | Listed with their files: `libs/lib`, `libs/lib/vendor/inner`, `libs/lib-ssh`, `libs/lib-ssh/vendor/inner` and `libs/ui`. `libs/ext`, `libs/private` and `third_party/zlib` are empty. The prompt's section has three lists. Published: `libs/lib`, `libs/lib/vendor/inner`. Read-only: `libs/lib-ssh` ("published from `libs/lib`"), `libs/lib-ssh/vendor/inner` ("published from `libs/lib/vendor/inner`"), `libs/ui` ("can't write"). Not checked out: `libs/ext` ("DAIV doesn't work on that repository"), `libs/private`, `third_party/zlib`, with their reasons. `assets/small.bin` is 1 MiB; `assets/big.bin` holds pointer text and is listed as over the limit. | US1 #1, US3 #1, US4 #1–3, #5, FR-004, FR-024 |
| 2 | Same session: "Run `git status`." | Clean: no submodule, no LFS file. | US1 #2, US3 #2, FR-011 |
| 3 | Same session: "Add a line to `README.md`, `libs/lib/lib.h`, `libs/ui/ui.h` and `assets/small.bin`, and create `assets/new.bin`." | Two merge requests. `demo/lib`'s holds `lib.h`. `demo/app`'s holds `README.md` and moves `libs/lib` to the head of the first one's branch. Their descriptions carry **Merge first** and **Recorded by**. The turn-end notice names `libs/ui` (`ui.h`), `assets/small.bin` and `assets/new.bin`. The session page shows a card for `demo/lib` with its merge request. | US2 #1, #3, US1 #3, US3 #3, FR-012, FR-013, FR-022, FR-026 |
| 4 | New chat: "Add a line to `libs/ui/ui.h` only." | No commit, no merge request, no error; the notice names the change. | US1 #4, FR-013 |
| 5 | Session of 3: "Add a line to `libs/lib/vendor/inner/inner.c`." | A merge request in `demo/inner`. `demo/lib`'s merge request gains a commit recording `vendor/inner` at it, and `demo/app`'s gains one recording `libs/lib` at that. Published innermost first. | US2 #4, #6, FR-021 |
| 6 | On `demo/lib`'s merge request, push a commit by hand. In the session of 3, ask: "Add a line to `README.md`." Then ask: "Add another line to `libs/lib/lib.h`." | After the first request, `demo/app`'s merge request still records DAIV's earlier `demo/lib` commit. After the second, `demo/lib`'s merge request has DAIV's commit on top of the hand-pushed one, and `demo/app`'s records it. | US2 #6, FR-023, edge case "a person pushes" |
| 7 | On `demo/app`'s merge request from 3, push a commit that moves `libs/ui` to a newer `demo/ui-lib` commit. Then ask for another change to `README.md` in the same chat. | The new push keeps the moved `libs/ui` commit. | US1 #5, FR-014 |
| 8 | Squash-merge `demo/inner`'s merge request from 5. | DAIV wakes the session of 3. `demo/lib`'s merge request records `vendor/inner` at the squash commit, and `demo/app`'s records `libs/lib` at `demo/lib`'s new head. The session's reply says so. | US2 #7, FR-025 |
| 9 | Merge `demo/lib`'s merge request with a merge commit, then `demo/app`'s. | No run starts for `demo/lib`'s merge. `demo/app`'s default branch records a commit on `demo/lib`'s default branch. | US2 #9 |
| 10 | New session: repeat 3. Merge `demo/app`'s merge request first, with "Delete source branch" off, then squash-merge `demo/lib`'s. Repeat with the source branch deleted. | Both times DAIV opens a new `demo/app` merge request, from the default branch, that records `libs/lib` at the squash commit. The session stays listed until that merge request merges. | US2 #8, FR-025, edge case "source branch was kept" |
| 11 | Sign in to DAIV as a user linked to the second GitLab account, and open the session URL from 3. Check `demo/app`'s repository memory. | The session is not found. The memory has no observation from that session. | FR-027, SC-008 |
| 12 | Repeat 1 and 3 with no sandbox environment (a disk run). | Same results, read through the file tools. | FR-002, layering rule 4 |
| 13 | Label an issue on `demo/app` `daiv` from the second GitLab account. | Its run checks out none of the submodules that account can't read, and publishes in none. | FR-005, FR-019 |
| 14 | Set `CODEBASE_LFS_MAX_CLONE_BYTES=0`, restart the worker, repeat 1. | Every LFS file is listed as over the limit. | FR-007 |
| 15 | Remove `checkout` from `.daiv.yml` and repeat 1 and 3. | Empty submodule directories, pointer text, no new prompt section, no notice; the merge request is what it is on `main` today. | US1 #6, US3 #4, FR-016 |
| 16 | Revoke DAIV's account from `demo/lib`, repeat 1. | `libs/lib` is listed as "DAIV's account can't read that repository", and the worker log has an ERROR naming `demo/lib` and no token. | US4 #4, FR-010 |
| 17 | Repeat 1, 3, 5 and 8 on GitHub with the equivalent fixture repositories (`owner/app`, …), the app installed on them, and squash merges allowed. | The same results. | FR-017 |

The case of US2 #5 (a submodule's publish fails), R20 (the remote moves a pointer DAIV also moves) and the race of
R21 (the parent merges before the repair is posted) need precise timing, so the unit tests cover them.

## Commands for the record

```bash
make lint
make lint-typing
make test
uv run --only-group=docs mkdocs build --clean --strict
```

The sandbox copy (SC-006) is checked by a unit test on `_make_repo_archive`, and manually in scenario 1 with
`du -sh /workspace/repo/.git` inside the sandbox: it holds no `lfs` directory.
