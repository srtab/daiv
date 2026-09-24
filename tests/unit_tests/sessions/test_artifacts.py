from __future__ import annotations

import uuid

from django.contrib.sites.models import Site
from django.core.files.base import ContentFile

import pytest
from asgiref.sync import sync_to_async
from sessions import artifacts as artifacts_module
from sessions.artifacts import (
    ArtifactError,
    ArtifactKind,
    aresolve_active_run,
    artifact_kind,
    aserialize_run_artifacts,
    astore_artifact,
    guess_content_type,
    serialize_artifact,
)
from sessions.conf import settings as sessions_settings
from sessions.models import Run, RunArtifact, RunStatus, Session, SessionOrigin

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


def _mk_artifact(
    run: Run, *, filename: str = "report.md", content: bytes = b"# Report", title: str = ""
) -> RunArtifact:
    artifact = RunArtifact(
        run=run,
        title=title or filename,
        filename=filename,
        content_type=guess_content_type(filename),
        size=len(content),
    )
    artifact.file.save(filename, ContentFile(content), save=True)
    return artifact


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
        ("archive.tar.gz", "application/x-tar"),
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


async def test_aresolve_active_run_prefers_running_over_ready():
    ready = await _amk_run(status=RunStatus.READY)
    session = await Session.objects.aget(pk=ready.session_id)
    running = await sync_to_async(_mk_run)(session, status=RunStatus.RUNNING)

    resolved = await aresolve_active_run(session.thread_id)

    assert resolved is not None
    assert resolved.pk == running.pk
    assert resolved.pk != ready.pk


async def test_aresolve_active_run_falls_back_to_ready_then_none():
    done = await _amk_run(status=RunStatus.SUCCESSFUL)
    session = await Session.objects.aget(pk=done.session_id)
    assert await aresolve_active_run(session.thread_id) is None

    ready = await sync_to_async(_mk_run)(session, status=RunStatus.READY)
    resolved = await aresolve_active_run(session.thread_id)
    assert resolved is not None
    assert resolved.pk == ready.pk


async def test_aresolve_active_run_unknown_thread():
    assert await aresolve_active_run(str(uuid.uuid4())) is None


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
    artifact = _mk_artifact(run, filename="report.md", content=b"# r", title="Report")

    payload = serialize_artifact(artifact)

    expected_path = f"/dashboard/sessions/{run.session_id}/artifacts/{artifact.pk}/"
    assert payload == {
        "id": str(artifact.pk),
        "title": "Report",
        "filename": "report.md",
        "content_type": "text/markdown",
        "size": 3,
        "url": f"https://daiv.example.com{expected_path}",
        "download_url": f"https://daiv.example.com{expected_path}raw/?download=1",
    }


async def test_aserialize_run_artifacts_orders_oldest_first_and_handles_none():
    run = await _amk_run()
    assert await aserialize_run_artifacts(run) == []

    first = await sync_to_async(_mk_artifact)(run, filename="a.md")
    second = await sync_to_async(_mk_artifact)(run, filename="b.csv", content=b"a,b")

    payloads = await aserialize_run_artifacts(run)

    assert [p["id"] for p in payloads] == [str(first.pk), str(second.pk)]
    assert payloads[1]["content_type"] == "text/csv"


def test_module_documents_content_type_over_sniffing():
    assert "nosniff" in (artifacts_module.__doc__ or "")
