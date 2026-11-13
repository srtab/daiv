import pytest
from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.runnables import RunnableLambda

from automation.agent.events import (
    CONTEXT_USAGE_EVENT,
    PUBLISH_RESULT_EVENT,
    context_usage_payload,
    publish_result_payload,
    published_outcome_payload,
    record_publish_result,
    repo_publish_payload,
)
from automation.agent.git_state import RepoPublish
from automation.agent.publishers import PublishOutcome
from automation.agent.usage_tracking import ResolvedWindow
from codebase.base import MergeRequestDiffStats
from tests.unit_tests.automation.agent.conftest import make_merge_request

USAGE = {"input_tokens": 100, "output_tokens": 20, "input_token_details": {"cache_read": 80}}


def _payload(**overrides):
    kwargs = {"model": "claude-sonnet-4-6", "usage": USAGE, "window": ResolvedWindow(1_000_000, "profile")}
    kwargs.update(overrides)
    return context_usage_payload(**kwargs)


def test_the_event_name_is_the_wire_literal():
    assert CONTEXT_USAGE_EVENT == "daiv_context_usage"


def test_the_builder_extracts_the_tokens_and_derives_the_total():
    payload = _payload()

    assert payload["input_tokens"] == 100
    assert payload["output_tokens"] == 20
    assert payload["cached_tokens"] == 80
    assert payload["used_tokens"] == 120


def test_a_sparse_usage_mapping_defaults_every_count_to_zero():
    payload = _payload(usage={})

    assert payload["input_tokens"] == 0
    assert payload["output_tokens"] == 0
    assert payload["cached_tokens"] == 0
    assert payload["used_tokens"] == 0


@pytest.mark.parametrize("tier", ["priority", "flex"])
def test_a_service_tier_cache_read_counts_as_cached(tier):
    payload = _payload(usage={"input_tokens": 100, "input_token_details": {f"{tier}_cache_read": 80}})

    assert payload["cached_tokens"] == 80


def test_a_resolved_window_ships_its_tokens():
    assert _payload(window=ResolvedWindow(200_000, "genai_prices"))["window_tokens"] == 200_000


def test_no_window_ships_null():
    assert _payload(window=None)["window_tokens"] is None


def test_the_key_set_is_the_wire_contract():
    assert set(_payload()) == {
        "model",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "used_tokens",
        "window_tokens",
    }


def test_the_publish_result_event_name_is_the_wire_literal():
    assert PUBLISH_RESULT_EVENT == "daiv_publish_result"


def test_the_publish_result_payload_carries_the_merge_request_and_stats():
    payload = publish_result_payload(
        status="opened",
        merge_request=make_merge_request(),
        diff_stats=MergeRequestDiffStats(files_changed=3, lines_added=10, lines_removed=2),
        pushed_sha="e96f1f77",
    )

    assert payload == {
        "status": "opened",
        "pushed_sha": "e96f1f77",
        "repository": "",
        "sibling": False,
        "merge_request_title": "Test MR",
        "merge_request_iid": 42,
        "merge_request_web_url": "https://example.com/owner/repo/-/merge_requests/42",
        "files_changed": 3,
        "lines_added": 10,
        "lines_removed": 2,
        "error_kind": None,
    }


def test_the_publish_result_payload_defaults_absent_values_to_none():
    assert publish_result_payload(status="failed", error_kind="push_rejected") == {
        "status": "failed",
        "pushed_sha": None,
        "repository": "",
        "sibling": False,
        "merge_request_title": None,
        "merge_request_iid": None,
        "merge_request_web_url": None,
        "files_changed": None,
        "lines_added": None,
        "lines_removed": None,
        "error_kind": "push_rejected",
    }


@pytest.mark.parametrize("merge_request", [None, "opened"])
def test_a_published_outcome_carries_the_sha_it_pushed(merge_request):
    outcome = PublishOutcome(
        merge_request=make_merge_request() if merge_request else None,
        published=True,
        created_merge_request=bool(merge_request),
        pushed_sha="e96f1f77",
    )

    assert published_outcome_payload(outcome)["pushed_sha"] == "e96f1f77"


async def _dispatch(*events: tuple[str, dict]) -> None:
    async def _emit(_input):
        for name, data in events:
            await adispatch_custom_event(name, data)

    await RunnableLambda(_emit).ainvoke(None)


async def test_the_recorder_keeps_the_last_publish_result_dispatched_inside_its_block():
    pushed, opened = publish_result_payload(status="pushed"), publish_result_payload(status="opened")

    with record_publish_result() as recorder:
        await _dispatch((PUBLISH_RESULT_EVENT, pushed), (PUBLISH_RESULT_EVENT, opened), (CONTEXT_USAGE_EVENT, {}))
    await _dispatch((PUBLISH_RESULT_EVENT, publish_result_payload(status="failed", error_kind="unknown")))

    assert recorder.payload == opened


async def test_the_recorder_holds_nothing_when_no_publish_result_was_dispatched():
    with record_publish_result() as recorder:
        await _dispatch((CONTEXT_USAGE_EVENT, {}))

    assert recorder.payload is None


MR_101 = make_merge_request(merge_request_id=101, title="Add the thing")


@pytest.mark.parametrize(
    ("publish", "expected"),
    [
        (
            RepoPublish("srtab/daiv", MR_101, True, "b", created_merge_request=True),
            {"status": "opened", "merge_request_iid": 101},
        ),
        (RepoPublish("srtab/daiv", MR_101, True, "b"), {"status": "pushed", "merge_request_iid": 101}),
        (RepoPublish("srtab/daiv", None, True, "b"), {"status": "pushed", "error_kind": "mr_pending"}),
        (
            RepoPublish("srtab/daiv", None, False, "b", error="x", error_kind="push_rejected"),
            {"status": "failed", "error_kind": "push_rejected"},
        ),
        (
            RepoPublish("srtab/daiv", None, False, "b", error="x", error_kind="conflicts"),
            {"status": "not_published", "error_kind": "conflicts"},
        ),
        (RepoPublish("srtab/daiv", None, False, "b", error="x"), {"status": "failed", "error_kind": "unknown"}),
    ],
)
def test_repo_publish_payload(publish, expected):
    payload = repo_publish_payload(publish)

    assert payload["repository"] == "srtab/daiv"
    assert payload["sibling"] is True
    assert payload.items() >= expected.items()


def test_repo_publish_payload_is_none_when_nothing_was_published():
    assert repo_publish_payload(RepoPublish("srtab/daiv", MR_101, False, "b")) is None


def test_the_publish_result_payload_carries_the_merge_request_title():
    assert publish_result_payload(status="opened", merge_request=MR_101)["merge_request_title"] == MR_101.title


async def test_the_recorder_keeps_the_main_repository_payload_over_a_later_sibling():
    main = publish_result_payload(status="opened", merge_request=MR_101)
    sibling = repo_publish_payload(RepoPublish("srtab/other", None, True, "b"))

    with record_publish_result() as recorder:
        await _dispatch((PUBLISH_RESULT_EVENT, main), (PUBLISH_RESULT_EVENT, sibling))

    assert recorder.payload == main
    assert recorder.siblings == {"srtab/other": sibling}


async def test_the_recorder_keeps_the_last_result_of_each_extra_repository():
    first, last = (repo_publish_payload(RepoPublish("srtab/other", None, True, "b", pushed_sha=sha)) for sha in "12")
    other = repo_publish_payload(RepoPublish("srtab/third", None, True, "b"))

    with record_publish_result() as recorder:
        await _dispatch((PUBLISH_RESULT_EVENT, first), (PUBLISH_RESULT_EVENT, other), (PUBLISH_RESULT_EVENT, last))

    assert recorder.payload is None
    assert list(recorder.siblings.values()) == [last, other]
