"""Run a JS module under node and read back what it did.

Two suites assert on behaviour a source-string grep cannot see — ``chat/test_resume_replay.py``
and ``core/test_nav_events_js.py``. They differ in the harness they run and the payload they
feed it, not in how node is invoked, so the invocation lives here.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # noqa: S404
from typing import Any

import pytest

NODE = shutil.which("node")

requires_node = pytest.mark.skipif(NODE is None, reason="node is required to drive JS modules under test")


def run_node(harness: str, payload: dict) -> Any:
    """Run ``harness`` as an ES module with ``payload`` on stdin, parsing its stdout as JSON."""
    proc = subprocess.run(  # noqa: S603
        [str(NODE), "--input-type=module", "-e", harness],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)
