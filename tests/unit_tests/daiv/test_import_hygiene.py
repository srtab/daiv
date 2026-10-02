import configparser
import os
import subprocess  # noqa: S404
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[3] / "daiv"

# Roots of the agent stack (~160MB RSS). django.setup() must not load any of them:
# every process (uvicorn app, db_worker, crontask scheduler) pays the cost otherwise.
HEAVY_ROOTS = ("langchain", "langgraph", "deepagents", "langsmith", "anthropic", "openai", "numpy")

PROBE = """
import os
import sys
import django

django.setup()
heavy = tuple(os.environ["HEAVY_ROOTS"].split(","))
roots = {m.split(".")[0] for m in sys.modules}
offenders = sorted(r for r in roots if r.startswith(heavy))
print("OFFENDERS:" + ",".join(offenders))
"""


def test_django_setup_does_not_load_agent_stack():
    """Guards the lazy-import seams (jobs/webhooks/memory/titling tasks, repo_config,
    automation.agent.__getattr__): a new module-level import of the agent stack from any
    eagerly-imported module (models, apps, signals, api views) regresses every process
    back to ~+160MB RSS and shows up here as a non-empty offender list.
    """
    env = os.environ | {
        "DJANGO_SETTINGS_MODULE": "daiv.settings.test",
        "NINJA_SKIP_REGISTRY": "true",
        "DB_PASSWORD": os.environ.get("DB_PASSWORD", "unused"),
        "HEAVY_ROOTS": ",".join(HEAVY_ROOTS),
    }
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", PROBE], capture_output=True, text=True, cwd=SOURCE_ROOT, env=env, timeout=120
    )
    assert result.returncode == 0, f"probe failed:\n{result.stderr}"
    offenders_line = next(line for line in result.stdout.splitlines() if line.startswith("OFFENDERS:"))
    offenders = [o for o in offenders_line.removeprefix("OFFENDERS:").split(",") if o]
    assert not offenders, (
        f"django.setup() eagerly imports the agent stack via: {offenders}. "
        "Defer the offending import into the function that uses it (see jobs/tasks.py)."
    )


def test_import_contracts_cover_every_top_level_package():
    """A package missing from ``root_packages`` is invisible to ``make lint-imports``, and one that no
    contract forbids can be imported from below unchecked; ``core`` sits below every app, so its
    contract must forbid them all.
    """
    config = configparser.ConfigParser()
    config.read(SOURCE_ROOT.parent / ".importlinter")
    root_packages = set(config["importlinter"]["root_packages"].split())
    core_forbidden = set(config["importlinter:contract:core"]["forbidden_modules"].split())
    packages = {path.name for path in SOURCE_ROOT.iterdir() if (path / "__init__.py").is_file()}

    assert root_packages == packages - {"activity"}, (
        "`.importlinter` root_packages must list every top-level package under daiv/; "
        "`activity` is left out because it only holds migration history."
    )
    assert core_forbidden == root_packages - {"core", "daiv"}
