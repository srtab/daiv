import pytest
from langchain_core.tools import tool
from pydantic import BaseModel, ValidationError

from core.sentry import before_send

EVENT = {"event_id": "abc"}


class _Payload(BaseModel):
    count: int


@tool
def _count(count: int) -> int:
    """Return the count."""
    return count


@tool
def _count_from_bad_payload(count: int) -> int:
    """Build a payload from invalid data inside the tool body."""
    return _Payload.model_validate({"count": "many"}).count


def _hint(exc: BaseException) -> dict:
    return {"exc_info": (type(exc), exc, exc.__traceback__)}


async def test_drops_a_validation_error_on_the_tool_arguments():
    with pytest.raises(ValidationError) as info:
        await _count.ainvoke({"count": "many"})

    assert before_send(EVENT, _hint(info.value)) is None


async def test_keeps_a_validation_error_raised_inside_the_tool_body():
    with pytest.raises(ValidationError) as info:
        await _count_from_bad_payload.ainvoke({"count": 1})

    assert before_send(EVENT, _hint(info.value)) is EVENT


def test_keeps_a_validation_error_raised_outside_a_tool():
    with pytest.raises(ValidationError) as info:
        _Payload.model_validate({"count": "many"})

    assert before_send(EVENT, _hint(info.value)) is EVENT


def test_keeps_events_without_an_exception():
    assert before_send(EVENT, {}) is EVENT
