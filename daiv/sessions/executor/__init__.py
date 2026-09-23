"""The run executor: a trigger describes one agent run as a ``spec.RunSpec`` and ``run.execute_run``
does the rest. The order inside a run, and why each step sits where it does:

1. ``lock.hold_session_lock`` claims the session's execution slot under the spec's ``LockPolicy`` and
   heartbeats it, so no two turns write the same checkpoint at once.
2. ``set_runtime_ctx`` clones the repository and opens the sandbox client; the checkpointer opens.
3. The model is resolved. For a spec with ``run_id`` it is recorded on the ``Run`` and its session
   before the invoke, so a run that fails mid-way still shows what it ran with.
4. The agent is built and invoked.
5. On success, still inside the context: the checkpoint is read once, the session's working branch is
   synced (``persist_ref``), the CI watch is armed (``arm_watch``) and the ``AgentResult`` is built.
   Ref and watch failures are logged and swallowed; the run already published its work.
6. The context and the checkpointer close.
7. ``hooks.on_success(outcome)``; or, for any error since the slot was claimed,
   ``hooks.on_failure(exc, draft_published=...)`` and then the error is re-raised.
8. The heartbeat is cancelled and the slot released, even when an earlier step raised.

Hooks therefore run while the slot is still held but after the clone and sandbox client are gone.
Callers import the submodule they need; this package has no façade.
"""
