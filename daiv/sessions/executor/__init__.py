"""The run executor: a trigger describes one agent run as a ``spec.RunSpec`` and ``run.execute_run``
does the rest. The order inside a run, and why each step sits where it does:

1. ``lock.hold_session_lock`` claims the session's execution slot under the spec's ``LockPolicy`` and
   heartbeats it, so a chat turn and a job on one session take turns instead of overlapping. Any error
   here — a ``Wait`` that gives up (``lock.SessionLockTimeoutError``) or another failure inside the
   claim — reaches ``hooks.on_failure`` without the slot, which was never claimed, so the trigger can
   tell its user.
2. ``set_runtime_ctx`` clones the repository and opens the sandbox client; the checkpointer opens. If
   the spec allows it (``fallback_ref_on_missing``) and the clone fell back to another ref than
   ``spec.ref``, the session's working branch is re-pinned to it at once, so the next turn doesn't ask
   for a branch that is gone; a failed re-pin is logged.
3. The model is resolved. For a spec with ``run_id`` it is recorded on the ``Run`` and its session
   before the invoke, so a run that fails mid-way still shows what it ran with.
4. The agent is built and invoked. If it raises and the spec asks for it (``recover_draft``), a draft
   merge request is published from its checkpoint while the clone and sandbox are still open, and the
   checkpoint is read again for the failure hook. Setup errors skip this.
5. On success, still inside the context: the checkpoint is read once, the session's working branch is
   synced against the ref the clone landed on (``persist_ref``), the CI watch is armed (``arm_watch``)
   and the ``AgentResult`` is built. A failed checkpoint read yields ``None``, and a failed ref sync or
   watch arm is logged; none of them fails a run the agent already finished.
6. The context and the checkpointer close.
7. ``hooks.on_success(outcome)``; or, for any ``Exception`` since the lock step,
   ``hooks.on_failure(exc, draft_published=..., snapshot=...)`` and then the error is re-raised.
8. The heartbeat is cancelled and the slot released, even when an earlier step raised.

Callers import the submodule they need; this package has no façade.
"""
