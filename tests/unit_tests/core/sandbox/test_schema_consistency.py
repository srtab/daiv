"""Schema-drift CI test.

Asserts daiv's wire schemas match a JSON-Schema dump from daiv-sandbox so a
sandbox-side change can't silently break the daiv client.

Refresh ``daiv/core/sandbox/schemas.dump.json`` after any change to
``daiv-sandbox/daiv_sandbox/schemas.py`` (see AGENTS.md).
"""

import json
from pathlib import Path

import pytest


def _normalize(schema):
    """Drop title/description noise that's irrelevant to wire compatibility."""
    if not isinstance(schema, dict):
        return schema
    out = {k: v for k, v in schema.items() if k not in ("title", "description")}
    return {k: _normalize(v) if isinstance(v, dict) else v for k, v in out.items()}


# Only the eager-sync shared types are checked. The other types
# (Run{Request,Response,Result}, StartSessionRequest/Response) intentionally
# differ between the two repos: daiv's RunCommands* keeps a stable client API,
# StartSessionRequest accepts a Dockerfile alternative, and StartSessionResponse
# isn't modelled on the daiv side.
_SHARED_TYPES = [
    "ApplyMutationsRequest",
    "ApplyMutationsResponse",
    "MutationResult",
    "PutMutation",
    "SeedSessionRequest",
]

# Types that exist on both sides but are deliberately allowed to diverge.
_INTENTIONALLY_DIVERGENT = {"RunCommandsRequest", "RunCommandsResponse", "RunCommandResult", "StartSessionRequest"}


@pytest.mark.parametrize("type_name", _SHARED_TYPES)
def test_daiv_schema_matches_sandbox_dump(type_name):
    """Daiv-side schemas must match the checked-in dump from daiv-sandbox."""
    dump_path = Path(__file__).parents[4] / "daiv" / "core" / "sandbox" / "schemas.dump.json"
    sandbox_dump = json.loads(dump_path.read_text())

    daiv_module = __import__("core.sandbox.schemas", fromlist=["*"])
    daiv_cls = getattr(daiv_module, type_name)

    sandbox_schema = _normalize(sandbox_dump[type_name])
    daiv_schema = _normalize(daiv_cls.model_json_schema())

    assert sandbox_schema == daiv_schema, (
        f"Schema for {type_name} drifted between daiv and daiv-sandbox.\n"
        f"Sandbox: {json.dumps(sandbox_schema, indent=2, sort_keys=True)}\n"
        f"Daiv:    {json.dumps(daiv_schema, indent=2, sort_keys=True)}"
    )


def test_shared_types_list_covers_dump():
    """Catches the inverse drift: a new type added to the sandbox dump that the daiv side never registered.

    Without this, additions on the sandbox side stay invisible because the per-type
    parametrization only iterates the hardcoded ``_SHARED_TYPES`` list.
    """
    dump_path = Path(__file__).parents[4] / "daiv" / "core" / "sandbox" / "schemas.dump.json"
    sandbox_dump = json.loads(dump_path.read_text())
    daiv_module = __import__("core.sandbox.schemas", fromlist=["*"])

    dumped_types_present_on_daiv = {name for name in sandbox_dump if hasattr(daiv_module, name)}
    untracked = dumped_types_present_on_daiv - set(_SHARED_TYPES) - _INTENTIONALLY_DIVERGENT
    assert not untracked, (
        f"Types {sorted(untracked)} exist in both repos' schemas but are neither in _SHARED_TYPES "
        "nor in _INTENTIONALLY_DIVERGENT. Categorize them in the test."
    )
