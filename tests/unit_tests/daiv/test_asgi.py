import asyncio
import json

import django

import httpx2
import pytest
from mcp_server.server import mcp

import daiv
from accounts.models import APIKey, User
from automation.agent.mcp.client import MCPHTTPStatusError, build_client, list_tools

pytestmark = pytest.mark.django_db(transaction=True)

DAIV_TOOLS = {
    "submit_job",
    "get_job_status",
    "list_jobs",
    "list_repositories",
    "list_environments",
    "get_environment",
    "schedule_job",
    "list_scheduled_jobs",
}


def _client(app, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else None
    return build_client("http", "http://testserver/mcp", headers, http_transport=httpx2.ASGITransport(app=app))


def _http(app):
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
def user(db):
    return User.objects.create_user(username="asgi-user", email="asgi@test.com", password="testpass")  # noqa: S106


@pytest.fixture
async def api_key(user):
    _, raw_key = await APIKey.objects.create_key(user, name="asgi-key")
    return raw_key


@pytest.fixture
async def mcp_app(monkeypatch):
    # Importing daiv.asgi re-runs django.setup(), and the test LOGGING then disables every existing logger.
    monkeypatch.setattr(django, "setup", lambda **kwargs: None)
    from daiv import asgi

    monkeypatch.setattr(asgi, "_mcp_application", None)
    app = asgi._get_mcp_application()
    started, stop = asyncio.Event(), asyncio.Event()

    async def hold_lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    # anyio needs the lifespan to exit in the task that entered it, but fixture setup and
    # teardown run in different tasks.
    lifespan = asyncio.create_task(hold_lifespan())
    await asyncio.wait([lifespan, asyncio.ensure_future(started.wait())], return_when=asyncio.FIRST_COMPLETED)
    if lifespan.done():
        lifespan.result()
    yield app
    stop.set()
    await lifespan


async def test_mcp_application_runs_stateless(mcp_app):
    assert mcp.session_manager.stateless is True


async def test_unauthenticated_request_is_rejected_with_resource_metadata(mcp_app):
    async with _http(mcp_app) as http:
        response = await http.post("/mcp", json={})

    assert response.status_code == 401
    assert "resource_metadata" in response.headers["www-authenticate"]


async def test_protected_resource_metadata_is_served(mcp_app):
    async with _http(mcp_app) as http:
        response = await http.get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code == 200
    assert response.json()["resource"].endswith("/mcp")


async def test_api_key_client_lists_the_daiv_tools(mcp_app, api_key):
    tools = await list_tools(_client(mcp_app, api_key))

    assert {tool.name for tool in tools} == DAIV_TOOLS


async def test_api_key_client_can_call_list_jobs(mcp_app, api_key):
    tools = await list_tools(_client(mcp_app, api_key))
    list_jobs = next(tool for tool in tools if tool.name == "list_jobs")

    message = await list_jobs.ainvoke({"id": "1", "name": "list_jobs", "args": {}, "type": "tool_call"})

    assert message.status != "error"
    assert json.loads(message.content[0]["text"]) == {"jobs": [], "next_cursor": None}


async def test_bad_token_is_rejected(mcp_app):
    with pytest.raises(MCPHTTPStatusError) as exc_info:
        await list_tools(_client(mcp_app, "not-a-key.secret"))

    assert exc_info.value.status_code == 401


async def test_server_info_reports_the_daiv_version(mcp_app, api_key):
    client = _client(mcp_app, api_key)

    async with client:
        assert client.server_info.version == daiv.__version__
