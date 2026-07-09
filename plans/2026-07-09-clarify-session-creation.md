# Clarify Session Creation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make it obvious when to use Chat ("work with") vs a Run ("hand off"), and fix the two workflow rough edges around run submission.

**Architecture:** The `Session`/`Run` backend is already unified — this is a front-door + two-bug change. (1) A new `/sessions/new/` **chooser** page routes to the existing chat hero (`/sessions/new/chat/`) or the existing run form (`/runs/new/`). (2) Run submissions always redirect to the sessions list. (3) The false "session expired" banner is suppressed while a run is in flight, which also makes an in-flight background run render its transcript like a chat session (existing polling).

**Tech Stack:** Django 5 CBVs, django-ninja (untouched here), Alpine.js (untouched — verify only), Tailwind templates, pytest (`asyncio_mode=auto`), `uv`.

## Global Constraints

- Python **3.14 only**; run tests with `uv run pytest` (never bare `pytest`).
- `pythonpath = "daiv"` — imports are `from sessions... import ...` (no `daiv.` prefix) in both app and tests.
- Test settings use **English only** (`daiv.settings.test` → `LANGUAGES = (("en", ...),)`), so pt translations never affect test outcomes.
- Icons: **never** hand-roll inline `<svg>`. Use `{% load icon_tags %}{% icon "name" "css-classes" %}`. Icons `chat-bubble` and `bolt` already exist.
- Server-rendered HTML pages are **CBVs** in `daiv/sessions/views.py` (`LoginRequiredMixin`, etc.); do not add ninja routes for this.
- Commits: Conventional Commits, **no** `Co-Authored-By` line, **no** "Generated with Claude Code" footer.
- Unit tests cover **custom/project logic only**, not Django framework behavior.
- i18n: `make makemessages` rewrites every app's catalog (cross-app churn). **Hand-add** msgid/msgstr to the correct `pt` catalog instead.

---

### Task 1: Redirect run submissions to the sessions list

Currently a single-repo run redirects to the session detail page (where the "expired" banner then wrongly fires); the design says always land on the batch-filtered list to reinforce "hand off". `batch_id` is always set on `BatchSubmitResult` (`services.py:267,386`), so this is safe for single runs.

**Files:**
- Modify: `daiv/sessions/views.py:429-431` (`AgentRunCreateView.form_valid`)
- Test: `tests/unit_tests/sessions/test_views_runs.py:111-121`

**Interfaces:**
- Consumes: `submit_batch_runs(...) -> BatchSubmitResult` with `.batch_id: uuid.UUID`, `.runs: list[Run]`, `.failed: list`.
- Produces: nothing new — behavior change only.

- [ ] **Step 1: Update the failing test** — replace `test_post_single_run_redirects_to_session_detail` (lines 111-121) with the list-redirect expectation.

```python
@pytest.mark.django_db
def test_post_single_run_redirects_to_batch_list(member_client):
    result = _fake_result(runs=1, failed=0, session_id="thread-abc")
    with (
        patch("sessions.views.resolve_repo_envs", side_effect=lambda *, user, repos, explicit_env_id: repos),
        patch("sessions.views.submit_batch_runs", return_value=result) as submit,
    ):
        resp = member_client.post(NEW_RUN_URL, _post_data())
    assert resp.status_code == 302
    # A single run now lands on the batch-scoped list (not the detail page), to
    # reinforce the "hand off and walk away" model.
    assert resp["Location"] == reverse("session_list") + f"?batch={result.batch_id}"
    submit.assert_called_once()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/unit_tests/sessions/test_views_runs.py::test_post_single_run_redirects_to_batch_list -v`
Expected: FAIL — current `Location` is `/dashboard/sessions/thread-abc/`, not the `?batch=` list URL.

- [ ] **Step 3: Simplify `form_valid` to always redirect to the list**

In `daiv/sessions/views.py`, replace the tail of `form_valid` (the `if len(result.runs) == 1 ...` block, lines ~429-431):

```python
        if result.failed:
            failed_ids = ", ".join(f.repo_id for f in result.failed)
            messages_module.warning(
                self.request, _("Some repositories failed to submit: %(ids)s") % {"ids": failed_ids}
            )

        # Always land on the batch-scoped sessions list — the "hand off" model:
        # fire the run, see it queued/running in the list, walk away.
        return redirect(reverse("session_list") + f"?batch={result.batch_id}")
```

- [ ] **Step 4: Run the run-view tests**

Run: `uv run pytest tests/unit_tests/sessions/test_views_runs.py -v`
Expected: PASS (the new single-run test, plus the existing multi/failure tests which already expect the `?batch=` list).

- [ ] **Step 5: Commit**

```bash
git add daiv/sessions/views.py tests/unit_tests/sessions/test_views_runs.py
git commit -m "fix(sessions): redirect run submissions to the batch-scoped list"
```

---

### Task 2: Suppress the false "expired" banner while a run is in flight

Root cause: `ahydrate_thread` returns `expired=True` whenever there is no checkpoint tuple — including a freshly-submitted run that hasn't checkpointed yet (`hydration.py:27-28`). Gate the banner on there being no in-flight run. This is also what unblocks requirement (b): with `expired` false, the in-flight "Agent is working" block + existing transcript polling render (the chat-equivalent view).

**Files:**
- Modify: `daiv/sessions/views.py:257-278` (`SessionDetailView.get_context_data`)
- Test: `tests/unit_tests/sessions/test_views_detail.py` (add after the existing expiry tests, ~line 129)

**Interfaces:**
- Consumes: `ahydrate_thread(thread_id) -> (messages, expired: bool, mr_payload)`; `RunStatus.terminal() -> set[str]`; `Run.status`.
- Produces: `ctx["expired"]` now means "expired **and** nothing in flight". `ctx["is_in_flight"]` unchanged.

- [ ] **Step 1: Write the failing tests** — append to `test_views_detail.py`. The first asserts the gate; the second pins the genuine-expiry path so we don't regress it.

```python
@pytest.mark.django_db
def test_detail_missing_checkpoint_not_expired_while_run_in_flight(member_client, member_user):
    """A just-submitted background run has no checkpoint yet; that must NOT render as
    'expired'. Instead the in-flight working state + transcript polling take over."""
    session = _create_session(user=member_user, ref="")  # ref="" skips the MR-payload lookup
    _create_run(session, trigger_type=SessionOrigin.UI_JOB, status=RunStatus.RUNNING)

    with patch("sessions.hydration.open_checkpointer") as cp_ctx:
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=None)  # no checkpoint yet
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is False
    assert resp.context["is_in_flight"] is True
    content = resp.content.decode()
    # In-flight + no checkpoint renders the working state, not the expired banner.
    assert "Agent is working" in content
    assert "has expired" not in content


@pytest.mark.django_db
def test_detail_missing_checkpoint_expired_when_all_runs_terminal(member_client, member_user):
    """No checkpoint AND no in-flight run => genuinely expired; banner still shows."""
    session = _create_session(user=member_user, ref="")
    _create_run(session, trigger_type=SessionOrigin.UI_JOB, status=RunStatus.SUCCESSFUL)

    with patch("sessions.hydration.open_checkpointer") as cp_ctx:
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=None)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is True
```

- [ ] **Step 2: Run them and watch the first fail**

Run: `uv run pytest tests/unit_tests/sessions/test_views_detail.py -k "missing_checkpoint" -v`
Expected: `..._not_expired_while_run_in_flight` FAILS (`expired` is currently `True`); `..._expired_when_all_runs_terminal` already PASSES.

- [ ] **Step 3: Gate `expired` on `is_in_flight`** — in `SessionDetailView.get_context_data`, compute the runs/in-flight flag **before** setting `expired`. Replace the block from `ctx["turns"] = build_turns(...)` down to the `ctx["is_in_flight"] = ...` line with:

```python
        runs = list(session.runs.order_by("created_at"))
        is_in_flight = any(r.status not in RunStatus.terminal() for r in runs)

        ctx["turns"] = build_turns(messages_history)
        # ``ahydrate_thread`` reports "no checkpoint" as ``expired`` — but a freshly
        # submitted run has not checkpointed yet. Only treat the session as expired
        # when nothing is in flight; otherwise the in-flight "working" state and the
        # transcript poller render the same view a chat session gets.
        ctx["expired"] = expired and not is_in_flight
        ctx["active_run_id"] = session.active_run_id or ""
        ctx["merge_request"] = merge_request
        ctx["runs"] = runs
        ctx["is_in_flight"] = is_in_flight
        ctx["in_flight_ids"] = ",".join(str(r.id) for r in runs if r.status not in RunStatus.terminal())
```

(The `poll_transcript` computation immediately below is unchanged.)

- [ ] **Step 4: Run the full detail suite**

Run: `uv run pytest tests/unit_tests/sessions/test_views_detail.py -v`
Expected: PASS — including the pre-existing `test_detail_with_missing_checkpoint_flags_expired` (no runs → not in flight → still expired) and `test_detail_expired_checkpoint_disables_composer`.

- [ ] **Step 5: Commit**

```bash
git add daiv/sessions/views.py tests/unit_tests/sessions/test_views_detail.py
git commit -m "fix(sessions): don't flag a session expired while a run is in flight"
```

---

### Task 3: Add the "New" chooser and split the chat/run entry points

Introduce one front door. `/sessions/new/` becomes a chooser with two cards; the chat hero moves to `/sessions/new/chat/`; the run form stays at `/runs/new/`. Update the sidebar and list CTAs to a single **New** action, and fix the two internal references (legacy redirect, nav-active set).

**Files:**
- Modify: `daiv/sessions/views.py` (add `SessionNewView`; import `TemplateView`)
- Modify: `daiv/sessions/urls.py` (chooser at `new/`, chat hero at `new/chat/`)
- Create: `daiv/sessions/templates/sessions/session_new.html` (chooser)
- Modify: `daiv/sessions/urls_legacy.py` (legacy `/chat/new/` → `session_new_chat`)
- Modify: `daiv/accounts/context_processors.py:11-22` (add `session_new_chat` to the `sessions` nav set)
- Modify: `daiv/accounts/templates/accounts/_sidebar.html:12-21` (label → "New", testid → `nav-new-cta`)
- Modify: `daiv/sessions/templates/sessions/session_list.html:22-24,54-60` (CTAs → "New" → `session_new`)
- Test: `tests/unit_tests/sessions/test_views_detail.py:50-57`, `tests/unit_tests/sessions/test_redirects.py:52`

**Interfaces:**
- Produces URL names: `session_new` (chooser, unchanged name → sidebar/list/expired-banner keep resolving), `session_new_chat` (chat hero, served by the existing `SessionDetailView` with no `thread_id`).
- Consumes: `reverse("session_new_chat")`, `reverse("runs:agent_run_new")` in the chooser template.

- [ ] **Step 1: Write the failing tests** — repoint the empty-state test to the new chat route and add a chooser-render test. In `test_views_detail.py`, replace `test_session_new_renders_empty_state` (lines 50-57) with:

```python
@pytest.mark.django_db
def test_session_new_chat_renders_empty_state(member_client):
    with patch("sessions.views.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_new_chat"))
    assert resp.status_code == 200
    assert resp.context["session"] is None
    assert resp.context["expired"] is False
    assert resp.context["turns"] == []


@pytest.mark.django_db
def test_session_new_renders_chooser_with_both_paths(member_client):
    resp = member_client.get(reverse("session_new"))
    assert resp.status_code == 200
    content = resp.content.decode()
    # The chooser links to both destinations — the guidance lives at the fork.
    assert reverse("session_new_chat") in content
    assert reverse("runs:agent_run_new") in content
```

(`test_session_new_requires_login` at line 44 stays as-is — the chooser is still `LoginRequiredMixin`.)

- [ ] **Step 2: Update the legacy-redirect test** — in `tests/unit_tests/sessions/test_redirects.py:52`, change the expected target from `session_new` to `session_new_chat` (legacy `/chat/new/` meant "new chat", so it should skip the chooser):

```python
    assert resp["Location"] == reverse("session_new_chat")
```

- [ ] **Step 3: Run the tests and watch them fail**

Run: `uv run pytest tests/unit_tests/sessions/test_views_detail.py -k "session_new" tests/unit_tests/sessions/test_redirects.py -v`
Expected: FAIL — `session_new_chat` is not a registered URL name yet (`NoReverseMatch`).

- [ ] **Step 4: Add the chooser view** — in `daiv/sessions/views.py`, extend the generic import and add the view (place it just above `SessionDetailView`):

```python
from django.views.generic import DetailView, FormView, TemplateView
```

```python
class SessionNewView(LoginRequiredMixin, BreadcrumbMixin, TemplateView):
    """Single front door: choose Chat ('work with the agent') or Run ('hand off a task').

    The chat hero and the run form are unchanged; this page only routes to them and
    carries the one-line rule of thumb so the choice is legible at the fork.
    """

    template_name = "sessions/session_new.html"

    def get_breadcrumbs(self):
        return [{"label": "Sessions", "url": reverse("session_list")}, {"label": "New", "url": None}]
```

- [ ] **Step 5: Wire the routes** — in `daiv/sessions/urls.py`, import `SessionNewView` and split `new/`. Specific paths stay before the `<slug:thread_id>` catch-all:

```python
from sessions.views import (
    AgentRunCreateView,
    RunDownloadMarkdownView,
    SessionDetailView,
    SessionListView,
    SessionNewView,
    SessionStreamView,
)

urlpatterns = [
    path("", SessionListView.as_view(), name="session_list"),
    # Specific "new/*" and "stream/" routes precede the slug catch-all so they match first.
    path("new/", SessionNewView.as_view(), name="session_new"),
    path("new/chat/", SessionDetailView.as_view(), name="session_new_chat"),
    path("stream/", SessionStreamView.as_view(), name="session_stream"),
    path("<slug:thread_id>/", SessionDetailView.as_view(), name="session_detail"),
    path(
        "<slug:thread_id>/runs/<uuid:pk>/download/md/",
        RunDownloadMarkdownView.as_view(),
        name="session_run_download_md",
    ),
]
```

- [ ] **Step 6: Create the chooser template** — `daiv/sessions/templates/sessions/session_new.html`:

```html
{% extends "base_app.html" %}
{% load i18n icon_tags %}

{% block title %}New — DAIV{% endblock %}

{% block container_width %}max-w-3xl{% endblock %}

{% block breadcrumb %}{% include "accounts/_breadcrumb.html" %}{% endblock %}

{% block app_content %}
<div class="animate-fade-up">
  <h1 class="text-2xl font-bold tracking-tight">{% translate "Start something new" %}</h1>
  <p class="mt-1.5 text-[15px] font-light text-gray-400">
    {% translate "Two ways to put the agent to work — pick the one that fits." %}
  </p>

  <div class="mt-6 grid gap-4 sm:grid-cols-2">
    {# Chat — work with the agent #}
    <a href="{% url 'session_new_chat' %}"
       data-testid="new-chat-card"
       class="group flex flex-col rounded-2xl border border-white/[0.06] bg-white/[0.02] p-6 transition-colors hover:border-white/[0.14] hover:bg-white/[0.04]">
      <span class="flex h-11 w-11 items-center justify-center rounded-xl bg-teal-500/10 text-teal-300 ring-1 ring-inset ring-teal-500/20">
        {% icon "chat-bubble" "h-5 w-5" %}
      </span>
      <h2 class="mt-4 text-[16px] font-semibold text-white">{% translate "Chat" %}</h2>
      <p class="mt-0.5 text-sm font-medium text-teal-300/90">{% translate "Work with the agent" %}</p>
      <p class="mt-2 text-[14px] font-light leading-relaxed text-gray-400">
        {% translate "Collaborate live and iterate turn by turn, steering as it goes. Best for exploring or shaping a change in a single repository." %}
      </p>
      <span class="mt-4 inline-flex items-center gap-1 text-sm font-medium text-gray-300 transition-transform group-hover:translate-x-0.5">
        {% translate "Start a chat" %} <span aria-hidden="true">&rarr;</span>
      </span>
    </a>

    {# Run — hand off a task #}
    <a href="{% url 'runs:agent_run_new' %}"
       data-testid="new-run-card"
       class="group flex flex-col rounded-2xl border border-white/[0.06] bg-white/[0.02] p-6 transition-colors hover:border-white/[0.14] hover:bg-white/[0.04]">
      <span class="flex h-11 w-11 items-center justify-center rounded-xl bg-indigo-500/10 text-indigo-300 ring-1 ring-inset ring-indigo-500/20">
        {% icon "bolt" "h-5 w-5" %}
      </span>
      <h2 class="mt-4 text-[16px] font-semibold text-white">{% translate "Run" %}</h2>
      <p class="mt-0.5 text-sm font-medium text-indigo-300/90">{% translate "Hand off a task" %}</p>
      <p class="mt-2 text-[14px] font-light leading-relaxed text-gray-400">
        {% translate "Describe a well-specified task and let it run in the background — get notified when it's done. Works across one or many repositories, or on a schedule." %}
      </p>
      <span class="mt-4 inline-flex items-center gap-1 text-sm font-medium text-gray-300 transition-transform group-hover:translate-x-0.5">
        {% translate "Start a run" %} <span aria-hidden="true">&rarr;</span>
      </span>
    </a>
  </div>
</div>
{% endblock app_content %}
```

- [ ] **Step 7: Fix the legacy redirect** — in `daiv/sessions/urls_legacy.py`, change the `/chat/new/` `RedirectView` `pattern_name` from `"session_new"` to `"session_new_chat"`.

- [ ] **Step 8: Add the chat route to the nav-active set** — in `daiv/accounts/context_processors.py`, add `"session_new_chat"` to the `"sessions"` set in `SECTION_URL_NAMES` (alongside `"session_new"`), so the Sessions sidebar item highlights on the chat hero.

- [ ] **Step 9: Update the sidebar CTA** — in `daiv/accounts/templates/accounts/_sidebar.html`, on the promoted CTA (lines 12-21): keep `href="{% url 'session_new' %}"`, change `data-testid="nav-chat-cta"` → `data-testid="nav-new-cta"`, and change the label span from `{% translate "New chat" %}` to `{% translate "New" %}`.

- [ ] **Step 10: Update the list CTAs** — in `daiv/sessions/templates/sessions/session_list.html`, point both CTAs at the chooser:
  - Top-right (lines 22-24): `href="{% url 'session_new' %}"`, label `{% translate "New" %}`.
  - Empty state (lines 58-60): `href="{% url 'session_new' %}"`, label `{% translate "New" %}`.

- [ ] **Step 11: Run the targeted tests**

Run: `uv run pytest tests/unit_tests/sessions/test_views_detail.py -k "session_new" tests/unit_tests/sessions/test_redirects.py -v`
Expected: PASS.

- [ ] **Step 12: Catch collateral assertions in the list/view suites**

Run: `uv run pytest tests/unit_tests/sessions/test_views_list.py -v`
Then: `rg -n "Start a run|agent_run_new|nav-chat-cta|New chat" tests/`
If any test asserts the old label/URL on the list or sidebar, update it to `New` / `session_new` / `nav-new-cta`. (Search is expected to return only source references after fixes.)

- [ ] **Step 13: Commit**

```bash
git add daiv/sessions/views.py daiv/sessions/urls.py daiv/sessions/urls_legacy.py \
        daiv/sessions/templates/sessions/session_new.html \
        daiv/accounts/context_processors.py daiv/accounts/templates/accounts/_sidebar.html \
        daiv/sessions/templates/sessions/session_list.html \
        tests/unit_tests/sessions/test_views_detail.py tests/unit_tests/sessions/test_redirects.py
git commit -m "feat(sessions): add New chooser and split chat/run entry points"
```

---

### Task 4: Translations + full verification

Fold i18n and the cross-cutting checks here. Translations do not affect tests (English-only test settings) but keep the pt catalog complete; the Alpine composer-gating check is manual because it is client-side behavior already present in code.

**Files:**
- Modify: the `pt` catalog that already holds the sessions strings (locate in Step 1)

- [ ] **Step 1: Locate the sessions pt catalog and hand-add the new strings**

```bash
rg -l "Start a run" daiv/*/locale/pt/LC_MESSAGES/django.po
```

Hand-add `msgid`/`msgstr` pairs (leave `msgstr ""` for a translator, or provide pt) for the new/changed strings: `"New"`, `"Start something new"`, `"Two ways to put the agent to work — pick the one that fits."`, `"Chat"`, `"Work with the agent"`, `"Collaborate live and iterate turn by turn, steering as it goes. Best for exploring or shaping a change in a single repository."`, `"Start a chat"`, `"Run"`, `"Hand off a task"`, `"Describe a well-specified task and let it run in the background — get notified when it's done. Works across one or many repositories, or on a schedule."`. Do **not** run `make makemessages` (cross-app churn). Then:

```bash
make compilemessages
```

- [ ] **Step 2: Manually verify the unified in-flight view + composer gating**

Reasoning check against the code (no automated test — Alpine behavior):
- `session_detail.html` renders the `chat()` component with `activeRunId: "{{ active_run_id }}"`. For an in-flight background run, `session.active_run_id` is set.
- `chat-stream.js`: `resuming: !!config.activeRunId` (line 155) and `submit()` early-returns while `this.resuming` (line 486) → composer is locked during the run.
- `_startResumePoll()` polls `session_status`; when `active` flips false it reloads → finished transcript, composer re-enabled.
- With Task 2's fix, `expired` is false while in flight, so the composer + `poll_transcript` (turns polling) render. Confirm by loading a queued/running UI run's detail page in a dev environment: you should see "Agent is working", the transcript filling in, and a disabled composer — not the expired banner.

- [ ] **Step 3: Full sessions suite + lint + typing**

```bash
uv run pytest tests/unit_tests/sessions/ -v
make lint-fix
make lint-typing
```

Expected: sessions tests PASS; `lint-fix` clean; `lint-typing` shows no **new** error classes (pre-existing Django descriptor false-positives are the known baseline).

- [ ] **Step 4: Commit**

```bash
git add daiv/**/locale/pt/LC_MESSAGES/django.po daiv/**/locale/pt/LC_MESSAGES/django.mo
git commit -m "chore(i18n): translate New chooser strings to pt"
```

(If no `.mo` files are tracked, drop them from the `git add` — only the `.po` is committed.)

---

## Self-Review

**Spec coverage:**
- Part 1 (launcher/chooser) → Task 3. ✓
- Part 2 (redirect to list) → Task 1. ✓
- Part 3 (fix false-expired + unify in-flight view) → Task 2 (gate) + Task 4 Step 2 (verify composer/transcript). ✓
- Out-of-scope items (no merged composer, no SSE for runs, no model changes) → respected; no task touches them. ✓
- Success criteria: one "New" action + chooser (T3); run submit lands on list, no expired banner (T1+T2); in-flight detail shows live transcript (T2); genuine expiry still banners (T2 second test). ✓

**Placeholder scan:** No TBD/TODO; every code and test step shows concrete code or an exact command. The one shell-discovery step (Task 4 Step 1 locating the pt catalog) is a real runnable command, not a placeholder.

**Type/name consistency:** `session_new` (chooser) and `session_new_chat` (hero) used consistently across urls, views, templates, context processor, legacy redirect, and tests. `SessionNewView` is the only new symbol. `expired`/`is_in_flight` context keys match their template consumers (`session_detail.html:75,118`). Redirect uses `reverse("session_list") + "?batch=" + batch_id`, matching the existing multi-run branch.
