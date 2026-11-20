# Layering

## The layers

`.importlinter` owns the layering and `make lint-imports` (part of `make lint`) enforces it.
Each app may import only the apps below it, bottom to top:

1. `core`
2. `codebase` and `sandbox_envs`, which do not import each other
3. `accounts`
4. `skills`
5. `slash_commands`
6. `automation`
7. `mcp_connectors`
8. `sessions`
9. `memory` and `schedules`, which do not import each other
10. `notifications`
11. `chat`, `jobs`, `mcp_api`, `webhooks`, `configuration` and `dashboard`. The trigger apps
    (`chat`, `jobs`, `mcp_api`, `webhooks`) do not import each other, not even through a
    lower layer.

What each app holds is in the repo map in `../../AGENTS.md`. A page or helper that reads runs,
schedules or notifications belongs in `dashboard`, not `accounts`.

## Rules

1. **The contracts are binding.** A broken contract is fixed by moving the code to the layer
   that owns it, not by loosening the contract. _Prevents_: lower layers learning about the
   trigger apps, so that a change to chat or webhooks breaks the agent runtime.
2. **An exception is a design decision.** A new `ignore_imports` line is justified in the plan's
   Complexity Tracking and approved in review. The two current exceptions are
   `automation.agent.middlewares.memory -> memory.models` and the one-release task alias
   `sessions.tasks -> notifications.tasks`. Deleting an import that an exception covers removes
   its line in the same change. _Prevents_: exceptions accumulating until the layering exists only
   on paper.
3. **Every package is placed.** A new top-level package under `daiv/`, app or not, goes in
   `root_packages`, in the `forbidden_modules` of every contract below its layer (always
   `core`'s), and, unless it joins the top layer, in a contract of its own that forbids every
   app above it. `tests/unit_tests/daiv/test_import_hygiene.py` checks that each package is
   listed and placed, and that every contract forbids what the apps above it forbid.
   _Prevents_: a package outside every contract, which import-linter never judges.
4. **Agent code never branches on run mode.** The run executor builds one `Workspace` per run,
   `DiskWorkspace` or `SandboxWorkspace`, and every part whose behaviour differs asks it (the
   `Workspace` protocol in `daiv/automation/agent/workspace/base.py`). _Prevents_: a feature that
   works on disk runs and breaks on sandbox runs, or the reverse.
5. **Use the extension point.** Agent tools come from middlewares in
   `daiv/automation/agent/middlewares/`, registered in `graph.py`. Git-platform access goes
   through `codebase.clients`. HTML views are class-based views in `<app>/views.py`; JSON
   endpoints are a django-ninja `Router` in `<app>/api/views.py` (or `api/router.py`),
   registered in `daiv/daiv/api.py`. Filtered lists use a `django_filters.FilterSet` with
   `FilterView(strict=False)`, not hand-rolled `request.GET` parsing and `Paginator`.
   _Prevents_: a second mechanism for a job one already does, each with its own bugs.
