"""Render the shared notification email templates for the batch and per-run status pills.

The batch payload dropped the raw-status keys the pill used to read, so batch emails lost their
status row entirely; the pill is now driven by an aggregate ``status_tone``. These render the real
templates the way ``EmailChannel.send`` does and assert the pill colour and label per tone."""

from types import SimpleNamespace

from django.template.loader import render_to_string

# Pill palette from notifications/emails/notification.html (background hex per tone).
GREEN_BG = "#ecfdf5"  # success (per-run only)
AMBER_BG = "#fffbeb"  # warning (partial batch)
RED_BG = "#fef2f2"  # failure


def _render(context: dict) -> tuple[str, str]:
    notif = SimpleNamespace(subject="Agent run batch: 3/5 need a look", body="the breakdown", context=context)
    tpl_ctx = {"notification": notif, "link_absolute_url": ""}
    html = render_to_string("notifications/emails/notification.html", tpl_ctx)
    text = render_to_string("notifications/emails/notification.txt", tpl_ctx)
    return html, text


def test_batch_warning_renders_amber_needs_attention_pill():
    html, text = _render({"status_label": "Needs attention", "status_tone": "warning", "duration_seconds": 371})
    assert "Needs attention" in html
    assert AMBER_BG in html
    assert RED_BG not in html
    assert GREEN_BG not in html
    assert "Status: Needs attention" in text  # the .txt row the batch used to lose


def test_batch_failure_renders_red_pill():
    html, _text = _render({"status_label": "Needs attention", "status_tone": "failure"})
    assert "Needs attention" in html
    assert RED_BG in html
    assert AMBER_BG not in html


def test_per_run_success_pill_stays_green():
    html, _text = _render({"status": "SUCCESSFUL", "status_label": "Done", "is_successful": True})
    assert "Done" in html
    assert GREEN_BG in html
    assert AMBER_BG not in html


def test_per_run_failure_pill_stays_red():
    html, _text = _render({"status": "FAILED", "status_label": "Failed", "is_successful": False})
    assert "Failed" in html
    assert RED_BG in html
    assert AMBER_BG not in html


def test_no_status_label_drops_the_pill_row():
    html, text = _render({"duration_seconds": 12})
    assert "Status" not in html
    assert "Status:" not in text
