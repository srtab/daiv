# Agent evals

An agent-behaviour change is one whose purpose is to change what the model does: a system
prompt, a built-in skill, a tool description, a subagent definition, or a middleware that
changes what the model sees.

The commands that run the suites, compare two runs and confirm a flip are in
`../../AGENTS.md`. Changes to `/code-review` are measured on ReviewBench instead, against the
baseline in `evals/results/reviewbench/`.

## Rules

1. **Measure where it pays.** An agent-behaviour change SHOULD carry before/after evidence on
   the suites it affects. _Prevents_: prompt changes that ship on intuition.
2. **Unmeasured is declared.** A pull request that skips measurement says so, and does not
   describe the change as improving agent behaviour or as free of regressions. _Prevents_: an
   unmeasured change presented as a measured one.
3. **A claim cites its comparison.** A claimed improvement or regression includes the
   `compare_runs.py` output, and an `unconfirmed` verdict is confirmed before it is claimed.
   Single runs, single flips and token medians never decide; model output varies enough
   between identical runs to fake them. _Prevents_: noise reported as a result.
4. **Like against like.** Before and after use the same `EVAL_MODEL` and the same suites, and
   a baseline taken on an older `main` is re-run. _Prevents_: drift in `main` or in the model
   credited to the change.
