"""Boot ``chat-stream.js`` under node, so a suite only writes what it does to the module.

Two suites drive the real module — ``test_resume_replay`` and ``test_progress_pill_reactivity``
— and they need the same thing to get to a component: a `vm` context carrying enough of the
browser for the file to evaluate, plus the `alpine:init` dispatch that registers `chat` in
``registry``. Only the stubs each one *adds* differ, so the boot lives here: a global the
module starts reaching for is then stubbed once, not once per suite.
"""

from __future__ import annotations

from typing import Any

from tests.unit_tests.jsdriver import run_node
from tests.unit_tests.test_template_comments import DAIV_DIR

CHAT_STREAM_JS = DAIV_DIR / "chat" / "static" / "chat" / "js" / "chat-stream.js"

_HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const payload = JSON.parse(readFileSync(0, "utf8"));
const registry = {};
// __PRELUDE__
const sandbox = {
  console: { log: () => {}, debug: () => {}, warn: () => {}, error: () => {} },
  crypto, setInterval, clearInterval,
  CSS: { supports: () => true },
  document: {
    getElementById: () => null,
    querySelector: () => null,
    addEventListener: (name, cb) => { if (name === "alpine:init") cb(); },
  },
  window: { Alpine: { data: (name, factory) => (registry[name] = factory) } },
  // __GLOBALS__
};
vm.createContext(sandbox);
vm.runInContext(readFileSync(payload.src, "utf8"), sandbox);
// __BODY__
"""


def run_chat_stream(body: str, payload: dict, *, prelude: str = "", extra_globals: str = "") -> Any:
    """Run ``body`` against a booted ``chat-stream.js``; return what it wrote to stdout.

    ``body`` reads its inputs off ``payload`` and the component factory off ``registry.chat``.
    ``prelude`` lands above the sandbox, for stubs its ``extra_globals`` entries name.
    """
    harness = _HARNESS.replace("// __PRELUDE__", prelude)
    harness = harness.replace("// __GLOBALS__", extra_globals).replace("// __BODY__", body)
    return run_node(harness, {"src": str(CHAT_STREAM_JS), **payload})
