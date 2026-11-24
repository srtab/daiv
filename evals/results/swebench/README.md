# SWE-bench results for issue-to-patch

What each change to DAIV's issue-to-patch agent did on [SWE-bench Verified Mini](https://huggingface.co/datasets/MariusHobbhahn/swe-bench-verified-mini), and the baseline the next change is compared against. The driver is [`evals/swebench.py`](../../swebench.py); its module docstring describes what it writes.

## Current baseline

| | |
|---|---|
| DAIV | `6757c19e6`, run on 2026-11-18 |
| Model | Claude Haiku 5.5 on the Anthropic provider (`anthropic:claude-haiku-5-5`), thinking level `high` |
| Tasks | All 50 of the Mini, two passes; each in its task image, with no network and a base-only clone |
| Resolved | **40 and 41 of 50**. Pass 1: django 21 of 25, sphinx 19 of 25 |
| Failed in both passes | `django__django-11790`, `-11848`; `sphinx-doc__sphinx-7590`, `-7748`, `-8056`, `-8548`, `-9229`, `-11510` |
| Flipped between passes | `sphinx-doc__sphinx-9461` resolved only in pass 1; `django__django-11885` and `-12325` only in pass 2 |
| Cost | $0.65 and $0.81 a pass: under $0.05 per task, median $0.009. 94% of input tokens were cache reads |
| Time | 1.2 and 1.3 hours a pass, one task at a time: 1.4 and 1.6 minutes per task on average, 12.2 at most |

Predictions, usage logs, transcripts and grading logs are in `eval-runs/swebench/mini-claude-haiku-5-5-6757c19e6/` and `…-pass2/` of the main checkout.

## The benchmark

- **Mini is two repositories.** Its 50 tasks are 25 django and 25 sphinx tasks of SWE-bench Verified, so a change that helps on other codebases does not show here. `make swebench SWEBENCH_SAMPLES=50` draws 50 of all 500 Verified tasks instead; seed 0's draw spans 10 repositories, about half of them django.
- **Grading uses `SWE-bench/SWE-bench_Verified`.** The harness (swebench 5.0.2) grades only datasets that name each task's Docker image and eval script. `SWE-bench/SWE-bench_Verified` has those columns; the Mini and `princeton-nlp/SWE-bench_Verified` do not. The Mini's tasks match Verified's field for field. The two Verified copies differ only in `PASS_TO_PASS` for `astropy__astropy-7606` and `django__django-10097`, neither of them in the Mini.
- **The agent cannot reach the fix.** In the first two pilots, 7 of 10 runs copied the upstream fix: they downloaded a later release of the project, or cloned its upstream repository. The next DeepSeek pilot had no network, and its first run found the fix as an unreachable object in DAIV's own clone, with `git fsck --unreachable`. So each task now runs with no network, and its clone holds only the base commit's history, with no refs or tags.
- **Each task runs in its own image.** The driver runs each task in its SWE-bench image, `swebench/sweb.eval.x86_64.<task>:latest`, through a sandbox environment that lives only for the run. The image's `testbed` interpreter has the task's dependencies installed, and the project installed in editable mode from the image's own `/testbed` copy. `PYTHONPATH=/workspace/repo/src:/workspace/repo` puts the agent's copy ahead of it, src layout included. The install also generated files there that the repository ignores, such as a version module from setuptools-scm or hatch-vcs and built extensions; the driver copies them from the image into the agent's clone, leaving out build trees and caches, so the package imports from the clone. Being ignored, they stay out of the patch. That interpreter is read-only, so nothing can be installed. Checked on django and sphinx tasks, which generate nothing, and on three SWE-rebench tasks: tox (a src layout with a generated version module), pgmpy, and astropy (17 built extensions). A package nested deeper, such as dagster's `python_modules/dagster`, still imports from the image's copy, so the agent's own test runs miss its edits; the grading is unaffected. pandas and scikit-learn build with meson-python, whose editable install takes every import of the package and first rebuilds it in `/testbed/build/<tag>`, which the sandbox cannot write, so the import fails. The driver turns that rebuild off with `MESONPY_EDITABLE_SKIP` and copies what the build installs into the clone, at the path it installs to; the clone's `.git/info/exclude` keeps the copied files the repository does not ignore, such as pandas's `_version_meson.py`, out of the patch. Checked on `pandas-dev__pandas-64796` and `scikit-learn__scikit-learn-33565`: the agent's test runs imported its own copy, and both resolved.
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

The harness writes its logs and a report, `<model>.grade.json`, next to the predictions. Because the Mini is graded against all of Verified, its "Total instances" line counts 500; the result is "Instances resolved" out of "Instances submitted". Each task's `logs/run_evaluation/grade/<model>/<task>/report.json` names the tests that failed. An "ambiguous failure" with `no_tests_collected` usually means the hidden test patch did not apply: when it adds new files, the harness's reset of the existing test files fails, so a patch that also edited one of them conflicts with it. To see whether the source fix holds, regrade a copy of the prediction without its `tests/` hunks, under another `SWEBENCH_RUN_ID`. Regrading the same predictions reuses the reports already written. Grading the pilot's 5 took under 2 minutes, pulling the images included.

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
- **Run-to-run noise.** Two passes of the baseline flipped 3 of 50 tasks, and the score moved by 1. So a difference of up to 3 tasks between two single passes says nothing; treat a change as real only after the step below.
- **Rerun only what differs.** After a full pass of both versions, rerun the tasks whose result differs with `SWEBENCH_IDS`, twice per side, before calling a difference real.

## Experiment log

Newest first. Runs before `a79192752` could reach the upstream fix; each says whether it did.

| Change | Model | Tasks | Resolved | Cost | Verdict |
|---|---|---|---|---|---|
| Second baseline pass at `6757c19e6`, on 2026-11-18 | Claude Haiku 5.5 on the Anthropic provider, high | All 50 | 41 of 50 | $0.81, 1.3 hours | 3 tasks flipped against pass 1 (`sphinx-doc__sphinx-9461` lost; `django__django-11885`, `-12325` gained), 39 resolved in both, 8 failed in both. `sphinx-doc__sphinx-11510` was ambiguous again |
| First baseline at `6757c19e6`, on 2026-11-18 | Claude Haiku 5.5 on the Anthropic provider, high | All 50 | 40 of 50 | $0.65, 1.2 hours | Baseline. No run raised or left an empty patch. The harness marked `sphinx-doc__sphinx-11510` ambiguous ("no tests collected"): the patch also edited `tests/test_directive_other.py`, and when the hidden tests add new files the harness's reset of existing test files fails, so the hidden test patch did not apply. Graded with the test-file edits removed, the source fix still fails, so it stays unresolved |
| Task prompt at `6757c19e6`: no longer says that a pre-existing test needing change signals a wrong fix, which contradicted DAIV's own test rule | | | Not measured on its own | | Runs before it used the old line, so compare runs on the same side of it. One Haiku run cited the line to submit no patch for `sphinx-doc__sphinx-9229`; the baseline above, run after it, left no empty patch |
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
- **SWE-rebench:** `make swerebench` and `make swerebench-evaluate`, graded with SWE-rebench's harness fork at a pinned commit. Its tasks are unseen only by a model whose training data predates the split. `SWEREBENCH_SPLIT` defaults to `2026_03`, still the newest split of [`nebius/SWE-rebench-leaderboard`](https://huggingface.co/datasets/nebius/SWE-rebench-leaderboard) on 2026-11-18, which the dataset last changed on 2026-07-28; check there for newer ones. Despite its name, it holds 110 tasks created from 2026-03-01 to 2026-05-12, the leaderboard's current Python set. The leaderboard's later tasks, `multilingual_2026_07`, are mostly in other languages and appear only in its trajectories dataset, without the tests to grade them.
  - **Leaderboard reference.** [`nebius/SWE-rebench-leaderboard-trajectories`](https://huggingface.co/datasets/nebius/SWE-rebench-leaderboard-trajectories), subset `python_spring2026`, holds the leaderboard's runs on exactly these 110 tasks, 5 per task, in its own scaffold. Resolved rates there include Claude Opus 5 62%, Claude Sonnet 4.6 51%, GLM 5.2 51%, MiniMax M3 46%, DeepSeek V4 Flash 38%. Its runs have their own prompt and tools, so treat a gap to DAIV as indicative.
  - **Baseline at `3d7695a46`, on 2026-11-18.** Claude Haiku 5.5 on the Anthropic provider, thinking level `high`, resolved **60 of the 110** (54.5%) in one pass, so its run-to-run noise is unmeasured. No run raised or left an empty patch, and every patch applied. Of the 50 misses, 29 passed none of the tests the fix should make pass, 19 passed some, and 2 passed all of them but broke existing tests. Two of the three pandas tasks missed: the agent could not import pandas, which the driver fixed after this run (see "Each task runs in its own image"). Rerun with the fix, `pandas-dev__pandas-64796` resolved. It cost $4.18: $0.005 to $0.38 per task, median $0.02, with 95% of input tokens cache reads. Tasks took 4.1 minutes on average, 27 at most; with grading, the run took 7.75 hours in 6 batches, each batch's images removed after grading. Predictions, usage log, transcripts and grading logs are in `eval-runs/swerebench/2026_03-claude-haiku-5-5-83703c507/` of the main checkout.
  - **Held-out test files, fixed after the baseline.** SWE-rebench's grading script applies the hidden test patch over the agent's tree without first resetting the files that patch touches. So when the agent had created a test file where the hidden tests add one, they did not apply and never ran: 5 baseline misses, though leaderboard runs that created the same files were graded resolved. Agent edits to files the hidden tests also edit merged under them, so those tasks were not graded on the held-out tests alone. The driver now leaves the agent's changes to every file the test patch touches out of the prediction. That changed 71 of the baseline's 110 patches; regraded, it resolves **61 of 110** (55.5%), 62 with the pandas rerun: `hkuds__openharness-217` gained, none lost. The other four now run their hidden tests and still fail, on an error message, a class not exported from the package, label rendering and a parameter check. `harumiweb__exstruct-113`, already unresolved, changed only test files and is now an empty patch. SWE-bench's harness already resets those files before applying its tests, so the Mini baselines stand. The regrade is in `eval-runs/swerebench/2026_03-claude-haiku-5-5-83703c507-heldout-regrade/`.
  - **Disk.** The three images checked share 2.6 GB of layers and add 0.6 to 1.0 GB each; Docker Hub lists all 110 at 174 GB compressed, counting the shared layers in every image. All 110 at once would take about 150 GB, so on a smaller disk run and grade in batches, `docker rmi`-ing each batch's images after grading.
  - **Checked on 2026-11-18** with three of `2026_03`'s 110 tasks, `tox-dev__tox-3904`, `pgmpy__pgmpy-3137` and `astropy__astropy-19438`. The harness grades the gold patches of the first two resolved. In tox's and pgmpy's images, `/testbed` holds the base commit's history only: no refs, and every stored object is reachable from it. A smoke run on Claude Haiku 5.5 resolved all three, for $0.02 to $0.35 a task and 2 to 11 minutes; its tests ran against the agent's copy, built extensions and generated version module included. Before the driver copied the generated files, the tox run lost turns to `No module named 'tox.version'` and wrote a stand-in. Runs are in `eval-runs/swerebench/smoke*/` of the main checkout. The images keep conda in `/opt/conda`, where SWE-bench's use `/opt/miniconda3`; the driver's `PATH` covers both. Before it did, a SWE-rebench task's sandbox had no `python`, and its `python3` was the system's 3.11, without the task's dependencies or pytest.
  - **The interface.** 39 of the split's tasks name the API the hidden tests call, in an `interface` field. Most problem statements already end with it; the driver appends it to the 6 of all 860 tasks whose statement lacks it.
  - **The split spans 79 repositories.** 39 of its tasks are src layouts, which the agent's test runs missed before `PYTHONPATH` covered `src/`. 15 write a version module at install. 4 are pandas or scikit-learn, which build with meson-python. No task has hints. Each image takes 3 to 3.6 GB on disk.
