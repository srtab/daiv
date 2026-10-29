"""Boot the transcript modules (labels, tool rows, run blocks) under node, without the chat component."""

from __future__ import annotations

from typing import Any

from tests.unit_tests.chat.chat_stream_driver import CHAT_STREAM_JS
from tests.unit_tests.jsdriver import run_node

JS_DIR = CHAT_STREAM_JS.parent
TRANSCRIPT_LABELS_JS = JS_DIR / "transcript-labels.js"
TOOL_RENDERERS_JS = JS_DIR / "tool-renderers.js"
TRANSCRIPT_BLOCKS_JS = JS_DIR / "transcript-blocks.js"

# jsdiff loads from a CDN on the page; this stand-in diffs as a whole replacement.
DIFF_STUB = """{
  diffLines: (a, b) => {
    const count = (s) => (s ? s.split("\\n").length - (s.endsWith("\\n") ? 1 : 0) : 0);
    return [{ removed: true, count: count(a) }, { added: true, count: count(b) }];
  },
}"""

_HARNESS = f"""
import {{ readFileSync }} from "node:fs";
import vm from "node:vm";

const payload = JSON.parse(readFileSync(0, "utf8"));
const sandbox = {{ document: {{ getElementById: () => null }}, Diff: {DIFF_STUB}, vars: payload.vars }};
sandbox.window = sandbox;
vm.createContext(sandbox);
for (const src of payload.sources) vm.runInContext(readFileSync(src, "utf8"), sandbox);
process.stdout.write(JSON.stringify(vm.runInContext(payload.expr, sandbox)));
"""


def run_transcript(expr: str, values: dict, *, blocks: bool = True) -> Any:
    """Evaluate ``expr`` once the modules have loaded, with ``values`` bound to ``vars``; return its JSON value."""
    sources = [TRANSCRIPT_LABELS_JS, TOOL_RENDERERS_JS, *([TRANSCRIPT_BLOCKS_JS] if blocks else [])]
    return run_node(_HARNESS, {"sources": [str(src) for src in sources], "expr": expr, "vars": values})
