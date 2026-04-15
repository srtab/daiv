# Website navigation redesign — design spec

**Date:** 2026-04-15
**Status:** Design approved, ready for implementation plan
**Scope:** Dashboard-authenticated pages only (not the public homepage or allauth flows)

## 1. Motivation

Two pain points drive this work:

1. **Pain A — Can't switch sections quickly.** Today, every sub-page (Activity, Schedules, Configuration, etc.) only has a "← Dashboard" back link. To go from Activity to Schedules, the user must bounce through the dashboard.
2. **Pain B — "Where am I?"** There is no persistent "you are here" indicator beyond the page `<h1>`. Deep pages feel disorienting.

Both are solved by introducing persistent primary navigation with a clear active state, plus breadcrumbs on sub-pages.

Non-goals for this iteration (explicitly out of scope):

- No rework of intra-section flows (Activity detail layout, Configuration sub-nav, Schedule form, etc.). Those keep their current internal structure.
- No command palette (Cmd+K).
- No collapse-to-rail sidebar toggle.
- No change to the public homepage, login pages, or OAuth authorize screens — all of those keep their current standalone layout.

## 2. Solution shape

A **persistent left sidebar** with primary nav, a **top-right utility strip** (bell + user menu), and **breadcrumbs on sub-pages only**. Role-aware visibility hides the Admin group from non-admins.

### 2.1 Sidebar

Fixed-width rail (240px) on the left, full-height, visible on every dashboard-authenticated page. Contains:

| Order | Item | Icon | Group | Visible to | Notes |
|-------|------|------|-------|------------|-------|
| 1 | Dashboard | home | — | all | — |
| 2 | Activity | bolt | — | all | Shows live badge: "N running" when N > 0 |
| 3 | Schedules | clock | — | all | — |
| 4 | Notification channels | bell | Account | all | Configures delivery targets (Slack, email) |
| 5 | API keys | key | Account | all | — |
| 6 | Users | users | Admin | admins only | Entire "Admin" group is hidden for non-admins |
| 7 | Configuration | cog-6-tooth | Admin | admins only | Keeps its existing internal sub-nav |
| 8 | API documentation ↗ | code-bracket | Footer | all | Pinned at the bottom, opens in same tab |

Grouping is rendered as small uppercase labels ("ACCOUNT", "ADMIN") with top padding. The first three items (daily work) have no group label — they sit directly under the brand.

**Active state:** background `bg-white/[0.06]`, text `text-white`, plus a 3px violet accent bar (`bg-violet-500`) flush to the left edge of the rail. One item is active at a time, determined by URL prefix (see §2.5).

**Icons:** pulled from the existing `{% icon %}` template tag (already in use across the project). Icon + label, not icons-only.

**Live badge on Activity:** when `activity.running > 0` (the number is already computed by the dashboard view for its hero card), the sidebar shows "N running" as a pill badge next to the label. When zero, no badge. Value is computed once per request via a context processor so every template has it.

### 2.2 Utility strip (top-right)

A thin bar above the page content, aligned to the right edge:

- **Notification bell** — the existing `notifications/_bell.html` dropdown, unchanged in behavior. Kept as-is.
- **User menu** — avatar (first letter of name/email on a gradient background) + email + chevron. Clicking opens a dropdown containing:
  - User email (header, non-clickable)
  - Divider
  - **Sign out** — moves from the current header chrome into this menu.

The utility strip is the same height (approx 52px) across all pages. Content begins below it.

### 2.3 Breadcrumbs

Breadcrumbs appear **only on sub-pages** — pages deeper than a section root. Section roots (Activity list, Schedules list, Users list, Configuration, API keys, Notification channels, Dashboard) do not show a breadcrumb because the active sidebar item already tells the user where they are.

**Breadcrumb catalog:**

| Page | Breadcrumb |
|------|-----------|
| Dashboard | — |
| Activity list | — |
| Activity detail | `Activity › Run #<id> — <repo>` |
| Schedules list | — |
| Schedule create | `Schedules › New schedule` |
| Schedule edit | `Schedules › "<schedule name>"` |
| Schedule delete confirm | `Schedules › "<schedule name>" › Delete` |
| Users list | — |
| User create | `Users › New user` |
| User edit | `Users › <email>` |
| User delete confirm | `Users › <email> › Delete` |
| Configuration | — (internal sub-nav remains) |
| API keys | — |
| Notification channels | — |
| Notification inbox (full-page) | — (detached page, no sidebar item is active) |

**Rules:**
- Last segment is the current page — rendered as plain text (not a link).
- Preceding segments are links to their respective pages (e.g. "Activity" links to the activity list).
- Separator: `›` (U+203A), muted color.
- Font size and color match the current "← Dashboard" back-link style so the visual weight is unchanged.
- The existing "← Dashboard" back-link on every sub-page is **removed** — the breadcrumb (where present) and the sidebar replace it.

### 2.4 Role-aware visibility

- The **Admin group label and its items (Users, Configuration)** are omitted entirely from the sidebar for non-admin users. No disabled or greyed-out state — the items simply do not render.
- The existing `AdminRequiredMixin` continues to enforce access at the view layer; the sidebar's hiding is a UX convenience, not a security boundary.
- A non-admin with a direct URL to an admin page hits the existing mixin's redirect — unchanged behavior.

### 2.5 Active item detection

The active sidebar item is determined by a helper template tag (e.g. `{% nav_active 'activity' %}`) that takes the section key and checks it against the current URL. Matching is done by resolved URL namespace / named URL, **not by substring match on the path**, to avoid the `/dashboard/` prefix matching every sub-section.

| Section key | Matches when the current named URL is… |
|-------------|---------------------------------------|
| dashboard | `dashboard` (exact — the dashboard root view only) |
| activity | any URL in the `activity` namespace (e.g. `activity_list`, `activity_detail`, `activity_stream`) |
| schedules | any URL in the `schedules` namespace |
| channels | `user_channels` |
| api_keys | `api_keys` (and any other URL defined in `accounts.urls.api_keys`) |
| users | any URL in `accounts.urls.users` (`user_list`, `user_create`, `user_update`, `user_delete`) |
| configuration | `site_configuration` |

**Detached pages** — `notifications:list` (the full-page notification inbox, reached via the bell's "View all") does **not** activate any sidebar item. The bell in the utility strip is its primary entry point; the sidebar has no duplicate link. This is explicit, not an oversight.

The helper returns the `active` CSS classes when matched, empty otherwise. This avoids hardcoding URL string comparisons in each template.

### 2.6 Mobile (<640px)

- Sidebar becomes an **off-canvas drawer**, hidden by default.
- A hamburger icon appears in the top-left of the page (where the brand used to be in the header). Tapping opens the drawer.
- The drawer slides in from the left, takes ~85% of viewport width, and dims the rest with a semi-transparent overlay. Tapping outside or the close icon dismisses.
- The utility strip's bell and user menu remain visible in the top-right; the hamburger replaces the brand on the left. On tablet+ (≥640px), the full sidebar reappears and the hamburger disappears.
- Implementation: Alpine.js `x-data` state on the page shell, standard off-canvas pattern.

## 3. Layout & components

### 3.1 Page shell

A new `base_app.html` template extends `base.html` and provides the app shell:

```
base.html (existing — <head>, messages, fonts)
└── base_app.html (NEW — sidebar + utility strip + {% block app_content %})
    ├── accounts/dashboard.html
    ├── activity/activity_list.html
    ├── activity/activity_detail.html
    ├── schedules/schedule_list.html
    ├── schedules/schedule_form.html
    ├── schedules/schedule_confirm_delete.html
    ├── accounts/users.html
    ├── accounts/user_form.html
    ├── accounts/user_confirm_delete.html
    ├── accounts/api_keys.html
    ├── core/site_configuration.html
    ├── notifications/channels_page.html
    └── notifications/notification_list.html
```

`base.html` (and any public-facing pages such as `accounts/homepage.html`, allauth templates) stays untouched.

### 3.2 New/changed template partials

- **NEW** `accounts/templates/accounts/_sidebar.html` — the sidebar rendering, reads `user` and a nav context processor to compute active state and the running-jobs badge.
- **NEW** `accounts/templates/accounts/_user_menu.html` — user-menu dropdown (Alpine-driven).
- **NEW** `accounts/templates/accounts/_breadcrumb.html` — takes a list of `(label, url_or_none)` pairs and renders the crumb.
- **NEW** `accounts/templates/base_app.html` — the app shell described above.
- **CHANGED** every template listed in §3.1 — switch `extends "base.html"` to `extends "base_app.html"`, remove the `{% include "accounts/_header.html" %}` line, remove the hard-coded "← Dashboard" link, and add a `{% block breadcrumb %}{% endblock %}` populated on sub-pages.
- **CHANGED** `accounts/templates/accounts/_header.html` — retained only for any pages that truly need the old chrome (likely none after migration). If no callers remain, this file is deleted.
- **REMOVED** from `dashboard.html` — the "Quick Links" grid. Its items (Users, Configuration, API Keys, Activity, Schedules, API Docs) are now always one click away via the sidebar, so they're redundant. The dashboard reclaims that vertical space for its stats sections.

### 3.3 Context processor

A new `accounts.context_processors.nav` supplies every request with:

```python
{
    "nav_running_jobs": int,  # count of currently-running activities, 0 if none
    "nav_active_section": str,  # resolved section key, e.g. "activity", "schedules"
}
```

Registered in `daiv/settings/components/common.py` under `TEMPLATES[0]["OPTIONS"]["context_processors"]`.

The running-jobs count is already calculated for the dashboard hero card; it's lifted into the context processor and cached per-request (not per-user or across requests — it's a cheap count but shouldn't run twice per render).

## 4. Interaction details

- **Link transitions:** use existing Tailwind transition utilities (`transition-colors duration-200`) so hover states match the current visual language.
- **Keyboard:** all nav items are links (`<a>`), so tab-nav works natively. The user-menu dropdown opens on Enter/Space when focused.
- **HTMX:** today many interactions use HTMX. No change — the sidebar is static chrome that doesn't participate in HTMX swaps. `hx-boost` is not introduced in this work.
- **Alpine.js:** the user-menu dropdown and the mobile drawer use Alpine, matching the rest of the project.

## 5. Testing

- **View tests:** for each view in §3.1, add an assertion that the rendered response contains the sidebar (look for a stable marker like `data-testid="app-sidebar"` on the `<aside>`).
- **Nav active-state tests:** for each section key in §2.5, render one representative page and assert that the correct `<a>` has the `active` class.
- **Role visibility tests:** render the sidebar for (a) an admin and (b) a non-admin, assert that the Admin group and its items are present in (a) and absent in (b).
- **Running-jobs badge test:** one test with zero running → no badge; one with N>0 → badge shows the count.
- **Breadcrumb tests:** for each sub-page in §2.3, assert the breadcrumb renders with the expected labels and link targets.
- No new integration tests required; the existing ones continue to hit the same URL patterns.

## 6. Migration / rollout

One-shot migration in a single PR — this is a chrome-only change, no database migrations, no API changes, no feature flags. Every affected template is updated in the same commit so there is no transitional state where some pages have the sidebar and others don't.

Translations: any new user-facing strings (group labels "ACCOUNT", "ADMIN", "Sign out" if previously sourced from another key) go through the project's standard `{% translate %}` / `gettext` pipeline and `.po` files get regenerated with `make makemessages`.

## 7. Out of scope (for reference)

Explicitly deferred — not part of this work:

- Command palette (Cmd+K to jump to any page)
- Sidebar collapse-to-rail toggle (icons-only mode)
- Bottom tab bar on mobile
- Reworking the Activity detail page layout
- Reworking the Configuration page sub-nav
- Recent/starred pages or pinning
- Global search

Any of these can be added later without contradicting the architecture here.
