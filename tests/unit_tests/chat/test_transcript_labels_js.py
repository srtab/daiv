from __future__ import annotations

import json

import pytest
from sessions.transcript_labels import transcript_icons

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
    payload = {"labels": {}}
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


def test_icon_html_is_the_server_rendered_icon_and_empty_for_an_unshipped_name():
    shipped = {"icons": transcript_icons()}
    assert _run("TranscriptLabels.iconHTML('pencil')", shipped) == transcript_icons()["pencil"]
    assert _run("TranscriptLabels.iconHTML('unmapped')", shipped) == ""


@pytest.mark.parametrize(("sec", "expected"), [(4, "4s"), (192, "3m 12s"), (3720, "1h 2m")])
def test_duration_tiers(sec, expected):
    assert _run(f"TranscriptLabels.duration({sec})") == expected
