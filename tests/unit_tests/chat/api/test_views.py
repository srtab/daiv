import pytest
from ninja.testing import TestAsyncClient

from daiv.api import api


@pytest.fixture
def client():
    return TestAsyncClient(api)


def _run_agent_input(**overrides) -> dict:
    return {
        "threadId": "t-1",
        "runId": "r-1",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
        **overrides,
    }


@pytest.mark.django_db
async def test_create_chat_completion_missing_repo_id_header(client: TestAsyncClient):
    response = await client.post("/chat/completions", json=_run_agent_input(), headers={"X-Ref": "main"})
    assert response.status_code == 404


@pytest.mark.django_db
async def test_create_chat_completion_missing_ref_header(client: TestAsyncClient):
    response = await client.post("/chat/completions", json=_run_agent_input(), headers={"X-Repo-ID": "owner/repo"})
    assert response.status_code == 404
