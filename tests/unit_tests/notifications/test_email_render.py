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


def test_a_nonsensical_overflow_count_is_not_rendered():
    # The count is read back from persisted context, not recomputed, so both bodies gate on the
    # sign: a truthiness check would print "… and -2 more" to a recipient.
    html, text = _render(_findings(n=1, overflow=-2))
    assert "more" not in html
    assert "more" not in text


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


def _notable(n=2, overflow=0):
    return {
        "status_label": "Needs attention",
        "status_tone": "warning",
        "notable_runs": [
            {"kind": "Failed", "label": f"acme/repo{i}", "ref": f"migration 004{i} errored"} for i in range(n)
        ],
        "notable_runs_overflow": overflow,
    }


def test_batch_notable_runs_are_listed_in_both_bodies():
    html, text = _render(_notable(overflow=3))
    for fragment in ("Needs a look", "acme/repo0", "migration 0040 errored", "acme/repo1"):
        assert fragment in html
        assert fragment in text
    assert "- [Failed] acme/repo0 (migration 0040 errored)" in text
    assert "… and 3 more" in html


def test_batch_rows_are_not_monospaced():
    # The ref slot holds prose here, not a file path; monospacing it reads as code.
    html, _text = _render(_notable(n=1))
    assert "ui-monospace" not in html


def test_findings_stay_monospaced():
    html, _text = _render(_findings(n=1))
    assert "ui-monospace" in html


def test_both_lists_render_together():
    ctx = _findings(n=1) | _notable(n=1)
    html, text = _render(ctx)
    for fragment in ("Findings", "Needs a look"):
        assert fragment in html
        assert fragment in text


def test_plain_text_does_not_html_escape_prose():
    """A schedule subject carries literal quotes and classifier prose carries apostrophes; the
    text/plain body must not show them as entities."""
    ctx = {"status_tone": "warning", "actionable": [{"kind": "bug", "label": "don't trust <input>", "ref": "app/x.py"}]}
    notif = SimpleNamespace(subject="'nightly' batch: 3/5 need a look", body='it\'s "broken"', context=ctx)
    text = render_to_string("notifications/emails/notification.txt", {"notification": notif, "link_absolute_url": ""})
    assert "&#x27;" not in text
    assert "&quot;" not in text
    assert "&lt;" not in text
    assert "'nightly' batch" in text
    assert "don't trust <input>" in text


def test_html_email_still_escapes_prose():
    """The text body turns autoescape off (text/plain has no HTML context); the HTML one must not."""
    ctx = {"status_tone": "warning", "actionable": [{"kind": "bug", "label": "<script>x</script>", "ref": "a"}]}
    notif = SimpleNamespace(subject="s", body="<b>b</b>", context=ctx)
    html = render_to_string("notifications/emails/notification.html", {"notification": notif, "link_absolute_url": ""})
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<b>b</b>" not in html
