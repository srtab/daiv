# Contract: Model-fallback notice on a run

## Recording (agent side)

`automation.agent.notices.RunNotices` is a protocol with two methods:

```text
record(notice: ModelFallbackNotice) -> None   # buffers; callable from the synchronous subagent loaders
async aflush() -> None                         # saves everything buffered so far to Run.notices
```

`create_daiv_agent` calls `aflush()` once the agent is built. The skill tool calls it right after
it records a notice.

- `create_daiv_agent(..., notices=None)` accepts an implementation. When it gets `None`, notices
  are only logged.
- The run executor passes the sessions-side recorder bound to the run id. Without a bound run, the
  recorder logs and stores nothing (`constitutions/patterns/errors.md` rule 6).
- The fields of a `ModelFallbackNotice` are in [data-model.md](../data-model.md#run-existing-model-one-new-field).

## When a notice is recorded

| Moment | Target | Condition |
|--------|--------|-----------|
| Agent assembly | `general-purpose`, a code-review detector, a repository subagent | Its configured entry cannot be built |
| Skill invocation | the invoked skill | Its configured entry cannot be built; recorded once per run per entry |

Each recorded notice is also logged at ERROR with the same fields.

## Display (session view)

- Each notice in `Run.notices` becomes one `run_notice` item in the session's turns, after that
  run's turns.
- It renders as a neutral chip with the warning icon and the sentence "<name> ran on
  <served_model>: <configured_model> could not be used (<reason label>)."
- Reason labels: `unknown_provider` → "unknown provider", `provider_unavailable` → "provider
  disabled or missing an API key", `init_failed` → "the model could not be started".
- The notice appears when the session page loads. Live display while the page is open is out of
  scope.
- The run posts nothing extra to the merge or pull request, and the notice never includes
  exception text.
