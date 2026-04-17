# Agent Run Retry & Start-a-Run Page — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a web UI to start a new agent run and to retry any terminal, non-webhook `Activity` with its original parameters editable before resubmit.

**Architecture:** Extend `Activity` with a `use_max` field and a new `TriggerType.UI_JOB`. Extract the four shared fields (`prompt`, `repo_id`, `ref`, `use_max`) into a reusable form mixin + template partial used by both the existing `ScheduledJobCreateForm` and a new `AgentRunCreateForm`. Add a single CBV `AgentRunCreateView` at `/runs/new/` that serves the blank form and — when `?from=<activity_uuid>` is provided and the source is retryable — a pre-filled form; submission enqueues `run_job_task` and creates a new `Activity(trigger_type=UI_JOB)`. UI-initiated submissions share the existing `jobs_throttle_rate` budget via a new helper.

**Tech Stack:** Django 5 (class-based views, ModelForms, LoginRequiredMixin), django-tasks (`run_job_task.aenqueue`), django-ninja (existing API throttle), Alpine.js + Tailwind templates, pytest + pytest-django.

**Spec:** `docs/superpowers/specs/2026-04-17-agent-run-retry-design.md`

---

## File Map

**Create:**
- `daiv/activity/forms.py` — `AgentRunFieldsMixin`, `AgentRunCreateForm`.
- `daiv/activity/urls_runs.py` — URL patterns for the `runs/` prefix.
- `daiv/activity/templates/activity/_agent_run_fields.html` — shared form-fields partial.
- `daiv/activity/templates/activity/agent_run_form.html` — full page template.
- `daiv/jobs/throttle.py` — `check_jobs_throttle(user)` helper.
- `daiv/activity/migrations/0006_activity_use_max_uijob.py` — model migration.
- `tests/unit_tests/activity/test_models_retry.py`
- `tests/unit_tests/activity/test_forms.py`
- `tests/unit_tests/activity/test_views_runs.py`
- `tests/unit_tests/activity/test_services_use_max.py`
- `tests/unit_tests/jobs/test_throttle.py`
- `tests/unit_tests/codebase/clients/gitlab/test_webhook_use_max.py` — label-to-flag derivation only.
- `tests/unit_tests/codebase/clients/github/test_webhook_use_max.py` — label-to-flag derivation only.

**Modify:**
- `daiv/activity/models.py` — add `use_max`, `TriggerType.UI_JOB`, label updates, `is_retryable`.
- `daiv/activity/services.py` — accept `use_max` kwarg in `create_activity`/`acreate_activity`.
- `daiv/activity/urls.py` — (no change) — new URL conf lives in `urls_runs.py`.
- `daiv/activity/templates/activity/activity_detail.html` — retry button.
- `daiv/schedules/forms.py` — refactor `ScheduledJobCreateForm` to use the mixin.
- `daiv/schedules/templates/schedules/schedule_form.html` — use shared partial.
- `daiv/schedules/tasks.py` — pass `use_max=schedule.use_max` into `create_activity`.
- `daiv/jobs/api/views.py` — pass `use_max=payload.use_max`; delegate throttle check to new helper.
- `daiv/mcp_server/server.py` — pass `use_max=use_max`.
- `daiv/codebase/clients/gitlab/api/models.py` — `has_max_label()` on `Issue` & `MergeRequest`.
- `daiv/codebase/clients/gitlab/api/callbacks.py` — thread `use_max` into 3 `acreate_activity` calls.
- `daiv/codebase/clients/github/api/models.py` — `has_max_label()` on `Issue` & MR/PR model.
- `daiv/codebase/clients/github/api/callbacks.py` — thread `use_max` into 3 `acreate_activity` calls.
- `daiv/daiv/urls.py` — mount `/runs/` prefix (find the current root URLconf and add it alongside `/activity/`).
- Sidebar template that currently links to `activity_list` / `schedule_list` — add `runs:agent_run_new` link (locate via grep).

**Throughout:** all new Python code must pass `make lint-fix` + `make lint-typing`.

---

## Task 1: Model — `use_max`, `TriggerType.UI_JOB`, `is_retryable`, migration

**Files:**
- Modify: `daiv/activity/models.py`
- Create: `daiv/activity/migrations/0006_activity_use_max_uijob.py`
- Test: `tests/unit_tests/activity/test_models_retry.py`

- [ ] **Step 1.1: Write the failing test — `is_retryable` truth table**

```python
# tests/unit_tests/activity/test_models_retry.py
import pytest
from activity.models import Activity, ActivityStatus, TriggerType


@pytest.mark.django_db
class TestIsRetryable:
    def _make(self, status: str, trigger: str) -> Activity:
        return Activity(status=status, trigger_type=trigger, repo_id="acme/repo")

    @pytest.mark.parametrize(
        "trigger", [TriggerType.API_JOB, TriggerType.MCP_JOB, TriggerType.SCHEDULE, TriggerType.UI_JOB]
    )
    @pytest.mark.parametrize("status", [ActivityStatus.SUCCESSFUL, ActivityStatus.FAILED])
    def test_terminal_non_webhook_is_retryable(self, status, trigger):
        assert self._make(status, trigger).is_retryable is True

    @pytest.mark.parametrize("status", [ActivityStatus.READY, ActivityStatus.RUNNING])
    def test_non_terminal_not_retryable(self, status):
        assert self._make(status, TriggerType.API_JOB).is_retryable is False

    @pytest.mark.parametrize("trigger", [TriggerType.ISSUE_WEBHOOK, TriggerType.MR_WEBHOOK])
    @pytest.mark.parametrize("status", [ActivityStatus.SUCCESSFUL, ActivityStatus.FAILED])
    def test_webhook_not_retryable_even_when_terminal(self, status, trigger):
        assert self._make(status, trigger).is_retryable is False
```

- [ ] **Step 1.2: Run test — verify it fails**

```
uv run pytest tests/unit_tests/activity/test_models_retry.py -v
```

Expected: fails with `AttributeError: 'Activity' object has no attribute 'is_retryable'` AND `AttributeError: UI_JOB`.

- [ ] **Step 1.3: Edit the model**

Apply these three edits to `daiv/activity/models.py`:

1) Extend `TriggerType` (~line 31) — add `UI_JOB`, update labels:

```python
class TriggerType(models.TextChoices):
    API_JOB = "api_job", _("API Run")
    MCP_JOB = "mcp_job", _("MCP Run")
    SCHEDULE = "schedule", _("Scheduled Run")
    UI_JOB = "ui_job", _("UI Run")
    ISSUE_WEBHOOK = "issue_webhook", _("Issue Webhook")
    MR_WEBHOOK = "mr_webhook", _("MR/PR Webhook")
```

2) Add `use_max` field (below `prompt`, ~line 94):

```python
use_max = models.BooleanField(_("use max model"), default=False)
```

3) Add `is_retryable` property on `Activity` (near `__str__`, ~line 147):

```python
@property
def is_retryable(self) -> bool:
    return self.status in ActivityStatus.terminal() and self.trigger_type not in {
        TriggerType.ISSUE_WEBHOOK,
        TriggerType.MR_WEBHOOK,
    }
```

- [ ] **Step 1.4: Create the migration**

```
uv run python daiv/manage.py makemigrations activity --name activity_use_max_uijob
```

The generated file will add the `use_max` column and alter the `trigger_type` choices. Rename the resulting file to `0006_activity_use_max_uijob.py` if the auto-generated number differs.

- [ ] **Step 1.5: Run tests — verify they pass**

```
uv run pytest tests/unit_tests/activity/test_models_retry.py -v
```

Expected: all tests PASS. Also:

```
uv run python daiv/manage.py migrate --settings=daiv.settings.test --dry-run
```

Expected: shows the new migration will be applied cleanly.

- [ ] **Step 1.6: Commit**

```bash
git add daiv/activity/models.py daiv/activity/migrations/0006_*.py tests/unit_tests/activity/test_models_retry.py
git commit -m "feat(activity): Add use_max field, UI_JOB trigger, and is_retryable"
```

---

## Task 2: Service — propagate `use_max` through `(a)create_activity`

**Files:**
- Modify: `daiv/activity/services.py`
- Test: `tests/unit_tests/activity/test_services_use_max.py`

- [ ] **Step 2.1: Write the failing test**

```python
# tests/unit_tests/activity/test_services_use_max.py
import pytest
from activity.models import Activity, TriggerType
from activity.services import acreate_activity, create_activity


@pytest.mark.django_db
def test_create_activity_persists_use_max_true(task_result_id):
    activity = create_activity(
        trigger_type=TriggerType.UI_JOB, task_result_id=task_result_id, repo_id="acme/repo", use_max=True
    )
    assert Activity.objects.get(pk=activity.pk).use_max is True


@pytest.mark.django_db
def test_create_activity_defaults_use_max_false(task_result_id):
    activity = create_activity(trigger_type=TriggerType.UI_JOB, task_result_id=task_result_id, repo_id="acme/repo")
    assert activity.use_max is False


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_acreate_activity_persists_use_max(task_result_id):
    activity = await acreate_activity(
        trigger_type=TriggerType.UI_JOB, task_result_id=task_result_id, repo_id="acme/repo", use_max=True
    )
    assert activity.use_max is True
```

`task_result_id` fixture: reuse whatever factory is used elsewhere. If none exists, create a lightweight one in the test file:

```python
import uuid
from django_tasks_db.models import DBTaskResult


@pytest.fixture
def task_result_id(db):
    result = DBTaskResult.objects.create(task_path="jobs.tasks.run_job_task", status="READY")
    return result.id
```

- [ ] **Step 2.2: Run test — verify it fails**

```
uv run pytest tests/unit_tests/activity/test_services_use_max.py -v
```

Expected: fails with `TypeError: ... got an unexpected keyword argument 'use_max'`.

- [ ] **Step 2.3: Edit `daiv/activity/services.py`**

Add `use_max: bool = False` to both `create_activity` and `acreate_activity` signatures, and pass it through to `.create(...)` / `.acreate(...)`. The rest of the body is unchanged.

```python
def create_activity(
    *,
    trigger_type: str,
    task_result_id: uuid.UUID,
    repo_id: str,
    ref: str = "",
    prompt: str = "",
    use_max: bool = False,  # NEW
    issue_iid: int | None = None,
    merge_request_iid: int | None = None,
    mention_comment_id: str = "",
    scheduled_job: ScheduledJob | None = None,
    user: User | None = None,
    external_username: str = "",
) -> Activity:
    return Activity.objects.create(
        trigger_type=trigger_type,
        task_result_id=task_result_id,
        repo_id=repo_id,
        ref=ref,
        prompt=prompt,
        use_max=use_max,  # NEW
        issue_iid=issue_iid,
        merge_request_iid=merge_request_iid,
        mention_comment_id=mention_comment_id,
        scheduled_job=scheduled_job,
        user=user,
        external_username=external_username,
    )
```

Apply the identical change to `acreate_activity` (kwarg + `.acreate(...)` body).

- [ ] **Step 2.4: Run test — verify it passes**

```
uv run pytest tests/unit_tests/activity/test_services_use_max.py -v
```

Expected: 3 PASSED.

- [ ] **Step 2.5: Commit**

```bash
git add daiv/activity/services.py tests/unit_tests/activity/test_services_use_max.py
git commit -m "feat(activity): Accept use_max in (a)create_activity"
```

---

## Task 3: Backfill — API submit endpoint

**Files:**
- Modify: `daiv/jobs/api/views.py:49-56`

- [ ] **Step 3.1: Edit the call site**

Change the `acreate_activity(...)` call in `submit_job` (at line 49) to include `use_max=payload.use_max`:

```python
await acreate_activity(
    trigger_type=TriggerType.API_JOB,
    task_result_id=result.id,
    repo_id=payload.repo_id,
    ref=payload.ref or "",
    prompt=payload.prompt,
    use_max=payload.use_max,  # NEW
    user=request.auth,
)
```

- [ ] **Step 3.2: Add a regression test**

Extend the existing jobs-API test file (find with `grep -rln "submit_job\|JobSubmitRequest" tests/unit_tests/jobs/`) with:

```python
@pytest.mark.asyncio
@pytest.mark.django_db
async def test_submit_job_persists_use_max(admin_client):  # from tests/unit_tests/conftest.py
    with mock.patch("jobs.api.views.run_job_task.aenqueue") as m:
        m.return_value.id = uuid.uuid4()
        resp = await admin_client.post(
            "/api/jobs", json={"repo_id": "acme/repo", "prompt": "do the thing", "use_max": True}
        )
    assert resp.status_code == 202
    activity = await Activity.objects.aget(task_result_id=m.return_value.id)
    assert activity.use_max is True
```

Match the surrounding test module's existing conventions (e.g. the `/api/jobs` tests may use an API-key-authenticated client rather than `admin_client` — check first and adapt). Do not invent new fixtures.

- [ ] **Step 3.3: Run tests**

```
uv run pytest tests/unit_tests/jobs/ -v
```

Expected: new test PASSES, existing tests unaffected.

- [ ] **Step 3.4: Commit**

```bash
git add daiv/jobs/api/views.py tests/unit_tests/jobs/
git commit -m "feat(jobs): Persist use_max on API-submitted activities"
```

---

## Task 4: Backfill — MCP submit tool

**Files:**
- Modify: `daiv/mcp_server/server.py:108-115`

- [ ] **Step 4.1: Edit the call site**

```python
await acreate_activity(
    trigger_type=TriggerType.MCP_JOB,
    task_result_id=result.id,
    repo_id=repo_id,
    ref=ref or "",
    prompt=prompt,
    use_max=use_max,  # NEW
    user=mcp_user,
)
```

- [ ] **Step 4.2: Add/extend regression test**

Locate MCP submit tests (`grep -rln "submit_job" tests/unit_tests/mcp_server/`). Add:

```python
@pytest.mark.asyncio
@pytest.mark.django_db
async def test_mcp_submit_persists_use_max(monkeypatch):
    fake_id = uuid.uuid4()

    async def fake_enqueue(**kwargs):
        return types.SimpleNamespace(id=fake_id)

    monkeypatch.setattr("mcp_server.server.run_job_task.aenqueue", fake_enqueue)

    await submit_job(repo_id="acme/repo", prompt="hi", ref=None, use_max=True, wait=False)

    activity = await Activity.objects.aget(task_result_id=fake_id)
    assert activity.use_max is True
```

Adapt import paths to the existing test file's conventions.

- [ ] **Step 4.3: Run tests**

```
uv run pytest tests/unit_tests/mcp_server/ -v
```

Expected: PASS.

- [ ] **Step 4.4: Commit**

```bash
git add daiv/mcp_server/server.py tests/unit_tests/mcp_server/
git commit -m "feat(mcp): Persist use_max on MCP-submitted activities"
```

---

## Task 5: Backfill — scheduled jobs

**Files:**
- Modify: `daiv/schedules/tasks.py:53-61`

- [ ] **Step 5.1: Edit the call site**

Inside `dispatch_scheduled_jobs_cron_task`, update the `create_activity(...)` call:

```python
create_activity(
    trigger_type=TriggerType.SCHEDULE,
    task_result_id=result.id,
    repo_id=schedule.repo_id,
    ref=ref or "",
    prompt=schedule.prompt,
    use_max=schedule.use_max,  # NEW
    scheduled_job=schedule,
    user=schedule.user,
)
```

- [ ] **Step 5.2: Add a regression test**

Locate scheduler tests (`grep -rln "dispatch_scheduled_jobs_cron_task" tests/unit_tests/schedules/`). Add a test that sets `ScheduledJob.use_max=True`, dispatches, and asserts the resulting `Activity.use_max is True`. Follow the existing test file's fixture patterns.

- [ ] **Step 5.3: Run tests**

```
uv run pytest tests/unit_tests/schedules/ -v
```

- [ ] **Step 5.4: Commit**

```bash
git add daiv/schedules/tasks.py tests/unit_tests/schedules/
git commit -m "feat(schedules): Persist use_max on scheduled-run activities"
```

---

## Task 6: Backfill — GitLab webhook callbacks

**Files:**
- Modify: `daiv/codebase/clients/gitlab/api/models.py`
- Modify: `daiv/codebase/clients/gitlab/api/callbacks.py`
- Test: `tests/unit_tests/codebase/clients/gitlab/test_webhook_use_max.py`

- [ ] **Step 6.1: Write failing tests for `has_max_label()`**

```python
# tests/unit_tests/codebase/clients/gitlab/test_webhook_use_max.py
from codebase.clients.gitlab.api.models import Issue, Label, MergeRequest


def _label(title: str) -> Label:
    return Label(id=1, title=title)


def test_issue_has_max_label_true():
    issue = Issue(id=1, iid=1, title="t", description="", state="opened", labels=[_label("daiv-max")])
    assert issue.has_max_label() is True


def test_issue_has_max_label_case_insensitive():
    issue = Issue(id=1, iid=1, title="t", description="", state="opened", labels=[_label("DAIV-Max")])
    assert issue.has_max_label() is True


def test_issue_has_max_label_false_when_absent():
    issue = Issue(id=1, iid=1, title="t", description="", state="opened", labels=[_label("daiv")])
    assert issue.has_max_label() is False


def test_merge_request_has_max_label_true():
    mr = MergeRequest(
        id=1,
        iid=1,
        title="t",
        state="opened",
        source_branch="feature",
        target_branch="main",
        labels=[_label("daiv-max")],
    )
    assert mr.has_max_label() is True
```

Field names/constructors must match the current pydantic models — inspect `daiv/codebase/clients/gitlab/api/models.py` for the real required args and adjust.

- [ ] **Step 6.2: Run tests — verify they fail**

```
uv run pytest tests/unit_tests/codebase/clients/gitlab/test_webhook_use_max.py -v
```

Expected: fails — `AttributeError: 'Issue' object has no attribute 'has_max_label'`.

- [ ] **Step 6.3: Add `has_max_label()` to both models**

In `daiv/codebase/clients/gitlab/api/models.py`, add a method to `Issue` (after `is_daiv()`, ~line 82) and to `MergeRequest` (after `is_daiv()`, ~line 111):

```python
def has_max_label(self) -> bool:
    """Check if the issue/MR carries the daiv-max label (case-insensitive)."""
    return any(label.title.lower() == BOT_MAX_LABEL.lower() for label in self.labels)
```

- [ ] **Step 6.4: Run tests — verify they pass**

```
uv run pytest tests/unit_tests/codebase/clients/gitlab/test_webhook_use_max.py -v
```

Expected: 4 PASSED.

- [ ] **Step 6.5: Thread `use_max` into the 3 callback sites**

In `daiv/codebase/clients/gitlab/api/callbacks.py`:

**Site 1 — `IssueCallback.process_callback` (line ~118):**

```python
await acreate_activity(
    trigger_type=TriggerType.ISSUE_WEBHOOK,
    task_result_id=result.id,
    repo_id=self.project.path_with_namespace,
    issue_iid=self.object_attributes.iid,
    use_max=self.object_attributes.has_max_label(),  # NEW
    user=daiv_user,
    external_username=self.user.username,
)
```

**Site 2 — `NoteCallback.process_callback` issue branch (line ~193):**

```python
await acreate_activity(
    trigger_type=TriggerType.ISSUE_WEBHOOK,
    task_result_id=result.id,
    repo_id=self.project.path_with_namespace,
    issue_iid=self.issue.iid,
    mention_comment_id=self.object_attributes.discussion_id,
    use_max=self.issue.has_max_label(),  # NEW
    user=daiv_user,
    external_username=self.user.username,
)
```

**Site 3 — `NoteCallback.process_callback` MR branch (line ~222):**

```python
await acreate_activity(
    trigger_type=TriggerType.MR_WEBHOOK,
    task_result_id=result.id,
    repo_id=self.project.path_with_namespace,
    merge_request_iid=self.merge_request.iid,
    mention_comment_id=self.object_attributes.discussion_id,
    use_max=self.merge_request.has_max_label(),  # NEW
    user=daiv_user,
    external_username=self.user.username,
)
```

- [ ] **Step 6.6: Run the GitLab callback test suite**

```
uv run pytest tests/unit_tests/codebase/clients/gitlab/ -v
```

Expected: all tests PASS (including the new `has_max_label` ones). Existing callback tests should still pass because `use_max` is persisted silently.

- [ ] **Step 6.7: Commit**

```bash
git add daiv/codebase/clients/gitlab/api/models.py daiv/codebase/clients/gitlab/api/callbacks.py tests/unit_tests/codebase/clients/gitlab/test_webhook_use_max.py
git commit -m "feat(gitlab): Persist use_max on webhook-triggered activities"
```

---

## Task 7: Backfill — GitHub webhook callbacks

**Files:**
- Modify: `daiv/codebase/clients/github/api/models.py`
- Modify: `daiv/codebase/clients/github/api/callbacks.py`
- Test: `tests/unit_tests/codebase/clients/github/test_webhook_use_max.py`

- [ ] **Step 7.1: Write failing test for `has_max_label()` (GitHub)**

```python
# tests/unit_tests/codebase/clients/github/test_webhook_use_max.py
from codebase.clients.github.api.models import Issue, Label


def _label(name: str) -> Label:
    return Label(id=1, name=name)


def test_issue_has_max_label_true():
    issue = Issue(id=1, number=1, title="t", state="open", labels=[_label("daiv-max")])
    assert issue.has_max_label() is True


def test_issue_has_max_label_case_insensitive():
    issue = Issue(id=1, number=1, title="t", state="open", labels=[_label("DAIV-MAX")])
    assert issue.has_max_label() is True


def test_issue_has_max_label_false_when_absent():
    issue = Issue(id=1, number=1, title="t", state="open", labels=[_label("daiv")])
    assert issue.has_max_label() is False
```

Adjust constructor args to match the real `Label` / `Issue` models.

- [ ] **Step 7.2: Run test — verify it fails**

```
uv run pytest tests/unit_tests/codebase/clients/github/test_webhook_use_max.py -v
```

Expected: `AttributeError: 'Issue' object has no attribute 'has_max_label'`.

- [ ] **Step 7.3: Add `has_max_label()` to the GitHub models**

In `daiv/codebase/clients/github/api/models.py`, after `Issue.is_daiv()` (line ~62), add:

```python
def has_max_label(self) -> bool:
    """Check if the issue/PR carries the daiv-max label (case-insensitive)."""
    return any(label.name.lower() == BOT_MAX_LABEL.lower() for label in self.labels)
```

If GitHub has a separate MR/PR-ish model with `labels`, add the same method there. (`grep -n "labels: list\[Label\]" daiv/codebase/clients/github/api/models.py` to locate.)

- [ ] **Step 7.4: Thread `use_max` into the 3 GitHub callback sites**

Same pattern as Task 6 Step 6.5, but in `daiv/codebase/clients/github/api/callbacks.py`. The 3 sites are at lines ~100, ~165, ~192 (based on recent grep). For each site, identify the issue/PR object available in scope and add `use_max=<obj>.has_max_label()` to the `acreate_activity(...)` call.

- [ ] **Step 7.5: Run tests**

```
uv run pytest tests/unit_tests/codebase/clients/github/ -v
```

- [ ] **Step 7.6: Commit**

```bash
git add daiv/codebase/clients/github/api/models.py daiv/codebase/clients/github/api/callbacks.py tests/unit_tests/codebase/clients/github/test_webhook_use_max.py
git commit -m "feat(github): Persist use_max on webhook-triggered activities"
```

---

## Task 8: Throttle helper

**Files:**
- Create: `daiv/jobs/throttle.py`
- Test: `tests/unit_tests/jobs/test_throttle.py`
- Modify: `daiv/jobs/api/views.py` — delegate `_LazyThrottle.get_rate` unchanged; add a sync helper that the new UI view will call.

- [ ] **Step 8.1: Write failing tests for `check_jobs_throttle`**

```python
# tests/unit_tests/jobs/test_throttle.py
import pytest
from django.core.cache import cache

from jobs.throttle import check_jobs_throttle


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def settings_with_rate(monkeypatch):
    def _set(rate: str):
        monkeypatch.setattr("core.site_settings.site_settings.jobs_throttle_rate", rate, raising=False)

    return _set


@pytest.mark.django_db
def test_allows_under_limit(member_user, settings_with_rate):
    settings_with_rate("3/minute")
    assert check_jobs_throttle(member_user) is True
    assert check_jobs_throttle(member_user) is True
    assert check_jobs_throttle(member_user) is True


@pytest.mark.django_db
def test_blocks_at_limit(member_user, settings_with_rate):
    settings_with_rate("2/minute")
    check_jobs_throttle(member_user)
    check_jobs_throttle(member_user)
    assert check_jobs_throttle(member_user) is False


@pytest.mark.django_db
def test_empty_rate_is_permissive(member_user, settings_with_rate):
    settings_with_rate("")
    assert check_jobs_throttle(member_user) is True


@pytest.mark.django_db
def test_invalid_rate_is_permissive(member_user, settings_with_rate):
    settings_with_rate("not-a-rate")
    assert check_jobs_throttle(member_user) is True


@pytest.mark.django_db
def test_per_user_buckets(admin_user, member_user, settings_with_rate):
    settings_with_rate("1/minute")
    assert check_jobs_throttle(admin_user) is True
    assert check_jobs_throttle(member_user) is True  # separate bucket
    assert check_jobs_throttle(admin_user) is False
```

Fixtures used (all from `tests/unit_tests/conftest.py`): `admin_user`, `member_user`. The `settings_with_rate` helper is defined inline above.

- [ ] **Step 8.2: Run tests — verify they fail**

```
uv run pytest tests/unit_tests/jobs/test_throttle.py -v
```

Expected: `ModuleNotFoundError: No module named 'jobs.throttle'`.

- [ ] **Step 8.3: Implement the helper**

Create `daiv/jobs/throttle.py`:

```python
"""Rate-limit helper for UI-initiated runs.

Shares the ``jobs_throttle_rate`` budget used by the API endpoint so that a
single user cannot exceed the configured hourly limit across API+UI combined.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from django.core.cache import cache

from core.site_settings import site_settings

if TYPE_CHECKING:
    from accounts.models import User

_RATE_RE = re.compile(r"^(\d+)/(second|minute|hour|day)$")
_WINDOW_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


def _parse_rate(rate: str) -> tuple[int, int] | None:
    """Parse ``"20/hour"`` into (count, window_seconds). Returns None for empty/invalid."""
    if not rate:
        return None
    m = _RATE_RE.match(rate.strip())
    if not m:
        return None
    return int(m.group(1)), _WINDOW_SECONDS[m.group(2)]


def check_jobs_throttle(user: "User") -> bool:
    """Return True if the user may submit another run, False if throttled.

    Empty/invalid rate strings are permissive — matches the existing API
    behaviour where a misconfigured rate does not lock everyone out.
    """
    parsed = _parse_rate(site_settings.jobs_throttle_rate)
    if parsed is None:
        return True

    limit, window = parsed
    now = int(time.time())
    bucket = now // window
    key = f"jobs_throttle:{user.pk}:{bucket}"
    # ``cache.incr`` raises if key is missing; use add + incr fallback for portability.
    if cache.add(key, 1, timeout=window):
        return True
    try:
        count = cache.incr(key)
    except ValueError:
        cache.add(key, 1, timeout=window)
        return True
    return count <= limit
```

- [ ] **Step 8.4: Run tests — verify they pass**

```
uv run pytest tests/unit_tests/jobs/test_throttle.py -v
```

Expected: 5 PASSED.

- [ ] **Step 8.5: Commit**

```bash
git add daiv/jobs/throttle.py tests/unit_tests/jobs/test_throttle.py
git commit -m "feat(jobs): Add shared check_jobs_throttle helper for UI submissions"
```

---

## Task 9: Form mixin + template partial

**Files:**
- Create: `daiv/activity/forms.py` (mixin only — the `AgentRunCreateForm` is added in Task 11)
- Create: `daiv/activity/templates/activity/_agent_run_fields.html`

- [ ] **Step 9.1: Create the mixin**

`daiv/activity/forms.py`:

```python
"""Shared form fields for any surface that submits an agent run.

Kept as a plain ``Form`` so both ``forms.Form`` and ``forms.ModelForm`` can
consume it as a mixin.
"""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _


class AgentRunFieldsMixin(forms.Form):
    prompt = forms.CharField(label=_("Prompt"), widget=forms.Textarea(attrs={"rows": 6}), required=True)
    repo_id = forms.CharField(label=_("Repository"), required=True)
    ref = forms.CharField(
        label=_("Branch / ref"), required=False, help_text=_("Leave empty to use the repository default branch.")
    )
    use_max = forms.BooleanField(
        label=_("Use max model"),
        required=False,
        initial=False,
        help_text=_("More capable model with thinking set to high."),
    )
```

- [ ] **Step 9.2: Create the template partial**

`daiv/activity/templates/activity/_agent_run_fields.html`:

```django
{% load i18n %}

<div class="space-y-4">
    <div>
        <label for="{{ form.prompt.id_for_label }}" class="block text-sm font-medium">
            {{ form.prompt.label }}
        </label>
        {{ form.prompt }}
        {% if form.prompt.errors %}
            <p class="mt-1 text-xs text-red-400">{{ form.prompt.errors|join:", " }}</p>
        {% endif %}
    </div>

    <div>
        <label for="{{ form.repo_id.id_for_label }}" class="block text-sm font-medium">
            {{ form.repo_id.label }}
        </label>
        {{ form.repo_id }}
        {% if form.repo_id.errors %}
            <p class="mt-1 text-xs text-red-400">{{ form.repo_id.errors|join:", " }}</p>
        {% endif %}
    </div>

    <div>
        <label for="{{ form.ref.id_for_label }}" class="block text-sm font-medium">
            {{ form.ref.label }}
        </label>
        {{ form.ref }}
        {% if form.ref.help_text %}
            <p class="mt-1 text-xs text-gray-400">{{ form.ref.help_text }}</p>
        {% endif %}
    </div>

    <div class="flex items-center gap-2">
        {{ form.use_max }}
        <label for="{{ form.use_max.id_for_label }}" class="text-sm font-medium">
            {{ form.use_max.label }}
        </label>
    </div>
    {% if form.use_max.help_text %}
        <p class="mt-1 text-xs text-gray-400">{{ form.use_max.help_text }}</p>
    {% endif %}
</div>
```

Before finalising the styles, open the existing `schedules/schedule_form.html` and match the Tailwind classes already used for equivalent `prompt`/`repo_id`/`ref`/`use_max` rows so the partial looks identical in both hosting templates. If the existing template applies custom widget CSS via form init, port it to the mixin's widget `attrs`.

- [ ] **Step 9.3: Commit**

```bash
git add daiv/activity/forms.py daiv/activity/templates/activity/_agent_run_fields.html
git commit -m "feat(activity): Add AgentRunFieldsMixin and shared template partial"
```

---

## Task 10: Refactor `ScheduledJobCreateForm` to consume the mixin

**Files:**
- Modify: `daiv/schedules/forms.py`
- Modify: `daiv/schedules/templates/schedules/schedule_form.html`

- [ ] **Step 10.1: Update `ScheduledJobCreateForm`**

Edit `daiv/schedules/forms.py`:

```python
from activity.forms import AgentRunFieldsMixin
# ... existing imports


class ScheduledJobCreateForm(AgentRunFieldsMixin, forms.ModelForm):
    class Meta:
        model = ScheduledJob
        fields = ["name", "prompt", "repo_id", "ref", "use_max", "frequency", "cron_expression", "time", "notify_on"]

    # existing _clean_conditional_fields, clean, save methods unchanged
```

Leave `ScheduledJobUpdateForm` untouched — it inherits via `ScheduledJobCreateForm`.

- [ ] **Step 10.2: Update schedule form template to use the partial**

In `daiv/schedules/templates/schedules/schedule_form.html`, replace the block(s) that render `{{ form.prompt }}`, `{{ form.repo_id }}`, `{{ form.ref }}`, `{{ form.use_max }}` individually with:

```django
{% include "activity/_agent_run_fields.html" with form=form %}
```

Keep surrounding markup for the schedule-specific fields (`name`, `frequency`, `cron_expression`, `time`, `notify_on`, `is_enabled`) unchanged.

- [ ] **Step 10.3: Verify existing schedule tests still pass**

```
uv run pytest tests/unit_tests/schedules/ -v
```

Expected: all existing tests still PASS. Then manually smoke-test the schedule create/edit UI:

```
uv run python daiv/manage.py runserver
```

Visit `/schedules/new/` and `/schedules/<pk>/edit/`; confirm the form renders the four shared fields identically to before.

- [ ] **Step 10.4: Commit**

```bash
git add daiv/schedules/forms.py daiv/schedules/templates/schedules/schedule_form.html
git commit -m "refactor(schedules): Use AgentRunFieldsMixin and shared fields partial"
```

---

## Task 11: `AgentRunCreateForm` with `submit()`

**Files:**
- Modify: `daiv/activity/forms.py` (append `AgentRunCreateForm`)
- Test: `tests/unit_tests/activity/test_forms.py`

- [ ] **Step 11.1: Write failing test for `submit()`**

```python
# tests/unit_tests/activity/test_forms.py
import uuid
from unittest import mock

import pytest

from activity.forms import AgentRunCreateForm
from activity.models import Activity, TriggerType


@pytest.mark.django_db
def test_submit_enqueues_and_creates_activity(member_user):
    user = member_user
    fake_task = mock.Mock(id=uuid.uuid4())
    form = AgentRunCreateForm(data={"prompt": "do the thing", "repo_id": "acme/repo", "ref": "main", "use_max": True})
    assert form.is_valid(), form.errors

    with mock.patch("activity.forms.run_job_task") as m_task:
        m_task.aenqueue = mock.AsyncMock(return_value=fake_task)
        activity = form.submit(user=user)

    m_task.aenqueue.assert_awaited_once_with(repo_id="acme/repo", prompt="do the thing", ref="main", use_max=True)

    reloaded = Activity.objects.get(pk=activity.pk)
    assert reloaded.trigger_type == TriggerType.UI_JOB
    assert reloaded.use_max is True
    assert reloaded.repo_id == "acme/repo"
    assert reloaded.ref == "main"
    assert reloaded.prompt == "do the thing"
    assert reloaded.user == user
    assert reloaded.task_result_id == fake_task.id


@pytest.mark.django_db
def test_submit_passes_none_for_empty_ref(member_user):
    user = member_user
    form = AgentRunCreateForm(data={"prompt": "x", "repo_id": "acme/repo", "ref": ""})
    assert form.is_valid(), form.errors

    with mock.patch("activity.forms.run_job_task") as m_task:
        m_task.aenqueue = mock.AsyncMock(return_value=mock.Mock(id=uuid.uuid4()))
        form.submit(user=user)

    kwargs = m_task.aenqueue.await_args.kwargs
    assert kwargs["ref"] is None
```

- [ ] **Step 11.2: Run test — verify it fails**

```
uv run pytest tests/unit_tests/activity/test_forms.py -v
```

Expected: `ImportError: cannot import name 'AgentRunCreateForm' from 'activity.forms'`.

- [ ] **Step 11.3: Implement `AgentRunCreateForm`**

Append to `daiv/activity/forms.py`:

```python
from asgiref.sync import async_to_sync

from activity.models import Activity, TriggerType
from activity.services import acreate_activity
from jobs.tasks import run_job_task


class AgentRunCreateForm(AgentRunFieldsMixin, forms.Form):
    """Submit a new agent run from the UI (blank form or retry pre-fill)."""

    def submit(self, *, user) -> Activity:
        data = self.cleaned_data
        ref = data["ref"] or None

        async def _submit() -> Activity:
            task = await run_job_task.aenqueue(
                repo_id=data["repo_id"], prompt=data["prompt"], ref=ref, use_max=data["use_max"]
            )
            return await acreate_activity(
                trigger_type=TriggerType.UI_JOB,
                task_result_id=task.id,
                repo_id=data["repo_id"],
                ref=data["ref"],
                prompt=data["prompt"],
                use_max=data["use_max"],
                user=user,
            )

        return async_to_sync(_submit)()
```

Note: `run_job_task.aenqueue` is async so the form wraps both operations in a single `async_to_sync` call. This keeps the view sync-friendly.

- [ ] **Step 11.4: Run tests — verify they pass**

```
uv run pytest tests/unit_tests/activity/test_forms.py -v
```

Expected: 2 PASSED.

- [ ] **Step 11.5: Commit**

```bash
git add daiv/activity/forms.py tests/unit_tests/activity/test_forms.py
git commit -m "feat(activity): Add AgentRunCreateForm with submit()"
```

---

## Task 12: `AgentRunCreateView` + URL wiring

**Files:**
- Modify: `daiv/activity/views.py`
- Create: `daiv/activity/urls_runs.py`
- Modify: `daiv/daiv/urls.py` — locate with `grep -n "activity" daiv/daiv/urls.py` and add a sibling include.
- Test: `tests/unit_tests/activity/test_views_runs.py`

- [ ] **Step 12.1: Write failing tests for the view**

```python
# tests/unit_tests/activity/test_views_runs.py
import uuid
from unittest import mock

import pytest
from django.urls import reverse

from activity.models import Activity, ActivityStatus, TriggerType


from accounts.models import Role
from accounts.models import User as AccountUser


def _make_user(username: str) -> AccountUser:
    return AccountUser.objects.create_user(
        username=username,
        email=f"{username}@test.com",
        password="testpass123",  # noqa: S106
        role=Role.MEMBER,
    )


@pytest.fixture
def source_activity(db, member_user) -> Activity:
    return Activity.objects.create(
        user=member_user,
        status=ActivityStatus.SUCCESSFUL,
        trigger_type=TriggerType.API_JOB,
        repo_id="acme/repo",
        ref="main",
        prompt="do the thing",
        use_max=False,
    )


@pytest.mark.django_db
def test_get_blank_renders_empty_form(member_client):
    resp = member_client.get(reverse("runs:agent_run_new"))
    assert resp.status_code == 200
    # Custom behavior: form starts without a source_activity context key
    assert resp.context["source_activity"] is None


@pytest.mark.django_db
def test_get_retry_prefills_fields(member_client, member_user):
    source = Activity.objects.create(
        user=member_user,
        status=ActivityStatus.SUCCESSFUL,
        trigger_type=TriggerType.API_JOB,
        repo_id="a/b",
        ref="develop",
        prompt="P",
        use_max=True,
    )
    resp = member_client.get(reverse("runs:agent_run_new") + f"?from={source.pk}")
    assert resp.status_code == 200
    assert resp.context["form"].initial == {"prompt": "P", "repo_id": "a/b", "ref": "develop", "use_max": True}
    assert resp.context["source_activity"].pk == source.pk


@pytest.mark.django_db
@pytest.mark.parametrize("status", [ActivityStatus.READY, ActivityStatus.RUNNING])
def test_get_retry_non_terminal_returns_404(member_client, member_user, status):
    source = Activity.objects.create(user=member_user, status=status, trigger_type=TriggerType.API_JOB, repo_id="a/b")
    resp = member_client.get(reverse("runs:agent_run_new") + f"?from={source.pk}")
    assert resp.status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize("trigger", [TriggerType.ISSUE_WEBHOOK, TriggerType.MR_WEBHOOK])
def test_get_retry_webhook_returns_404(member_client, member_user, trigger):
    source = Activity.objects.create(
        user=member_user, status=ActivityStatus.SUCCESSFUL, trigger_type=trigger, repo_id="a/b"
    )
    resp = member_client.get(reverse("runs:agent_run_new") + f"?from={source.pk}")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_get_retry_other_users_activity_returns_404(member_client):
    owner = _make_user("owner2")
    source = Activity.objects.create(
        user=owner, status=ActivityStatus.SUCCESSFUL, trigger_type=TriggerType.API_JOB, repo_id="a/b"
    )
    resp = member_client.get(reverse("runs:agent_run_new") + f"?from={source.pk}")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_post_valid_submits_and_redirects(member_client):
    fake_task = mock.Mock(id=uuid.uuid4())
    with mock.patch("activity.forms.run_job_task") as m_task:
        m_task.aenqueue = mock.AsyncMock(return_value=fake_task)
        resp = member_client.post(
            reverse("runs:agent_run_new"), data={"prompt": "go", "repo_id": "acme/repo", "ref": "", "use_max": "on"}
        )
    assert resp.status_code == 302
    created = Activity.objects.get(task_result_id=fake_task.id)
    assert resp["Location"] == reverse("activity_detail", args=[created.pk])
    assert created.trigger_type == TriggerType.UI_JOB
    assert created.use_max is True


@pytest.mark.django_db
def test_post_throttled_rerenders_with_error(member_client, monkeypatch):
    monkeypatch.setattr("activity.views.check_jobs_throttle", lambda u: False)
    resp = member_client.post(reverse("runs:agent_run_new"), data={"prompt": "go", "repo_id": "acme/repo"})
    assert resp.status_code == 200
    assert "Rate limit" in resp.content.decode()
```

- [ ] **Step 12.2: Run tests — verify they fail**

```
uv run pytest tests/unit_tests/activity/test_views_runs.py -v
```

Expected: `NoReverseMatch: 'runs' is not a registered namespace`.

- [ ] **Step 12.3: Implement the view**

Append to `daiv/activity/views.py`:

```python
from django.http import Http404
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView

from activity.forms import AgentRunCreateForm
from activity.models import Activity
from jobs.throttle import check_jobs_throttle


class AgentRunCreateView(LoginRequiredMixin, BreadcrumbMixin, FormView):
    template_name = "activity/agent_run_form.html"
    form_class = AgentRunCreateForm

    def _get_source_activity(self) -> Activity | None:
        source_id = self.request.GET.get("from")
        if not source_id:
            return None
        source = Activity.objects.by_owner(self.request.user).filter(pk=source_id).first()
        if source is None or not source.is_retryable:
            raise Http404("Activity is not retryable.")
        return source

    def get_initial(self) -> dict:
        source = self._get_source_activity()
        if source is None:
            return {}
        return {"prompt": source.prompt, "repo_id": source.repo_id, "ref": source.ref, "use_max": source.use_max}

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["source_activity"] = self._get_source_activity()
        return ctx

    def form_valid(self, form):
        if not check_jobs_throttle(self.request.user):
            form.add_error(None, _("Rate limit exceeded; try again later."))
            return self.form_invalid(form)
        activity = form.submit(user=self.request.user)
        return self._redirect_to_activity(activity.pk)

    def _redirect_to_activity(self, pk):
        from django.shortcuts import redirect

        return redirect("activity_detail", pk=pk)

    def get_breadcrumbs(self):
        return [{"label": "Activity", "url": reverse("activity_list")}, {"label": "Start a run", "url": None}]
```

- [ ] **Step 12.4: Create `daiv/activity/urls_runs.py`**

```python
from django.urls import path

from activity.views import AgentRunCreateView

app_name = "runs"

urlpatterns = [path("new/", AgentRunCreateView.as_view(), name="agent_run_new")]
```

- [ ] **Step 12.5: Mount `/runs/` in the root URLconf**

Find the project's root URLconf (`grep -n "include.*activity" daiv/daiv/urls.py`). Alongside the existing activity `include`, add:

```python
(path("runs/", include("activity.urls_runs", namespace="runs")),)
```

- [ ] **Step 12.6: Create the page template**

`daiv/activity/templates/activity/agent_run_form.html`:

```django
{% extends "base_app.html" %}
{% load i18n %}

{% block title %}{% if source_activity %}Retry run{% else %}Start a run{% endif %} — DAIV{% endblock %}

{% block breadcrumb %}
{% include "accounts/_breadcrumb.html" with crumbs=breadcrumbs %}
{% endblock %}

{% block app_content %}
<div class="animate-fade-up">
    <h1 class="text-2xl font-bold tracking-tight">
        {% if source_activity %}Retry run{% else %}Start a run{% endif %}
    </h1>
    {% if source_activity %}
    <p class="mt-1.5 text-[15px] font-light text-gray-400">
        Retried from
        <a href="{% url 'activity_detail' source_activity.pk %}" class="underline">
            {{ source_activity.repo_id }} &middot; {{ source_activity.created_at|date:"Y-m-d H:i" }}
        </a>
    </p>
    {% endif %}
</div>

<form method="post" class="animate-fade-up mt-8 rounded-2xl border border-white/[0.06] bg-white/[0.02] p-6">
    {% csrf_token %}
    {% if form.non_field_errors %}
    <div class="mb-4 rounded-md bg-red-900/40 p-3 text-sm text-red-200">
        {{ form.non_field_errors|join:", " }}
    </div>
    {% endif %}

    {% include "activity/_agent_run_fields.html" with form=form %}

    <div class="mt-6">
        <button type="submit" class="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium">
            {% if source_activity %}Retry{% else %}Start run{% endif %}
        </button>
    </div>
</form>
{% endblock %}
```

- [ ] **Step 12.7: Run all activity tests**

```
uv run pytest tests/unit_tests/activity/ -v
```

Expected: all 7 new view tests PASS along with existing activity tests.

- [ ] **Step 12.8: Commit**

```bash
git add daiv/activity/views.py daiv/activity/urls_runs.py daiv/activity/templates/activity/agent_run_form.html daiv/daiv/urls.py tests/unit_tests/activity/test_views_runs.py
git commit -m "feat(activity): Add AgentRunCreateView with retry pre-fill"
```

---

## Task 13: Retry button on activity detail page

**Files:**
- Modify: `daiv/activity/templates/activity/activity_detail.html`

- [ ] **Step 13.1: Add the retry button**

Insert inside the status-badges `<div class="flex flex-wrap items-center gap-3">` block (around line 27-47 of `activity_detail.html`), before the closing `</div>`:

```django
{% if activity.is_retryable %}
<a href="{% url 'runs:agent_run_new' %}?from={{ activity.pk }}"
   class="ml-auto inline-flex items-center gap-1.5 rounded-md border border-white/10 bg-white/[0.04] px-3 py-1 text-sm font-medium hover:bg-white/[0.08]">
    {% load icon_tags %}{% icon "refresh" %}
    Retry
</a>
{% endif %}
```

Match the icon name to whatever the existing `icon_tags` library provides (grep `{% icon "` in existing templates to find a valid name; fall back to text-only "Retry" if no suitable icon exists).

- [ ] **Step 13.2: Smoke-test**

```
uv run python daiv/manage.py runserver
```

Visit an activity detail page:
- For a terminal API/MCP/schedule/UI activity → **Retry button visible**.
- For a RUNNING activity → no button.
- For a webhook activity → no button.

Click Retry → lands on `/runs/new/?from=<id>` with fields pre-filled.

- [ ] **Step 13.3: Commit**

```bash
git add daiv/activity/templates/activity/activity_detail.html
git commit -m "feat(activity): Add Retry button to activity detail page"
```

---

## Task 14: Nav entry — "Start a run"

**Files:**
- Modify: the sidebar/nav template (locate via `grep -rln "activity_list\|schedule_list" daiv/*/templates/ daiv/*/*/templates/`).

- [ ] **Step 14.1: Add the nav link**

In the sidebar template (likely `accounts/templates/accounts/_sidebar.html` or similar), add a link item alongside "Activity" and "Schedules":

```django
<a href="{% url 'runs:agent_run_new' %}" class="nav-link {% if request.resolver_match.namespace == 'runs' %}active{% endif %}">
    {% load icon_tags %}{% icon "play" %}
    <span>Start a run</span>
</a>
```

Match class names and icon conventions already used in the file.

- [ ] **Step 14.2: Smoke-test**

```
uv run python daiv/manage.py runserver
```

- Click "Start a run" from the sidebar → blank form loads.
- Submit the form → new activity visible in `/activity/` and status begins streaming.

- [ ] **Step 14.3: Commit**

```bash
git add <sidebar template path>
git commit -m "feat(activity): Add Start a run entry to sidebar nav"
```

---

## Task 15: Delegate API throttle to shared helper (optional tightening)

**Files:**
- Modify: `daiv/jobs/api/views.py`

Ninja's `AuthRateThrottle` keeps its own per-user bucket, independent of the new `check_jobs_throttle` cache. Two options:
- **Leave as-is:** API and UI each have their own counter against the same configured rate. Two separate buckets means a determined user could get `2×` the limit by alternating channels.
- **Unify:** replace the ninja throttle with a plain check against `check_jobs_throttle`, returning 429 on block.

Choose the unified path only if you care about the cross-channel budget. If you don't, skip this task — the spec does not require unification and YAGNI applies. If you keep it separate, update the spec's "Rate limiting" bullet to clarify "same rate, independent buckets".

- [ ] **Step 15.1 (only if unifying): Rewrite the ninja throttle wrapper**

```python
from ninja.throttling import BaseThrottle
from jobs.throttle import check_jobs_throttle


class _SharedThrottle(BaseThrottle):
    def allow_request(self, request) -> bool:
        user = request.auth
        if user is None:
            return True
        return check_jobs_throttle(user)
```

Replace `_LazyThrottle` with `_SharedThrottle` in the `@jobs_router.post(...)` decorator.

- [ ] **Step 15.2 (if done): Commit**

```bash
git add daiv/jobs/api/views.py
git commit -m "refactor(jobs): Unify API and UI throttle budgets via check_jobs_throttle"
```

---

## Task 16: Full verification & changelog

- [ ] **Step 16.1: Run the whole test suite**

```
make test
```

Expected: all tests pass, including pre-existing.

- [ ] **Step 16.2: Lint & types**

```
make lint-fix
make lint-typing
```

Expected: both clean.

- [ ] **Step 16.3: Translation strings**

```
make makemessages
```

Expected: the new `_("…")` strings appear in the `.po` files. Commit `.po` changes separately if your workflow requires it.

```
make compilemessages
```

- [ ] **Step 16.4: Changelog**

Append one line under the "Unreleased" section of `CHANGELOG.md` (or wherever your repo tracks notable changes):

```
- Added "Start a run" page and one-click retry of past agent runs from the activity detail page.
```

- [ ] **Step 16.5: Final commit**

```bash
git add CHANGELOG.md daiv/locale/**/*.po
git commit -m "chore(release): Document agent-run retry and start-a-run page"
```

- [ ] **Step 16.6: Push branch & open PR**

```bash
git push -u origin docs/agent-run-retry-spec  # existing branch carries spec + code
gh pr create --title "feat(activity): Agent-run retry and start-a-run page" --body-file docs/superpowers/specs/2026-04-17-agent-run-retry-design.md
```

---

## Self-review checklist (post-execution)

- All tasks marked done above.
- `Activity.is_retryable` returns `True` only for terminal × non-webhook.
- `use_max` persisted for every Activity-creation call site.
- `ScheduledJobCreateForm` renders identically to before the refactor.
- `/runs/new/` renders blank form; `/runs/new/?from=<retryable-uuid>` pre-fills.
- Retry button hidden for non-terminal and webhook activities.
- Throttle helper returns `False` once the limit is exceeded for a single user.
- No new `# noqa`, `type: ignore`, or TODOs introduced.
