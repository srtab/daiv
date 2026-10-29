from __future__ import annotations

import json

from django.templatetags.static import static

import pytest
import sessions.transcript_labels as sessions_transcript_labels
from sessions.transcript_labels import transcript_icon_urls

from core.templatetags import icon_tags
from core.templatetags.icon_tags import icon
from tests.unit_tests.jsdriver import requires_node, run_node
from tests.unit_tests.test_template_comments import DAIV_DIR

pytestmark = requires_node

TRANSCRIPT_LABELS_JS = DAIV_DIR / "chat" / "static" / "chat" / "js" / "transcript-labels.js"

_HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const payload = JSON.parse(readFileSync(0, "utf8"));
const sandbox = {
  document: {
    getElementById: (id) =>
      id === "chat-transcript-labels" ? { textContent: payload.script } : null,
  },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(readFileSync(payload.src, "utf8"), sandbox);
const result = vm.runInContext(payload.expr, sandbox);
process.stdout.write(JSON.stringify(result));
"""


def _run(expr: str, overrides: dict | None = None):
    payload = {"labels": {}, "icon_base": static("core/img/icons/")}
    payload.update(overrides or {})
    return run_node(_HARNESS, {"src": str(TRANSCRIPT_LABELS_JS), "expr": expr, "script": json.dumps(payload)})


def test_t_fills_placeholders_and_keeps_unknown_ones():
    assert _run("TranscriptLabels.t('row_read', {name: 'queue.py'})") == "Read queue.py"
    assert _run("TranscriptLabels.t('row_mcp', {server: 'Sentry'})") == "Sentry · {tool}"


def test_tn_picks_singular_or_plural():
    assert _run("TranscriptLabels.tn('sum_read', 1)") == "read 1 file"
    assert _run("TranscriptLabels.tn('sum_read', 3)") == "read 3 files"


def test_server_overrides_win():
    out = _run("TranscriptLabels.t('row_read', {name: 'a'})", overrides={"labels": {"row_read": "Leu {name}"}})
    assert out == "Leu a"


def test_icon_html_matches_the_template_tag_and_rejects_bad_names(monkeypatch):
    assert _run("TranscriptLabels.iconHTML('pencil', 'h-3 w-3')") == icon("pencil", "h-3 w-3")
    assert _run("TranscriptLabels.iconHTML('pencil', 'a\"b')") == icon("pencil", 'a"b')
    assert _run("TranscriptLabels.iconHTML('../x')") == ""

    def hashed(path):
        return "/static/" + path.removesuffix(".svg") + ".0123abcd.svg"

    monkeypatch.setattr(icon_tags, "static", hashed)
    monkeypatch.setattr(sessions_transcript_labels, "static", hashed)
    mapped = {"icons": transcript_icon_urls()}
    assert _run("TranscriptLabels.iconHTML('pencil', 'h-3 w-3')", mapped) == icon("pencil", "h-3 w-3")
    assert "url('/static/core/img/icons/unmapped.svg')" in _run("TranscriptLabels.iconHTML('unmapped')", mapped)


@pytest.mark.parametrize(("sec", "expected"), [(4, "4s"), (192, "3m 12s"), (3720, "1h 2m")])
def test_duration_tiers(sec, expected):
    assert _run(f"TranscriptLabels.duration({sec})") == expected
