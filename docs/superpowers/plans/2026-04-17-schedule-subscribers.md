# Schedule Subscribers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let schedule owners add other DAIV users as subscribers who receive the same finish-notifications and can view the resulting activities, and let subscribers unsubscribe themselves from the activity detail page.

**Architecture:** Add a `ScheduledJob.subscribers` M2M. Fan the `activity_finished` notification to owner + subscribers (deduped, failure-isolated per recipient). Broaden `Activity.objects.by_owner` so subscribers can view linked activities. Add a Ninja JSON endpoint + Alpine.js chip-picker for the owner UI, and a POST-only `ScheduleUnsubscribeView` exposed as a button on the activity detail page.

**Tech Stack:** Django 5, Django Ninja (`daiv/api.py`), Alpine.js, Tailwind, pytest + pytest-django (`asyncio_mode = "auto"`), uv for tool invocation.

**Spec reference:** `docs/superpowers/specs/2026-04-17-schedule-subscribers-design.md`

---

## File structure

### Created

| Path | Responsibility |
|---|---|
| `daiv/schedules/migrations/0008_scheduledjob_subscribers.py` | M2M table migration (auto-generated) |
| `daiv/accounts/api/__init__.py` | Package marker |
| `daiv/accounts/api/schemas.py` | `UserSearchResult` Pydantic schema |
| `daiv/accounts/api/router.py` | Ninja router with `/users/search` |
| `daiv/schedules/static/schedules/js/subscriber-picker.js` | Alpine component for multi-select chip picker |
| `daiv/schedules/templates/schedules/_subscriber_picker.html` | Partial rendering the picker inside the form |
| `tests/unit_tests/accounts/test_api.py` | UserSearch API tests |

### Modified

| Path | Change |
|---|---|
| `daiv/schedules/models.py` | Add `subscribers` M2M |
| `daiv/schedules/forms.py` | `subscribers` field, `owner` kwarg, owner-exclusion cleaning |
| `daiv/schedules/views.py` | Pass `owner` kwarg to forms; add `ScheduleUnsubscribeView` |
| `daiv/schedules/urls.py` | Add `schedule_unsubscribe` route |
| `daiv/schedules/templates/schedules/schedule_form.html` | Include subscriber picker partial + load JS |
| `daiv/activity/models.py` | Broaden `ActivityManager.by_owner` with subscriber predicate |
| `daiv/activity/views.py` | Compute `is_subscriber` in `ActivityDetailView.get_context_data` |
| `daiv/activity/templates/activity/activity_detail.html` | Plain-text schedule name for non-owners; conditional Unsubscribe form |
| `daiv/notifications/signals.py` | Fanout to owner + subscribers (deduped, per-recipient try/except) |
| `daiv/daiv/api.py` | Register accounts router |
| `tests/unit_tests/schedules/test_models.py` | Subscribers M2M tests |
| `tests/unit_tests/schedules/test_forms.py` | Subscribers field tests |
| `tests/unit_tests/schedules/test_views.py` | ScheduleUnsubscribeView tests; owner-kwarg wiring |
| `tests/unit_tests/activity/test_views.py` | Subscriber visibility + `is_subscriber` context tests |
| `tests/unit_tests/notifications/test_signals.py` | Subscriber fanout tests |
| `docs/features/scheduled-jobs.md` | Document subscribers + self-unsubscribe |

---

## Task 1: Subscribers M2M field and migration

**Files:**
- Modify: `daiv/schedules/models.py`
- Create: `daiv/schedules/migrations/0008_scheduledjob_subscribers.py`
- Test: `tests/unit_tests/schedules/test_models.py`

- [ ] **Step 1: Add the failing model test**

Append to `tests/unit_tests/schedules/test_models.py`:

```python
class TestScheduledJobSubscribers:
    def _make(self, user, **overrides):
        defaults = {
            "user": user,
            "name": "s",
            "prompt": "p",
            "repo_id": "x/y",
            "frequency": "daily",
            "time": "12:00",
        }
        defaults.update(overrides)
        job = ScheduledJob.objects.create(**defaults)
        return job

    def test_subscribers_empty_by_default(self, member_user):
        job = self._make(member_user)
        assert list(job.subscribers.all()) == []

    def test_add_and_remove_subscribers(self, member_user, admin_user):
        job = self._make(member_user)
        job.subscribers.add(admin_user)
        assert list(job.subscribers.all()) == [admin_user]
        job.subscribers.remove(admin_user)
        assert list(job.subscribers.all()) == []

    def test_deleting_subscriber_user_removes_membership(self, member_user, admin_user):
        job = self._make(member_user)
        job.subscribers.add(admin_user)
        admin_user.delete()
        job.refresh_from_db()
        assert list(job.subscribers.all()) == []

    def test_reverse_accessor_subscribed_schedules(self, member_user, admin_user):
        job = self._make(member_user)
        job.subscribers.add(admin_user)
        assert list(admin_user.subscribed_schedules.all()) == [job]
```

Keep the `@pytest.mark.django_db` already applied at the module level by the existing classes — add the same decorator above the new class:

```python
@pytest.mark.django_db
class TestScheduledJobSubscribers:
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit_tests/schedules/test_models.py::TestScheduledJobSubscribers -v
```

Expected: FAIL — `AttributeError: 'ScheduledJob' object has no attribute 'subscribers'`.

- [ ] **Step 3: Add `subscribers` field to `ScheduledJob`**

Edit `daiv/schedules/models.py`. Inside the `ScheduledJob` class, insert the new field immediately after the existing `notify_on` line:

```python
notify_on = models.CharField(_("notify on"), max_length=16, choices=NotifyOn.choices, default=NotifyOn.NEVER)
subscribers = models.ManyToManyField(
    settings.AUTH_USER_MODEL,
    blank=True,
    related_name="subscribed_schedules",
    verbose_name=_("subscribers"),
    help_text=_("Other users CC'd on this schedule's finish notifications."),
)
```

- [ ] **Step 4: Generate the migration**

```bash
uv run python daiv/manage.py makemigrations schedules
```

Expected output includes: `Migrations for 'schedules':` and a new file `daiv/schedules/migrations/0008_scheduledjob_subscribers.py`.

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/unit_tests/schedules/test_models.py::TestScheduledJobSubscribers -v
```

Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add daiv/schedules/models.py daiv/schedules/migrations/0008_scheduledjob_subscribers.py tests/unit_tests/schedules/test_models.py
git commit -m "feat(schedules): add subscribers m2m to ScheduledJob"
```

---

## Task 2: Subscriber-aware activity visibility

**Files:**
- Modify: `daiv/activity/models.py:39-48`
- Test: `tests/unit_tests/activity/test_views.py`

- [ ] **Step 1: Add the failing activity-view tests**

Append to `tests/unit_tests/activity/test_views.py` (using existing `member_client`, `member_user`, `admin_user` fixtures and a locally-created schedule):

```python
import pytest
from django.test import Client
from django.urls import reverse

from accounts.models import User
from activity.models import Activity, ActivityStatus, TriggerType
from schedules.models import Frequency, ScheduledJob


@pytest.mark.django_db
class TestActivityVisibilityForSubscribers:
    def _schedule(self, owner, **overrides):
        data = {
            "user": owner, "name": "s", "prompt": "p", "repo_id": "x/y",
            "frequency": Frequency.DAILY, "time": "12:00",
        }
        data.update(overrides)
        return ScheduledJob.objects.create(**data)

    def _activity(self, schedule, **overrides):
        data = {
            "trigger_type": TriggerType.SCHEDULE,
            "repo_id": schedule.repo_id,
            "status": ActivityStatus.SUCCESSFUL,
            "scheduled_job": schedule,
            "user": schedule.user,
        }
        data.update(overrides)
        return Activity.objects.create(**data)

    def test_subscriber_can_view_linked_activity_detail(self, member_user):
        owner = User.objects.create_user(username="owner", email="owner@t.com", password="x")  # noqa: S106
        schedule = self._schedule(owner)
        schedule.subscribers.add(member_user)
        activity = self._activity(schedule)

        client = Client()
        client.force_login(member_user)
        response = client.get(reverse("activity_detail", args=[activity.pk]))
        assert response.status_code == 200

    def test_non_subscriber_cannot_view_linked_activity_detail(self, member_user):
        owner = User.objects.create_user(username="owner", email="owner@t.com", password="x")  # noqa: S106
        schedule = self._schedule(owner)
        # member_user is NOT added as subscriber
        activity = self._activity(schedule)

        client = Client()
        client.force_login(member_user)
        response = client.get(reverse("activity_detail", args=[activity.pk]))
        assert response.status_code == 404

    def test_subscriber_sees_activity_in_list(self, member_user):
        owner = User.objects.create_user(username="owner", email="owner@t.com", password="x")  # noqa: S106
        schedule = self._schedule(owner)
        schedule.subscribers.add(member_user)
        activity = self._activity(schedule)

        client = Client()
        client.force_login(member_user)
        response = client.get(reverse("activity_list"))
        assert response.status_code == 200
        assert str(activity.pk) in response.content.decode()

    def test_list_does_not_duplicate_rows_for_admins_matching_twice(self, admin_user):
        """Admin branch short-circuits — still only one row even if admin is also a subscriber."""
        owner = User.objects.create_user(username="owner", email="owner@t.com", password="x")  # noqa: S106
        schedule = self._schedule(owner)
        schedule.subscribers.add(admin_user)
        activity = self._activity(schedule)

        client = Client()
        client.force_login(admin_user)
        response = client.get(reverse("activity_list"))
        content = response.content.decode()
        assert content.count(str(activity.pk)) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit_tests/activity/test_views.py::TestActivityVisibilityForSubscribers -v
```

Expected: `test_subscriber_can_view_linked_activity_detail` and `test_subscriber_sees_activity_in_list` FAIL with 404 or empty list.

- [ ] **Step 3: Broaden the by_owner queryset**

Edit `daiv/activity/models.py`. Replace the body of `ActivityManager.by_owner` (lines 40-48):

```python
    def by_owner(self, user: User) -> models.QuerySet[Activity]:
        """Return activities visible to the given user.

        Admins see all. Regular users see activities where they are:
        - the owner (``user`` FK), or
        - matched by ``external_username``, or
        - a subscriber of the linked ``scheduled_job``.
        """
        if user.is_admin:
            return self.all()
        return self.filter(
            models.Q(user=user)
            | models.Q(external_username=user.username)
            | models.Q(scheduled_job__subscribers=user)
        ).distinct()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/unit_tests/activity/test_views.py::TestActivityVisibilityForSubscribers -v
```

Expected: 4 passed.

- [ ] **Step 5: Sanity-check the rest of the activity tests still pass**

```bash
uv run pytest tests/unit_tests/activity/ -v
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add daiv/activity/models.py tests/unit_tests/activity/test_views.py
git commit -m "feat(activity): include subscribed schedules in user-visible activities"
```

---

## Task 3: Fan notifications out to subscribers

**Files:**
- Modify: `daiv/notifications/signals.py:63-88`
- Test: `tests/unit_tests/notifications/test_signals.py`

- [ ] **Step 1: Add the failing fanout tests**

Append to `tests/unit_tests/notifications/test_signals.py` after the existing `TestOnActivityFinished` class (reuse the module-level `schedule` fixture):

```python
@pytest.mark.django_db
class TestFanoutToSubscribers:
    def _make_user(self, username):
        u = User.objects.create_user(
            username=username, email=f"{username}@test.com", password="x",  # noqa: S106
        )
        # sync_email_binding signal creates the binding automatically
        return u

    def test_owner_plus_two_subscribers_each_get_one_notification(self, member_user, schedule):
        sub1 = self._make_user("sub1")
        sub2 = self._make_user("sub2")
        schedule.subscribers.add(sub1, sub2)

        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            scheduled_job=schedule,
        )
        activity_finished.send(sender=Activity, activity=activity)

        assert Notification.objects.filter(recipient=member_user).count() == 1
        assert Notification.objects.filter(recipient=sub1).count() == 1
        assert Notification.objects.filter(recipient=sub2).count() == 1

    def test_owner_accidentally_in_subscribers_still_one_notification(self, member_user, schedule):
        schedule.subscribers.add(member_user)  # owner == subscriber (invariant violation)
        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            scheduled_job=schedule,
        )
        activity_finished.send(sender=Activity, activity=activity)
        assert Notification.objects.filter(recipient=member_user).count() == 1

    def test_notify_on_never_skips_all_subscribers(self, member_user, schedule):
        schedule.notify_on = NotifyOn.NEVER
        schedule.save()
        sub = self._make_user("sub1")
        schedule.subscribers.add(sub)

        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            scheduled_job=schedule,
        )
        activity_finished.send(sender=Activity, activity=activity)
        assert Notification.objects.count() == 0

    def test_one_recipient_failure_does_not_block_others(self, member_user, schedule, mocker):
        from notifications.services import notify as real_notify

        sub1 = self._make_user("sub1")
        sub2 = self._make_user("sub2")
        schedule.subscribers.add(sub1, sub2)

        def flaky_notify(*, recipient, **kwargs):
            if recipient.pk == sub1.pk:
                raise RuntimeError("boom")
            return real_notify(recipient=recipient, **kwargs)

        mocker.patch("notifications.signals.notify", side_effect=flaky_notify)

        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            scheduled_job=schedule,
        )
        activity_finished.send(sender=Activity, activity=activity)

        assert Notification.objects.filter(recipient=member_user).count() == 1
        assert Notification.objects.filter(recipient=sub1).count() == 0
        assert Notification.objects.filter(recipient=sub2).count() == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit_tests/notifications/test_signals.py::TestFanoutToSubscribers -v
```

Expected: subscriber assertions fail because only `schedule.user` is currently notified.

- [ ] **Step 3: Implement the fanout**

Edit `daiv/notifications/signals.py`. Replace the entire `on_activity_finished` function body (lines 63-88):

```python
@receiver(activity_finished, dispatch_uid="notifications.on_activity_finished")
def on_activity_finished(sender, activity: Activity, **kwargs) -> None:
    schedule = activity.scheduled_job
    if schedule is None or schedule.notify_on == NotifyOn.NEVER:
        return
    if not _status_matches(schedule.notify_on, activity.status):
        return

    channels = [cls.channel_type for cls in all_channels()]
    if not channels:
        return

    recipients: dict[int, object] = {schedule.user_id: schedule.user}
    for sub in schedule.subscribers.all():
        recipients.setdefault(sub.pk, sub)

    subject = _render_subject(schedule, activity)
    body = _render_body(schedule, activity)
    link_url = reverse("activity_detail", args=[activity.pk])
    context = {"status": activity.status, "schedule_name": schedule.name}

    for recipient in recipients.values():
        try:
            notify(
                recipient=recipient,
                event_type="schedule.finished",
                source_type="activity.Activity",
                source_id=str(activity.pk),
                subject=subject,
                body=body,
                link_url=link_url,
                channels=channels,
                context=context,
            )
        except Exception:
            logger.exception(
                "Failed to create notification for activity %s, recipient pk=%s",
                activity.pk, getattr(recipient, "pk", None),
            )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/unit_tests/notifications/test_signals.py -v
```

Expected: all green (existing tests for owner-only behavior still pass because `subscribers` is empty).

- [ ] **Step 5: Commit**

```bash
git add daiv/notifications/signals.py tests/unit_tests/notifications/test_signals.py
git commit -m "feat(notifications): fan schedule finish events to subscribers"
```

---

## Task 4: User search API endpoint

**Files:**
- Create: `daiv/accounts/api/__init__.py`
- Create: `daiv/accounts/api/schemas.py`
- Create: `daiv/accounts/api/router.py`
- Modify: `daiv/daiv/api.py`
- Create: `tests/unit_tests/accounts/test_api.py`

- [ ] **Step 1: Add the failing API tests**

Create `tests/unit_tests/accounts/test_api.py`:

```python
import pytest
from django.test import Client
from django.urls import reverse

from accounts.models import Role, User


@pytest.mark.django_db
class TestUserSearchEndpoint:
    URL = "/api/accounts/users/search"

    def test_requires_authentication(self):
        client = Client()
        response = client.get(f"{self.URL}?q=ali")
        # django_auth rejects unauthenticated requests with 401
        assert response.status_code == 401

    def test_returns_empty_for_short_query(self, member_client):
        response = member_client.get(f"{self.URL}?q=a")
        assert response.status_code == 200
        assert response.json() == []

    def test_matches_by_username(self, member_client):
        User.objects.create_user(username="alice", email="alice@t.com", password="x")  # noqa: S106
        response = member_client.get(f"{self.URL}?q=ali")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["username"] == "alice"

    def test_matches_by_email(self, member_client):
        User.objects.create_user(username="alice", email="bob@company.com", password="x")  # noqa: S106
        response = member_client.get(f"{self.URL}?q=bob@com")
        assert response.status_code == 200
        assert [u["username"] for u in response.json()] == ["alice"]

    def test_excludes_requesting_user(self, member_client, member_user):
        response = member_client.get(f"{self.URL}?q={member_user.username[:3]}")
        data = response.json()
        assert all(u["username"] != member_user.username for u in data)

    def test_honors_explicit_exclude_list(self, member_client):
        alice = User.objects.create_user(username="alice", email="a@t.com", password="x")  # noqa: S106
        User.objects.create_user(username="alicia", email="b@t.com", password="x")  # noqa: S106
        response = member_client.get(f"{self.URL}?q=ali&exclude={alice.pk}")
        data = response.json()
        assert all(u["id"] != alice.pk for u in data)

    def test_excludes_inactive_users(self, member_client):
        u = User.objects.create_user(username="alice", email="a@t.com", password="x")  # noqa: S106
        u.is_active = False
        u.save()
        response = member_client.get(f"{self.URL}?q=ali")
        assert response.json() == []

    def test_response_shape(self, member_client):
        User.objects.create_user(
            username="alice", email="a@t.com", password="x", name="Alice Doe", role=Role.MEMBER,  # noqa: S106
        )
        response = member_client.get(f"{self.URL}?q=ali")
        body = response.json()[0]
        assert set(body.keys()) == {"id", "username", "name", "email"}
        assert body["name"] == "Alice Doe"
        assert body["email"] == "a@t.com"
```

- [ ] **Step 2: Run to verify failure (endpoint does not exist)**

```bash
uv run pytest tests/unit_tests/accounts/test_api.py -v
```

Expected: all tests FAIL with 404 on the URL.

- [ ] **Step 3: Create the schema**

Create `daiv/accounts/api/__init__.py` (empty file):

```python
```

Create `daiv/accounts/api/schemas.py`:

```python
from ninja import Schema


class UserSearchResult(Schema):
    id: int
    username: str
    name: str
    email: str
```

- [ ] **Step 4: Create the router**

Create `daiv/accounts/api/router.py`:

```python
from django.db.models import Q
from django.http import HttpRequest  # noqa: TC002 - required at runtime by Django Ninja

from ninja import Router
from ninja.security import django_auth

from accounts.api.schemas import UserSearchResult
from accounts.models import User

router = Router(tags=["accounts"])

MIN_QUERY_LENGTH = 2
MAX_RESULTS = 20


@router.get("/users/search", response=list[UserSearchResult], auth=django_auth)
def search_users(
    request: HttpRequest, q: str = "", exclude: str = "",
) -> list[UserSearchResult]:
    """Search active users by username, email, or name for autocomplete.

    Excludes the requesting user and any ids passed in the ``exclude`` CSV param.
    """
    if len(q) < MIN_QUERY_LENGTH:
        return []

    exclude_ids: set[int] = {request.user.pk}
    for part in exclude.split(","):
        part = part.strip()
        if part.isdigit():
            exclude_ids.add(int(part))

    qs = (
        User.objects.filter(is_active=True)
        .filter(Q(username__icontains=q) | Q(email__icontains=q) | Q(name__icontains=q))
        .exclude(pk__in=exclude_ids)
        .order_by("username")[:MAX_RESULTS]
    )
    return [
        UserSearchResult(id=u.pk, username=u.username, name=u.name, email=u.email) for u in qs
    ]
```

- [ ] **Step 5: Register the router on the API**

Edit `daiv/daiv/api.py`. Add the import and registration:

```python
from jobs.api.views import jobs_router
from mcp_server.api.views import oauth_router
from ninja import NinjaAPI

from accounts.api.router import router as accounts_router
from chat.api.views import chat_router, models_router
from codebase.api.router import router as codebase_router

from . import __version__

api = NinjaAPI(version=__version__, title="Daiv API", docs_url="/docs/", urls_namespace="api")
api.add_router("/accounts", accounts_router)
api.add_router("/codebase", codebase_router)
api.add_router("/chat", chat_router)
api.add_router("/models", models_router)
api.add_router("/jobs", jobs_router)
api.add_router("/oauth", oauth_router)
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
uv run pytest tests/unit_tests/accounts/test_api.py -v
```

Expected: 8 passed.

- [ ] **Step 7: Commit**

```bash
git add daiv/accounts/api/ daiv/daiv/api.py tests/unit_tests/accounts/test_api.py
git commit -m "feat(accounts): add user search api endpoint for autocomplete"
```

---

## Task 5: Form accepts subscribers with owner exclusion

**Files:**
- Modify: `daiv/schedules/forms.py`
- Test: `tests/unit_tests/schedules/test_forms.py`

- [ ] **Step 1: Add failing form tests**

Append to `tests/unit_tests/schedules/test_forms.py`:

```python
from accounts.models import User
from schedules.models import ScheduledJob


@pytest.mark.django_db
class TestScheduledJobCreateFormSubscribers:
    def _sub_user(self, username="alice"):
        return User.objects.create_user(
            username=username, email=f"{username}@t.com", password="x",  # noqa: S106
        )

    def test_form_accepts_subscribers(self, member_user):
        alice = self._sub_user("alice")
        form = ScheduledJobCreateForm(
            data=_valid_data(subscribers=[alice.pk]), owner=member_user,
        )
        assert form.is_valid(), form.errors
        job = form.save(commit=False)
        job.user = member_user
        job.save()
        form.save_m2m()
        assert list(job.subscribers.all()) == [alice]

    def test_owner_excluded_from_queryset(self, member_user):
        form = ScheduledJobCreateForm(owner=member_user)
        qs_pks = list(form.fields["subscribers"].queryset.values_list("pk", flat=True))
        assert member_user.pk not in qs_pks

    def test_inactive_users_excluded_from_queryset(self, member_user):
        inactive = self._sub_user("bob")
        inactive.is_active = False
        inactive.save()
        form = ScheduledJobCreateForm(owner=member_user)
        qs_pks = list(form.fields["subscribers"].queryset.values_list("pk", flat=True))
        assert inactive.pk not in qs_pks

    def test_submitting_owner_pk_in_subscribers_is_rejected(self, member_user):
        form = ScheduledJobCreateForm(
            data=_valid_data(subscribers=[member_user.pk]), owner=member_user,
        )
        # Owner is excluded from the queryset, so ModelMultipleChoiceField rejects.
        assert not form.is_valid()
        assert "subscribers" in form.errors

    def test_accepts_empty_subscribers(self, member_user):
        form = ScheduledJobCreateForm(data=_valid_data(), owner=member_user)
        assert form.is_valid(), form.errors
```

Because the existing `_valid_data` helper does not include `subscribers`, the overrides kwarg (`**overrides`) already lets us inject it by passing `subscribers=[…]`. If Django's ModelForm requires the field key to be present even when empty, the absence is handled by the field's `required=False` (the M2M has `blank=True`).

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit_tests/schedules/test_forms.py::TestScheduledJobCreateFormSubscribers -v
```

Expected: FAIL — `TypeError: ScheduledJobCreateForm.__init__() got an unexpected keyword argument 'owner'` (or field missing).

- [ ] **Step 3: Update the form**

Edit `daiv/schedules/forms.py`. Replace the entire file contents:

```python
from django import forms

from accounts.models import User
from schedules.models import Frequency, ScheduledJob


class ScheduledJobCreateForm(forms.ModelForm):
    """Form for creating a new schedule. Excludes ``is_enabled`` since new schedules are always enabled."""

    class Meta:
        model = ScheduledJob
        fields = [
            "name", "prompt", "repo_id", "ref",
            "frequency", "cron_expression", "time",
            "use_max", "notify_on", "subscribers",
        ]
        widgets = {
            # Rendered by the custom Alpine picker partial; a multiple select is
            # the simplest underlying widget for ModelMultipleChoiceField.
            "subscribers": forms.SelectMultiple(attrs={"class": "hidden"}),
        }

    def __init__(self, *args, owner=None, **kwargs):
        super().__init__(*args, **kwargs)
        if "subscribers" in self.fields:
            qs = User.objects.filter(is_active=True)
            if owner is not None:
                qs = qs.exclude(pk=owner.pk)
            self.fields["subscribers"].queryset = qs
            self.fields["subscribers"].required = False

    def _clean_conditional_fields(self, cleaned_data: dict) -> dict:
        """Clear fields that are irrelevant for the selected frequency."""
        frequency = cleaned_data.get("frequency")
        if frequency != Frequency.CUSTOM:
            cleaned_data["cron_expression"] = ""
        if frequency in (Frequency.HOURLY, Frequency.CUSTOM):
            cleaned_data["time"] = None
        return cleaned_data

    def clean(self):
        cleaned_data = super().clean()
        return self._clean_conditional_fields(cleaned_data)

    def save(self, commit: bool = True) -> ScheduledJob:
        instance = super().save(commit=False)
        instance.compute_next_run()
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class ScheduledJobUpdateForm(ScheduledJobCreateForm):
    """Form for editing an existing schedule. Adds ``is_enabled`` toggle."""

    class Meta(ScheduledJobCreateForm.Meta):
        fields = [*ScheduledJobCreateForm.Meta.fields, "is_enabled"]
```

Note: the original `save()` returned before `save_m2m()` was ever called because `form.save(commit=True)` on a `ModelForm` with M2M fields requires saving M2M after the instance is persisted. The override above calls `self.save_m2m()` when `commit=True`. Without this, subscribers would silently be dropped on save.

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/unit_tests/schedules/test_forms.py -v
```

Expected: all green (existing tests continue to pass — they don't pass `owner`, and the field is optional).

- [ ] **Step 5: Commit**

```bash
git add daiv/schedules/forms.py tests/unit_tests/schedules/test_forms.py
git commit -m "feat(schedules): accept subscribers in create/update forms"
```

---

## Task 6: Views pass `owner` to the form

**Files:**
- Modify: `daiv/schedules/views.py:45-69`
- Test: `tests/unit_tests/schedules/test_views.py`

- [ ] **Step 1: Add failing view wiring tests**

Append to `tests/unit_tests/schedules/test_views.py` (inside a new class — the file already imports `User` and `reverse`):

```python
@pytest.mark.django_db
class TestScheduleCreateViewSubscribers:
    def test_owner_passed_to_form_on_create(self, member_client, member_user):
        alice = User.objects.create_user(username="alice", email="a@t.com", password="x")  # noqa: S106
        payload = {
            "name": "Daily", "prompt": "p", "repo_id": "x/y", "ref": "",
            "frequency": "daily", "cron_expression": "", "time": "09:00",
            "use_max": "false", "notify_on": "never",
            "subscribers": [str(alice.pk)],
        }
        response = member_client.post(reverse("schedule_create"), data=payload)
        assert response.status_code in (302, 200), response.content.decode()[:400]
        schedule = ScheduledJob.objects.get(name="Daily")
        assert list(schedule.subscribers.all()) == [alice]
        assert schedule.user == member_user

    def test_owner_rejected_as_own_subscriber_on_create(self, member_client, member_user):
        payload = {
            "name": "Daily", "prompt": "p", "repo_id": "x/y", "ref": "",
            "frequency": "daily", "cron_expression": "", "time": "09:00",
            "use_max": "false", "notify_on": "never",
            "subscribers": [str(member_user.pk)],
        }
        response = member_client.post(reverse("schedule_create"), data=payload)
        # Form invalid → re-renders, no schedule persisted
        assert response.status_code == 200
        assert not ScheduledJob.objects.filter(name="Daily").exists()


@pytest.mark.django_db
class TestScheduleUpdateViewSubscribers:
    def test_owner_passed_to_form_on_update(self, member_client, member_user, schedule):
        alice = User.objects.create_user(username="alice", email="a@t.com", password="x")  # noqa: S106
        payload = {
            "name": schedule.name, "prompt": schedule.prompt, "repo_id": schedule.repo_id, "ref": schedule.ref,
            "frequency": schedule.frequency, "cron_expression": "",
            "time": "09:00", "use_max": "false", "notify_on": "never",
            "is_enabled": "true", "subscribers": [str(alice.pk)],
        }
        response = member_client.post(reverse("schedule_update", args=[schedule.pk]), data=payload)
        assert response.status_code in (302, 200), response.content.decode()[:400]
        schedule.refresh_from_db()
        assert list(schedule.subscribers.all()) == [alice]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit_tests/schedules/test_views.py::TestScheduleCreateViewSubscribers tests/unit_tests/schedules/test_views.py::TestScheduleUpdateViewSubscribers -v
```

Expected: FAIL — the form will fail to validate or silently accept the owner because the view does not pass `owner`.

- [ ] **Step 3: Update `ScheduleCreateView` and `ScheduleUpdateView`**

Edit `daiv/schedules/views.py`. Add `get_form_kwargs` overrides to both create and update views. Locate the two class bodies (around lines 45-69) and replace them with:

```python
class ScheduleCreateView(BreadcrumbMixin, _ScheduleOwnerMixin, SuccessMessageMixin, LoginRequiredMixin, CreateView):
    model = ScheduledJob
    form_class = ScheduledJobCreateForm
    template_name = "schedules/schedule_form.html"
    success_url = reverse_lazy("schedule_list")
    success_message = "Schedule '%(name)s' created."
    breadcrumbs = [{"label": "Schedules", "url": reverse_lazy("schedule_list")}, {"label": "New schedule", "url": None}]

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["owner"] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.instance.user = self.request.user
        return super().form_valid(form)


class ScheduleUpdateView(BreadcrumbMixin, _ScheduleOwnerMixin, SuccessMessageMixin, LoginRequiredMixin, UpdateView):
    model = ScheduledJob
    form_class = ScheduledJobUpdateForm
    template_name = "schedules/schedule_form.html"
    success_url = reverse_lazy("schedule_list")
    success_message = "Schedule '%(name)s' updated."

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["owner"] = self.object.user
        return kwargs

    def get_breadcrumbs(self):
        return [
            {"label": "Schedules", "url": reverse("schedule_list")},
            {"label": f'"{self.object.name}"', "url": None},
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/unit_tests/schedules/test_views.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add daiv/schedules/views.py tests/unit_tests/schedules/test_views.py
git commit -m "feat(schedules): pass owner to form to scope subscriber picker"
```

---

## Task 7: Subscriber picker UI in the schedule form

**Files:**
- Create: `daiv/schedules/static/schedules/js/subscriber-picker.js`
- Create: `daiv/schedules/templates/schedules/_subscriber_picker.html`
- Modify: `daiv/schedules/templates/schedules/schedule_form.html`

This task is pure UI glue — assertions run via Django's template rendering test. No new Python logic.

- [ ] **Step 1: Add a template-render smoke test**

Append to `tests/unit_tests/schedules/test_views.py`:

```python
@pytest.mark.django_db
class TestSchedulePickerRendering:
    def test_create_form_renders_picker_markers(self, member_client):
        response = member_client.get(reverse("schedule_create"))
        html = response.content.decode()
        assert 'id="id_subscribers"' in html
        assert "subscriberPicker" in html  # Alpine data function
        assert "Subscribers" in html

    def test_update_form_prefills_selected_subscribers(self, member_client, member_user, schedule):
        alice = User.objects.create_user(username="alice", email="a@t.com", password="x")  # noqa: S106
        schedule.subscribers.add(alice)
        response = member_client.get(reverse("schedule_update", args=[schedule.pk]))
        html = response.content.decode()
        assert "alice" in html
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/unit_tests/schedules/test_views.py::TestSchedulePickerRendering -v
```

Expected: FAIL — markers not yet in template.

- [ ] **Step 3: Write the Alpine picker component**

Create `daiv/schedules/static/schedules/js/subscriber-picker.js`:

```javascript
/**
 * Alpine component: multi-select user chip picker backed by /api/accounts/users/search.
 *
 * Usage (inside a form):
 *   <div x-data="subscriberPicker({ initial: {{ initial_json|safe }} })">
 *     <select name="subscribers" id="id_subscribers" multiple class="hidden">
 *       <template x-for="u in selected" :key="u.id">
 *         <option :value="u.id" selected x-text="u.username"></option>
 *       </template>
 *     </select>
 *     <input type="text" @input="search($event.target.value)" ...>
 *     <!-- chips list, results dropdown -->
 *   </div>
 */
document.addEventListener("alpine:init", () => {
    Alpine.data("subscriberPicker", ({ initial = [] } = {}) => ({
        selected: [...initial],
        query: "",
        results: [],
        isLoading: false,
        _timer: null,
        _controller: null,

        isSelected(id) {
            return this.selected.some((u) => u.id === id);
        },

        add(user) {
            if (!this.isSelected(user.id)) {
                this.selected.push(user);
            }
            this.query = "";
            this.results = [];
        },

        remove(id) {
            this.selected = this.selected.filter((u) => u.id !== id);
        },

        search(value) {
            this.query = value;
            clearTimeout(this._timer);
            if (value.length < 2) {
                this.results = [];
                return;
            }
            this._timer = setTimeout(() => this._fetch(value), 300);
        },

        async _fetch(value) {
            this._controller?.abort();
            this._controller = new AbortController();
            this.isLoading = true;
            try {
                const excludeIds = this.selected.map((u) => u.id).join(",");
                const params = new URLSearchParams({ q: value });
                if (excludeIds) params.set("exclude", excludeIds);
                const resp = await fetch(
                    "/api/accounts/users/search?" + params.toString(),
                    { signal: this._controller.signal },
                );
                this.results = resp.ok ? await resp.json() : [];
            } catch (e) {
                if (e.name !== "AbortError") this.results = [];
            } finally {
                this.isLoading = false;
            }
        },
    }));
});
```

- [ ] **Step 4: Write the picker partial**

Create `daiv/schedules/templates/schedules/_subscriber_picker.html`:

```django
{% load i18n %}
<div class="mt-6 border-t border-white/[0.06] pt-6"
     x-data="subscriberPicker({ initial: {{ subscriber_initial_json|default:'[]'|safe }} })">
    <label class="block text-[15px] font-medium text-gray-400">{% trans "Subscribers" %}</label>
    <p class="mt-1 text-sm text-gray-400">{% trans "Other users CC'd on this schedule's finish notifications." %}</p>

    <!-- Hidden multi-select — the form's ModelMultipleChoiceField reads this. -->
    <select name="{{ form.subscribers.html_name }}" id="{{ form.subscribers.id_for_label }}" multiple class="hidden">
        <template x-for="u in selected" :key="u.id">
            <option :value="u.id" selected x-text="u.username"></option>
        </template>
    </select>

    <!-- Selected chips -->
    <div class="mt-3 flex flex-wrap gap-2">
        <template x-for="u in selected" :key="u.id">
            <span class="inline-flex items-center gap-1.5 rounded-full bg-white/[0.08] px-3 py-1 text-sm text-gray-200">
                <span x-text="u.name || u.username"></span>
                <button type="button" @click="remove(u.id)" class="text-gray-400 hover:text-white" aria-label="Remove">
                    <svg class="h-3.5 w-3.5" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
                        <path d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z"/>
                    </svg>
                </button>
            </span>
        </template>
        <p x-show="selected.length === 0" class="text-sm text-gray-500">{% trans "No subscribers yet." %}</p>
    </div>

    <!-- Search input -->
    <div class="relative mt-3">
        <input type="text" :value="query" @input="search($event.target.value)"
               placeholder="{% trans 'Search users by name, email, or username...' %}"
               autocomplete="off" class="w-full">
        <div x-show="results.length > 0 || isLoading" x-cloak
             class="absolute z-20 mt-1 max-h-56 w-full overflow-auto rounded-xl border border-white/[0.06] bg-[#0d1117] shadow-lg">
            <ul>
                <template x-for="u in results" :key="u.id">
                    <li @click="add(u)"
                        class="cursor-pointer px-3 py-2 text-[15px] text-gray-300 hover:bg-white/[0.06] hover:text-white">
                        <span x-text="u.username"></span>
                        <span x-show="u.name" x-text="' — ' + u.name" class="text-gray-400"></span>
                        <span class="ml-2 text-xs text-gray-500" x-text="u.email"></span>
                    </li>
                </template>
                <li x-show="isLoading" class="px-3 py-2 text-sm text-gray-400">{% trans "Searching..." %}</li>
            </ul>
        </div>
    </div>

    {% if form.subscribers.errors %}
    <p class="mt-1.5 text-sm text-red-400">{{ form.subscribers.errors.0 }}</p>
    {% endif %}
</div>
```

- [ ] **Step 5: Wire the picker into the schedule form template**

Edit `daiv/schedules/templates/schedules/schedule_form.html`. Two changes:

**5a.** Replace the `{% block alpine_plugins %}` to also load the picker script. Replace lines 6-11:

```django
{% block alpine_plugins %}
<script defer src="https://cdn.jsdelivr.net/npm/@alpinejs/ui@3.15.11/dist/cdn.min.js"
        integrity="sha384-USgPxo+ohBkt/xxOPsfCDC5BYAwgFHCatL+RFkcPCWWkvKSp5KzH52tUZZ7taB/c"
        crossorigin="anonymous"></script>
<script defer src="{% static 'codebase/js/repo-search.js' %}"></script>
<script defer src="{% static 'schedules/js/subscriber-picker.js' %}"></script>
{% endblock %}
```

**5b.** Insert the picker partial after the Notifications block and before the Enabled block. Locate the `<!-- Notifications -->` block (ends around line 162) and right after its closing `</div>`, add:

```django
{% include "schedules/_subscriber_picker.html" %}
```

**5c.** Build the `subscriber_initial_json` context from the view. Edit `daiv/schedules/views.py` and add a helper on the shared mixin so both create and update pass it. Add an override on `ScheduleCreateView` and `ScheduleUpdateView`:

```python
import json

# ...

def _subscriber_initial_json(schedule) -> str:
    if schedule is None:
        return "[]"
    rows = [
        {"id": u.pk, "username": u.username, "name": u.name, "email": u.email}
        for u in schedule.subscribers.all()
    ]
    return json.dumps(rows)
```

Add `get_context_data` to both views:

```python
class ScheduleCreateView(...):
    ...
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["subscriber_initial_json"] = "[]"
        return context


class ScheduleUpdateView(...):
    ...
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["subscriber_initial_json"] = _subscriber_initial_json(self.object)
        return context
```

Place `_subscriber_initial_json` as a module-level function above the view classes and import `json` at the top of `daiv/schedules/views.py`.

- [ ] **Step 6: Run tests to verify they pass**

```bash
uv run pytest tests/unit_tests/schedules/test_views.py::TestSchedulePickerRendering -v
```

Expected: 2 passed.

- [ ] **Step 7: Smoke-test the full form flow**

```bash
uv run pytest tests/unit_tests/schedules/ -v
```

Expected: all green.

- [ ] **Step 8: Run lint**

```bash
make lint-fix
```

Expected: no errors.

- [ ] **Step 9: Commit**

```bash
git add daiv/schedules/static/ daiv/schedules/templates/schedules/_subscriber_picker.html daiv/schedules/templates/schedules/schedule_form.html daiv/schedules/views.py tests/unit_tests/schedules/test_views.py
git commit -m "feat(schedules): add subscriber chip picker to schedule form"
```

---

## Task 8: ScheduleUnsubscribeView

**Files:**
- Modify: `daiv/schedules/views.py`
- Modify: `daiv/schedules/urls.py`
- Test: `tests/unit_tests/schedules/test_views.py`

- [ ] **Step 1: Add the failing view tests**

Append to `tests/unit_tests/schedules/test_views.py`:

```python
@pytest.mark.django_db
class TestScheduleUnsubscribeView:
    def _subscriber(self, username="sub1"):
        return User.objects.create_user(username=username, email=f"{username}@t.com", password="x")  # noqa: S106

    def test_subscriber_can_unsubscribe(self, schedule):
        sub = self._subscriber()
        schedule.subscribers.add(sub)

        client = Client()
        client.force_login(sub)
        response = client.post(reverse("schedule_unsubscribe", args=[schedule.pk]))
        assert response.status_code == 302
        schedule.refresh_from_db()
        assert sub not in schedule.subscribers.all()

    def test_non_subscriber_gets_404(self, schedule):
        other = self._subscriber("other")
        client = Client()
        client.force_login(other)
        response = client.post(reverse("schedule_unsubscribe", args=[schedule.pk]))
        assert response.status_code == 404

    def test_owner_gets_404(self, member_client, schedule):
        # Owner is not in subscribers → 404 (matches non-subscriber branch).
        response = member_client.post(reverse("schedule_unsubscribe", args=[schedule.pk]))
        assert response.status_code == 404

    def test_rejects_get(self, schedule):
        sub = self._subscriber()
        schedule.subscribers.add(sub)
        client = Client()
        client.force_login(sub)
        response = client.get(reverse("schedule_unsubscribe", args=[schedule.pk]))
        assert response.status_code == 405

    def test_next_redirect_honored_when_safe(self, schedule):
        sub = self._subscriber()
        schedule.subscribers.add(sub)
        client = Client()
        client.force_login(sub)
        response = client.post(
            reverse("schedule_unsubscribe", args=[schedule.pk]),
            data={"next": "/dashboard/activity/"},
        )
        assert response.status_code == 302
        assert response.url == "/dashboard/activity/"

    def test_unsafe_next_falls_back_to_activity_list(self, schedule):
        sub = self._subscriber()
        schedule.subscribers.add(sub)
        client = Client()
        client.force_login(sub)
        response = client.post(
            reverse("schedule_unsubscribe", args=[schedule.pk]),
            data={"next": "https://evil.example.com/phish"},
        )
        assert response.status_code == 302
        assert response.url == reverse("activity_list")

    def test_unauthenticated_redirects_to_login(self, schedule):
        client = Client()
        response = client.post(reverse("schedule_unsubscribe", args=[schedule.pk]))
        assert response.status_code == 302
        assert "/login" in response.url or "/accounts/" in response.url

    def test_nonexistent_schedule_returns_404(self, member_client):
        response = member_client.post(reverse("schedule_unsubscribe", args=[99999]))
        assert response.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit_tests/schedules/test_views.py::TestScheduleUnsubscribeView -v
```

Expected: `NoReverseMatch: Reverse for 'schedule_unsubscribe' not found`.

- [ ] **Step 3: Add the view**

Edit `daiv/schedules/views.py`. Add these imports near the top (keep existing imports grouped as they are):

```python
from django.http import Http404
from django.utils.http import url_has_allowed_host_and_scheme
```

Append a new view class after `ScheduleDeleteView`:

```python
class ScheduleUnsubscribeView(LoginRequiredMixin, View):
    """Let a subscriber remove themselves from a schedule. Owner-only path is disallowed."""

    http_method_names = ["post"]

    def post(self, request, pk):
        schedule = get_object_or_404(ScheduledJob, pk=pk)
        if not schedule.subscribers.filter(pk=request.user.pk).exists():
            raise Http404
        schedule.subscribers.remove(request.user)
        messages.success(request, f"You are no longer subscribed to '{schedule.name}'.")
        next_url = request.POST.get("next", "")
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            return redirect(next_url)
        return redirect("activity_list")
```

- [ ] **Step 4: Register the URL**

Edit `daiv/schedules/urls.py`. Add the import and path:

```python
from django.urls import path

from schedules.views import (
    ScheduleCreateView,
    ScheduleDeleteView,
    ScheduleListView,
    ScheduleRunNowView,
    ScheduleToggleView,
    ScheduleUnsubscribeView,
    ScheduleUpdateView,
)

urlpatterns = [
    path("", ScheduleListView.as_view(), name="schedule_list"),
    path("create/", ScheduleCreateView.as_view(), name="schedule_create"),
    path("<int:pk>/edit/", ScheduleUpdateView.as_view(), name="schedule_update"),
    path("<int:pk>/delete/", ScheduleDeleteView.as_view(), name="schedule_delete"),
    path("<int:pk>/toggle/", ScheduleToggleView.as_view(), name="schedule_toggle"),
    path("<int:pk>/run/", ScheduleRunNowView.as_view(), name="schedule_run_now"),
    path("<int:pk>/unsubscribe/", ScheduleUnsubscribeView.as_view(), name="schedule_unsubscribe"),
]
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/unit_tests/schedules/test_views.py::TestScheduleUnsubscribeView -v
```

Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add daiv/schedules/views.py daiv/schedules/urls.py tests/unit_tests/schedules/test_views.py
git commit -m "feat(schedules): add self-unsubscribe endpoint for subscribers"
```

---

## Task 9: Activity detail unsubscribe button + non-owner schedule rendering

**Files:**
- Modify: `daiv/activity/views.py`
- Modify: `daiv/activity/templates/activity/activity_detail.html:86-94`
- Test: `tests/unit_tests/activity/test_views.py`

- [ ] **Step 1: Add failing context/template tests**

Append to `tests/unit_tests/activity/test_views.py`:

```python
@pytest.mark.django_db
class TestActivityDetailSubscriberContext:
    def _fixture(self):
        owner = User.objects.create_user(username="own", email="own@t.com", password="x")  # noqa: S106
        sub = User.objects.create_user(username="sub", email="sub@t.com", password="x")  # noqa: S106
        schedule = ScheduledJob.objects.create(
            user=owner, name="s", prompt="p", repo_id="x/y",
            frequency=Frequency.DAILY, time="12:00",
        )
        schedule.subscribers.add(sub)
        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE, repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL, scheduled_job=schedule, user=owner,
        )
        return owner, sub, schedule, activity

    def test_is_subscriber_true_for_subscriber(self):
        _, sub, _, activity = self._fixture()
        client = Client()
        client.force_login(sub)
        response = client.get(reverse("activity_detail", args=[activity.pk]))
        assert response.context["is_subscriber"] is True

    def test_is_subscriber_false_for_owner(self):
        owner, _, _, activity = self._fixture()
        client = Client()
        client.force_login(owner)
        response = client.get(reverse("activity_detail", args=[activity.pk]))
        assert response.context["is_subscriber"] is False

    def test_unsubscribe_button_visible_to_subscriber(self):
        _, sub, schedule, activity = self._fixture()
        client = Client()
        client.force_login(sub)
        response = client.get(reverse("activity_detail", args=[activity.pk]))
        html = response.content.decode()
        assert reverse("schedule_unsubscribe", args=[schedule.pk]) in html
        assert "Unsubscribe" in html

    def test_unsubscribe_button_hidden_for_owner(self):
        owner, _, _, activity = self._fixture()
        client = Client()
        client.force_login(owner)
        response = client.get(reverse("activity_detail", args=[activity.pk]))
        html = response.content.decode()
        assert "schedule_unsubscribe" not in html
        assert "Unsubscribe" not in html

    def test_schedule_name_is_plain_text_for_subscriber(self):
        _, sub, schedule, activity = self._fixture()
        client = Client()
        client.force_login(sub)
        response = client.get(reverse("activity_detail", args=[activity.pk]))
        html = response.content.decode()
        # No link to schedule_update for a non-owner
        assert reverse("schedule_update", args=[schedule.pk]) not in html
        # Schedule name still shown
        assert schedule.name in html

    def test_schedule_name_is_link_for_owner(self):
        owner, _, schedule, activity = self._fixture()
        client = Client()
        client.force_login(owner)
        response = client.get(reverse("activity_detail", args=[activity.pk]))
        html = response.content.decode()
        assert reverse("schedule_update", args=[schedule.pk]) in html
```

Add `from schedules.models import Frequency, ScheduledJob` to the imports at the top of the test file if not already present.

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/unit_tests/activity/test_views.py::TestActivityDetailSubscriberContext -v
```

Expected: failures — `is_subscriber` not in context; button markup absent.

- [ ] **Step 3: Compute `is_subscriber` in the view**

Edit `daiv/activity/views.py`. Replace `ActivityDetailView.get_context_data` (lines 77-81):

```python
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        activity: Activity = context["activity"]
        context["is_in_flight"] = activity.status not in ActivityStatus.terminal()

        user = self.request.user
        schedule = activity.scheduled_job
        is_owner_or_admin = user.is_admin or (schedule is not None and schedule.user_id == user.pk)
        context["is_schedule_owner_or_admin"] = is_owner_or_admin
        context["is_subscriber"] = bool(
            schedule is not None
            and schedule.user_id != user.pk
            and schedule.subscribers.filter(pk=user.pk).exists()
        )
        return context
```

- [ ] **Step 4: Update the template**

Edit `daiv/activity/templates/activity/activity_detail.html`. Locate the schedule-name block (currently lines 86-94):

```django
                {% if activity.scheduled_job %}
                <div class="flex items-center gap-2">
                    <span class="text-gray-400">Schedule:</span>
                    <a href="{% url 'schedule_update' activity.scheduled_job.pk %}"
                       class="text-gray-300 underline decoration-gray-600 hover:text-white hover:decoration-gray-400">
                        {{ activity.scheduled_job.name }}
                    </a>
                </div>
                {% endif %}
```

Replace with:

```django
                {% if activity.scheduled_job %}
                <div class="flex items-center gap-2">
                    <span class="text-gray-400">Schedule:</span>
                    {% if is_schedule_owner_or_admin %}
                    <a href="{% url 'schedule_update' activity.scheduled_job.pk %}"
                       class="text-gray-300 underline decoration-gray-600 hover:text-white hover:decoration-gray-400">
                        {{ activity.scheduled_job.name }}
                    </a>
                    {% else %}
                    <span class="text-gray-300">{{ activity.scheduled_job.name }}</span>
                    {% endif %}
                    {% if is_subscriber %}
                    <form method="post" action="{% url 'schedule_unsubscribe' activity.scheduled_job.pk %}" class="inline">
                        {% csrf_token %}
                        <input type="hidden" name="next" value="{{ request.get_full_path }}">
                        <button type="submit" class="ml-2 rounded-md bg-white/[0.04] px-2 py-0.5 text-xs text-gray-400 hover:bg-white/[0.08] hover:text-white">
                            Unsubscribe
                        </button>
                    </form>
                    {% endif %}
                </div>
                {% endif %}
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/unit_tests/activity/test_views.py -v
```

Expected: all green.

- [ ] **Step 6: Full regression test**

```bash
make test
```

Expected: no new failures.

- [ ] **Step 7: Lint**

```bash
make lint-fix
make lint-typing
```

Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add daiv/activity/views.py daiv/activity/templates/activity/activity_detail.html tests/unit_tests/activity/test_views.py
git commit -m "feat(activity): expose unsubscribe button to subscribers on detail page"
```

---

## Task 10: Documentation

**Files:**
- Modify: `docs/features/scheduled-jobs.md`

- [ ] **Step 1: Rewrite the "Managing schedules" section and add a Subscribers section**

Edit `docs/features/scheduled-jobs.md`. Locate the "Managing schedules" section (around line 70) and add a new "Subscribers" subsection immediately after it. Paste this block between the existing "Managing schedules" section and the "Relationship with the Jobs API" section:

```markdown
## Subscribers

Schedule owners can CC other DAIV users on the finish notifications for their schedules. Subscribers:

- Receive the same notification as the owner whenever the schedule's `Notify on` condition matches (e.g., "On success only" or "Always").
- Gain **read-only** access to the activities produced by the schedule — they can click through from the notification and view the activity detail, output, and code changes.
- Do **not** see the schedule itself in their own Scheduled Jobs list, and cannot edit, pause, run, or delete it.

### Adding subscribers

On the schedule form, use the **Subscribers** search to find a user by username, email, or name. Click a result to add them as a chip. Remove a chip with the × button. Save the schedule to persist the subscriber list.

Only the owner (or an admin) can change a schedule's subscribers.

### Self-unsubscribe

When a subscriber opens an activity produced by a schedule they are CC'd on, the activity detail page shows an **Unsubscribe** button next to the schedule name. Clicking it removes the subscriber from that schedule — no owner action needed.

!!! note "Notification preferences"
    All subscribers inherit the schedule's `Notify on` setting. There is no per-subscriber override today. If the owner changes the setting, every subscriber's notification behavior changes with it.
```

- [ ] **Step 2: Verify the mkdocs build still passes (if available)**

```bash
uv run mkdocs build --strict 2>&1 | tail -20 || echo "mkdocs not configured — skipping"
```

Expected: either clean build or the "skipping" message.

- [ ] **Step 3: Commit**

```bash
git add docs/features/scheduled-jobs.md
git commit -m "docs(schedules): document subscribers and self-unsubscribe"
```

---

## Final verification

- [ ] **Run full test suite**

```bash
make test
```

Expected: all green.

- [ ] **Run lint + typing**

```bash
make lint-fix
make lint-typing
```

Expected: clean.

- [ ] **Manually verify the happy path** (if you have a local dev server)

1. Log in as user A. Create a schedule with `Notify on = Always`.
2. Search for user B in the Subscribers picker; add them; save.
3. Trigger a run-now on the schedule.
4. Log in as user B. Confirm the notification appears in the bell.
5. Click the notification; confirm the activity detail loads (no 404).
6. Click **Unsubscribe** next to the schedule name.
7. Re-run the schedule and confirm user B no longer receives a notification.
