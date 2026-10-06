"""Recall of the ``/code-review`` skill on hand-planted bugs, with clean twins to price the false positives.

Each run clones srtab/daiv, points the clone's ``main`` at the case's ``base_sha`` and drops its upstream
(``set_runtime_ctx`` clones with ``git clone --branch``, which takes no commit sha, and the agent is told it is on
``main``), applies the case's patch to the working tree, and asks for an interactive working-tree review in a fresh
sandbox seeded from that clone. A case that cannot be set up stops the whole run: it is a broken case, not a vote.
"""

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from git import GitCommandError
from langgraph.checkpoint.memory import InMemorySaver
from langsmith import testing as t
from sandbox_envs.services import build_sandbox_spec

from automation.agent.graph import create_daiv_agent
from automation.agent.workspace.sandbox import SandboxWorkspace
from automation.agent.workspace.session import SandboxSession
from codebase.base import Scope
from codebase.context import set_runtime_ctx
from core.site_settings import site_settings

from .code_review_grading import (
    Finding,
    JudgeError,
    blocking,
    clean_case_violation,
    grade_bug_case,
    is_degraded,
    parse_report,
    severity_counts,
    validate_cases,
)
from .prompt_leak import assert_no_prompt_leak
from .utils import CODE_REVIEW_MODELS, agent_settings_on, final_text, measure, require_provider_for_model

DATA_DIR = Path(__file__).parent / "data" / "code_review"
TEST_SUITE = "DAIV: Code review recall"
REVIEW_REQUEST = "/code-review"


def load_cases() -> list:
    cases = [json.loads(line) for line in (DATA_DIR / "cases.jsonl").read_text().splitlines() if line.strip()]
    validate_cases(cases, DATA_DIR)
    assert_no_prompt_leak([REVIEW_REQUEST, *(case["planted"]["defect"] for case in cases if case["kind"] == "bug")])
    return [pytest.param(case, id=case["id"]) for case in cases]


CASES = load_cases()


@asynccontextmanager
async def patched_checkout(case: dict):
    async with set_runtime_ctx(
        repo_id="srtab/daiv", scope=Scope.GLOBAL, ref="main", sandbox_spec=await build_sandbox_spec(None)
    ) as ctx:
        if ctx.sandbox_client is None:
            pytest.skip("The global default sandbox environment has no base image.")
        try:
            ctx.gitrepo.git.checkout("-B", "main", case["base_sha"])
            ctx.gitrepo.git.branch("--unset-upstream", "main")
        except GitCommandError as err:
            pytest.exit(
                f"{case['id']}: could not check out base_sha {case['base_sha']}: {err.stderr.strip()}", returncode=2
            )
        try:
            ctx.gitrepo.git.apply(str(DATA_DIR / case["patch_path"]))
        except GitCommandError as err:
            pytest.exit(
                f"{case['id']}: {case['patch_path']} does not apply on {case['base_sha']}: {err.stderr.strip()}",
                returncode=2,
            )
        session = SandboxSession(ctx.sandbox_client, ctx.sandbox, credential_source=ctx.credential_source)
        try:
            yield ctx, session
        finally:
            await session.release(resumable=False)


_SAME_FILE_OTHER_BUG = Finding(
    severity="Important",
    title="Exported `time` drops the timezone",
    location="`daiv/schedules/api/views.py:26`",
    details="`schedule.time.isoformat()` emits a naive time of day, so a client in another timezone reads the wrong "
    "run time. Include the timezone the scheduler evaluates `time` in.",
)
_PLANTED_BUG = Finding(
    severity="Critical",
    title="Any API caller can export any schedule",
    location="`daiv/schedules/api/views.py:14`",
    details="The endpoint queries `ScheduledJob.objects` with no owner filter, so any API key reads other users' "
    "schedules, prompts and subscriber emails. Scope it with `ScheduledJob.objects.by_owner(request.auth)`.",
)


@pytest.mark.code_review
@pytest.mark.langsmith(test_suite_name=TEST_SUITE)
async def test_the_judge_tells_the_planted_bug_from_another_in_the_same_file():
    """Runs before the cases: a judge that fails it would grade every recall row, so it stops the whole run."""
    planted = next(param.values[0] for param in CASES if param.id == "bug-schedule-export-no-owner-check")["planted"]

    try:
        hit = (await grade_bug_case([_PLANTED_BUG], planted)).hit
        other = (await grade_bug_case([_SAME_FILE_OTHER_BUG], planted)).hit
    except JudgeError as err:
        pytest.exit(f"The recall judge is unavailable: {err}", returncode=2)
    if not hit or other:
        pytest.exit(
            f"The recall judge cannot tell the planted bug from another in the same file (planted: {hit}, other: "
            f"{other}).",
            returncode=2,
        )


@pytest.mark.code_review
@pytest.mark.langsmith(test_suite_name=TEST_SUITE)
@pytest.mark.parametrize("model_name", CODE_REVIEW_MODELS)
@pytest.mark.parametrize("case", CASES)
async def test_code_review_recall(model_name, case, eval_request):
    require_provider_for_model(model_name)
    if site_settings.sandbox_api_key is None:
        pytest.skip("SANDBOX_API_KEY is not configured.")
    t.log_inputs({"model_name": model_name, "case": case["id"]})

    async with patched_checkout(case) as (ctx, session):
        agent = await create_daiv_agent(
            settings=agent_settings_on(model_name, ctx),
            ctx=ctx,
            workspace=SandboxWorkspace(session),
            auto_commit_changes=False,
            checkpointer=InMemorySaver(),
            ask_user_enabled=False,
        )
        with measure(eval_request) as metrics:
            metrics.extra["kind"] = case["kind"]
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": REVIEW_REQUEST}]},
                context=ctx,
                config={"configurable": {"thread_id": "1"}},
            )
    metrics.messages = result["messages"]
    report = final_text(result["messages"])
    findings = parse_report(report)
    metrics.extra.update(findings=severity_counts(findings), degraded=is_degraded(report))
    t.log_outputs({"report": report})

    if case["kind"] == "clean":
        violation = clean_case_violation(report, findings)
        metrics.extra["noise"] = len(blocking(findings))
        assert violation is None, violation
        return

    try:
        grade = await grade_bug_case(findings, case["planted"])
    except JudgeError as err:
        pytest.skip(f"The judge failed, which is not a review outcome: {err}")
    metrics.extra.update(noise=grade.noise, hit_severity=grade.hit_severity)
    assert grade.hit, (
        f"No finding matched the planted defect: {case['planted']['defect']}\n"
        f"Judge: {list(grade.explanations)}\nReport:\n{report}"
    )
