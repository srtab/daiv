# Quickstart: validating submodules and Git LFS

Phase 1 of [plan.md](plan.md). How to show the feature works end to end on the local stack, after the unit tests
pass. The settings are in [contracts/repository-config.md](contracts/repository-config.md); the agent's and the
person's texts are in [contracts/agent-context.md](contracts/agent-context.md).

## Prerequisites

- The local stack running with this branch's image: `make up` (the stack includes GitLab and the sandbox). The image
  must hold `git-lfs`: `docker compose exec worker git lfs version`.
- A DAIV user signed in with GitLab, who is a member of the projects below except `private-lib`, and a second GitLab
  account that is a member of `app` only.
- `git` and `git-lfs` on the machine used to push the fixtures.

## Fixture repositories (local GitLab)

1. `demo/lib`: one file `lib.h`, and a nested submodule `vendor/inner` pointing at `demo/inner` with a relative URL
   (`../inner.git`).
2. `demo/inner`: one file `inner.c`.
3. `demo/private-lib`: one file. The DAIV user is **not** a member.
4. `demo/app`, with:
   - submodules `libs/lib` → `../lib.git`; `libs/lib-ssh` → `git@<gitlab-host>:demo/lib.git`;
     `libs/private` → `../private-lib.git`; `third_party/zlib` → `https://github.com/madler/zlib.git`;
   - `git lfs track "*.bin"`, with `assets/small.bin` (1 MiB) and `assets/big.bin` (300 MiB);
   - on the default branch, `.daiv.yml` with `checkout: {submodules: true, lfs: true}`.

Wait for the repository access sync (or run it), so that DAIV's access mirror lists the members.

## Scenarios

| # | Do | Expect | Covers |
|---|---|---|---|
| 1 | Chat on `demo/app` with a sandbox environment: "List the files in every submodule and print the size of each file in `assets/`." | `libs/lib`, `libs/lib-ssh` and `libs/lib/vendor/inner` are listed with their files. `libs/private` and `third_party/zlib` are empty, and the prompt's section names them with the reasons "the person … can't read" and "outside this git platform". `assets/small.bin` is 1 MiB; `assets/big.bin` holds pointer text and is listed as over the limit. | US1 #1, US2 #1, US3 #1–3 |
| 2 | Same session: "Run `git status`." | Clean: no submodule, no LFS file. | US1 #2, US2 #2, FR-011 |
| 3 | "Add a line to `README.md`, to `libs/lib/lib.h` and to `assets/small.bin`, and create `assets/new.bin`." | The merge request changes `README.md` only, and no submodule commit. The turn ends with the notice naming `libs/lib` (`lib.h`), `assets/small.bin` and `assets/new.bin`. | US1 #3, US2 #3, FR-012, FR-013 |
| 4 | New chat: "Add a line to `libs/lib/lib.h` only." | No commit, no merge request, no error; the notice names the change. | US1 #4, FR-013 |
| 5 | On the merge request from 3, push a commit that moves `libs/lib` to a newer `demo/lib` commit, then ask DAIV for another change to `README.md` in the same chat. | The new push keeps the moved `libs/lib` commit. | US1 #5, FR-014 |
| 6 | Repeat 1 and 3 with no sandbox environment (a disk run). | Same results, read through the file tools. | FR-002, layering rule 4 |
| 7 | Label an issue on `demo/app` `daiv` from the second GitLab account. | Its run checks out none of the submodules that account can't read. | FR-005 |
| 8 | Set `CODEBASE_LFS_MAX_CLONE_BYTES=0`, restart the worker, repeat 1. | Every LFS file is listed as over the limit. | FR-007 |
| 9 | Remove `checkout` from `.daiv.yml` and repeat 1 and 3. | Empty submodule directories, pointer text, no new prompt section, no notice; the merge request is what it is on `main` today. | US1 #6, US2 #4, FR-016 |
| 10 | Revoke DAIV's account from `demo/lib`, repeat 1. | `libs/lib` is listed as "DAIV's account can't read that repository", and the worker log has an ERROR naming `demo/lib` and no token. | US3 #4, FR-010 |
| 11 | Repeat 1 on GitHub with the equivalent fixture repositories (`owner/app`, …) and the app installed on them. | The same results. | FR-017 |

## Commands for the record

```bash
make lint
make lint-typing
make test
uv run --only-group=docs mkdocs build --clean --strict
```

The sandbox copy (SC-006) is checked by a unit test on `_make_repo_archive`, and manually in scenario 1 with
`du -sh /workspace/repo/.git` inside the sandbox: it holds no `lfs` directory.
