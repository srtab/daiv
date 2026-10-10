"""Boot ``tool-renderers.js`` under node so a suite only writes what it does to the module.

``toolBodyHTML`` feeds straight into ``x-html`` (raw innerHTML), so the XSS guards on
its output cannot be asserted by a source-string grep. This driver loads the real
module in a ``vm`` context with just enough of the browser for the IIFE to assign
``window.toolBodyHTML`` — a passthrough ``DOMPurify`` proves the body's own escaping
and scheme checks make the output inert independent of sanitisation, while still
exercising the routing path.
"""

from __future__ import annotations

from typing import Any

from tests.unit_tests.jsdriver import run_node
from tests.unit_tests.test_template_comments import DAIV_DIR

TOOL_RENDERERS_JS = DAIV_DIR / "chat" / "static" / "chat" / "js" / "tool-renderers.js"

_HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const payload = JSON.parse(readFileSync(0, "utf8"));
let dompurifyCalled = false;
const sandbox = {
  console: { log: () => {}, warn: () => {}, error: () => {} },
  // node's global URL — summarizeUrl / externalLink parse schemes with new URL().
  URL,
  window: {
    // A passthrough DOMPurify: the body's own escaping/scheme checks are what make
    // the output inert; this only records that the boundary was routed through.
    DOMPurify: { sanitize: (html) => { dompurifyCalled = true; return html; } },
  },
  document: { createElement: () => ({ innerHTML: "", content: { querySelectorAll: () => [] } }) },
};
vm.createContext(sandbox);
vm.runInContext(readFileSync(payload.src, "utf8"), sandbox);
const html = sandbox.window.toolBodyHTML(
  payload.name, payload.argsStr, payload.result, payload.status ?? "done",
);
process.stdout.write(JSON.stringify({ html, dompurifyCalled }));
"""


def run_tool_renderers(name: str, args_str: str, result: str, *, status: str = "done") -> dict[str, Any]:
    """Render a tool segment through the real ``toolBodyHTML``; return ``{html, dompurifyCalled}``."""
    return run_node(
        _HARNESS, {"src": str(TOOL_RENDERERS_JS), "name": name, "argsStr": args_str, "result": result, "status": status}
    )
