# Schedule Subscribers — Design

## Problem

When a scheduled job finishes, only the owner is notified. Stakeholders who care about the outcome (team members, project leads) must either ask the owner to forward results or be added as co-owners — neither is an option today. We want owners to CC other DAIV users on the same finish-notifications.

## Summary

Add an optional many-to-many relation between `ScheduledJob` and `User` (subscribers). When an activity tied to a subscribed schedule finishes and the schedule's `notify_on` condition matches, fan the same notification out to the owner **and** each subscriber. Subscribers gain read-only access to the linked activity pages (so the notification link works), but do not see the schedule in their list or gain any management rights. Subscribers can self-remove via an "Unsubscribe" control rendered on each activity page they landed on from a notification.

Explicitly **in** scope:

- Data model: `ScheduledJob.subscribers` M2M.
- Fanout in `notifications.signals.on_activity_finished`.
- Subscriber-aware activity visibility (`Activity.objects.by_owner`).
- Owner-side UI for managing subscribers on the schedule create/edit form.
- Subscriber-side self-unsubscribe control on the activity detail page.

Explicitly **out of** scope (YAGNI):

- Per-subscriber `notify_on` override — everyone inherits the schedule's setting.
- Subscribers seeing the schedule in their own schedule list.
- Subscribers getting any edit / toggle / run / delete rights.
- External email recipients (non-DAIV users).
- Signed unsubscribe tokens in email bodies.
- Auditing who added/removed whom.

## Design decisions (locked)

| # | Decision | Rationale |
|---|---|---|
| 1 | Subscribers are existing DAIV users (no external emails) | Leverages existing `UserChannelBinding` plumbing; keeps permissions trivial. |
| 2 | All subscribers inherit the schedule's `notify_on` | Matches "CC'd on the same events"; no preferences matrix. Future per-user override is additive. |
| 3 | Subscribers get read access to linked activities; no schedule list visibility | Notification links must work. Owner asked for listeners, not co-owners. |
| 4 | Self-unsubscribe exposed as a button on the activity detail page (not a new settings view) | Point-of-friction is exactly where the recipient lands — naturally dismissable. |

## Data model

### `schedules/models.py`

Add one field to `ScheduledJob`:

```python
subscribers = models.ManyToManyField(
    settings.AUTH_USER_MODEL,
    blank=True,
    related_name="subscribed_schedules",
    verbose_name=_("subscribers"),
    help_text=_("Other users CC'd on this schedule's finish notifications."),
)
```

- Cascade behavior: Django auto-manages the join table; deleting either side removes the row.
- No `through` model — nothing to carry beyond the pair.
- `related_name="subscribed_schedules"` gives us `user.subscribed_schedules.all()`.

Migration: `schedules/migrations/0008_scheduledjob_subscribers.py` (auto-generated).

### Invariants enforced in code

- **Owner is never a subscriber of their own schedule.** Enforced in form (`clean_subscribers` strips the owner) and in fanout (de-dupe by user pk so if the invariant is ever violated we still don't double-notify).
- **No self-subscription from outside.** The only write path is the owner's schedule form. Subscribers cannot add themselves.

## Notification fanout

In `daiv/notifications/signals.py::on_activity_finished`:

Replace the single `notify(recipient=schedule.user, …)` call with a loop:

```python
recipients: dict[int, User] = {schedule.user_id: schedule.user}
for sub in schedule.subscribers.all():
    recipients.setdefault(sub.pk, sub)  # de-dupe owner-as-subscriber

for recipient in recipients.values():
    try:
        notify(
            recipient=recipient,
            event_type="schedule.finished",
            source_type="activity.Activity",
            source_id=str(activity.pk),
            subject=_render_subject(schedule, activity),
            body=_render_body(schedule, activity),
            link_url=reverse("activity_detail", args=[activity.pk]),
            channels=channels,
            context={"status": activity.status, "schedule_name": schedule.name},
        )
    except Exception:
        logger.exception(
            "Failed to create notification for activity %s, recipient pk=%s",
            activity.pk, recipient.pk,
        )
```

Notes:

- **Failure isolation:** wrap the `notify()` call per recipient so one recipient's failure doesn't prevent the rest.
- **No template change:** subject/body are the same for owner and subscribers — the subscribers inherit `notify_on` so the "Your scheduled job 'X' finished" phrasing stays accurate (it *is* a scheduled job they've subscribed to).
- **Query efficiency:** `schedule.subscribers.all()` issues one query per activity finish. Acceptable.

## Activity visibility

In `daiv/activity/models.py::ActivityManager.by_owner`:

```python
def by_owner(self, user: User) -> models.QuerySet[Activity]:
    if user.is_admin:
        return self.all()
    return self.filter(
        models.Q(user=user)
        | models.Q(external_username=user.username)
        | models.Q(scheduled_job__subscribers=user)
    ).distinct()
```

The `.distinct()` is required because the new `subscribers` join can produce duplicate rows when the same user matches multiple predicates. Admin branch unchanged.

This single change propagates to:

- `ActivityListView` (list)
- `ActivityDetailView` (detail)
- Any other call site of `Activity.objects.by_owner()`

The activity detail template already renders `activity.scheduled_job` as a link to `schedule_update`. A non-owning subscriber clicking that link will hit the existing `_ScheduleOwnerMixin` queryset and get a 404 — **intentional**, consistent with "no management surface for subscribers." The template will be updated to render the schedule name as plain text (not a link) when `activity.scheduled_job.user != request.user and not user.is_admin`.

## Owner UI: managing subscribers

### Form

Add `subscribers` to both `ScheduledJobCreateForm` and `ScheduledJobUpdateForm`:

```python
class Meta:
    fields = [
        "name", "prompt", "repo_id", "ref",
        "frequency", "cron_expression", "time",
        "use_max", "notify_on", "subscribers",
    ]
```

Custom widget / cleaning:

- Limit the queryset to **active users excluding the owner**. The owner is exposed via `self.instance.user` on update and via the view (`self.request.user`) on create. Because the form doesn't know the user at queryset-build time, override `__init__` to take an `owner` kwarg and narrow `fields["subscribers"].queryset` accordingly.
- `clean_subscribers`: strip the owner if somehow present, strip inactive users.

```python
def __init__(self, *args, owner=None, **kwargs):
    super().__init__(*args, **kwargs)
    if "subscribers" in self.fields:
        qs = self.fields["subscribers"].queryset.filter(is_active=True)
        if owner is not None:
            qs = qs.exclude(pk=owner.pk)
        self.fields["subscribers"].queryset = qs
```

Views pass `owner=self.request.user` (create) or `owner=self.object.user` (update) via `get_form_kwargs`.

### Template

A new section on `schedule_form.html`, placed after the Notifications block. Mirrors the existing repo-search pattern:

- A text input with debounced HTMX `GET` to a new `user_search` endpoint.
- Results rendered as a dropdown of selectable users (email + display name).
- Selected users shown as chips under the input; each chip has an `×` that removes it from the hidden multi-value `subscribers` form field.
- Alpine component manages the selected set and keeps the `<select multiple>` synced on submit.

A new partial `accounts/_user_search_results.html` returns the dropdown fragment. A new view `accounts.views.UserSearchView` returns matching active users (max ~20), excluding the current user and any already-selected ids sent as query params. Route: `GET /accounts/users/search/?q=…&exclude=…`.

- Access: login required; member or admin. Exposing user names + emails across the instance is acceptable — DAIV is a trusted-team-per-install product.

### List view

Optional nice-to-have: show a small "+N" subscriber-count badge next to a schedule in the list. **Out of scope** for this design — add later if needed.

## Subscriber UI: self-unsubscribe

On `activity_detail.html`, near the existing "Schedule: <name>" metadata row, conditionally render an "Unsubscribe" button:

```django
{% if activity.scheduled_job and is_subscriber %}
  <form method="post" action="{% url 'schedule_unsubscribe' activity.scheduled_job.pk %}"
        class="inline">
    {% csrf_token %}
    <input type="hidden" name="next" value="{{ request.get_full_path }}">
    <button type="submit" class="btn-secondary-xs">Unsubscribe</button>
  </form>
{% endif %}
```

`is_subscriber` is a boolean computed in `ActivityDetailView.get_context_data`:

```python
context["is_subscriber"] = (
    self.object.scheduled_job_id is not None
    and self.object.scheduled_job.user_id != self.request.user.pk
    and self.object.scheduled_job.subscribers.filter(pk=self.request.user.pk).exists()
)
```

### View: `ScheduleUnsubscribeView`

```python
class ScheduleUnsubscribeView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, pk):
        schedule = get_object_or_404(ScheduledJob, pk=pk)
        if not schedule.subscribers.filter(pk=request.user.pk).exists():
            raise Http404  # not a subscriber → pretend it doesn't exist
        schedule.subscribers.remove(request.user)
        messages.success(
            request,
            f"You are no longer subscribed to '{schedule.name}'.",
        )
        next_url = request.POST.get("next")
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            return redirect(next_url)
        return redirect("activity_list")
```

Route: `POST /schedules/<int:pk>/unsubscribe/` → `schedule_unsubscribe` (matches the `<int:pk>` pattern used by the other schedule routes).

Security:

- Subscriber check before removal. A non-subscriber gets a 404 rather than leaking "subscribers list exists."
- Owner using this endpoint gets 404 (they're not in `subscribers`) — they can't accidentally remove themselves from something they never subscribed to.
- `next` param validated with `url_has_allowed_host_and_scheme` to prevent open redirects.

## Permissions matrix

| Action | Owner | Admin | Subscriber | Other user |
|---|---|---|---|---|
| View schedule in their list | ✅ | ✅ (all) | ❌ | ❌ |
| Edit schedule / manage subscribers | ✅ | ✅ | ❌ | ❌ |
| View linked activity detail | ✅ | ✅ (all) | ✅ | ❌ |
| View schedule detail (edit page) | ✅ | ✅ | ❌ (404) | ❌ |
| Receive finish notifications | ✅ (per `notify_on`) | ❌ (unless subscribed) | ✅ (per `notify_on`) | ❌ |
| Self-unsubscribe | ❌ (not a subscriber) | n/a | ✅ | ❌ |
| Search users for subscriber picker | ✅ | ✅ | n/a | n/a |

## Testing plan

Mirrors existing test layout. Tests cover only custom project logic, not Django M2M plumbing.

### `tests/unit_tests/schedules/test_models.py`
- Adding/removing subscribers on a schedule.
- Deleting a user removes them from the M2M.
- Deleting a schedule removes its M2M rows.

### `tests/unit_tests/schedules/test_forms.py`
- `ScheduledJobCreateForm` accepts `subscribers` and persists them on save.
- The owner is excluded from `subscribers` queryset when `owner=user` is passed.
- Inactive users are excluded.
- Submitting the owner's pk in `subscribers` is silently stripped.

### `tests/unit_tests/schedules/test_views.py`
- `ScheduleCreateView` passes `owner=request.user` to the form.
- `ScheduleUpdateView` passes `owner=self.object.user` to the form.
- `ScheduleUnsubscribeView`:
  - Subscriber POST removes them and redirects.
  - Non-subscriber POST returns 404.
  - Owner POST returns 404 (they're not a subscriber).
  - `next` param honored when safe; bad `next` redirects to default.

### `tests/unit_tests/notifications/test_signals.py`
- Owner only → one notification.
- Owner + 2 subscribers → three notifications, one per user.
- Owner accidentally in subscribers → still one notification per user (deduped).
- `notify_on = NEVER` → nobody notified.
- `notify_on = ON_FAILURE` + successful activity → nobody notified.
- A recipient's `notify()` failure does not prevent others from being notified (verify via patched `notify` that raises for one user).

### `tests/unit_tests/activity/test_views.py` (or equivalent)
- Subscriber can GET activity detail for a subscribed activity (200).
- Non-subscriber gets 404 on activity detail.
- Activity list for a subscriber includes subscribed activities.
- `is_subscriber` context flag is True for subscribers, False for owner.

### `tests/unit_tests/accounts/test_views.py` (or new file)
- `UserSearchView` returns matching active users.
- `UserSearchView` excludes the requesting user and any `exclude` ids.
- Non-authenticated requests redirect to login.

## Migration & rollout

- One migration: adds the M2M table.
- No data migration needed — existing schedules have zero subscribers.
- Backward compatible: the fanout still notifies the owner when `subscribers` is empty, preserving today's behavior.

## Docs

Update `docs/features/scheduled-jobs.md`:

- Add a "Subscribers" subsection under "Managing schedules."
- Mention self-unsubscribe from the activity detail page.
- Note that subscribers inherit the schedule's `notify_on` and get read-only access to the linked activities.
