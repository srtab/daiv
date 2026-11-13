# Code-review tool diagnostics

Set `DAIV_EVAL_TRACE_OUT` to an output JSONL path when running a selected
`test_code_review_recall` case. The eval adds a local callback to its agent
invocation and appends one row when the invocation finishes or raises.
With the variable unset, the agent config and eval behavior are unchanged.
The destination is checked before the agent starts.

Each row contains the case's pytest node ID, `DAIV_EVAL_RUN`, model name,
tool arguments, returned content, tool errors, and tool messages observed
by the model-start callback. Run ancestry associates nested calls with
`task(subagent_type=...)`, including concurrent detectors. Detector summaries
returned as `Command` messages retain their source detector. Repeated tool
invocations remain separate events; repeated model-visible messages with
the same detector, call ID, and content are recorded once.

The recorder omits injected runtime objects and non-tool model messages.
It does not call a model, upload traces, change prompts, relax the loop
breaker, or alter grading. The callback records model-call messages, rather
than the provider's serialized HTTP request. Use a separate output path per
pytest process and retain traces with their metrics and provider budget
ledger in ignored `eval-runs/` artifacts.

For a budget-guarded invocation, add:

```bash
export DAIV_EVAL_TRACE_OUT="$PWD/eval-runs/read-diagnostic-trace.jsonl"
```

Use the existing fixed provider routing and spending guard. Selecting a
single clean case avoids a paid judge call; the judge calibration test is
a separate node and must not be selected for a read-only diagnostic.
Do not use diagnostic rows as replacement baseline votes.

## Before changing recall prompts

1. Reproduce the read behavior locally with a scripted model and the actual
   sandbox backend. Confirm the returned window and next offset, then compare
   repeated first-page calls with correctly advancing calls.
2. Audit saved clean reports against their frozen source and repository rules.
   Distinguish failed workflows, unavailable detectors, incorrect premises,
   genuine fixture omissions, and debatable priority. Preserve original grades.
3. Capture one previously failing case under a separately authorized cap.
   Compare returned read content with model-call tool messages and the next
   arguments. A successful scripted probe does not establish what a historical
   model saw or why it repeated a call.
4. Fix a confirmed cause and verify completion before a matched recall A/B.
   For that A/B, change detector candidate emission and aggregation filtering
   together; keep candidate and lookup budgets, fixtures, model/provider,
   judge, output limits, and grading constant.

## Audit snapshot: 2026-10-11

The incomplete baseline at `5bb99ee4cb6481a5fbb16e18fa3f6b30cb3f094a`
has ten graded clean outcomes: three parent read-loop failures, three degraded
reports, two completed clean reports, and two completed reports with blocking
findings. These are different failure categories; 2/10 passes does not imply
that eight reviews invented a blocking bug. Original grades remain unchanged.

The real sandbox probe on `clean-mute-job` returned
`@@ lines 1-100 of 1037 | next offset 100 @@`. Advancing the offset returned
lines 101–200. Scripted repetition triggered the existing loop breaker.
Historical model inputs were not saved, so this does not establish the cause
of the original failures.

Four blocking findings in the saved clean reports need distinct treatment:

| Finding | Local audit |
|---|---|
| Duplicated newest-run ordering | Shared ordering exists, but the cited helper selects per session, while the patch selects per batch. Blocking priority is unproven. |
| New schedule export has no tests | The patch adds an endpoint without tests. The layout rule alone does not establish blocking priority, but the fixture is not proven clean for every review dimension. |
| Raw run-status labels | `waiting_input` and `successful` differ from the existing human labels `Needs input` and `Done`. The presentation difference is real. |
| Subscriber-driven batch over-fetch | The subscriber-fanout premise is incorrect. At frozen base `f5580ea4d7011f7e005fa46bfc0f3ba935595f69`, `schedules/tasks.py` builds targets from repositories, `sessions/services.py` creates one run per target, and `sessions/validators.py` caps targets at 20. Fetching surplus rows is real, but material slowdown was not measured. |

The ignored audit artifacts retain saved-report positions and SHA-256 hashes.
All 25 original baseline-manifest file hashes were verified unchanged.
The next diagnostic selects only `clean-mute-job`, which failed in the parent
loop in both saved passes, with tool tracing enabled under a separate spend
cap. The free probe and audit made no provider calls.
