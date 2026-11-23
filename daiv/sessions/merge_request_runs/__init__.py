"""Running something on a merge request: a mention of DAIV on it, or a job that names it.

The trigger apps call :class:`sessions.merge_request_runs.service.MergeRequestRuns`, which sends the run where a mention
by its caller would go: the session that owns the merge request, or the merge request's own session
(:class:`sessions.merge_request_runs.starter.RunStarter`), whose task (:mod:`sessions.merge_request_runs.tasks`) runs
the addressor (:mod:`sessions.merge_request_runs.addressor`) and posts its outcome on the merge request.
"""
