"""Recall of the ``/code-review`` skill on hand-planted bugs, with clean twins to price the false positives.

Each run clones srtab/daiv, checks out the case's ``base_sha`` (``set_runtime_ctx`` clones with ``git clone --branch``,
which takes no commit sha), applies the case's patch to the working tree, and asks for an interactive working-tree
review in a fresh sandbox seeded from that clone.
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
    blocking,
    clean_case_violation,
    grade_bug_case,
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
            ctx.gitrepo.git.checkout("--detach", case["base_sha"])
        except GitCommandError as err:
            pytest.fail(f"base_sha {case['base_sha']} is not in the integration copy of srtab/daiv: {err}")
        ctx.gitrepo.git.apply(str(DATA_DIR / case["patch_path"]))
        session = SandboxSession(ctx.sandbox_client, ctx.sandbox, credential_source=ctx.credential_source)
        try:
            yield ctx, session
        finally:
            await session.release(resumable=False)


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
    metrics.extra["findings"] = severity_counts(findings)
    t.log_outputs({"report": report})

    if case["kind"] == "clean":
        violation = clean_case_violation(report, findings)
        metrics.extra["noise"] = len(blocking(findings))
        assert violation is None, violation
        return

    grade = await grade_bug_case(findings, case["planted"])
    metrics.extra["noise"] = grade.noise
    assert grade.hit, (
        f"No finding matched the planted defect: {case['planted']['defect']}\n"
        f"Judge: {list(grade.explanations)}\nReport:\n{report}"
    )


_SAME_FILE_OTHER_BUG = Finding(
    severity="Important",
    title="Export response is not cached",
    location="`daiv/schedules/views.py:425`",
    details="Each download serialises the schedule again; add a cache header so browsers reuse the file.",
)
_PLANTED_BUG = Finding(
    severity="Critical",
    title="Any user can export any schedule",
    location="`daiv/schedules/views.py:410`",
    details="The view fetches from `ScheduledJob.objects` with no owner filter, so a logged-in user can read other "
    "users' schedules, prompts and subscriber emails. Scope it with `_ScheduleOwnerMixin`.",
)


@pytest.mark.code_review
@pytest.mark.langsmith(test_suite_name=TEST_SUITE)
async def test_the_judge_tells_the_planted_bug_from_another_in_the_same_file():
    planted = next(param.values[0] for param in CASES if param.id == "bug-schedule-export-no-owner-check")["planted"]

    assert (await grade_bug_case([_PLANTED_BUG], planted)).hit
    assert not (await grade_bug_case([_SAME_FILE_OTHER_BUG], planted)).hit
