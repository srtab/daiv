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
| Font family        | `"Outfit"`, ui-sans-serif, system-ui, sans-serif            |
| Weights loaded     | 300 (light), 400 (regular), 500 (medium), 600 (semi), 700 (bold), 800 (extra-bold) |
| Body text          | `text-[14px]` regular, `text-gray-300` or `text-gray-400`  |
| Small / meta text  | `text-[13px]` or `text-[12px]`                              |
| Headings           | `font-semibold` or `font-bold`, `text-white` or `text-gray-100` |
| Uppercase labels   | `tracking-[0.15em]` to `tracking-[0.2em]`, `font-semibold` |

### Color Palette

The UI is **dark-mode only**. All colors are applied directly with Tailwind utilities — there are no custom CSS variables beyond `--font-sans`.

| Role             | Value                              | Usage                               |
|------------------|------------------------------------|---------------------------------------|
| Page background  | `bg-[#030712]`                     | `<body>` background                   |
| Surface          | `bg-white/[0.02]`                  | Cards, containers                     |
| Surface hover    | `bg-white/[0.04]`                  | Card hover state                      |
| Surface elevated | `bg-white/[0.06]`                  | Inline code, subtle wells             |
| Border default   | `border-white/[0.06]`             | Card borders, dividers, form inputs   |
| Border hover     | `border-white/[0.12]`             | Interactive hover borders             |
| Border focus     | `border-white/[0.15]`             | Focused inputs                        |
| Text primary     | `text-white`                       | Headings, strong content              |
| Text secondary   | `text-gray-300`                    | Body text                             |
| Text tertiary    | `text-gray-400`                    | Labels, meta, descriptions            |
| Text muted       | `text-gray-500`                    | Placeholders                          |
| Select bg        | `bg-[#0d1117]`                     | `<select>` dropdown background        |

### Semantic Colors

| Semantic   | Border                    | Background              | Text              |
|------------|---------------------------|-------------------------|--------------------|
| Success    | `border-emerald-800/50`   | `bg-emerald-950/80`    | `text-emerald-200` |
| Warning    | `border-amber-800/50`     | `bg-amber-950/80`      | `text-amber-200`   |
| Error      | `border-red-800/50`       | `bg-red-950/80`        | `text-red-200`     |
| Info       | `border-gray-800/50`      | `bg-gray-900/80`       | `text-gray-300`    |

### Spacing Scale

Use Tailwind's default spacing scale. Common values:

- **Page padding**: `px-6`
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

- **Max content width**: `max-w-5xl` (consistent across all pages)
- **Horizontal padding**: `px-6`
- **Responsive breakpoints**: mobile-first; `sm:` (640px), `lg:` (1024px)
- Grid columns: single on mobile, multi-column at `sm:` and `lg:`

## Component Library

All components are Django templates. Reusable partials are **underscore-prefixed** (`_component.html`).

### Buttons

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

`agent`, `beaker`, `bolt`, `chart-bar`, `check`, `check-circle`, `clock`, `code-bracket`, `cog-6-tooth`, `command-line`, `cpu-chip`, `cube`, `diff-to-metadata`, `envelope`, `exclamation-circle`, `exclamation-triangle`, `github`, `gitlab`, `information-circle`, `jobs`, `key`, `link`, `lock-closed`, `providers`, `puzzle-piece`, `sandbox`, `squares-2x2`, `squares-plus`, `users`, `web-fetch`, `web-search`

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
- `<body class="h-full bg-[#030712] font-sans text-white antialiased">`
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
- Maintain focus states on all interactive elements (inputs have ring styles)
- Ensure color contrast: light text (`white`, `gray-300`) on dark backgrounds
- Use `x-cloak` to prevent flash of unstyled Alpine content

## Responsive Design

Mobile-first approach. Common patterns:

```html
<!-- Single column on mobile, 2 columns on sm, 3 on lg -->
<div class="grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-3">

<!-- Stack on mobile, row on sm -->
<div class="flex flex-col gap-4 sm:flex-row sm:items-center">
```

## File Paths Reference

| What                    | Path                                          |
|-------------------------|-----------------------------------------------|
| Tailwind source         | `daiv/static_src/css/input.css`               |
| Compiled CSS            | `daiv/static/css/styles.css`                  |
| Base template           | `daiv/accounts/templates/base.html`           |
| Header partial          | `daiv/accounts/templates/accounts/_header.html` |
| Pagination partial      | `daiv/accounts/templates/accounts/_pagination.html` |
| Quick link card partial | `daiv/accounts/templates/accounts/_quick_link_card.html` |
| Default field template  | `daiv/core/templates/core/fields/default.html` |
| Icon template           | `daiv/core/templates/core/icons/_icon.html`   |
| Icon SVGs               | `daiv/core/static/core/img/icons/`            |
| Icon template tag       | `daiv/core/templatetags/icon_tags.py`         |
| Repo search component   | `daiv/codebase/static/codebase/js/repo-search.js` |
