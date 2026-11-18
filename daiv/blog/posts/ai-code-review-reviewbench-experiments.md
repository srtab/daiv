---
title: "Cutting AI code review to $0.31 a PR: 7 experiments on ReviewBench, including the ones that failed"
date: 2027-01-26
summary: "Seven measured changes to DAIV's AI code review on ReviewBench, from $1.07 to $0.31 a pull request, including the two ideas I dropped."
author: Sandro Rodrigues
image: code-review.png
---

On ReviewBench's 25 pull requests, which hold 352 known issues, DAIV's code review now finds 150 of them for $7.75 in total, about $0.31 a pull request. That came out of seven measured changes, two of which I dropped. The biggest cost cut was one change: moving the reviewer from Claude Sonnet 5.5 to Haiku 5.5 took a full run from $26.86 to $7.76, about $1.07 to $0.31 a pull request, and found more issues under the same judge. Below is each change with its numbers, and the run-to-run noise to read them against.

The reviewer is `/code-review`, the code-review skill of DAIV, an open-source coding agent for GitLab and GitHub that you host yourself.

It hands the diff to detector subagents, each looking for one kind of problem, and merges what they report. Recall below is the share of known issues found; precision is the share of matched findings that are real issues.

| # | Change | PRs | Result | Verdict |
|---|---|---|---|---|
| — | First baseline, Sonnet 5.5 reviewer | 3 | Recall 13.7%, precision 100%, $4.05 | Starting point |
| 1 | Report more of what the detectors find | 4, then 25 | Recall 13.0% → 21.6% on 4 PRs; 23.3% on all 25, $23.17 | Kept |
| 2 | Confidence floor of 50, 40 or 30 | 25, saved findings | Recall 22.9 / 27.9 / 32.8%, false positives 5 / 7 / 16 | Floor 40 kept |
| 3 | Confidence means likelihood, not severity | 25 | Recall 23.3% → 37.5%, false positives 5 → 13, $23.17 → $26.86 | Kept |
| 4 | Haiku 5.5 instead of Sonnet 5.5 as reviewer | 25 | Recall 37.5% → 44.1%, $26.86 → $7.76 | Kept |
| 5 | Split diffs over 1,000 lines into slices, and count each issue at a site separately | 25 | Sliced PRs 70 → 85 issues for $6.22 → $10.41; small PRs 70 → 57 | Rule dropped |
| 6 | Split diffs over 1,000 lines into slices, without that rule | 11 sliced | 70 → 83 issues, $6.22 → $5.80 | Kept |
| 7 | A tests detector, and two detectors told to run only when needed | 25 | Testing recall 20.0% → 34.3%; the second part did not bind | Detector kept, rest dropped |

Recall and precision are grounded: only known issues count. The first baseline and rows 1 to 6 were judged by Sonnet 5, row 7 and the current baseline by Sonnet 5.5, so compare a row with its own before and after, not with the other rows. Every row is one pass. All of them are in the [experiment log](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/evals/results/reviewbench/README.md#experiment-log), newest first, with the commit of each kept change.

## Setup

[ReviewBench](https://github.com/review-bench/ReviewBench) is an open benchmark of real pull requests, each with a human-reviewed set of known issues. Its test set has 25 pull requests from 25 repositories and 352 known issues. A judge model matches each finding to the known issues it describes, and classifies each unmatched finding as a valid issue or a false positive.

- **Grounded** scores count only the known issues. Grounded precision looks at findings matched to the set: the share that matched a real issue rather than one the set marks as a false positive.
- **Augmented** scores also count valid findings outside the set, so they reward a reviewer for its own discoveries. ReviewBench recommends grounded recall for comparing reviewers, and this post uses it.
- Precision and recall are macro averages: every pull request weighs the same.

The detectors cover correctness, structure, security, performance and the repository's own rules, plus tests from change 7 on. Each reports candidates with a confidence score, and the review agent checks them and writes the report. Haiku ran with thinking set to `xhigh`.

Every review ran with the network off. The current baseline also ran in a DAIV sandbox with no web or MCP tools, because the upstream pull request's review comments are part of what the known issues were built from. Cost is the model usage of the review agent and its detectors at Anthropic's list prices, and leaves out the judge and the sandbox ([benchmarks page](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/docs/reference/benchmarks.md#L58-L76)).

**Noise.** Two identical Haiku runs on the 14 smaller pull requests found 70 and 67 known issues, a grounded recall of 48.5% and 44.0%, and single pull requests moved by up to 3. So a few known issues, or about 4.5 points of recall, is inside the noise. The Sonnet runs in rows 1 to 3 have no noise measure of their own. Changing the judge moves the scores too: on the same findings, Sonnet 5.5 counted 32 false positives where Sonnet 5 counted 17.

![A DAIV code review on a demo GitLab merge request: one Important issue, a test that never asserts which item the offset selects, with its location, evidence and fix, then a Suggestion and recommended actions.](code-review.png)

## The first baseline

The first run used Sonnet 5.5 as the reviewer on 3 pull requests. Grounded precision was 100% and recall 13.7%, for $4.05. Of the 28 known issues in those pull requests, no detector raised 21.

## 1. Report more of what the detectors find

The first change loosened the filters between the detectors and the report, and changed the report's format:

- six candidates on diffs over 1,000 lines;
- candidates with one unverified link are reported;
- a new Suggestion tier;
- a confidence floor of 50 instead of 80;
- every location on the finding's title line, and no closing prose.

On pull requests 0 to 3, grounded recall went from 13.0% to 21.6% and augmented recall from 23.7% to 46.4%, with precision still 100%. Cost rose from $5.37 to $6.32. On all 25 pull requests, grounded recall was 23.3% with 5 false positives in 121 findings, for $23.17.

## 2. Where to put the confidence floor

This one needed no new reviews. The review had dropped 80 candidates scored between 30 and 49, so I judged the saved findings with each floor applied:

| Floor | Grounded recall | Augmented recall | False positives |
|---|---|---|---|
| 50 | 22.9% | 30.7% | 5 |
| 40 | 27.9% | 37.6% | 7 |
| 30 | 32.8% | 44.9% | 16 |

The floor is 40. For comparison, going from 40 to 30 more than doubles the false positives for 4.9 points of recall.

## 3. Confidence means likelihood, not severity

The detectors' instructions asked for a confidence from 0 to 100 without saying what it measured, and they scored Suggestions low however certain they were. So the floor of 40 was acting as a severity filter ([commit](https://github.com/srtab/daiv/commit/6d68a470e71ccfc8cb0674f33c8031c3fb04a47e)). Each detector now scores how likely the finding is to be real, on one shared scale.

This run already had the floor at 40 from change 2, which alone gave 27.9% on change 1's findings. With both, grounded recall went from 23.3% to 37.5% and augmented recall from 31.0% to 48.5%. Findings went from 121 to 215 and false positives from 5 to 13; the known-issue set itself labels 12 of those 13 as false positives. Cost rose from $23.17 to $26.86.

The median confidence of `cr-structure`'s Suggestions went from 45 to 61. The same change also had `cr-correctness` check changed docs, config and tests, which did nothing measurable: testing recall went from 6.8% to 4.8%.

## 4. Haiku instead of Sonnet

The same skill on Haiku 5.5 scored 92.3% grounded precision and 44.1% recall, for $7.76 over 25 pull requests. Sonnet 5.5 had scored 92.7% and 37.5%, for $26.86. That is $1.07 a pull request down to $0.31. The recall gain of 6.6 points is just above the noise band measured later on Haiku.

Haiku drifts from the report template more often: bold section headings instead of the template's, or one-line entries with the location in the title. The benchmark driver reads both forms. Every later experiment ran on Haiku.

## 5 and 6. Slicing big diffs, and a rule I dropped

The review splits a diff over 1,000 lines into at most four slices of whole files, and gives each slice its own correctness and structure detector ([SKILL.md](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/automation/agent/skills/code-review/SKILL.md#L64)).

The first attempt paired slicing with a second rule: each distinct issue at one location is its own candidate. On the 11 sliced pull requests, known issues found went from 70 to 85, but cost went from $6.22 to $10.41, and one pull request, emdash, from $1.31 to $5.20. On the 14 small pull requests, which are not sliced, known issues fell from 70 to 57.

Slicing alone, on the 11 sliced pull requests, took known issues found from 70 to 83 and false positives from 11 to 13, while cost fell from $6.22 to $5.80. Set side by side, across separate runs, the rule added 2 known issues on the sliced pull requests, inside the noise, for $4.61 more. The log records the rule as dropped and slicing as kept.

## 7. A tests detector, and gating that didn't bind

Testing recall stood at 20.0%. The last change added `cr-tests`, a detector that reads only the changed test files. The same change told `cr-security` and `cr-performance` to run only when the diff showed their narrower trigger.

Testing recall went to 34.3%, five more testing issues on one pass. `cr-tests` itself found only 2 of them. Its 21 findings were 3 known issues, 15 valid issues outside the set and 3 false positives, at 4.7% of the cost. The gating did not bind: the security detector still ran on 19 pull requests and the performance detector on 23 of 25.

The before here is the previous baseline: the skill before this change, re-judged by Sonnet 5.5, with 153 known issues found, 32 false positives and $7.34. Against it, known issues fell to 143 and false positives rose to 49. Two pull requests drove that: emdash, with 58 findings for $7.05, and figlet, where the review demoted a real bug to a Question. Without those two, the change was 144 to 141 known issues and 29 to 33 false positives. The detector stayed, and the gating went.

## Where it stands

The current baseline is Haiku 5.5 with every kept change, judged by Sonnet 5.5:

| Metric | Result |
|---|---|
| Grounded precision / recall | 92.1% / 44.6% |
| Augmented precision / recall | 88.9% / 58.6% |
| Known issues found | 150 of 352 |
| Findings / false positives | 301 / 40 |
| Cost | $7.75 for 25, $0.05 to $1.47 each |
| Time | 8.3 minutes on average, 20.3 at most |

Against the previous baseline, known issues went from 153 to 150, inside the noise, and false positives from 32 to 40. Testing recall went from 20.0% to 37.1%, six more testing issues, on one pass each.

## What didn't work

- **Counting each issue at a site separately** added 2 known issues on big diffs for $4.61 more, and the small pull requests lost 13 in the same run.
- **Gating detectors in the prompt** did not bind. The review agent ran them anyway.
- **Asking the correctness detector to also check docs, config and tests** changed nothing that could be measured.

## Open problems

- **The review agent itself is the largest cost**: 28% of the baseline. Its 23.8 million prompt tokens over 25 pull requests nearly match `cr-correctness`'s 24.1 million over 45 tasks, and what it spends them on is not measured yet.
- **The security detector earns little.** In the baseline it ran on 24 pull requests, returned nothing on 15, found no known issue the others missed, and 4 of its 12 findings were false positives, for 12.6% of the cost.
- **Some reviews are unstable.** emdash has cost from $1.24 to $7.05 across runs of nearby versions of the skill.
- **The review agent sometimes overrides the skill.** It demoted a real bug to a Question on figlet, which the skill forbids, and skipped the skill entirely on two pull requests.
- **The weakest categories**, pooled over all 25 pull requests, are low-severity issues at 27.5% recall, maintainability at 15.9% and documentation at 23.8%.
- **It is a small sample.** The 25 pull requests are part of ReviewBench's 219, which DAIV has not run, and these are offline runs, not leaderboard entries ([benchmarks page, limits](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/docs/reference/benchmarks.md#L80-L90)).

## Reproducing it

The [How to run](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/evals/results/reviewbench/README.md#how-to-run) section covers the review run against a local DAIV stack and the judge settings for Claude 5 models. The [baseline files](https://github.com/srtab/daiv/tree/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/evals/results/reviewbench/baseline) hold the findings, the judge's score for each finding, and the usage of every agent. The [benchmarks page](https://docs.daivagent.com/latest/reference/benchmarks/) in the docs has the full results by category and severity.
