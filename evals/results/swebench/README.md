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
- **The agent cannot reach the fix.** In the first two pilots, 7 of 10 runs copied the upstream fix: they downloaded a later release of the project, or cloned its upstream repository. The next DeepSeek pilot had no network, and its first run found the fix as an unreachable object in DAIV's own clone, with `git fsck --unreachable`. So each task now runs with no network, and its clone holds only the base commit's history, with no refs or tags.
- **Each task runs in its own image.** The driver runs each task in its SWE-bench image, `swebench/sweb.eval.x86_64.<task>:latest`, through a sandbox environment that lives only for the run. The image's `testbed` interpreter has the task's dependencies installed, and `PYTHONPATH=/workspace/repo` points it at the agent's copy of the repository. That interpreter is read-only, so nothing can be installed. Checked on django and sphinx tasks only. A repository with compiled extensions, such as astropy or scikit-learn, may fail to import from the agent's copy until the extensions are built. SWE-rebench images are assumed to have the same layout.
- **Docker.** The run and the grading use the same image, about 1.1 GB compressed; the sandbox pulls it the first time. The Mini's 50 images share 9.7 GB of compressed layers. On disk, the pilot's 5 images took 1.4 GB each on top of a 1.4 GB base they share, which puts all 50 at about 70 GB. Neither the sandbox nor the harness removes them; `docker rmi` them when done.

## How to run

Start the local stack (`docker start daiv-db daiv-redis daiv-sandbox`), with an enabled provider for the model and the sandbox API key set in the site configuration. Run from the repository root, outside any sandboxed shell (the sandbox's loopback is not the host's). The make targets point the run at the main stack from the host; a variable set in the shell wins over their defaults. The run reads the main database, `db`, which must have the checkout's migrations: a missing column fails every task at once, at no cost. In a worktree, use the slot's own database instead, `DB_NAME=db_<WORKTREE_SLOT from .env>`, which the slot's `make up` migrates.

**1. Price it on 5 tasks.** Pick a cheap model from `ModelName` in `daiv/core/constants.py`, such as `openrouter:deepseek/deepseek-v4.1-flash`, `openrouter:z-ai/glm-5.3-flash` or `openrouter:minimax/minimax-m3`, and give the run its own predictions path:

```bash
make swebench-mini SWEBENCH_MODEL=openrouter:deepseek/deepseek-v4.1-flash SWEBENCH_SAMPLES=5 SWEBENCH_PREDICTIONS=eval-runs/swebench/mini-deepseek-v4.1-flash/predictions.json
```

Each task prints its time and cost when it ends, and the run closes with the total of every attempt in the usage log, `predictions.usage.jsonl` next to the predictions. For comparison, the ReviewBench baseline cost $0.31 per PR on Haiku. Each finished task's conversation goes to `predictions.transcripts/<task>.json`, which `.claude/skills/trace-analyzer` reads; a run that raised has none.

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
- **Ctrl-C** loses only the task in progress, whose partial cost is still logged; the rerun runs it again. A run started with `&` from a script ignores Ctrl-C and SIGINT, and SIGTERM leaves its sandbox container and environment row behind.
- **Tracing** is off. Set `LANGSMITH_TRACING=true LANGCHAIN_TRACING_V2=true` in the shell to trace; each run carries the task's `instance_id` in its metadata.

## Comparing runs

- **Same setup only.** Compare runs of one model, thinking level, dataset and seed. Each DAIV version gets its own predictions path.
- **Run-to-run noise.** Not measured on the 50 yet. Run the first baseline twice, into two predictions paths, and record here how many tasks flip between the passes. The only samples so far: two GLM 5.3 Flash passes on the same 5 tasks differed on one, and Claude Haiku 5.5 resolved `django__django-11815` in 2 of 3 runs.
- **Rerun only what differs.** After a full pass of both versions, rerun the tasks whose result differs with `SWEBENCH_IDS`, twice per side, before calling a difference real.

## Experiment log

Newest first. Runs before `a79192752` could reach the upstream fix; each says whether it did.

| Change | Model | Tasks | Resolved | Cost | Verdict |
|---|---|---|---|---|---|
| Reruns of Haiku's two misses, two passes each, with transcripts, on 2026-11-18 | Claude Haiku 5.5 on the Anthropic provider, high | `django__django-11815`, `sphinx-doc__sphinx-9229` | django: 2 of 2. sphinx: 0 of 2; one pass kept the `alias of` line after the docstring, the other changed nothing, judging that the change would break existing tests | $0.08 | The django miss was variance: the failed run also switched to `__qualname__`, which the issue did not ask for. Every sphinx-9229 patch from all three models keeps `alias of :class:` after the docstring, where the hidden test wants the docstring to replace it, while the existing tests pin that line for generic aliases. No harness change would fix it without fitting this task |
| Pricing pilot at `afd7a0918`, on 2026-11-18: task image, no network, base-only clone | Claude Haiku 5.5 on the Anthropic provider, high | The same 5 | 3 of 5. Missed `sphinx-doc__sphinx-9229`, and `django__django-11815`: it serialized the enum by `__qualname__`, which the task's test rejects for an enum defined inside a function | $0.10: under $0.01 to $0.05 per task, $0.02 on average. 1.8 minutes per task on average, 3.4 at most. 95% of input tokens were cache reads | Puts the 50 at about $1 and 1.5 hours. Resolved as many as GLM 5.3 Flash at under a third of its cost and time, and never looked for the fix in git |
| Pricing pilot at `a79192752`, on 2026-11-18: task image, no network, base-only clone | GLM 5.3 Flash, high | Seed 0's first 5: 3 sphinx, 2 django | 3 of 5. Missed `sphinx-doc__sphinx-9229` and `sphinx-doc__sphinx-10673` | $0.35: $0.01 to $0.22 per task, $0.07 on average. 6.3 minutes per task on average, 17.8 at most. 99% of input tokens were cache reads | Puts the 50 at about $3.50 and 5.3 hours, run one task at a time |
| Pricing pilot at `a79192752`, on 2026-11-18: task image, no network, base-only clone | DeepSeek V4.1 Flash, high | The same 5 | 4 of 5. Missed `sphinx-doc__sphinx-9229` | $1.40: $0.01 to $0.74 per task, $0.28 on average. 16.3 minutes per task on average, 40.7 at most. 96% of input tokens were cache reads | Puts the 50 at about $14 and 14 hours. It kept hunting for the fix in git (`fsck`, the reflog, commit SHAs it guessed) on the three sphinx tasks; none of it is in the clone or the image |
| Task image and no network at `1420ed80b`, on 2026-11-18, full clone | GLM 5.3 Flash, high | The same 5 | 4 of 5. Missed `sphinx-doc__sphinx-9229` | $0.21 | Stands: no run read the clone's unreachable objects. Against the pass above, `sphinx-doc__sphinx-10673` flipped |
| Task image and no network at `1420ed80b`, on 2026-11-18, full clone | DeepSeek V4.1 Flash, high | `sphinx-doc__sphinx-9229` | Void: it found the fix with `git fsck --unreachable`, then stopped at the 500-step limit after 31 minutes | $0.35 | Void; stopped after this task |
| Pricing pilot at `3d49c0ffa`, on 2026-11-18, with network | GLM 5.3 Flash, high | The same 5 | Void: 5 of 5, but 3 runs copied the fix from a later Sphinx release | $0.45 | Void |
| Pricing pilot at `5c81c016a`, on 2026-11-18, with network | DeepSeek V4.1 Flash, high | Seed 0's first 5: 3 sphinx, 2 django | Void: 4 of 5, but 4 runs copied the fix from a later release. The miss, `django__django-11815`, copied Django 3.0b1's later `__qualname__` code | $0.48 | Void |

## Other datasets

- **SWE-bench Verified:** `make swebench` and `make swebench-evaluate`, 10 tasks by default.
- **SWE-rebench:** `make swerebench` and `make swerebench-evaluate`, graded with SWE-rebench's harness fork at a pinned commit. Its tasks are unseen only by a model whose training data predates the split. `SWEREBENCH_SPLIT` defaults to `2026_03`, the newest split of [`nebius/SWE-rebench-leaderboard`](https://huggingface.co/datasets/nebius/SWE-rebench-leaderboard) on 2026-11-17; check there for newer ones.
