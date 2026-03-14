# Example: Simple Plan — CSS Bug Fix

**User request:** "The dropdown menu in the navbar overlaps the content below it on mobile. Plan a fix."

**Complexity assessment:** Simple. Single visual bug, isolated to CSS, no backend impact.

**Exploration performed:** Read `src/components/Navbar.css`, `src/components/Navbar.jsx`. Grepped for `.dropdown` across the stylesheet. Checked existing z-index values in the project.

---

# Fix: Navbar Dropdown Overlap on Mobile

## What's Changing

The `.dropdown-menu` is missing `z-index` and uses `position: relative` instead of `position: absolute`, causing it to push content down rather than float above it on viewports below 768px.

## Changes

- `src/components/Navbar.css` (line ~83) — change `.dropdown-menu` to `position: absolute` and add `z-index: 100` inside the `@media (max-width: 768px)` block
- `src/components/Navbar.css` (line ~12) — if `overflow: hidden` on `.navbar` clips the dropdown, change to `overflow: visible`
- `src/components/Navbar.test.jsx` — add snapshot test for mobile dropdown render

## Notes

- The existing modal uses `z-index: 200`, so 100 is safe.
- Desktop is unaffected — fix is scoped to the mobile media query.
- No JavaScript changes needed.
