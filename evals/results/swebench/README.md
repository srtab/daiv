# SWE-bench results for issue-to-patch

What each change to DAIV's issue-to-patch agent did on [SWE-bench Verified Mini](https://huggingface.co/datasets/MariusHobbhahn/swe-bench-verified-mini), and the baseline the next change is compared against. The driver is [`evals/swebench.py`](../../swebench.py); its module docstring describes what it writes.

## Current baseline

None yet. The first one is the pilot under [How to run](#how-to-run); record it here as:

| | |
|---|---|
| DAIV | commit, run on date |
| Model | model, thinking level |
| Tasks | all 50 of the Mini, one pass |
| Resolved | n of 50 |
| Cost | total, per task, from the usage log |
| Time | minutes per task on average, at most |

## The benchmark

- **Mini is two repositories.** Its 50 tasks are 25 django and 25 sphinx tasks of SWE-bench Verified, so a change that helps on other codebases does not show here. `make swebench SWEBENCH_SAMPLES=50` draws 50 of all 500 Verified tasks instead; seed 0's draw spans 10 repositories, about half of them django.
- **Grading uses `SWE-bench/SWE-bench_Verified`.** The harness (swebench 5.0.2) grades only datasets that name each task's Docker image and eval script. `SWE-bench/SWE-bench_Verified` has those columns; the Mini and `princeton-nlp/SWE-bench_Verified` do not. The Mini's tasks match Verified's field for field. The two Verified copies differ only in `PASS_TO_PASS` for `astropy__astropy-7606` and `django__django-10097`, neither of them in the Mini.
- **Docker.** Each task has its own image, `swebench/sweb.eval.x86_64.<task>:latest`, about 1.1 GB compressed. The Mini's 50 images share 9.7 GB of compressed layers. On disk, the pilot's 5 images took 1.4 GB each on top of a 1.4 GB base they share, which puts all 50 at about 70 GB. The harness leaves them in place; `docker rmi` them when done.

## How to run

Start the local stack (`docker start daiv-db daiv-redis daiv-sandbox`), with an enabled provider for the model and the sandbox API key set in the site configuration. Run from the repository root, outside any sandboxed shell (the sandbox's loopback is not the host's). The make targets point the run at the main stack from the host; a variable set in the shell wins over their defaults. The run reads the main database, `db`, which must have the checkout's migrations: a missing column fails every task at once, at no cost. In a worktree, use the slot's own database instead, `DB_NAME=db_<WORKTREE_SLOT from .env>`, which the slot's `make up` migrates.

**1. Price it on 5 tasks.** Pick a cheap model from `ModelName` in `daiv/core/constants.py`, such as `openrouter:deepseek/deepseek-v4.1-flash`, `openrouter:z-ai/glm-5.3-flash` or `openrouter:minimax/minimax-m3`, and give the run its own predictions path:

```bash
make swebench-mini SWEBENCH_MODEL=openrouter:deepseek/deepseek-v4.1-flash SWEBENCH_SAMPLES=5 SWEBENCH_PREDICTIONS=eval-runs/swebench/mini-deepseek-v4.1-flash/predictions.json
```

Each task prints its time and cost when it ends, and the run closes with the total of every attempt in the usage log, `predictions.usage.jsonl` next to the predictions. For comparison, the ReviewBench baseline cost $0.31 per PR on Haiku.

**2. Run the other 45.** The same command without `SWEBENCH_SAMPLES`. The 5 predicted tasks are skipped: the sample is the dataset shuffled by `SWEBENCH_SEED` (default 0), and a smaller sample is always the start of a larger one.

**3. Grade.** Docker pulls each task's image the first time:

```bash
make swebench-mini-evaluate SWEBENCH_PREDICTIONS=eval-runs/swebench/mini-deepseek-v4.1-flash/predictions.json
```

The harness writes its logs and a report, `<model>.grade.json`, next to the predictions. Because the Mini is graded against all of Verified, its "Total instances" line counts 500; the result is "Instances resolved" out of "Instances submitted". Each task's `logs/run_evaluation/grade/<model>/<task>/report.json` names the tests that failed. Regrading the same predictions reuses the reports already written. Grading the pilot's 5 took under 2 minutes, pulling the images included.

**Variables.**

| Variable | Default | |
|---|---|---|
| `SWEBENCH_PREDICTIONS` | `eval-runs/swebench/mini/predictions.json` | One path per model, thinking level and DAIV version: the driver refuses to add runs of another model chain or thinking level to a file |
| `SWEBENCH_MODEL` | `openrouter:minimax/minimax-m3` | Several, space-separated, form a fallback chain |
| `SWEBENCH_THINKING` | `high` | Earlier runs used `high` |
| `SWEBENCH_SAMPLES` | all 50 | `make swebench` and `make swerebench` default to 10 |
| `SWEBENCH_SEED` | `0` | |
| `SWEBENCH_IDS` | | Space-separated task IDs; runs exactly those |
| `SWEBENCH_RETRY_FAILED` | | Set to `1` to run again the tasks whose last attempt raised |
| `SWEBENCH_WORKERS` | `4` | Grading containers in parallel |

- **A run that raises counts as unresolved.** It predicts an empty patch, and a rerun does not retry it: retrying agent failures would grade the best of several attempts. `SWEBENCH_RETRY_FAILED=1` retries them; use it after an infrastructure failure, such as a clone or sandbox error, which the traceback names.
- **Ctrl-C** loses only the task in progress, whose partial cost is still logged; the rerun runs it again.
- **Tracing** is off. Set `LANGSMITH_TRACING=true LANGCHAIN_TRACING_V2=true` in the shell to trace; each run carries the task's `instance_id` in its metadata.

## Comparing runs

- **Same setup only.** Compare runs of one model, thinking level, dataset and seed. Each DAIV version gets its own predictions path.
- **Run-to-run noise.** Not measured yet. Run the first baseline twice, into two predictions paths, and record here how many tasks flip between the passes.
- **Rerun only what differs.** After a full pass of both versions, rerun the tasks whose result differs with `SWEBENCH_IDS`, twice per side, before calling a difference real.

## Experiment log

Newest first. Every run was offline, with no web tools.

| Change | Model | Tasks | Resolved | Cost | Verdict |
|---|---|---|---|---|---|
| Pricing pilot at `5c81c016a`, on 2026-11-18 | DeepSeek V4.1 Flash, high | Seed 0's first 5: 3 sphinx, 2 django | 4 of 5. `django__django-11815` passed one of its two target tests: it serialized the enum by `__qualname__`, which breaks an enum defined inside a function | $0.48: $0.01 to $0.22 per task, $0.10 on average. 6.6 minutes per task on average, 12.1 at most. 98% of input tokens were cache reads. All 5 finished with a patch | Puts the 50 at about $5 and 5.5 hours, run one task at a time |

## Other datasets

- **SWE-bench Verified:** `make swebench` and `make swebench-evaluate`, 10 tasks by default.
- **SWE-rebench:** `make swerebench` and `make swerebench-evaluate`, graded with SWE-rebench's harness fork at a pinned commit. Its tasks are unseen only by a model whose training data predates the split. `SWEREBENCH_SPLIT` defaults to `2026_03`, the newest split of [`nebius/SWE-rebench-leaderboard`](https://huggingface.co/datasets/nebius/SWE-rebench-leaderboard) on 2026-11-17; check there for newer ones.
