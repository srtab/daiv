import base64
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx
import pytest
from pydantic import SecretStr


@pytest.fixture
def fake_settings():
    """Patch core.sandbox.client.settings and site_settings to avoid DB access."""
    with (
        patch("core.sandbox.client.settings") as core_settings_patch,
        patch("core.sandbox.client.site_settings") as site_settings_patch,
    ):
        url_mock = MagicMock()
        url_mock.unicode_string.return_value = "http://sandbox.test/"
        core_settings_patch.SANDBOX_URL = url_mock
        site_settings_patch.sandbox_api_key = SecretStr("fake-key")
        site_settings_patch.sandbox_timeout = 10
        yield


@pytest.fixture
def mock_post(monkeypatch):
    """Capture every httpx.AsyncClient.post call; default reply is 200 OK with no body.

    Set ``status`` and ``json_body`` to customise the next reply.
    """
    state: dict = {"status": 200, "json_body": None, "client_ids": []}

    async def fake_post(self, url, **kwargs):
        state["url"] = url
        state["kwargs"] = kwargs
        state["client_ids"].append(id(self))
        return httpx.Response(state["status"], json=state["json_body"], request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return state


async def test_seed_session_posts_repo_archive(fake_settings, mock_post):
    from core.sandbox.client import DAIVSandboxClient

    async with DAIVSandboxClient() as client:
        await client.seed_session("sid-123", repo_archive=b"tar-bytes")

    assert mock_post["url"] == "session/sid-123/seed/"
    field_name, (filename, content, content_type) = next(iter(mock_post["kwargs"]["files"].items()))
    assert field_name == "repo_archive"
    assert content == b"tar-bytes"
    assert content_type == "application/octet-stream"


async def test_seed_session_posts_both_archives(fake_settings, mock_post):
    from core.sandbox.client import DAIVSandboxClient

    async with DAIVSandboxClient() as client:
        await client.seed_session("sid", repo_archive=b"r", skills_archive=b"s")

    files = mock_post["kwargs"]["files"]
    assert set(files.keys()) == {"repo_archive", "skills_archive"}
    assert files["repo_archive"][1] == b"r"
    assert files["skills_archive"][1] == b"s"


async def test_seed_session_skills_only(fake_settings, mock_post):
    from core.sandbox.client import DAIVSandboxClient

    async with DAIVSandboxClient() as client:
        await client.seed_session("sid", skills_archive=b"s")

    assert set(mock_post["kwargs"]["files"].keys()) == {"skills_archive"}


async def test_seed_session_treats_409_as_already_seeded(fake_settings, mock_post):
    from core.sandbox.client import DAIVSandboxClient

    mock_post["status"] = 409
    mock_post["json_body"] = {"detail": "Session already seeded"}
    async with DAIVSandboxClient() as client:
        await client.seed_session("sid-123", repo_archive=b"tar")


async def test_apply_file_mutations_posts_payload(fake_settings, mock_post):
    from core.sandbox.client import DAIVSandboxClient
    from core.sandbox.schemas import ApplyMutationsRequest, PutMutation

    mock_post["json_body"] = {"results": [{"path": "/repo/x.py", "ok": True, "error": None}]}
    req = ApplyMutationsRequest(mutations=[PutMutation(path="/repo/x.py", content=base64.b64encode(b"hi"), mode=0o644)])
    async with DAIVSandboxClient() as client:
        resp = await client.apply_file_mutations("sid", req)

    assert mock_post["url"] == "session/sid/files/"
    assert resp.results[0].ok is True


async def test_apply_file_mutations_returns_per_item_failures(fake_settings, mock_post):
    from core.sandbox.client import DAIVSandboxClient
    from core.sandbox.schemas import ApplyMutationsRequest, PutMutation

    mock_post["json_body"] = {"results": [{"path": "/skills/x", "ok": False, "error": "must be under /repo"}]}
    req = ApplyMutationsRequest(mutations=[PutMutation(path="/skills/x", content=base64.b64encode(b""), mode=0o644)])
    async with DAIVSandboxClient() as client:
        resp = await client.apply_file_mutations("sid", req)

    assert resp.results[0].ok is False
    assert resp.results[0].error == "must be under /repo"


async def test_connection_pool_reused_across_calls_in_one_block(fake_settings, mock_post):
    """One ``async with`` block must reuse a single httpx.AsyncClient across N calls."""
    from core.sandbox.client import DAIVSandboxClient

    async with DAIVSandboxClient() as client:
        await client.seed_session("sid", repo_archive=b"a")
        await client.seed_session("sid", repo_archive=b"b")
        await client.seed_session("sid", repo_archive=b"c")

    assert len(mock_post["client_ids"]) == 3
    assert len(set(mock_post["client_ids"])) == 1, "expected the same httpx.AsyncClient to handle all three calls"


async def test_double_open_raises(fake_settings):
    """Re-entering an already-open client would leak the previous httpx.AsyncClient."""
    from core.sandbox.client import DAIVSandboxClient

    client = DAIVSandboxClient()
    await client.open()
    try:
        with pytest.raises(RuntimeError, match="already open"):
            await client.open()
    finally:
        await client.close()


async def test_call_before_open_raises(fake_settings):
    """Calling a method on a never-opened client raises (it is a programmer error to skip ``open()``)."""
    from core.sandbox.client import DAIVSandboxClient
    from core.sandbox.schemas import RunCommandsRequest

    client = DAIVSandboxClient()
    with pytest.raises(AttributeError):
        await client.run_commands("sid", RunCommandsRequest(commands=["echo"], fail_fast=True))


async def test_fs_write_posts_to_fs_write(fake_settings, mock_post):
    from core.sandbox.client import DAIVSandboxClient
    from core.sandbox.schemas import FsWriteRequest

    mock_post["json_body"] = {"ok": True, "error": None}
    async with DAIVSandboxClient() as client:
        resp = await client.fs_write(
            "sid-1", FsWriteRequest(path="/workspace/a.txt", content=base64.b64encode(b"hi"), mode=0o644)
        )
    assert mock_post["url"] == "session/sid-1/fs/write"
    assert resp.ok is True


async def test_fs_read_parses_response(fake_settings, mock_post):
    from core.sandbox.client import DAIVSandboxClient
    from core.sandbox.schemas import FsReadRequest

    mock_post["json_body"] = {"content": "hello", "encoding": "utf-8", "error": None}
    async with DAIVSandboxClient() as client:
        resp = await client.fs_read("sid-1", FsReadRequest(path="/workspace/a.txt"))
    assert mock_post["url"] == "session/sid-1/fs/read"
    assert resp.content == "hello" and resp.encoding == "utf-8"


@pytest.mark.parametrize(
    ("method_name", "make_request", "json_body", "expected_url"),
    [
        ("fs_ls", lambda s: s.FsLsRequest(path="/workspace/d"), {"entries": [], "error": None}, "session/sid/fs/ls"),
        (
            "fs_grep",
            lambda s: s.FsGrepRequest(pattern="x", path="/workspace/d"),
            {"matches": [], "error": None},
            "session/sid/fs/grep",
        ),
        (
            "fs_glob",
            lambda s: s.FsGlobRequest(pattern="*.py", path="/workspace/d"),
            {"matches": [], "error": None},
            "session/sid/fs/glob",
        ),
        (
            "fs_edit",
            lambda s: s.FsEditRequest(path="/workspace/a", old="a", new="b"),
            {"occurrences": 1, "error": None},
            "session/sid/fs/edit",
        ),
        (
            "fs_delete",
            lambda s: s.FsDeleteRequest(path="/workspace/a"),
            {"ok": True, "error": None},
            "session/sid/fs/delete",
        ),
    ],
)
async def test_fs_methods_post_to_expected_url(
    fake_settings, mock_post, method_name, make_request, json_body, expected_url
):
    """Pin the endpoint string of every fs_* method so a wrong route (e.g. fs/delete→fs/remove)
    is caught — the backend tests use a mock client and wouldn't notice."""
    from core.sandbox import schemas
    from core.sandbox.client import DAIVSandboxClient

    mock_post["json_body"] = json_body
    async with DAIVSandboxClient() as client:
        await getattr(client, method_name)("sid", make_request(schemas))
    assert mock_post["url"] == expected_url


async def test_session_exists_true_on_204():
    from core.sandbox.client import DAIVSandboxClient

    client = DAIVSandboxClient()
    client._client = Mock()
    client._client.get = AsyncMock(return_value=Mock(status_code=204, raise_for_status=Mock()))
    assert await client.session_exists("sid") is True
    client._client.get.assert_awaited_once_with("session/sid/")


async def test_session_exists_false_on_404():
    from core.sandbox.client import DAIVSandboxClient

    client = DAIVSandboxClient()
    client._client = Mock()
    client._client.get = AsyncMock(return_value=Mock(status_code=404))
    assert await client.session_exists("sid") is False


async def test_session_exists_raises_on_non_404_error():
    """A transient sandbox error (e.g. 500) must propagate, not be treated as "session gone" —
    the caller falls back to a cold create only on a real HTTP error, never silently discards a
    live session on a blip."""
    from core.sandbox.client import DAIVSandboxClient

    client = DAIVSandboxClient()
    client._client = Mock()
    err = httpx.HTTPStatusError("boom", request=httpx.Request("GET", "x"), response=httpx.Response(500))
    client._client.get = AsyncMock(return_value=Mock(status_code=500, raise_for_status=Mock(side_effect=err)))
    with pytest.raises(httpx.HTTPStatusError):
        await client.session_exists("sid")


async def test_close_session_default_stops_via_force_false():
    from core.sandbox.client import DAIVSandboxClient

    client = DAIVSandboxClient()
    client._client = Mock()
    client._client.delete = AsyncMock(return_value=Mock(raise_for_status=Mock()))
    await client.close_session("sid")
    client._client.delete.assert_awaited_once_with("session/sid/", params={"force": False})


async def test_close_session_force_removes():
    from core.sandbox.client import DAIVSandboxClient

    client = DAIVSandboxClient()
    client._client = Mock()
    client._client.delete = AsyncMock(return_value=Mock(raise_for_status=Mock()))
    await client.close_session("sid", force=True)
    client._client.delete.assert_awaited_once_with("session/sid/", params={"force": True})
