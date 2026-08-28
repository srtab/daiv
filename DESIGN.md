# Design System Rules

This document defines the design system for DAIV's UI. Follow these rules when creating or modifying any template, component, or style.

## Stack

| Layer         | Technology                           | Notes                                          |
|---------------|--------------------------------------|-------------------------------------------------|
| Templates     | Django templates (server-rendered)   | No SPA; all pages extend `base.html`            |
| CSS           | **Tailwind CSS v4** (utility-first)  | Source: `daiv/static_src/css/input.css`          |
| Interactivity | **Alpine.js 3.15** + Alpine UI       | Loaded from CDN; kept minimal                   |
| Icons         | SVG via CSS-mask `{% icon %}` tag    | `daiv/core/static/core/img/icons/`              |
| Build         | Tailwind standalone CLI              | `make tailwind-build` / `make tailwind-watch`   |

There is no separate `tailwind.config.js` — all theme configuration lives inside `input.css` using Tailwind v4's `@theme` block.

## Token Definitions

### Typography

| Token              | Value                                                       |
|--------------------|-------------------------------------------------------------|
| Sans family        | `"Geist"`, ui-sans-serif, system-ui, sans-serif (`font-sans`) — prose, headings, section headers, body |
| Mono family        | `"Geist Mono"`, ui-monospace, SFMono-Regular, monospace (`font-mono`) — all *data*: counts, timestamps, repo tags, uppercase eyebrow/label roles |
| Hosting            | **Self-hosted woff2** at `daiv/static/fonts/geist/` (variable weight `100 900`, `font-display: swap`, latin subset covers ã ç õ é). `@font-face` lives in `input.css`. **No font CDN.** |
| Body text          | `text-[14px]` regular, `text-text` or `text-text-muted`    |
| Small / meta text  | `text-[13px]` or `text-[12px]`, often `font-mono`           |
| Headings           | `font-semibold` or `font-bold`, `text-text` or `text-text-strong` |
| Uppercase labels   | `tracking-[0.14em]` to `tracking-[0.2em]`, `font-semibold`, `font-mono` |
| Tabular figures    | `tabular-nums` wherever digits align or change             |

### Color Palette

The UI is **dark-mode only** (no light theme). The app shell and every new or
restyled surface use the **semantic design-token layer** below; older pages still
use the raw Tailwind utilities in the legacy table that follows. Prefer the tokens.

#### Semantic Design Tokens (Tailwind v4 `@theme`)

Declared as `--color-*` / `--font-*` / `--shadow-*` in `input.css`'s `@theme`
block, so utilities generate automatically (`bg-ground`, `text-text-muted`,
`border-border`, `text-status-found`, `font-mono`, `shadow-overlay`, …). `input.css`
declares each value exactly once and the table below documents the roles; **nothing
else restates a value.**

| Token | Value | Role |
|---|---|---|
| `ground` | `#0D1117` | base plane (`bg-ground` on `<body>`) |
| `surface-1` | `#10151D` | sidebar + top bar |
| `surface-2` | `#161C26` | cards + hero |
| `surface-3` | `#1E2733` | hover / inset chips |
| `border` | `#232B36` | 1px hairline separators |
| `text` / `text-strong` / `text-muted` / `text-faint` | `#E6EDF3` / `#FFFFFF` / `#9AA4B0` / `#767F8E` | text ramp |
| `brand` / `brand-bright` | `#8B5CF6` / `#A78BFA` | violet — WHO/brand & mark only, **never a CTA** |
| `accent` / `accent-bright` / `accent-ink` | `#2DD4BF` / `#5FE6D4` / `#04211D` | teal — DO/action; owns "clickable" |
| `focus` | = `accent-bright` | focus ring — reads the accent token, so the teal ramp moves as one |
| `status-clear` / `status-found` / `status-attn` / `status-fail` | `#3FB950` / `#D6A036` / `#38BDF8` / `#F85149` | green / amber / cyan / red — each AA-legible on `ground`; a status color never signals "clickable" |

Weak-tint status backgrounds = the status token at ~14–16% alpha via
`color-mix(in srgb, <token> 14%, transparent)`; such tints carry no text.

#### Legacy raw utilities (pages not yet on the tokens)

These are what most pages still use. The last column is the token each row becomes, so
migrating a page is mechanical rather than a judgement call — take the whole page at once,
never half.

| Role             | Legacy value                       | Usage                                 | Migrates to     |
|------------------|------------------------------------|---------------------------------------|-----------------|
| Surface          | `bg-white/[0.02]`                  | Cards, containers                     | `surface-2`     |
| Surface hover    | `bg-white/[0.04]`                  | Card hover state                      | `surface-3`     |
| Surface elevated | `bg-white/[0.06]`                  | Inline code, subtle wells             | `surface-3`     |
| Border default   | `border-white/[0.06]`             | Card borders, dividers, form inputs   | `border`        |
| Border hover     | `border-white/[0.12]`             | Interactive hover borders             | `border`        |
| Border focus     | `border-white/[0.15]`             | Focused inputs                        | `focus` (ring)  |
| Text primary     | `text-white`                       | Headings, strong content              | `text-strong`   |
| Text secondary   | `text-gray-300`                    | Body text                             | `text`          |
| Text tertiary    | `text-gray-400`                    | Labels, meta, descriptions            | `text-muted`    |
| Text muted       | `text-gray-500` / `text-gray-600`  | Placeholders, captions                | `text-faint`    |
| Select bg        | `bg-[#0d1117]`                     | `<select>` dropdown background        | `surface-2`     |

### Semantic Colors

**Legacy until migrated** — the four rows below are the pre-token spelling of the four
`status-*` tokens (success→`status-clear`, warning→`status-found`, info→`status-attn`,
error→`status-fail`). New surfaces use the tokens.

| Semantic   | Border                    | Background              | Text              |
|------------|---------------------------|-------------------------|--------------------|
| Success    | `border-emerald-800/50`   | `bg-emerald-950/80`    | `text-emerald-200` |
| Warning    | `border-amber-800/50`     | `bg-amber-950/80`      | `text-amber-200`   |
| Error      | `border-red-800/50`       | `bg-red-950/80`        | `text-red-200`     |
| Info       | `border-gray-800/50`      | `bg-gray-900/80`       | `text-gray-300`    |

### Spacing Scale

Use Tailwind's default spacing scale. Common values:

- **Page padding**: `px-4`, `sm:px-(--app-content-gutter)` (1.5rem) — see §Layout
- **Section gaps**: `gap-6`, `mt-8`
- **Card padding**: `p-6`
- **Component gaps**: `gap-4`, `gap-3`, `gap-2`
- **Inline spacing**: `gap-2`, `gap-1.5`

### Border Radius

| Element         | Radius          |
|-----------------|-----------------|
| Cards           | `rounded-2xl`   |
| Buttons/inputs  | `rounded-xl`    |
| Small controls  | `rounded-lg`    |
| Badges/pills    | `rounded-full`  |

### Layout

- **Content width — `container_width` tier system.** `base_app.html` exposes a
  `{% block container_width %}` whose allowed values are `max-w-3xl` (narrow),
  `max-w-6xl` (default), `max-w-screen-2xl` (wide), or `max-w-none` (fluid). Pick
  the tier per page; don't hard-code arbitrary widths.
- **Horizontal padding**: `px-4`, `sm:px-(--app-content-gutter)` (1.5rem) — the shell's own gutter, which the bottom sheets inset by
- **Responsive breakpoints**: mobile-first; `sm:` (640px), `lg:` (1024px), `xl:` (1280px).
  The app shell itself reflows at **768px** (`md:` — sidebar → sheet + bottom tab
  bar) and **1024px** (`lg:` — icon rail → full sidebar). `--app-sidebar-width` *is*
  that second tier — 4rem, widened to 15rem in a `:root` switch at `lg:` — so the
  sidebar's width lives in one place and everything keyed to it (the `--sheet-inset-*`
  switch, which tracks `md:` rather than `sm:`) follows both tiers for free.
- **Flat elevation.** Depth is built from the `surface-1 → surface-2 → surface-3`
  ramp plus hairline `border-border` — **persistent surfaces carry no box-shadow.**
  The single sanctioned shadow (`shadow-overlay`, `0 16px 40px -24px rgba(0,0,0,.85)`)
  is reserved for transient overlays (menus / popovers / dialogs).
- **Focus & keyboard.** A global `:focus-visible` teal ring
  (`outline: 2px solid var(--color-focus); outline-offset: 2px`) applies to every
  interactive element that doesn't draw its own — the chat composer and the pickers
  still carry hand-rolled `:focus-visible` rings in `@layer components`, which win.
  New components should rely on the global one. `Esc` closes the topmost
  drawer/popover. No command palette.
- Grid columns: single on mobile, multi-column at `sm:` and `lg:`

## Component Library

All components are Django templates. Reusable partials are **underscore-prefixed** (`_component.html`).

### Buttons

**Legacy until migrated** — `.btn-primary`'s white fill predates the token layer, where
teal `accent` owns "clickable" (see the sidebar's `bg-accent text-accent-ink` CTA). Keep
using these classes on pages still on the legacy utilities; a page moved to the tokens
moves its primary action to `accent` at the same time.

Defined as Tailwind `@layer components` classes in `input.css`:

```html
<!-- Primary (white bg, dark text) -->
<button class="btn-primary">Save</button>

<!-- Secondary (translucent, gray text) -->
<button class="btn-secondary">Cancel</button>

<!-- Danger (red bg) -->
<button class="btn-danger">Delete</button>

<!-- Danger outline (red tinted) -->
<button class="btn-danger-outline">Revoke</button>
```

All buttons share: `rounded-xl px-5 py-2.5 text-[14px]`, transition animations, `active:scale-[0.98]`.

For smaller inline buttons (e.g. pagination, header sign-out), override with `rounded-lg px-3.5 py-1.5`.

### Cards

Standard card pattern — use consistently everywhere:

```html
<div class="rounded-2xl border border-white/[0.06] bg-white/[0.02] p-6">
  <!-- card content -->
</div>
```

Interactive (linked) cards add hover:

```html
<a href="..." class="group rounded-2xl border border-white/[0.06] bg-white/[0.02] p-6
                      transition-all duration-200 hover:border-white/[0.1] hover:bg-white/[0.04]">
  <!-- card content -->
</a>
```

### Quick Link Card

Reusable partial at `accounts/templates/accounts/_quick_link_card.html`:

```django
{% include "accounts/_quick_link_card.html" with url=target_url icon="key" title="API Keys" description="Manage tokens" badge=count %}
```

Accepts: `url`, `icon` (icon name), `title`, `description`, `badge` (optional).

### Page Header

Every page title with actions at its top right is a `.page-header` holding exactly two
children: the title block, which must carry `.page-header__title` (that class is what
holds it open), and the action — a single control, or a `.page-header__actions` cluster
when there is more than one:

```html
<div class="animate-fade-up page-header">
    <div class="page-header__title">
        <h1 class="text-2xl font-bold tracking-tight">Skills</h1>
        <p class="mt-1.5 text-[15px] font-light text-gray-400">Description.</p>
    </div>
    <a href="…" class="btn-primary">Upload skill</a>
</div>
```

The title block takes whatever the actions leave and wraps its own text; the actions
drop to a line of their own once it would go below `16rem`. **Never add a breakpoint
for this**: a viewport breakpoint can't see the shell beside it — the sidebar is a
64px rail from `md:` and 240px from `lg:`, so the content area a `sm:`/`lg:` rule
switches on is never the width the rule names. The same wrap — grow the
details, floor them, let the controls fall below — is how list rows with trailing
controls stack (`mcp_servers/_server_list.html`), and it adapts per row when the
controls are conditional.

### Badges / Pills

```html
<span class="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-0.5 text-[12px] font-medium text-gray-400">
    Label
</span>
```

Status variants use semantic colors (e.g. `bg-emerald-950/80 text-emerald-200` for active).

### Form Fields

Form inputs are styled globally in `@layer base` inside `input.css` — no per-field classes needed. The standard field template is at `core/templates/core/fields/default.html`:

```html
<div>
    <label class="flex items-center gap-1 text-[14px] font-medium text-gray-400">Field Label</label>
    <div class="mt-2">{{ field }}</div>
    <p class="mt-1.5 text-[13px] text-red-400">Error message</p>       <!-- if errors -->
    <p class="mt-1.5 text-[13px] text-gray-400">Help text</p>          <!-- if help_text -->
</div>
```

### Toast Messages

Auto-dismissing notifications anchored `fixed top-5 right-5 z-50`. Color-coded by Django message tag (error, success, warning, info). Staggered `animate-fade-up` animation. Auto-dismiss after 5 seconds.

### Pagination

Reusable partial at `accounts/templates/accounts/_pagination.html`:

```django
{% include "accounts/_pagination.html" %}
```

Requires `is_paginated` and `page_obj` in template context (standard Django `ListView`).

### App Shell

`base_app.html` is the chrome every signed-in page inherits: sidebar, top bar,
scrolling `<main>`, and — below `md:` — a mobile nav sheet plus a four-tab bottom bar.

- **Tiers.** `< md` sheet + bottom tab bar; `md–lg` icon rail; `>= lg` full sidebar.
- **Sidebar hooks.** Anything the rail must hide carries `sidebar__collapsible`; the
  elements it re-centres carry `sidebar__brand` / `sidebar__cta` / `sidebar__nav-item` /
  `sidebar__footer-link`, and a group heading keeps its box via `sidebar__group-heading`.
  A new nav item wraps its text in `sidebar__collapsible`, or the label overflows the
  4rem rail. The mobile sheet includes the same partial *without* `sidebar--rail`, so it
  keeps its labels. A nav item's appearance is the `.sidebar__nav-item` component class,
  not a utility chain, and its active state comes from `{% nav_active %}` — which adds
  `sidebar__nav-item--active`, the class that draws the 3px brand rail.
- **Bottom tab bar.** The four tabs are `NAV_TABS` in `accounts/context_processors.py`,
  beside the section keys they highlight against; `.tabbar__link` carries their appearance
  and the ≥44px touch-target floor. Height is `--app-tabbar-height`, which `<main>` pads by
  below `md` — that padding is also what keeps the chat surface's sticky dock off the bar.
- **Top bar slot.** `{% block topbar_start %}` holds page-specific controls; empty by default.

### Header

Reusable partial at `accounts/templates/accounts/_header.html`:

```django
{% include "accounts/_header.html" with header_max_w="max-w-7xl" %}
```

Defaults to `max-w-5xl`. Contains logo + user name + sign-out button.

### Prose / Markdown Content

Use the `.prose-dark` component class for rendered markdown inside dark containers:

```html
<div class="prose-dark">
    {{ rendered_markdown }}
</div>
```

Defined in `input.css` under `@layer components`. Handles headings, lists, links, code blocks, blockquotes, tables, and horizontal rules.

## Icon System

Icons are SVGs rendered via a CSS-mask technique for easy theming with `currentColor`.

### Adding a New Icon

1. Place the SVG file in `daiv/core/static/core/img/icons/<name>.svg`
2. Use in templates: `{% load icon_tags %}{% icon "<name>" "<css-classes>" %}`

### Usage Pattern

```django
{% load icon_tags %}

<!-- Standard icon -->
{% icon "key" "h-5 w-5 text-gray-400" %}

<!-- Icon that changes color on parent hover -->
{% icon "bolt" "h-5 w-5 text-gray-400 transition-colors group-hover:text-white" %}
```

### Available Icons

See `daiv/core/static/core/img/icons/` — the filename (without `.svg`) is the icon name.

### Icon Container Pattern

Icons inside cards often sit in a bordered container:

```html
<div class="flex h-10 w-10 items-center justify-center rounded-xl border border-white/[0.08] bg-white/[0.03]">
    {% icon "key" "h-5 w-5 text-gray-400" %}
</div>
```

## Animation

### Fade Up

Used for staggered entry animations on page sections and toast messages:

```html
<div class="animate-fade-up" style="animation-delay: 80ms">...</div>
<div class="animate-fade-up" style="animation-delay: 160ms">...</div>
```

Keyframe: opacity 0 + translateY(10px) to full opacity. Duration: 0.5s ease-out.

### Transitions

All interactive elements use smooth transitions:

- **Default**: `transition-all duration-200`
- **Color only**: `transition-colors duration-200`
- **Button press**: `active:scale-[0.98]`

## Alpine.js Patterns

Alpine.js is used for lightweight interactivity — **not** as a full application framework.

### Conventions

- Define data inline with `x-data="{ ... }"` for simple state
- Register reusable components via `Alpine.data()` in dedicated JS files
- Use `x-cloak` on elements that should be hidden until Alpine initializes
- Use `x-show` / `x-model` for conditional rendering and two-way binding
- Load the Alpine UI plugin for advanced components (combobox, etc.)

### Existing Reusable Components

**`repoSearch(initial)`** — Async repository search combobox (`codebase/static/codebase/js/repo-search.js`):

```html
<div x-data="repoSearch('owner/repo')">
    <div x-combobox x-model="selected" nullable>
        <input type="text" x-combobox:input @input="search($event.target.value)" ...>
        <!-- options from `results` array, each with .slug and .name -->
    </div>
</div>
```

Features: 300ms debounced search, abort controller, loading state.

## Template Architecture

### Base Template

All pages extend `accounts/templates/base.html`, which provides:

- HTML shell with `<head>` (fonts, CSS, Alpine.js, meta tags)
- `<body class="h-full bg-ground font-sans text-text antialiased">`
- Toast message system
- Blocks: `title`, `meta_description`, `meta_robots`, `canonical`, `open_graph`, `head_extra`, `alpine_plugins`, `content`

### Page Template Pattern

```django
{% extends "base.html" %}
{% load static icon_tags %}

{% block title %}Page Title — DAIV{% endblock %}

{% block content %}
{% include "accounts/_header.html" %}

<main class="mx-auto max-w-5xl px-6 py-8">
    <!-- page content -->
</main>
{% endblock %}
```

### Partial Naming

- Reusable partials: `_name.html` (underscore prefix)
- Page templates: `name.html` (no prefix)
- Located in each app's `templates/<app>/` directory

## Accessibility

- Use semantic HTML: `<nav>`, `<main>`, `<header>`, `<footer>`, `<section>`
- Add `aria-label` on navigation landmarks
- Focus is global: `input.css` puts a teal `:focus-visible` ring on everything. Only
  opt out (`focus:outline-none`) when replacing it with an equally visible ring
- Ensure color contrast: the `text` / `text-muted` / `text-faint` ramp and every
  `status-*` token are AA-legible on `ground`; check anything outside them
- Use `x-cloak` to prevent flash of unstyled Alpine content

## Responsive Design

Mobile-first approach. Common patterns:

```html
<!-- Single column on mobile, 2 columns on sm, 3 on lg -->
<div class="grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-3">

<!-- Stack on mobile, row on sm -->
<div class="flex flex-col gap-4 sm:flex-row sm:items-center">
```

Breakpoints are viewport-relative, but from `md:` up a page sits next to the shell —
a 64px icon rail, then the full `--app-sidebar-width` (15rem) from `lg:` — so the
content area a rule switches on is always narrower than the width the rule names.
Where the switch depends on whether two blocks still fit (a title and its actions, a
list row and its controls), wrap on content instead: see §Page Header.

A `position: fixed` surface has the same problem with no layout to lean on: it measures
the viewport, not the column it belongs to. From `md:` up — the width the sidebar
appears at — bottom sheets (pickers and the composer's own) are therefore inset to the
content column instead, so one never opens across the sidebar. The `--sheet-inset-*`
switch in `input.css` is keyed to `--breakpoint-md` for exactly that reason: move the
sidebar's tier and that switch moves with it.

## File Paths Reference

| What                    | Path                                          |
|-------------------------|-----------------------------------------------|
| Tailwind source         | `daiv/static_src/css/input.css`               |
| Compiled CSS            | `daiv/static/css/styles.css`                  |
| Base template           | `daiv/accounts/templates/base.html`           |
| App shell template      | `daiv/accounts/templates/base_app.html`       |
| Sidebar partial         | `daiv/accounts/templates/accounts/_sidebar.html` |
| Self-hosted fonts       | `daiv/static/fonts/geist/`                    |
| Header partial          | `daiv/accounts/templates/accounts/_header.html` |
| Pagination partial      | `daiv/accounts/templates/accounts/_pagination.html` |
| Quick link card partial | `daiv/accounts/templates/accounts/_quick_link_card.html` |
| Default field template  | `daiv/core/templates/core/fields/default.html` |
| Icon template           | `daiv/core/templates/core/icons/_icon.html`   |
| Icon SVGs               | `daiv/core/static/core/img/icons/`            |
| Icon template tag       | `daiv/core/templatetags/icon_tags.py`         |
| Repo search component   | `daiv/codebase/static/codebase/js/repo-search.js` |
