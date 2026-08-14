from __future__ import annotations


def backfill(mcp_server_model) -> None:
    """Map the legacy ``enabled`` boolean onto ``status``: True → active, False → disabled.
    Behavior-preserving: previously-off servers stay off and are not silently made opt-in-able."""
    mcp_server_model.objects.filter(enabled=True).update(status="active")
    mcp_server_model.objects.filter(enabled=False).update(status="disabled")
