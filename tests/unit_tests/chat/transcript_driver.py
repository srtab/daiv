"""Boot the transcript modules (labels, tool rows, run blocks) under node, without the chat component."""

from __future__ import annotations

from typing import Any

from tests.unit_tests.chat.chat_stream_driver import (
    DIFF_STUB,
    TOOL_RENDERERS_JS,
    TRANSCRIPT_BLOCKS_JS,
    TRANSCRIPT_LABELS_JS,
)
from tests.unit_tests.jsdriver import run_node

_HARNESS = f"""
import {{ readFileSync }} from "node:fs";
import vm from "node:vm";

const payload = JSON.parse(readFileSync(0, "utf8"));
const script = payload.labels == null ? null : {{ textContent: JSON.stringify(payload.labels) }};
const escape = (s) => String(s).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
const markdown = (raw) => escape(raw).replace(/\\*\\*(.+?)\\*\\*/g, "<strong>$1</strong>");
const sandbox = {{
  document: {{ getElementById: (id) => (id === "chat-transcript-labels" ? script : null) }},
  Diff: {DIFF_STUB},
  vars: payload.vars,
}};
if (payload.markdown) sandbox.renderMarkdown = markdown;
sandbox.window = sandbox;
vm.createContext(sandbox);
for (const src of payload.sources) vm.runInContext(readFileSync(src, "utf8"), sandbox);
process.stdout.write(JSON.stringify(vm.runInContext(payload.expr, sandbox)));
"""


def run_transcript(
    expr: str, values: dict, *, blocks: bool = True, labels: dict | None = None, markdown: bool = False
) -> Any:
    """Evaluate ``expr`` once the modules have loaded, with ``values`` bound to ``vars``; return its JSON value.

    ``labels`` is the ``chat-transcript-labels`` payload the page would carry (e.g. ``{"show_raw": True}``).
    ``markdown`` installs a ``renderMarkdown`` stand-in: ``**x**`` becomes ``<strong>x</strong>``, the rest is escaped.
    """
    sources = [TRANSCRIPT_LABELS_JS, TOOL_RENDERERS_JS, *([TRANSCRIPT_BLOCKS_JS] if blocks else [])]
    payload = {"sources": [str(src) for src in sources], "expr": expr, "vars": values, "markdown": markdown}
    if labels is not None:
        payload["labels"] = labels
    return run_node(_HARNESS, payload)
