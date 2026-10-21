import configparser
import os
import subprocess  # noqa: S404
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[3] / "daiv"

# Roots of the agent stack (~160MB RSS). django.setup() must not load any of them:
# every process (uvicorn app, db_worker, crontask scheduler) pays the cost otherwise.
HEAVY_ROOTS = ("langchain", "langgraph", "deepagents", "langsmith", "anthropic", "openai", "numpy")

# The URLconf still loads langgraph and langchain_core (hydration, the chat streamer); it must not add
# deepagents or a provider SDK (~+90MB), which only a run or a model-catalog fetch needs.
PROVIDER_SDK_ROOTS = ("deepagents", "anthropic", "openai", "google", "langchain_anthropic", "langchain_openai")

PROBE = """
import importlib
import os
import sys
import django

django.setup()
for module in filter(None, os.environ["IMPORT_AFTER_SETUP"].split(",")):
    importlib.import_module(module)
heavy = tuple(os.environ["HEAVY_ROOTS"].split(","))
roots = {m.split(".")[0] for m in sys.modules}
offenders = sorted(r for r in roots if r.startswith(heavy))
print("OFFENDERS:" + ",".join(offenders))
"""


def _heavy_roots_loaded(*modules: str, roots: tuple[str, ...] = HEAVY_ROOTS) -> list[str]:
    """``roots`` found in ``sys.modules`` of a fresh process after ``django.setup()`` and importing ``modules``."""
    env = os.environ | {
        "DJANGO_SETTINGS_MODULE": "daiv.settings.test",
        "NINJA_SKIP_REGISTRY": "true",
        "DB_PASSWORD": os.environ.get("DB_PASSWORD", "unused"),
        "HEAVY_ROOTS": ",".join(roots),
        "IMPORT_AFTER_SETUP": ",".join(modules),
    }
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", PROBE], capture_output=True, text=True, cwd=SOURCE_ROOT, env=env, timeout=120
    )
    assert result.returncode == 0, f"probe failed:\n{result.stderr}"
    offenders_line = next(line for line in result.stdout.splitlines() if line.startswith("OFFENDERS:"))
    return [o for o in offenders_line.removeprefix("OFFENDERS:").split(",") if o]


def test_django_setup_does_not_load_agent_stack():
    """Guards the lazy-import seams (run_job_task, webhooks/memory/titling tasks, repo_config):
    a new module-level import of the agent stack from any eagerly-imported module (models, apps,
    signals, api views) regresses every process back to ~+160MB RSS and shows up here as a
    non-empty offender list.
    """
    offenders = _heavy_roots_loaded()
    assert not offenders, (
        f"django.setup() eagerly imports the agent stack via: {offenders}. "
        "Defer the offending import into the function that uses it (see sessions/executor/tasks.py)."
    )


def test_model_spec_validation_does_not_load_agent_stack():
    """Forms, the model picker and run admission import these at URL-load time; building the chat
    model, which needs the provider SDKs, stays in ``automation.agent.base``.
    """
    offenders = _heavy_roots_loaded("automation.agent.model_spec", "automation.agent.validators")
    assert not offenders, f"validating a model spec imports the agent stack via: {offenders}"


def test_urlconf_does_not_load_provider_sdks():
    offenders = _heavy_roots_loaded("daiv.urls", roots=PROVIDER_SDK_ROOTS)
    assert not offenders, (
        f"loading the URLconf imports {offenders}. Defer the import into the function that needs it "
        "(see automation/picker/model_catalog/registry.py and skills/services.py)."
    )


def _import_linter_config() -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.read(SOURCE_ROOT.parent / ".importlinter")
    return config


def test_import_contracts_cover_every_top_level_package():
    """A package missing from ``root_packages`` is invisible to ``make lint-imports``, and one that no
    contract forbids can be imported from below unchecked; ``core`` sits below every app, so its
    contract must forbid them all.
    """
    config = _import_linter_config()
    root_packages = set(config["importlinter"]["root_packages"].split())
    core_forbidden = set(config["importlinter:contract:core"]["forbidden_modules"].split())
    packages = {path.name for path in SOURCE_ROOT.iterdir() if (path / "__init__.py").is_file()}

    assert root_packages == packages - {"activity"}, (
        "`.importlinter` root_packages must list every top-level package under daiv/; "
        "`activity` is left out because it only holds migration history."
    )
    assert core_forbidden == root_packages - {"core", "daiv"}


def test_import_contracts_place_every_app_in_the_layering():
    """Every app below the top layer has its own forbidden contract, and each contract also forbids
    whatever the apps above it forbid: an app added to one layer's list cannot be skipped in a lower one.
    """
    config = _import_linter_config()
    root_packages = set(config["importlinter"]["root_packages"].split())
    forbidden = {
        section["source_modules"].strip(): set(section["forbidden_modules"].split())
        for name, section in config.items()
        if name.startswith("importlinter:contract:") and section.get("type") == "forbidden"
    }
    top_layer = set.intersection(*forbidden.values())

    unplaced = root_packages - forbidden.keys() - top_layer - {"daiv"}
    assert not unplaced, (
        f"{sorted(unplaced)} has no forbidden contract: add one forbidding every app above it, or forbid it "
        "in every contract if it belongs in the top layer."
    )
    for source, above in forbidden.items():
        for app in above & forbidden.keys():
            missing = forbidden[app] - above - {source}
            assert not missing, f"`{source}` forbids `{app}` but not {sorted(missing)}, which sit above `{app}`"
