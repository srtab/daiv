"""Repository-wide guard on which queue each background task runs on.

Lives at the top of tests/unit_tests/ rather than in an app package: tasks ship from six
apps, the queues they name are declared in the settings, and the workers that serve those
queues are declared in the compose file and the deployment guide — a split that is only
real while all of them agree.

``db_worker`` claims one task and runs it to completion before looking for the next, so
concurrency is worker *processes*, not tasks, and priority only reorders what is waiting —
it never preempts what is running. An agent run holds its worker for minutes (``run_job_task``
alone can sleep up to 30 minutes inside the task waiting on the session lock), so a title
sharing that queue is only as fast as the longest run ahead of it, which is what left sessions
reading "generating title…" an hour after they were created. Hence the split: short,
user-visible work goes on ``interactive`` and nothing long may join it.
"""

from __future__ import annotations

import importlib
import re
from typing import TYPE_CHECKING

from django.conf import settings

import yaml
from django_tasks.base import Task

from core.constants import TASK_QUEUE_INTERACTIVE
from tests.unit_tests.test_template_comments import DAIV_DIR, REPO_ROOT

if TYPE_CHECKING:
    from collections.abc import Iterator

COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
DEPLOYMENT_DOC = REPO_ROOT / "docs" / "getting-started" / "deployment.md"
WORKER_ENTRYPOINTS = (
    REPO_ROOT / "docker" / "production" / "app" / "start-worker",
    REPO_ROOT / "docker" / "local" / "app" / "start-worker",
)
# The project settings package holds a `tasks.py` of its own — the queue declaration itself.
TASK_MODULES = sorted(
    ".".join(path.relative_to(DAIV_DIR).with_suffix("").parts)
    for path in DAIV_DIR.rglob("tasks.py")
    if not path.is_relative_to(DAIV_DIR / "daiv")
)

DECLARED_QUEUES = set(settings.TASKS["default"]["QUEUES"])

# Every task allowed on the interactive queue, as ``module:name``. "Short" is not a property
# the code exposes, so this roster is the policy — and it matters in both directions: a short
# task drifting back to ``default`` regains the latency the split removed, and a long one
# joining ``interactive`` hands that latency to everything already there.
INTERACTIVE_TASKS = {
    "automation.titling.tasks:generate_title_task",
    "automation.titling.tasks:generate_batch_title_task",
    "notifications.tasks:deliver_notification_task",
    "sessions.tasks:classify_run_task",
}
TITLING_TASKS = {"automation.titling.tasks:generate_title_task", "automation.titling.tasks:generate_batch_title_task"}

QUEUE_ARGUMENT = re.compile(r"--queue-name \"\$\{1:-([^}]+)\}\"")
YAML_BLOCK = re.compile(r"```yaml\n(.*?)```", re.DOTALL)
WORKER_ENTRYPOINT = "start-worker"


def _iter_tasks() -> Iterator[tuple[str, Task]]:
    for module_path in TASK_MODULES:
        module = importlib.import_module(module_path)
        for name, value in vars(module).items():
            if isinstance(value, Task) and value.func.__module__ == module_path:
                yield f"{module_path}:{name}", value


ALL_TASKS = dict(_iter_tasks())


def queues_served(stack: str) -> set[str]:
    """The queues the worker services of one compose/stack document serve between them."""
    services = yaml.safe_load(stack)["services"].values()
    commands = [
        service["command"].split() for service in services if WORKER_ENTRYPOINT in (service.get("command") or "")
    ]
    assert commands, "no worker service in this stack"

    served: set[str] = set()
    for command in commands:
        entrypoint = next(index for index, part in enumerate(command) if part.endswith(WORKER_ENTRYPOINT))
        args = command[entrypoint + 1 :]
        # No argument is the image default, which is every queue.
        served |= set(args[0].split(",")) if args else DECLARED_QUEUES
    return served


def test_every_task_runs_on_a_queue_the_backend_declares():
    silent = set(TASK_MODULES) - {name.split(":")[0] for name in ALL_TASKS}
    assert not silent, f"no task discovered in {silent} — the tasks.py layout drifted"

    undeclared = {name: task.queue_name for name, task in ALL_TASKS.items() if task.queue_name not in DECLARED_QUEUES}
    assert not undeclared, f"queue not in TASKS['default']['QUEUES'] — enqueuing raises InvalidTask: {undeclared}"


def test_only_short_work_shares_the_queue_the_titler_runs_on():
    on_interactive = {name for name, task in ALL_TASKS.items() if task.queue_name == TASK_QUEUE_INTERACTIVE}

    assert on_interactive == INTERACTIVE_TASKS


def test_the_titler_outranks_its_queue_mates():
    titling = min(ALL_TASKS[name].priority for name in TITLING_TASKS)
    mates = [ALL_TASKS[name].priority for name in INTERACTIVE_TASKS - TITLING_TASKS]

    assert titling > max(mates)


def test_the_image_serves_every_queue_by_default():
    # The entrypoint ships inside the image, so a deployment that never edits its compose file
    # still drains everything from its one worker — slowly, but nothing is stranded.
    for entrypoint in WORKER_ENTRYPOINTS:
        argument = QUEUE_ARGUMENT.search(entrypoint.read_text())
        assert argument, f"{entrypoint} does not pass a queue list to db_worker"
        assert argument.group(1) == "*", "db_worker reads '*' as every queue; a list here goes stale"


def test_the_compose_workers_cover_every_declared_queue():
    assert queues_served(COMPOSE_FILE.read_text()) == DECLARED_QUEUES


def test_every_documented_stack_covers_every_declared_queue():
    # What a self-hosted deployment is copied from: a queue no documented service names is a
    # queue whose tasks pile up unclaimed there, however the repo's own compose file is set up.
    stacks = [block for block in YAML_BLOCK.findall(DEPLOYMENT_DOC.read_text()) if "services:" in block]

    assert len(stacks) == 2, f"{len(stacks)} stacks found in {DEPLOYMENT_DOC.name} — the deployment guide drifted"
    for stack in stacks:
        assert queues_served(stack) == DECLARED_QUEUES
