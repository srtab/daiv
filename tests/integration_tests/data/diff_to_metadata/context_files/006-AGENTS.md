# Repo conventions

## Branch naming
We use different branch prefixes depending on change type:

- Features: feat/<kebab-summary>
- Bug fixes: bug/<issue-key>-<kebab-summary>
- Chores: chore/<kebab-summary>

Notes:
- <issue-key> must be lowercase (e.g., cal-204)
- <kebab-summary> must be lowercase ASCII, digits allowed, hyphens only (no underscores)

## Commit messages
Also differs by change type:

- Bug fixes: [<ISSUE-KEY>] <Summary>
  - <ISSUE-KEY> must preserve original casing (e.g., CAL-204)
  - Summary must be short, Title Case, no trailing period
- Features/chores: Conventional Commits subject line, e.g. "feat: add X", "chore: bump Y"

## PR description
If tests are not changed in the diff, explicitly say "Not shown in diff."
