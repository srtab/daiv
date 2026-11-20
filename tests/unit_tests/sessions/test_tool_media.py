from __future__ import annotations

import uuid
from unittest.mock import patch

from django.core.files.storage import Storage, default_storage
from django.urls import reverse

import pytest
from asgiref.sync import sync_to_async
from langchain_core.messages import ToolMessage
from sessions.artifacts import bind_active_run
from sessions.conf import settings as sessions_settings
from sessions.models import Run, RunStatus, Session, SessionOrigin, ToolResultMedia
from sessions.tool_media import RunToolMediaStore, TranscriptMedia

from automation.agent.tool_media import ToolMediaError

pytestmark = pytest.mark.django_db(transaction=True)


def _mk_run() -> Run:
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.API_JOB, repo_id="group/repo")
    return Run.objects.create(
        session=session, trigger_type=SessionOrigin.UI_JOB, repo_id=session.repo_id, status=RunStatus.RUNNING
    )


async def _astore(run: Run, *, ordinal: int = 0, mime_type: str = "image/png", content: bytes = b"png") -> str:
    with bind_active_run(run.pk):
        return await RunToolMediaStore().astore(
            thread_id=run.session_id, tool_call_id="call_1", ordinal=ordinal, mime_type=mime_type, content=content
        )


async def _rejection(run: Run, **kwargs) -> str:
    with pytest.raises(ToolMediaError) as exc_info:
        await _astore(run, **kwargs)
    return exc_info.value.code


@pytest.mark.parametrize(
    ("mime", "expected"),
    [
        ("image/png", "image/png"),
        ("IMAGE/PNG", "image/png"),
        ("image/png; charset=binary", "image/png"),
        ("image/jpeg", "image/jpeg"),
        ("image/webp", "image/webp"),
        ("audio/x-wav", "audio/x-wav"),
        ("video/mp4", "video/mp4"),
        ("video/x-unknown", None),
        ("application/pdf", "application/pdf"),
        ("image/svg+xml", None),
        ("text/html", None),
        ("application/octet-stream", None),
        ("", None),
    ],
)
def test_stored_content_type(mime, expected):
    assert RunToolMediaStore.content_type(mime) == expected


async def test_astore_saves_the_bytes_on_the_bound_run():
    run = await sync_to_async(_mk_run)()

    media_id = await _astore(run, ordinal=2)

    row = await ToolResultMedia.objects.aget(pk=media_id)
    assert (row.run_id, row.tool_call_id, row.ordinal) == (run.pk, "call_1", 2)
    assert (row.content_type, row.size) == ("image/png", 3)
    assert row.file.name == f"tool-media/{row.pk}.png"
    with row.file.open("rb") as fh:
        assert fh.read() == b"png"


async def test_astore_rejects_an_unsupported_type():
    run = await sync_to_async(_mk_run)()

    assert await _rejection(run, mime_type="image/svg+xml") == "unsupported_type"
    assert not await ToolResultMedia.objects.aexists()


async def test_astore_enforces_the_size_cap(monkeypatch):
    monkeypatch.setattr(sessions_settings, "TOOL_MEDIA_MAX_BYTES", 2)
    run = await sync_to_async(_mk_run)()

    assert await _rejection(run) == "too_large"
    assert not await ToolResultMedia.objects.aexists()


async def test_astore_enforces_the_per_run_cap(monkeypatch):
    monkeypatch.setattr(sessions_settings, "TOOL_MEDIA_PER_RUN_MAX", 1)
    run = await sync_to_async(_mk_run)()
    kept = await ToolResultMedia.objects.aget(pk=await _astore(run))

    with patch.object(Storage, "save", side_effect=AssertionError("wrote the file")):
        assert await _rejection(run, ordinal=1) == "run_limit"
    assert default_storage.exists(kept.file.name)


async def test_astore_without_a_bound_run_fails():
    run = await sync_to_async(_mk_run)()

    with pytest.raises(ToolMediaError) as exc_info:
        await RunToolMediaStore().astore(
            thread_id=run.session_id, tool_call_id="call_1", ordinal=0, mime_type="image/png", content=b"png"
        )

    assert exc_info.value.code == "failed"


async def test_astore_rejects_a_type_before_looking_up_the_run():
    with pytest.raises(ToolMediaError) as exc_info:
        await RunToolMediaStore().astore(
            thread_id="t1", tool_call_id="call_1", ordinal=0, mime_type="text/html", content=b"<p>"
        )

    assert exc_info.value.code == "unsupported_type"


async def test_deleting_the_run_removes_the_file():
    run = await sync_to_async(_mk_run)()
    row = await ToolResultMedia.objects.aget(pk=await _astore(run))
    assert default_storage.exists(row.file.name)

    await run.adelete()

    assert not default_storage.exists(row.file.name)


STORED_ID = "11111111-1111-1111-1111-111111111111"


def test_media_items_maps_each_entry_kind():
    entries = [
        {"ordinal": 0, "kind": "image", "mime_type": "image/png", "id": STORED_ID, "size": 3},
        {"ordinal": 1, "kind": "image", "mime_type": "image/png", "url": "https://x.test/a.png"},
        {"ordinal": 2, "kind": "file", "mime_type": "application/pdf", "error": "too_large", "size": 99},
        {"ordinal": 3, "kind": "image", "mime_type": "image/png", "url": "javascript:alert(1)"},
        {"ordinal": 4, "kind": "image", "mime_type": "image/png", "url": "data:image/png;base64,AAAA"},
    ]

    assert TranscriptMedia("t1").items(entries) == [
        {
            "kind": "image",
            "mime_type": "image/png",
            "size": 3,
            "url": reverse("session_tool_media", kwargs={"thread_id": "t1", "pk": STORED_ID}),
        },
        {"kind": "image", "mime_type": "image/png", "url": "https://x.test/a.png", "external": True},
        {"kind": "file", "mime_type": "application/pdf", "size": 99, "error": "too_large"},
        {"kind": "image", "mime_type": "image/png", "error": "unsupported_type"},
        {"kind": "image", "mime_type": "image/png", "error": "unsupported_type"},
    ]


def test_untagged_message_shows_not_saved_placeholders():
    message = ToolMessage(
        content=[
            {"type": "text", "text": "x"},
            {"type": "image", "base64": "AAAA", "mime_type": "image/png"},
            {"type": "image", "url": "https://x.test/a.png", "mime_type": "image/png"},
            {"type": "image", "url": "data:image/png;base64,AAAA", "mime_type": "image/png"},
        ],
        tool_call_id="c1",
    )

    assert TranscriptMedia("t1").of_message(message) == [
        {"kind": "image", "mime_type": "image/png", "error": "not_saved"},
        {"kind": "image", "mime_type": "image/png", "url": "https://x.test/a.png", "external": True},
        {"kind": "image", "mime_type": "image/png", "error": "not_saved"},
    ]


def test_tagged_message_uses_its_entries():
    entries = [{"ordinal": 0, "kind": "image", "mime_type": "image/png", "error": "run_limit"}]
    message = ToolMessage(
        content=[{"type": "image", "base64": "AAAA", "mime_type": "image/png"}],
        tool_call_id="c1",
        additional_kwargs={"daiv_media": entries},
    )

    assert TranscriptMedia("t1").of_message(message) == [
        {"kind": "image", "mime_type": "image/png", "error": "run_limit"}
    ]


def test_text_only_message_has_no_media():
    assert TranscriptMedia("t1").of_message(ToolMessage(content="ok", tool_call_id="c1")) == []


def test_a_url_that_is_not_a_string_is_unsupported():
    entries = [{"ordinal": 0, "kind": "image", "mime_type": "image/png", "url": 123}]

    assert TranscriptMedia("t1").items(entries) == [
        {"kind": "image", "mime_type": "image/png", "error": "unsupported_type"}
    ]


def test_an_untagged_block_whose_url_is_not_a_string_is_not_saved():
    message = ToolMessage(content=[{"type": "image", "url": 123, "mime_type": "image/png"}], tool_call_id="c1")

    assert TranscriptMedia("t1").of_message(message) == [
        {"kind": "image", "mime_type": "image/png", "error": "not_saved"}
    ]


@pytest.mark.parametrize(
    "url", ['https://x.test/a.png" onmouseover="x', "https://x.test/a b.png", "https://x.test/<a>.png"]
)
def test_media_items_rejects_urls_that_are_not_plain(url):
    entry = {"ordinal": 0, "kind": "image", "mime_type": "image/png", "url": url}

    assert TranscriptMedia("t1").items([entry]) == [
        {"kind": "image", "mime_type": "image/png", "error": "unsupported_type"}
    ]


@pytest.mark.parametrize(
    ("mime", "expected"),
    [
        ("image/jpg", "image/jpeg"),
        ("image/pjpeg", "image/jpeg"),
        ("audio/x, text/html", None),
        ("video/mp4\r\nx-y: z", None),
        ("audio/" + "a" * 120, None),
    ],
)
def test_stored_content_type_normalises_and_validates(mime, expected):
    assert RunToolMediaStore.content_type(mime) == expected
