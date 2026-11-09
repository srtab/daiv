"""The coordinator workflow of ``delegate_jobs``: fanning legs out, ending the turn, and the run a batch resumes.

Each case runs the agent on a disk clone of srtab/daiv ``main`` with a delegator that records what it is asked to start
and puts those legs in the database instead of starting them; its status and cancel are the real ones, over the legs in
the database. A resumed
run gets what a real ``delegation_finished`` event renders: ``render_batch_summary`` over those legs, after the turn
that delegated them.
"""

import json
import re
import uuid
from dataclasses import asdict, dataclass

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from django_tasks_db.models import DBTaskResult, get_date_max
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langsmith import testing as t
from sessions.delegation import (
    _UNTRACKED_LEG,
    LEG_REPLY_MAX_CHARS,
    SessionJobDelegator,
    _on_own_branch,
    _session_url,
    check_delegation,
    render_batch_summary,
)
from sessions.inbox.kinds import SourceKey
from sessions.inbox.render import RunInput
from sessions.models import EventKind, EventState, InboxEvent, Run, RunStatus, Session, SessionOrigin
from sessions.queue import LINK_FAILED_PREFIX

from automation.agent.delegation import (
    DELEGATE_JOBS_TOOL_NAME,
    Checkout,
    DelegatedTarget,
    DelegationResult,
    FailedLeg,
    StartedLeg,
)
from automation.agent.middlewares.delegate_jobs import NO_CHANGE_CONVENTION

from .prompt_leak import assert_no_prompt_leak
from .utils import (
    DELEGATION_MODELS,
    WRITE_INTERRUPTS,
    extract_tool_calls,
    final_text,
    measure,
    require_provider_for_model,
    run_agent_once,
)

pytestmark = [
    pytest.mark.delegation,
    pytest.mark.langsmith(test_suite_name="DAIV: Delegation"),
    pytest.mark.parametrize("model_name", DELEGATION_MODELS),
    pytest.mark.django_db(transaction=True),
]

BILLING = "acme/billing-api"
NOTIFIER = "acme/notifier"

RENAME_HEADER = (
    f"Both {BILLING} and {NOTIFIER} still read the `X-Request-Id` header, which our gateway now sends as "
    "`X-Correlation-Id`. Update each of them to read the new header, falling back to the old one, with a merge "
    "request per repository."
)
BUMP_REQUESTS = (
    f"Start a job on {NOTIFIER} that bumps its `requests` dependency to 2.33 on the default branch, and tell me once "
    "it has started."
)
PIN_HTTPX = f"Pin `httpx` below 0.30 in {BILLING} and in {NOTIFIER}, with a merge request per repository."
RENAME_SETTING = (
    f"Rename the `LEGACY_TOKEN` setting to `SERVICE_TOKEN` in {BILLING} and in {NOTIFIER}, with a merge request per "
    "repository."
)
FIND_500 = (
    f"Some billing requests still fail with HTTP 500 since last week's database migration. Have {BILLING} checked "
    "endpoint by endpoint, and tell me which endpoint still fails and why."
)
STILL_FAILING = (
    "Still failing: `GET /v2/invoices/{id}/pdf` answers 500, because the migration dropped the "
    "`invoices.pdf_blob_key` column that the PDF renderer still reads."
)
CHECKED_ENDPOINTS = "\n".join(
    f"- `GET /v2/{resource}{suffix}` answers 200 against the migrated schema."
    for resource in (
        "customers",
        "invoices",
        "payments",
        "refunds",
        "subscriptions",
        "plans",
        "coupons",
        "tax-rates",
        "credit-notes",
        "webhooks",
    )
    for suffix in ("", "/{id}", "/{id}/events", "/{id}/history")
)
ENDPOINT_REPORT = (
    "I replayed every billing endpoint against a copy of the migrated database.\n\n"
    f"{CHECKED_ENDPOINTS}\n\n{STILL_FAILING}"
)

assert_no_prompt_leak([RENAME_HEADER, BUMP_REQUESTS, PIN_HTTPX, RENAME_SETTING, FIND_500, STILL_FAILING])

LOST_LEG_WORDING = re.compile(
    r"\blost\b|untracked|(?:may|might|could) still (?:open|run|publish)"
    r"|(?:can|could)(?:not|n['’]t| not) (?:track|follow)",
    re.IGNORECASE,
)


@pytest.fixture(autouse=True)
def _require_provider(model_name):
    require_provider_for_model(model_name)


class RecordingDelegator(SessionJobDelegator):
    """Starts nothing: records every ``delegate_jobs`` call it accepts and puts each leg in the database as queued,
    except those on ``lost`` repositories, which fail the way a leg DAIV started but lost track of does."""

    def __init__(self, coordinator: Session, *, lost: frozenset[str] = frozenset()):
        super().__init__(coordinator.thread_id)
        self.coordinator = coordinator
        self.lost = lost
        self.calls: list[list[DelegatedTarget]] = []

    async def adelegate(self, *, checkout: Checkout, goal: str, targets: list[DelegatedTarget]) -> DelegationResult:
        check_delegation(goal, targets)
        self.calls.append(list(targets))
        batch_id = uuid.uuid4()
        started: list[StartedLeg] = []
        failed: list[FailedLeg] = []
        for target in targets:
            lost = target.repo_id in self.lost
            session = await Session.objects.acreate(
                thread_id=str(uuid.uuid4()),
                origin=SessionOrigin.DELEGATED_JOB,
                repo_id=target.repo_id,
                ref=target.ref,
                parent=self.coordinator,
            )
            await Run.objects.acreate(
                session=session,
                trigger_type=SessionOrigin.DELEGATED_JOB,
                batch_id=batch_id,
                repo_id=target.repo_id,
                ref=target.ref,
                prompt=target.prompt,
                status=RunStatus.FAILED if lost else RunStatus.QUEUED,
                error_message=LINK_FAILED_PREFIX if lost else "",
            )
            if lost:
                failed.append(FailedLeg(target.repo_id, target.ref, _UNTRACKED_LEG, still_runs=True))
                continue
            started.append(
                StartedLeg(
                    target.repo_id,
                    target.ref,
                    session.thread_id,
                    await sync_to_async(_session_url)(session.thread_id),
                    starts_after_this_turn=_on_own_branch(checkout, target),
                )
            )
        return DelegationResult(batch_id=str(batch_id), started=started, failed=failed)

    def targets(self) -> list[DelegatedTarget]:
        return [target for call in self.calls for target in call]


@dataclass(frozen=True)
class FinishedLeg:
    repo_id: str
    prompt: str
    reply: str
    branch: str = ""
    merge_request_url: str = ""


def _finished_batch(coordinator: Session, batch_id: uuid.UUID, legs: tuple[FinishedLeg, ...]) -> list[Run]:
    """SYNC ONLY: one succeeded ``DELEGATED_JOB`` run per leg, each in a session of its own under ``coordinator``."""
    runs = []
    for leg in legs:
        session = Session.objects.create(
            thread_id=str(uuid.uuid4()),
            origin=SessionOrigin.DELEGATED_JOB,
            repo_id=leg.repo_id,
            ref=leg.branch,
            parent=coordinator,
        )
        task_result = DBTaskResult.objects.create(
            id=uuid.uuid4(),
            status="SUCCESSFUL",
            task_path="sessions.executor.tasks.run_job_task",
            args_kwargs={"args": [], "kwargs": {}},
            queue_name="default",
            backend_name="default",
            run_after=get_date_max(),
            return_value={"response": leg.reply, "merge_request_web_url": leg.merge_request_url or None},
        )
        runs.append(
            Run.objects.create(
                session=session,
                trigger_type=SessionOrigin.DELEGATED_JOB,
                batch_id=batch_id,
                repo_id=leg.repo_id,
                ref="",
                prompt=leg.prompt,
                status=RunStatus.SUCCESSFUL,
                task_result=task_result,
                # Stored cut, as Run.sync_from_task_result stores it.
                result_summary=leg.reply[:2000],
                merge_request_web_url=leg.merge_request_url,
                finished_at=timezone.now(),
            )
        )
    return runs


def _delegating_turn(request: str, batch_id: uuid.UUID, legs: list[Run]) -> list:
    """The coordinator's earlier turn: the request, its ``delegate_jobs`` call with every leg started, and its plan."""
    call_id = "call_delegate"
    result = DelegationResult(
        batch_id=str(batch_id),
        started=[StartedLeg(leg.repo_id, leg.ref, leg.session_id, _session_url(leg.session_id)) for leg in legs],
    )
    return [
        HumanMessage(request),
        AIMessage(
            "",
            tool_calls=[
                {
                    "name": DELEGATE_JOBS_TOOL_NAME,
                    "args": {
                        "goal": request.split(". ")[0],
                        "targets": [{"repo_id": leg.repo_id, "prompt": leg.prompt} for leg in legs],
                    },
                    "id": call_id,
                }
            ],
        ),
        ToolMessage(json.dumps(asdict(result)), tool_call_id=call_id, name=DELEGATE_JOBS_TOOL_NAME),
        AIMessage("I delegated one job per repository; I will report back once they have all finished."),
    ]


async def _acoordinator() -> Session:
    return await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="srtab/daiv", ref="main"
    )


async def _resumed(request: str, *legs: FinishedLeg) -> tuple[RecordingDelegator, list]:
    """The delegator and input of the run a finished batch resumes, as the inbox renders it for the coordinator."""
    coordinator = await _acoordinator()
    batch_id = uuid.uuid4()
    runs = await sync_to_async(_finished_batch)(coordinator, batch_id, legs)
    summary = await sync_to_async(render_batch_summary)(batch_id, runs)
    resume = await Run.objects.acreate(
        session=coordinator,
        trigger_type=SessionOrigin.DELEGATION_RESUME,
        repo_id="srtab/daiv",
        ref="main",
        status=RunStatus.READY,
    )
    await InboxEvent.objects.acreate(
        session=coordinator,
        run=resume,
        state=EventState.TAKEN,
        kind=EventKind.DELEGATION_FINISHED,
        wake=True,
        source_key=SourceKey.batch(batch_id),
        ref="main",
        payload={"batch_id": str(batch_id), "summary": summary, "succeeded": len(runs), "total": len(runs)},
    )
    rendered = (await RunInput.aof_run(resume)).render(resume).messages
    return RecordingDelegator(coordinator), [*_delegating_turn(request, batch_id, runs), *rendered]


async def _run(model_name: str, messages, delegator: RecordingDelegator, request: pytest.FixtureRequest):
    """The tool calls the agent made in this run, without those of the input's seeded turns, and its final reply."""
    t.log_inputs({"model_name": model_name, "messages": [message.text for message in messages]})
    with measure(request) as metrics:
        result = await run_agent_once(
            model_name, messages, interrupt_on=WRITE_INTERRUPTS, ask_user_enabled=False, delegator=delegator
        )
    metrics.messages = result["messages"]
    t.log_outputs(result)
    seeded = {call["id"] for call in extract_tool_calls(messages)}
    tool_calls = [call for call in extract_tool_calls(result["messages"]) if call["id"] not in seeded]
    return tool_calls, final_text(result["messages"])


async def _afresh_delegator(**kwargs) -> RecordingDelegator:
    return RecordingDelegator(await _acoordinator(), **kwargs)


async def test_a_change_in_two_other_repositories_is_delegated_as_one_leg_each(model_name, eval_request):
    delegator = await _afresh_delegator()

    await _run(model_name, [HumanMessage(RENAME_HEADER)], delegator, eval_request)

    assert len(delegator.calls) == 1, f"expected one delegate_jobs call, got {len(delegator.calls)}"
    assert sorted(target.repo_id for target in delegator.targets()) == [BILLING, NOTIFIER], delegator.targets()
    missing = [target.repo_id for target in delegator.targets() if NO_CHANGE_CONVENTION not in target.prompt]
    assert not missing, f"leg prompts without the no-change convention: {missing}"


async def test_after_delegating_it_ends_the_turn_without_checking_on_the_legs(model_name, eval_request):
    delegator = await _afresh_delegator()

    tool_calls, _reply = await _run(model_name, [HumanMessage(BUMP_REQUESTS)], delegator, eval_request)

    names = [call["name"] for call in tool_calls]
    assert DELEGATE_JOBS_TOOL_NAME in names, names
    after = [name for name in names[names.index(DELEGATE_JOBS_TOOL_NAME) + 1 :] if name != "write_todos"]
    assert not after, f"kept working after delegating instead of ending the turn: {after}"


async def test_a_resumed_run_reports_every_merge_request_without_delegating_again(model_name, eval_request):
    billing_mr = "https://gitlab.example/acme/billing-api/-/merge_requests/12"
    notifier_mr = "https://gitlab.example/acme/notifier/-/merge_requests/7"
    delegator, messages = await _resumed(
        PIN_HTTPX,
        FinishedLeg(
            BILLING,
            f"Pin `httpx` below 0.30. {NO_CHANGE_CONVENTION}",
            "Pinned `httpx>=0.27,<0.30` in pyproject.toml and refreshed uv.lock; the test suite passes.",
            branch="chore/pin-httpx",
            merge_request_url=billing_mr,
        ),
        FinishedLeg(
            NOTIFIER,
            f"Pin `httpx` below 0.30. {NO_CHANGE_CONVENTION}",
            "Pinned `httpx<0.30` in requirements.txt; nothing else imports it directly.",
            branch="chore/pin-httpx-below-0-30",
            merge_request_url=notifier_mr,
        ),
    )

    tool_calls, reply = await _run(model_name, messages, delegator, eval_request)

    assert not delegator.calls, f"delegated again after every leg succeeded: {delegator.targets()}"
    assert billing_mr in reply, reply
    assert notifier_mr in reply, reply
    assert DELEGATE_JOBS_TOOL_NAME not in [call["name"] for call in tool_calls]


async def test_a_leg_that_started_but_was_lost_is_not_delegated_again(model_name, eval_request):
    delegator = await _afresh_delegator(lost=frozenset({NOTIFIER}))

    _tool_calls, reply = await _run(model_name, [HumanMessage(RENAME_SETTING)], delegator, eval_request)

    notifier_legs = [target for target in delegator.targets() if target.repo_id == NOTIFIER]
    assert len(notifier_legs) == 1, f"the lost {NOTIFIER} leg was delegated {len(notifier_legs)} times"
    assert "notifier" in reply.lower(), reply
    assert LOST_LEG_WORDING.search(reply), f"the reply does not say the {NOTIFIER} leg was lost: {reply}"


async def test_a_finding_deep_in_a_legs_reply_reaches_the_report(model_name, eval_request):
    assert ENDPOINT_REPORT.index(STILL_FAILING) > 2000, "the finding must sit past every reply cap DAIV applies today"
    delegator, messages = await _resumed(
        FIND_500,
        FinishedLeg(
            BILLING,
            f"Replay every billing endpoint against the migrated database and report which still answer 500, and "
            f"why. Change nothing. {NO_CHANGE_CONVENTION}",
            ENDPOINT_REPORT,
        ),
    )

    _tool_calls, reply = await _run(model_name, messages, delegator, eval_request)

    assert re.search(r"invoices/\{id\}/pdf|pdf_blob_key", reply), (
        f"expected to fail while the coordinator sees only a reply's first {LEG_REPLY_MAX_CHARS} characters: {reply}"
    )
