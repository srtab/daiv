from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.messages import AnyMessage


@dataclass
class ReviewContext:
    discussion_id: str
    resolve_id: str
    notes: list[AnyMessage] = field(default_factory=list)
    diff: str
