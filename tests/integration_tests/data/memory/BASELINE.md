# Memory eval baseline

Recorded against the escaping-fixed prompts (commit `6ff27d1e`, "fix(memory): stop
HTML-escaping transcript, entry and observation text" — Task 1), measured at tree commit
`26633a02`, **before Fix 1 and Fix 2**. At measurement time `extraction_human` had no
`{{{memory}}}` block (Fix 1 not yet applied) and no few-shot constants existed anywhere in
the prompts (Fix 2 not yet applied); the tree was clean.

Command:

```
LANGSMITH_TEST_TRACKING=false uv run pytest --envfile +docker/local/app/config.secrets.env \
  --reuse-db tests/integration_tests --no-cov --log-level=INFO -m memory -v
```

`DAIV_EVAL_REPEATS` was unset, so `EVAL_REPEATS=3`: every case-model pair ran 3 attempts. A
cell's outcome is the majority of those three; a 2-1 (or 1-2) split is additionally flagged
**UNSTABLE** and is excluded from any later delta in either direction — a majority result on
its own isn't enough to trust as evidence when a single flipped attempt would have changed it.

50/50 expected case-model pairs are present below, matching the run's own tally (41 passed, 9
failed, 8 unstable; 834.65s). Exit code 1 is expected — pytest fails the run whenever any
majority is FAIL, which is a result, not a harness problem.

## Extraction — `MEMORY_EXTRACTION_MODELS`

| Case | gpt-5.4-mini | claude-haiku-4.5 |
|---|---|---|
| 001-sandbox-env-var | PASS 3/3 | PASS 3/3 |
| 002-nothing-learned | PASS 2/3 UNSTABLE | PASS 3/3 |
| 003-migration-before-seed | FAIL 0/3 | FAIL 0/3 |
| 004-reviewer-rejects-inline-sql | PASS 3/3 | PASS 3/3 |
| 005-plant-inside-long-tool-output | FAIL 0/3 | PASS 3/3 |
| 006-generic-advice-only | FAIL 1/3 UNSTABLE | PASS 3/3 |
| 007-ci-branch-prefix | PASS 3/3 | PASS 3/3 |
| 008-ephemeral-flake | PASS 2/3 UNSTABLE | FAIL 0/3 |
| 009-reexported-public-api | FAIL 1/3 UNSTABLE | PASS 3/3 |
| 010-memory-read-only | FAIL 0/3 | FAIL 0/3 |
| 011-memory-reverified | PASS 3/3 | PASS 3/3 |
| 012-memory-contradicted | PASS 3/3 | PASS 3/3 |

**`010-memory-read-only` fails 0/3 on both extraction models.** This is Fix 1's one clean
source of delta — the memory block is present in the case's `memory` field but not yet
forwarded to the prompt, so the model has no way to know the fact is already recorded and
re-emits it every time. Caveat: the case is also satisfiable by the prompt's own
"rediscoverable from the repository's docs" rule alone (the fact here — a build dependency
documented in `docs/setup.md` — is exactly that kind of rediscoverable fact), so an
improvement here after Fix 1 may reflect general over-emission suppression rather than memory
awareness specifically. A reader should not credit this cell to Fix 1 without checking which
mechanism actually produced the improvement.

**`011-memory-reverified` passes 3/3 on both models.** That is its designed role: a Fix-1
no-regression guard with no decoys and its plant stated outright in the case (`must_capture`
literally restates the memory content). It demonstrates the pipeline doesn't break when memory
is present and correct, not that extraction quality is good — do not read this 3/3 as evidence
of anything beyond "no regression."

**`012-memory-contradicted` passes 3/3 on both models.** This *contradicts* a review
prediction: the case's compound plant (fact = "the coverage gate is 85 percent, not 70") and
its surface-twin decoy (must_not_capture = "that the coverage gate is 70 percent") were
predicted to grade unstably per-repetition in a single LLM-judge call, because the two strings
overlap heavily. They did not — the vote was clean 3/3 on both models. Record the predicted
instability as **not materialized**; consequently, like `011`, Fix 1 has no room to show
improvement on this case either — it's already at ceiling.

**`003-migration-before-seed` fails 0/3 on both models — genuinely by over-emission, not by
missing the plant.** `max_observations` for this case is 2, but a thorough model can find two
independently real facts in the transcript (the migration-before-seed fact this case is built
to test for, *and* a second real fact about a leftover-partial-seed duplicate-key error), and
both models found both, plus generally one more redundant restatement, landing at 3 emitted
observations against the cap of 2 every time:

- gpt-5.4-mini, all 3 attempts: 3 observations emitted, e.g. (attempt 1) `` `make seed-demo`
  can fail with `DatabaseError: relation "product" does not exist` when catalog migrations
  have not been applied yet. ``, `` `make migrate-status` reports pending catalog migrations
  as `0001_initial` and `0002_add_product` ... ``, and `` After `make migrate` applies ...,
  rerunning `make seed-demo` can still fail with `duplicate key value violates unique
  constraint "product_sku_key"` ... ``.
- claude-haiku-4.5, all 3 attempts (byte-identical across attempts): the same three facts —
  migrate-before-seed, a `make migrate-status` restatement, and the duplicate-key-after-partial-
  seed fact.

The decoy (`must_not_capture`: "that the run ended with twelve errors") was **never graded on
this case**: `_attempt` returns as soon as `extraction_violations` reports the cap breach
(`tests/integration_tests/test_memory_extraction.py:45-46`), before `judge_claims` is ever
reached, and all six attempts (both models × 3 repetitions) tripped the cap, so the
decoy-grading judge call ran zero times for `003`. Read from the raw emission only — this is
an ungraded observation, not a graded leak verdict — gpt-5.4-mini's attempt 3 (log line 163)
nonetheless restates the decoy fact in numeral form: "...can still fail with `duplicate key
value violates unique constraint \"product_sku_key\"` and finish with **12 errors**, indicating
leftover partial demo rows need cleanup before reseeding." The graded, log-supported finding on
`003` is the cap violation; whether the case is actually clean on decoy suppression is untested
by this run, not established by it — a reader should not treat this cell as evidence either
way on that question.

**`005-plant-inside-long-tool-output` fails 0/3 on gpt-5.4-mini only, and by a different
mechanism: decoy leakage, not over-emission.** Each of gpt's 3 attempts emitted only 2
observations (at the cap) and correctly captured the single-threaded-suite fact, but all 3 also
leaked the `must_not_capture` decoy verbatim or near-verbatim inside the *second* observation,
e.g. (attempt 1) `` ...so the integration suite must be invoked single-threaded in CI. `` paired
with a leaked "that the build took four minutes and twelve seconds" elsewhere in the same
attempt's output (all three attempts recorded `leaked decoy: 'that the build took four minutes
and twelve seconds'`). claude-haiku-4.5 passed 3/3 on this case — it captured the same required
fact without leaking the decoy.

**`008-ephemeral-flake` fails 0/3 on claude-haiku-4.5 (over-emission, not a decoy issue) and is
UNSTABLE at 2/3 PASS on gpt-5.4-mini.** `max_observations` is 0 (must_capture is empty — the
transcript describes a one-off flake the prompt should recognize as not worth recording).
haiku emitted 2 observations in all 3 attempts, byte-identical each time: `` test_upload_large_
file in tests/test_uploads.py is flaky and can be killed by external resource constraints on
first run but passes on retry without code changes ... `` and a second about running it with
`-v`. gpt-5.4-mini's unstable 2/3 PASS means it got this right twice and wrong once out of
three attempts (the failing attempt's own emission detail isn't captured in the summary block,
only the vote); either way this cell is excluded from delta because it's unstable.

## Consolidation — `MEMORY_CONSOLIDATION_MODELS`

| Case | claude-sonnet-4.6 | gpt-5.3-codex |
|---|---|---|
| 010-merge-two-fragments | PASS 3/3 | PASS 3/3 |
| 011-discard-generic-advice | PASS 3/3 | PASS 3/3 |
| 012-discard-ephemeral | PASS 3/3 | PASS 3/3 |
| 013-confirm-duplicate | PASS 3/3 | PASS 3/3 |
| 014-update-contradiction | PASS 3/3 | PASS 3/3 |
| 015-add-cold-start | PASS 3/3 | PASS 3/3 |
| 016-in-batch-dedup | PASS 3/3 | PASS 3/3 |
| 017-no-cross-category-merge | PASS 3/3 | PASS 3/3 |
| 018-update-preferred-over-add | PASS 3/3 | PASS 2/3 UNSTABLE |
| 019-mixed-batch | FAIL 0/3 | PASS 2/3 UNSTABLE |
| 020-converges-over-three-rounds | PASS 2/3 UNSTABLE | PASS 3/3 |
| 021-repeated-fact-does-not-duplicate | PASS 3/3 | PASS 3/3 |
| 022-fragments-collapse | PASS 3/3 | PASS 2/3 UNSTABLE |

**`011-discard-generic-advice`, `012-discard-ephemeral` and `013-confirm-duplicate` all pass
3/3 on both models** — the ceiling effect a review predicted, because these encode failure
modes the consolidation prompt already states explicitly (discard generic advice, discard
ephemeral facts, confirm an exact duplicate). Fix 2 has no room on two of its three named
consolidation targets before it even lands. Also note `011`'s content is **synthetic**: per
`PROVENANCE.md`, the real production sample contained zero `generic_advice` rows — the case is
a paraphrase of the nearest real row (`E012`, "reuse existing shared classifiers", trimmed of
its concrete anchor) built specifically because the production sample had no real example to
draw from. It is a regression guard against a plausible failure mode, not evidence that the
failure occurs in production.

**`019-mixed-batch` fails 0/3 on claude-sonnet-4.6 (and is UNSTABLE 2/3 PASS on gpt-5.3-codex).**
This is a decision-correctness failure, not an emission-count problem. All 3 sonnet attempts
made the same two mistakes: observation `o3` (which should DISCARD) instead got `ADD` in every
attempt, and observations `o4`/`o5` (which should target entries `['e2', 'e3']`, a cross-entry
merge) instead targeted only `['e2']` in every attempt — sonnet consistently found a narrower,
single-entry update where the case calls for a two-entry merge, and consistently treated the
discard-worthy observation as new information instead.

**`016-in-batch-dedup` passes 3/3 on both models in this run**, but a reviewer relying on this
table for Task 12 should know the grading has a blind spot worth watching for in later runs: the
case's `expect` allows `o1`/`o2` to each be `ADD`ed as long as they collapse to `one_operation_
for`, but an alternative correct answer — ADD `o1` and DISCARD `o2` as a restatement of `o1` —
would create no duplicate entry yet could score FAIL under a naive vote-only read. Task 12 must
read the evidence dict for this case, not only the PASS/FAIL vote.

**`015-add-cold-start` passes 3/3 on both models.** Its `expect` only requires that both `o1`
and `o2` are ADDed independently (there are no existing entries — cold start) — it is
satisfiable by accident by a single model behavior (naming both observations) that says nothing
about whether the model actually distinguished the two facts as separate, so a future PASS here
should not by itself be read as strong evidence of discrimination quality.

## Summary

- 50 case-model pairs: 41 majority-PASS, 9 majority-FAIL, 8 of the 50 flagged UNSTABLE
  (6 unstable-PASS: consolidation `018`/gpt, `019`/gpt, `020`/sonnet, `022`/gpt; extraction
  `002`/gpt, `008`/gpt — plus 2 unstable-FAIL: extraction `006`/gpt, `009`/gpt). Unstable cells
  are excluded from any later delta in either direction regardless of which side of the vote
  they landed on.
- **Fix 1 targets** (per the plan): `010-memory-read-only` (clean FAIL 0/3 on both models — real
  room, with the over-emission caveat above) and `012-memory-contradicted` (clean PASS 3/3 on
  both models already — no room; the predicted instability on this case did not materialize).
- **Fix 2 targets** (per the plan's named cases: `002`, `006`, `008`, `011-discard-generic-
  advice`, `012-discard-ephemeral`, `016-in-batch-dedup`, `021-repeated-fact-does-not-duplicate`):
  only `008-ephemeral-flake` on claude-haiku-4.5 is a clean, stable majority-FAIL (0/3) with
  real room to move. `006-generic-advice-only` on gpt-5.4-mini is a majority-FAIL but UNSTABLE
  (1/3) — some signal, but noisy. `002-nothing-learned` on gpt-5.4-mini is majority-PASS but
  UNSTABLE (2/3) — also noisy, on the PASS side. `011`, `012`, `016`, and `021` are all already
  at ceiling (PASS 3/3 on both models) with no room to demonstrate improvement.
- Independent of either fix's named targets, three cases fail outside the two fixes' scope.
  `003-migration-before-seed` (both models, clean FAIL 0/3) and `009-reexported-public-api`
  (gpt-5.4-mini only, FAIL 1/3 UNSTABLE) both fail by over-emission against a `max_observations`
  cap that's tight for the case's real fact count — `009`'s gpt attempts show the same shape as
  `003`'s: 3 observations emitted against a cap of 2 (log lines 446-448). `005-plant-inside-
  long-tool-output` (gpt-5.4-mini only, clean FAIL 0/3) fails instead by decoy leakage, a
  different mechanism (see detail above). None of the three is expected to move from Fix 1 or
  Fix 2 by design, so a mover on any of them should be treated as a surprise worth
  investigating, not credited to either fix by default.

## Fix 1

Measured against Fix 1's final state (commit `722d230c`, "fix(memory): close the stray blank
line left by the fallback-line removal"). This is the third and last paid run for Fix 1, per a
stopping rule pre-registered before it ran: re-measure once more, and report whatever comes back
— improvement, regression, or null — rather than tune the prompt further against one
acknowledged-weak case. Command and repetition count are the same as the baseline run above.

Two earlier Fix 1 runs preceded this one and are not the basis for this table, but are cited
below where they isolate a cause: `memory-fix1.txt` (round-2 prompt text — the re-verification
predicate said a fact was re-verified by reading "the code, config, or convention that *states*
it", and the `{{^memory}}` "no memory yet" fallback line was still present) and
`memory-fix1-v2.txt` (this run's log, round-2's predicate tightened to *defines*, and the
fallback line removed). `memory-baseline-run2.txt` is the log behind the baseline table already
recorded above.

50/50 expected case-model pairs, matching the run's own tally (39 passed, 11 failed, 8 unstable;
862.70s). Exit code 1 is expected, as with the baseline run.

### Fix 1's own targets: a clean null

| Case | gpt-5.4-mini | claude-haiku-4.5 | vs. baseline |
|---|---|---|---|
| 010-memory-read-only | FAIL 0/3 | FAIL 0/3 | unchanged |
| 011-memory-reverified | PASS 3/3 | PASS 3/3 | unchanged |
| 012-memory-contradicted | PASS 3/3 | PASS 3/3 | unchanged |

**`010-memory-read-only` did not move: FAIL 0/3 on both models, exactly as at baseline.** This
is Fix 1's only case with real room (per the baseline's own note, `012` was already at ceiling
and `011` is a no-regression guard, not a target). It did not move across any of Fix 1's three
measured states — the original carve-out, the widened re-verification predicate, or this run's
`defines`-tightened, fallback-free final text. **The result on Fix 1's declared targets is a
clean null: no improvement, and the plan's threshold — at least two named target cases flipping
from majority-fail to majority-pass, with no case regressing — was not met.** `011` and `012`
held their baseline verdicts exactly, so the module's guard cases show no regression either.

### Fix 1 is regression-free

No case that was a **stable** majority-PASS at baseline became a stable majority-FAIL in this
run, or vice versa. One cell, `022-fragments-collapse` on `claude-sonnet-4.6`, did move from a
stable `PASS 3/3` at baseline to an unstable `FAIL 1/3` here — a literal majority flip — but it
is flagged UNSTABLE under the baseline's own exclusion rule and is discussed under the noise
floor below, not counted as a regression: `022` is a **consolidation** case, and Fix 1 touches
only `extraction_human` and `extraction.py` — nothing under `daiv/memory/consolidation*` or the
consolidation prompts changed in any of Fix 1's three rounds, so this case had zero code-level
exposure to Fix 1 at all. Its movement is measured proof of run-to-run model nondeterminism, not
evidence of anything Fix 1 did.

### `007-ci-branch-prefix`: the fallback-line regression, confirmed fixed

| Run | gpt-5.4-mini | claude-haiku-4.5 |
|---|---|---|
| Baseline (no memory feature at all) | PASS 3/3 | PASS 3/3 |
| First Fix 1 run (`{{^memory}}` fallback line present) | PASS 2/3 UNSTABLE | PASS 2/3 UNSTABLE |
| This run (fallback line removed) | PASS 3/3 | PASS 3/3 |

`007` carries no `memory` field, so across all three runs the only prompt-level difference it
was ever exposed to was the presence or absence of the `{{^memory}}` "This repository has no
memory yet." line (the `states`→`defines` re-verification wording never applies to a
memory-less prompt in any version) — confirmed directly, not just argued: rendering the first
Fix 1 run's template (`19249b21`) with `memory=""` and diffing it against the baseline template
(`ecc25251`) rendered the same way shows exactly those two lines added and nothing else, so the
exposure is cleanly isolated. The regression appeared on **both** models exactly when that line
was introduced, and disappeared on **both** models exactly when it was removed. That said, the
"during" arm is two `PASS 2/3` cells — the very split pattern this document flags UNSTABLE and
excludes from deltas everywhere else — measured at one run per arm, against the ~16% per-run
movement rate the next section reports. Call this **strongly suggestive, not proven**: a clean
isolation and a same-direction move on both models, but drawn from a single before/during/after
sample rather than repeated runs.

### The noise floor: 7 of 44 cells moved on unchanged input

Because an empty-memory prompt is now byte-identical to what the baseline measured (guaranteed
by `test_extraction_prompt_is_byte_identical_to_pre_memory_baseline_when_there_is_no_memory` in
`tests/unit_tests/memory/test_prompts.py`), extraction cases 001-009 — none of which carry a
`memory` field — received the literal same model input (system prompt, human prompt, transcript)
in the baseline run and in this run. Consolidation cases 010-022 received the same input too,
since Fix 1 never touched the consolidation prompts or code at all. That covers 44 of the 50
cells (18 extraction + 26 consolidation); the remaining 6 — `010`/`011`/`012` × 2 models — are
exactly the cells whose input Fix 1 changed, so they're excluded from a rate about identical
input. So every one of these 44 cells moved (or didn't) with **zero** code-level or prompt-level
change behind it — the variables were the extraction/consolidation model itself and, for any
cell whose grading reached it, the LLM judge in `judge_claims`/`judge_duplicate_facts`
(`tests/integration_tests/memory_grading.py`) — both called again the same day, hours apart
(16:30 and 23:02):

| Cell | Baseline | This run |
|---|---|---|
| `002-nothing-learned` / gpt-5.4-mini | PASS 2/3 UNSTABLE | FAIL 1/3 UNSTABLE |
| `004-reviewer-rejects-inline-sql` / gpt-5.4-mini | PASS 3/3 | PASS 2/3 UNSTABLE |
| `009-reexported-public-api` / gpt-5.4-mini | FAIL 1/3 UNSTABLE | FAIL 0/3 |
| `020-converges-over-three-rounds` / claude-sonnet-4.6 (consolidation) | PASS 2/3 UNSTABLE | PASS 3/3 |
| `020-converges-over-three-rounds` / gpt-5.3-codex (consolidation) | PASS 3/3 | PASS 2/3 UNSTABLE |
| `022-fragments-collapse` / claude-sonnet-4.6 (consolidation) | PASS 3/3 | FAIL 1/3 UNSTABLE |
| `022-fragments-collapse` / gpt-5.3-codex (consolidation) | PASS 2/3 UNSTABLE | PASS 3/3 |

7 of 44 cells (16%) moved between the two runs on identical input. **Three of those seven —
`004-reviewer-rejects-inline-sql`/gpt-5.4-mini, `020-converges-over-three-rounds`/gpt-5.3-codex,
and `022-fragments-collapse`/claude-sonnet-4.6 — departed a previously *stable*, unanimous `3/3`
verdict.** A unanimous 3-repetition result is therefore not proof against future instability on
the same input; it is itself a sample that can land unanimous by chance and not repeat.

This also closes the standing instruction in the baseline table above, that a mover on
`003`/`005`/`009` "should be treated as a surprise worth investigating, not credited to either
fix by default": `009-reexported-public-api`/gpt-5.4-mini did move (FAIL 1/3 UNSTABLE → FAIL 0/3).
It is investigated here, not merely logged — `009` carries no `memory` field, so this move is
part of the same unchanged-input noise floor as the other six cells, not a Fix 1 side effect.

### Consequence for reading a future delta out of this harness

At `EVAL_REPEATS=3`, roughly one cell in six or seven moves between two runs with no change
behind them at all, and that rate is high enough to have flipped three previously unanimous
cells. A single-cell delta on a single model — one case's majority flipping from FAIL to PASS on
only one of its two models — sits inside this measured noise band and cannot, by itself, be
distinguished from it. This vindicates the UNSTABLE-exclusion rule the baseline already applies
(it was previously a design choice; it is now an empirically measured necessity) and extends its
lesson to *stable* cells too: a future fix's delta claim needs either materially more
repetitions than 3, or a case moving in the same direction on **both** models at once, before it
can be read as a real effect rather than this noise floor.

### Known limitations carried forward

Two limitations recorded in Task 11's implementation report remain open and are not addressed by
this run:

- **`010` fails by both named mechanisms, and the `defines` tightening did not take on one
  model.** `010`'s `expect` is `must_capture: []` with `max_observations: 0`, so correct
  suppression IS the pass condition — a FAIL 0/3 verdict means the model emitted on every single
  attempt; "correctly suppressing for an unrelated reason" is not a possible reading of that
  verdict. The log settles which mechanism, per model, rather than leaving it untested:
  `memory-fix1-v2.txt:603-605` shows gpt-5.4-mini emitting two observations in all three
  attempts, the second a standalone `docs/setup.md` citation (e.g. "`docs/setup.md` lists build
  prerequisites and explicitly calls out the protobuf compiler...") — exactly the source class
  the `defines` tightening was written to disqualify, so on this model the tightening
  demonstrably did not take. `memory-fix1-v2.txt:658-660` shows claude-haiku-4.5 emitting one
  observation in all three attempts along the untouched "ran it and it worked" path ("The build
  requires protobuf-compiler to be installed; `apt-get install -y protobuf-compiler` successfully
  installs it and allows `make build` to succeed."), with no doc citation at all. So `010` fails
  on gpt-5.4-mini because the doc-citation path the tightening targeted is still being taken, and
  fails on claude-haiku-4.5 because of the separate, never-tightened command-succeeded path —
  this is the most decision-relevant fact in this run for whether the `defines` approach is worth
  iterating further, and per the pre-registered stopping rule no further tightening was attempted
  to act on it.
- **The `workflow`-category gap.** A workflow fact backed by nothing but prose convention (no
  CI/hook/config artifact that actually enforces it) has no qualifying "defines" source under the
  tightened predicate, the same way `010`'s doc-only fact does not. This is a pre-existing gap,
  not introduced by any Fix 1 round, and is not addressed here.

## Summary (Fix 1)

- **Null result on Fix 1's own targets.** `010-memory-read-only` did not move (FAIL 0/3, both
  models, unchanged from baseline across all three measured Fix 1 states); `011` and `012` held
  their baseline verdicts. The plan's landing threshold was not met.
- **No regression.** No stable cell flipped from majority-PASS to majority-FAIL because of Fix
  1; the one literal majority flip (`022`/sonnet) is a consolidation case Fix 1 never touches,
  and is folded into the noise-floor finding below rather than counted separately.
- **A strongly suggestive causal finding:** the `{{^memory}}` fallback line's introduction and
  removal cleanly bracket `007-ci-branch-prefix`'s regression (PASS 3/3 → PASS 2/3 UNSTABLE on
  both models when introduced → PASS 3/3 on both models when removed), on a case with no
  `memory` field, with the exposure verified airtight by rendering both templates. It moved on
  both models in the same direction, but from a single before/during/after sample whose "during"
  arm is itself the UNSTABLE split pattern this document excludes elsewhere — treat it as strong
  evidence, not as more certain than everything else in this file.
- **A measured noise floor:** 7 of 44 cells (16%) moved between the baseline and this run on
  literally unchanged input, including three departures from a previously unanimous `3/3`. Any
  future single-cell, single-model delta claim out of this harness must be read against that
  floor — it needs more repetitions or agreement across both models to be distinguishable from
  chance.
- Fix 1's deliverable — extraction can see the current memory document, with a rule that
  preserves both contradiction-driven correction and re-verification-driven confirmation rather
  than suppressing them — stands independent of this null; see the Task 11 implementation report
  for the design rationale.

## Fix 2

Measured against Fix 2's final state (commit `379db83c`, "fix(memory): close the few-shot leak
guard's coverage gaps") — the state accepted after a review round found and fixed two Critical
and two Important issues in the leak guard and the few-shot text itself (see the Task 12
implementation report). Command and repetition count are the same as the baseline and Fix 1 runs
above (`DAIV_EVAL_REPEATS` unset, `EVAL_REPEATS=3`). Log: `memory-fix2.txt`. The Fix 1 run
above (`memory-fix1-v2.txt`) is the state this run moves from, per the plan — Fix 1 had already
landed on `main` when Fix 2 was built.

50/50 expected case-model pairs, matching the run's own tally (43 passed, 7 failed, 736.29s) —
the best raw pass count of the three runs (baseline 41/9, Fix 1 39/11). Exit code 1 is expected,
as with the earlier runs.

**Headline: no cell moved in the same direction on both of its models. Every one of this run's
10 moved cells is a single-model move. Fix 2's effect is NOT established by this run.**

### Every cell, this run

| Case | gpt-5.4-mini | claude-haiku-4.5 |
|---|---|---|
| 001-sandbox-env-var | PASS 3/3 | PASS 3/3 |
| 002-nothing-learned | PASS 3/3 | PASS 3/3 |
| 003-migration-before-seed | FAIL 0/3 | PASS 3/3 |
| 004-reviewer-rejects-inline-sql | FAIL 1/3 UNSTABLE | PASS 3/3 |
| 005-plant-inside-long-tool-output | PASS 2/3 UNSTABLE | PASS 3/3 |
| 006-generic-advice-only | FAIL 1/3 UNSTABLE | PASS 3/3 |
| 007-ci-branch-prefix | PASS 3/3 | PASS 3/3 |
| 008-ephemeral-flake | PASS 3/3 | FAIL 0/3 |
| 009-reexported-public-api | PASS 2/3 UNSTABLE | PASS 3/3 |
| 010-memory-read-only | FAIL 0/3 | PASS 3/3 |
| 011-memory-reverified | PASS 3/3 | PASS 3/3 |
| 012-memory-contradicted | PASS 3/3 | PASS 3/3 |

| Case | claude-sonnet-4.6 | gpt-5.3-codex |
|---|---|---|
| 010-merge-two-fragments | PASS 3/3 | PASS 3/3 |
| 011-discard-generic-advice | PASS 3/3 | PASS 3/3 |
| 012-discard-ephemeral | PASS 3/3 | PASS 3/3 |
| 013-confirm-duplicate | PASS 3/3 | PASS 3/3 |
| 014-update-contradiction | PASS 3/3 | PASS 3/3 |
| 015-add-cold-start | PASS 3/3 | PASS 3/3 |
| 016-in-batch-dedup | FAIL 1/3 UNSTABLE | PASS 3/3 |
| 017-no-cross-category-merge | PASS 3/3 | PASS 3/3 |
| 018-update-preferred-over-add | PASS 3/3 | PASS 3/3 |
| 019-mixed-batch | FAIL 0/3 | PASS 2/3 UNSTABLE |
| 020-converges-over-three-rounds | PASS 3/3 | PASS 2/3 UNSTABLE |
| 021-repeated-fact-does-not-duplicate | PASS 3/3 | PASS 3/3 |
| 022-fragments-collapse | PASS 3/3 | PASS 3/3 |

### Movement vs. Fix 1: 10 of 50 cells (20%), and this run has no control cells to check it against

Unlike the Fix 1 run, which left extraction cases 001-009 and every consolidation case on a
byte-identical prompt (letting the previous section measure a 7/44 (~16%) noise floor on
literally unchanged input), Fix 2 changes both system templates unconditionally — every one of
the 50 cells saw different model input this run than in the Fix 1 run. There is no control group
left to re-measure the floor against, only the number itself to compare: 10/50 (20%) moved here
against the 7/44 (~16%) measured previously. That is barely above the prior floor, on a
comparison the harness cannot sharpen further at this repetition count, which is the same
conclusion the headline states from a different angle: total movement this run is not
distinguishable from what unchanged-input noise alone already produces.

| Cell | Fix 1 | This run |
|---|---|---|
| `002-nothing-learned` / gpt-5.4-mini | FAIL 1/3 UNSTABLE | PASS 3/3 |
| `003-migration-before-seed` / claude-haiku-4.5 | FAIL 0/3 | PASS 3/3 |
| `004-reviewer-rejects-inline-sql` / gpt-5.4-mini | PASS 2/3 UNSTABLE | FAIL 1/3 UNSTABLE |
| `005-plant-inside-long-tool-output` / gpt-5.4-mini | FAIL 0/3 | PASS 2/3 UNSTABLE |
| `008-ephemeral-flake` / gpt-5.4-mini | PASS 2/3 UNSTABLE | PASS 3/3 |
| `009-reexported-public-api` / gpt-5.4-mini | FAIL 0/3 | PASS 2/3 UNSTABLE |
| `010-memory-read-only` / claude-haiku-4.5 | FAIL 0/3 | PASS 3/3 |
| `016-in-batch-dedup` / claude-sonnet-4.6 (consolidation) | PASS 3/3 | FAIL 1/3 UNSTABLE |
| `018-update-preferred-over-add` / gpt-5.3-codex (consolidation) | PASS 2/3 UNSTABLE | PASS 3/3 |
| `022-fragments-collapse` / claude-sonnet-4.6 (consolidation) | FAIL 1/3 UNSTABLE | PASS 3/3 |

Of these 10, every single one is a **single-model** move: in no case did both of a case's two
models move, whether in the same direction or opposite directions. `004`'s and `016`'s partner
models (`claude-haiku-4.5` and `gpt-5.3-codex` respectively) held their Fix 1 verdicts exactly;
so did every other moved cell's partner. That is the concrete basis for the headline.

### The pre-registered predictions, scored

These were recorded before this run's data was seen, per the process this document has followed
since Fix 1's stopping rule. Scoring them as pre-registered (rather than reading the table fresh)
is what stops a real fix's evaluation from becoming a post-hoc story:

- **`008-ephemeral-flake`/claude-haiku-4.5: predicted FAIL 0/3 → FAIL 0/3 (null). Hit.** The
  prediction's reasoning: removing the retry-shaped REJECT (Critical 2 of the review round) was
  required to stop it paraphrasing `008`'s own graded conclusion, and that REJECT was pair 3's
  most direct teaching example for exactly this case's failure mode — so no improvement was
  expected from the rewrite that made the fix safe to ship. It stayed FAIL 0/3, exactly as
  predicted. (`008`/gpt-5.4-mini did move, PASS 2/3 UNSTABLE → PASS 3/3, but that is the one
  model this prediction was not about, and a stability-only change on the model already passing.)
- **`005-plant-inside-long-tool-output`/gpt-5.4-mini: predicted FAIL 0/3 → PASS (directional,
  not a delta claim). Directionally consistent, explicitly not evidence.** The standing baseline
  instruction that a `005` mover is "a surprise worth investigating, not credited to either fix"
  was revised in advance for this one case: `005`/gpt fails at baseline by leaking a one-run
  number (`must_not_capture`: "that the build took four minutes and twelve seconds"), and the
  rewritten pair 3 (Critical 2's fix) teaches directly against stating a one-run number as if it
  mattered. It moved, in the predicted direction, by the named mechanism — but landed at PASS 2/3,
  which is UNSTABLE, so per this document's own exclusion rule it is **excluded from any delta**.
  Record it as directionally consistent with a named mechanism, not as evidence that mechanism
  worked.
- **`019-mixed-batch`: predicted unchanged on both models (confound-clearing). Hit.** The review
  flagged the consolidation MERGE few-shot as a candidate to worry about here; it produced no
  movement at all — `claude-sonnet-4.6` stayed FAIL 0/3, `gpt-5.3-codex` stayed PASS 2/3 UNSTABLE,
  byte-identical verdicts to Fix 1 on both models. The confound the review raised did not
  materialize.
- **`006-generic-advice-only`/gpt-5.4-mini: a named Fix 2 target with real room. Predicted no
  guaranteed movement; observed none.** FAIL 1/3 UNSTABLE → FAIL 1/3 UNSTABLE, identical split.
  `claude-haiku-4.5`'s `006` also held (PASS 3/3 → PASS 3/3). No movement on either model.
- **`003-migration-before-seed`/claude-haiku-4.5: FAIL 0/3 → PASS 3/3, unanimous at both ends —
  and, by this document's own pre-registration, explicitly NOT credited to Fix 2.** The baseline
  table's standing instruction calls a `003` or `009` mover "a surprise worth investigating, not
  credited to either fix by default," and the `005` revision above was deliberately scoped to
  `005` alone, leaving `003` and `009` as surprises. However tempting a clean, unanimous 0/3 → 3/3
  flip is to read as evidence, the pre-registered rule says otherwise, and this document holds to
  it: **not credited.** (`009`/gpt-5.4-mini also moved this run, FAIL 0/3 → PASS 2/3 UNSTABLE —
  the same "surprise, not credited" instruction covers it, and it is additionally excluded as
  unstable.)

### The predicted regression that materialized

Two named Fix 2 targets **regressed** relative to Fix 1, and one of them was anticipated in
review before this run:

- **`004-reviewer-rejects-inline-sql`/gpt-5.4-mini.** Stable `PASS 3/3` at the original baseline,
  already knocked to `PASS 2/3 UNSTABLE` by Fix 1 (a pre-existing, code-unrelated noise-floor
  move — Fix 1 never touched extraction's few-shots, since none existed yet), and now `FAIL 1/3
  UNSTABLE` here. The review that accepted Fix 2's rewritten pair 2 (`REJECT: "Prefer descriptive
  variable names over abbreviations."`) flagged it as category-adjacent to `004`'s own decoy
  ("that code should follow the project's style guide") and judged the risk acceptable *because*
  `004` was at ceiling at baseline, so exposure "can only show a regression, never a manufactured
  delta." That is precisely what happened: a stable-at-ceiling cell moved toward FAIL after the
  few-shot landed. `claude-haiku-4.5`'s `004` did not move (PASS 3/3 → PASS 3/3), so this is
  single-model, and the new split is UNSTABLE, so it does not meet this document's bar for a
  confirmed regression either — but it is the one place this run's data lines up exactly with a
  cost the review foresaw and accepted, and that is worth recording as such rather than as an
  unexplained wobble.
- **`016-in-batch-dedup`/claude-sonnet-4.6.** Stable `PASS 3/3` at both the original baseline and
  Fix 1, now `FAIL 1/3 UNSTABLE`. This is notable beyond being a regression: `016` is one of Fix
  2's own named consolidation targets, and the baseline table already recorded it at ceiling
  (PASS 3/3, both models) before either fix landed — meaning it had **no room to improve**, only
  to hold or regress, and the model most exposed to Fix 2's new in-batch-dedup rule and
  MERGE/DISCARD few-shots (`claude-sonnet-4.6`) is exactly the one that regressed on it.
  `gpt-5.3-codex`'s `016` held (PASS 3/3 → PASS 3/3), so this is single-model too, and UNSTABLE
  (1/3), so it is not a confirmed regression under this document's exclusion rule — but paired
  with `004`, it means both of this run's clearest adverse moves land on cells the review either
  predicted (`004`) or built new prompt material directly targeting (`016`).

Neither regression is "confirmed" by the UNSTABLE-exclusion rule this document applies
everywhere else, and that rule cuts both ways here: it also means neither can be read as proof
Fix 2 made things worse. Recorded as what the pre-registered review discussion said to watch for,
observed happening, at exactly the instability threshold that keeps it from being conclusive
either way.

### The largest single moves, excluded by design

`003-migration-before-seed` and `010-memory-read-only`, both on `claude-haiku-4.5`, moved from a
stable, unanimous `FAIL 0/3` (at both the original baseline and Fix 1) to a stable, unanimous
`PASS 3/3` here — the largest possible single-cell swing the harness can register. Both are
single-model (`gpt-5.4-mini`'s `003` and `010` held their Fix 1 `FAIL 0/3` verdicts exactly), so
neither meets the both-models bar. `003` is additionally excluded by this document's own
pre-registration (see above); `010` carries no such standing instruction, but is still only one
model of a two-model case and cannot alone establish a cross-model effect.

### Fix 2's own named targets, side by side

The baseline table already recorded, before either fix landed, that `011`, `012`, `016` and `021`
were at ceiling (PASS 3/3, both models) with no room to demonstrate improvement, and that only
four of the fourteen target cells had any room at all: `002`/gpt and `008`/gpt were unstable-PASS
(noisy, already leaning pass), `006`/gpt was unstable-FAIL, and `008`/haiku was the only clean,
stable majority-FAIL.

| Target | Model | Room at baseline? | Fix 1 | This run |
|---|---|---|---|---|
| `002-nothing-learned` | gpt-5.4-mini | Yes (unstable-PASS) | FAIL 1/3 UNSTABLE | PASS 3/3 |
| `002-nothing-learned` | claude-haiku-4.5 | No (ceiling) | PASS 3/3 | PASS 3/3 |
| `006-generic-advice-only` | gpt-5.4-mini | Yes (unstable-FAIL) | FAIL 1/3 UNSTABLE | FAIL 1/3 UNSTABLE |
| `006-generic-advice-only` | claude-haiku-4.5 | No (ceiling) | PASS 3/3 | PASS 3/3 |
| `008-ephemeral-flake` | gpt-5.4-mini | Yes (unstable-PASS) | PASS 2/3 UNSTABLE | PASS 3/3 |
| `008-ephemeral-flake` | claude-haiku-4.5 | Yes (stable-FAIL) | FAIL 0/3 | FAIL 0/3 |
| `011-discard-generic-advice` | both | No (ceiling) | PASS 3/3 | PASS 3/3 |
| `012-discard-ephemeral` | both | No (ceiling) | PASS 3/3 | PASS 3/3 |
| `016-in-batch-dedup` | claude-sonnet-4.6 | No (ceiling) | PASS 3/3 | FAIL 1/3 UNSTABLE |
| `016-in-batch-dedup` | gpt-5.3-codex | No (ceiling) | PASS 3/3 | PASS 3/3 |
| `021-repeated-fact-does-not-duplicate` | both | No (ceiling) | PASS 3/3 | PASS 3/3 |

Of the ten cells with no room, nine stayed exactly where they were and one — `016`/sonnet — moved,
and the only direction available to a ceiling cell is down. Of the four cells with real room:
`002`/gpt and `008`/gpt each moved to a stable PASS (single-model improvements, no cross-model
confirmation); `006`/gpt showed no movement at all; `008`/haiku showed no movement at all — the
predicted null, hit exactly. No named target flipped from majority-fail to majority-pass on both
of its models; the plan's landing threshold needed at least two such cases.

## Summary (Fix 2)

- **The plan's threshold — at least two named target cases flipping from majority-fail to
  majority-pass, with no case regressing from majority-pass to majority-fail — was not met**, by
  the same standard applied to Fix 1. `002`/gpt-5.4-mini is the only named target that flipped
  fail-to-pass, and it did so on one model only, landing stable while its case partner
  (`claude-haiku-4.5`) was already passing before either fix existed. No named target flipped on
  both of its models.
- **No cell moved in the same direction on both models, on any case, named target or not.** Every
  one of the 10 moved cells (out of 50) is a single-model move; the partner model held its Fix 1
  verdict exactly in every one of those 10 cases. This is the reason Fix 2's effect is not
  established by this run, independent of any individual cell's direction.
- **Two regressions, both foreseeable, neither confirmed.** `004`/gpt-5.4-mini (a risk the review
  explicitly named and accepted, at ceiling before Fix 2) and `016`/claude-sonnet-4.6 (a
  regression on Fix 2's own named consolidation target) both moved from stable PASS 3/3 toward
  FAIL. Both landed UNSTABLE (1/3), so neither meets this document's bar for a confirmed
  regression — but both are recorded with the prominence a foreseen cost that came due deserves,
  not filed as unexplained noise.
- **Movement (10/50, 20%) sits just above the previously measured 16% unchanged-input noise
  floor, but this run has no control cells left to re-measure that floor against** — Fix 2 changed
  both system prompts for every one of the 50 cases, so unlike the Fix 1 run there is no subset of
  cells that saw byte-identical input across runs. The comparison is suggestive, not dispositive:
  total movement is not clearly distinguishable from what pure model/judge nondeterminism already
  produces at this repetition count.
- **Three paid runs (baseline, Fix 1, Fix 2) have now been measured on this corpus at
  `EVAL_REPEATS=3`, and neither fix's effect has been established by any of them.** Fix 1 landed a
  clean null on its own targets with no regression. Fix 2 landed a raw pass-count improvement
  (43/50 vs. 39/50) but no cell moved on both of a case's models in either direction, two named-
  target-adjacent cells show foreseeable (if unconfirmed) regressions, and total movement does not
  clearly clear the measured noise floor. That is not a verdict that either fix does nothing — the
  mechanisms both fixes add (memory-aware extraction; discriminative few-shots with a leak guard)
  stand on their own design merits, documented in the Task 11 and Task 12 implementation reports —
  but it is the honest limit of what this harness, at this corpus size and this repetition count,
  can resolve. A future measurement wanting to move past this limit needs materially more
  repetitions per cell (the plan's own guidance: `DAIV_EVAL_REPEATS=7` on a specific contested
  case) or a larger corpus with more cases per failure mode, not a third run at the same settings.
