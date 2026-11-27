# Contract: Configuration

What maintainers and operators set. The reference pages own the final wording
(`docs/customization/repository-config.md`, `docs/reference/env-variables.md`);
`tests/unit_tests/test_docs_reference.py` and `test_env_variables_doc.py` fail until both keys are there.

## `.daiv.yml`

```yaml
checkout:
  submodules: true   # default false
  lfs: true          # default false
```

| Option | Type | Default | Behaviour |
|---|---|---|---|
| `checkout.submodules` | `bool` | `false` | DAIV checks out the repository's submodules, nested ones included, at the commits the branch records. It fetches only submodules on the same git platform host, in repositories DAIV works on, and only those the run's person can read. Changes inside a submodule whose repository the person can write become a merge request there, and this repository's merge request points the submodule at it. Changes inside other submodules are not published. |
| `checkout.lfs` | `bool` | `false` | DAIV downloads the content of LFS-tracked files, in the repository and in the submodules it checked out, up to the operator's limit. Changes to LFS-tracked files, new ones included, are not published. |

- The file is read from the default branch, so a merge request cannot turn either setting on for its own runs.
- Each repository of a session (main and extra) follows its own file. A submodule's own `.daiv.yml` gives its
  default branch, omitted content and context file name when DAIV publishes it, as for an extra repository; its
  `checkout` section is not read, since nested submodules follow the repository that started the clone.
- As elsewhere in `.daiv.yml`, an unknown key under `checkout` is ignored. An invalid value makes DAIV log the error
  and use the default configuration for the whole file, so both settings are off.

- On GitLab, each submodule's project needs DAIV's webhook, as every project DAIV works on does, so DAIV learns when
  a submodule's merge request merges (research R21).

## Environment

| Variable | Type | Default | Behaviour |
|---|---|---|---|
| `CODEBASE_LFS_MAX_CLONE_BYTES` | integer, bytes | `268435456` (256 MiB) | The most LFS content one clone downloads, summed over the repository and its submodules. Files past it keep their pointer text and are reported to the agent. `0` downloads no LFS content for any repository. |

## Deployment

DAIV's image ships `git-lfs`. An operator who builds their own image adds the `git-lfs` package; without it, a
repository with `checkout.lfs: true` reports every LFS file as a failed download, and the operator log names the
missing program. A sandbox image needs no `git-lfs`.
