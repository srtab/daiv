# Memory model selection sweep

Scores replacement candidates for the two memory suites' models against the incumbents, measured
at tree commit `b9a04e2b` — the same prompt state as BASELINE.md's **Fix 2** section, so that
section's numbers are directly comparable to the 3-repeat tables here.

Candidates were parametrized through the `DAIV_EVAL_MEMORY_EXTRACTION_MODELS` /
`DAIV_EVAL_MEMORY_CONSOLIDATION_MODELS` overrides added to `tests/integration_tests/utils.py`;
with neither set, both suites still resolve to the production pairs, so the CI gate is unchanged.

```
LANGSMITH_TEST_TRACKING=false DAIV_EVAL_REPEATS=<3|7> \
DAIV_EVAL_MEMORY_<SUITE>_MODELS="openrouter:...,openrouter:..." \
uv run pytest --envfile +docker/local/app/config.secrets.env --reuse-db \
  tests/integration_tests/test_memory_<suite>.py --no-cov --log-level=INFO -m memory -v
```

The two suites ran as separate processes. That is safe (and roughly halves wall time): the test DB
is sqlite `:memory:`, so it is per-process, and `_VOTES` is per-process too — which is also why
neither run may use `-n`/xdist.

**Incumbents were re-run inside every sweep, not compared across runs.** BASELINE.md measures a
~16% per-cell movement rate on byte-identical input, so a candidate scored against BASELINE.md's
recorded incumbent numbers would be comparing across that noise. The control worked:
`claude-haiku-4.5` reproduced its Fix-2 verdict on all 12 extraction cells exactly, and
`claude-sonnet-4.6` on 12 of 13 consolidation cells (`016` deepened `FAIL 1/3` → `FAIL 0/3`,
same majority).

## Which runs are valid

Two runs were poisoned by provider-side HTTP errors. An attempt that raises is voted FAIL by
design (`test_memory_extraction.py`'s `except Exception` — a crashed attempt must not vanish), so
a throttled attempt is indistinguishable from a quality failure in the vote alone and has to be
attributed from the log.

| Run | Repeats | Rate-limit (429) | Credit (402) | Verdict |
|---|---|---|---|---|
| `sweep-extraction` | 3 | 0 | 0 | valid |
| `sweep-consolidation` | 3 | 0 | 0 | valid |
| `confirm-extraction` | 7 | 59, **all `qwen3.8-flash`** | 0 | valid except `qwen3.8-flash` |
| `confirm-consolidation` | 7 | 0 | 0 | valid |
| `confirm-extraction-2` | 7 | 66, all `qwen3.8-flash` | 24, all `gemini-3.8-flash` | **write-off** |

`confirm-extraction-2` was the re-measurement of `qwen3.8-flash` and `gemini-3.8-flash`; the
OpenRouter credit balance ran out partway through it (`Error code: 402 — This request requires
more credits, or fewer max_tokens. You requested up to 51200 tokens`). Its numbers
(`gemini` 51.2%, `qwen` 11.9%) are artifacts and are **not** reported below. The bare string
`402` appears in the three earlier logs only inside UUID fragments — `Error code: 402` appears
in none of them.

## Extraction

12 cases. Production is `memory_extraction_model_name` = `gpt-5.4-mini` with
`memory_extraction_fallback_model_name` = `claude-haiku-4.5`.

"Clean FAIL" means a `0/n` cell — no attempt passed. A majority-FAIL that is merely *unstable*
(some attempts passed) is counted in the unstable column instead, per BASELINE.md's exclusion rule.

| Model | maj-PASS (3 rep) | rate (3 rep) | maj-PASS (7 rep) | **rate (7 rep)** | unstable (7) | clean FAIL (7) | $/M in→out |
|---|---|---|---|---|---|---|---|
| `claude-haiku-4.5` *(incumbent fallback)* | 11/12 | 91.7% | 11/12 | **91.7%** (77/84) | 0 | `008` | 1.00 → 5.00 |
| `gpt-5.6-luna` | 10/12 | 83.3% | 10/12 | **86.9%** (73/84) | 1 (`003`, 3/7) | `010` | 0.20 → 1.20 |
| `glm-5.3-flash` | 11/12 | 86.1% | 8/12 | **69.0%** (58/84) | 8 | none | 0.075 → 0.25 |
| `gpt-5.4-mini` *(incumbent primary)* | 9/12 | 72.2% | 9/12 | **67.9%** (57/84) | 7 | `010` | 0.75 → 4.50 |
| `gemini-3.8-flash` | 10/12 | 83.3% | — | not validly measured | — | `003`, `009` *(3 rep)* | 0.75 → 3.75 |
| `qwen3.8-flash` | 12/12 | 86.1% | — | **unmeasurable** | — | — | 0.15 → 0.47 |

### The 7-repeat run reversed the 3-repeat ranking

`glm-5.3-flash` fell from 86.1% to **69.0%** with zero provider errors behind the drop, and from
1 unstable cell to 8. Its 3-repeat score was a lucky sample. `qwen3.8-flash`'s 12/12 was the same
illusion from the other direction — it is unmeasurable, below. This is BASELINE.md's stated noise
rule playing out on candidate selection, and it means **no 3-repeat number in this file should be
used to choose a model.**

`claude-haiku-4.5` is the stability outlier: 91.7% in both runs, **0 unstable cells across 84
attempts**, and the same single failing case (`008-ephemeral-flake`) that BASELINE.md records for
it at baseline, Fix 1 and Fix 2. Four independent runs agree.

### Over-emission is recoverable; under-emission is not

This is the criterion that decides extraction, and it does not appear in the pass rates:

- **`gemini-3.8-flash` fails by under-emission.** It emitted *nothing* on all 3 attempts of
  `009-reexported-public-api` and on 2 of 3 of `003`. Nothing is persisted, so no consolidation
  round can recover the fact and the transcript TTLs out — the loss is silent and permanent.
  Disqualifying for extraction regardless of rate.
- **`haiku`'s and `luna`'s failures all over-emit**, and consolidation is measured to clean up
  exactly that: `haiku`'s `008` records a one-off flake (consolidation's `012-discard-ephemeral`
  passes 7/7 on every model tested), `luna`'s `010` restates an already-known fact
  (`013-confirm-duplicate` 7/7), and `luna`'s `003` emits 3 observations against the eval's cap of
  2 — a cap that exists only in the eval, since `_usable()` applies no batch cap in production.

### Discounting `010`

`010-memory-read-only` is the cell BASELINE.md documents as unwinnable while the prompt's
"when you cannot tell which applies, emit it" tie-break stands (`daiv/memory/prompts.py:94`);
all three of its failures here are that exact shape — run `make build`, see it succeed, restate
it. Excluding it: **`gpt-5.6-luna` 94.8% (73/77) vs `claude-haiku-4.5` 90.9% (70/77)**. Excluding
`003`'s eval-only cap as well, `gpt-5.6-luna` is **70/70** and `claude-haiku-4.5` 63/70.

Both exclusions are defensible but not free: `haiku` passes `003` 7/7, which proves that cap *is*
satisfiable rather than a pure artifact, so `003` should not be discounted as freely as `010`.

### `qwen3.8-flash` is disqualified on availability, not quality

**125 of its 168 attempts across two runs were refused upstream** (59/84, then 66/84 in a run
where it was one of only two models), every one
`429 — qwen/qwen3.8-flash is temporarily rate-limited upstream`, while every other model in the
same windows took zero. Its quality is therefore unknown and unmeasured. That is already
sufficient to reject it for the *primary* slot: the eval is harsher than production here, since
`build_structured_llm` gives production a fallback model on exception (and deliberately no
`with_retry`), but a primary that is refused three times in four burns the fallback on every run.

## Consolidation

13 cases. **`memory_consolidation_model_name` defaults to `None`**, i.e. consolidation currently
reuses *the repository's agent model* — so the two "incumbents" below are the likely agent models,
not a configured setting. Adopting a candidate here means *setting* that field for the first time.

| Model | maj-PASS (3 rep) | rate (3 rep) | maj-PASS (7 rep) | **rate (7 rep)** | unstable (7) | rejected ops (3 rep) | $/M in→out |
|---|---|---|---|---|---|---|---|
| `gemini-3.8-flash` | 13/13 | 97.4% | **13/13** | **100.0%** (91/91) | **0** | **0** | 0.75 → 3.75 |
| `gpt-5.6-terra` | 13/13 | 94.9% | 12/13 | 90.1% (82/91) | 3 | **0** | 2.00 → 12.00 |
| `gpt-5.3-codex` *(agent-model incumbent)* | 10/13 | 84.6% | 11/13 | 87.9% (80/91) | 4 | 0 | 1.75 → 14.00 |
| `claude-sonnet-4.6` *(agent-model incumbent)* | 11/13 | 84.6% | 11/13 | 87.9% (80/91) | 1 | **3** | 3.00 → 15.00 |
| `glm-5.3` | 12/13 | 89.7% | — | not measured | — | 0 | 1.40 → 4.40 |
| `qwen3.8-max-0902` | 11/13 | 79.5% | — | not measured | — | 3 | 2.00 → 6.00 |
| `deepseek-v4-pro-0813` | 10/13 | 69.2% | — | not measured | — | 2 | 1.05 → 3.15 |

`gemini-3.8-flash` scored **91/91 — every attempt of every case** — on a run with zero rate-limit
and zero credit errors, corroborating its 97.4% at 3 repeats. It is the only model of the seven
that never lost a single attempt.

### `019-mixed-batch` is the discriminator

| Model | `019` at 7 repeats |
|---|---|
| `gemini-3.8-flash` | **PASS 7/7** |
| `gpt-5.3-codex` | FAIL 2/7 |
| `gpt-5.6-terra` | FAIL 2/7 |
| `claude-sonnet-4.6` | **FAIL 0/7** |

Counting these two runs with BASELINE.md's three prior ones — baseline `FAIL 0/3`, Fix 1
`FAIL 0/3` and Fix 2 `FAIL 0/3` (Fix 1's value is recorded in BASELINE.md's Fix-2 prediction
scoring, which notes `sonnet` "stayed `FAIL 0/3` ... byte-identical verdicts to Fix 1") —
`claude-sonnet-4.6` has failed `019` on **0 of 19 attempts across five independent runs**, with
the same three mistakes every time: `o3`
gets `ADD` where `DISCARD` is required, and `o4`/`o5` target only `['e2']` where the case needs
the `['e2','e3']` cross-entry merge. `gemini-3.8-flash` is the only model tested that solves it
reliably. This is the most reproducible single result in the memory eval's history.

### Two further mechanisms, both deterministic (no judge in the loop)

- **`claude-sonnet-4.6`'s cold-start MERGE defect persists.** On `016-in-batch-dedup` it emits a
  `MERGE` with `entry_ids: []` against a case that has no entries, the validator rejects it
  (`consolidation.py:115 — MERGE must target at least two entries, got 0`) and the round applies
  nothing. It happened on all 3 attempts at 3 repeats and on 4 of 7 at 7 repeats. BASELINE.md
  first identified this at Fix 2 (then 1 of 3 attempts); it is now confirmed across two further
  runs. `sonnet` and `deepseek-v4-pro` are the only models that produce it.
- **`018-update-preferred-over-add` splits the field.** Four models emit `ADD` where `UPDATE` on
  `e1` is required — i.e. they duplicate a fact instead of superseding it: `qwen3.8-max` 0/3,
  `deepseek-v4-pro` 0/3, `glm-5.3` 1/3, and `gpt-5.3-codex` 1/3 (a regression from Fix 2's 3/3,
  which recovered to 6/7 at 7 repeats). `gemini-3.8-flash`, `gpt-5.6-terra` and `sonnet-4.6` are
  clean.
- **Rejected-operation counts** are a grading-noise-immune signal, since operation validation
  never calls the judge. `gemini-3.8-flash`, `gpt-5.6-terra` and `glm-5.3` produced **zero**
  rejected operations across 39 attempts each.

## The cross-suite finding

**`gemini-3.8-flash` is simultaneously the best consolidation model (100%) and disqualified for
extraction (silent under-emission).** Both follow from one disposition — it is conservative about
emitting. In extraction that loses facts outright; in consolidation the same conservatism is
exactly right, because it is what `018` and `019` punish the other models for lacking. Pick per
suite; there is no single best memory model.

## Recommendation

| Setting | Current | Recommended | Basis |
|---|---|---|---|
| `memory_extraction_model_name` | `gpt-5.4-mini` | **`openrouter:openai/gpt-5.6-luna`** | 86.9% vs 67.9% at 7 repeats; 70/70 excluding `003`/`010`; 3.75× cheaper both directions |
| `memory_extraction_fallback_model_name` | `claude-haiku-4.5` | **unchanged** | highest raw rate (91.7%), 0 unstable in 84 attempts, 4 runs agreeing; different upstream from the primary, which is what a fallback is for |
| `memory_consolidation_model_name` | `None` (inherits agent model) | **`openrouter:google/gemini-3.8-flash`** | 91/91 at 7 repeats; only model solving `019`; 4× cheaper than `sonnet-4.6` in and out |

`gpt-5.4-mini` should be retired from extraction: it is last on every reading (67.9% raw, 74.0%
excluding `010`) among the validly-measured models, and it is *more* expensive than the model
replacing it.

Swapping the extraction pair's order alone (`haiku` primary, `gpt-5.4-mini` fallback) is the
conservative variant of the same recommendation, and is defensible if the `010` discount is
rejected — on raw rates `haiku` leads. It costs 5× more per input token than the `luna` primary.

### What remains unmeasured

- `qwen3.8-flash`'s extraction quality — blocked by persistent upstream 429s, not by the harness.
- `gemini-3.8-flash`'s extraction at 7 repeats — only the 3-repeat 83.3% is valid; the mechanism
  finding (under-emission) is what disqualifies it, and that came from the valid 3-repeat run.
- `glm-5.3`, `qwen3.8-max-0902`, `deepseek-v4-pro-0813` at 7 repeats for consolidation. All three
  placed below both incumbents at 3 repeats with clean structural failures on `018`/`019`, so
  confirming them was not worth the spend.
- Per-model latency: the captured logs carry no timestamps. Both memory paths run out-of-band
  (extraction after a run finishes, consolidation on a threshold), so latency is a weak criterion
  here, but it is genuinely unmeasured rather than judged unimportant.
