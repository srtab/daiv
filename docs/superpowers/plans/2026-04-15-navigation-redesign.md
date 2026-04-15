# Navigation redesign — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current minimal header with a persistent left sidebar (role-aware), a top-right utility strip (bell + user menu), and sub-page breadcrumbs across every dashboard-authenticated page.

**Architecture:** One new app-shell template (`base_app.html`) wraps a new sidebar partial, a user-menu partial, and a breadcrumb partial. A nav context processor supplies `nav_running_jobs` + `nav_active_section` to all templates. A `nav_active` template tag returns active-state CSS classes. Each affected dashboard template switches from extending `base.html` to extending `base_app.html` and drops the old header/back-link chrome.

**Tech Stack:** Django templates, Tailwind utility classes already in the project, Alpine.js for the user-menu dropdown and mobile drawer, the existing `{% icon %}` inclusion tag, pytest for tests.

**Spec:** [docs/superpowers/specs/2026-04-15-navigation-redesign-design.md](../specs/2026-04-15-navigation-redesign-design.md)

---

## File structure

### New files

| Path | Responsibility |
|------|----------------|
| `daiv/accounts/context_processors.py` | Supplies `nav_running_jobs` (int) and `nav_active_section` (str) to every request. |
| `daiv/accounts/templatetags/nav_tags.py` | `{% nav_active 'section_key' %}` tag returning CSS classes for the active sidebar item. |
| `daiv/accounts/templates/accounts/_sidebar.html` | Sidebar rendering (brand, daily items, Account group, Admin group, footer link). |
| `daiv/accounts/templates/accounts/_user_menu.html` | Avatar chip + Alpine dropdown with email and sign-out form. |
| `daiv/accounts/templates/accounts/_breadcrumb.html` | Renders a list of `(label, url_or_none)` pairs. |
| `daiv/accounts/templates/base_app.html` | App shell: extends `base.html`, provides sidebar + utility strip + `{% block app_content %}`. |
| `tests/unit_tests/accounts/test_nav_tags.py` | Template-tag tests. |
| `tests/unit_tests/accounts/test_context_processors.py` | Context-processor tests. |
| `tests/unit_tests/accounts/test_sidebar.py` | Role-visibility, running-badge, sidebar-smoke tests. |

### Modified files

| Path | Change |
|------|--------|
| `daiv/daiv/settings/components/common.py:72-78` | Register `accounts.context_processors.nav` in `TEMPLATES[0]["OPTIONS"]["context_processors"]`. |
| `daiv/accounts/templates/accounts/dashboard.html` | Switch to `base_app.html`; drop `_header.html` include; delete the entire Quick Links grid (lines 180–198). |
| `daiv/activity/templates/activity/activity_list.html` | Switch to `base_app.html`; drop `_header.html`; drop "← Dashboard" link. |
| `daiv/activity/templates/activity/activity_detail.html` | Switch to `base_app.html`; drop `_header.html`; drop "← Activity" link; add breadcrumb. |
| `daiv/schedules/templates/schedules/schedule_list.html` | Switch to `base_app.html`; drop `_header.html`; drop "← Dashboard" link. |
| `daiv/schedules/templates/schedules/schedule_form.html` | Switch; drop header/back-link; add breadcrumb. |
| `daiv/schedules/templates/schedules/schedule_confirm_delete.html` | Switch; drop header/back-link; add breadcrumb. |
| `daiv/accounts/templates/accounts/users.html` | Switch; drop header/back-link. |
| `daiv/accounts/templates/accounts/user_form.html` | Switch; drop header/back-link; add breadcrumb. |
| `daiv/accounts/templates/accounts/user_confirm_delete.html` | Switch; drop header/back-link; add breadcrumb. |
| `daiv/accounts/templates/accounts/api_keys.html` | Switch; drop header/back-link. |
| `daiv/core/templates/core/site_configuration.html` | Switch; drop header/back-link. |
| `daiv/notifications/templates/notifications/channels_page.html` | Switch; drop header; no breadcrumb. |
| `daiv/notifications/templates/notifications/notification_list.html` | Switch; drop header; no breadcrumb (detached page). |

### Deleted (at the end)

| Path | When |
|------|------|
| `daiv/accounts/templates/accounts/_header.html` | After all callers are migrated (verified via `grep -r '_header.html' daiv/`). |

---

## Section-key mapping (used by tag & context processor)

```python
SECTION_URL_NAMES: dict[str, set[str]] = {
    "dashboard": {"dashboard"},
    "activity": {"activity_list", "activity_detail", "activity_stream", "activity_download_md"},
    "schedules": {
        "schedule_list",
        "schedule_create",
        "schedule_update",
        "schedule_delete",
        "schedule_toggle",
        "schedule_run_now",
    },
    "channels": {"user_channels"},
    "api_keys": {"api_keys", "api_key_create", "api_key_revoke"},
    "users": {"user_list", "user_create", "user_update", "user_delete"},
    "configuration": {"site_configuration"},
}
```

`notifications:list` is intentionally omitted — it's a detached page (see spec §2.5).

---

## Task 1 — Template tag `nav_active`

**Files:**
- Create: `daiv/accounts/templatetags/nav_tags.py`
- Test: `tests/unit_tests/accounts/test_nav_tags.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit_tests/accounts/test_nav_tags.py
from django.template import Context, Template
from django.test import RequestFactory

import pytest


def _render(template_source: str, context_dict: dict) -> str:
    return Template(template_source).render(Context(context_dict))


class TestNavActive:
    def test_returns_active_classes_when_section_matches(self):
        ctx = {"nav_active_section": "activity"}
        out = _render("{% load nav_tags %}{% nav_active 'activity' %}", ctx)
        assert "bg-white/[0.06]" in out
        assert "text-white" in out

    def test_returns_empty_string_when_section_does_not_match(self):
        ctx = {"nav_active_section": "activity"}
        out = _render("{% load nav_tags %}{% nav_active 'schedules' %}", ctx)
        assert out.strip() == ""

    def test_returns_empty_string_when_no_active_section_in_context(self):
        out = _render("{% load nav_tags %}{% nav_active 'activity' %}", {})
        assert out.strip() == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd daiv && uv run pytest ../tests/unit_tests/accounts/test_nav_tags.py -v`
Expected: FAIL — `TemplateSyntaxError: 'nav_tags' is not a registered tag library`

- [ ] **Step 3: Implement the tag**

```python
# daiv/accounts/templatetags/nav_tags.py
from __future__ import annotations

from django import template

register = template.Library()

ACTIVE_CLASSES = "bg-white/[0.06] text-white"


@register.simple_tag(takes_context=True)
def nav_active(context, section_key: str) -> str:
    """Return CSS classes when the sidebar item for ``section_key`` is the active section.

    The active section is computed once per request by ``accounts.context_processors.nav``
    and exposed as ``nav_active_section`` in the template context.
    """
    if context.get("nav_active_section") == section_key:
        return ACTIVE_CLASSES
    return ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd daiv && uv run pytest ../tests/unit_tests/accounts/test_nav_tags.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add daiv/accounts/templatetags/nav_tags.py tests/unit_tests/accounts/test_nav_tags.py
git commit -m "feat(accounts): add nav_active template tag for sidebar"
```

---

## Task 2 — Context processor (`nav_running_jobs`, `nav_active_section`)

**Files:**
- Create: `daiv/accounts/context_processors.py`
- Modify: `daiv/daiv/settings/components/common.py:72-78` (register processor)
- Test: `tests/unit_tests/accounts/test_context_processors.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit_tests/accounts/test_context_processors.py
from django.test import Client, RequestFactory
from django.urls import reverse

import pytest

from accounts.context_processors import nav
from accounts.models import User
from activity.models import Activity, ActivityStatus, TriggerType


@pytest.fixture
def user(db):
    return User.objects.create_user(username="alice", email="alice@test.com", password="testpass123")  # noqa: S106


@pytest.mark.django_db
class TestNavContextProcessor:
    def test_returns_empty_for_anonymous_user(self):
        request = RequestFactory().get("/dashboard/")
        from django.contrib.auth.models import AnonymousUser

        request.user = AnonymousUser()
        assert nav(request) == {}

    def test_returns_zero_running_jobs_when_none(self, user):
        request = RequestFactory().get("/dashboard/")
        request.user = user
        request.resolver_match = None
        out = nav(request)
        assert out["nav_running_jobs"] == 0
        assert out["nav_active_section"] == ""

    def test_counts_only_running_jobs_owned_by_user(self, user, db):
        Activity.objects.create(
            status=ActivityStatus.RUNNING, trigger_type=TriggerType.MCP_JOB, user=user, repo_id="daiv/api"
        )
        Activity.objects.create(
            status=ActivityStatus.SUCCESSFUL, trigger_type=TriggerType.MCP_JOB, user=user, repo_id="daiv/api"
        )
        other = User.objects.create_user(username="bob", email="bob@test.com", password="x123456789")  # noqa: S106
        Activity.objects.create(
            status=ActivityStatus.RUNNING, trigger_type=TriggerType.MCP_JOB, user=other, repo_id="daiv/api"
        )

        request = RequestFactory().get("/dashboard/")
        request.user = user
        request.resolver_match = None
        out = nav(request)
        assert out["nav_running_jobs"] == 1

    def test_resolves_active_section_from_url_name(self, db):
        # Use a real request through the client so resolver_match is populated.
        user_obj = User.objects.create_user(username="charlie", email="c@test.com", password="x123456789")  # noqa: S106
        client = Client()
        client.force_login(user_obj)
        response = client.get(reverse("dashboard"))
        assert response.context["nav_active_section"] == "dashboard"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd daiv && uv run pytest ../tests/unit_tests/accounts/test_context_processors.py -v`
Expected: FAIL — `ModuleNotFoundError: accounts.context_processors` or `ImportError`.

- [ ] **Step 3: Implement the context processor**

```python
# daiv/accounts/context_processors.py
from __future__ import annotations

import logging
from typing import Any

from django.db import Error as DatabaseError

logger = logging.getLogger("daiv.accounts")

SECTION_URL_NAMES: dict[str, set[str]] = {
    "dashboard": {"dashboard"},
    "activity": {"activity_list", "activity_detail", "activity_stream", "activity_download_md"},
    "schedules": {
        "schedule_list",
        "schedule_create",
        "schedule_update",
        "schedule_delete",
        "schedule_toggle",
        "schedule_run_now",
    },
    "channels": {"user_channels"},
    "api_keys": {"api_keys", "api_key_create", "api_key_revoke"},
    "users": {"user_list", "user_create", "user_update", "user_delete"},
    "configuration": {"site_configuration"},
}


def _resolve_active_section(request) -> str:
    match = getattr(request, "resolver_match", None)
    if match is None:
        return ""
    url_name = match.url_name or ""
    for section_key, names in SECTION_URL_NAMES.items():
        if url_name in names:
            return section_key
    return ""


def nav(request) -> dict[str, Any]:
    """Supply ``nav_running_jobs`` and ``nav_active_section`` to every authenticated request."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}

    from activity.models import Activity, ActivityStatus  # local import to avoid circulars

    try:
        running = Activity.objects.by_owner(user).filter(status=ActivityStatus.RUNNING).count()
    except DatabaseError:
        logger.exception("Failed to compute nav_running_jobs for user %s", user.pk)
        running = 0

    return {"nav_running_jobs": running, "nav_active_section": _resolve_active_section(request)}
```

- [ ] **Step 4: Register the processor in settings**

Modify `daiv/daiv/settings/components/common.py:72-78` — add one line to the `context_processors` list:

```python
    TEMPLATES = [
        {
            "BACKEND": "django.template.backends.django.DjangoTemplates",
            "APP_DIRS": True,
            "OPTIONS": {
                "context_processors": [
                    "django.template.context_processors.debug",
                    "django.template.context_processors.request",
                    "django.contrib.auth.context_processors.auth",
                    "django.contrib.messages.context_processors.messages",
                    "notifications.context_processors.unread_notification_count",
                    "accounts.context_processors.nav",
                ]
            },
        }
    ]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd daiv && uv run pytest ../tests/unit_tests/accounts/test_context_processors.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add daiv/accounts/context_processors.py daiv/daiv/settings/components/common.py tests/unit_tests/accounts/test_context_processors.py
git commit -m "feat(accounts): add nav context processor for running jobs and active section"
```

---

## Task 3 — Sidebar partial (`_sidebar.html`)

**Files:**
- Create: `daiv/accounts/templates/accounts/_sidebar.html`

- [ ] **Step 1: Create the partial**

```html
{# daiv/accounts/templates/accounts/_sidebar.html #}
{% load i18n icon_tags nav_tags %}

<aside data-testid="app-sidebar"
       class="hidden w-60 shrink-0 border-r border-white/[0.06] bg-[#030712] sm:flex sm:flex-col"
       x-cloak>
  <div class="flex items-center gap-2.5 px-5 pt-5 pb-6">
    <span class="inline-block h-6 w-6 rounded-md bg-gradient-to-br from-violet-500 to-indigo-500"></span>
    <a href="{% url 'dashboard' %}" class="text-[17px] font-bold tracking-tight text-white">DAIV</a>
  </div>

  <nav class="flex flex-1 flex-col px-3 pb-4">
    {# Daily work — no group label #}
    <a href="{% url 'dashboard' %}"
       class="group relative mb-0.5 flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-gray-400 transition-colors hover:text-gray-200 {% nav_active 'dashboard' %}">
      <span class="absolute left-0 top-1.5 bottom-1.5 w-[3px] rounded-r bg-violet-500 {% if nav_active_section != 'dashboard' %}opacity-0{% endif %}"></span>
      {% icon "squares-2x2" "h-4 w-4" %}
      <span>{% translate "Dashboard" %}</span>
    </a>

    <a href="{% url 'activity_list' %}"
       class="group relative mb-0.5 flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-gray-400 transition-colors hover:text-gray-200 {% nav_active 'activity' %}">
      <span class="absolute left-0 top-1.5 bottom-1.5 w-[3px] rounded-r bg-violet-500 {% if nav_active_section != 'activity' %}opacity-0{% endif %}"></span>
      {% icon "bolt" "h-4 w-4" %}
      <span class="flex-1">{% translate "Activity" %}</span>
      {% if nav_running_jobs %}
      <span data-testid="nav-running-badge"
            class="inline-flex items-center rounded-full bg-violet-500/15 px-2 py-0.5 text-[11px] font-medium text-violet-300">
        {% blocktranslate count counter=nav_running_jobs %}{{ counter }} running{% plural %}{{ counter }} running{% endblocktranslate %}
      </span>
      {% endif %}
    </a>

    <a href="{% url 'schedule_list' %}"
       class="group relative mb-0.5 flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-gray-400 transition-colors hover:text-gray-200 {% nav_active 'schedules' %}">
      <span class="absolute left-0 top-1.5 bottom-1.5 w-[3px] rounded-r bg-violet-500 {% if nav_active_section != 'schedules' %}opacity-0{% endif %}"></span>
      {% icon "clock" "h-4 w-4" %}
      <span>{% translate "Schedules" %}</span>
    </a>

    {# Account group #}
    <div class="mt-5 mb-1 px-3 text-[11px] font-semibold uppercase tracking-[0.14em] text-gray-500">
      {% translate "Account" %}
    </div>

    <a href="{% url 'user_channels' %}"
       class="group relative mb-0.5 flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-gray-400 transition-colors hover:text-gray-200 {% nav_active 'channels' %}">
      <span class="absolute left-0 top-1.5 bottom-1.5 w-[3px] rounded-r bg-violet-500 {% if nav_active_section != 'channels' %}opacity-0{% endif %}"></span>
      {% icon "envelope" "h-4 w-4" %}
      <span>{% translate "Notification channels" %}</span>
    </a>

    <a href="{% url 'api_keys' %}"
       class="group relative mb-0.5 flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-gray-400 transition-colors hover:text-gray-200 {% nav_active 'api_keys' %}">
      <span class="absolute left-0 top-1.5 bottom-1.5 w-[3px] rounded-r bg-violet-500 {% if nav_active_section != 'api_keys' %}opacity-0{% endif %}"></span>
      {% icon "key" "h-4 w-4" %}
      <span>{% translate "API keys" %}</span>
    </a>

    {% if user.is_admin %}
    {# Admin group — omitted entirely for non-admins #}
    <div data-testid="nav-admin-group"
         class="mt-5 mb-1 px-3 text-[11px] font-semibold uppercase tracking-[0.14em] text-gray-500">
      {% translate "Admin" %}
    </div>

    <a href="{% url 'user_list' %}"
       class="group relative mb-0.5 flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-gray-400 transition-colors hover:text-gray-200 {% nav_active 'users' %}">
      <span class="absolute left-0 top-1.5 bottom-1.5 w-[3px] rounded-r bg-violet-500 {% if nav_active_section != 'users' %}opacity-0{% endif %}"></span>
      {% icon "users" "h-4 w-4" %}
      <span>{% translate "Users" %}</span>
    </a>

    <a href="{% url 'site_configuration' %}"
       class="group relative mb-0.5 flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-gray-400 transition-colors hover:text-gray-200 {% nav_active 'configuration' %}">
      <span class="absolute left-0 top-1.5 bottom-1.5 w-[3px] rounded-r bg-violet-500 {% if nav_active_section != 'configuration' %}opacity-0{% endif %}"></span>
      {% icon "cog-6-tooth" "h-4 w-4" %}
      <span>{% translate "Configuration" %}</span>
    </a>
    {% endif %}

    {# Footer — API docs pinned to the bottom #}
    <div class="mt-auto border-t border-white/[0.05] pt-3">
      <a href="{% url 'api:openapi-view' %}"
         class="flex items-center gap-3 rounded-lg px-3 py-2 text-xs text-gray-500 transition-colors hover:text-gray-300">
        {% icon "code-bracket" "h-3.5 w-3.5" %}
        <span>{% translate "API documentation" %}</span>
      </a>
    </div>
  </nav>
</aside>
```

- [ ] **Step 2: Commit**

```bash
git add daiv/accounts/templates/accounts/_sidebar.html
git commit -m "feat(accounts): add sidebar partial with role-aware grouping and active state"
```

---

## Task 4 — User-menu partial (`_user_menu.html`)

**Files:**
- Create: `daiv/accounts/templates/accounts/_user_menu.html`

- [ ] **Step 1: Create the partial**

```html
{# daiv/accounts/templates/accounts/_user_menu.html #}
{% load i18n %}

<div data-testid="app-user-menu" x-data="{ open: false }" class="relative" @keydown.escape="open = false">
  <button type="button"
          @click="open = !open"
          :aria-expanded="open.toString()"
          class="flex items-center gap-2 rounded-full border border-white/[0.06] bg-white/[0.02] py-1 pl-1 pr-3 text-sm text-gray-300 transition-colors hover:bg-white/[0.04]">
    <span class="inline-flex h-6 w-6 items-center justify-center rounded-full bg-gradient-to-br from-violet-500 to-cyan-400 text-[11px] font-semibold text-white">
      {{ user.name|default:user.email|first|upper }}
    </span>
    <span class="hidden text-[13px] text-gray-300 sm:inline">{{ user.email }}</span>
    <svg class="h-3 w-3 text-gray-500" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5">
      <path d="M3 4.5L6 7.5L9 4.5" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
  </button>

  <div x-show="open"
       @click.outside="open = false"
       x-cloak
       x-transition
       class="absolute right-0 top-full z-40 mt-2 w-56 rounded-xl border border-white/[0.06] bg-[#0a0e1a] py-1 shadow-xl"
       role="menu">
    <div class="px-4 py-2.5 text-[13px] text-gray-400">
      {{ user.email }}
    </div>
    <div class="my-1 border-t border-white/[0.06]"></div>
    <form method="post" action="{% url 'account_logout' %}" class="px-1.5 py-1">
      {% csrf_token %}
      <button type="submit" class="w-full rounded-md px-2.5 py-1.5 text-left text-[13px] text-gray-300 transition-colors hover:bg-white/[0.04] hover:text-white">
        {% translate "Sign out" %}
      </button>
    </form>
  </div>
</div>
```

- [ ] **Step 2: Commit**

```bash
git add daiv/accounts/templates/accounts/_user_menu.html
git commit -m "feat(accounts): add user-menu partial with Alpine dropdown"
```

---

## Task 5 — Breadcrumb partial (`_breadcrumb.html`)

**Files:**
- Create: `daiv/accounts/templates/accounts/_breadcrumb.html`

The partial expects a `crumbs` variable in context: a list of `{"label": str, "url": str|None}` dicts. The last crumb should have `url=None` and renders as plain text (the current page).

- [ ] **Step 1: Create the partial**

```html
{# daiv/accounts/templates/accounts/_breadcrumb.html #}
{% load i18n %}

{% if crumbs %}
<nav data-testid="app-breadcrumb" aria-label="Breadcrumb" class="mb-3 flex items-center gap-2 text-[13px] text-gray-500">
  {% for crumb in crumbs %}
    {% if crumb.url %}
      <a href="{{ crumb.url }}" class="text-gray-400 transition-colors hover:text-gray-200">{{ crumb.label }}</a>
    {% else %}
      <span class="text-gray-200">{{ crumb.label }}</span>
    {% endif %}
    {% if not forloop.last %}<span aria-hidden="true" class="text-gray-600">›</span>{% endif %}
  {% endfor %}
</nav>
{% endif %}
```

- [ ] **Step 2: Commit**

```bash
git add daiv/accounts/templates/accounts/_breadcrumb.html
git commit -m "feat(accounts): add breadcrumb partial"
```

---

## Task 6 — App shell (`base_app.html`)

**Files:**
- Create: `daiv/accounts/templates/base_app.html`

- [ ] **Step 1: Create the app shell**

```html
{# daiv/accounts/templates/base_app.html #}
{% extends "base.html" %}
{% load i18n %}

{% block content %}
<div x-data="{ mobileNavOpen: false }" class="flex min-h-dvh">
  {# Desktop sidebar (≥sm) #}
  {% include "accounts/_sidebar.html" %}

  {# Mobile drawer (<sm) #}
  <div x-show="mobileNavOpen" x-cloak class="fixed inset-0 z-40 sm:hidden">
    <div x-show="mobileNavOpen" x-transition.opacity
         @click="mobileNavOpen = false"
         class="absolute inset-0 bg-black/60"></div>
    <div x-show="mobileNavOpen"
         x-transition:enter="transition-transform duration-200"
         x-transition:enter-start="-translate-x-full"
         x-transition:enter-end="translate-x-0"
         x-transition:leave="transition-transform duration-150"
         x-transition:leave-start="translate-x-0"
         x-transition:leave-end="-translate-x-full"
         class="relative flex h-full w-[85%] max-w-[260px] flex-col bg-[#030712] shadow-2xl">
      {% include "accounts/_sidebar.html" with force_mobile=1 %}
    </div>
  </div>

  <div class="flex flex-1 flex-col min-w-0">
    {# Utility strip #}
    <header class="flex items-center justify-between gap-3 border-b border-white/[0.06] px-4 py-2.5 sm:px-6">
      <button type="button"
              @click="mobileNavOpen = !mobileNavOpen"
              class="sm:hidden inline-flex h-9 w-9 items-center justify-center rounded-lg border border-white/[0.06] text-gray-400"
              aria-label="{% translate 'Open navigation' %}">
        <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M4 6h16M4 12h16M4 18h16" stroke-linecap="round"/>
        </svg>
      </button>
      <div class="flex-1"></div>
      <div class="flex items-center gap-4">
        {% include "notifications/_bell.html" %}
        {% include "accounts/_user_menu.html" %}
      </div>
    </header>

    <main class="flex-1 overflow-x-hidden px-4 py-8 sm:px-6 sm:py-10">
      <div class="mx-auto {{ content_max_w|default:'max-w-6xl' }}">
        {% block breadcrumb %}{% endblock %}
        {% block app_content %}{% endblock %}
      </div>
    </main>
  </div>
</div>
{% endblock %}
```

- [ ] **Step 2: Commit**

```bash
git add daiv/accounts/templates/base_app.html
git commit -m "feat(accounts): add base_app.html shell with sidebar, utility strip, and mobile drawer"
```

---

## Task 7 — Migrate `dashboard.html` (and drop the Quick Links grid)

**Files:**
- Modify: `daiv/accounts/templates/accounts/dashboard.html`

- [ ] **Step 1: Rewrite the template**

Replace the entire contents of `daiv/accounts/templates/accounts/dashboard.html` (203 lines → ~170 lines) with the version below. Key changes:
- `{% extends "base.html" %}` → `{% extends "base_app.html" %}`
- `{% block content %}` → `{% block app_content %}`
- Remove the outer `<div class="min-h-dvh">`, the `{% include "accounts/_header.html" %}`, and the `<main class="mx-auto max-w-6xl px-6 py-12">` wrapper (all supplied by the shell now)
- **Delete** the Quick Links grid (old lines 180–198, the `<div class="animate-fade-up mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">` and everything inside it, up to and including the closing `</div>` before `</main>`)

```html
{% extends "base_app.html" %}
{% load static dashboard_tags i18n icon_tags %}

{% block title %}Dashboard — DAIV{% endblock %}

{% block app_content %}
<!-- Title row with period filter -->
<div class="animate-fade-up flex flex-col gap-6 sm:flex-row sm:items-start sm:justify-between">
    <div>
        <h1 class="text-2xl font-bold tracking-tight">Dashboard</h1>
        <p class="mt-1.5 text-[15px] font-light text-gray-400">Welcome back, {{ user.name|default:user.email }}</p>
    </div>
    <div class="flex items-center gap-1 rounded-xl border border-white/[0.06] bg-white/[0.02] p-1">
        {% for p in periods %}
        <a href="?period={{ p.key }}"
           class="rounded-lg px-3 py-1.5 text-sm font-medium transition-all duration-200
                  {% if p.key == current_period %}bg-white/[0.1] text-white shadow-sm{% else %}text-gray-400 hover:text-gray-300{% endif %}">
            {{ p.label }}
        </a>
        {% endfor %}
    </div>
</div>

<!-- Agent Activity -->
<section class="animate-fade-up mt-10" style="animation-delay: 100ms">
    <div class="flex items-center justify-between mb-6">
        <div class="flex items-center gap-2.5">
            <div class="flex h-7 w-7 items-center justify-center rounded-lg border border-white/[0.08] bg-white/[0.04]">
                {% icon "beaker" "h-3.5 w-3.5 text-gray-400" %}
            </div>
            <h2 class="text-[15px] font-semibold tracking-tight text-gray-300">Agent Activity</h2>
        </div>
        <a href="{% url 'activity_list' %}" class="text-sm text-gray-400 transition-colors hover:text-gray-300">View all &rarr;</a>
    </div>

    <div class="rounded-2xl border border-white/[0.06] bg-white/[0.015] p-6">
        {# Hero numbers — unchanged from before #}
        <div class="grid grid-cols-2 gap-6 sm:grid-cols-5">
            <div>
                <p class="animate-number text-[42px] font-extrabold tabular-nums leading-none tracking-tight text-white" style="animation-delay: 150ms">{{ activity.total }}</p>
                <p class="mt-2 text-sm text-gray-400">jobs processed</p>
            </div>
            <div>
                <p class="animate-number text-[42px] font-extrabold tabular-nums leading-none tracking-tight {{ activity.success_rate_raw|success_rate_color }}" style="animation-delay: 200ms">{{ activity.success_rate }}</p>
                <p class="mt-2 text-sm text-gray-400">success rate</p>
            </div>
            <div>
                <div class="flex items-baseline gap-2">
                    <p class="animate-number text-[42px] font-extrabold tabular-nums leading-none tracking-tight text-blue-400" style="animation-delay: 250ms">{{ activity.running }}</p>
                    {% if activity.running %}<span class="pulse-dot mb-1 inline-block h-2.5 w-2.5 rounded-full bg-blue-400"></span>{% endif %}
                </div>
                <p class="mt-2 text-sm text-gray-400">running now</p>
            </div>
            <div>
                <p class="animate-number text-[42px] font-extrabold tabular-nums leading-none tracking-tight text-white" style="animation-delay: 300ms">{{ activity.code_changes }}</p>
                <p class="mt-2 text-sm text-gray-400">with code changes <span class="text-gray-500">({{ activity.code_changes_pct }})</span></p>
            </div>
            <div>
                <p class="animate-number text-[42px] font-extrabold tabular-nums leading-none tracking-tight text-white" style="animation-delay: 350ms">{{ activity.avg_duration }}</p>
                <p class="mt-2 text-sm text-gray-400">avg duration</p>
            </div>
        </div>

        {# Outcome breakdown bar — unchanged from before #}
        {% if activity.segments %}
        <div class="mt-6 border-t border-white/[0.04] pt-6">
            <p class="mb-3 text-[13px] font-medium uppercase tracking-[0.15em] text-gray-500">Outcome breakdown</p>
            <div class="flex h-3 w-full overflow-hidden rounded-full bg-white/[0.03]">
                {% for seg in activity.segments %}
                <div class="animate-bar {{ seg.css }}" style="width: {{ seg.pct }}%; animation-delay: {{ forloop.counter0|add:"5" }}00ms"></div>
                {% endfor %}
            </div>
            <div class="mt-4 flex flex-wrap gap-x-6 gap-y-2">
                {% for seg in activity.segments %}
                {% if seg.url %}
                <a href="{{ seg.url }}" class="group flex items-center gap-2 transition-colors">
                {% else %}
                <span class="flex items-center gap-2">
                {% endif %}
                    <span class="inline-block h-2.5 w-2.5 rounded-sm {{ seg.css }}"></span>
                    <span class="text-sm text-gray-400 {% if seg.url %}group-hover:text-gray-300{% endif %}">{{ seg.label }}</span>
                    <span class="text-sm font-semibold tabular-nums text-white">{{ seg.value }}</span>
                {% if seg.url %}
                </a>
                {% else %}
                </span>
                {% endif %}
                {% endfor %}
            </div>
        </div>
        {% endif %}
    </div>
</section>

<!-- Code Velocity — unchanged from before -->
{% if velocity %}
<section class="animate-fade-up mt-10" style="animation-delay: 250ms">
    <div class="flex items-center justify-between mb-6">
        <div class="flex items-center gap-2.5">
            <div class="flex h-7 w-7 items-center justify-center rounded-lg border border-white/[0.08] bg-white/[0.04]">
                {% icon "chart-bar" "h-3.5 w-3.5 text-gray-400" %}
            </div>
            <h2 class="text-[15px] font-semibold tracking-tight text-gray-300">Code Velocity</h2>
        </div>
        <span class="cursor-not-allowed text-sm text-gray-600/50" title="Available soon">View details &rarr;</span>
    </div>

    <div class="rounded-2xl border border-white/[0.06] bg-white/[0.015] p-6">
        <div class="grid grid-cols-1 gap-6 sm:grid-cols-5">
            <div class="sm:col-span-2">
                <p class="animate-number text-[56px] font-extrabold tabular-nums leading-none tracking-tight text-white" style="animation-delay: 350ms">{{ velocity.total_merges }}</p>
                <p class="mt-2 text-sm text-gray-400">merges into default branches</p>
            </div>
            <div class="sm:col-span-3">
                <p class="mb-3 text-[13px] font-medium uppercase tracking-[0.15em] text-gray-500">Lines changed</p>
                <div class="flex items-center gap-3">
                    <span class="w-14 text-right text-sm font-semibold tabular-nums text-emerald-400">+{{ velocity.lines_added }}</span>
                    <div class="h-5 flex-1 overflow-hidden rounded-lg bg-white/[0.03]">
                        <div class="animate-bar h-full rounded-lg bg-emerald-500/30" style="width: {{ velocity.lines_added_pct }}%; animation-delay: 500ms"></div>
                    </div>
                </div>
                <div class="h-2"></div>
                <div class="flex items-center gap-3">
                    <span class="w-14 text-right text-sm font-semibold tabular-nums text-red-400">-{{ velocity.lines_removed }}</span>
                    <div class="h-5 flex-1 overflow-hidden rounded-lg bg-white/[0.03]">
                        <div class="animate-bar h-full rounded-lg bg-red-500/30" style="width: {{ velocity.lines_removed_pct }}%; animation-delay: 600ms"></div>
                    </div>
                </div>
                <p class="mt-2 text-xs text-gray-500">Net change: {% if velocity.net_lines >= 0 %}+{% endif %}{{ velocity.net_lines }} lines</p>
            </div>
        </div>

        <div class="mt-6 border-t border-white/[0.04] pt-6">
            <p class="mb-4 text-[13px] font-medium uppercase tracking-[0.15em] text-gray-500">DAIV attribution</p>
            <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <div class="rounded-xl border border-white/[0.05] bg-white/[0.02] px-5 py-4">
                    <div class="flex items-baseline justify-between">
                        <span class="text-sm text-gray-400">MRs involving DAIV</span>
                        <span class="text-[20px] font-bold tabular-nums text-violet-300">{{ velocity.daiv_merges_pct }}</span>
                    </div>
                    <div class="mt-3 flex h-2 w-full overflow-hidden rounded-full bg-white/[0.04]">
                        <div class="animate-bar h-full rounded-full bg-gradient-to-r from-violet-600/60 to-violet-400/40" style="width: {{ velocity.daiv_merges_pct_raw }}%; animation-delay: 700ms"></div>
                    </div>
                    <div class="mt-2 flex justify-between text-xs text-gray-500">
                        <span>DAIV: {{ velocity.daiv_merges }} MRs</span>
                        <span>Human: {{ velocity.human_merges }} MRs</span>
                    </div>
                </div>
                <div class="rounded-xl border border-white/[0.05] bg-white/[0.02] px-5 py-4">
                    <div class="flex items-baseline justify-between">
                        <span class="text-sm text-gray-400">Commits authored by DAIV</span>
                        <span class="text-[20px] font-bold tabular-nums text-sky-300">{{ velocity.daiv_commits_pct }}</span>
                    </div>
                    <div class="mt-3 flex h-2 w-full overflow-hidden rounded-full bg-white/[0.04]">
                        <div class="animate-bar h-full rounded-full bg-gradient-to-r from-sky-600/60 to-sky-400/40" style="width: {{ velocity.daiv_commits_pct_raw }}%; animation-delay: 800ms"></div>
                    </div>
                    <div class="mt-2 flex justify-between text-xs text-gray-500">
                        <span>DAIV: {{ velocity.daiv_commits }} commits</span>
                        <span>Human: {{ velocity.human_commits }} commits</span>
                    </div>
                </div>
            </div>
        </div>
    </div>
</section>
{% endif %}
{% endblock %}
```

- [ ] **Step 2: Smoke-check the dashboard renders without errors**

Run: `cd daiv && uv run pytest ../tests/unit_tests/accounts/ -v -k "dashboard or Dashboard"`
Expected: Existing dashboard tests continue to pass.

- [ ] **Step 3: Commit**

```bash
git add daiv/accounts/templates/accounts/dashboard.html
git commit -m "refactor(accounts): migrate dashboard to base_app shell; drop quick-links grid"
```

---

## Task 8 — Migrate activity templates

**Files:**
- Modify: `daiv/activity/templates/activity/activity_list.html`
- Modify: `daiv/activity/templates/activity/activity_detail.html`

- [ ] **Step 1: Migrate `activity_list.html`**

In `daiv/activity/templates/activity/activity_list.html`:
- Change line 1 from `{% extends "base.html" %}` to `{% extends "base_app.html" %}`.
- Change `{% block content %}` (line 14) to `{% block app_content %}`.
- Remove the outer `<div class="min-h-dvh" x-data="...">` wrapper — move the `x-data` attribute onto the first inner element (`<div class="animate-fade-up flex items-center justify-between">` on line 19) so the Alpine scope still wraps the filters and list.
- Remove the `{% include "accounts/_header.html" %}` line (16).
- Remove the `<main class="mx-auto max-w-6xl px-6 py-12">` wrapper (line 18) and its closing `</main>` (the shell supplies that).
- Remove the "← Dashboard" anchor block (lines 26–29).
- Remove the redundant outer closing `</div>`.

Open the file and apply the above. The resulting top (lines 1–32 before the preserved body) should read:

```html
{% extends "base_app.html" %}
{% load activity_tags dashboard_tags i18n icon_tags l10n static %}

{% block title %}Agent Activity — DAIV{% endblock %}

{% block alpine_plugins %}
<script defer src="https://cdn.jsdelivr.net/npm/@alpinejs/ui@3.15.11/dist/cdn.min.js"
        integrity="sha384-USgPxo+ohBkt/xxOPsfCDC5BYAwgFHCatL+RFkcPCWWkvKSp5KzH52tUZZ7taB/c"
        crossorigin="anonymous"></script>
<script defer src="{% static 'codebase/js/repo-search.js' %}"></script>
<script defer src="{% static 'activity/js/activity-stream.js' %}"></script>
{% endblock %}

{% block app_content %}
<div x-data="activityStream('{% url "activity_stream" %}', '{{ in_flight_ids }}')">
    <div class="animate-fade-up">
        <h1 class="text-2xl font-bold tracking-tight">Agent Activity</h1>
        <p class="mt-1.5 text-[15px] font-light text-gray-400">
            {% if schedule_name %}{{ schedule_name }} &middot; {% endif %}All agent executions across jobs, schedules, and webhooks.
        </p>
    </div>
```

Everything below the title-row block in the original file (the filters, status pills, activity table, pagination, etc.) is preserved **verbatim** — only the outer `min-h-dvh`, header include, `main`, and `← Dashboard` link are removed. The closing tags at the very bottom of the file change from:

```html
    </main>
</div>
{% endblock %}
```

to:

```html
</div>
{% endblock %}
```

(only the `x-data` wrapper's closing `</div>` remains).

- [ ] **Step 2: Migrate `activity_detail.html`**

In `daiv/activity/templates/activity/activity_detail.html`:
- Change line 1 from `{% extends "base.html" %}` to `{% extends "base_app.html" %}`.
- Change `{% block content %}` (line 12) to `{% block app_content %}`.
- Remove the outer `<div class="min-h-dvh" ...>` wrapper — move its conditional `x-data` attribute to an inner wrapper.
- Remove `{% include "accounts/_header.html" %}` (line 14).
- Remove `<main class="mx-auto max-w-6xl px-6 py-12">` and its closing `</main>`.
- Remove the "← Activity" anchor block (lines 24–27).
- **Add** a `{% block breadcrumb %}` at the top of `app_content`:

```django
{% block breadcrumb %}
{% include "accounts/_breadcrumb.html" with crumbs=breadcrumbs %}
{% endblock %}
```

- [ ] **Step 3: Supply `breadcrumbs` from `ActivityDetailView`**

The Activity model's primary key is a UUIDField (`daiv/activity/models.py:59`), so there is no small integer ID. Use the first 8 characters of the UUID as a short identifier.

Modify `daiv/activity/views.py` — inside `ActivityDetailView.get_context_data`, add to the returned context:

```python
from django.urls import reverse

# ...
context["breadcrumbs"] = [
    {"label": "Activity", "url": reverse("activity_list")},
    {"label": f"Run {str(self.object.pk)[:8]} — {self.object.repo_id}", "url": None},
]
```

- [ ] **Step 4: Run existing activity tests**

Run: `cd daiv && uv run pytest ../tests/unit_tests/activity/ -v`
Expected: all existing tests pass (they don't assert on the removed back-link or outer wrapper).

- [ ] **Step 5: Commit**

```bash
git add daiv/activity/templates/activity/activity_list.html daiv/activity/templates/activity/activity_detail.html daiv/activity/views.py
git commit -m "refactor(activity): migrate list and detail to base_app; add breadcrumb on detail"
```

---

## Task 9 — Migrate schedules templates (with breadcrumbs)

**Files:**
- Modify: `daiv/schedules/templates/schedules/schedule_list.html`
- Modify: `daiv/schedules/templates/schedules/schedule_form.html`
- Modify: `daiv/schedules/templates/schedules/schedule_confirm_delete.html`
- Modify: `daiv/schedules/views.py` (add `breadcrumbs` to the form and delete views' context)

- [ ] **Step 1: Migrate `schedule_list.html`**

Replace the top of the file (lines 1–26) with:

```html
{% extends "base_app.html" %}
{% load dashboard_tags %}

{% block title %}Scheduled Jobs — DAIV{% endblock %}

{% block app_content %}
<div class="animate-fade-up flex items-center justify-between">
    <div>
        <h1 class="text-2xl font-bold tracking-tight">Scheduled Jobs</h1>
        <p class="mt-1.5 text-[15px] font-light text-gray-400">Automate agent runs on repositories with recurring schedules.</p>
    </div>
    <a href="{% url 'schedule_create' %}" class="btn-primary">Create schedule</a>
</div>
```

- Remove the outer `<div class="min-h-dvh">` wrapper and its closing `</div>`.
- Remove `{% include "accounts/_header.html" %}`.
- Remove `<main class="mx-auto max-w-6xl px-6 py-12">` and its `</main>`.
- Remove the "← Dashboard" link block.

- [ ] **Step 2: Migrate `schedule_form.html` with breadcrumb**

Replace the top of the file (lines 1–30) with:

```html
{% extends "base_app.html" %}
{% load i18n static %}

{% block title %}{% if object %}Edit Schedule{% else %}Create Schedule{% endif %} — DAIV{% endblock %}

{% block alpine_plugins %}
<script defer src="https://cdn.jsdelivr.net/npm/@alpinejs/ui@3.15.11/dist/cdn.min.js"
        integrity="sha384-USgPxo+ohBkt/xxOPsfCDC5BYAwgFHCatL+RFkcPCWWkvKSp5KzH52tUZZ7taB/c"
        crossorigin="anonymous"></script>
<script defer src="{% static 'codebase/js/repo-search.js' %}"></script>
{% endblock %}

{% block breadcrumb %}
{% include "accounts/_breadcrumb.html" with crumbs=breadcrumbs %}
{% endblock %}

{% block app_content %}
<div class="animate-fade-up">
    <h1 class="text-2xl font-bold tracking-tight">{% if object %}Edit schedule{% else %}Create schedule{% endif %}</h1>
    <p class="mt-1.5 text-[15px] font-light text-gray-400">
        {% if object %}Update "{{ object.name }}".{% else %}Set up a recurring agent run on a repository.{% endif %}
    </p>
</div>
```

- Apply the same outer-wrapper/main/header/back-link removals as in schedule_list.
- Preserve the remainder of the file verbatim.

- [ ] **Step 3: Migrate `schedule_confirm_delete.html` with breadcrumb**

Apply the identical pattern:
- `extends "base_app.html"`, block renamed, outer wrappers removed, `← Back` link removed, `{% block breadcrumb %}` added at the top.

- [ ] **Step 4: Supply `breadcrumbs` from the schedule views**

In `daiv/schedules/views.py`, add to `ScheduleCreateView.get_context_data`:

```python
from django.urls import reverse

# ...
context["breadcrumbs"] = [
    {"label": "Schedules", "url": reverse("schedule_list")},
    {"label": "New schedule", "url": None},
]
```

In `ScheduleUpdateView.get_context_data`:

```python
context["breadcrumbs"] = [
    {"label": "Schedules", "url": reverse("schedule_list")},
    {"label": f'"{self.object.name}"', "url": None},
]
```

In `ScheduleDeleteView.get_context_data`:

```python
context["breadcrumbs"] = [
    {"label": "Schedules", "url": reverse("schedule_list")},
    {"label": f'"{self.object.name}"', "url": reverse("schedule_update", args=[self.object.pk])},
    {"label": "Delete", "url": None},
]
```

- [ ] **Step 5: Run schedule tests**

Run: `cd daiv && uv run pytest ../tests/unit_tests/schedules/ -v`
Expected: existing tests continue to pass.

- [ ] **Step 6: Commit**

```bash
git add daiv/schedules/templates/schedules/ daiv/schedules/views.py
git commit -m "refactor(schedules): migrate list/form/delete to base_app with breadcrumbs"
```

---

## Task 10 — Migrate users templates (with breadcrumbs)

**Files:**
- Modify: `daiv/accounts/templates/accounts/users.html`
- Modify: `daiv/accounts/templates/accounts/user_form.html`
- Modify: `daiv/accounts/templates/accounts/user_confirm_delete.html`
- Modify: `daiv/accounts/views.py` (add `breadcrumbs` to the create/update/delete views)

- [ ] **Step 1: Migrate `users.html`**

- Swap `extends`, rename `content` → `app_content`.
- Remove outer `<div class="min-h-dvh">`, `_header.html` include, `<main>` wrapper, and the "← Dashboard" anchor.

Resulting top:

```html
{% extends "base_app.html" %}
{% load static %}

{% block title %}Users — DAIV{% endblock %}

{% block app_content %}
<div class="animate-fade-up flex items-center justify-between">
    <div>
        <h1 class="text-2xl font-bold tracking-tight">Users</h1>
        <p class="mt-1.5 text-[15px] font-light text-gray-400">Manage who has access to DAIV.</p>
    </div>
    <a href="{% url 'user_create' %}" class="btn-primary">Create user</a>
</div>
```

- [ ] **Step 2: Migrate `user_form.html` with breadcrumb**

Apply the same base swap + wrapper removals. Add `{% block breadcrumb %}` populated from `breadcrumbs` context.

- [ ] **Step 3: Migrate `user_confirm_delete.html` with breadcrumb**

Same pattern.

- [ ] **Step 4: Supply `breadcrumbs` from the user views**

In `daiv/accounts/views.py`:

```python
# In UserCreateView.get_context_data:
context["breadcrumbs"] = [{"label": "Users", "url": reverse("user_list")}, {"label": "New user", "url": None}]

# In UserUpdateView.get_context_data:
context["breadcrumbs"] = [{"label": "Users", "url": reverse("user_list")}, {"label": self.object.email, "url": None}]

# In UserDeleteView.get_context_data:
context["breadcrumbs"] = [
    {"label": "Users", "url": reverse("user_list")},
    {"label": self.object.email, "url": reverse("user_update", args=[self.object.pk])},
    {"label": "Delete", "url": None},
]
```

- [ ] **Step 5: Run accounts tests**

Run: `cd daiv && uv run pytest ../tests/unit_tests/accounts/test_views.py -v`
Expected: existing tests continue to pass.

- [ ] **Step 6: Commit**

```bash
git add daiv/accounts/templates/accounts/users.html daiv/accounts/templates/accounts/user_form.html daiv/accounts/templates/accounts/user_confirm_delete.html daiv/accounts/views.py
git commit -m "refactor(accounts): migrate users templates to base_app with breadcrumbs"
```

---

## Task 11 — Migrate remaining templates (no breadcrumb)

**Files:**
- Modify: `daiv/accounts/templates/accounts/api_keys.html`
- Modify: `daiv/core/templates/core/site_configuration.html`
- Modify: `daiv/notifications/templates/notifications/channels_page.html`
- Modify: `daiv/notifications/templates/notifications/notification_list.html`

For **each** of these four files, apply the same mechanical migration:
1. `{% extends "base.html" %}` → `{% extends "base_app.html" %}`.
2. `{% block content %}` → `{% block app_content %}`.
3. Remove the outer `<div class="min-h-dvh">` wrapper (and its closing `</div>` at end of file).
4. Remove `{% include "accounts/_header.html" %}`.
5. Remove the `<main class="...">` wrapper and its closing `</main>` (shell supplies the main).
6. Remove any "← Dashboard" / "← Back" anchor blocks.

Note — `site_configuration.html` passes `header_max_w="max-w-6xl"` to the header include. That's no longer needed; the shell provides a `content_max_w` block variable. Leave the existing inner max-widths alone; the shell already wraps in `max-w-6xl` by default.

Note — `notification_list.html` currently uses `<main class="mx-auto max-w-3xl ...">`. To preserve the narrower layout for readability, the template should pass a narrower wrapper explicitly:

```django
{% block app_content %}
<div class="mx-auto max-w-3xl">
  ...existing content...
</div>
{% endblock %}
```

(`channels_page.html` has the same max-w-3xl pattern — apply the same treatment.)

- [ ] **Step 1: Migrate `api_keys.html`**

Apply steps 1–6 above.

- [ ] **Step 2: Migrate `site_configuration.html`**

Apply steps 1–6. Drop the `with header_max_w="max-w-6xl"` argument on the (now removed) header include.

- [ ] **Step 3: Migrate `channels_page.html`**

Apply steps 1–6. Wrap the inner content in `<div class="mx-auto max-w-3xl">...</div>` to preserve its narrower reading width.

- [ ] **Step 4: Migrate `notification_list.html`**

Apply steps 1–6. Same `max-w-3xl` inner wrapper as channels.

- [ ] **Step 5: Run full unit test suite**

Run: `make test` (from repo root)
Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add daiv/accounts/templates/accounts/api_keys.html daiv/core/templates/core/site_configuration.html daiv/notifications/templates/notifications/channels_page.html daiv/notifications/templates/notifications/notification_list.html
git commit -m "refactor: migrate api-keys, configuration, and notifications to base_app"
```

---

## Task 12 — Sidebar role-visibility, running-badge, and smoke tests

**Files:**
- Create: `tests/unit_tests/accounts/test_sidebar.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit_tests/accounts/test_sidebar.py
from django.test import Client
from django.urls import reverse

import pytest

from accounts.models import Role, User
from activity.models import Activity, ActivityStatus, TriggerType


@pytest.fixture
def member(db):
    return User.objects.create_user(username="alice", email="alice@test.com", password="x123456789")  # noqa: S106


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="admin",
        email="admin@test.com",
        password="x123456789",  # noqa: S106
        role=Role.ADMIN,
    )


def _client(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.mark.django_db
class TestSidebarSmoke:
    @pytest.mark.parametrize(
        "url_name,kwargs_fn",
        [
            ("dashboard", lambda u: {}),
            ("activity_list", lambda u: {}),
            ("schedule_list", lambda u: {}),
            ("user_channels", lambda u: {}),
            ("api_keys", lambda u: {}),
        ],
    )
    def test_sidebar_present_on_every_section_root(self, member, url_name, kwargs_fn):
        response = _client(member).get(reverse(url_name, kwargs=kwargs_fn(member)))
        assert response.status_code == 200
        assert b'data-testid="app-sidebar"' in response.content
        assert b'data-testid="app-user-menu"' in response.content


@pytest.mark.django_db
class TestAdminGroupVisibility:
    def test_admin_sees_admin_group(self, admin):
        response = _client(admin).get(reverse("dashboard"))
        assert b'data-testid="nav-admin-group"' in response.content
        assert b"Users" in response.content
        assert b"Configuration" in response.content

    def test_member_does_not_see_admin_group(self, member):
        response = _client(member).get(reverse("dashboard"))
        assert b'data-testid="nav-admin-group"' not in response.content


@pytest.mark.django_db
class TestRunningJobsBadge:
    def test_no_badge_when_zero_running(self, member):
        response = _client(member).get(reverse("dashboard"))
        assert b'data-testid="nav-running-badge"' not in response.content

    def test_badge_shows_count_when_running(self, member):
        Activity.objects.create(
            status=ActivityStatus.RUNNING, trigger_type=TriggerType.MCP_JOB, user=member, repo_id="daiv/api"
        )
        Activity.objects.create(
            status=ActivityStatus.RUNNING, trigger_type=TriggerType.MCP_JOB, user=member, repo_id="daiv/api"
        )
        response = _client(member).get(reverse("dashboard"))
        assert b'data-testid="nav-running-badge"' in response.content
        assert b"2 running" in response.content


@pytest.mark.django_db
class TestNavActiveState:
    """Satisfies spec §5: for each section key, render a representative page and
    confirm the correct sidebar item carries the active CSS classes."""

    @pytest.mark.parametrize(
        "url_name,expected_section",
        [
            ("dashboard", "dashboard"),
            ("activity_list", "activity"),
            ("schedule_list", "schedules"),
            ("user_channels", "channels"),
            ("api_keys", "api_keys"),
        ],
    )
    def test_active_section_matches_url(self, admin, url_name, expected_section):
        response = _client(admin).get(reverse(url_name))
        assert response.status_code == 200
        assert response.context["nav_active_section"] == expected_section

    def test_admin_only_sections_resolve_for_admin(self, admin):
        users_response = _client(admin).get(reverse("user_list"))
        assert users_response.context["nav_active_section"] == "users"
        config_response = _client(admin).get(reverse("site_configuration"))
        assert config_response.context["nav_active_section"] == "configuration"
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd daiv && uv run pytest ../tests/unit_tests/accounts/test_sidebar.py -v`
Expected: PASS (12 tests across 4 classes — 5 parametrized sidebar-smoke, 2 admin-group, 2 running-badge, 5 parametrized + 2 admin-only = 7 active-state tests).

- [ ] **Step 3: Commit**

```bash
git add tests/unit_tests/accounts/test_sidebar.py
git commit -m "test(accounts): cover sidebar presence, admin-group visibility, and running badge"
```

---

## Task 13 — Breadcrumb tests

**Files:**
- Create: `tests/unit_tests/accounts/test_breadcrumbs.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit_tests/accounts/test_breadcrumbs.py
from django.test import Client
from django.urls import reverse

import pytest

from accounts.models import Role, User
from schedules.models import ScheduledJob


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="admin",
        email="admin@test.com",
        password="x123456789",
        role=Role.ADMIN,  # noqa: S106
    )


def _client(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.mark.django_db
class TestBreadcrumbs:
    def test_activity_list_has_no_breadcrumb(self, admin):
        response = _client(admin).get(reverse("activity_list"))
        assert b'data-testid="app-breadcrumb"' not in response.content

    def test_schedule_create_breadcrumb(self, admin):
        response = _client(admin).get(reverse("schedule_create"))
        assert b'data-testid="app-breadcrumb"' in response.content
        assert b"Schedules" in response.content
        assert b"New schedule" in response.content

    def test_user_create_breadcrumb(self, admin):
        response = _client(admin).get(reverse("user_create"))
        assert b'data-testid="app-breadcrumb"' in response.content
        assert b"Users" in response.content
        assert b"New user" in response.content
```

- [ ] **Step 2: Run tests**

Run: `cd daiv && uv run pytest ../tests/unit_tests/accounts/test_breadcrumbs.py -v`
Expected: PASS (3 tests).

- [ ] **Step 3: Commit**

```bash
git add tests/unit_tests/accounts/test_breadcrumbs.py
git commit -m "test: cover breadcrumb presence and content on sub-pages"
```

---

## Task 14 — Delete `_header.html`, translations, final lint

**Files:**
- Delete: `daiv/accounts/templates/accounts/_header.html`
- Regenerate: `.po` files via `make makemessages`

- [ ] **Step 1: Verify `_header.html` has no remaining callers**

Run: `grep -r '_header.html' daiv/ tests/`
Expected: no matches (all former callers migrated in Tasks 7–11).

If any matches remain, return to the relevant task and finish its migration before proceeding.

- [ ] **Step 2: Delete the file**

```bash
rm daiv/accounts/templates/accounts/_header.html
```

- [ ] **Step 3: Regenerate translations**

The sidebar and user menu introduce new translatable strings ("Dashboard", "Activity", "Schedules", "Notification channels", "API keys", "Account", "Admin", "Users", "Configuration", "API documentation", "Open navigation", "Sign out", "{n} running").

Run: `make makemessages`
Then edit each generated `.po` file with the appropriate translations for existing locales (preserve the existing conventions).
Then run: `make compilemessages`.

- [ ] **Step 4: Run the full test suite**

Run: `make test`
Expected: all tests pass, no regressions.

- [ ] **Step 5: Run lint-fix and type-check**

Run: `make lint-fix && make lint-typing`
Expected: no errors remain.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: remove obsolete _header.html and regenerate translations"
```

---

## Notes for the implementer

- The `_header.html` include currently appears in 13 templates. All 13 are migrated in Tasks 7–11. If a template that uses `_header.html` is added during this work, migrate it before deleting `_header.html` in Task 14.
- The `_sidebar.html` partial is included twice — once for desktop (hidden on `<sm`) and once inside the mobile drawer. Both instances render from the same template to keep a single source of truth.
- Keep all Tailwind class strings exactly as written — the project uses JIT compilation of `daiv/static/css/styles.css` from source, and arbitrary-value classes like `bg-white/[0.06]` are only emitted if they appear literally in templates. Do not refactor to shorter variants mid-implementation.
- The spec's breadcrumb for the Activity detail page is `Activity › Run #<id> — <repo>`. The `<id>` is whatever short, user-recognizable identifier the `Activity` model provides. If the model has no short-form field, use the first 8 characters of `pk`.
- Translations: every new `{% translate %}` or `{% blocktranslate %}` usage lands in the `.po` files after `make makemessages`. Existing translation coverage in the project is English-primary; follow whatever conventions the existing `.po` files use (in particular, leave English msgstrs empty if the project does that).
- HTMX considerations: the sidebar, utility strip, and breadcrumb are all static chrome. They do not participate in HTMX swaps. If a view returns a partial via HTMX, it must not also include the app shell.
