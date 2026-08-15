from __future__ import annotations


def backfill(mcp_server_model) -> None:
    """Map the legacy ``enabled`` boolean onto ``status``: True → active, False → disabled.
    Behavior-preserving: previously-off servers stay off and are not silently made opt-in-able."""
    mcp_server_model.objects.filter(enabled=True).update(status="active")
    mcp_server_model.objects.filter(enabled=False).update(status="disabled")


def unbackfill(mcp_server_model) -> None:
    """Reverse of :func:`backfill`. Reversing 0010 re-adds ``enabled`` at its field default
    (``True``), so leaving this a no-op turns every deliberately-disabled server — the ``sentry``
    builtin among them — back on during a rollback.

    ``on-demand`` has no pre-``status`` equivalent and maps to ``True``: it was reachable by a run,
    which is what ``enabled`` meant. That lossiness is one-way, and re-running 0009 forward would
    then read those rows as ``active``."""
    mcp_server_model.objects.filter(status="disabled").update(enabled=False)
    mcp_server_model.objects.exclude(status="disabled").update(enabled=True)
