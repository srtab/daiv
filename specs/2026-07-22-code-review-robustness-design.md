# Code-review v3.6.0 robustness spec — B2/B3, B4, B6, B8

> **For agentic workers:** this is a change spec to apply on `feat/code-review-skill-v3-6-0`. Each item states the verified root cause, what already exists (so we harden, not rebuild), the **generic** change, exact files, acceptance tests, and the overfit traps explicitly rejected. Steps use `- [ ]` for tracking.

**Goal:** Make the code-review skill's *execution* robust across models and infrastructure — so the procedure runs, the dedup marker survives, a persistent sandbox outage can't cause thrash, and severity reflects real impact — without any model-specific or case-specific special-casing.

**Architecture:** Four independent changes. B2/B3 hoists non-skippable mandates into the always-loaded router. B4 removes model hand-serialization of the marker by routing the body through a file. B6 converts an existing prompt-based backstop into a code-enforced circuit breaker. B8 adds an exploitability qualifier to the existing deterministic severity mapping.

**Tech stack:** Python 3.14, LangGraph/deepagents middleware, pytest (`pythonpath=daiv`, `asyncio_mode=auto`), the code-review skill under `daiv/automation/agent/skills/code-review/`.

## Global constraints (apply to every item)

- **Generic only.** No branching on model id, no matching a specific error string, no case-specific rules (no "translator-injection = Low", no "if codex …"). Every change must help any model / any repo / any tool.
- **Do not teach the generic `gitlab` tool (`middlewares/git_platform.py`) about `daiv-cr`.** Marker knowledge stays in the skill (`scripts/marker.py`). Tool-level additions must be content-agnostic.
- Never edit `pyproject.toml`; Python 3.14; unit tests cover custom logic only (not framework behavior); tests live under `tests/unit_tests/` mirroring `daiv/`.
- These are the four the user scoped. B1 (sonnet-5 `cache_control` crash), B5 (sonnet cost/noise), B7 (project-tooling verification) are **out of scope** here.

---

## Item 1 — B2/B3: make the review procedure non-skippable (router-level mandate)

**Root cause (verified).** All 11 short-circuited codex runs loaded `SKILL.md` but **never loaded `review-workflow.md`** (`wf=0`): the agent adjudicates the diff directly in the main turn, so it never fans out (B3) and never reaches `gitlab-delivery.md`, so it never delivers / parses notes / reconciles the summary (B2). 21/33 codex threads ran no fan-out; 18/33 posted nothing.

**What already exists.** The mandates are correctly written — Stage-1 fan-out (`review-workflow.md:24`), "delivery empty result does not mean skip delivery" (`review-workflow.md:65`, `gitlab-delivery.md` intro), and v3.6.0's **triage gate** (`review-workflow.md:31`). The defect is *reachability*: every one of these lives one level below the always-loaded router, so an agent that decides it can answer from the diff never encounters them. **The triage gate in particular cannot fire for an agent that skips loading `review-workflow.md`.**

**Generic change.** Promote the two invariants into `SKILL.md`'s "Non-negotiables (every mode)" — the only file guaranteed to be in context — and make "Run the review" state them as gates rather than a soft pointer.

**Files:**
- Modify: `daiv/automation/agent/skills/code-review/SKILL.md`

**Change detail.** Under "## Run the review", reword step 1 and add two non-negotiables:

Add to **Non-negotiables (every mode)**:
- `- **Every run enters `references/review-workflow.md`.** You may not adjudicate the diff from the router alone. The workflow itself decides scope and whether a change is trivial enough for the triage-inline pass (it has that gate) — that decision is not yours to make before loading it.`
- `- **Delivery mode always completes `references/gitlab-delivery.md`, even at zero findings.** "No findings" is not "nothing to deliver": Steps 1/2/6 (parse notes, answer pending replies, reconcile the summary) still run, so the review leaves a record and prior findings/replies are never silently dropped.`

Reword "Run the review" step 1 so entering the workflow is unconditional (it already says "Read … and follow it" — make it "**Always** read … first; do not pre-judge the diff").

**Acceptance criteria (behavioural, checked against future traces / manual runs):**
- A clean-MR delivery run loads `gitlab-delivery.md` and posts (or updates) a summary discussion (even if "no findings"), and runs `parse-notes` at least once.
- A delivery run loads `review-workflow.md` before producing any verdict.

**Test:** this is prompt/doc guidance — no unit test. Add a line to `CHANGELOG.md`. Validation is via a follow-up trace review (see "Verification" at end); the acceptance is that the router now *contains* the mandate, closing the reachability gap.

**Overfit traps rejected:** branching on model id ("if codex, force fan-out"); hard-coding "always spawn 5 detectors" (defeats the legitimate triage gate and re-creates B5 cost). The fix is *reachability of an existing mandate*, model-agnostic.

---

## Item 2 — B4: remove model hand-serialization of the marker (body-via-file)

**Root cause (verified).** The delivery step requires the model to transcribe `marker.py build`'s output into a separate `gitlab … --body "<marker> …"` shell arg. Models re-serialize the marker in their own style — codex emits Python-dict single quotes `{'v':1,…}` even after running `build` and getting correct `{"v":1,…}` (trace `019f895d`, 2026-07-22). `marker.py parse_marker` (`marker.py:80`) parses with `json.loads`, so a single-quote marker raises → the note is dropped from the dedup set → the finding is **re-posted as a duplicate on the next re-review**.

**What already exists.** `gitlab-delivery.md:122/150` already says "capture the `marker.py build` output **verbatim** as the body's first line" — but it's an unenforced instruction the model violates. `parse_marker` already *detects* the corruption and warns to stderr — but still drops it. Neither prevents the duplicate.

**Generic change.** Eliminate the transcription. Add a **content-agnostic** `--body-file <path>` to the `gitlab` tool, and give `marker.py` a subcommand that writes the *complete* post-ready body (marker line + the model's prose) to a file. The model then posts with `--body-file`; it never hand-types the marker. The generic tool stays marker-agnostic (it just reads a file); marker assembly stays in the skill.

**Files:**
- Modify: `daiv/automation/agent/middlewares/git_platform.py` — add `--body-file` handling
- Modify: `daiv/automation/agent/skills/code-review/scripts/marker.py` — add `compose` subcommand
- Modify: `daiv/automation/agent/skills/code-review/references/gitlab-delivery.md` — Steps 3–6 post via composed body-file
- Test: `tests/unit_tests/automation/agent/skills/code_review/test_marker.py`
- Test: `tests/unit_tests/automation/agent/middlewares/test_git_platform.py` (confirm exact path/existence first)

**Interfaces:**
- `marker.py compose --kind <inline|summary|reply> --sha <sha> [--archetype --file --line --anchor] --prose-file <path> [--out <path>]` → writes `"<marker line>\n<prose>"` to `--out` (default `/workspace/tmp/cr-body-<n>.md`), prints the out path. Reuses the existing `build_marker(...)` for the first line; reads prose bytes verbatim; performs no JSON re-encoding of caller text.
- `git_platform.py`: `--body-file <path>` accepted wherever `--body` is, on `discussion create`, `discussion-note create`, and `discussion(-note) update`. Resolution: if `--body-file` present, read the file as UTF-8 and use it as the body; error if both `--body` and `--body-file` are given, or the file is missing. Applies in both `_create_gitlab_inline_discussion` (parse `--body-file` alongside `--body`) and the subprocess path (materialize into `--body` before exec).

**Steps (TDD):**
- [ ] **Step 1 — marker.py `compose` failing test.** In `test_marker.py`, add a test: write a prose file containing a `suggestion` block; call `compose` for an inline finding; assert the output file's first line is exactly `build`'s marker for the same fields (double-quoted JSON) and the remainder equals the prose bytes verbatim; assert the printed path is the out file.
- [ ] **Step 2 — run, verify it fails** (`uv run pytest tests/unit_tests/automation/agent/skills/code_review/test_marker.py -k compose -v`) — Expected: FAIL (unknown subcommand).
- [ ] **Step 3 — implement `compose`** in `marker.py` (argparse subparser + handler reusing `build_marker`).
- [ ] **Step 4 — run, verify it passes.**
- [ ] **Step 5 — round-trip test:** feed the composed body's first line to `parse_marker`; assert it returns the payload dict (proves the posted marker is always dedup-parseable). Run; make pass.
- [ ] **Step 6 — `--body-file` failing test** in `test_git_platform.py`: a `discussion create --body-file <tmp>` posts a body equal to the file contents; `--body` + `--body-file` together → `error:`; missing file → `error:`. Run, verify fail.
- [ ] **Step 7 — implement `--body-file`** in `git_platform.py` (both inline and subprocess paths). Run, verify pass.
- [ ] **Step 8 — docs:** rewrite `gitlab-delivery.md` Steps 3/5/6 to: write prose to a file → `marker.py compose …` → `gitlab … --body-file <out>`. Remove the "type the marker as the first line" instruction; keep `build` documented for callers not using `compose`.
- [ ] **Step 9 — commit** (`feat(code-review): post notes via composed body-file so markers never re-serialize`).

**Acceptance criteria:**
- A finding posted via the documented path always carries a `json.loads`-parseable marker (round-trip test), so it always dedups on re-review.
- `--body-file` works for any body (no `daiv-cr` awareness in `git_platform.py`).

**Overfit traps rejected:** a single→double-quote `sed` on the body; making `parse_marker` accept single quotes (`ast.literal_eval`) — that rewards malformed output and still relies on the model; teaching `git_platform.py` to validate `daiv-cr` markers (couples the generic tool to the skill). The fix removes the transcription step for everyone. *(Optional follow-on, not required here: a `--position-file` by the same pattern would also de-fragilize the loud facet-a `--position` errors.)*

---

## Item 3 — B6: code-enforce the sandbox circuit breaker

**Root cause (verified).** On a persistent sandbox outage the agent keeps calling bash — `019f4660`: ~147 transport failures, **160 bash calls after the first failure, 14 succeeded**; `019f5c10`: 63 post-failure calls. The run still reports `success`, masking a degraded review.

**What already exists.** `sandbox.py` already classifies transport errors (`is_transient_sandbox_error`, `sandbox.py:305–346`), returns a "retry this exact command ONCE" transient message and a "unavailable for the rest of this conversation" permanent message, and the system prompt (`sandbox.py:126`) tells the model to stop after the tool says unavailable **or the same `error:` occurs twice**. The gap: that stop is **prompt-based only** — no code enforces it, and a model ignored it (thrash above). (The thrash traces are from ~2026-07-09, before the classifier landed, but the enforcement gap is still live in current code.)

**Generic change.** Track consecutive transient sandbox failures in run-scoped state; once a threshold of consecutive failures is reached, **short-circuit** further bash invocations for the rest of the run by returning the existing permanent "tool unavailable" message *without* dispatching to the sandbox. A single success resets the counter (so transient blips — e.g. `019f6061`, which recovered — are unaffected). This makes the existing backstop independent of model adherence.

**Files:**
- Modify: `daiv/automation/agent/middlewares/sandbox.py` — consecutive-failure counter in run state + short-circuit gate in the bash tool wrapper
- Test: `tests/unit_tests/automation/agent/middlewares/test_sandbox.py` (confirm exact path first; the branch already added transport-error tests per commit `1a38bef9`)

**Interfaces:**
- Reuse the existing `is_transient_sandbox_error` classifier and the existing permanent-unavailable message constant — do not invent new strings.
- State: a per-run counter (on the middleware's run context / backend, matching how the branch already stores run-scoped sandbox state); increment on transient failure, reset to 0 on any successful command, latch "unavailable" on permanent failure.
- Threshold: a module-level constant (e.g. `_MAX_CONSECUTIVE_SANDBOX_FAILURES = 2`) to mirror the prompt's "two consecutive" rule — a tunable, not a magic per-case value.

**Steps (TDD):**
- [ ] **Step 1 — failing test:** simulate the sandbox raising a transient transport error on N consecutive bash calls; assert call N+1 (N=threshold) returns the permanent-unavailable message **without** calling the sandbox client (mock/patch the client; assert not called on the short-circuited call).
- [ ] **Step 2 — failing test (reset):** transient, then a success, then transient; assert the success resets the counter (no premature short-circuit).
- [ ] **Step 3 — run, verify both fail.**
- [ ] **Step 4 — implement** the counter + gate in the bash wrapper.
- [ ] **Step 5 — run, verify pass; run the existing sandbox test module to confirm no regressions.**
- [ ] **Step 6 — commit** (`fix(agent): code-enforce sandbox circuit breaker after consecutive transport failures`).

**Acceptance criteria:**
- After the threshold of consecutive transient failures, no further sandbox dispatches occur in that run (the counter, not the model, stops it).
- One success between failures resets the counter (transient blips unaffected).

**Overfit traps rejected:** matching the literal `"Sandbox call failed"` string (it's an older message; classify by transport-error type, which the existing classifier already does); hard-tuning retry counts inside the code-review skill (this belongs in the sandbox middleware and benefits every skill). The property is "stop hammering an unavailable tool and surface it," tool- and skill-agnostic.

---

## Item 4 — B8: add an exploitability qualifier to the severity mapping

**Root cause (verified).** v3.6.0 assigns severity **deterministically** (`review-workflow.md:98–107`): **High = any `defect` from `correctness`, `security`, or `custom-rules`**. So a security defect that requires committer access to `.po` files (translator-injection XSS, `019ed100`) or a log-forging CRLF issue is auto-High regardless of how reachable or impactful it is. The inflation is *in the mapping* — there is no reachability/impact input.

**Generic change.** Add a reachability-and-impact qualifier to the High tier, applied uniformly across `correctness`/`security`/`custom-rules`. A defect earns **High** only when it is reachable by a realistic actor/input in the code's actual deployment *and* its impact is material (wrong results, broken authz, data loss, rule violation with real consequence). A defect gated behind privileged/committer access, an unrealistic precondition, or defense-in-depth-only concerns demotes to **Medium** (or **Low** if also local in scope). This stays deterministic and generic — it adds one dimension (reachability×impact) to the existing rule, not a list of special cases.

**Files:**
- Modify: `daiv/automation/agent/skills/code-review/references/review-workflow.md` — the `### Severity` block (`:98–107`)
- Consider: one aligned line in `agents/cr-security.md` and `agents/cr-correctness.md` so the detectors carry the same calibration into their finding rationale (keep it a shared principle, not per-detector special cases)

**Change detail.** Rewrite the High bullet, e.g.:
- `- **High** — a `defect` from `correctness`, `security`, or `custom-rules` that is **reachable by a realistic actor/input in this code's actual use and has material impact** (wrong results, broken authz, data loss, a violated rule with real consequence). A defect that requires privileged/committer access, an unrealistic precondition, or is defense-in-depth only is **not** High — grade it Medium (or Low if the concern is also local in scope).`

Keep Medium/Low as-is (they already grade by scope). Add one calibration sentence: "Reachability and impact, not the detector alone, decide High."

**Acceptance criteria:**
- The rubric requires a realistic-reachability + material-impact judgement before High; the two observed cases (translator-injection requiring repo write access; log-forging with no reproduction path) would grade Medium/Low under the new text.
- Still deterministic in shape (bar × detector × reachability/impact), no enumerated exceptions.

**Test:** doc/prompt change — no unit test; add a `CHANGELOG.md` line. (Optional: add a calibration example to `references/few-shot-examples.md` showing a privileged-access security defect graded Medium — one calibration, per the branch's "one calibration per shape" convention.)

**Overfit traps rejected:** naming specific vulnerability classes ("translator injection = Low", "log-forging = Low"). The fix adds a *general* reachability/impact dimension that any finding is scored on.

---

## Cross-item verification

- `make test` green; `make lint-fix` clean; `make lint-typing` shows no *new* error class (baseline has ~400 pre-existing Django false-positives).
- Marker round-trip (Item 2, Step 5) is the load-bearing regression guard for the duplicate-comment class.
- Behavioural items (B2/B3 router mandate, B8 severity) have no unit test; validate by re-running the skill on a sample of the analyzed MRs (or new ones) and confirming: clean reviews now post a summary + run parse-notes; security defects behind privileged access grade below High. Capture a short before/after trace comparison.

## Sequencing / commits
Four independent commits, any order (B4 has two: marker.py+tool, then docs). Each is independently testable and reviewable. Suggested order: B4 (highest concrete risk — duplicate comments) → B6 (cost/reliability) → B2/B3 (router) → B8 (calibration).

## Open decisions for the author
1. **B4 body-file plumbing:** implement `--body-file` in `git_platform.py` directly (spec's assumption, self-contained) **vs.** rely on python-gitlab CLI's `@file` value convention if it exists on this version (confirm before choosing).
2. **B6 threshold:** 2 (mirrors the prompt's "two consecutive") vs 3 — pick and encode as a named constant.
3. **B8 depth:** rubric-only (review-workflow.md) vs also echoing the calibration into `cr-security.md`/`cr-correctness.md`.
