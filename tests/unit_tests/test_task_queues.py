"""Repository-wide guard on which queue each background task runs on.

Lives at the top of tests/unit_tests/ rather than in an app package: tasks ship from six
apps, the queues they name are declared in the settings, and the workers that serve those
queues are declared in the compose file and the image's entrypoint — a split that is only
real while all four agree.

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
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings

import yaml
from django_tasks.base import Task

from core.constants import TASK_QUEUE_DEFAULT, TASK_QUEUE_INTERACTIVE

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
DAIV_DIR = REPO_ROOT / "daiv"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
DEPLOYMENT_DOC = REPO_ROOT / "docs" / "getting-started" / "deployment.md"
WORKER_ENTRYPOINTS = (
    REPO_ROOT / "docker" / "production" / "app" / "start-worker",
    REPO_ROOT / "docker" / "local" / "app" / "start-worker",
)

# Read from the settings component rather than ``settings.TASKS``: the suite runs under
# ``daiv.settings.test``, whose own declaration is checked against this one below.
DECLARED_QUEUES = set(importlib.import_module("daiv.settings.components.tasks").TASKS["default"]["QUEUES"])

# Every task allowed on the interactive queue, as ``module:name``. Both directions matter — a
# short task that drifts back to ``default`` regains the latency this split removed, and a long
# one that joins ``interactive`` reintroduces it for everything already there.
INTERACTIVE_TASKS = {
    "automation.titling.tasks:generate_title_task",
    "automation.titling.tasks:generate_batch_title_task",
    "notifications.tasks:deliver_notification_task",
    "sessions.tasks:classify_run_task",
}
# The titler is what a reader watches for, so it outranks its queue mates.
TITLING_TASKS = {name for name in INTERACTIVE_TASKS if name.startswith("automation.titling")}

QUEUE_FALLBACK = re.compile(r"--queue-name \"\$\{1:-([^}]+)\}\"")
YAML_BLOCK = re.compile(r"```yaml\n(.*?)```", re.DOTALL)
WORKER_ENTRYPOINT = "start-worker"


def iter_tasks() -> Iterator[tuple[str, Task]]:
    """Yield ``("<module>:<name>", task)`` for every task defined under ``daiv/``."""
    for path in sorted(DAIV_DIR.rglob("tasks.py")):
        module_path = ".".join(path.relative_to(DAIV_DIR).with_suffix("").parts)
        module = importlib.import_module(module_path)
        for name, value in vars(module).items():
            if isinstance(value, Task) and value.func.__module__ == module_path:
                yield f"{module_path}:{name}", value


def test_every_task_runs_on_a_queue_the_backend_declares():
    tasks = dict(iter_tasks())

    assert len(tasks) >= 8, f"only {len(tasks)} tasks discovered — the tasks.py layout drifted"
    undeclared = {name: task.queue_name for name, task in tasks.items() if task.queue_name not in DECLARED_QUEUES}
    assert not undeclared, f"queue not in TASKS['default']['QUEUES'] — enqueuing raises InvalidTask: {undeclared}"


def test_the_test_settings_declare_the_queues_production_declares():
    # A queue declared here but not in production makes a task import cleanly in the suite and
    # raise at import time in production, where nothing catches it.
    assert set(settings.TASKS["default"]["QUEUES"]) == DECLARED_QUEUES


def test_only_short_work_shares_the_queue_the_titler_runs_on():
    on_interactive = {name for name, task in iter_tasks() if task.queue_name == TASK_QUEUE_INTERACTIVE}

    assert on_interactive == INTERACTIVE_TASKS


def test_the_titler_never_waits_behind_an_agent_run():
    tasks = dict(iter_tasks())

    assert tasks["jobs.tasks:run_job_task"].queue_name == TASK_QUEUE_DEFAULT
    for name in TITLING_TASKS:
        assert tasks[name].queue_name == TASK_QUEUE_INTERACTIVE


def test_the_titler_outranks_its_queue_mates():
    tasks = dict(iter_tasks())
    titling = min(tasks[name].priority for name in TITLING_TASKS)
    others = [tasks[name].priority for name in INTERACTIVE_TASKS - TITLING_TASKS]

    assert others, "no queue mates left to outrank — the priority is then pointless, not satisfied"
    assert titling > max(others)


def test_the_image_serves_every_declared_queue_by_default():
    # The entrypoint ships inside the image, so a deployment that never edits its compose file
    # still drains both queues from its one worker — slowly, but nothing is stranded.
    for entrypoint in WORKER_ENTRYPOINTS:
        fallback = QUEUE_FALLBACK.search(entrypoint.read_text())
        assert fallback, f"{entrypoint} does not pass a queue list to db_worker"
        assert set(fallback.group(1).split(",")) == DECLARED_QUEUES


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


def test_the_compose_workers_cover_every_declared_queue():
    assert queues_served(COMPOSE_FILE.read_text()) == DECLARED_QUEUES


def test_every_documented_stack_covers_every_declared_queue():
    # What a self-hosted deployment is copied from: a queue no documented service names is a
    # queue whose tasks pile up unclaimed there, however the repo's own compose file is set up.
    stacks = [block for block in YAML_BLOCK.findall(DEPLOYMENT_DOC.read_text()) if "services:" in block]

    assert len(stacks) == 2, f"{len(stacks)} stacks found in {DEPLOYMENT_DOC.name} — the deployment guide drifted"
    for stack in stacks:
        assert queues_served(stack) == DECLARED_QUEUES
