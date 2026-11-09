# Quickstart: validating delegated leg follow-ups

This guide shows the feature working end to end. Shapes and texts are in the [contracts](contracts/), entities in the
[data model](data-model.md); this guide only refers to them.

## Prerequisites

- This branch, rebased on a `main` that holds the `delegation` eval suite (PR #1780; research R17).
- `uv sync` done; Docker for `make up`.
- For the manual scenarios: the local stack with its GitLab (`make up`; `CONTRIBUTING.md`), a repository with CI, and
  orchestration on for it (`orchestration.enabled` in `.daiv.yml`, `docs/customization/repository-config.md`).
- For evals: `docker/local/app/config.secrets.env` with an LLM key, as for `make integration-tests`.

## 1. Gates

```bash
make lint-fix && make lint     # includes import-linter: no new contract or ignore_imports
make lint-typing
make test
```

Every test listed under each scenario below must fail on `main` and pass here (`patterns/tests.md` rule 1).

## 2. Unit scenarios

Run them focused while working:

```bash
uv run pytest tests/unit_tests/sessions/test_legs.py tests/unit_tests/sessions/test_delegation.py \
  tests/unit_tests/sessions/inbox/ tests/unit_tests/sessions/test_queue.py tests/unit_tests/sessions/test_services.py \
  tests/unit_tests/sessions/test_header.py tests/unit_tests/sessions/test_transcript.py \
  tests/unit_tests/automation/agent/middlewares/test_delegate_jobs.py tests/unit_tests/chat/test_tool_renderers_js.py -q
```

| Scenario | Spec | Expected |
|---|---|---|
| A follow-up to a leg that published its merge request | US1-1, FR-002/003, SC-004 | a new run in the leg's session; enqueued with `fork_branch=False`; no new session or run elsewhere |
| A follow-up to a leg that published nothing, from the default branch or the coordinator's | US1-2/3, R9 | enqueued with `fork_branch=True` |
| A leg with its own branch whose checkpoint lost its merge request | edge case, R9 | still `fork_branch=False` |
| Mixed call: one new leg and one follow-up | US1-5, FR-006 | one `batch_id` on both runs; one `delegation_finished` event after both end |
| A follow-up on an idle leg session that is denied access at take | FR-005a, R6 | its run FAILED with the `access_denied` label; the batch resumes; the summary says "failed" without error text |
| A follow-up whose run fails | FR-011a, R7 | event `failed` with `run_failed`; no second run is created; the batch resumes once |
| Every refusal, alone and beside a valid target | US2, FR-008/009 | one `FailedLeg` with `thread_id` and the contract's text per refusal; the valid target starts |
| The same leg named twice | FR-010 | `{"error": ...}`; no event, no run |
| A follow-up to a busy leg session, behind a CI-fix run and a person's chat from the same user | US3-1, FR-004 | no second active run; the follow-up's run takes only its own waking event plus the remembered ones; chat and CI get runs of their own |
| A follow-up waiting while a push is remembered | US3-2 | the run's input carries the `branch_pushed` context |
| Cancel a waiting follow-up with a CI failure also waiting | US3-4, FR-013, SC-006 | its event `dismissed`, its run cancelled; the CI event is still `pending` and still wakes |
| A batch of only follow-ups, all cancelled before starting | US3-6 | no `delegation_finished` event |
| The merge request merged between send and take | FR-008a, R5 | event `failed` (`mr_merged`); run FAILED, `never_started`; status word "not run: its merge request was merged"; the batch resumes; the summary has the "delegate a new leg" line |
| A leg at three DAIV-started runs in a row | FR-004a, R8 | the follow-up still runs; afterwards a CI failure is remembered with `run_limit` |
| A reply of 2,600, 4,000 and 4,500 characters | US4, FR-016, SC-007 | whole, whole, cut plus the marker, in the resume and in `check_delegated_jobs(batch_id)`; 1,000 in the overview |
| The batch status of a follow-up that started on the leg's own branch | FR-014 | `branch` is the leg's branch; `ref` is where the leg started |
| A coordinator page with a leg in two batches | US5-2, FR-019 | `total` counts it once; both batches list it |
| The card for a call with a follow-up target and a refused one | US5-3, FR-020 | rows matched by `thread_id`, labelled "leg follow-up"; the refusal is a row with its error |

## 3. Manual end-to-end (local stack)

1. Open a chat session on a repository with CI. Ask DAIV to change two repositories in a way that leaves one leg's
   tests failing (for example, a prompt that forbids touching the test fixtures the change needs).
2. Wait for the resume. On the coordinator's page:
   - the summary names each leg's session id;
   - the call card lists both legs.
3. Expect the coordinator to call `delegate_jobs` with the failing leg's `thread_id`. Check that:
   - the leg's session shows a second run, continuing the first;
   - its commits land on the leg's branch and existing merge request, and no new merge request appears;
   - the coordinator is resumed once more, with "(leg follow-up)" on that leg's entry;
   - "Delegated jobs" in the details panel counts two sessions, and the first batch and the second both list the
     followed-up leg.
4. Merge the other leg's merge request in GitLab. Ask the coordinator to follow that leg up.
   - Expected: it is refused at once with "…was merged: delegate a new leg…".
5. Ask for a follow-up to the remaining leg while chatting in that leg's session, so it is busy.
   - Expected: the follow-up waits in the leg's inbox panel as "Leg follow-up", then runs alone after the chat turn
     ends.
   - Cancel it from the coordinator before it starts. Expected: it is withdrawn and the chat continues unaffected.

## 4. Evals (Constitution V)

```bash
# on main, with the delegation suite merged
make eval-prompts SUITES=delegation OUT=eval-runs/before.jsonl
# on this branch
make eval-prompts SUITES=delegation OUT=eval-runs/after.jsonl
uv run evals/compare_runs.py eval-runs/before.jsonl eval-runs/after.jsonl
```

Expected (SC-001 to SC-003):

- **The five baseline cases**: no `regressed` verdict, and any `unconfirmed` verdict confirmed with the printed
  `CASES=` re-runs and `--confirm`.
- **`test_a_finding_deep_in_a_legs_reply_reaches_the_report`**: flips to passing.
- **The two new cases** (`…gets_a_leg_follow_up`, `…gets_a_new_leg`): pass in a majority of three votes each. They are
  new, so `compare_runs.py` lists them without a before.

Paste the `compare_runs.py` table into the pull request. If evals were not run, say so there instead
(`patterns/agent-evals.md` rule 2).

## 5. Docs

```bash
uv run --only-group=docs mkdocs build --clean --strict
```

`docs/features/orchestration.md` has the new "Following up a leg" section ([contract](contracts/session-page.md#docs-docsfeaturesorchestrationmd)).
