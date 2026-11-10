"""The coordinator workflow of ``delegate_jobs``: fanning legs out, ending the turn, and the run a batch resumes.

Each case runs the agent on a disk clone of srtab/daiv ``main`` with a delegator that records what it is asked to start
and puts those legs in the database instead of starting them, a leg follow-up as a run on its leg's session; its status
and cancel are the real ones, over the legs in the database. A resumed run gets what a real ``delegation_finished``
event renders: ``render_batch_summary`` over those legs, after the turn that delegated them.
"""

import re
import uuid
from dataclasses import dataclass

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from django_tasks_db.models import DBTaskResult, get_date_max
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sessions.delegation import (
    LEG_REPLY_MAX_CHARS,
    SessionJobDelegator,
    _failed_leg,
    _on_own_branch,
    _session_url,
    check_delegation,
    delegation_finished_payload,
)
from sessions.inbox.kinds import SourceKey
from sessions.inbox.render import RunInput
from sessions.models import (
    RESULT_SUMMARY_MAX_CHARS,
    EventKind,
    EventState,
    InboxEvent,
    Run,
    RunStatus,
    Session,
    SessionOrigin,
)
from sessions.queue import LINK_FAILED_PREFIX
from sessions.services import BatchSubmitFailure

from automation.agent.delegation import (
    DELEGATE_JOBS_TOOL_NAME,
    Checkout,
    DelegatedTarget,
    DelegationResult,
    FailedLeg,
    StartedLeg,
)
from automation.agent.middlewares.delegate_jobs import NO_CHANGE_CONVENTION, delegation_payload

from .prompt_leak import assert_no_prompt_leak
from .utils import DELEGATION_MODELS, require_provider_for_model, run_eval_case

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
OPS_DOCS = "acme/ops-docs"
PIN_THEN_DOCUMENT = (
    f"Pin `httpx` below 0.30 in {BILLING} and in {NOTIFIER}, with a merge request per repository. Once both pins are "
    f"in, record the new ceiling in `dependencies.md` of {OPS_DOCS}, with a merge request there too."
)
NOTIFIER_TESTS_FAIL = (
    "I pinned `httpx<0.30` in requirements.txt and opened the merge request, but the work is not done: "
    "`tests/test_client.py::test_retry_on_timeout` now fails, because it builds `httpx.Timeout(read=...)` without the "
    "positional default that 0.29 requires. I did not change the test. The merge request's CI will stay red until "
    "that test is fixed."
)
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

assert_no_prompt_leak([
    RENAME_HEADER,
    BUMP_REQUESTS,
    PIN_HTTPX,
    RENAME_SETTING,
    FIND_500,
    STILL_FAILING,
    PIN_THEN_DOCUMENT,
    NOTIFIER_TESTS_FAIL,
])

LOST_LEG_WORDING = re.compile(
    r"\blost\b|untracked|(?:may|might|could) still (?:open|run|publish)"
    r"|(?:can|could)(?:not|n['’]t| not) (?:track|follow)",
    re.IGNORECASE,
)


@pytest.fixture(autouse=True)
def _require_provider(model_name):
    require_provider_for_model(model_name)


def _coordinator() -> Session:
    """SYNC ONLY: a chat session on srtab/daiv ``main``, the session that delegates."""
    return Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="srtab/daiv", ref="main"
    )


def _create_leg(
    coordinator_id: str, batch_id: uuid.UUID, repo_id: str, prompt: str, *, session_ref: str, **run_fields
) -> Run:
    """SYNC ONLY: a ``DELEGATED_JOB`` run in a session of its own under ``coordinator_id``; given a ``task_result``,
    it takes its outcome from it as ``Run.sync_from_task_result`` does for a real leg."""
    session = Session.objects.create(
        thread_id=str(uuid.uuid4()),
        origin=SessionOrigin.DELEGATED_JOB,
        repo_id=repo_id,
        ref=session_ref,
        parent_id=coordinator_id,
    )
    run = Run(
        session=session,
        trigger_type=SessionOrigin.DELEGATED_JOB,
        batch_id=batch_id,
        repo_id=repo_id,
        prompt=prompt,
        **run_fields,
    )
    run.sync_from_task_result()
    run.save()
    return run


class RecordingDelegator(SessionJobDelegator):
    """Starts nothing: records every ``delegate_jobs`` call it accepts and puts each leg in the database as queued,
    except those on ``lost`` repositories, which fail the way a leg DAIV started but lost track of does. A leg follow-up
    the coordinator may send becomes a queued run on its leg's session, in the call's batch; one it may not send is
    refused as the real delegator refuses it."""

    def __init__(self, thread_id: str, *, lost: frozenset[str] = frozenset()):
        super().__init__(thread_id)
        self.lost = lost
        self.calls: list[list[DelegatedTarget]] = []

    async def adelegate(self, *, checkout: Checkout, goal: str, targets: list[DelegatedTarget]) -> DelegationResult:
        check_delegation(goal, targets)
        self.calls.append(list(targets))
        return await sync_to_async(self._record)(checkout, targets)

    def _record(self, checkout: Checkout, targets: list[DelegatedTarget]) -> DelegationResult:
        batch_id = uuid.uuid4()
        result = DelegationResult(batch_id=str(batch_id))
        for target in targets:
            if target.thread_id:
                self._record_follow_up(target, batch_id, result)
                continue
            lost = target.repo_id in self.lost
            leg = _create_leg(
                self._thread_id,
                batch_id,
                target.repo_id,
                target.prompt,
                session_ref=target.ref,
                ref=target.ref,
                status=RunStatus.FAILED if lost else RunStatus.QUEUED,
                error_message=LINK_FAILED_PREFIX if lost else "",
            )
            if lost:
                result.failed.append(
                    _failed_leg(BatchSubmitFailure(target.repo_id, target.ref, "", task_enqueued=True))
                )
            else:
                result.started.append(
                    StartedLeg(
                        target.repo_id,
                        target.ref,
                        leg.session_id,
                        _session_url(leg.session_id),
                        starts_after_this_turn=_on_own_branch(checkout, target),
                    )
                )
        return result

    def _record_follow_up(self, target: DelegatedTarget, batch_id: uuid.UUID, result: DelegationResult) -> None:
        leg, refusal = self._check_follow_up(target, None)
        if refusal is not None:
            result.failed.append(FailedLeg(target.repo_id, target.ref, refusal, thread_id=target.thread_id))
            return
        session = leg.session
        Run.objects.create(
            session=session,
            trigger_type=SessionOrigin.DELEGATED_JOB,
            batch_id=batch_id,
            repo_id=session.repo_id,
            ref=session.ref,
            prompt=target.prompt,
            status=RunStatus.QUEUED,
        )
        result.started.append(
            StartedLeg(session.repo_id, session.ref, session.pk, _session_url(session.pk), leg_follow_up=True)
        )

    def targets(self) -> list[DelegatedTarget]:
        return [target for call in self.calls for target in call]


async def _afresh_delegator(*, lost: frozenset[str] = frozenset()) -> RecordingDelegator:
    coordinator = await sync_to_async(_coordinator)()
    return RecordingDelegator(coordinator.thread_id, lost=lost)


@dataclass(frozen=True)
class FinishedLeg:
    repo_id: str
    prompt: str
    reply: str
    branch: str = ""
    merge_request_url: str = ""


def _finished_batch(coordinator_id: str, batch_id: uuid.UUID, legs: tuple[FinishedLeg, ...]) -> list[Run]:
    """SYNC ONLY: one succeeded leg per ``legs``, synced from the task result its finished task would leave."""
    return [
        _create_leg(
            coordinator_id,
            batch_id,
            leg.repo_id,
            leg.prompt,
            session_ref=leg.branch,
            task_result=DBTaskResult.objects.create(
                id=uuid.uuid4(),
                status="SUCCESSFUL",
                task_path="sessions.executor.tasks.run_job_task",
                args_kwargs={"args": [], "kwargs": {}},
                queue_name="default",
                backend_name="default",
                run_after=get_date_max(),
                finished_at=timezone.now(),
                return_value={"response": leg.reply, "merge_request_web_url": leg.merge_request_url or None},
            ),
        )
        for leg in legs
    ]


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
        ToolMessage(delegation_payload(result), tool_call_id=call_id, name=DELEGATE_JOBS_TOOL_NAME),
        AIMessage("I delegated one job per repository; I will report back once they have all finished."),
    ]


def _resume_input(request: str, legs: tuple[FinishedLeg, ...]) -> tuple[str, list]:
    """SYNC ONLY: the coordinator's thread id, and the input of the run its finished batch resumes as the inbox
    renders it."""
    coordinator = _coordinator()
    batch_id = uuid.uuid4()
    runs = _finished_batch(coordinator.thread_id, batch_id, legs)
    resume = Run.objects.create(
        session=coordinator,
        trigger_type=SessionOrigin.DELEGATION_RESUME,
        repo_id="srtab/daiv",
        ref="main",
        status=RunStatus.READY,
    )
    InboxEvent.objects.create(
        session=coordinator,
        run=resume,
        state=EventState.TAKEN,
        kind=EventKind.DELEGATION_FINISHED,
        wake=True,
        source_key=SourceKey.batch(batch_id),
        ref="main",
        payload=delegation_finished_payload(batch_id, runs),
    )
    rendered = RunInput.of_run(resume).render(resume).messages
    return coordinator.thread_id, [*_delegating_turn(request, batch_id, runs), *rendered]


async def _resumed(request: str, *legs: FinishedLeg) -> tuple[RecordingDelegator, list]:
    """The delegator and input of the run a finished batch resumes."""
    thread_id, messages = await sync_to_async(_resume_input)(request, legs)
    return RecordingDelegator(thread_id), messages


async def test_a_change_in_two_other_repositories_is_delegated_as_one_leg_each(model_name, eval_request):
    delegator = await _afresh_delegator()

    await run_eval_case(model_name, [HumanMessage(RENAME_HEADER)], eval_request, delegator=delegator)

    assert len(delegator.calls) == 1, f"expected one delegate_jobs call, got {len(delegator.calls)}"
    assert sorted(target.repo_id for target in delegator.targets()) == [BILLING, NOTIFIER], delegator.targets()
    missing = [target.repo_id for target in delegator.targets() if NO_CHANGE_CONVENTION not in target.prompt]
    assert not missing, f"leg prompts without the no-change convention: {missing}"


async def test_after_delegating_it_ends_the_turn_without_checking_on_the_legs(model_name, eval_request):
    delegator = await _afresh_delegator()

    tool_calls, _reply = await run_eval_case(
        model_name, [HumanMessage(BUMP_REQUESTS)], eval_request, delegator=delegator
    )

    names = [call["name"] for call in tool_calls]
    assert DELEGATE_JOBS_TOOL_NAME in names, names
    after = [name for name in names[names.index(DELEGATE_JOBS_TOOL_NAME) + 1 :] if name != "write_todos"]
    assert not after, f"kept working after delegating instead of ending the turn: {after}"


async def test_a_resumed_run_reports_every_merge_request_without_delegating_again(model_name, eval_request):
    leg_prompt = f"Pin `httpx` below 0.30. {NO_CHANGE_CONVENTION}"
    billing_mr = "https://gitlab.example/acme/billing-api/-/merge_requests/12"
    notifier_mr = "https://gitlab.example/acme/notifier/-/merge_requests/7"
    delegator, messages = await _resumed(
        PIN_HTTPX,
        FinishedLeg(
            BILLING,
            leg_prompt,
            "Pinned `httpx>=0.27,<0.30` in pyproject.toml and refreshed uv.lock; the test suite passes.",
            branch="chore/pin-httpx",
            merge_request_url=billing_mr,
        ),
        FinishedLeg(
            NOTIFIER,
            leg_prompt,
            "Pinned `httpx<0.30` in requirements.txt; nothing else imports it directly.",
            branch="chore/pin-httpx-below-0-30",
            merge_request_url=notifier_mr,
        ),
    )

    tool_calls, reply = await run_eval_case(model_name, messages, eval_request, delegator=delegator)

    assert DELEGATE_JOBS_TOOL_NAME not in [call["name"] for call in tool_calls], (
        f"delegated again after every leg succeeded: {delegator.targets()}"
    )
    assert billing_mr in reply, reply
    assert notifier_mr in reply, reply


async def test_a_leg_that_started_but_was_lost_is_not_delegated_again(model_name, eval_request):
    delegator = await _afresh_delegator(lost=frozenset({NOTIFIER}))

    _tool_calls, reply = await run_eval_case(
        model_name, [HumanMessage(RENAME_SETTING)], eval_request, delegator=delegator
    )

    notifier_legs = [target for target in delegator.targets() if target.repo_id == NOTIFIER]
    assert len(notifier_legs) == 1, f"the lost {NOTIFIER} leg was delegated {len(notifier_legs)} times"
    assert "notifier" in reply.lower(), reply
    assert LOST_LEG_WORDING.search(reply), f"the reply does not say the {NOTIFIER} leg was lost: {reply}"


async def test_a_finding_deep_in_a_legs_reply_reaches_the_report(model_name, eval_request):
    finding = ENDPOINT_REPORT.index(STILL_FAILING)
    assert RESULT_SUMMARY_MAX_CHARS < finding < finding + len(STILL_FAILING) <= LEG_REPLY_MAX_CHARS, (
        "the finding must sit past the stored summary's cap, inside the reply the coordinator reads"
    )
    delegator, messages = await _resumed(
        FIND_500,
        FinishedLeg(
            BILLING,
            f"Replay every billing endpoint against the migrated database and report which still answer 500, and "
            f"why. Change nothing. {NO_CHANGE_CONVENTION}",
            ENDPOINT_REPORT,
        ),
    )

    _tool_calls, reply = await run_eval_case(model_name, messages, eval_request, delegator=delegator)

    assert re.search(r"invoices/\{id\}/pdf|pdf_blob_key", reply), (
        f"the coordinator reads up to {LEG_REPLY_MAX_CHARS} characters of a leg's reply, the finding included: {reply}"
    )


def _leg_session(coordinator_id: str, repo_id: str) -> str:
    """SYNC ONLY."""
    return Session.objects.get(parent_id=coordinator_id, repo_id=repo_id).pk


async def test_a_leg_that_reports_its_work_incomplete_gets_a_leg_follow_up(model_name, eval_request):
    leg_prompt = f"Pin `httpx` below 0.30. {NO_CHANGE_CONVENTION}"
    delegator, messages = await _resumed(
        PIN_HTTPX,
        FinishedLeg(
            BILLING,
            leg_prompt,
            "Pinned `httpx>=0.27,<0.30` in pyproject.toml and refreshed uv.lock; the test suite passes.",
            branch="chore/pin-httpx",
            merge_request_url="https://gitlab.example/acme/billing-api/-/merge_requests/12",
        ),
        FinishedLeg(
            NOTIFIER,
            leg_prompt,
            NOTIFIER_TESTS_FAIL,
            branch="chore/pin-httpx-below-0-30",
            merge_request_url="https://gitlab.example/acme/notifier/-/merge_requests/7",
        ),
    )
    notifier = await sync_to_async(_leg_session)(delegator._thread_id, NOTIFIER)

    await run_eval_case(model_name, messages, eval_request, delegator=delegator)

    assert len(delegator.calls) == 1, f"expected one delegate_jobs call, got {len(delegator.calls)}"
    [targets] = delegator.calls
    assert [(target.thread_id, target.ref) for target in targets] == [(notifier, "")], (
        f"expected one leg follow-up to the {NOTIFIER} leg, with no ref: {targets}"
    )


async def test_an_independent_second_part_gets_a_new_leg(model_name, eval_request):
    leg_prompt = f"Pin `httpx` below 0.30. {NO_CHANGE_CONVENTION}"
    delegator, messages = await _resumed(
        PIN_THEN_DOCUMENT,
        FinishedLeg(
            BILLING,
            leg_prompt,
            "Pinned `httpx>=0.27,<0.30` in pyproject.toml and refreshed uv.lock; the test suite passes.",
            branch="chore/pin-httpx",
            merge_request_url="https://gitlab.example/acme/billing-api/-/merge_requests/12",
        ),
        FinishedLeg(
            NOTIFIER,
            leg_prompt,
            "Pinned `httpx<0.30` in requirements.txt; nothing else imports it directly, and the test suite passes.",
            branch="chore/pin-httpx-below-0-30",
            merge_request_url="https://gitlab.example/acme/notifier/-/merge_requests/7",
        ),
    )

    await run_eval_case(model_name, messages, eval_request, delegator=delegator)

    new_legs = [target for target in delegator.targets() if not target.thread_id]
    assert new_legs, f"expected a new leg for the second part, got: {delegator.targets()}"
    assert OPS_DOCS in [target.repo_id for target in new_legs], new_legs
