"""``run_job_task`` must arm the CI watch off the publisher's own verdict.

It reads ``published`` from the checkpoint it already fetches for ``apersist_session_ref``.
Reading ``code_changes`` instead is what made the no-diff give-up unreachable: that flag stays
true for a clean tree already on its merge request, which every fix run is by definition.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jobs.tasks import run_job_task

MR = {"merge_request_id": 7, "source_branch": "daiv/branch"}


def _recorder(armed):
    class _RecordingWatch:
        def __init__(self, repo_id):
            self.repo_id = repo_id

        async def aarm_after_run(self, **kwargs):
            armed.append({"repo_id": self.repo_id, **kwargs})

    return _RecordingWatch


async def _drive(state_values: dict, *, run_id: str | None = None, user_id: int | None = None) -> list[dict]:
    """Run the task over a canned final checkpoint and return the watch-arm calls."""
    armed: list[dict] = []

    last_message = MagicMock()
    last_message.content = "ok"

    agent = AsyncMock()
    agent.ainvoke = AsyncMock(return_value={"messages": [last_message]})
    agent.aget_state = AsyncMock(return_value=SimpleNamespace(values=state_values))

    runtime_ctx = MagicMock()
    runtime_ctx.config.models.agent = MagicMock()

    with (
        patch("jobs.tasks._acquire_session_lock", new=AsyncMock(return_value=None)),
        patch("core.checkpointer.open_checkpointer") as cp_ctx,
        patch("codebase.context.set_runtime_ctx") as rc_ctx,
        patch("automation.agent.graph.create_daiv_agent", new=AsyncMock(return_value=agent)),
        patch(
            "automation.agent.utils.get_daiv_agent_kwargs",
            return_value={"model_names": ["m"], "thinking_level": "medium"},
        ),
        patch("automation.agent.utils.build_langsmith_config", return_value={}),
        patch("automation.agent.results.build_agent_result", new=AsyncMock(return_value={"response": "ok"})),
        patch("automation.agent.usage_tracking.build_usage_summary", return_value=MagicMock(to_dict=lambda: {})),
        patch("automation.agent.usage_tracking.track_usage_metadata"),
        patch("sessions.services.apersist_session_ref", new=AsyncMock()),
        patch("jobs.tasks.PipelineWatch", _recorder(armed)),
    ):
        cp_ctx.return_value.__aenter__.return_value = object()
        rc_ctx.return_value.__aenter__.return_value = runtime_ctx

        await run_job_task.func(
            repo_id="group/repo", prompt="p", ref="main", thread_id="t-1", run_id=run_id, user_id=user_id
        )

    return armed


@pytest.mark.django_db
async def test_it_arms_off_the_published_flag():
    armed = await _drive({"merge_request": MR, "published": True}, run_id="run-1", user_id=9)

    assert len(armed) == 1
    assert armed[0]["repo_id"] == "group/repo"
    assert armed[0]["merge_request"] == MR
    assert armed[0]["published"] is True
    assert armed[0]["run_id"] == "run-1"
    assert armed[0]["user_id"] == 9


@pytest.mark.django_db
async def test_a_clean_tree_on_its_mr_is_not_reported_as_published():
    """The regression that matters: ``code_changes`` is true here, ``published`` is not. Passing
    the former let a fix run that changed nothing re-arm instead of giving up, stranding the
    watch in ``watching`` until the six-hour expiry."""
    armed = await _drive({"merge_request": MR, "code_changes": True, "published": False}, run_id="run-1")

    assert armed[0]["published"] is False
