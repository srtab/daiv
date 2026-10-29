# Quickstart: Validate configurable skill and subagent models

## Prerequisites

- A local DAIV stack with at least two enabled providers, or two models on one provider, called
  `M_RUN` (the run's agent model) and `M_SKILL` below.
- A test repository connected to DAIV, with permission to commit a `.daiv.yml` to its default
  branch.
- An admin account for site configuration.

## Automated checks

```bash
make lint
```

```bash
make lint-typing
```

```bash
make test
```

The unit tests listed in [plan.md](plan.md#source-code-repository-root) cover:
- precedence;
- fallback without skipping a subagent;
- the notices;
- the form rules in [contracts/site-configuration.md](contracts/site-configuration.md).

## Manual scenarios

1. **Site entry applies to a skill (User Story 1).**
   1. In site configuration, under Agent → Skill and subagent models, add `code-review` → `M_SKILL`
      and save.
   2. Ask for a code review in the test repository.
   3. In the session's usage panel, `M_SKILL` appears beside `M_RUN`, and the review's calls after
      the skill started are billed to `M_SKILL`.
2. **Site entry applies to a detector (User Story 2).** Add `cr-security` → `M_SKILL` and run a
   review. The security detector's calls are billed to `M_SKILL`; the other detectors stay on
   `M_RUN`.
3. **Repository override (User Story 3).**
   1. Commit `models.skills.code-review: M_RUN` to the test repository's `.daiv.yml`.
   2. Run a review there: its skill calls use `M_RUN`.
   3. Run one in another repository: it still uses `M_SKILL`.
4. **Exact pin does not win (clarification Q1).** Start a run pinned to `M_RUN` through the jobs
   API (`model_names`) that asks for a code review. The skill's calls use the configured
   `M_SKILL`.
5. **Unusable entry falls back (FR-009).**
   1. Set `models.subagents.cr-tests: nosuchprovider:model` in `.daiv.yml` and run a review.
   2. Check the outcome:
      - the run completes;
      - `cr-tests` still runs, on `M_RUN`;
      - the worker log has an ERROR naming `cr-tests`;
      - the session view shows a notice chip naming `cr-tests`, the spec that failed and `M_RUN`;
      - with the chip on the page, the to-do panel and the outputs list still render;
      - nothing extra is posted to the merge or pull request.
6. **Form rules (FR-008, FR-008a).** In site configuration, try each case:
   - a model on a disabled provider: refused, with an error on that row;
   - a name such as `my-repo-skill`: saved, with a warning.
7. **Environment lock.** Set `DAIV_AGENT_SKILL_MODELS='{"code-review": "<M_SKILL>"}'` and
   restart. The section is read-only and shows the environment value.
