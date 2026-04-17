# Agent Run Retry & Start-a-Run Page — Design

**Date:** 2026-04-17
**Status:** Approved (pending spec review)

## Problem

A user cannot re-execute a past agent run with the same parameters. Today, every submission path (HTTP API, MCP tool, scheduled jobs, Git webhooks) creates an `Activity` record, but there is no way from the UI to re-trigger a run from a terminal `Activity`. The "trigger a new agent run from the UI" capability is also missing and sits in the product backlog.

## Goals

1. Add a **Start a run** page in the web UI that submits agent runs directly (no API-key round-trip).
2. Add **Retry** from an `Activity` detail page: opens the same form pre-filled with the original parameters, editable before re-submission.
3. Persist enough state on `Activity` that any UI/API-originated run is fully reconstructible.
4. Share field declarations, widgets, and template partials between the new form and the existing `ScheduledJobCreateForm` to keep them in lockstep.
5. Introduce **"Run"** as the user-facing term for a single agent execution (replacing "Job" in the submission concept); the existing Activity listing term is unchanged.

## Non-goals

- No retry for webhook-originated activities (their context — issue/MR IID, mention comment — is not carried by `run_job_task`).
- No rename of the `Activity` model, `/activity/` URL, or existing API endpoints (`/api/jobs/`). This design only renames the submission concept in new UI surfaces.
- No bulk retry, no scheduling from the retry form, no feature flag / staged rollout.

## Design

### Model changes

**`daiv/activity/models.py`**

- Add `use_max = BooleanField(default=False, verbose_name=_("use max model"))` to `Activity`.
- Add `TriggerType.UI_JOB = "ui_job", _("UI Run")` — used by both the new start-a-run page and retries.
- Update user-facing labels on existing trigger choices to "Run"-flavored: `API_JOB → _("API Run")`, `MCP_JOB → _("MCP Run")`, `SCHEDULE → _("Scheduled Run")`. Webhook labels unchanged.
- Add an `is_retryable` property:

  ```python
  @property
  def is_retryable(self) -> bool:
      return (
          self.status in ActivityStatus.terminal()
          and self.trigger_type not in {TriggerType.ISSUE_WEBHOOK, TriggerType.MR_WEBHOOK}
      )
  ```

Stored enum values (`api_job`, `mcp_job`, `schedule`) are not renamed — avoiding a data migration with no functional gain.

**Migration:** a single file performing `AddField(use_max, default=False)` and `AlterField(trigger_type.choices)` with the new `UI_JOB` choice. Existing rows keep `use_max=False`, which is accurate for all historical runs (none were submitted with `use_max=True` visible to `Activity` before this change).

### Service changes

**`daiv/activity/services.py::acreate_activity`** gains a `use_max: bool = False` keyword argument, persisted on the new row.

Six call-sites thread `use_max` through:

| Call site | Source of `use_max` |
|---|---|
| `daiv/jobs/api/views.py` (submit endpoint) | `payload.use_max` |
| `daiv/mcp_server/server.py` (MCP submit tool) | tool argument |
| `daiv/schedules/tasks.py` (scheduled run) | `scheduled_job.use_max` |
| `daiv/codebase/clients/gitlab/api/callbacks.py` (3 webhook paths) | `daiv-max` label present on the event |
| `daiv/codebase/clients/github/api/callbacks.py` (3 webhook paths) | `daiv-max` label present on the event |

The label-to-bool derivation in webhook callbacks is the only new logic among the six.

### Forms (DRY with ScheduledJob)

**New `AgentRunFieldsMixin`** in `daiv/activity/forms.py`:

```python
class AgentRunFieldsMixin(forms.Form):
    prompt = forms.CharField(widget=forms.Textarea, required=True)
    repo_id = forms.CharField(required=True)
    ref = forms.CharField(required=False)
    use_max = forms.BooleanField(required=False, initial=False)
```

The mixin owns shared field declarations, widgets, validators, and `help_text`. It does not know about models.

**Refactor `ScheduledJobCreateForm`** to consume the mixin:

```python
class ScheduledJobCreateForm(AgentRunFieldsMixin, forms.ModelForm):
    class Meta:
        model = ScheduledJob
        fields = [
            "name", "prompt", "repo_id", "ref", "use_max",
            "frequency", "cron_expression", "time", "notify_on",
        ]
    # existing clean() / save() unchanged
```

`ScheduledJobUpdateForm` is untouched (inherits via the parent).

**New `AgentRunCreateForm(AgentRunFieldsMixin, forms.Form)`** — plain form (no model), used by the new view. Provides:

```python
def submit(self, user) -> Activity:
    task = run_job_task.aenqueue(
        repo_id=self.cleaned_data["repo_id"],
        prompt=self.cleaned_data["prompt"],
        ref=self.cleaned_data["ref"] or None,
        use_max=self.cleaned_data["use_max"],
    )
    return acreate_activity(
        trigger_type=TriggerType.UI_JOB,
        task_result_id=task.id,
        user=user,
        repo_id=self.cleaned_data["repo_id"],
        ref=self.cleaned_data["ref"],
        prompt=self.cleaned_data["prompt"],
        use_max=self.cleaned_data["use_max"],
    )
```

**Template partial** `daiv/activity/templates/activity/_agent_run_fields.html` renders the four shared fields. Included by both `schedules/schedule_form.html` and the new `activity/agent_run_form.html` via `{% include %}`.

### Views & URLs

**New CBV `AgentRunCreateView`** (`daiv/activity/views.py`) — handles the blank form, retry pre-fill, and submission:

- `GET /runs/new/` → blank `AgentRunCreateForm`.
- `GET /runs/new/?from=<activity_uuid>` → pre-filled from the source Activity:
  - Source resolved via `Activity.objects.by_owner(request.user).get(pk=...)`.
  - If `not source.is_retryable`, respond 404.
  - Initial form data: `{prompt, repo_id, ref, use_max}` from the source.
  - Context includes `source_activity` for the "Retried from …" banner.
- `POST /runs/new/` → validates, calls `form.submit(user=request.user)`, redirects to `activity_detail` for the new activity.

**Rate limiting** shares the `jobs_throttle_rate` budget (default `20/hour`). New helper `daiv/jobs/throttle.py::check_jobs_throttle(user) -> bool` (returns `True` if allowed, `False` if throttled), implemented with `django.core.cache` keyed per-user. The existing ninja throttle class delegates to this helper; the new `AgentRunCreateView.form_valid` calls it before `form.submit()` and, when throttled, re-renders the bound form with a non-field error ("Rate limit exceeded; try again later").

**URL wiring:** top-level `/runs/` prefix. Add to the root URLconf:

```python
path("runs/", include("activity.urls_runs", namespace="runs")),
```

`activity/urls_runs.py` registers `path("new/", AgentRunCreateView.as_view(), name="agent_run_new")` (full URL name: `runs:agent_run_new`).

### Templates & UX

**New `activity/templates/activity/agent_run_form.html`** — extends the base layout. Page title: "Start a run" or "Retry run" based on `source_activity` presence. Body includes the shared partial plus a submit button. In retry mode, a small banner: "Retried from [source activity link]".

**Existing `schedules/schedule_form.html`** refactored to include the shared partial for the four common fields; schedule-specific fields (`name`, `frequency`, `cron_expression`, `time`, `notify_on`, `is_enabled`) remain rendered inline.

**`activity/templates/activity/activity_detail.html`** — add a Retry action near the status badges (~line 25):

```django
{% if activity.is_retryable %}
  <a href="{% url 'runs:agent_run_new' %}?from={{ activity.id }}" class="...">Retry</a>
{% endif %}
```

No view changes for retry-button gating — `is_retryable` is a model property.

**Nav** — add a "Start a run" link in the sidebar alongside "Activity" and "Schedules".

**Post-submit UX** — redirect to the new activity's detail page; the existing activity-stream SSE picks up the running state so the user immediately sees progress.

### Permissions

- `AgentRunCreateView` requires login (`LoginRequiredMixin`). Any authenticated user can start a run — matches the existing API endpoint policy (no admin gate).
- Retry pre-fill scoped through `Activity.objects.by_owner(request.user)`; regular users can only retry runs they can see, admins can retry any.

### Admin

- `Activity` admin: add `use_max` to the appropriate `list_display` / `readonly_fields` where context fields live.
- `ScheduledJob` admin unchanged (already exposes `use_max`).

### Edge cases

- **Repo deleted/renamed between original run and retry:** `run_job_task` already raises on missing repo; the retry activity ends in `FAILED` with a clear error message.
- **`jobs_throttle_rate` empty/misconfigured:** the shared helper mirrors existing API behavior and skips throttling.
- **Concurrent retries of the same source:** each creates an independent new `Activity`; no deduplication (intentional — user may want to re-run multiple times).

## Testing

Scoped strictly to custom project logic. Third-party framework behavior (Django field validators, CBV redirect mechanics, default ORM scoping, ninja throttle internals) is not tested.

- **`Activity.is_retryable`** — truth table over `status × trigger_type`: terminal × {API_JOB, MCP_JOB, SCHEDULE, UI_JOB} are retryable; non-terminal or webhook are not.
- **`acreate_activity(use_max=True)`** — flag is persisted on the created row.
- **`AgentRunCreateForm.submit(user)`** — mocks `run_job_task.aenqueue`; asserts the exact `(repo_id, prompt, ref or None, use_max)` tuple passed and that the created `Activity` has `trigger_type=UI_JOB`, correct `use_max`, and the given `user`.
- **`AgentRunCreateView` gating** — all four branches we added:
  - `?from=<non-terminal>` → 404
  - `?from=<webhook-triggered>` → 404
  - `?from=<other-user's-activity>` (non-admin) → 404
  - `?from=<retryable>` → form initial matches source `prompt/repo_id/ref/use_max`
- **`check_jobs_throttle` helper** — returns `True` under the limit, `False` once exceeded; boundary cases at empty/invalid rate strings (matches existing permissive API behavior).
- **Backfill call-sites** — one test per site that `use_max` is threaded through: API view (`payload.use_max`), MCP tool argument, scheduled task (`scheduled_job.use_max`), GitLab webhook (label→flag derivation), GitHub webhook (label→flag derivation).

## Rollout

- Single PR; no feature flag.
- Changelog entry (user-facing): "Added 'Start a run' page and one-click retry of past agent runs from the activity detail page."
- Pre-merge: `make test`, `make lint-fix`, `make lint-typing` all green.

## Open items

None. All design decisions confirmed during brainstorming.
