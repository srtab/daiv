"""Execute frozen review patches locally: prove planted behavior and the clean twins without LLM calls."""

from __future__ import annotations

import ast
import io
import json
import logging
import re
import shutil
import subprocess  # noqa: S404
import tarfile
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from django.db.models import Prefetch

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin

from schedules.models import ScheduledJob

DATA_DIR = Path(__file__).parents[1] / "integration_tests" / "data" / "code_review"
REPO_ROOT = Path(__file__).parents[2]
GIT = shutil.which("git")
CASES = {case["id"]: case for line in (DATA_DIR / "cases.jsonl").read_text().splitlines() if (case := json.loads(line))}


@pytest.fixture
def patched_tree(tmp_path):
    """Apply an actual shipped patch to the exact Git blobs it names; no clone, checkout, or network."""

    def apply(case_id):
        assert case_id in CASES, f"missing recall fixture: {case_id}"
        case = CASES[case_id]
        patch = DATA_DIR / case["patch_path"]
        paths = re.findall(r"^diff --git a/(\S+) b/", patch.read_text(), re.MULTILINE)
        tracked = subprocess.run(  # noqa: S603, S607
            [GIT, "ls-tree", "-r", "--name-only", case["base_sha"]],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        existing = [path for path in paths if path in tracked]
        tree = tmp_path / case_id
        tree.mkdir()
        archive = subprocess.run(  # noqa: S603, S607
            [GIT, "archive", case["base_sha"], "--", *existing], cwd=REPO_ROOT, check=True, capture_output=True
        )
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as blobs:
            blobs.extractall(tree, filter="data")
        subprocess.run([GIT, "apply", str(patch)], cwd=tree, check=True, capture_output=True)  # noqa: S603
        return tree

    return apply


def _function(tree, path, name, *, class_name=None, **namespace):
    """Compile a patch's real function with its real ORM dependencies, leaving route registration out."""
    module = ast.parse((tree / path).read_text())
    parent = (
        next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == class_name)
        if class_name
        else module
    )
    function = next(
        node for node in parent.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    )
    function.decorator_list = []
    unit = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(unit), str(tree / path), "exec"), namespace)  # noqa: S102
    return namespace[name]


@pytest.mark.parametrize("case_id", CASES)
def test_frozen_patch_applies_to_its_declared_base(patched_tree, case_id):
    patched_tree(case_id)


@pytest.mark.parametrize(
    "case_id,override,want",
    [
        ("clean-run-mute-override", False, False),
        ("clean-run-mute-override", True, True),
        ("clean-run-mute-override", None, True),
        ("bug-run-mute-false-override", False, True),
    ],
)
def test_mute_override_fixture_distinguishes_false_from_inherit(patched_tree, case_id, override, want):
    effective_muted = _function(patched_tree(case_id), "daiv/sessions/models.py", "effective_muted", class_name="Run")
    schedule = ScheduledJob(pk=1, muted=True)
    session = Session(thread_id=str(uuid.uuid4()), scheduled_job=schedule)
    run = Run(session=session, muted=override)

    assert effective_muted(run) is want


@pytest.mark.django_db
@pytest.mark.parametrize(
    "case_id,leaks", [("clean-session-owner-correlation", False), ("bug-session-owner-cross-thread", True)]
)
def test_owner_fixture_limits_actor_access_to_their_own_thread(patched_tree, member_user, admin_user, case_id, leaks):
    tree = patched_tree(case_id)
    namespace = {"__name__": "recall_fixture_managers"}
    exec(compile((tree / "daiv/sessions/managers.py").read_text(), "fixture_managers.py", "exec"), namespace)  # noqa: S102
    mine = Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.ISSUE_WEBHOOK, user=admin_user, repo_id="group/shared"
    )
    victim = Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.ISSUE_WEBHOOK, user=admin_user, repo_id="group/shared"
    )
    Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.ISSUE_WEBHOOK, user=admin_user, repo_id="group/other"
    )
    Run.objects.create(session=mine, repo_id=mine.repo_id, user=member_user, trigger_type=SessionOrigin.ISSUE_WEBHOOK)
    queryset = namespace["SessionQuerySet"](model=Session, using="default").by_owner(member_user)

    assert set(queryset.values_list("pk", flat=True)) == ({mine.pk, victim.pk} if leaks else {mine.pk})


@pytest.mark.django_db
@pytest.mark.parametrize(
    "case_id,queries", [("clean-session-runs-prefetch", 2), ("bug-session-runs-prefetch-deferred-fk", 8)]
)
def test_prefetch_fixture_counts_the_implicit_foreign_key_reads(
    patched_tree, admin_user, django_assert_num_queries, case_id, queries
):
    get_queryset = _function(
        patched_tree(case_id),
        "daiv/sessions/views.py",
        "get_queryset",
        class_name="SessionListView",
        Session=Session,
        Run=Run,
        Prefetch=Prefetch,
    )
    sessions = [
        Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.ISSUE_WEBHOOK, user=admin_user, repo_id="group/repo"
        )
        for _ in range(3)
    ]
    for session in sessions:
        for _ in range(2):
            Run.objects.create(session=session, repo_id=session.repo_id, trigger_type=SessionOrigin.ISSUE_WEBHOOK)
    view = SimpleNamespace(request=SimpleNamespace(user=admin_user))
    queryset = get_queryset(view).filter(pk__in=[session.pk for session in sessions])

    with django_assert_num_queries(queries):
        rows = list(queryset)
        assert [len(row.runs.all()) for row in rows] == [2, 2, 2]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("status", [RunStatus.READY, RunStatus.WAITING_INPUT, RunStatus.SUCCESSFUL, RunStatus.FAILED])
@pytest.mark.parametrize("case_id,saved", [("clean-mute-job", True), ("bug-mute-job-missing-await", False)])
async def test_mute_job_fixture_changes_future_delivery_regardless_of_status(
    patched_tree, member_user, case_id, saved, status
):
    async def resolve_user():
        return member_user, None

    mute_job = _function(
        patched_tree(case_id),
        "daiv/mcp_api/server.py",
        "mute_job",
        Run=Run,
        uuid_mod=uuid,
        _resolve_mcp_user=resolve_user,
        logger=logging.getLogger("recall_fixture"),
    )
    session = await Session.objects.acreate(thread_id=str(uuid.uuid4()), origin=SessionOrigin.MCP_JOB, user=member_user)
    run = await Run.objects.acreate(
        session=session,
        user=member_user,
        repo_id="group/repo",
        muted=False,
        status=status,
        trigger_type=SessionOrigin.MCP_JOB,
    )
    warnings = nullcontext() if saved else pytest.warns(RuntimeWarning, match="was never awaited")

    with warnings:
        result = await mute_job(str(run.pk))
    await run.arefresh_from_db()

    assert result == {"job_id": str(run.pk), "muted": True}
    assert run.muted is saved


@pytest.mark.django_db
def test_clean_schedule_list_batches_queries_and_breaks_creation_time_ties(
    patched_tree, member_user, django_assert_num_queries
):
    attach = _function(
        patched_tree("clean-schedule-list"), "daiv/schedules/views.py", "_attach_last_run_status", Run=Run
    )
    batch_id = uuid.uuid4()
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.SCHEDULE)
    first = Run.objects.create(
        id=uuid.UUID(int=1),
        session=session,
        batch_id=batch_id,
        repo_id="group/repo",
        trigger_type=SessionOrigin.SCHEDULE,
        status=RunStatus.FAILED,
    )
    Run.objects.create(
        id=uuid.UUID(int=2),
        session=session,
        batch_id=batch_id,
        repo_id="group/repo",
        trigger_type=SessionOrigin.SCHEDULE,
        status=RunStatus.SUCCESSFUL,
        created_at=first.created_at,
    )
    schedules = [SimpleNamespace(last_run_batch_id=batch_id), SimpleNamespace(last_run_batch_id=None)]

    with django_assert_num_queries(1):
        attach(schedules)

    assert [schedule.last_run_status for schedule in schedules] == [RunStatus.SUCCESSFUL, None]


@pytest.mark.django_db(transaction=True)
async def test_clean_export_fixture_preserves_owner_boundary_and_serializable_settings(
    patched_tree, member_user, admin_user
):
    tree = patched_tree("clean-schedule-export")
    namespace = {"__name__": "recall_fixture_export"}
    exec(compile((tree / "daiv/schedules/api/views.py").read_text(), "fixture_export.py", "exec"), namespace)  # noqa: S102
    schedule = await ScheduledJob.objects.acreate(
        user=member_user, name="daily", prompt="p", repos=[{"repo_id": "group/repo", "ref": "main"}]
    )
    request = SimpleNamespace(auth=member_user)

    status, payload = await namespace["export_schedule"](request, schedule.pk)

    assert status == 200
    assert json.loads(json.dumps(payload))["repos"] == [{"repo_id": "group/repo", "ref": "main"}]
    assert payload["name"] == "daily"
    outsider = await type(member_user).objects.acreate(username="outsider", email="outsider@example.com")
    assert await namespace["export_schedule"](SimpleNamespace(auth=outsider), schedule.pk) == (
        404,
        {"detail": "Schedule not found"},
    )
