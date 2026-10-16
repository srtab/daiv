# Code-review recall fixtures

The suite has nine planted bugs and six clean twins. Every patch applies to an immutable public
`srtab/daiv` commit. The agent sees the patched checkout and `/code-review`; planted descriptions
are visible only to the grader. Case wording must pass the prompt-leak guard.

The original six bugs were all found in three GLM-5.2 passes on agent commit `56de6672`.
The three additional pairs exercise context-dependent behavior:

| Bug | Clean twin | Local proof |
| --- | --- | --- |
| `bug-run-mute-false-override` | `clean-run-mute-override` | A `False` run override enables notifications even when the schedule is muted; only `None` inherits. |
| `bug-session-owner-cross-thread` | `clean-session-owner-correlation` | Acting in one thread does not authorize another thread on the same repository. |
| `bug-session-runs-prefetch-deferred-fk` | `clean-session-runs-prefetch` | Three sessions with six runs take two queries to prefetch; deferring `session_id` increases that to eight. |

These pairs share base commit `56de6672c7c3d886363d94577f9ca9859d1d55cc`. They were chosen
before their first paid execution. Their difficulty and recall headroom still need live measurement;
local reproduction proves the defects, not whether a model misses them.

The earlier pairs retain base commit `f5580ea4d7011f7e005fa46bfc0f3ba935595f69`, with three
contract corrections applied to both sides where relevant:

- `mute_job` stores the override for any run status. Classification and notification delivery
  happen asynchronously, so a terminal status does not mean a notification has been sent.
  Already delivered notifications are not retracted. The bug still omits the save's `await`.
- Schedule export derives duplication fields from `ScheduledJob.to_schedule_kwargs()`, converts
  time values to ISO strings, and adds connection overrides and subscriber emails. The clean
  lookup is owner-scoped; the bug's lookup is not.
- Last-run status uses `-created_at, -id` ordering, including the equal-time case. The clean
  implementation batches the query; the bug still queries once per schedule.

`tests/unit_tests/test_integration_code_review.py` applies the shipped patches and executes their
real functions against Django's ORM. It verifies the new defects and the corrected clean contracts
without provider calls or a sandbox. Git history containing both bases is required; CI fetches it.
For live runs, the driver fetches a missing frozen base from the public fixture repository if the
local GitLab mirror is stale. An already available base needs no fetch.

```bash
uv run pytest tests/unit_tests/test_integration_code_review.py \
  tests/unit_tests/test_integration_code_review_grading.py \
  tests/unit_tests/test_integration_prompt_leak.py --no-cov
```

The grader accepts explicit `None` markers in empty blocking sections and ignores an explicit
`Review unavailable for: none` marker. It still rejects unreadable blocking content, missing reports,
and actual detector failures. Clean-run metrics include `clean_violation` alongside severity counts,
noise, and degraded status, so format/degraded failures can be separated from blocking findings.

This case/grader revision invalidates prior before/after comparisons. Preserve old outputs as
historical data, freeze this revision, and collect a fresh baseline before changing detector prompts.
Keep cases, grading, model, and run settings identical on both sides of a prompt experiment.

For a low-cost first check, select just the prefetch bug, one pass, plus the judge sanity control:

```bash
DAIV_EVAL_REPEATS=1 make eval-prompts \
  CASES="tests/integration_tests/test_code_review.py::test_the_judge_tells_the_planted_bug_from_another_in_the_same_file tests/integration_tests/test_code_review.py::test_code_review_recall[bug-session-runs-prefetch-deferred-fk-openrouter:z-ai/glm-5.2]" \
  OUT=eval-runs/recall-prefetch-smoke.jsonl
```

The command uses the standard judge and has no spend cap: provide a capped key or request-level
budget guard before running it on limited credits. A capped smoke may also use GLM-5.2 for the judge
and an output-token ceiling, but those settings must be recorded; its outcome is not a full baseline
or evidence of prompt improvement. Do not expand or repeat paid runs automatically.

Once a budget is available, run three passes over the frozen suite with the standard judge. Require
better bug recall with no increase in clean blocking findings or bug-case noise; qualifying result
flips need at least nine votes per side under `evals/compare_runs.py`. If the expanded baseline is
also saturated, defer the detector change.
