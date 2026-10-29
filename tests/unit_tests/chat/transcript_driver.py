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
