from typing import TYPE_CHECKING

from core.utils import generate_uuid

if TYPE_CHECKING:
    from codebase.base import Scope


def compute_thread_id(*, repo_slug: str, scope: Scope, entity_iid: int | str) -> str:
    """Deterministic Session primary key, which is also the LangGraph checkpoint key, for an issue or
    merge-request conversation.

    Webhook callbacks mint this and hand it to the addressor task, so every event on the same
    issue or merge request resumes the same Session. Existing Sessions are keyed by these values,
    so the derivation must never change.
    """
    if not repo_slug or scope is None or entity_iid is None or entity_iid == "":
        raise ValueError(
            f"compute_thread_id requires non-empty values; "
            f"got repo_slug={repo_slug!r}, scope={scope!r}, entity_iid={entity_iid!r}"
        )
    return generate_uuid(f"{repo_slug}:{scope}/{entity_iid}")
