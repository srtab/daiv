# ReviewBench results for `/code-review`

What each change to the code-review skill did on the 25 [ReviewBench](https://github.com/review-bench/ReviewBench) test-set PRs, and the judged baseline the next change is compared against. The driver is [`evals/reviewbench.py`](../../reviewbench.py); its module docstring describes what it writes.

## Current baseline

`baseline/` holds the judging input (`findings/`), the judge's scores (`results.json`, and per finding in `results.details.json`) and the per-agent usage (`usage/`) of one run:

| | |
|---|---|
| Skill and driver | `d3f513a56`, run on 2026-10-27 |
| Reviewer | `anthropic:claude-haiku-5-5`, thinking `xhigh` |
| Judge | `claude-sonnet-5-5` (classifier and matcher) |
| PRs | all 25 of the test set, one pass |

| Macro average | |
|---|---|
| Grounded precision / recall | 92.1% / 44.6% |
| Augmented precision / recall | 88.9% / 58.6% |
| Known issues found | 150 of 352 |
| Findings / novel true positives / false positives | 301 / 136 / 40 |
| Review cost | $7.75; 8.3 minutes per PR on average, 20.3 at most |

Grounded recall by category, over all known issues: correctness 47.4% (of 133), reliability 55.2% (58), maintainability 15.9% (44), testing 37.1% (35), security 55.6% (27), documentation 23.8% (21), api-architecture 33.3% (12), performance 75.0% (12), accessibility 20.0% (10). By severity: high 58.3% (36), medium 59.0% (134), low 27.5% (182).

Where the cost went and what each agent found. Cost comes from `baseline/usage/`. The finding columns match each detector's report to the judged findings within 5 lines, from the transcripts, so they are approximate; 23 found known issues match no detector report.

| Agent | PRs (tasks) | Cost | Reported findings | Known issues only it found | False positives |
|---|---|---|---|---|---|
| Review agent | 25 | $2.18 (28.2%) | | | |
| `cr-correctness` | 25 (45) | $1.81 (23.4%) | 88 | 39 | 2 |
| `cr-structure` | 24 (44) | $1.21 (15.6%) | 100 | 27 | 10 |
| `cr-security` | 24 | $0.98 (12.6%) | 12 | 0 | 4 |
| `cr-performance` | 22 | $0.92 (11.9%) | 19 | 3 | 3 |
| `cr-tests` | 15 | $0.33 (4.2%) | 24 | 3 | 2 |
| `cr-custom-rules` | 6 | $0.32 (4.1%) | 5 | 0 | 2 |

jadx took three attempts. The first review followed the skill but opened with a verdict paragraph instead of the `## Code Review` heading, so the driver rejected it; on the second, Haiku skipped the skill. Their $0.13 is not in the cost above.

## How to run

**Review.** Start the local stack (`docker start daiv-db daiv-redis daiv-sandbox`), with a provider for the reviewer model and the sandbox API key set in the site configuration. From the repository root, outside any sandboxed shell (the sandbox's loopback is not the host's):

```bash
DB_HOST=127.0.0.1 \
DJANGO_REDIS_URL=redis://127.0.0.1:6379/0 \
DJANGO_REDIS_SESSION_URL=redis://127.0.0.1:6379/1 \
DJANGO_REDIS_CHECKPOINT_URL=redis://127.0.0.1:6379/0 \
DAIV_SANDBOX_URL=http://127.0.0.1:8888 \
CODEBASE_GITLAB_URL=http://127.0.0.1:8929 \
PYTHONPATH=daiv:. LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false \
uv run --env-file docker/local/app/config.env --env-file docker/local/app/config.secrets.env \
  python -m evals.reviewbench --reviewbench-dir ../ReviewBench --output-dir eval-runs/reviewbench/<run> \
  --model-name anthropic:claude-haiku-5-5 --thinking-level xhigh --pr 0 1 14 15 20
```

- The shell's variables win over `--env-file`, which is what points the stack at the host.
- An xhigh review takes 8 minutes on average and up to 20, even on a small PR. Run several `--pr` shards in parallel into one output directory, balanced by `usage.time_in_ms` in the previous run's findings files, not by diff size. Four shards on Haiku hit no rate limits.
- A rerun skips PRs that already have findings, so a stopped run resumes. A PR whose final message is not a review report fails and is retried on the next rerun.

**Judge.** In a ReviewBench checkout, run `npm ci` and add `.judge/models.json`. Their offline-eval example fails with "matcher returned invalid JSON response" on Claude 5 models; these two settings fix it:

```json
{
  "providers": {
    "anthropic": {
      "apiKey": "ANTHROPIC_API_KEY",
      "models": [
        {
          "id": "claude-sonnet-5-5",
          "name": "Claude Sonnet 5.5",
          "reasoning": true,
          "input": ["text"],
          "contextWindow": 1000000,
          "maxTokens": 8192,
          "compat": {"forceAdaptiveThinking": true}
        }
      ]
    }
  }
}
```

```bash
PI_CODING_AGENT_DIR=$PWD/.judge npm run judge -- --candidate <run>/findings --provider anthropic \
  --model claude-sonnet-5-5 --output <run>/scoring/results.json --repo-dir $PWD/.reviewbench-repos --concurrency 4
```

- A classifier call can time out after 1,200 seconds and stop the judge. Rerunning with the same `--output` resumes from `results.checkpoint.json`, so wrap it in a retry loop.
- A candidate set with no findings at all needs `--allow-empty`.

## Comparing runs

- **Same judge only.** Sonnet 5.5 is stricter than Sonnet 5: on the same findings it counted 32 false positives where Sonnet 5 counted 17. When the judge changes, re-judge `baseline/findings` instead of re-running the reviews.
- **Run-to-run noise.** Two identical Haiku runs on the 14 smaller PRs found 70 and 67 known issues, with single PRs moving by up to 3. emdash's cost ranged from $1.24 to $7.05 across runs of nearby skill versions. Judge a change on the PRs it affects, and run each side twice when the difference is a few known issues.
- **Metrics.** Grounded scores count only the golden set (352 known issues over the 25 PRs). Augmented scores also count the judge's novel true positives, valid issues outside the golden set. "Known issues found" is the judge's `golden_covered_count`. Macro averages weight every PR equally.
- **Cost by agent.** `usage/<pr key>.json` splits each review's tokens and cost between the review agent and each `cr-*` detector.

## Experiment log

Newest first. Every review ran with the network off. Results are macro averages unless stated otherwise.

| Change | Reviewer | Judge | PRs | Result | Verdict |
|---|---|---|---|---|---|
| Baseline after `cr-tests` (`d3f513a56`) | Haiku 5.5 xhigh | Sonnet 5.5 | 25 | Grounded P 92.1% / R 44.6%, augmented P 88.9% / R 58.6%, 150 known issues, 301 findings, 40 false positives, $7.75. Against the previous baseline: known issues 153 → 150, within noise; testing recall 20.0% → 37.1%; false positives 32 → 40 | Current baseline |
| `cr-tests` detector, and dispatching `cr-security`/`cr-performance` only when their narrowed condition shows | Haiku 5.5 xhigh | Sonnet 5.5 | 25 | Against the previous baseline: testing recall 20.0% → 34.3%, but `cr-tests` alone found 2 of the 5 extra testing issues; its 21 findings were 3 known issues, 15 novel true positives and 3 false positives, at 4.7% of cost. The gating didn't bind: security still ran on 19 PRs and performance on 23 of 25. Overall 153 → 143 known issues and 32 → 49 false positives, driven by emdash (58 findings, $7.05) and figlet (a real bug demoted to a Question); without those two, 144 → 141 and 29 → 33 | `cr-tests` kept (`b982a7dee`); gating dropped |
| Previous baseline: the skill at `bfaeaa424`, re-judged | Haiku 5.5 xhigh | Sonnet 5.5 | 25 | Grounded P 92.3% / R 45.6%, augmented P 90.3% / R 59.3%, 153 known issues, 314 findings, 32 false positives, $7.34. Assembled from the slicing-only run for the 11 sliced PRs and the first Haiku run for the other 14, whose diffs are too small to slice | Superseded |
| Diff slicing alone: diffs over 1,000 lines split into ≤ 4 whole-file slices, each with its own `cr-correctness` and `cr-structure` task | Haiku 5.5 xhigh | Sonnet 5 | 11 sliced | Known issues 70 → 83, grounded R 38.6% → 43.3%, false positives 11 → 13, cost $6.22 → $5.80 | Kept (`b07e680ce`) |
| Noise pass: the `587a32676` skill run a second time | Haiku 5.5 xhigh | Sonnet 5 | 14 small | 70 vs 67 known issues, grounded R 48.5% vs 44.0%, single PRs moving by up to 3, $1.82 | Sets the noise band |
| Diff slicing plus "each distinct issue at a site is its own candidate" | Haiku 5.5 xhigh | Sonnet 5 | 25 | Sliced PRs 70 → 85 known issues at $6.22 → $10.41 (emdash alone $1.31 → $5.20); the 14 small PRs 70 → 57 | One-issue rule dropped |
| Reviewer model: the skill at `587a32676` on Haiku | Haiku 5.5 xhigh | Sonnet 5 | 25 | Grounded P 92.3% / R 44.1%, augmented R 57.7%, $7.76, against Sonnet 5.5's 92.7% / 37.5% / 48.5% at $26.86. Haiku drifts from the report template more often (bold section headings, one-line entries with the location in the title); the driver reads both since `cca939b94` and `bfaeaa424` | Haiku used for experiments since |
| Detector confidence defined as likelihood, not severity; `cr-correctness` checks changed docs, config and tests; skipped subagents not reported as unavailable | Sonnet 5.5 | Sonnet 5 | 25 | Grounded P 93.3% → 92.7%, R 23.3% → 37.5%; augmented R 31.0% → 48.5%; findings 121 → 215, false positives 5 → 13 (12 of them labelled false positive in the golden set too), $23.17 → $26.86. The calibration carried it (`cr-structure` Suggestion median confidence 45 → 61); the docs/config/test pass had no measurable effect (testing recall 6.8% → 4.8%) | Kept (`6d68a470e`, `587a32676`, `a7b83bdad`) |
| Confidence floor 50, 40 or 30, judged on saved findings plus the 80 dropped candidates scored 30–49 (no new reviews) | Sonnet 5.5 | Sonnet 5 | 25 | Grounded R 22.9% / 27.9% / 32.8%, augmented R 30.7% / 37.6% / 44.9%, false positives 5 / 7 / 16 | Floor 40 kept (`be1befb27`) |
| Report more of what the detectors find: 6 candidates on diffs over 1,000 lines, report candidates with one unverified link, a Suggestion tier, floor 80 → 50, every site on the title line, no closing prose | Sonnet 5.5 | Sonnet 5 | 4, then 25 | PRs 0–3: grounded R 13.0% → 21.6%, augmented R 23.7% → 46.4%, precision 100%, $5.37 → $6.32. All 25: grounded P 93.3% / R 23.3%, augmented P 94.2% / R 31.0%, 121 findings, 5 false positives, $23.17 | Kept (`6708f5aa4`) |
| First baseline | Sonnet 5.5 | Sonnet 5 | 3 | Grounded P 100% / R 13.7%, augmented P 95% / R 27.8%, $4.05. 21 of the 28 known issues were never raised by any detector | — |

## Open leads

- **The review agent's own cost.** It is the largest single share: 28% in the baseline, 37% in the run before, where emdash's review agent alone spent $2.97. Its 23.8M prompt tokens over 25 PRs nearly match `cr-correctness`'s 24.1M over 45 tasks. What it spends them on is not yet measured.
- **`cr-security`'s yield.** In the baseline it ran on 24 PRs, returned nothing on 15, found no known issue the other detectors missed, and 4 of its 12 reported findings were false positives, for 12.6% of cost. Earlier runs found 0–3 issues only it caught. Narrowing its dispatch condition in the prompt did not reduce how often it ran.
- **Unstable reviews.** emdash has ranged from 14 findings at $1.24 to 58 at $7.05; in the baseline it had 30 findings, 7 of them false positives, at $1.47.
- **The review agent overriding the skill.** figlet's review once demoted a real bug to a Question, which step 5 forbids. Haiku has skipped the skill entirely twice (LANdalf, jadx) and once dropped the `## Code Review` heading (jadx).
- **Weakest categories.** Low-severity recall is 27.5%, maintainability 15.9% and documentation 23.8%.
