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


def test_status_tone_failure_beats_is_successful():
    # A found-issues/failed run can finish successfully (is_successful True); the pill must follow the
    # envelope tone, not the run's own success, so it stays red rather than going green.
    html, _text = _render({"status_label": "Failed", "status_tone": "failure", "is_successful": True})
    assert RED_BG in html
    assert GREEN_BG not in html


def test_per_run_found_issues_renders_amber():
    html, _text = _render({"status_label": "Found issues", "status_tone": "warning", "is_successful": True})
    assert "Found issues" in html
    assert AMBER_BG in html
    assert GREEN_BG not in html


def test_no_status_label_drops_the_pill_row():
    html, text = _render({"duration_seconds": 12})
    assert "Status" not in html
    assert "Status:" not in text


def _findings(n=2, overflow=0):
    return {
        "status_label": "Found issues",
        "status_tone": "warning",
        "actionable": [
            {"kind": "bug", "label": f"off-by-one in paging {i}", "ref": f"app/views.py:{i}"} for i in range(n)
        ],
        "actionable_overflow": overflow,
    }


def test_findings_are_listed_in_both_email_bodies():
    html, text = _render(_findings())
    for fragment in ("Findings", "bug", "off-by-one in paging 0", "app/views.py:0", "off-by-one in paging 1"):
        assert fragment in html
        assert fragment in text
    assert "- [bug] off-by-one in paging 0 (app/views.py:0)" in text


def test_findings_overflow_is_spelled_out():
    html, text = _render(_findings(n=1, overflow=4))
    assert "… and 4 more" in html
    assert "… and 4 more" in text


def test_no_findings_drops_the_whole_block():
    html, text = _render({"status_label": "Needs attention", "status_tone": "warning", "actionable": []})
    assert "Findings" not in html
    assert "Findings" not in text
    assert "Status: Needs attention" in text  # the rows below the block are unaffected


def test_a_finding_without_kind_or_ref_still_lists_its_label():
    ctx = {"status_tone": "warning", "actionable": [{"kind": "", "label": "just a label", "ref": ""}]}
    html, text = _render(ctx)
    assert "just a label" in html
    assert "- just a label" in text
