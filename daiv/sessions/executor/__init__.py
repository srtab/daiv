"""The run executor: a trigger describes one agent run as a ``spec.RunSpec``; ``run.execute_run`` invokes the agent
and ``run.stream_run`` streams it, and either does the rest. The order inside a run, the same for both, and why each
step sits where it does:

1. ``lock.hold_session_lock`` claims the session's execution slot under the spec's ``LockPolicy`` (``execute_run``
   heartbeats it from a background task, a stream between its events, step 4), so a chat turn and a job on one
   session take turns instead of overlapping. A ``Held`` slot was claimed by the caller, which also releases it; the
   executor only heartbeats it. Any error here — a ``Wait`` that gives up (``lock.SessionLockTimeoutError``) or
   another failure inside the claim — reaches ``hooks.on_failure`` without the slot, which was never claimed, so the
   trigger can tell its user. A one-shot run (``thread_id=None``, evals) has no session and holds no slot.
2. The sandbox spec is built from ``spec.sandbox_env_id`` (``None``: the GLOBAL default alone); an id that names
   no environment fails the run here, as a setup error. ``set_runtime_ctx`` clones the repository and opens
   the sandbox client; the checkpointer opens, and a one-shot run checkpoints in memory under a fresh thread id
   instead. If the spec allows it (``fallback_ref_on_missing``) and the clone fell back to another ref than
   ``spec.ref``, the session's working branch is re-pinned to it at once, so the next turn doesn't ask for a
   branch that is gone; a failed re-pin is logged.
   ``hooks.on_context_ready`` then learns the ref the clone landed on, and returns the id of any ``Run`` it creates;
   the agent then runs bound to it (``bind_active_run``), as it does to ``spec.run_id``, and step 6 measures it.
3. The model is resolved, or taken from ``spec.model_names``. For a spec with ``run_id`` it is recorded on the
   ``Run`` and its session before the invoke, so a run that fails mid-way still shows what it ran with.
4. The agent is built over the run's workspace (a sandbox session that ``SandboxMiddleware`` acquires, reusing the
   thread's warm container when it can, or else the worker's clone) and invoked, or handed to ``stream_run``'s stream
   factory, whose events are yielded as they come. Between its events, at most every
   ``run.STREAM_HEARTBEAT_INTERVAL_S``, a stream heartbeats its slot and asks ``should_stop``: a slot a stale takeover
   reassigned raises ``lock.SessionLockLostError``, a stop request ``run.RunStoppedError``, and either one closes the
   stream (the trigger's generator), abandoning the graph run inside it. If the agent or its stream raises (a lost slot
   or a stop request included) and the spec asks for it (``recover_draft``), a draft merge request is published from its
   checkpoint through that workspace while the clone and sandbox are still open, and the checkpoint is read again for
   the failure hook. Setup errors skip this.
5. On success, still inside the context: the checkpoint is read once, the session's working branch is synced
   against the ref the clone landed on (``persist_ref``), the CI watch is armed (``arm_watch``) and the
   ``AgentResult`` is built. A failed checkpoint read yields ``None`` and logs an error, a failed ref sync or watch
   arm is logged; none of them fails a run the agent already finished.
6. The sandbox session, if the run has one, is released — stopped for a session run, whose next turn can reuse it,
   and removed for a one-shot run — even when the agent or its stream raised, was stopped or lost its reader. A stream
   that lost its slot leaves the container running while the checkpoint still names it, since the holder that took
   over reuses it.
   A run with a ``Run`` row (``spec.run_id``, or the one ``on_context_ready`` created) then records on it how long its
   clone took and how its container was acquired (``clone_seconds``, ``sandbox_acquisition``), even when a repeated stop
   cancelled the release; a failed write, or a missing row, is logged. Then the context and the checkpointer close.
7. ``hooks.on_success(outcome)``; or, for any ``Exception`` since the lock step,
   ``hooks.on_failure(exc, draft_published=..., snapshot=...)`` and then the error is re-raised.
8. The heartbeat is cancelled and a ``Wait`` claim released, even when an earlier step raised.

Callers import the submodule they need; this package has no façade.
"""
