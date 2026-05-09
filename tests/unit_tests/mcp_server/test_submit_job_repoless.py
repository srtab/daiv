import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.django_db


async def test_submit_job_repos_empty_creates_one_repoless_run():
    from mcp_server.server import submit_job

    fake_activity = MagicMock()
    fake_activity.task_result_id = "task-uuid"
    fake_result = MagicMock()
    fake_result.batch_id = "batch-uuid"
    fake_result.activities = [fake_activity]
    fake_result.failed = []

    with (
        patch("mcp_server.server.get_current_user", AsyncMock(return_value=None)),
        patch("mcp_server.server.asubmit_batch_runs", AsyncMock(return_value=fake_result)) as submit_mock,
    ):
        out = await submit_job(repos=[], prompt="hello")

    parsed = json.loads(out)
    assert parsed["batch_id"] == "batch-uuid"
    assert len(parsed["jobs"]) == 1
    assert parsed["jobs"][0]["repo_id"] is None
    assert parsed["jobs"][0]["ref"] is None
    submit_mock.assert_awaited_once()
    call_kwargs = submit_mock.await_args.kwargs
    assert call_kwargs["repos"] == []


async def test_submit_job_rejects_empty_prompt():
    from mcp_server.server import submit_job

    out = await submit_job(repos=[], prompt="")
    parsed = json.loads(out)
    assert "error" in parsed
