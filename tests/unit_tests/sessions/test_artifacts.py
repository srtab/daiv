from __future__ import annotations

import json
import uuid
from pathlib import PurePosixPath
from unittest.mock import AsyncMock, Mock, patch

from django.contrib.sites.models import Site
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import DatabaseError
from django.db.backends.base.base import BaseDatabaseWrapper

import pytest
from asgiref.sync import sync_to_async
from deepagents.backends.protocol import FileDownloadResponse
from langchain.tools import ToolRuntime
from sessions import artifacts as artifacts_module
from sessions.artifacts import (
    KNOWN_KIND_CONTENT_TYPES,
    ArtifactKind,
    RunArtifactStore,
    aread_artifact,
    areplace_artifact,
    aresolve_active_run,
    artifact_kind,
    aserialize_run_artifacts,
    aserialize_run_artifacts_for_status,
    astore_artifact,
    bind_active_run,
    content_types_for_kind,
    guess_content_type,
    serialize_artifact,
)
from sessions.conf import settings as sessions_settings
from sessions.models import Run, RunArtifact, RunStatus, Session, SessionOrigin

from automation.agent.artifacts import FETCH_ARTIFACT_TOOL_NAME, PUBLISH_ARTIFACT_TOOL_NAME, ArtifactError
from automation.agent.middlewares.artifacts import ArtifactsMiddleware
from tests.unit_tests.conftest import FakeArtifactStore, FakeWorkspace
from tests.unit_tests.sessions.conftest import make_artifact

pytestmark = pytest.mark.django_db(transaction=True)


def _mk_session(**kwargs) -> Session:
    defaults = {"thread_id": str(uuid.uuid4()), "origin": SessionOrigin.API_JOB, "repo_id": "group/repo"}
    defaults.update(kwargs)
    return Session.objects.create(**defaults)


def _mk_run(session: Session, **kwargs) -> Run:
    defaults = {"trigger_type": SessionOrigin.UI_JOB, "repo_id": session.repo_id, "status": RunStatus.RUNNING}
    defaults.update(kwargs)
    return Run.objects.create(session=session, **defaults)


async def _amk_run(**run_kwargs) -> Run:
    """Session + run built off the event loop (the ORM helpers above are sync)."""
    return await sync_to_async(lambda: _mk_run(_mk_session(), **run_kwargs))()


def _runtime(thread_id: str) -> ToolRuntime:
    return ToolRuntime(
        state={},
        context=Mock(),
        config={"configurable": {"thread_id": thread_id}},
        stream_writer=Mock(),
        tool_call_id="c1",
        store=None,
    )


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("report.html", "text/html"),
        ("REPORT.HTM", "text/html"),
        ("findings.md", "text/markdown"),
        ("notes.markdown", "text/markdown"),
        ("data.csv", "text/csv"),
        ("data.json", "application/json"),
        ("chart.svg", "image/svg+xml"),
        ("chart.png", "image/png"),
        ("out.log", "text/plain"),
        ("archive.tar.gz", "application/octet-stream"),
        ("report.html.gz", "application/octet-stream"),
        ("chart.svgz", "application/octet-stream"),
        ("mystery.bin", "application/octet-stream"),
        ("noext", "application/octet-stream"),
    ],
)
def test_guess_content_type(filename, expected):
    assert guess_content_type(filename) == expected


@pytest.mark.parametrize(
    ("content_type", "kind"),
    [
        ("text/markdown", ArtifactKind.MARKDOWN),
        ("text/html", ArtifactKind.HTML),
        ("image/png", ArtifactKind.IMAGE),
        ("image/svg+xml", ArtifactKind.IMAGE),
        ("text/csv", ArtifactKind.TEXT),
        ("application/json", ArtifactKind.TEXT),
        ("application/pdf", ArtifactKind.OTHER),
        ("application/octet-stream", ArtifactKind.OTHER),
    ],
)
def test_artifact_kind(content_type, kind):
    assert artifact_kind(content_type) == kind


@pytest.mark.parametrize("kind", [ArtifactKind.MARKDOWN, ArtifactKind.HTML, ArtifactKind.IMAGE, ArtifactKind.TEXT])
def test_content_types_for_kind_round_trips_through_artifact_kind(kind):
    content_types = content_types_for_kind(kind)
    assert content_types
    for content_type in content_types:
        assert artifact_kind(content_type) == kind


def test_content_types_for_kind_other_is_empty():
    assert content_types_for_kind(ArtifactKind.OTHER) == frozenset()


@pytest.mark.parametrize("content_type", ["application/pdf", "application/octet-stream"])
def test_content_type_outside_known_kinds_maps_to_other(content_type):
    assert content_type not in KNOWN_KIND_CONTENT_TYPES
    assert artifact_kind(content_type) == ArtifactKind.OTHER


async def test_aresolve_active_run_without_a_bound_run_does_not_guess_one():
    """A RUNNING row may be a run waiting on the session lock, not the one executing."""
    running = await _amk_run(status=RunStatus.RUNNING)
    session = await Session.objects.aget(pk=running.session_id)
    await sync_to_async(_mk_run)(session, status=RunStatus.READY)

    assert await aresolve_active_run(session.thread_id) is None


async def test_aresolve_active_run_prefers_the_bound_run_over_a_newer_waiting_one():
    executing = await _amk_run(status=RunStatus.RUNNING)
    session = await Session.objects.aget(pk=executing.session_id)
    await sync_to_async(_mk_run)(session, status=RunStatus.RUNNING)

    with bind_active_run(executing.pk):
        resolved = await aresolve_active_run(session.thread_id)

    assert resolved is not None
    assert resolved.pk == executing.pk


async def test_aresolve_active_run_bound_to_another_thread_finds_nothing(caplog):
    executing = await _amk_run(status=RunStatus.RUNNING)
    other = await _amk_run(status=RunStatus.RUNNING)

    with bind_active_run(executing.pk):
        assert await aresolve_active_run(other.session_id) is None
    assert f"Bound run {executing.pk} is not on thread {other.session_id}" in caplog.text


async def test_astore_artifact_persists_file_and_metadata():
    run = await _amk_run()

    artifact = await astore_artifact(
        run, filename="/workspace/tmp/audit.html", content=b"<h1>ok</h1>", title="  Audit "
    )

    stored = await RunArtifact.objects.select_related("run").aget(pk=artifact.pk)
    assert stored.run_id == run.pk
    assert stored.title == "Audit"
    assert stored.filename == "audit.html"
    assert stored.content_type == "text/html"
    assert stored.size == len(b"<h1>ok</h1>")
    assert stored.file.name == f"artifacts/{run.pk}/{stored.pk}.html"
    with stored.file.open("rb") as fh:
        assert fh.read() == b"<h1>ok</h1>"


async def test_astore_artifact_title_defaults_to_filename():
    run = await _amk_run()
    artifact = await astore_artifact(run, filename="findings.md", content=b"# x")
    assert artifact.title == "findings.md"


@pytest.mark.parametrize("filename", ["", ".", ".."])
async def test_astore_artifact_rejects_non_file_names(filename):
    run = await _amk_run()
    with pytest.raises(ArtifactError, match="not a file name"):
        await astore_artifact(run, filename=filename, content=b"x")


async def test_astore_artifact_rejects_empty_content():
    run = await _amk_run()
    with pytest.raises(ArtifactError, match="empty"):
        await astore_artifact(run, filename="empty.md", content=b"")
    assert not await RunArtifact.objects.filter(run=run).aexists()


async def test_astore_artifact_enforces_size_cap(monkeypatch):
    monkeypatch.setattr(sessions_settings, "ARTIFACT_MAX_BYTES", 4)
    run = await _amk_run()
    with pytest.raises(ArtifactError, match="capped at 4 bytes"):
        await astore_artifact(run, filename="big.md", content=b"12345")


@pytest.mark.parametrize("failing", ["row", "commit"])
async def test_astore_artifact_removes_the_file_when_persisting_fails(failing):
    run = await _amk_run()
    if failing == "row":
        patcher = patch.object(RunArtifact, "save", side_effect=RuntimeError("db down"))
    else:
        patcher = patch.object(BaseDatabaseWrapper, "commit", side_effect=DatabaseError("db down"))

    with patcher, pytest.raises((RuntimeError, DatabaseError), match="db down"):
        await astore_artifact(run, filename="audit.md", content=b"# a")

    assert await sync_to_async(default_storage.listdir)(f"artifacts/{run.pk}") == ([], [])


async def test_astore_artifact_enforces_per_run_cap(monkeypatch):
    monkeypatch.setattr(sessions_settings, "ARTIFACTS_PER_RUN_MAX", 1)
    run = await _amk_run()
    await astore_artifact(run, filename="one.md", content=b"1")
    with pytest.raises(ArtifactError, match="already published 1 artifacts"):
        await astore_artifact(run, filename="two.md", content=b"2")
    assert await RunArtifact.objects.filter(run=run).acount() == 1


def test_serialize_artifact_uses_site_domain_for_absolute_urls():
    Site.objects.update_or_create(pk=1, defaults={"domain": "daiv.example.com", "name": "DAIV"})
    run = _mk_run(_mk_session())
    artifact = make_artifact(run, filename="report.md", content=b"# r", title="Report")

    payload = serialize_artifact(artifact)

    expected_path = f"/dashboard/sessions/{run.session_id}/artifacts/{artifact.pk}/"
    assert payload.model_dump() == {
        "id": str(artifact.pk),
        "title": "Report",
        "filename": "report.md",
        "content_type": "text/markdown",
        "kind": "markdown",
        "size": 3,
        "url": f"https://daiv.example.com{expected_path}",
        "download_url": f"https://daiv.example.com{expected_path}raw/?download=1",
    }


async def test_aserialize_run_artifacts_orders_oldest_first_and_handles_none():
    run = await _amk_run()
    assert await aserialize_run_artifacts(run) == []

    first = await sync_to_async(make_artifact)(run, filename="a.md")
    second = await sync_to_async(make_artifact)(run, filename="b.csv", content=b"a,b")

    payloads = await aserialize_run_artifacts(run)

    assert [p.id for p in payloads] == [str(first.pk), str(second.pk)]
    assert payloads[1].content_type == "text/csv"


async def test_aserialize_run_artifacts_for_status_degrades_to_a_flagged_empty_list(caplog):
    run = await _amk_run()
    await sync_to_async(make_artifact)(run, filename="a.md")

    with patch.object(artifacts_module, "serialize_artifact", side_effect=RuntimeError("no site")):
        artifacts, error = await aserialize_run_artifacts_for_status(run)

    assert artifacts == []
    assert error is not None and "could not be listed" in error
    assert "Failed to list artifacts" in caplog.text


def test_run_artifact_store_quotes_the_sessions_limits(monkeypatch):
    monkeypatch.setattr(sessions_settings, "ARTIFACT_MAX_BYTES", 1234)
    monkeypatch.setattr(sessions_settings, "ARTIFACTS_PER_RUN_MAX", 7)

    store = RunArtifactStore()

    assert (store.max_bytes, store.per_run_max) == (1234, 7)


async def test_run_artifact_store_accepts_a_publish_only_on_the_bound_runs_thread():
    run = await _amk_run()
    other = await _amk_run()
    store = RunArtifactStore()

    assert await store.aaccepts(run.session_id) is False
    with bind_active_run(run.pk):
        assert await store.aaccepts(run.session_id) is True
        assert await store.aaccepts(other.session_id) is False


async def test_run_artifact_store_files_on_the_bound_run_and_returns_the_published_result():
    await Site.objects.aupdate_or_create(pk=1, defaults={"domain": "daiv.example.com", "name": "DAIV"})
    executing = await _amk_run()
    session = await Session.objects.aget(pk=executing.session_id)
    await sync_to_async(_mk_run)(session, status=RunStatus.RUNNING)

    with bind_active_run(executing.pk):
        result = await RunArtifactStore().astore(
            thread_id=session.thread_id, filename="audit.html", content=b"<h1>Audit</h1>", title="Audit"
        )

    artifact = await RunArtifact.objects.aget(run=executing)
    viewer = f"https://daiv.example.com/dashboard/sessions/{session.thread_id}/artifacts/{artifact.pk}/"
    assert json.loads(result) == {
        "status": "published",
        "id": str(artifact.pk),
        "title": "Audit",
        "filename": "audit.html",
        "content_type": "text/html",
        "kind": "html",
        "size": 14,
        "url": viewer,
        "download_url": f"{viewer}raw/?download=1",
    }


async def test_run_artifact_store_without_a_bound_run_rejects_the_file():
    run = await _amk_run()

    with pytest.raises(ArtifactError, match="no session to attach artifacts to"):
        await RunArtifactStore().astore(thread_id=run.session_id, filename="r.md", content=b"# r")

    assert not await RunArtifact.objects.filter(run=run).aexists()


async def test_run_artifact_store_enforces_the_per_run_budget(monkeypatch):
    monkeypatch.setattr(sessions_settings, "ARTIFACTS_PER_RUN_MAX", 1)
    run = await _amk_run()
    store = RunArtifactStore()

    with bind_active_run(run.pk):
        await store.astore(thread_id=run.session_id, filename="one.md", content=b"1")
        with pytest.raises(ArtifactError, match="already published 1 artifacts"):
            await store.astore(thread_id=run.session_id, filename="two.md", content=b"2")

    assert await RunArtifact.objects.filter(run=run).acount() == 1


async def test_run_artifact_store_returns_a_flagged_relative_url_when_absolute_urls_fail(caplog):
    run = await _amk_run()

    with (
        bind_active_run(run.pk),
        patch.object(artifacts_module, "serialize_artifact", side_effect=RuntimeError("no site")),
    ):
        result = await RunArtifactStore().astore(thread_id=run.session_id, filename="r.md", content=b"# r")

    artifact = await RunArtifact.objects.aget(run=run)
    assert json.loads(result) == {
        "status": "published",
        "id": str(artifact.pk),
        "url": f"/dashboard/sessions/{run.session_id}/artifacts/{artifact.pk}/",
        "warning": "Stored, but DAIV could not build absolute URLs; the URL is relative to the DAIV host.",
    }
    assert "could not build its absolute URLs" in caplog.text


async def test_the_publish_tool_stores_through_the_run_artifact_store():
    """The middleware's tests use a fake store; this one runs the real store through the tool."""
    await Site.objects.aupdate_or_create(pk=1, defaults={"domain": "daiv.example.com", "name": "DAIV"})
    run = await _amk_run()
    path = "/workspace/tmp/report.md"
    workspace = FakeWorkspace()
    workspace.download_file = AsyncMock(return_value=FileDownloadResponse(path=path, content=b"# Report", error=None))
    tool, _ = ArtifactsMiddleware(workspace=workspace, store=RunArtifactStore()).tools
    default_tool, _ = ArtifactsMiddleware(workspace=FakeWorkspace(), store=FakeArtifactStore()).tools

    with bind_active_run(run.pk):
        result = await tool.coroutine(path=path, runtime=_runtime(run.session_id), title="Report")

    artifact = await RunArtifact.objects.aget(run=run)
    assert json.loads(result)["status"] == "published"
    assert json.loads(result)["id"] == str(artifact.pk)
    assert tool.description == default_tool.description


async def _astored_bytes(name: str) -> bytes:
    def read() -> bytes:
        with default_storage.open(name, "rb") as fh:
            return fh.read()

    return await sync_to_async(read)()


async def test_areplace_artifact_swaps_the_file_and_keeps_the_id():
    run = await _amk_run()
    original = await astore_artifact(run, filename="audit.md", content=b"# v1", title="Audit")
    original_name = original.file.name

    revised = await areplace_artifact(run, str(original.pk), filename="audit.html", content=b"<h1>v2</h1>")

    stored = await RunArtifact.objects.aget(pk=original.pk)
    assert revised.pk == original.pk
    assert (stored.run_id, stored.title, stored.filename, stored.content_type, stored.size) == (
        run.pk,
        "Audit",
        "audit.html",
        "text/html",
        len(b"<h1>v2</h1>"),
    )
    assert stored.updated_at is not None
    assert await _astored_bytes(stored.file.name) == b"<h1>v2</h1>"
    assert not await sync_to_async(default_storage.exists)(original_name)


async def test_areplace_artifact_keeps_its_file_on_a_storage_that_overwrites():
    run = await _amk_run()
    original = await astore_artifact(run, filename="audit.md", content=b"# v1")

    with patch.object(default_storage, "get_available_name", side_effect=lambda name, max_length=None: name):
        await areplace_artifact(run, str(original.pk), filename="audit.md", content=b"# v2")

    stored = await RunArtifact.objects.aget(pk=original.pk)
    assert stored.file.name != original.file.name
    assert await _astored_bytes(stored.file.name) == b"# v2"
    assert not await sync_to_async(default_storage.exists)(original.file.name)


async def test_areplace_artifact_takes_a_new_title():
    run = await _amk_run()
    original = await astore_artifact(run, filename="audit.md", content=b"# v1", title="Audit")

    await areplace_artifact(run, str(original.pk), filename="audit.md", content=b"# v2", title=" Audit v2 ")

    assert (await RunArtifact.objects.aget(pk=original.pk)).title == "Audit v2"


async def test_areplace_artifact_from_a_later_run_keeps_the_first_run():
    first = await _amk_run(status=RunStatus.SUCCESSFUL)
    original = await astore_artifact(first, filename="audit.md", content=b"# v1")
    session = await Session.objects.aget(pk=first.session_id)
    second = await sync_to_async(_mk_run)(session, status=RunStatus.RUNNING)

    await areplace_artifact(second, str(original.pk), filename="audit.md", content=b"# v2")

    assert (await RunArtifact.objects.aget(pk=original.pk)).run_id == first.pk


async def test_areplace_artifact_twice_keeps_only_the_latest_file():
    run = await _amk_run()
    original = await astore_artifact(run, filename="audit.md", content=b"# v1")

    await areplace_artifact(run, str(original.pk), filename="audit.md", content=b"# v2")
    await areplace_artifact(run, str(original.pk), filename="audit.md", content=b"# v3")

    stored = await RunArtifact.objects.aget(pk=original.pk)
    stored_names = await sync_to_async(default_storage.listdir)(f"artifacts/{run.pk}")
    assert stored_names == ([], [PurePosixPath(stored.file.name).name])
    assert await _astored_bytes(stored.file.name) == b"# v3"


@pytest.mark.parametrize("artifact_id", ["not-a-uuid", "3f1c2b6e-0d7a-4f0e-9c4b-2a8d6e1f5b70"])
async def test_areplace_artifact_rejects_an_artifact_the_session_lacks(artifact_id):
    run = await _amk_run()

    with pytest.raises(ArtifactError, match=artifact_id):
        await areplace_artifact(run, artifact_id, filename="audit.md", content=b"# v2")


async def test_areplace_artifact_cannot_reach_another_sessions_artifact():
    owner = await _amk_run()
    other = await _amk_run()
    artifact = await astore_artifact(owner, filename="audit.md", content=b"# v1")

    with pytest.raises(ArtifactError, match="this session has no artifact"):
        await areplace_artifact(other, str(artifact.pk), filename="audit.md", content=b"# hijack")

    assert await _astored_bytes((await RunArtifact.objects.aget(pk=artifact.pk)).file.name) == b"# v1"


async def test_areplace_artifact_rejects_empty_content_and_keeps_the_file():
    run = await _amk_run()
    artifact = await astore_artifact(run, filename="audit.md", content=b"# v1")

    with pytest.raises(ArtifactError, match="empty"):
        await areplace_artifact(run, str(artifact.pk), filename="audit.md", content=b"")

    assert await _astored_bytes(artifact.file.name) == b"# v1"


@pytest.mark.parametrize("failing", ["row", "commit"])
async def test_areplace_artifact_keeps_the_old_file_when_persisting_fails(failing):
    run = await _amk_run()
    artifact = await astore_artifact(run, filename="audit.md", content=b"# v1")
    if failing == "row":
        patcher = patch.object(RunArtifact, "save", side_effect=RuntimeError("db down"))
    else:
        patcher = patch.object(BaseDatabaseWrapper, "commit", side_effect=DatabaseError("db down"))

    with patcher, pytest.raises((RuntimeError, DatabaseError), match="db down"):
        await areplace_artifact(run, str(artifact.pk), filename="audit.md", content=b"# v2")

    stored = await RunArtifact.objects.aget(pk=artifact.pk)
    assert stored.file.name == artifact.file.name
    assert await sync_to_async(default_storage.listdir)(f"artifacts/{run.pk}") == ([], [f"{artifact.pk}.md"])


async def test_aread_artifact_returns_the_current_file():
    run = await _amk_run()
    artifact = await astore_artifact(run, filename="audit.md", content=b"# v1")
    await areplace_artifact(run, str(artifact.pk), filename="audit.html", content=b"<h1>v2</h1>")

    assert await aread_artifact(run.session_id, str(artifact.pk)) == ("audit.html", b"<h1>v2</h1>")


async def test_aread_artifact_cannot_reach_another_sessions_artifact():
    owner = await _amk_run()
    other = await _amk_run()
    artifact = await astore_artifact(owner, filename="audit.md", content=b"# v1")

    with pytest.raises(ArtifactError, match="this session has no artifact"):
        await aread_artifact(other.session_id, str(artifact.pk))


async def test_aread_artifact_with_a_missing_file_advises_a_revision(caplog):
    run = await _amk_run()
    artifact = await astore_artifact(run, filename="audit.md", content=b"# v1")
    await sync_to_async(default_storage.delete)(artifact.file.name)

    with pytest.raises(ArtifactError, match=f'is missing.*`artifact_id="{artifact.pk}"`'):
        await aread_artifact(run.session_id, str(artifact.pk))
    assert "is missing" in caplog.text


async def test_aread_artifact_reads_a_revision_that_commits_after_the_lookup():
    run = await _amk_run()
    artifact = await astore_artifact(run, filename="audit.md", content=b"# v1")
    lookup = artifacts_module._session_artifact

    def lookup_then_revise(thread_id: str, artifact_id: str) -> RunArtifact:
        loaded = lookup(thread_id, artifact_id)
        revised = default_storage.save(f"artifacts/{run.pk}/{artifact.pk}-rev.md", ContentFile(b"# v2"))
        RunArtifact.objects.filter(pk=artifact.pk).update(file=revised)
        default_storage.delete(loaded.file.name)
        return loaded

    with patch.object(artifacts_module, "_session_artifact", side_effect=lookup_then_revise):
        assert await aread_artifact(run.session_id, str(artifact.pk)) == ("audit.md", b"# v2")


async def test_run_artifact_store_revises_an_artifact_and_returns_the_updated_result():
    await Site.objects.aupdate_or_create(pk=1, defaults={"domain": "daiv.example.com", "name": "DAIV"})
    run = await _amk_run()
    store = RunArtifactStore()

    with bind_active_run(run.pk):
        published = json.loads(await store.astore(thread_id=run.session_id, filename="audit.md", content=b"# v1"))
        updated = json.loads(
            await store.astore(
                thread_id=run.session_id, filename="audit.md", content=b"# v2", artifact_id=published["id"]
            )
        )

    assert updated == {**published, "status": "updated", "size": 4}
    assert await RunArtifact.objects.filter(run=run).acount() == 1


async def test_aserialize_run_artifacts_lists_what_a_later_run_revised_once():
    first = await _amk_run(status=RunStatus.SUCCESSFUL)
    artifact = await astore_artifact(first, filename="audit.md", content=b"# v1")
    second = await sync_to_async(_mk_run)(await Session.objects.aget(pk=first.session_id))
    bystander = await sync_to_async(_mk_run)(await Session.objects.aget(pk=first.session_id))

    for content in (b"# v2", b"# v3"):
        await areplace_artifact(second, str(artifact.pk), filename="audit.md", content=content)

    assert [p.id for p in await aserialize_run_artifacts(first)] == [str(artifact.pk)]
    assert [(p.id, p.size) for p in await aserialize_run_artifacts(second)] == [(str(artifact.pk), 4)]
    assert await aserialize_run_artifacts(bystander) == []


async def test_run_artifact_store_revisions_do_not_spend_the_run_budget(monkeypatch):
    monkeypatch.setattr(sessions_settings, "ARTIFACTS_PER_RUN_MAX", 1)
    run = await _amk_run()
    store = RunArtifactStore()

    with bind_active_run(run.pk):
        published = json.loads(await store.astore(thread_id=run.session_id, filename="one.md", content=b"1"))
        for content in (b"2", b"3"):
            await store.astore(
                thread_id=run.session_id, filename="one.md", content=content, artifact_id=published["id"]
            )

    assert await RunArtifact.objects.filter(run=run).acount() == 1


async def test_the_fetch_and_publish_tools_revise_an_earlier_runs_artifact():
    """The middleware's tests use a fake store; this one runs a revision through both tools and the real store."""
    first = await _amk_run(status=RunStatus.SUCCESSFUL)
    artifact = await astore_artifact(first, filename="audit.md", content=b"# v1", title="Audit")
    session = await Session.objects.aget(pk=first.session_id)
    second = await sync_to_async(_mk_run)(session, status=RunStatus.RUNNING)
    files: dict[str, bytes] = {}
    workspace = FakeWorkspace()
    workspace.backend.aupload_files = AsyncMock(side_effect=lambda batch: files.update(batch) or [Mock(error=None)])
    workspace.download_file = AsyncMock(
        side_effect=lambda path, **_: FileDownloadResponse(path=path, content=files[path] + b"\n## v2", error=None)
    )
    tools = {t.name: t for t in ArtifactsMiddleware(workspace=workspace, store=RunArtifactStore()).tools}

    with bind_active_run(second.pk):
        fetched = await tools[FETCH_ARTIFACT_TOOL_NAME].coroutine(
            artifact_id=str(artifact.pk), runtime=_runtime(session.thread_id)
        )
        result = await tools[PUBLISH_ARTIFACT_TOOL_NAME].coroutine(
            path="/workspace/tmp/audit.md", runtime=_runtime(session.thread_id), artifact_id=str(artifact.pk)
        )

    assert fetched.startswith(f"Fetched artifact '{artifact.pk}' (audit.md, 4 bytes) to /workspace/tmp/audit.md.")
    assert json.loads(result)["status"] == "updated"
    stored = await RunArtifact.objects.aget(pk=artifact.pk)
    assert (stored.run_id, stored.title) == (first.pk, "Audit")
    assert await _astored_bytes(stored.file.name) == b"# v1\n## v2"
