from __future__ import annotations

import uuid

from django.core.files.base import ContentFile
from django.urls import reverse

import pytest
from sessions import views as views_module
from sessions.artifacts import guess_content_type
from sessions.models import Run, RunArtifact, RunStatus, Session, SessionOrigin

pytestmark = pytest.mark.django_db


def _create_session(**kwargs) -> Session:
    defaults = {
        "thread_id": str(uuid.uuid4()),
        "origin": SessionOrigin.UI_JOB,
        "repo_id": "group/project",
        "ref": "main",
    }
    defaults.update(kwargs)
    return Session.objects.create(**defaults)


def _create_run(session: Session, **kwargs) -> Run:
    defaults = {
        "session": session,
        "trigger_type": SessionOrigin.UI_JOB,
        "repo_id": session.repo_id,
        "status": RunStatus.SUCCESSFUL,
        "user": session.user,
    }
    defaults.update(kwargs)
    return Run.objects.create(**defaults)


def _create_artifact(
    run: Run, *, filename: str = "report.md", content: bytes = b"# Title\n\nbody", title: str = ""
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


def _detail_url(artifact: RunArtifact) -> str:
    return reverse("session_artifact_detail", kwargs={"thread_id": artifact.run.session_id, "pk": artifact.pk})


def _raw_url(artifact: RunArtifact) -> str:
    return reverse("session_artifact_raw", kwargs={"thread_id": artifact.run.session_id, "pk": artifact.pk})


def _own_artifact(user, **artifact_kwargs) -> RunArtifact:
    return _create_artifact(_create_run(_create_session(user=user)), **artifact_kwargs)


def test_detail_requires_login(client, member_user):
    artifact = _own_artifact(member_user)
    resp = client.get(_detail_url(artifact))
    assert resp.status_code == 302
    assert "login" in resp["Location"].lower()


def test_detail_404_for_other_users_artifact(member_client, other_user):
    artifact = _own_artifact(other_user)
    assert member_client.get(_detail_url(artifact)).status_code == 404
    assert member_client.get(_raw_url(artifact)).status_code == 404


@pytest.mark.parametrize("url_name", ["session_artifact_detail", "session_artifact_raw"])
def test_404_when_thread_does_not_own_artifact(member_client, member_user, url_name):
    artifact = _own_artifact(member_user)
    other_session = _create_session(user=member_user)
    url = reverse(url_name, kwargs={"thread_id": other_session.thread_id, "pk": artifact.pk})
    assert member_client.get(url).status_code == 404


def test_session_owner_opens_artifact_of_a_run_someone_else_triggered(member_client, member_user, other_user):
    session = _create_session(user=member_user)
    artifact = _create_artifact(_create_run(session, user=other_user))

    assert member_client.get(_detail_url(artifact)).status_code == 200
    assert member_client.get(_raw_url(artifact)).status_code == 200


def test_detail_renders_markdown_inline(member_client, member_user):
    artifact = _own_artifact(
        member_user, filename="findings.md", content=b"# Findings\n\n<script>x</script>", title="Findings"
    )

    resp = member_client.get(_detail_url(artifact))

    assert resp.status_code == 200
    assert resp.context["kind"] == "markdown"
    html = resp.content.decode()
    assert "<h1>Findings</h1>" in html
    assert "<script>x</script>" not in html
    assert [c["label"] for c in resp.context["breadcrumbs"]] == ["Sessions", "group/project", "Findings"]


def test_detail_embeds_html_in_sandboxed_iframe(member_client, member_user):
    artifact = _own_artifact(member_user, filename="audit.html", content=b"<h1>Audit</h1><script>alert(1)</script>")

    resp = member_client.get(_detail_url(artifact))

    html = resp.content.decode()
    assert resp.context["kind"] == "html"
    assert "<h1>Audit</h1>" not in html
    assert f'<iframe src="{_raw_url(artifact)}"' in html
    assert 'sandbox="allow-scripts allow-popups"' in html


def test_detail_renders_text_escaped(member_client, member_user):
    artifact = _own_artifact(member_user, filename="data.csv", content=b"a,b\n1,<2>")

    resp = member_client.get(_detail_url(artifact))

    assert resp.context["kind"] == "text"
    assert "1,&lt;2&gt;" in resp.content.decode()


def test_detail_renders_image_from_raw_endpoint(member_client, member_user):
    artifact = _own_artifact(member_user, filename="chart.png", content=b"\x89PNG")

    resp = member_client.get(_detail_url(artifact))

    assert resp.context["kind"] == "image"
    assert f'<img src="{_raw_url(artifact)}"' in resp.content.decode()


def test_detail_offers_download_for_unpreviewable_types(member_client, member_user):
    artifact = _own_artifact(member_user, filename="report.pdf", content=b"%PDF-1.4")

    resp = member_client.get(_detail_url(artifact))

    assert resp.context["kind"] == "other"
    assert resp.context["text"] is None
    assert "not previewed" in resp.content.decode()


def test_detail_large_text_is_flagged_too_large_not_unpreviewable(member_client, member_user, monkeypatch):
    monkeypatch.setattr(views_module, "ARTIFACT_INLINE_TEXT_MAX_BYTES", 4)
    artifact = _own_artifact(member_user, filename="big.md", content=b"# too big")

    resp = member_client.get(_detail_url(artifact))

    assert resp.context["kind"] == "markdown"
    assert resp.context["too_large"] is True
    assert resp.context["text"] is None
    html = resp.content.decode()
    assert "too large to preview" in html
    assert "not previewed" not in html


def test_missing_file_renders_unavailable_and_raw_404s(member_client, member_user, caplog):
    artifact = _own_artifact(member_user, filename="audit.html", content=b"<p>x</p>")
    artifact.file.storage.delete(artifact.file.name)

    detail = member_client.get(_detail_url(artifact))
    raw = member_client.get(_raw_url(artifact))

    assert detail.status_code == 200
    assert detail.context["unavailable"] is True
    assert "no longer available" in detail.content.decode()
    assert "<iframe" not in detail.content.decode()
    assert raw.status_code == 404
    assert "MEDIA_ROOT" in caplog.text


def test_raw_requires_login(client, member_user):
    artifact = _own_artifact(member_user)
    resp = client.get(_raw_url(artifact))
    assert resp.status_code == 302


def test_raw_streams_bytes_as_utf8_text(member_client, member_user):
    artifact = _own_artifact(member_user, filename="audit.html", content="<h1>Relatório</h1>".encode())

    resp = member_client.get(_raw_url(artifact))

    assert resp.status_code == 200
    assert b"".join(resp.streaming_content) == "<h1>Relatório</h1>".encode()
    assert resp["Content-Type"] == "text/html; charset=utf-8"
    assert resp["Content-Disposition"] == 'inline; filename="audit.html"'


@pytest.mark.parametrize(
    ("filename", "query"), [("audit.html", ""), ("chart.svg", ""), ("chart.png", ""), ("audit.html", "?download=1")]
)
def test_raw_always_sends_the_sandbox_policy(member_client, member_user, filename, query):
    artifact = _own_artifact(member_user, filename=filename, content=b"<svg/>")

    resp = member_client.get(_raw_url(artifact) + query)

    csp = resp["Content-Security-Policy"]
    assert csp.startswith("sandbox allow-scripts allow-popups;")
    assert "frame-ancestors 'self'" in csp
    assert "connect-src 'none'" in csp
    assert resp["X-Content-Type-Options"] == "nosniff"
    assert resp["X-Frame-Options"] == "SAMEORIGIN"
    assert resp["Referrer-Policy"] == "no-referrer"
    assert resp["Cache-Control"] == "private, no-store"


def test_raw_binary_type_carries_no_charset(member_client, member_user):
    artifact = _own_artifact(member_user, filename="chart.png", content=b"\x89PNG")

    assert member_client.get(_raw_url(artifact))["Content-Type"] == "image/png"


def test_raw_download_flag_forces_attachment(member_client, member_user):
    artifact = _own_artifact(member_user, filename="data.csv", content=b"a,b")

    resp = member_client.get(_raw_url(artifact) + "?download=1")

    assert resp["Content-Disposition"] == 'attachment; filename="data.csv"'
    assert resp["Content-Type"] == "text/csv; charset=utf-8"
    assert b"".join(resp.streaming_content) == b"a,b"


def test_raw_visible_to_admin(admin_client, member_user):
    artifact = _own_artifact(member_user)
    assert admin_client.get(_raw_url(artifact)).status_code == 200
