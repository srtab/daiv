"""Runs built from session inbox events (research.md R18).

Each case gives the agent what ``arender_input`` gives a real inbox run, on a disk clone of srtab/daiv ``main``. Writes
are interrupted, so a case judges the agent's tool calls up to its first write, and its final message.
"""

import re
import uuid

import pytest
from langsmith import testing as t
from sessions.inbox.render import arender_input
from sessions.models import EventKind, EventState, InboxEvent, Run, RunStatus, Session, SessionOrigin

from .prompt_leak import assert_no_prompt_leak
from .utils import INBOX_MODELS, extract_tool_calls, final_text, measure, require_provider_for_model, run_agent_once

pytestmark = [
    pytest.mark.inbox,
    pytest.mark.langsmith(test_suite_name="DAIV: Inbox"),
    pytest.mark.parametrize("model_name", INBOX_MODELS),
    pytest.mark.django_db(transaction=True),
]

RUNNER_OUT_OF_DISK = (
    "The publish-image job died while the runner pulled the base image: "
    "`write /var/lib/docker/tmp: no space left on device`. Nothing in the diff touches the Dockerfile."
)
WHICH_COMMAND = "@daiv which management command releases the queued runs nobody started?"
ADD_FLAG = (
    "Add a `--dry-run` flag to the `release_orphan_queued_sessions` management command that prints what it "
    "would release without changing anything."
)

assert_no_prompt_leak([RUNNER_OUT_OF_DISK, WHICH_COMMAND, ADD_FLAG])

WRITE_INTERRUPTS = {"edit_file": True, "write_file": True, "bash": True, "task": True}
WRITES = {"edit_file", "write_file"}
GIT_SYNC = re.compile(r"\bgit\s+(pull|fetch|reset|rebase|merge|checkout)\b")
CI_FAILURE = {
    "kind": EventKind.CI_FAILED,
    "wake": True,
    "merge_request_iid": 41,
    "payload": {
        "pipeline_id": 830724,
        "sha": "e96f1f77aa",
        "status": "failed",
        "web_url": "https://gitlab.example/srtab/daiv/-/pipelines/830724",
        "failed_jobs": ["publish-image"],
    },
}


@pytest.fixture(autouse=True)
def _require_provider(model_name):
    require_provider_for_model(model_name)


async def _inbox_input(*events: dict, fast_forward: str | None = None):
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="srtab/daiv", ref="main"
    )
    run = await Run.objects.acreate(
        session=session,
        trigger_type=SessionOrigin.PIPELINE_WEBHOOK,
        repo_id="srtab/daiv",
        ref="main",
        status=RunStatus.READY,
    )
    for index, fields in enumerate(events):
        await InboxEvent.objects.acreate(
            session=session,
            run=run,
            state=EventState.TAKEN,
            source_key=f"case:{index}",
            ref="main" if fields.get("wake") else "",
            **fields,
        )
    return (await arender_input(run, fast_forward=fast_forward)).messages


async def _run(model_name: str, messages, request: pytest.FixtureRequest) -> tuple[list, str]:
    t.log_inputs({"model_name": model_name, "messages": [message.text for message in messages]})
    with measure(request) as metrics:
        result = await run_agent_once(model_name, messages, interrupt_on=WRITE_INTERRUPTS, ask_user_enabled=False)
    metrics.messages = result["messages"]
    t.log_outputs(result)
    return extract_tool_calls(result["messages"]), final_text(result["messages"])


def _remembered_comment(body: str) -> dict:
    return {
        "kind": EventKind.COMMENT,
        "wake": False,
        "merge_request_iid": 41,
        "author_username": "sfr",
        "payload": {"body": body, "web_url": "https://gitlab.example/srtab/daiv/-/merge_requests/41#note_1"},
    }


async def test_a_ci_failure_the_code_did_not_cause_is_explained_without_a_change(model_name, eval_request):
    messages = await _inbox_input(_remembered_comment(RUNNER_OUT_OF_DISK), CI_FAILURE)

    tool_calls, reply = await _run(model_name, messages, eval_request)

    assert not [call for call in tool_calls if call["name"] in WRITES], "edited the code for a runner failure"
    assert re.search(r"disk|space|runner|infrastructure", reply, re.IGNORECASE), reply


async def test_a_mention_and_a_ci_failure_are_both_answered(model_name, eval_request):
    mention = {
        "kind": EventKind.MENTION,
        "wake": True,
        "merge_request_iid": 41,
        "author_username": "sfr",
        "payload": {
            "note_id": 2,
            "discussion_id": "d2",
            "body": WHICH_COMMAND,
            "web_url": "https://gitlab.example/srtab/daiv/-/merge_requests/41#note_2",
        },
    }
    messages = await _inbox_input(_remembered_comment(RUNNER_OUT_OF_DISK), CI_FAILURE, mention)

    tool_calls, reply = await _run(model_name, messages, eval_request)

    assert not [call for call in tool_calls if call["name"] in WRITES], "edited the code for a question"
    assert "release_orphan_queued_sessions" in reply, reply
    assert re.search(r"disk|space|runner|publish-image|CI|pipeline", reply, re.IGNORECASE), reply


async def test_after_the_branch_moved_it_works_from_the_new_head(model_name, eval_request):
    pushed = {
        "kind": EventKind.BRANCH_PUSHED,
        "wake": False,
        "payload": {"before": "1a2b3c4d5e", "after": "9f8e7d6c5b", "commit_count": 2, "pusher_username": "sfr"},
    }
    chat = {"kind": EventKind.CHAT_MESSAGE, "wake": True, "payload": {"text": ADD_FLAG, "agui_message_id": "m-1"}}
    messages = await _inbox_input(pushed, chat, fast_forward="advanced")

    tool_calls, _reply = await _run(model_name, messages, eval_request)

    syncs = [call for call in tool_calls if call["name"] == "bash" and GIT_SYNC.search(str(call["args"]))]
    assert not syncs, f"re-synced a working copy that was already fast-forwarded: {syncs}"
