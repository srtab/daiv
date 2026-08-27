"""The raw OpenAI/OpenRouter streaming wire shape, for tests that drive a real chat model.

One home for the chunk envelope and the client stand-ins, so the two suites that exercise the
same upstream merge — ``test_chat_models`` (the source-level dedupe) and
``test_usage_tracking`` (the read-side repair, over a *stock* ``ChatOpenAI``) — cannot drift
from each other on the shape they feed it.
"""

from __future__ import annotations

import operator
from contextlib import asynccontextmanager, contextmanager
from functools import reduce
from typing import TYPE_CHECKING
from unittest import mock

if TYPE_CHECKING:
    from langchain_core.messages import AIMessageChunk
    from langchain_openai import ChatOpenAI

MODEL = "z-ai/glm-5.2"


def chunk(delta: dict, *, model: str = "anthropic/claude-haiku-4.5", finish_reason: str | None = None) -> dict:
    """A raw Chat-Completions stream chunk dict, as the OpenAI SDK hands it to
    ``_convert_chunk_to_generation_chunk`` (model_dump of a ChatCompletionChunk).

    ``system_fingerprint``/``service_tier`` ride along unconditionally because upstream only
    reads them on a chunk that also carries a ``finish_reason``.

    No ``usage``, deliberately: adding one puts ``token_usage`` in the accumulated
    ``response_metadata``, where it differs between a 1- and a 2-finish stream and so lands in
    ``test_the_pinned_key_set_is_what_upstream_stamps``' derived set.
    """
    return {
        "id": "c",
        "model": model,
        "object": "chat.completion.chunk",
        "system_fingerprint": "fp_abc",
        "service_tier": "default",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def finishing_chunks(model: str, finish_chunks: int) -> list[dict]:
    """One content delta, then ``finish_chunks`` chunks each carrying a ``finish_reason``.

    More than one is what OpenRouter does for some upstream providers, and what makes
    ``merge_dicts`` concatenate every string stamped inside that branch.
    """
    return [chunk({"role": "assistant", "content": "hi"}, model=model)] + [
        chunk({}, model=model, finish_reason="stop") for _ in range(finish_chunks)
    ]


@contextmanager
def fake_stream(chunks: list[dict]):
    """Stands in for the ``openai`` streaming response the sync client returns."""
    yield iter(chunks)


@asynccontextmanager
async def fake_astream(chunks: list[dict]):
    """The async twin: upstream enters it with ``async with`` and iterates with ``async for``."""

    async def chunk_iter():
        for raw in chunks:
            yield raw

    yield chunk_iter()


def accumulate(chunks: list[AIMessageChunk]) -> AIMessageChunk:
    """Merge streamed chunks the way every consumer does — this is where the merge bites."""
    return reduce(operator.add, chunks)


def stream_message(llm: ChatOpenAI, *, finish_chunks: int, model: str = MODEL) -> AIMessageChunk:
    """Drive ``llm`` over a stream ending in ``finish_chunks`` finish-carrying chunks.

    End to end rather than a hand-written ``response_metadata``, because the doubling comes
    from upstream's own chunk merge: a fixture asserting the doubled literal would keep passing
    after upstream stopped producing it.
    """
    # ``client`` is a pydantic field: assign to the instance, since
    # ``mock.patch.object(type(llm), "client")`` raises AttributeError.
    llm.client = mock.Mock(create=lambda **_: fake_stream(finishing_chunks(model, finish_chunks)))
    return accumulate(list(llm.stream("go")))


async def astream_message(llm: ChatOpenAI, *, finish_chunks: int, model: str = MODEL) -> AIMessageChunk:
    """The async twin of :func:`stream_message`."""
    # A factory, not one entered context manager: survives a transport that retries its create.
    llm.async_client = mock.Mock(
        create=mock.AsyncMock(side_effect=lambda **_: fake_astream(finishing_chunks(model, finish_chunks)))
    )
    return accumulate([part async for part in llm.astream("go")])
