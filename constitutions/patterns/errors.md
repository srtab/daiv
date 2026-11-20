# Errors and diagnostics

Two audiences act on failures: the agent, through what a tool returns, and the operator,
through logs and Sentry. Each gets what it can act on.

## Rules

1. **Log where someone can act.** An operator-actionable failure logs with
   `logger.exception` or `logger.error`. Sentry records events at ERROR and above only, so a
   warning becomes at most a breadcrumb on some later event. Transient, self-healing failures
   may log at warning. _Prevents_: a feature that silently does nothing while Sentry looks
   clean.
2. **Never swallow.** There is no empty `except`. A broad `except` logs the failure, and the
   code states why continuing is safe. _Prevents_: a failure nobody can diagnose because its
   evidence was discarded where it happened.
3. **A partial result beats a bare error.** When a tool or step already has a usable result
   and only a secondary step fails, return the result flagged as degraded. `web_fetch`, for
   example, returns the raw page marked "summariser unavailable" when its summariser fails. A
   bare `error:` result is for when no usable output exists. _Prevents_: throwing away work
   the agent could still use, over a failure only the operator can fix.
4. **Report the real outcome.** Run status, notifications, run classification and merge/pull
   request comments reflect what actually happened; a failed or partial publish is never
   reported as done. _Prevents_: users trusting work that did not land.
5. **Failures do not disclose.** Error text that reaches the agent, a user or an API client
   carries no credential, internal configuration or raw exception; a failed run streams a
   generic `RUN_ERROR` to its session page. The evidence goes to the log. _Prevents_: secrets and internals leaking
   through error paths.
6. **Bind the run.** Every agent run carries its `Run` id (`RunSpec.run_id` /
   `bind_active_run`). A new entry point binds it; webhook tasks use `takes_context=True` and
   resolve it with `aget_task_run_id`, because the Run links to the task result and
   `webhooks.dispatch` commits both in one transaction. Code with no bound run gets no run,
   never a guessed one.
   _Prevents_: artifacts, transcripts and diagnostics attached to the wrong run.
