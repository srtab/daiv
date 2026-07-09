import uuid
from unittest.mock import patch

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.signals import render_batch_summary, resume_coordinator_on_batch_complete

pytestmark = pytest.mark.django_db(transaction=True)


def _session(thread_id, **kw):
    return Session.objects.create(
        thread_id=thread_id, origin=SessionOrigin.DELEGATED_JOB, repo_id=kw.pop("repo_id", "g/leg"), **kw
    )


def _run(session, **kw):
    kw.setdefault("trigger_type", SessionOrigin.DELEGATED_JOB)
    kw.setdefault("repo_id", session.repo_id)
    kw.setdefault("status", RunStatus.SUCCESSFUL)
    return Run.objects.create(session=session, **kw)


def test_render_batch_summary_includes_mr_and_truncates():
    batch = uuid.uuid4()
    s = _session("leg-1", repo_id="g/a", parent_thread_id="coord")
    long_summary = "x" * 600
    runs = [
        _run(
            s,
            repo_id="g/a",
            status=RunStatus.SUCCESSFUL,
            batch_id=batch,
            merge_request_web_url="https://gl/mr/1",
            result_summary=long_summary,
        ),
        _run(
            _session("leg-2", repo_id="g/b", parent_thread_id="coord"),
            repo_id="g/b",
            status=RunStatus.FAILED,
            batch_id=batch,
            error_message="boom",
        ),
    ]
    text = render_batch_summary(batch, runs)
    assert "1 succeeded, 1 failed" in text
    assert "https://gl/mr/1" in text
    assert "g/a (successful)" in text
    assert "g/b (failed)" in text
    # result_summary is truncated to 500 characters by the renderer
    assert ("x" * 500) in text
    assert ("x" * 501) not in text


def test_receiver_ignores_broadcast_batch_without_parent():
    """A batch whose leg session has no parent_thread_id is an ordinary broadcast — no resume."""
    batch = uuid.uuid4()
    s = _session("leg-x", parent_thread_id=None)
    run = _run(s, batch_id=batch, status=RunStatus.SUCCESSFUL)
    resume_coordinator_on_batch_complete(sender=Run, run=run)
    assert not Run.objects.filter(continuation_of_batch_id=batch).exists()


def test_receiver_creates_one_continuation_when_all_terminal():
    batch = uuid.uuid4()
    Session.objects.create(thread_id="coord", origin=SessionOrigin.MCP_JOB, repo_id="g/coord")
    leg = _session("leg-1", repo_id="g/a", parent_thread_id="coord")
    run = _run(leg, batch_id=batch, status=RunStatus.SUCCESSFUL, merge_request_web_url="https://gl/mr/9")

    with patch("sessions.signals._enqueue_queued_run", return_value=True) as m_enqueue:
        resume_coordinator_on_batch_complete(sender=Run, run=run)

    cont = Run.objects.get(continuation_of_batch_id=batch)
    assert cont.session_id == "coord"
    assert cont.repo_id == "g/coord"
    assert cont.trigger_type == SessionOrigin.DELEGATED_JOB
    assert "https://gl/mr/9" in cont.prompt
    m_enqueue.assert_called_once()


def test_receiver_noop_while_a_sibling_is_pending():
    batch = uuid.uuid4()
    Session.objects.create(thread_id="coord2", origin=SessionOrigin.MCP_JOB, repo_id="g/coord")
    a = _session("leg-a", repo_id="g/a", parent_thread_id="coord2")
    b = _session("leg-b", repo_id="g/b", parent_thread_id="coord2")
    _run(a, batch_id=batch, status=RunStatus.RUNNING)  # still running
    done = _run(b, batch_id=batch, status=RunStatus.SUCCESSFUL)
    resume_coordinator_on_batch_complete(sender=Run, run=done)
    assert not Run.objects.filter(continuation_of_batch_id=batch).exists()
