from __future__ import annotations

from django.core.files.base import ContentFile
from django.urls import reverse

import pytest
from sessions.models import Run, Session, ToolResultMedia
from sessions.views import ARTIFACT_RAW_HEADERS, TOOL_MEDIA_RAW_HEADERS

from tests.unit_tests.sessions.test_views_artifacts import _create_run, _create_session

pytestmark = pytest.mark.django_db


def _media(run: Run, content: bytes = b"png", content_type: str = "image/png") -> ToolResultMedia:
    media = ToolResultMedia(run=run, tool_call_id="call_1", ordinal=0, content_type=content_type, size=len(content))
    media.file.save("media", ContentFile(content), save=True)
    return media


def _own_media(user) -> ToolResultMedia:
    return _media(_create_run(_create_session(user=user)))


def _url(media: ToolResultMedia, session: Session | None = None) -> str:
    thread_id = session.thread_id if session is not None else media.run.session_id
    return reverse("session_tool_media", kwargs={"thread_id": thread_id, "pk": media.pk})


def test_owner_gets_the_bytes_with_the_artifact_headers(member_client, member_user):
    response = member_client.get(_url(_own_media(member_user)))

    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"png"
    assert response["Content-Type"] == "image/png"
    assert all(response[key] == value for key, value in TOOL_MEDIA_RAW_HEADERS.items())
    assert response["Content-Security-Policy"] == ARTIFACT_RAW_HEADERS["Content-Security-Policy"]
    assert response["Cache-Control"] == "private, max-age=86400, immutable"


def test_requires_login(client, member_user):
    response = client.get(_url(_own_media(member_user)))

    assert response.status_code == 302
    assert "login" in response["Location"].lower()


def test_another_users_media_404s(member_client, other_user):
    assert member_client.get(_url(_own_media(other_user))).status_code == 404


def test_media_of_another_session_404s(member_client, member_user):
    media = _own_media(member_user)
    other_session = _create_session(user=member_user)

    assert member_client.get(_url(media, other_session)).status_code == 404


def test_missing_file_404s_and_logs(member_client, member_user, caplog):
    media = _own_media(member_user)
    media.file.storage.delete(media.file.name)

    assert member_client.get(_url(media)).status_code == 404
    assert "MEDIA_ROOT" in caplog.text


def test_a_pdf_downloads_instead_of_opening(member_client, member_user):
    media = _media(_create_run(_create_session(user=member_user)), content=b"%PDF", content_type="application/pdf")

    response = member_client.get(_url(media))

    assert response["Content-Disposition"].startswith("attachment")


def test_the_content_type_follows_the_stored_extension(member_client, member_user):
    media = _media(_create_run(_create_session(user=member_user)), content=b"x", content_type="audio/x-unknown")

    response = member_client.get(_url(media))

    assert response["Content-Type"] == "application/octet-stream"
    assert response["Content-Disposition"].startswith("attachment")


def test_an_image_opens_inline(member_client, member_user):
    response = member_client.get(_url(_own_media(member_user)))

    assert "attachment" not in response.get("Content-Disposition", "")
