# Benchmarks

DAIV's [`/code-review`](https://docs.daivagent.com/dev/features/slash-commands/#code-review) is measured on [ReviewBench](https://github.com/review-bench/ReviewBench), an open benchmark of real pull requests. Each pull request comes with a human-reviewed golden set of known issues. This page reports a baseline measured on 2026-10-27 at commit `d3f513a56`, how it was measured, and what it does not tell you.

______________________________________________________________________

## Code review on ReviewBench

|           |                                                                                                                                                                                            |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Benchmark | The ReviewBench test set: 25 pull requests from 25 repositories, 352 known issues                                                                                                          |
| Reviewer  | The `/code-review` skill as merged in [#1711](https://github.com/srtab/daiv/pull/1711), run at `d3f513a56`, on Claude Haiku 5.5 (`anthropic:claude-haiku-5-5`) with thinking level `xhigh` |
| Judge     | Claude Sonnet 5.5, as ReviewBench's matcher and classifier                                                                                                                                 |
| Runs      | One pass over all 25 pull requests, on 2026-10-27                                                                                                                                          |

### Results

Precision and recall are macro averages: every pull request weighs the same.

| Metric              | Result     |
| ------------------- | ---------- |
| Grounded precision  | 92.1%      |
| Grounded recall     | 44.6%      |
| Augmented precision | 88.9%      |
| Augmented recall    | 58.6%      |
| Known issues found  | 150 of 352 |

The review reported 301 findings. 125 matched a known issue, 136 matched none but the judge classified them as valid (novel true positives), and 40 were false positives. One finding can match more than one known issue, so known issues found (150) is higher than matched findings (125).

### Recall by category and severity

Known issues found out of all known issues in that category or severity, pooled over the 25 pull requests rather than averaged per pull request.

| Category         | Known issues | Found | Recall |
| ---------------- | ------------ | ----- | ------ |
| Correctness      | 133          | 63    | 47.4%  |
| Reliability      | 58           | 32    | 55.2%  |
| Maintainability  | 44           | 7     | 15.9%  |
| Testing          | 35           | 13    | 37.1%  |
| Security         | 27           | 15    | 55.6%  |
| Documentation    | 21           | 5     | 23.8%  |
| API architecture | 12           | 4     | 33.3%  |
| Performance      | 12           | 9     | 75.0%  |
| Accessibility    | 10           | 2     | 20.0%  |

| Severity | Known issues | Found | Recall |
| -------- | ------------ | ----- | ------ |
| High     | 36           | 21    | 58.3%  |
| Medium   | 134          | 79    | 59.0%  |
| Low      | 182          | 50    | 27.5%  |

The weak spots are low-severity issues, maintainability and documentation.

### Cost and time

|                      |                                      |
| -------------------- | ------------------------------------ |
| Cost, all 25 reviews | $7.75, about $0.31 per review        |
| Cost per review      | From $0.05 to $1.47                  |
| Time per review      | 8.3 minutes on average, 20.3 at most |

Cost is the model usage of the review agent and its detectors, priced by DAIV's usage tracking at Anthropic's list prices. It does not include the judge or the sandbox.

______________________________________________________________________

## How it was measured

**Review.** [`evals/reviewbench.py`](https://github.com/srtab/daiv/blob/main/evals/reviewbench.py) fetches each pull request's base and head from its ReviewBench mirror and runs `/code-review` on the head in a DAIV sandbox. The network is off and the reviewer has no MCP servers and no web tools, because the upstream pull request's review comments are a source of the golden set. The driver writes the findings in ReviewBench's judging format.

**Judging.** ReviewBench's judge compares the findings with the golden set. A matcher model pairs each finding with the known issues it describes. A classifier model labels each finding the matcher left unmatched as valid or a false positive. Both ran on Claude Sonnet 5.5.

**Grounded and augmented.** Grounded metrics count only the golden set. Grounded precision looks only at findings matched to a golden-set entry: the share whose entry is a known issue rather than one the golden set labels a false positive. Grounded recall is the share of known issues found. Augmented metrics also count the classifier's verdicts on unmatched findings. Augmented precision is the share of all findings that are valid, and augmented recall adds the novel true positives to both the found and the total. ReviewBench's [methodology](https://github.com/review-bench/ReviewBench/blob/main/docs/METHODOLOGY.md) defines all four.

______________________________________________________________________

## Limits

- **25 pull requests.** The test set is a sample of the 219-pull-request full set, which DAIV has not been measured on.
- **One pass.** Results move between identical runs. Two identical runs on the 14 smaller pull requests found 70 and 67 known issues, with single pull requests moving by up to 3.
- **An LLM judge.** The judge model changes the scores. On the same findings, Sonnet 5.5 counted 32 false positives where Sonnet 5 counted 17.
- **Augmented recall grows with the reviewer's own findings.** Its denominator includes the reviewer's novel true positives, so ReviewBench recommends grounded recall for comparing reviewers.
- **One reviewer configuration.** Only Claude Haiku 5.5 at `xhigh` was measured on this version of the skill. Other models will score differently.
- **One retried pull request.** jadx took three attempts: the driver rejected the first report because it lacked the `## Code Review` heading, and the second review skipped the skill. The third is the one scored. The first two cost $0.13, which is not in the cost above.

Not a leaderboard result

This is an offline run. The [ReviewBench leaderboard](https://review-bench.ai) scores all 219 pull requests three times through its own portal, judged by Claude Sonnet 5. These numbers are not a leaderboard submission and are not comparable with leaderboard entries.

______________________________________________________________________

## Experiment log and reproducing

The [experiment log](https://github.com/srtab/daiv/blob/main/evals/results/reviewbench/README.md#experiment-log) lists each measured change to the code-review skill with its result and verdict, the changes that were dropped included. The [baseline files](https://github.com/srtab/daiv/tree/main/evals/results/reviewbench/baseline) hold the findings, the judge's scores per finding and the usage per agent.

To reproduce the run, follow [How to run](https://github.com/srtab/daiv/blob/main/evals/results/reviewbench/README.md#how-to-run): start a local DAIV stack, run `evals/reviewbench.py` on the 25 pull requests, then score the findings with ReviewBench's `npm run judge` on Claude Sonnet 5.5.
