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

Neither model leaked the `must_not_capture` decoy (the "twelve errors" count). The failure
mode here is exclusively over-emission against the cap, on a case that has more real signal in
it than `max_observations` allows for a careful model — a review flag, confirmed by the run.

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
- Independent of either fix's named targets, `003-migration-before-seed` (both models) and
  `005-plant-inside-long-tool-output` (gpt-5.4-mini only) are clean, stable majority-FAILs
  outside the two fixes' scope, for the reasons detailed above (over-emission against a cap
  that's tight for the case's real fact count, and decoy leakage, respectively) — neither is
  expected to move from Fix 1 or Fix 2 and a mover here should be treated as a surprise worth
  investigating, not credited to either fix by default.
